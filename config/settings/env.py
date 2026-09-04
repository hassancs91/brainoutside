"""Typed env-var loader.

Replaces ad-hoc `os.environ.get()` calls scattered across `settings/`.
Values come from the OS env first, then `.env`, then declared defaults.
Required-in-prod fields are enforced by `assert_prod_safe()`, which is
called from `prod.py` so boot fails fast (`ImproperlyConfigured`) rather
than at first request.

Sensitive values use `SecretStr` so they redact in `__repr__` and any log
that interpolates the Settings object.

Usage:
    from config.settings.env import settings
    SECRET_KEY = settings.SECRET_KEY.get_secret_value()
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Project-wide typed settings."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # tolerate unknown env vars without failing boot
    )

    # ----- Core -----
    APP_NAME: str = "{{APP_NAME}}"
    DJANGO_SETTINGS_MODULE: str = "config.settings.dev"

    # ----- Brain repo (my-brain-web-app) -----
    # All default-empty; base.py treats empty string as unset (Coolify
    # injects every compose ${VAR} as "" — never distinguish unset/empty).
    BRAIN_REPO_URL: str = ""
    BRAIN_REPO_DIR: str = ""
    BRAIN_VIEWS_DIR: str = ""
    # Small persisted volume for state the app generates and must never
    # lose — today just the boot secrets (see `boot_secrets.py`). Kept
    # separate from the clone (git-managed) and the snapshots
    # (rebuildable) precisely because this one is neither.
    BRAIN_STATE_DIR: str = ""
    # When set, git talks to origin via this SSH key (the server's
    # read-only deploy key). Empty in dev — git uses the local credential
    # helper for the https URL.
    BRAIN_GIT_SSH_KEY_PATH: str = ""
    # HMAC secret for POST /webhooks/github. Empty = webhook disabled
    # (403 on every delivery) — never open.
    GITHUB_WEBHOOK_SECRET: SecretStr = SecretStr("")
    # Cap on one feed proposal's content (pasted or fetched), in KB.
    # Env-tunable because long video transcripts are a legit payload
    # (grill A12). Pasted content over the cap is rejected; fetched
    # content is truncated at the cap with a flag.
    FEED_PAYLOAD_MAX_KB: int = 512
    # WRITE credential for the M2.5 approval handler (grill C13: split
    # credentials — the standing clone/sync path stays on the read-only
    # deploy key). Set ONLY in the worker container: either the
    # fine-grained PAT itself, or a path to a file holding it (compose
    # secrets style; the path belongs on the SDK agents' deny list).
    # Both empty → pushes go to `origin` with ambient credentials (dev).
    BRAIN_GIT_WRITE_PAT: SecretStr = SecretStr("")
    BRAIN_GIT_WRITE_PAT_PATH: str = ""
    # Identity on the `feed:` commits the approval handler makes. Generic
    # defaults — each deployment sets its own (OPEN-SOURCE.md 5.1).
    BRAIN_COMMIT_NAME: str = "brain-app"
    BRAIN_COMMIT_EMAIL: str = "brain-app@localhost"

    # ----- Public-facing identifiers (rendered into /docs/guide/* pages) -----
    # These four feed `apps.docs.services.guides._resolve_placeholders` —
    # every {{ NAME }} token in the markdown under `apps/docs/guides/` is
    # substituted at render time so the public guides reflect THIS deploy's
    # hostnames, not a placeholder shipped in the template. Defaults are
    # obvious "must configure" values rather than blank so a fresh clone
    # still renders complete sentences.
    #   - PUBLIC_BASE_URL: the API host customers call (in curl + SDK snippets)
    #   - SUPPORT_EMAIL:   where customers email a `request_id` after a 5xx
    #   - STATUS_URL:      optional public status page; blank → omitted
    #   - PUBLIC_REPO_URL: optional GitHub URL for webhook receiver recipes
    PUBLIC_BASE_URL: str = "https://api.example.com"
    SUPPORT_EMAIL: str = "support@example.com"
    STATUS_URL: str = ""
    PUBLIC_REPO_URL: str = ""
    SECRET_KEY: SecretStr = SecretStr("change-me-dev-only-not-for-prod")
    DEBUG: bool = False
    ALLOWED_HOSTS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"]
    )

    # ----- Admin URL hardening -----
    # `ops/`, not `admin/`. This default is the one an operator who never
    # sets the var actually gets, so it has to match what everything else
    # says the ops UI is: `.env.example`, every doc, every `or "ops/"`
    # fallback in the code, `templates/ops/`, the nav tests. It said
    # `admin/` and a fresh deploy served the panel from a path no
    # document mentioned.
    #
    # It is also the whole point of the setting. `/admin/` is the most
    # scanned path on the web; defaulting an admin panel there and
    # calling the setting "URL hardening" is a contradiction.
    ADMIN_PANEL_URL_PATH: str = "ops/"
    DJANGO_ADMIN_URL_PATH: str = "_django-admin-CHANGE-ME/"
    ADMIN_IP_ALLOWLIST: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ----- Reverse proxy / real client IP -----
    # Behind a proxy, REMOTE_ADDR is the proxy. Every per-IP control
    # (admin-login lockout, honeypot counter, ADMIN_IP_ALLOWLIST) then
    # buckets the whole internet together — see
    # `apps.core.security.client_ip` for why that inverts the lockout.
    # Name the header your proxy sets ("CF-Connecting-IP" for Cloudflare,
    # "X-Forwarded-For" for a plain reverse proxy) AND the addresses that
    # proxy connects FROM. The header is honoured only on requests whose
    # TCP peer is in TRUSTED_PROXY_IPS, so it can't be spoofed by a
    # direct-to-origin caller. Both or neither: `assert_prod_safe()`
    # refuses boot on half a configuration.
    TRUSTED_PROXY_IP_HEADER: str = ""
    TRUSTED_PROXY_IPS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Per-minute cap for callers with no API key and no operator session,
    # bucketed on the resolved client IP. A backstop for paths nobody has
    # explicitly sized — an endpoint intended to serve the public should
    # get its own Consumer key and limit rather than lean on this.
    ANONYMOUS_RATE_LIMIT_PER_MIN: int = 20

    # ----- Dev-only direct-login shortcut (NEVER enable in prod) -----
    # When True AND DEBUG is True, exposes GET /auth/dev-login/ which logs you
    # straight in as a staff superuser AND pre-clears the staff-2FA gate for
    # that session — no email round-trip, no TOTP. Pure local-dev convenience
    # for building/testing the admin panel + endpoints. BOTH switches must be
    # on: DEBUG alone or this flag alone does nothing. `assert_prod_safe()`
    # REFUSES BOOT when this is True, so a prod deploy that sets it crashes at
    # startup instead of shipping an unauthenticated admin bypass.
    DEV_LOGIN_ENABLED: bool = False
    # Email the shortcut signs you in as. Created if missing and force-promoted
    # to staff+superuser on each hit. Override per-request with `?email=...`.
    DEV_LOGIN_EMAIL: str = "dev@localhost"

    # ----- Database -----
    DATABASE_URL: str = ""
    # CONN_MAX_AGE wires straight into `DATABASES["default"]["CONN_MAX_AGE"]`.
    # Default 60s keeps connections warm across requests on a stock deploy.
    # Set to 0 if you front Postgres with a transaction-mode pooler
    # (PgBouncer and friends) — connections aren't sticky across requests
    # there, so persistent connections poison the pool. This stack ships
    # no such service; the comment here used to point at an opt-in
    # compose service and a scaling doc, neither of which exists.
    # Env-driven so operators A/B-testing pool sizes don't
    # need a code edit + redeploy.
    DB_CONN_MAX_AGE: int = 60

    # ----- ASGI runtime (gunicorn + uvicorn workers) -----
    # These three control the gunicorn invocation in the Dockerfile / Procfile /
    # docker-compose / Railway / Fly / Render configs. Tune per-machine size
    # without rebuilding the image:
    #   - WEB_CONCURRENCY: worker process count. Rule of thumb `2 * CPU + 1`
    #     for sync workers; uvicorn workers are async so a lower count (2-4)
    #     is usually plenty.
    #   - WEB_GRACEFUL_TIMEOUT: seconds gunicorn waits for in-flight requests
    #     to drain on SIGTERM before sending SIGKILL. Bump for endpoints that
    #     legitimately block on slow upstreams.
    #   - WEB_TIMEOUT: per-request hard ceiling. Any worker that doesn't reply
    #     within this window is killed and restarted.
    # Surfaced on Settings so `manage.py doctor` and the admin diagnostics
    # page can echo the effective values back to operators.
    WEB_CONCURRENCY: int = 3
    WEB_GRACEFUL_TIMEOUT: int = 30
    WEB_TIMEOUT: int = 60

    # ----- Cache / queue -----
    REDIS_URL: str = "redis://localhost:6379/0"

    # ----- Q2 worker tuning (UPDATES.md #8) -----
    # Cluster-wide defaults for the django-q2 worker, tuned for AI workloads
    # (Replicate / fal.ai / OpenAI / Anthropic image-video-audio generation,
    # which routinely take 1-5 minutes per call). Per-endpoint overrides via
    # `@endpoint(... async_timeout_seconds=N)` still win — drop them for
    # endpoints that are genuinely fast and you want a tighter ceiling.
    #   - Q_TASK_TIMEOUT_SECONDS: hard SIGTERM after this many seconds. 600s
    #     (10 min) covers the long-tail of AI generation jobs out of the box.
    #     Operators with shorter workloads can override per-endpoint with
    #     `async_timeout_seconds=N` on the decorator, or lower the cluster
    #     default here.
    #   - Q_ACK_TIMEOUT_SECONDS: how long Q2 waits for a worker to ack a
    #     task before another worker picks it up. **Load-bearing invariant**:
    #     this MUST be strictly greater than Q_TASK_TIMEOUT_SECONDS, otherwise
    #     a second worker can grab a task while the first is still running —
    #     double execution → users pay upstream APIs (Replicate / fal.ai /
    #     OpenAI) twice for the same job. The `_q_ack_gt_timeout` validator
    #     below refuses boot if the invariant is violated. The default 720s
    #     is 1.2× the task timeout, matching the recommended ratio.
    #   - Q_WORKER_COUNT: how many concurrent workers per Q2 cluster. 4 is
    #     the AI-workload baseline; tune up with CPU/memory headroom, down
    #     for tight containers.
    #   - Q_RECYCLE_AFTER_TASKS: recycle a worker after this many tasks to
    #     bound memory drift from third-party libs.
    Q_TASK_TIMEOUT_SECONDS: int = 600
    Q_ACK_TIMEOUT_SECONDS: int = 720
    Q_WORKER_COUNT: int = 4
    Q_RECYCLE_AFTER_TASKS: int = 500

    # bumping the version invalidates every cache key on the
    # next deploy (the prefix becomes `mcp:<env>:v<N>:` so old keys are
    # orphaned and TTL out). Use this when a release changes the cached
    # value's shape and you can't tolerate stale-shape reads.
    CACHE_KEY_VERSION: str = "1"
    # env label baked into the cache key prefix so prod and
    # dev never share a key namespace even when pointed at the same
    # Redis instance (rare but happens during staging-against-prod-redis
    # incidents). Defaults to "dev" for safety; prod.py overrides.
    CACHE_ENV: str = "dev"

    # ----- Email -----
    # This server sends almost no mail — `security/alerts.py` is the only
    # sender, and `base.py` pins EMAIL_BACKEND to the console backend, so
    # an alert goes to the container's stdout. The SMTP / Postmark /
    # Resend knobs that used to be declared here were never read by
    # anything; wiring a real backend is a deliberate future change, not
    # an env var somebody can set today.
    EMAIL_BACKEND_DRIVER: Literal["console", "dummy", "smtp", "postmark", "resend"] = "console"
    DEFAULT_FROM_EMAIL: str = "noreply@example.com"

    # ----- OAuth -----
    # Public issuer URL announced in /.well-known/oauth-authorization-server
    # and /.well-known/oauth-protected-resource. MUST match the host clients
    # use to reach you (no trailing slash). For local Claude.ai testing via
    # ngrok, set this to your ngrok https URL before starting `make dev`.
    # PROD: `assert_prod_safe()` REFUSES BOOT unless this is a non-localhost
    # https:// origin — a localhost issuer makes MCP OAuth clients hang on
    # token refresh / dynamic client registration.
    OAUTH_ISSUER: str = "http://localhost:8000"

    # Dynamic client registration mode (RFC 7591):
    #   - "anonymous"     → open registration (default; what Claude.ai expects)
    #   - "iat_required"  → caller must present a pre-issued initial-access-token
    #   - "disabled"      → registration endpoint returns 403
    MCP_OAUTH_DCR_MODE: Literal["anonymous", "iat_required", "disabled"] = "anonymous"

    # ----- Security -----
    FIELD_ENCRYPTION_KEY: SecretStr = SecretStr("")
    STAFF_2FA_TIMEOUT: int = 8  # hours
    AUDIT_RETENTION_DAYS: int = 180
    # `True` emits `Content-Security-Policy-Report-Only`
    # (violations reported, never block); `False` enforces. Default `None`
    # is the "operator hasn't set it" sentinel — `base.py` then picks
    # `False` (enforce) and `dev.py` flips to `True` (report-only) so dev
    # gets a forgiving CSP without breaking the dashboard. Setting the
    # env var explicitly wins in BOTH dev and prod.
    CSP_REPORT_ONLY: bool | None = None
    # populated by the operator before going to prod. When
    # blank, /.well-known/security.txt returns 404 (we don't ship a fake
    # contact field).
    SECURITY_TXT_CONTACT: str = ""
    SECURITY_TXT_POLICY_URL: str = ""
    SECURITY_TXT_ENCRYPTION_URL: str = ""
    SECURITY_TXT_EXPIRES_DAYS: int = 365
    # request body size caps. Defaults are conservative;
    # individual endpoints that need more (file uploads to `safe_request`,
    # CSV imports) override per-view via `request.upload_handlers`.
    DATA_UPLOAD_MAX_MEMORY_MB: int = 5
    FILE_UPLOAD_MAX_MEMORY_MB: int = 2

    # ----- Observability -----
    # Read by `doctor` (which warns when it is unset in prod) and by
    # base.py. No Sentry SDK is initialised here — setting it does not
    # start reporting, which is why the sample-rate and environment
    # knobs that used to sit beside it went: they configured nothing.
    SENTRY_DSN: str = ""
    # Google Analytics 4 measurement ID (e.g. "G-XXXXXXXXXX"). Blank (the
    # default) disables analytics entirely: no gtag.js, no cookie-consent
    # banner, and the CSP stays tight (no googletagmanager / google-analytics
    # hosts are allowlisted). When set, gtag.js loads ONLY after the visitor
    # accepts the cookie-consent banner — see templates/partials/_analytics.html
    # + templates/partials/_cookie_consent.html. This is a public client-side
    # identifier that ships in page HTML by design, so it's a plain `str`, not
    # a `SecretStr`.
    GOOGLE_ANALYTICS_ID: str = ""

    # ----- Backend driver registry (protocols) -----
    # The only registry with a registrant. The payment / OAuth-provider /
    # MCP-transport driver names that used to sit here selected between
    # implementations this project does not have.
    STORAGE_DRIVER: str = "filesystem"

    # ----- MCP subprocess -----
    # The Django proxy view at /mcp/ forwards to FastMCP on this loopback host:port.
    # The host-only dev path stays on 127.0.0.1 and the subprocess auto-trusts
    # loopback peers. Docker / k8s deployments split Django and MCP into separate
    # containers/pods so the connection arrives from a bridge IP, not loopback;
    # set `MCP_LOOPBACK_SECRET` below in those deployments and the subprocess
    # trusts callers presenting a matching `X-MCP-Loopback-Secret` header
    # regardless of peer IP.
    MCP_LOOPBACK_HOST: str = "127.0.0.1"
    MCP_LOOPBACK_PORT: int = 9001
    MCP_LOOPBACK_SECRET: SecretStr | None = None

    # ----- URL-based MCP auth -----
    # Master switch for the `/mcp/k/<token>/` surface. When False:
    #   - the URL pattern returns 404 (the view sees the flag and bails
    #     before resolving the token);
    #   - the dashboard "URL tokens" page is hidden;
    #   - already-minted tokens stop authenticating (the proxy view
    #     refuses to dispatch them).
    # Default False here, inverting the PLAN2 default: the token app
    # (`apps.url_mcp_tokens`) is not vendored yet, so the surface resolves
    # ordinary `mcpsk_` bearer keys from the path. That is enough to test
    # a Claude.ai custom connector and NOT enough to run, because the
    # prefix lockout only covers `mcpurl_*` (see
    # `apps.core.security.lockout.extract_url_token_prefix`) and nothing
    # scrubs the token out of `request.path` before the access log.
    # On until you have a reason means a credential in your logs.
    MCP_URL_AUTH_ENABLED: bool = False
    # Default TTL applied by the dashboard mint flow when the user picks
    # "default". 90d matches the PLAN2 spec; operators can lower for
    # stricter rotation. The mint flow always allows shorter/longer
    # values from the dropdown — this is just the default.
    URL_TOKEN_DEFAULT_TTL_DAYS: int = 90

    # ----- Validators -----

    @field_validator(
        "ALLOWED_HOSTS",
        "ADMIN_IP_ALLOWLIST",
        "TRUSTED_PROXY_IPS",
        mode="before",
    )
    @classmethod
    def _csv_to_list(cls, v: Any) -> Any:
        """Accept `host1,host2` as well as a real list (env var → list)."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v
        return v

    @field_validator("SECURITY_TXT_EXPIRES_DAYS", mode="before")
    @classmethod
    def _blank_expires_days_is_unset(cls, v: Any) -> Any:
        """Empty env is unset, not an invalid int.

        `.env.example` used to ship `SECURITY_TXT_EXPIRES_DAYS=` blank, and
        Coolify injects every compose `${VAR}` as `""`. pydantic-settings
        treats that as a value, so the `int = 365` default never applies
        and boot raises `int_parsing`. Blank → the documented default.
        """
        if isinstance(v, str) and not v.strip():
            return 365
        return v

    @model_validator(mode="after")
    def _derive_public_origin(self) -> Settings:
        """Fill `OAUTH_ISSUER` / `PUBLIC_BASE_URL` in from the domain.

        Both are "what host am I?" values, and the operator already told us
        that in `ALLOWED_HOSTS`. Making them derive keeps the required-env
        list at two entries (SETUP-DESIGN.md) instead of four, and the
        derived value beats the shipped placeholder in every case — a docs
        page that says `https://api.example.com` is wrong for every
        deployment that exists.

        Only shipped defaults are replaced; an explicit value always wins.
        """
        host = next(
            (
                h
                for h in self.ALLOWED_HOSTS
                if h.strip() and h.strip() not in ("localhost", "127.0.0.1", "::1", "*")
            ),
            "",
        )
        if not host:
            return self
        origin = f"https://{host.strip().lstrip('.')}"
        if not self.OAUTH_ISSUER.strip() or self.OAUTH_ISSUER.strip() == "http://localhost:8000":
            self.OAUTH_ISSUER = origin
        if not self.PUBLIC_BASE_URL.strip() or self.PUBLIC_BASE_URL.strip() == "https://api.example.com":
            self.PUBLIC_BASE_URL = origin
        return self

    @model_validator(mode="after")
    def _q_ack_gt_timeout(self) -> Settings:
        """Refuse boot when Q_ACK_TIMEOUT_SECONDS ≤ Q_TASK_TIMEOUT_SECONDS.

        Q2 hands a task to a second worker once ack-timeout elapses without
        the first acknowledging completion. If ack-timeout fires before the
        first worker's task-timeout SIGTERM, the task is running on TWO
        workers simultaneously — every upstream API call gets billed twice.

        The check fails at Settings construction (boot time) with a message
        that points at the operator's mistake, so a misconfigured deploy
        crashes during `manage.py runserver` / gunicorn boot rather than
        the next time someone bumps timeout in production.
        """
        if self.Q_ACK_TIMEOUT_SECONDS <= self.Q_TASK_TIMEOUT_SECONDS:
            raise ValueError(
                "Q_ACK_TIMEOUT_SECONDS must be strictly greater than "
                f"Q_TASK_TIMEOUT_SECONDS (got ack={self.Q_ACK_TIMEOUT_SECONDS}, "
                f"task={self.Q_TASK_TIMEOUT_SECONDS}). When ack ≤ task, Q2 "
                "hands the task to a second worker before the first has been "
                "killed — your users pay upstream APIs twice. Recommended: "
                "set Q_ACK_TIMEOUT_SECONDS to roughly 1.2-1.5× Q_TASK_TIMEOUT_SECONDS."
            )
        return self

    # ----- Boot-time prod safety -----

    def assert_prod_safe(self) -> None:
        """Called from `prod.py` to refuse boot on default secrets / weak admin paths."""
        from django.core.exceptions import ImproperlyConfigured

        if self.SECRET_KEY.get_secret_value() in ("", "change-me-dev-only-not-for-prod"):
            raise ImproperlyConfigured(
                "SECRET_KEY must be set to a non-default value in production. "
                "Generate one: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        if not self.ALLOWED_HOSTS:
            raise ImproperlyConfigured(
                "ALLOWED_HOSTS must be set in production (comma-separated env var)."
            )
        admin_path = (self.DJANGO_ADMIN_URL_PATH or "").strip("/")
        if not admin_path or admin_path == "admin" or "CHANGE-ME" in admin_path:
            raise ImproperlyConfigured(
                "DJANGO_ADMIN_URL_PATH must be set to a non-default, non-CHANGE-ME value "
                "in production. Bots scan default admin paths."
            )
        # refuse to boot in prod with field encryption coupled
        # to SECRET_KEY. If FIELD_ENCRYPTION_KEY is unset, base.py falls back
        # to SECRET_KEY for the django-cryptography KDF input — fine in dev,
        # operationally dangerous in prod because rotating SECRET_KEY then
        # destroys every encrypted column. Force operators to set a
        # dedicated key so the two rotation lifecycles stay independent.
        if not self.FIELD_ENCRYPTION_KEY.get_secret_value():
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEY must be set in production. "
                "Generate one: python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        # The dev-login shortcut (`/auth/dev-login/`) is an unauthenticated
        # admin-login bypass — local-development convenience ONLY. Refuse to
        # boot prod with it set so an env var copied from a dev `.env` can
        # never ship the bypass to a public deploy. The view also gates on
        # DEBUG, but this fail-loud guard is the load-bearing one.
        if self.DEV_LOGIN_ENABLED:
            raise ImproperlyConfigured(
                "DEV_LOGIN_ENABLED must be False (unset) in production. It "
                "exposes a passwordless staff-login bypass at /auth/dev-login/ "
                "and is a local-dev convenience only. Remove it from your prod "
                "environment before deploying."
            )
        # Half a proxy configuration is worse than none: the operator
        # believes per-IP controls see real callers while the header is
        # silently ignored (or, with the peers set but no header named,
        # nothing is read at all). Fail at boot rather than let the
        # admin-login lockout stay quietly inverted.
        header, peers = self.TRUSTED_PROXY_IP_HEADER.strip(), self.TRUSTED_PROXY_IPS
        if bool(header) != bool(peers):
            missing, present = (
                ("TRUSTED_PROXY_IPS", "TRUSTED_PROXY_IP_HEADER")
                if header
                else ("TRUSTED_PROXY_IP_HEADER", "TRUSTED_PROXY_IPS")
            )
            raise ImproperlyConfigured(
                f"{present} is set but {missing} is not. Both are required: the "
                "header names where the caller's address is carried, and the "
                "peer list says which proxies may be believed. Without the "
                "peer list the header would be spoofable by anyone reaching "
                "the origin directly, so it is ignored — leaving every per-IP "
                "control (admin-login lockout, honeypot, ADMIN_IP_ALLOWLIST) "
                "bucketing all callers as the proxy."
            )
        # OAUTH_ISSUER is the base for every URL in the OAuth discovery
        # documents. In the TEMPLATE this was a boot-refusal: a localhost
        # issuer makes MCP OAuth clients hang on token refresh, and the
        # failure looks intermittent because cached tokens keep working.
        #
        # This app does not vendor the OAuth flows (`MCP_OAUTH_DCR_MODE` is
        # forced to "off" and `MCP_URL_AUTH_ENABLED` to False in base.py);
        # MCP clients authenticate with an API key. The issuer only appears
        # in 401 hints, so a wrong value is cosmetic, not a hang — and
        # `_derive_public_origin` fills it in from ALLOWED_HOSTS anyway.
        # Refusing to boot over it would add a third required env var
        # (SETUP-DESIGN.md: the target is two), so it warns instead.
        # Only worth saying when it is not simply true. `_derive_public_origin`
        # has already run, and it overwrites a localhost issuer whenever
        # ALLOWED_HOSTS names a real host — so reaching here with a localhost
        # issuer means ALLOWED_HOSTS named none. If the operator listed only
        # loopback, they are running locally on purpose (the getting-started
        # path does exactly this) and a localhost issuer is the correct
        # answer, not a misconfiguration to warn them about three times
        # before they have even seen the wizard.
        #
        # What is still worth flagging is a deployment that looks public but
        # gave us no hostname to derive from — `ALLOWED_HOSTS=*` being the
        # usual way in. There the advice is actionable.
        from urllib.parse import urlparse

        issuer = (self.OAUTH_ISSUER or "").strip()
        issuer_host = urlparse(issuer).hostname or ""
        _LOOPBACK = ("localhost", "127.0.0.1", "::1")
        entries = [h.strip() for h in self.ALLOWED_HOSTS if h.strip()]
        running_locally = bool(entries) and all(h in _LOOPBACK for h in entries)
        if (not issuer or issuer_host in _LOOPBACK) and not running_locally:
            import logging

            logging.getLogger(__name__).warning(
                "OAUTH_ISSUER is %r in production. Harmless here (the OAuth "
                "flows are not vendored — MCP uses API keys), but it means "
                "ALLOWED_HOSTS carries no public hostname to derive it from; "
                "check that ALLOWED_HOSTS is your real domain.",
                issuer or "unset",
            )


# Module-level singleton — settings/* modules read attributes from here.
settings = Settings()  # type: ignore[call-arg]

# Fill in SECRET_KEY / FIELD_ENCRYPTION_KEY / MCP_LOOPBACK_SECRET / the admin
# slug from the persisted state file when the operator hasn't supplied them,
# generating (once) if the file doesn't have them yet. This runs BEFORE
# `assert_prod_safe()` — which is the point: a stock deploy passes those
# checks without a human writing a single secret by hand. Explicit env always
# wins, so an infrastructure-as-code setup is unaffected.
from .boot_secrets import apply_generated_secrets  # noqa: E402

apply_generated_secrets(settings, base_dir=REPO_ROOT)
