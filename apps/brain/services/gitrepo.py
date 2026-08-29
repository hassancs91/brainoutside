"""The server's clone of the brain repo — the single source of truth.

Design contract (PLAN.md §4):
- The clone lives on a volume shared by web + mcp + worker containers.
- EVERY mutating git operation runs under `repo_lock()` — a file lock on
  the shared volume, so the single-writer guarantee holds across
  containers, not just threads.
- Bootstrap is idempotent: an empty/invalid directory becomes a fresh
  clone; a valid clone is reused untouched (Coolify volume-rename safety)
  — but ONLY when its `origin` is the configured repo. Reuse used to be
  unconditional, which meant changing `BRAIN_REPO_URL` silently kept
  serving the previous brain: no error, no warning, just the wrong mind.
  That was survivable while the URL lived in env (you set it once, at
  deploy time); it is not survivable now that the setup wizard makes it
  a field someone can edit. Mismatch fails loudly and `replace_clone()`
  is the repair.
- After any clone/pull, `verify_contract()` fails LOUDLY if the operating
  contract (CLAUDE.md, skills, lenses) is missing from the clone — a
  server without the contract must not serve.
- The standing remote credential is the READ-ONLY deploy key
  (`BRAIN_GIT_SSH_KEY_PATH`). The write credential arrives only in the
  M2 approval handler, worker-side.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from django.conf import settings
from filelock import FileLock, Timeout

log = logging.getLogger(__name__)

#: Paths (relative to the clone root) that MUST exist for the server to
#: consider the clone a usable brain. Kept in sync with the brain repo's
#: layout — if the contract grows, extend here deliberately.
CONTRACT_PATHS: tuple[str, ...] = (
    "CLAUDE.md",
    "INDEX.md",
    ".claude/skills/mind-feeder/SKILL.md",
    ".claude/skills/mind-feeder/server-mode.md",
    ".claude/skills/mind-reader/SKILL.md",
    ".claude/skills/mind-reader/server-mode.md",
    ".claude/skills/content-writer/SKILL.md",
    "lenses",
    "identity",
    "knowledge",
)

#: The contract THIS server speaks, matching the `contract-version` in
#: `brain-template/CLAUDE.md` (a test pins them together). A brain is a
#: copy of that template and never auto-updates — we have no push access
#: to it — so this constant and the number in someone's clone are the
#: only way to notice they have drifted apart.
#:
#: Bump when an existing brain would be SERVED DIFFERENTLY by this
#: server. An additive section that defaults to off changes nothing for a
#: brain that omits it and is not a bump; a renamed field, a new required
#: one, or a changed default is.
CONTRACT_VERSION = "1.0"

LOCK_TIMEOUT_SECONDS = 120


class BrainRepoError(RuntimeError):
    """Raised when the clone is unusable (missing, invalid, or contractless)."""


def repo_dir() -> Path:
    return Path(settings.BRAIN_REPO_DIR)


def configured_url() -> str:
    """The brain remote this server is supposed to serve.

    Env/compose wins (it is read through pydantic, so a `.env` file counts
    as env too); otherwise the value the setup wizard stored. Nothing here
    moves an existing clone — `origin_probe()` is what notices that they
    have drifted apart.
    """
    env_value = (settings.BRAIN_REPO_URL or "").strip()
    if env_value:
        return env_value
    try:
        from apps.brainconfig import services as cfg

        return cfg.get("BRAIN_REPO_URL").strip()
    except Exception:  # pragma: no cover - DB down / apps not ready
        log.warning("brain: could not read BRAIN_REPO_URL from settings", exc_info=True)
        return ""


def _lock_path() -> Path:
    # Sibling of the clone so it lives on the same shared volume but is
    # never part of the git working tree.
    d = repo_dir().parent
    d.mkdir(parents=True, exist_ok=True)
    return d / "brain-repo.lock"


@contextmanager
def repo_lock(timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Cross-container single-writer lock for ALL mutating git operations."""
    lock = FileLock(str(_lock_path()))
    try:
        with lock.acquire(timeout=timeout):
            yield
    except Timeout as exc:
        raise BrainRepoError(
            f"Could not acquire brain-repo lock within {timeout}s — "
            "another git operation is stuck or long-running."
        ) from exc


def _git_env() -> dict[str, str] | None:
    """Extra env for git subprocesses; wires the deploy key when configured.

    The key may be an operator-mounted file or one the app generated and
    holds encrypted — `gitcreds.ssh_key_path()` owns that precedence.
    """
    from apps.brain.services import gitcreds

    key_path = gitcreds.ssh_key_path()
    if not key_path:
        return None
    import os

    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    )
    return env


