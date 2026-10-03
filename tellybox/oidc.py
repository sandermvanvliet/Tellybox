"""Admin sign-in with an OpenID Connect provider (AD-6): the authorization code flow with PKCE.

The provider's discovery document and keys are fetched on the first sign-in and kept in memory,
not at start, so the kid app and password sign-in work while the provider is down. A sign-in in
progress is one `oidc_login` row, found by the SHA-256 of its state and used once, and bound to
the browser that started it by a random cookie (`BROWSER_COOKIE`). The ID token is checked with
joserfc: signature against the provider's keys, then issuer, audience, expiry and nonce. Who may
use the admin is decided by one group (`is_admin`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

import httpx
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet

from tellybox.config import OidcConfig
from tellybox.db import from_db, to_db
from tellybox.library import _transaction

log = logging.getLogger(__name__)

LOGIN_TTL = timedelta(minutes=10)  # from the start of a sign-in to its callback
BROWSER_COOKIE = "tb_oidc"  # binds a sign-in to the browser that started it; only its SHA-256 is stored
LEEWAY_S = 60  # clock skew allowed on the ID token's exp and iat
# Signed with the provider's key pair only: no "none", and no HMAC with the client secret.
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")


class OidcError(Exception):
    """A sign-in that can't go on. The message is for the log; the page shows a general one."""


@dataclass(frozen=True)
class Identity:
    subject: str  # the provider's `sub`
    name: str  # preferred_username, name or email, for the log
    groups: tuple[str, ...]


def is_admin(identity: Identity, group: str) -> bool:
    """Only members of the configured group may use the admin (AD-6)."""
    return group in identity.groups


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _code_challenge(verifier: str) -> str:
    """PKCE S256 (RFC 7636): base64url of the verifier's SHA-256, without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _groups(value) -> tuple[str, ...]:
    """The groups claim: a list of names, or one name."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str))
    return ()


def purge_stale_logins(conn: sqlite3.Connection, now: datetime) -> int:
    """Sign-ins that never came back from the provider."""
    return conn.execute("DELETE FROM oidc_login WHERE created_at <= ?", (to_db(now - LOGIN_TTL),)).rowcount


