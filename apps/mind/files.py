"""Snapshot file access — the only door the serving layers read through.

Everything serves from `/data/brain-views/<tier>/` (PLAN.md §5): if a
file isn't in the caller's tier snapshot, it does not exist for that
caller. That single rule carries the whole visibility model — including
raw/ link-inheritance and the stripped public spans — because the
snapshot builder already enforced it at build time.

`read_skill()` is the exception: it reads directly from the brain clone's
`.claude/skills/` directory, bypassing snapshots. Skills are infrastructure,
not content — all tiers see the same skills.
"""
from __future__ import annotations

import re
from pathlib import Path

from apps.brain.services import snapshots

_SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class SnapshotMiss(ValueError):
    """Requested path absent from the caller's tier snapshot."""


def read(tier: str, relpath: str, *, subdir: str | None = None) -> str:
    """Read `relpath` from the caller's tier snapshot.

    `subdir` additionally confines the read to one directory inside the
    snapshot. Callers that restrict by prefix (`get-raw` serves only
    `raw/`) must pass it: a `startswith("raw/")` test is satisfied by
    `raw/../INDEX.md`, which resolves back out of `raw/` and used to be
    served — skipping the per-entity `tiers.allows` check that `get-note`
    applies, since nothing about a raw read consults the DB.
    """
    base = snapshots.tier_dir(tier).resolve()
    root = (base / subdir).resolve() if subdir else base
    target = (base / relpath).resolve()
    # Traversal guard: the resolved target must stay inside the snapshot,
    # and inside `subdir` when the caller named one.
    if root not in target.parents and target != root:
        raise SnapshotMiss(f"unknown path: {relpath}")
    if not target.is_file():
        raise SnapshotMiss(f"unknown path: {relpath}")
    return target.read_text(encoding="utf-8", errors="replace")


def exists(tier: str, relpath: str, *, subdir: str | None = None) -> bool:
    try:
        read(tier, relpath, subdir=subdir)
        return True
    except SnapshotMiss:
        return False


# ---- skills (bypass snapshots — infrastructure, not content) -----------

_SKILLS_DIR = ".claude/skills"


class SkillMiss(ValueError):
    """Requested skill not found in the brain clone."""


def read_skill(name: str) -> str:
    """Read a skill's SKILL.md from the brain clone.

    Skills live outside the tier-snapshot system: all tiers see the same
    skills because they are infrastructure, not content. The name is
    validated against a strict allowlist to prevent path traversal.
    """
    if not _SKILL_NAME_RE.match(name):
        raise SkillMiss(f"invalid skill name: {name!r}")
    from apps.brain.services import gitrepo

    target = (gitrepo.repo_dir() / _SKILLS_DIR / name / "SKILL.md").resolve()
    skills_root = (gitrepo.repo_dir() / _SKILLS_DIR).resolve()
    if skills_root not in target.parents:
        raise SkillMiss(f"invalid skill path: {name!r}")
    if not target.is_file():
        raise SkillMiss(f"unknown skill: {name!r}")
    return target.read_text(encoding="utf-8", errors="replace")


def list_skills() -> list[dict[str, str]]:
    """List all available skills with name and description.

    Scans `.claude/skills/` in the brain clone, parses each SKILL.md's
    YAML frontmatter for `name` and `description`. Returns a list of
    dicts sorted by name.
    """
    from apps.brain.services import gitrepo

    skills_dir = gitrepo.repo_dir() / _SKILLS_DIR
    if not skills_dir.is_dir():
        return []

    result: list[dict[str, str]] = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.is_file():
            continue
        name = child.name
        description = ""
        try:
            text = skill_file.read_text(encoding="utf-8", errors="replace")
            # Parse frontmatter — supports YAML (---) and JSON ({})
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1:
                    from apps.brain.services.indexer import parse_frontmatter

                    fm, _, _ = parse_frontmatter(text)
                    description = str(fm.get("description", ""))
            elif text.startswith("{"):
                import json

                end = text.find("}")
                if end != -1:
                    fm = json.loads(text[: end + 1])
                    description = str(fm.get("description", ""))
        except Exception:
            pass
        result.append({"name": name, "description": description.strip()})
    return result