#: Credentials embedded in URLs (tokenized push/fetch, M2.5) must never
#: reach error strings — they land in Feed.error, Events, logs, and the
#: ops UI. Scrub `scheme://ANYTHING@` down to the token marker.
_URL_CRED_RE = re.compile(r"(?<=://)[^@/\s]+@")


def _redact(text: str) -> str:
    return _URL_CRED_RE.sub("***@", text)


def _git(*args: str, cwd: Path | None = None, timeout: int = 300) -> str:
    """Run a git command; raise BrainRepoError with REDACTED stderr on
    failure — argv may carry a one-shot tokenized URL."""
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=_git_env(),
            capture_output=True,
            # Explicit, not `text=True` alone: bare text decodes with the
            # host locale, which on a Windows host is cp1252 — git's UTF-8
            # output (any em dash, any accented name) arrives mangled.
            # The containers are UTF-8 and never noticed. `replace` so a
            # stray non-UTF-8 byte degrades one character instead of
            # raising an exception no caller of _git() expects.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise BrainRepoError("git binary not found in this container/image.") from exc
    except subprocess.TimeoutExpired as exc:
        raise BrainRepoError(f"git {_redact(' '.join(args))} timed out after {timeout}s.") from exc
    if proc.returncode != 0:
        raise BrainRepoError(
            _redact(
                f"git {' '.join(args)} failed (exit {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        )
    return proc.stdout.strip()


def run(*args: str, timeout: int = 300) -> str:
    """Public git primitive against the clone, NO lock taken. `repo_lock()`
    is not reentrant across instances, so multi-step sequences (the M2.5
    approval handler) hold ONE lock and drive the steps through this."""
    return _git(*args, cwd=repo_dir(), timeout=timeout)


def is_valid_repo() -> bool:
    d = repo_dir()
    if not (d / ".git").is_dir():
        return False
    try:
        _git("rev-parse", "--is-inside-work-tree", cwd=d)
        return True
    except BrainRepoError:
        return False


# ---- origin identity ----------------------------------------------------
#
# `git@github.com:me/brain.git`, `https://github.com/me/brain` and
# `ssh://git@github.com/me/brain.git` are the SAME repository. The setup
# wizard makes this a live concern rather than a theoretical one: it hands
# out an SSH deploy key, so a brain first cloned over HTTPS legitimately
# switches to an SSH remote. Comparing raw strings would call that a repo
# swap and refuse to boot.
#
# So comparison happens on a canonical `host/owner/name`. A false match
# across two genuinely different repos would need someone to own the same
# path on the same host; a false MISmatch would take a working server down
# on a cosmetic URL difference. The former is the safer error.

_SCP_LIKE_RE = re.compile(r"^(?P<user>[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")


def normalize_remote(url: str) -> str:
    """Reduce a git remote URL to `host/path`, lowercased, sans credentials.

    Returns "" for an empty input. Non-URL inputs (local paths, used by
    tests and by anyone mounting a clone directly) come back POSIX-slashed
    and otherwise untouched, so two spellings of one path still match.
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    from urllib.parse import urlsplit

    host = ""
    path = raw
    if "://" in raw:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        path = parts.path
    else:
        m = _SCP_LIKE_RE.match(raw)
        # A bare Windows path ("D:/repos/x") also matches the scp-like
        # shape; a single-letter "host" is the giveaway that it isn't one.
        if m and len(m.group("host")) > 1:
            host = m.group("host").lower()
            path = m.group("path")

    path = path.replace("\\", "/").strip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")]
    # GitHub/GitLab treat owner and repo names case-insensitively.
    return f"{host}/{path}".lower() if host else path.lower()


def origin_url() -> str:
    """The clone's `origin`, with any embedded credential scrubbed."""
    try:
        return _redact(_git("remote", "get-url", "origin", cwd=repo_dir()))
    except BrainRepoError:
        return ""


def origin_probe() -> dict[str, object]:
    """Does the clone on disk point at the repo we are configured to serve?

    `ok` is True when they match AND when there is nothing to compare
    (no configured URL, or no clone yet) — this answers "is the clone
    WRONG", so "no opinion" is not a failure.
    """
    configured = configured_url()
    if not configured or not is_valid_repo():
        return {"ok": True, "configured": _redact(configured), "actual": ""}
    actual = origin_url()
    if not actual:
        # A clone with no `origin` (someone `git init`'d the volume) can't
        # be pulled or pushed, so it is a mismatch, not an unknown.
        return {
            "ok": False,
            "configured": _redact(configured),
            "actual": "",
            "reason": "the clone has no `origin` remote",
        }
    return {
        "ok": normalize_remote(actual) == normalize_remote(configured),
        "configured": _redact(configured),
        "actual": actual,
    }


def _assert_origin_matches() -> None:
    probe = origin_probe()
    if probe["ok"]:
        return
    raise BrainRepoError(
        f"The clone at {repo_dir()} is not the configured brain. "
        f"BRAIN_REPO_URL is {probe['configured']!r} but the clone's origin "
        f"is {probe['actual'] or 'missing'!r}. Refusing to serve — the "
        "alternative is silently answering every question from the wrong "
        "mind. Either point the configuration back at the original repo, "
        "or use the 'Replace the clone' repair action to re-clone from the "
        "new URL (which discards the local copy)."
    )


def head_sha() -> str:
    return _git("rev-parse", "HEAD", cwd=repo_dir())


def verify_contract() -> None:
    """Fail loudly when the clone is missing the operating contract.

    Deliberately says nothing about `contract-version`: this is the
    function that refuses to serve, and `/readyz` gates on it. A brain
    written against an older contract is still a brain, and taking the
    server down over a number would be a worse failure than the drift.
    `contract_version_probe()` is the warn-only counterpart.
    """
    d = repo_dir()
    missing = [p for p in CONTRACT_PATHS if not (d / p).exists()]
    if missing:
        raise BrainRepoError(
            "Brain clone is missing contract paths — refusing to serve. "
            f"Missing: {', '.join(missing)}. "
            "Did the brain repo push include CLAUDE.md/.claude/lenses?"
        )


def read_contract_version(text: str) -> str:
    """The `contract-version` declared in a CLAUDE.md, "" when absent.

    Never raises: an absent, keyless or malformed frontmatter block all
    read as undeclared, which the caller reports as "unknown" rather than
    treating as current. The value is stringified because
    `contract-version: 1.0` unquoted is a YAML *float*, and a version
    that renames itself to "1" on the way through is worse than none.
    """
    # Deferred: `indexer` imports this module at module scope, and the
    # five-dialect frontmatter parser lives there. Reusing it beats a
    # second parser that drifts.
    from apps.brain.services.indexer import parse_frontmatter

    try:
        fm, _body, _malformed = parse_frontmatter(text)
    except Exception:  # pragma: no cover - parse_frontmatter swallows its own
        return ""
    value = fm.get("contract-version")
    if value is None:
        return ""
    return str(value).strip()


def _version_tuple(value: str) -> tuple[int, ...] | None:
    parts = value.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def contract_version_probe() -> dict[str, str]:
    """Compare the clone's declared contract to the one we speak.

    Warn-only by construction — it returns a state, never raises, and no
    caller is allowed to turn any state into a refusal. States:
    `ok`, `older`, `newer`, `unknown` (undeclared or unparseable).
    """
    info = {"server": CONTRACT_VERSION, "brain": "", "state": "unknown"}
    try:
        text = (repo_dir() / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    declared = read_contract_version(text)
    info["brain"] = declared
    if not declared:
        return info
    theirs, ours = _version_tuple(declared), _version_tuple(CONTRACT_VERSION)
    if theirs is None or ours is None:
        return info
    if theirs == ours:
        info["state"] = "ok"
    elif theirs < ours:
        info["state"] = "older"
    else:
        info["state"] = "newer"
    return info


def bootstrap() -> dict[str, str]:
    """Idempotent: clone when absent/invalid, reuse when valid. Verified either way."""
    url = configured_url()
    d = repo_dir()
    with repo_lock():
        if is_valid_repo():
            _assert_origin_matches()
            action = "reused"
        else:
            if not url:
                raise BrainRepoError(
                    "BRAIN_REPO_URL is not set and no valid clone exists at "
                    f"{d} — cannot bootstrap."
                )
            if d.exists() and any(d.iterdir()):
                raise BrainRepoError(
                    f"{d} exists, is non-empty, and is not a valid git repo — "
                    "refusing to clobber it. Inspect and remove it manually."
                )
            d.parent.mkdir(parents=True, exist_ok=True)
            log.info("brain: cloning %s -> %s", url, d)
            _git("clone", "--single-branch", url, str(d))
            action = "cloned"
        verify_contract()
        sha = head_sha()
    log.info("brain: bootstrap %s at %s", action, sha)
    return {"action": action, "head": sha, "dir": str(d)}


def rebase_in_progress() -> bool:
    """True when a rebase was started and never finished or aborted."""
    g = repo_dir() / ".git"
    return (g / "rebase-merge").exists() or (g / "rebase-apply").exists()


def abort_rebase() -> bool:
    """Undo a half-finished rebase. Returns True if there was one.

    Nothing in this codebase ever ran `rebase --abort`, so a conflicting
    pull wedged the clone permanently: `.git/rebase-merge` stayed put,
    every later pull failed with "there is already a rebase-merge
    directory", the working tree read dirty, and `replace_clone` — the
    documented repair — refused for exactly that reason. The brain
    served whatever it had at the time, forever.

    Abort, not `reset --hard`: the pre-rebase state can hold local
    commits that exist nowhere else (an approval that committed but
    failed to push), and discarding those is the thing `replace_clone`
    guards against. `--autostash` restores its own stash on abort.
    """
    if not rebase_in_progress():
        return False
    try:
        _git("rebase", "--abort", cwd=repo_dir())
        log.warning("brain: aborted an interrupted rebase in the clone")
    except BrainRepoError:
        log.exception("brain: `rebase --abort` failed; the clone needs manual repair")
    return True


def pull_rebase() -> dict[str, str]:
    """Fetch + rebase under the lock. Returns old/new HEAD for sync logging."""
    d = repo_dir()
    with repo_lock():
        if not is_valid_repo():
            raise BrainRepoError(f"No valid clone at {d} — run bootstrap first.")
        # Clear a rebase an earlier run left behind, so an already-wedged
        # install heals on the next sync rather than needing a human.
        abort_rebase()
        old = head_sha()
        try:
            _git("pull", "--rebase", "--autostash", cwd=d)
        except BrainRepoError as exc:
            if abort_rebase():
                raise BrainRepoError(
                    f"{exc} The rebase was aborted, so the clone is usable and "
                    f"back at {old[:12]}. This usually means the clone holds "
                    "commits that conflict with origin — check whether an "
                    "approved feed committed here but never pushed."
                ) from exc
            raise
        new = head_sha()
        verify_contract()
    return {"old": old, "new": new, "changed": str(old != new).lower()}


def status_probe() -> dict[str, object]:
    """Cheap health snapshot for /readyz and the dashboard tile."""
    try:
        valid = is_valid_repo()
        info: dict[str, object] = {"valid": valid, "dir": str(repo_dir())}
        if valid:
            info["head"] = head_sha()
            info["rebase_in_progress"] = rebase_in_progress()
            missing = [p for p in CONTRACT_PATHS if not (repo_dir() / p).exists()]
            info["contract_ok"] = not missing
            if missing:
                info["contract_missing"] = missing
            # Reported beside `contract_ok`, never folded into it: readyz
            # gates on that flag and a version drift must not close it.
            info["contract_version"] = contract_version_probe()
            origin = origin_probe()
            info["origin_ok"] = origin["ok"]
            if not origin["ok"]:
                info["origin"] = origin
        return info
    except BrainRepoError as exc:  # pragma: no cover - defensive
        return {"valid": False, "error": str(exc)}


def local_only_commits() -> int:
    """Commits on HEAD that the tracked upstream branch doesn't have.

    Non-zero means the brain on disk is ahead of GitHub — approvals
    committed but never pushed. Returns 0 when there is no upstream to
    compare against (a detached or untracked branch can't be "ahead").
    """
    try:
        out = _git("rev-list", "--count", "@{u}..HEAD", cwd=repo_dir())
        return int(out or 0)
    except (BrainRepoError, ValueError):
        return 0


def working_tree_dirty() -> bool:
    try:
        return bool(_git("status", "--porcelain", cwd=repo_dir()))
    except BrainRepoError:  # pragma: no cover - defensive
        return False


def replace_clone(*, force: bool = False) -> dict[str, str]:
    """Discard the clone on disk and re-clone from the configured URL.

    The repair for an origin mismatch. Destructive by nature, so it
    refuses when the existing clone holds work that exists nowhere else —
    unpushed commits or uncommitted changes — unless explicitly forced.
    That check is the whole reason this isn't just `rm -rf`: an approval
    that committed but failed to push lives only here.
    """
    url = configured_url()
    if not url:
        raise BrainRepoError(
            "BRAIN_REPO_URL is not set — there is nothing to re-clone from."
        )
    d = repo_dir()
    with repo_lock():
        if is_valid_repo() and not force:
            ahead = local_only_commits()
            dirty = working_tree_dirty()
            if ahead or dirty:
                bits = []
                if ahead:
                    bits.append(f"{ahead} commit(s) not pushed to its origin")
                if dirty:
                    bits.append("uncommitted changes")
                raise BrainRepoError(
                    f"The clone at {d} has {' and '.join(bits)}. Replacing it "
                    "would destroy work that exists nowhere else. Push or "
                    "discard those changes first, or re-run with force."
                )
        if d.exists():
            shutil.rmtree(d)
        d.parent.mkdir(parents=True, exist_ok=True)
        log.warning("brain: replacing clone at %s from %s", d, _redact(url))
        _git("clone", "--single-branch", url, str(d))
        verify_contract()
        _assert_origin_matches()
        sha = head_sha()
    log.info("brain: clone replaced, now at %s", sha)
    return {"action": "replaced", "head": sha, "dir": str(d)}