class OidcClient:
    """One provider, from `OidcConfig`. `transport` is for tests (httpx.MockTransport)."""

    def __init__(self, config: OidcConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10.0) -> None:
        self.config = config
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)
        self._metadata: dict | None = None
        self._keys: KeySet | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ----------------------------------------------------------------------- provider documents

    async def _get_json(self, url: str, headers: dict[str, str] | None = None) -> dict:
        try:
            r = await self._http.get(url, headers={"Accept": "application/json", **(headers or {})})
        except httpx.HTTPError as exc:
            raise OidcError(f"GET {url}: {exc!r}") from exc
        if r.status_code != 200:
            raise OidcError(f"GET {url}: HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as exc:
            raise OidcError(f"GET {url}: not JSON") from exc
        if not isinstance(data, dict):
            raise OidcError(f"GET {url}: not a JSON object")
        return data

    async def metadata(self) -> dict:
        """The discovery document, fetched once. Its issuer must be the configured one."""
        if self._metadata is None:
            issuer = self.config.issuer.rstrip("/")
            data = await self._get_json(f"{issuer}/.well-known/openid-configuration")
            if not isinstance(data.get("issuer"), str) or data["issuer"].rstrip("/") != issuer:
                raise OidcError(f"discovery names issuer {data.get('issuer')!r}, not {self.config.issuer!r}")
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                if not isinstance(data.get(key), str):
                    raise OidcError(f"discovery has no {key}")
            self._metadata = data
        return self._metadata

    async def _key_set(self, refresh: bool = False) -> KeySet:
        """The provider's signing keys; `refresh` fetches them again (the provider rotated its keys)."""
        if self._keys is None or refresh:
            data = await self._get_json((await self.metadata())["jwks_uri"])
            try:
                self._keys = KeySet.import_key_set(data)  # type: ignore[arg-type]
            except (JoseError, KeyError, TypeError, ValueError) as exc:
                raise OidcError(f"unusable JWKS: {exc!r}") from exc
        return self._keys

    # ----------------------------------------------------------------------- the flow

    async def begin(self, conn: sqlite3.Connection, next: str, now: datetime) -> tuple[str, str]:
        """Start a sign-in: store its state, nonce and PKCE verifier. Returns the provider's URL and
        the value for `BROWSER_COOKIE`, which the callback must bring back.

        `next` must already be safe (`_safe_next`); it's where the callback goes on success.
        """
        endpoint = (await self.metadata())["authorization_endpoint"]
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        browser = secrets.token_urlsafe(32)
        conn.execute(
            """INSERT INTO oidc_login (state_hash, nonce, code_verifier, next, browser_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (_sha256(state), nonce, verifier, next, _sha256(browser), to_db(now)),
        )
        params = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": self.config.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": _code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}", browser

    async def finish(self, conn: sqlite3.Connection, code: str, state: str, browser: str | None,
                     now: datetime) -> tuple[Identity, str]:
        """Complete a sign-in from the callback; returns who signed in and where to go next.

        `browser` is the `BROWSER_COOKIE` value. Without the one `begin` gave out, a leaked callback
        URL can't be used in another browser, and nobody can sign a victim in to their own account
        (login CSRF). The stored sign-in is taken and deleted first, so a state works once, also
        when the rest fails.
        """
        h = _sha256(state)
        with _transaction(conn):
            row = conn.execute("SELECT * FROM oidc_login WHERE state_hash = ?", (h,)).fetchone()
            conn.execute("DELETE FROM oidc_login WHERE state_hash = ?", (h,))
        if row is None:
            raise OidcError("unknown or already used state")
        if not browser or not hmac.compare_digest(row["browser_hash"], _sha256(browser)):
            raise OidcError("the sign-in was started in another browser (tb_oidc cookie missing or wrong)")
        if now - from_db(row["created_at"]) >= LOGIN_TTL:
            raise OidcError("the sign-in took longer than 10 minutes")
        meta = await self.metadata()
        tokens = await self._exchange(meta, code, row["code_verifier"])
        claims = await self._validate(tokens.get("id_token"), meta, row["nonce"], now)
        groups = claims.get(self.config.groups_claim)
        if groups is None and meta.get("userinfo_endpoint") and isinstance(tokens.get("access_token"), str):
            # Some providers only put the groups in the userinfo response.
            info = await self._get_json(meta["userinfo_endpoint"],
                                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
            if info.get("sub") != claims["sub"]:
                raise OidcError("userinfo is about another subject")
            groups = info.get(self.config.groups_claim)
        name = claims.get("preferred_username") or claims.get("name") or claims.get("email") or claims["sub"]
        return Identity(subject=claims["sub"], name=str(name), groups=_groups(groups)), row["next"]

    async def _exchange(self, meta: dict, code: str, verifier: str) -> dict:
        """The token request: the code and PKCE verifier, with the client secret if there is one."""
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.config.redirect_uri,
                "code_verifier": verifier}
        auth = None
        secret = self.config.client_secret
        if secret is None:
            data["client_id"] = self.config.client_id
        elif "client_secret_basic" in meta.get("token_endpoint_auth_methods_supported", ["client_secret_basic"]):
            # RFC 6749 2.3.1: both parts form-encoded before they go into the Basic header.
            auth = httpx.BasicAuth(quote(self.config.client_id, safe=""), quote(secret, safe=""))
        else:
            data |= {"client_id": self.config.client_id, "client_secret": secret}
        url = meta["token_endpoint"]
        try:
            r = await self._http.post(url, data=data, auth=auth, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OidcError(f"POST {url}: {exc!r}") from exc
        if r.status_code != 200:
            raise OidcError(f"POST {url}: HTTP {r.status_code} {r.text[:200]}")
        try:
            tokens = r.json()
        except ValueError as exc:
            raise OidcError(f"POST {url}: not JSON") from exc
        if not isinstance(tokens, dict):
            raise OidcError(f"POST {url}: not a JSON object")
        return tokens

    async def _validate(self, id_token, meta: dict, nonce: str, now: datetime) -> dict:
        """The ID token's claims, once its signature, issuer, audience, times and nonce check out."""
        if not isinstance(id_token, str):
            raise OidcError("the token response has no id_token")
        try:
            try:
                token = jwt.decode(id_token, await self._key_set(), algorithms=ALGORITHMS)
            except InvalidKeyIdError:
                token = jwt.decode(id_token, await self._key_set(refresh=True), algorithms=ALGORITHMS)
            jwt.JWTClaimsRegistry(
                now=int(now.timestamp()),
                leeway=LEEWAY_S,
                iss={"essential": True, "value": meta["issuer"]},
                aud={"essential": True, "value": self.config.client_id},
                sub={"essential": True},
                exp={"essential": True},
                iat={"essential": True},
                nonce={"essential": True, "value": nonce},
            ).validate(token.claims)
        except (JoseError, ValueError) as exc:
            raise OidcError(f"ID token refused: {exc!r}") from exc
        claims = token.claims
        aud = claims["aud"]
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.config.client_id:
            raise OidcError("ID token for several audiences, and not authorized for us (azp)")
        return claims
