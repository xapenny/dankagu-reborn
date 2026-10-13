"""Application configuration with environment-variable support.

Security model
--------------
Secrets are **never** committed to this repository. ``takasho_key`` and
``jwt_secret`` either come from the environment (or a local ``.env`` file) or
the process refuses to start. This module fails closed: an unset secret is an
error, not a silent default.

Copy ``.env.example`` to ``.env`` and fill in the values before launching.
"""

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("dankagu.config")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class MissingSecretError(RuntimeError):
    """Raised when a required secret has not been configured."""


class Settings(BaseSettings):
    """Application settings with environment variable support.

    Environment variables use the ``DANKAGU_`` prefix, e.g.
    ``DANKAGU_JWT_SECRET``, ``DANKAGU_TAKASHO_KEY_HEX``. A local ``.env`` file
    in the project root is read automatically but never overrides a real
    environment variable.
    """

    model_config = SettingsConfigDict(
        env_prefix="DANKAGU_",
        env_file=_PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Base paths
    base_dir: Path = Field(default=_PROJECT_ROOT)
    data_dir: Path = Field(default=_PROJECT_ROOT / "data")
    certs_dir: Path = Field(default=_PROJECT_ROOT / "certs")
    # hexdata_cf_dir: operator-supplied response templates for common_featureset
    # calls. Not distributed with this repository; see the README.
    hexdata_cf_dir: Path = Field(
        default=_PROJECT_ROOT / "data" / "hexdata" / "common_featureset" / "player_api"
    )
    # hexdata_fes_dir: operator-supplied response templates for fes calls
    # (login_bonus, reconstruction_spot).
    hexdata_fes_dir: Path = Field(default=_PROJECT_ROOT / "data" / "hexdata" / "fes" / "player_api")

    #: Root log level (``DANKAGU_LOG_LEVEL``): DEBUG, INFO, WARNING or ERROR.
    log_level: str = "INFO"

    # Host & ports
    host: str = "0.0.0.0"
    lan_ip: str | None = None
    lcx_port: int = 9443
    lcx_ios_port: int = 443
    grpc_port: int = 50051
    grpc_tls_port: int = 50052
    asset_port: int = 8080
    #: Master switch for the plain-HTTP portal & asset CDN listener
    #: (``DANKAGU_ASSET_SERVER_ENABLED``). When false, ``asset_port`` is not
    #: bound at all. The asset routes stay reachable over HTTPS on the LCX
    #: listeners, which mount the same router; only the plain-HTTP certificate
    #: bootstrap and the ``/status`` shortcut on that port go away.
    asset_server_enabled: bool = True

    # Public identity / certificates
    #: Hostname used for absolute URLs served to the client (announcement
    #: images, redirects). Empty means "derive from lan_ip".
    public_base_url: str = ""
    #: The hostname clients use to reach this server. Added to the generated
    #: certificate's SAN list, e.g. ``DANKAGU_PUBLIC_HOST=danmaku.example.com``.
    public_host: str = ""
    #: Extra comma-separated SAN entries merged on top of ``public_host``.
    extra_cert_dns_names: str = ""

    # Secrets (never commit real values)
    #: 32-byte Takasho transport key used by your client build, as a
    #: 64-character hex string. Not distributed with this project — supply
    #: your own. When empty, the server falls back to the public development
    #: key, which only works with development clients.
    takasho_key_hex: str = ""
    #: HMAC key for signing LCX JWT tokens. Generate with
    #: ``python -c "import secrets; print(secrets.token_urlsafe(48))"``.
    jwt_secret: str = ""

    # Identity / session policy
    #: Extra device identifiers that must never be used as a device identity
    #: because every install shares them (comma separated). The historical LCX
    #: ``storeAccountIdHash`` constant is rejected regardless of this setting.
    shared_device_ids: str = ""
    #: Lifetime of the LCX ``signInSessionToken`` in seconds
    #: (``DANKAGU_SIGN_IN_SESSION_TTL_SECONDS``, default 7 days).
    sign_in_session_ttl_seconds: int = 86400 * 7
    #: When true (default), a request that carries no token and matches an *already
    #: onboarded* account by ``device_account`` re-attaches to that account — the
    #: behaviour a device that reinstalls expects, and the only way such a player gets
    #: their save back without a transfer code.
    #:
    #: Set it to false to force the registration flow on every device: a device match
    #: then creates a fresh player instead, which also means an account that has been
    #: onboarded can no longer be reached by device alone. The 2026-10-13 "two devices,
    #: one account" report was caused by a *token* being reused across devices, which
    #: is refused regardless of this setting (see ``lcx.router`` device binding).
    reuse_device_account_when_onboarded: bool = True
    #: When true, a session token that records no device binding is rejected as soon
    #: as the request carries a device identifier. Tokens issued before device
    #: binding existed (2026-10-13) therefore stop working on a real client and the
    #: player signs in once more. Set it to false only while migrating a client that
    #: legitimately refreshes without any device information.
    require_device_bound_tokens: bool = True

    # Database
    db_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/dankagu"

    # Schema versioning (Alembic). See src/dankagu/db/README.md.
    #: Apply pending migrations during startup, before any listener binds
    #: (``DANKAGU_DB_AUTO_MIGRATE``). A database created before Alembic existed is
    #: *stamped* with the baseline revision instead of being re-created, so
    #: enabling this on a live server never rewrites existing player data. Set it
    #: to false when the deployment script runs ``python -m dankagu.db.migrate``.
    db_auto_migrate: bool = True
    #: Seconds to wait for another instance to release the migration lock
    #: (``DANKAGU_DB_MIGRATE_WAIT_SECONDS``) before startup fails.
    db_migrate_wait_seconds: int = 300

    # Validators
    @field_validator("takasho_key_hex")
    @classmethod
    def _validate_takasho_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            raw = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("DANKAGU_TAKASHO_KEY_HEX must be a hexadecimal string") from exc
        if len(raw) != 32:
            raise ValueError(f"DANKAGU_TAKASHO_KEY_HEX must decode to 32 bytes, got {len(raw)}")
        return value

    # Derived values
    @property
    def takasho_key(self) -> bytes:
        """Decoded 32-byte Takasho transport key, or ``b""`` when unset."""
        return bytes.fromhex(self.takasho_key_hex) if self.takasho_key_hex else b""

    @property
    def takasho_dev_key(self) -> bytes:
        """Public development key shipped with development clients."""
        return b"ZA1Cu0eZosC3o8YTFuGjloxRkCg6ugVv"

    @property
    def public_base(self) -> str:
        """Base URL used for absolute links served to the client.

        Prefers ``public_base_url``, then ``public_host`` (served over HTTPS on
        the LCX port), and finally the LAN IP: on the asset port when the
        plain-HTTP listener is enabled, otherwise on the LCX HTTPS port, whose
        app mounts the same asset router.
        """
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if self.public_host:
            return f"https://{self.public_host}"
        host = self.lan_ip or "127.0.0.1"
        if self.asset_server_enabled:
            return f"http://{host}:{self.asset_port}"
        port = "" if self.lcx_port == 443 else f":{self.lcx_port}"
        return f"https://{host}{port}"

    @property
    def cert_dns_names(self) -> list[str]:
        """DNS names to embed in the generated TLS server certificate.

        Built from ``public_host`` (opt-in via configuration) plus loopback.
        """
        names: list[str] = ["localhost"]
        if self.public_host:
            host = self.public_host.strip().lower()
            names.append(host)
            # Cover subdomains of the host and its parent domains, without
            # wildcarding a public suffix (we stop at the last two labels).
            labels = host.split(".")
            for i in range(1, len(labels) - 1):
                parent = ".".join(labels[i:])
                names.append("*." + parent)
                names.append(parent)
        if self.extra_cert_dns_names:
            names.extend(
                part.strip().lower()
                for part in self.extra_cert_dns_names.split(",")
                if part.strip()
            )
        return list(dict.fromkeys(names))

    @property
    def cert_common_name(self) -> str:
        """Common Name for the generated server certificate.

        Uses ``public_host`` when configured. Otherwise a neutral local name is
        used rather than impersonating a hostname this server does not own.
        """
        return self.public_host.strip().lower() or "DanKagu Local Server"

    # Secret accessors (fail closed)
    def require_jwt_secret(self) -> str:
        """Return the JWT signing secret or raise ``MissingSecretError``.

        Called lazily at first use so importing the package (and running CLI
        helpers) never requires secrets to be present.
        """
        secret = self.jwt_secret.strip()
        if not secret:
            raise MissingSecretError(
                "DANKAGU_JWT_SECRET is not set. LCX token signing is disabled.\n"
                "  Generate one with:\n"
                '    python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
                "  Then put it in your .env file (see .env.example)."
            )
        return secret

    def warn_on_insecure_defaults(self) -> None:
        """Log a warning for each security-relevant value left unset."""
        if not self.takasho_key_hex:
            logger.warning(
                "DANKAGU_TAKASHO_KEY_HEX is unset - falling back to the public "
                "development key. Genuine clients will fail to decrypt responses "
                "until you supply the production key. See .env.example."
            )
        if not self.jwt_secret.strip():
            logger.warning(
                "DANKAGU_JWT_SECRET is unset - LCX token issuance will fail at "
                "runtime. See .env.example."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


settings = get_settings()
