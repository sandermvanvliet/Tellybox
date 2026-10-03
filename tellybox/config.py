"""Runtime configuration from environment variables (NF-9: state on mounted volumes)."""

from __future__ import annotations

import os
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def _local_zone_name() -> str:
    if tz := os.environ.get("TELLYBOX_TZ") or os.environ.get("TZ"):
        return tz
    try:
        return Path("/etc/timezone").read_text().strip()
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        return target.split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return "UTC"


def lan_ip() -> str:
    """IP of the interface that routes outward; the address the Chromecast can reach."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]


def load_or_create_secret(path: Path) -> bytes:
    """Shared signing secret (NF-3). Created once; safe if web and cast race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes().strip()
    with os.fdopen(fd, "wb") as f:
        key = secrets.token_hex(32).encode()
        f.write(key)
    return key


def admin_password_from_env(env: dict[str, str]) -> str | None:
    """TELLYBOX_ADMIN_PASSWORD, or the contents of TELLYBOX_ADMIN_PASSWORD_FILE (AD-1). None if unset/empty."""
    value = env.get("TELLYBOX_ADMIN_PASSWORD")
    if not value and (file := env.get("TELLYBOX_ADMIN_PASSWORD_FILE")):
        value = Path(file).read_text().rstrip("\r\n")
    return value or None


OIDC_CALLBACK_PATH = "/admin/oidc/callback"
OIDC_REQUIRED = ("TELLYBOX_OIDC_CLIENT_ID", "TELLYBOX_OIDC_REDIRECT_URI", "TELLYBOX_OIDC_ADMIN_GROUP")


@dataclass(frozen=True)
class OidcConfig:
    """Admin sign-in with an OpenID Connect provider (AD-6); only members of `admin_group` get in."""

    issuer: str  # e.g. https://id.example.org; discovery is <issuer>/.well-known/openid-configuration
    client_id: str
    redirect_uri: str  # exactly as registered at the provider; ends in OIDC_CALLBACK_PATH
    admin_group: str
    client_secret: str | None = field(default=None, repr=False)  # None: a public client (PKCE only)
    groups_claim: str = "groups"
    scopes: str = "openid profile email groups"


def oidc_from_env(env: dict[str, str]) -> OidcConfig | None:
    """TELLYBOX_OIDC_* (AD-6). None when TELLYBOX_OIDC_ISSUER is unset or empty.

    With an issuer, a missing or malformed setting raises ValueError, so the services refuse to
    start rather than letting every account at the provider in. The provider itself isn't
    contacted here: it may be down while the kid app and password sign-in work.
    """
    issuer = env.get("TELLYBOX_OIDC_ISSUER", "").strip()
    if not issuer:
        return None
    if missing := [name for name in OIDC_REQUIRED if not env.get(name, "").strip()]:
        raise ValueError(f"TELLYBOX_OIDC_ISSUER is set, so these must be set too: {', '.join(missing)}")
    redirect_uri = env["TELLYBOX_OIDC_REDIRECT_URI"].strip()
    parts = urlsplit(redirect_uri)
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.path.endswith(OIDC_CALLBACK_PATH):
        raise ValueError(f"TELLYBOX_OIDC_REDIRECT_URI must be a full URL ending in {OIDC_CALLBACK_PATH}")
    if urlsplit(issuer).scheme not in ("http", "https"):
        raise ValueError("TELLYBOX_OIDC_ISSUER must be a URL, e.g. https://id.example.org")
    secret = env.get("TELLYBOX_OIDC_CLIENT_SECRET")
    if not secret and (file := env.get("TELLYBOX_OIDC_CLIENT_SECRET_FILE")):
        secret = Path(file).read_text().rstrip("\r\n")
    scopes = " ".join(env.get("TELLYBOX_OIDC_SCOPES", "").split()) or OidcConfig.scopes
    if "openid" not in scopes.split():
        raise ValueError("TELLYBOX_OIDC_SCOPES must include openid")
    return OidcConfig(
        issuer=issuer,
        client_id=env["TELLYBOX_OIDC_CLIENT_ID"].strip(),
        redirect_uri=redirect_uri,
        admin_group=env["TELLYBOX_OIDC_ADMIN_GROUP"].strip(),
        client_secret=secret or None,
        groups_claim=env.get("TELLYBOX_OIDC_GROUPS_CLAIM", "").strip() or OidcConfig.groups_claim,
        scopes=scopes,
    )


@dataclass(frozen=True)
class Config:
    db_path: Path
    media_dir: Path
    data_dir: Path
    tz: ZoneInfo
    web_host: str
    web_port: int
    cast_api_host: str
    cast_api_port: int
    media_base_url: str  # how the Chromecast reaches the web service, e.g. http://192.168.1.10:8080
    secret: bytes
    admin_password: str | None = field(default=None, repr=False)  # None: admin is locked
    oidc: OidcConfig | None = None  # None: password sign-in only (AD-6)
    version: str = "dev"  # TELLYBOX_VERSION, set by the Dockerfile's APP_VERSION build arg

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        data_dir = Path(env.get("TELLYBOX_DATA_DIR", "data"))
        web_port = int(env.get("TELLYBOX_WEB_PORT", "8080"))
        base_url = env.get("TELLYBOX_MEDIA_BASE_URL") or f"http://{lan_ip()}:{web_port}"
        return cls(
            db_path=Path(env.get("TELLYBOX_DB", data_dir / "tellybox.db")),
            media_dir=Path(env.get("TELLYBOX_MEDIA_DIR", "media")),
            data_dir=data_dir,
            tz=ZoneInfo(_local_zone_name()),
            web_host=env.get("TELLYBOX_WEB_HOST", "0.0.0.0"),
            web_port=web_port,
            cast_api_host=env.get("TELLYBOX_CAST_API_HOST", "127.0.0.1"),
            cast_api_port=int(env.get("TELLYBOX_CAST_API_PORT", "8081")),
            media_base_url=base_url.rstrip("/"),
            secret=load_or_create_secret(Path(env.get("TELLYBOX_SECRET_FILE", data_dir / "secret.key"))),
            admin_password=admin_password_from_env(env),
            oidc=oidc_from_env(env),
            version=env.get("TELLYBOX_VERSION", "dev"),
        )
