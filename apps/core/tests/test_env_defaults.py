"""Guardrail: the code default and `.env.example` must not drift apart.

`.env.example` is what an operator copies. The pydantic default in
`config/settings/env.py` is what they get when they *don't* — a fresh
`docker compose up` with no `.env` at all, which is exactly the path the
getting-started docs walk. Those two are the same promise made twice, and
nothing was checking they said the same thing.

They didn't. `.env.example` said `ADMIN_PANEL_URL_PATH=ops/`; the code
default said `admin/`. Every doc, every `or "ops/"` fallback in the code,
`templates/ops/` and the nav tests said `ops`. So a lab instance brought
up without a `.env` served its ops UI from `/admin/` — 200 there, 404 at
the `/ops/` every document named — and the `/setup` wizard's Finish
button cheerfully landed on a URL no page mentioned.

The divergence is only visible when you boot without a `.env`, which is
the one configuration nobody develops against and every newcomer uses.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO / ".env.example"

#: Settings where a silent disagreement changes where the app *is*, or
#: whether it is reachable — not merely how it behaves. Add sparingly:
#: plenty of keys are legitimately blank in the example and defaulted in
#: code (secrets, hostnames), and this list is not meant to grow into a
#: full mirror of the settings model.
MUST_AGREE = ["ADMIN_PANEL_URL_PATH", "SECURITY_TXT_EXPIRES_DAYS"]


def _example_values() -> dict[str, str]:
    """Parse `.env.example` into {key: value}. Deliberately dumb — no
    dotenv dependency, no interpolation, no `export` handling. If the
    example file ever needs those, this guardrail should be revisited
    rather than quietly taught to handle them."""
    out: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if match:
            out[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return out


def test_env_example_exists() -> None:
    """Without it the rest of this file would vacuously pass."""
    assert ENV_EXAMPLE.is_file(), f"{ENV_EXAMPLE} is missing"


@pytest.mark.parametrize("key", MUST_AGREE)
def test_code_default_matches_env_example(key: str) -> None:
    from config.settings.env import Settings

    example = _example_values()
    assert key in example, (
        f"{key} is in MUST_AGREE but absent from .env.example — either add "
        f"it there or drop it from the list."
    )

    field = Settings.model_fields[key]
    assert str(field.default) == example[key], (
        f"{key} disagrees: .env.example says {example[key]!r}, the code "
        f"default in config/settings/env.py says {field.default!r}. An "
        f"operator who copies the example and one who doesn't must land in "
        f"the same place."
    )


def test_blank_security_txt_expires_days_uses_the_code_default(monkeypatch) -> None:
    """Coolify (and a copied `.env` that left the line blank) injects
    `SECURITY_TXT_EXPIRES_DAYS=""`. pydantic-settings treats that empty
    string as a value, not unset, so the `int = 365` default never applies
    and boot raises `int_parsing` — the web container crash-loops on first
    `./dev.sh up`. Empty must mean the documented 365-day default.
    """
    from config.settings.env import Settings

    monkeypatch.setenv("SECURITY_TXT_EXPIRES_DAYS", "")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.SECURITY_TXT_EXPIRES_DAYS == 365


def test_ops_ui_default_is_not_the_most_scanned_path_on_the_web() -> None:
    """`ADMIN_PANEL_URL_PATH` exists to move the panel off a guessable
    URL — the settings block is literally headed "Admin URL hardening".
    Defaulting it to `admin/` cancels the feature for everyone who never
    sets it, which is the default population."""
    from config.settings.env import Settings

    default = str(Settings.model_fields["ADMIN_PANEL_URL_PATH"].default).strip("/")
    assert default != "admin", (
        "ADMIN_PANEL_URL_PATH defaults to 'admin/' again. The setting is "
        "URL hardening; the default has to be a path a scanner does not "
        "try first."
    )
