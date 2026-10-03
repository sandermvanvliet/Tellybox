"""Admin sign-in with OIDC (AD-6): tellybox/oidc.py and /admin/oidc/*, against a fake provider.

The provider is an httpx.MockTransport (discovery, JWKS, token and userinfo endpoints) whose
ID tokens are signed with a joserfc RSA key; times come from the fake clock.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import html
import logging
import re
from datetime import timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from tellybox import auth, oidc
from tellybox.config import OidcConfig
from tellybox.web.app import create_app
from tests.web.admin.conftest import ORIGIN, sign_in

ISSUER = "https://id.example.org"
CLIENT_ID = "tellybox"
GROUP = "tellybox-admins"
OIDC = OidcConfig(issuer=ISSUER, client_id=CLIENT_ID, client_secret="client-secret",
                  redirect_uri="http://testserver/admin/oidc/callback", admin_group=GROUP)
FAILED = "Sign-in with OIDC failed. Try again."
NOT_ALLOWED = "Your account may not use the Tellybox admin."


@pytest.fixture(scope="module")
def keys():
    """Two signing keys: generating RSA keys is slow, so once per module."""
    return (RSAKey.generate_key(2048, parameters={"kid": "key-1"}),
            RSAKey.generate_key(2048, parameters={"kid": "key-2"}))


class FakeProvider:
    """An OpenID provider on httpx.MockTransport. Tests change `claims`, `published` and so on."""

    def __init__(self, clock, key: RSAKey) -> None:
        self.clock = clock
        self.signing_key = key
        self.header: dict = {"alg": "RS256", "kid": key.kid}
        self.published = [key]  # what /jwks lists
        self.claims: dict = {}  # changes to the next ID token; a value of None drops the claim
        self.userinfo: dict | None = None  # None: no userinfo endpoint in discovery
        self.auth_methods: list[str] | None = None  # token_endpoint_auth_methods_supported
        self.nonce: str | None = None  # from the last authorization request
        self.down = False
        self.paths: list[str] = []
        self.token_requests: list[httpx.Request] = []

    def discovery(self) -> dict:
        doc = {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
               "jwks_uri": f"{ISSUER}/jwks"}
        if self.userinfo is not None:
            doc["userinfo_endpoint"] = f"{ISSUER}/userinfo"
        if self.auth_methods is not None:
            doc["token_endpoint_auth_methods_supported"] = self.auth_methods
        return doc

    def id_token(self) -> str:
        now = int(self.clock.now().timestamp())
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "user-1", "preferred_username": "parent",
                  "iat": now, "exp": now + 300, "nonce": self.nonce, "groups": [GROUP], **self.claims}
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(self.header, claims, self.signing_key, algorithms=[self.header["alg"]])

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("provider down", request=request)
        path = request.url.path
        self.paths.append(path)
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery())
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [k.as_dict(private=False) for k in self.published]})
        if path == "/token":
            self.token_requests.append(request)
            return httpx.Response(200, json={"access_token": "access", "token_type": "Bearer",
                                             "id_token": self.id_token()})
        if path == "/userinfo" and self.userinfo is not None:
            assert request.headers["authorization"] == "Bearer access"
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)


@pytest.fixture
def provider(clock, keys):
    return FakeProvider(clock, keys[0])


def _app(config, lib, fake_cast, clock, admin_static, provider, oidc_config=OIDC):
    conn, _ = lib
    client = oidc.OidcClient(oidc_config, transport=httpx.MockTransport(provider.handler))
    return create_app(dataclasses.replace(config, oidc=oidc_config), conn=conn, cast=fake_cast, clock=clock,
                      static_dir=admin_static, ytdlp=object(), oidc=client)


@pytest.fixture
def app(admin_config, lib, fake_cast, clock, admin_static, provider):
    return _app(admin_config, lib, fake_cast, clock, admin_static, provider)


@pytest.fixture
def db(lib):
    return lib[0]


@pytest.fixture
def browser(app) -> TestClient:
    return TestClient(app, headers={"Origin": ORIGIN})


def start(browser, provider, next: str = "/admin") -> dict:
    """Click "Sign in with OIDC"; returns the authorization request's parameters."""
    r = browser.get("/admin/oidc/start", params={"next": next}, follow_redirects=False)
    assert r.status_code == 303, r.text
    url = httpx.URL(r.headers["location"])
    assert f"{url.scheme}://{url.host}{url.path}" == f"{ISSUER}/authorize"
    params = dict(url.params)
    provider.nonce = params["nonce"]
    return params


def callback(browser, params: dict, code: str = "the-code"):
    """The provider sends the browser back with a code."""
    return browser.get("/admin/oidc/callback", params={"code": code, "state": params["state"]},
                       follow_redirects=False)


def sign_in_with_oidc(browser, provider, next: str = "/admin"):
    return callback(browser, start(browser, provider, next))


def continues_to(r) -> str:
    """Where the signed-in page moves on to: its meta refresh, which the visible link repeats."""
    refresh = re.search(r'<meta http-equiv="refresh" content="0;url=([^"]*)">', r.text).group(1)
    link = re.search(r'<a class="btn primary" href="([^"]*)">', r.text).group(1)
    assert refresh == link
    return html.unescape(refresh)


def signed_in(browser) -> bool:
    return browser.get("/admin/jobs", follow_redirects=False).status_code == 200


def logins(conn) -> int:
    return conn.execute("SELECT count(*) FROM oidc_login").fetchone()[0]


def browser_cookie(r) -> str | None:
    """The response's Set-Cookie for tb_oidc, lower-cased; None if it doesn't touch it."""
    found = [c.lower() for c in r.headers.get_list("set-cookie") if c.startswith(f"{oidc.BROWSER_COOKIE}=")]
    return found[0] if found else None


def clears_browser_cookie(r) -> bool:
    cookie = browser_cookie(r)
    return cookie is not None and "max-age=0" in cookie and "path=/admin/oidc" in cookie


# --------------------------------------------------------------------------- the sign-in page


def test_without_oidc_there_is_no_button_and_no_routes(anon):
    assert "Sign in with OIDC" not in anon.get("/admin/login").text
    assert anon.get("/admin/oidc/start", follow_redirects=False).status_code == 404
    assert anon.get("/admin/oidc/callback", follow_redirects=False).status_code == 404


def test_with_oidc_the_button_comes_first_and_keeps_next(browser):
    html = browser.get("/admin/login", params={"next": "/admin/jobs"}).text
    assert '<a class="btn primary" href="/admin/oidc/start?next=/admin/jobs">Sign in with OIDC</a>' in html
    assert html.index("Sign in with OIDC") < html.index('type="password"')  # the password form stays (A-24)


def test_start_redirects_with_state_nonce_and_pkce(browser, provider, db):
    params = start(browser, provider)
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == OIDC.redirect_uri
    assert params["scope"] == "openid profile email groups"
    assert params["state"] and params["nonce"]
    assert params["code_challenge_method"] == "S256"
    row = db.execute("SELECT * FROM oidc_login").fetchone()
    assert row["state_hash"] != params["state"]  # only its hash is stored
    assert params["code_challenge"] == oidc._code_challenge(row["code_verifier"])
    assert row["nonce"] == params["nonce"]
    value = browser.cookies.get(oidc.BROWSER_COOKIE)
    assert value and row["browser_hash"] == hashlib.sha256(value.encode()).hexdigest()  # only its hash


def test_start_sets_the_browser_cookie(browser):
    r = browser.get("/admin/oidc/start", follow_redirects=False)
    cookie = browser_cookie(r)
    # Lax, not Strict: it must come along on the provider's cross-site redirect back to us.
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "path=/admin/oidc" in cookie and "max-age=600" in cookie
    assert "secure" not in cookie


def test_the_browser_cookie_is_secure_behind_an_https_proxy(app):
    client = TestClient(app, headers={"Origin": ORIGIN, "X-Forwarded-Proto": "https"})
    assert "secure" in browser_cookie(client.get("/admin/oidc/start", follow_redirects=False))


def test_code_challenge_is_rfc7636_s256():
    # RFC 7636, appendix B
    assert oidc._code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


# --------------------------------------------------------------------------- signing in


def test_a_member_of_the_group_gets_a_session_and_lands_on_next(browser, provider, db, caplog):
    with caplog.at_level(logging.INFO):
        r = sign_in_with_oidc(browser, provider, "/admin/jobs")
    # 200, not a redirect: a redirect from the provider's cross-site navigation would arrive without
    # the SameSite=Strict cookie. The page's own navigation is same-site and carries it.
    assert r.status_code == 200 and "location" not in r.headers
    assert continues_to(r) == "/admin/jobs"
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "Signed in. Continuing…" in r.text
    assert clears_browser_cookie(r)
    assert signed_in(browser)
    assert "admin signed in with OIDC as parent (user-1)" in caplog.text
    assert logins(db) == 0  # the sign-in in progress is used up


def test_the_token_request_sends_the_code_verifier_and_the_secret(browser, provider, db):
    params = start(browser, provider)
    verifier = db.execute("SELECT code_verifier FROM oidc_login").fetchone()[0]
    callback(browser, params)
    request = provider.token_requests[0]
    form = dict(parse_qsl(request.content.decode()))
    assert form == {"grant_type": "authorization_code", "code": "the-code", "redirect_uri": OIDC.redirect_uri,
                    "code_verifier": verifier}
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"tellybox:client-secret").decode()


def test_the_secret_goes_in_the_form_when_the_provider_only_takes_that(browser, provider):
    provider.auth_methods = ["client_secret_post"]
    assert sign_in_with_oidc(browser, provider).status_code == 200
    request = provider.token_requests[0]
    form = dict(parse_qsl(request.content.decode()))
    assert form["client_id"] == CLIENT_ID and form["client_secret"] == "client-secret"
    assert "authorization" not in request.headers


def test_a_public_client_sends_only_its_id(admin_config, lib, fake_cast, clock, admin_static, provider):
    app = _app(admin_config, lib, fake_cast, clock, admin_static, provider,
               dataclasses.replace(OIDC, client_secret=None))
    browser = TestClient(app, headers={"Origin": ORIGIN})
    assert sign_in_with_oidc(browser, provider).status_code == 200
    request = provider.token_requests[0]
    assert dict(parse_qsl(request.content.decode()))["client_id"] == CLIENT_ID
    assert "authorization" not in request.headers


@pytest.mark.parametrize("unsafe", ["//evil.example", "https://evil.example/admin", "/kid"])
def test_next_is_limited_to_the_admin(browser, provider, unsafe):
    r = sign_in_with_oidc(browser, provider, unsafe)
    assert r.status_code == 200 and continues_to(r) == "/admin"


def test_next_is_escaped_in_the_signed_in_page(browser, provider):
    r = sign_in_with_oidc(browser, provider, '/admin/jobs?q="><script>x</script>')
    assert r.status_code == 200
    assert "<script>x" not in r.text
    assert continues_to(r) == '/admin/jobs?q="><script>x</script>'


def test_logout_ends_an_oidc_session(browser, provider):
    sign_in_with_oidc(browser, provider)
    assert browser.post("/admin/logout", follow_redirects=False).status_code == 303
    assert not signed_in(browser)


# --------------------------------------------------------------------------- the admin group


def test_not_in_the_group_is_refused_with_the_password_form(browser, provider, caplog):
    provider.claims = {"groups": ["family"]}
    with caplog.at_level(logging.WARNING):
        r = sign_in_with_oidc(browser, provider)
    assert r.status_code == 403
    assert NOT_ALLOWED in r.text and 'type="password"' in r.text
    assert clears_browser_cookie(r)
    assert not signed_in(browser)
    assert "parent (user-1) is not in group 'tellybox-admins'" in caplog.text


def test_no_groups_claim_is_refused(browser, provider):
    provider.claims = {"groups": None}
    assert sign_in_with_oidc(browser, provider).status_code == 403
    assert not signed_in(browser)


def test_a_single_group_as_a_string_counts(browser, provider):
    provider.claims = {"groups": GROUP}
    assert sign_in_with_oidc(browser, provider).status_code == 200


def test_groups_only_from_userinfo(browser, provider):
    provider.claims = {"groups": None}
    provider.userinfo = {"sub": "user-1", "groups": ["family", GROUP]}
    assert sign_in_with_oidc(browser, provider).status_code == 200
    assert "/userinfo" in provider.paths
    assert signed_in(browser)


def test_userinfo_about_someone_else_is_refused(browser, provider):
    provider.claims = {"groups": None}
    provider.userinfo = {"sub": "user-2", "groups": [GROUP]}
    r = sign_in_with_oidc(browser, provider)
    assert r.status_code == 400 and FAILED in r.text


def test_userinfo_is_not_asked_when_the_id_token_has_the_groups(browser, provider):
    provider.userinfo = {"sub": "user-1", "groups": []}
    assert sign_in_with_oidc(browser, provider).status_code == 200
    assert "/userinfo" not in provider.paths


def test_is_admin():
    member = oidc.Identity(subject="s", name="n", groups=("a", GROUP))
    assert oidc.is_admin(member, GROUP)
    assert not oidc.is_admin(member, "tellybox")  # whole names only
    assert not oidc.is_admin(oidc.Identity(subject="s", name="n", groups=()), GROUP)


# --------------------------------------------------------------------------- the ID token


@pytest.mark.parametrize("claims", [
    {"iss": "https://other.example.org"},
    {"aud": "someone-else"},
    {"aud": [CLIENT_ID, "someone-else"]},  # several audiences and no azp
    {"nonce": "not-the-nonce"},
    {"nonce": None},
    {"sub": None},
    {"exp": None},
], ids=["iss", "aud", "aud-list-without-azp", "nonce", "no-nonce", "no-sub", "no-exp"])
def test_a_wrong_claim_is_refused(browser, provider, claims, caplog):
    provider.claims = claims
    with caplog.at_level(logging.WARNING):
        r = sign_in_with_oidc(browser, provider)
    assert r.status_code == 400 and FAILED in r.text
    assert clears_browser_cookie(r)
    assert not signed_in(browser)
    assert "admin sign-in with OIDC failed" in caplog.text  # the detail goes to the log only


def test_several_audiences_with_azp_are_accepted(browser, provider):
    provider.claims = {"aud": [CLIENT_ID, "someone-else"], "azp": CLIENT_ID}
    assert sign_in_with_oidc(browser, provider).status_code == 200


def test_expired_token_is_refused_after_the_leeway(app, browser, provider, clock):
    now = int(clock.now().timestamp())
    provider.claims = {"iat": now - 600, "exp": now - oidc.LEEWAY_S + 5}  # within the 60 s leeway
    assert sign_in_with_oidc(browser, provider).status_code == 200
    provider.claims = {"iat": now - 600, "exp": now - oidc.LEEWAY_S - 5}
    assert sign_in_with_oidc(TestClient(app, headers={"Origin": ORIGIN}), provider).status_code == 400


def test_a_token_signed_with_another_key_is_refused(browser, provider):
    provider.signing_key = RSAKey.generate_key(2048, parameters={"kid": "key-1"})  # same kid, other key
    assert sign_in_with_oidc(browser, provider).status_code == 400


def test_a_token_signed_with_the_client_secret_is_refused(browser, provider):
    provider.signing_key = OctKey.generate_key(256)
    provider.header = {"alg": "HS256", "kid": "key-1"}
    assert sign_in_with_oidc(browser, provider).status_code == 400


def test_an_unknown_kid_fetches_the_keys_once_more(app, browser, provider, keys):
    assert sign_in_with_oidc(browser, provider).status_code == 200  # keys cached
    provider.signing_key, provider.header = keys[1], {"alg": "RS256", "kid": keys[1].kid}
    provider.published = [keys[1]]  # the provider rotated its key
    second = TestClient(app, headers={"Origin": ORIGIN})
    assert sign_in_with_oidc(second, provider).status_code == 200
    assert provider.paths.count("/jwks") == 2
    assert provider.paths.count("/.well-known/openid-configuration") == 1  # discovery is cached


def test_a_kid_that_stays_unknown_is_refused_after_one_refetch(browser, provider, keys):
    provider.signing_key, provider.header = keys[1], {"alg": "RS256", "kid": keys[1].kid}  # never published
    assert sign_in_with_oidc(browser, provider).status_code == 400
    assert provider.paths.count("/jwks") == 2


# --------------------------------------------------------------------------- the state


def test_an_unknown_state_is_refused(browser, provider):
    start(browser, provider)
    r = callback(browser, {"state": "made-up"})
    assert r.status_code == 400 and FAILED in r.text
    assert provider.token_requests == []


def test_a_state_works_once(app, browser, provider):
    params = start(browser, provider)
    assert callback(browser, params).status_code == 200
    again = TestClient(app, headers={"Origin": ORIGIN})
    assert callback(again, params).status_code == 400
    assert not signed_in(again)


def test_a_state_older_than_10_minutes_is_refused(browser, provider, clock, db):
    params = start(browser, provider)
    clock.advance(timedelta(minutes=10).total_seconds())
    assert callback(browser, params).status_code == 400
    assert provider.token_requests == []
    assert logins(db) == 0


def test_the_providers_error_is_refused(browser, provider, caplog):
    params = start(browser, provider)
    with caplog.at_level(logging.WARNING):
        r = browser.get("/admin/oidc/callback", params={"error": "access_denied", "state": params["state"]},
                        follow_redirects=False)
    assert r.status_code == 400 and FAILED in r.text
    assert "access_denied" in caplog.text
    assert provider.token_requests == []


def test_a_callback_without_a_code_is_refused(browser):
    assert browser.get("/admin/oidc/callback", follow_redirects=False).status_code == 400


# --------------------------------------------------------------------------- bound to the browser


def test_a_callback_url_used_in_another_browser_is_refused(app, browser, provider, db):
    params = start(browser, provider)
    elsewhere = TestClient(app, headers={"Origin": ORIGIN})  # no tb_oidc cookie
    r = callback(elsewhere, params)
    assert r.status_code == 400 and FAILED in r.text
    assert not signed_in(elsewhere)
    assert provider.token_requests == []
    assert logins(db) == 0  # used up: the right browser can't finish it either
    assert callback(browser, params).status_code == 400


def test_a_wrong_browser_cookie_is_refused(browser, provider, db):
    params = start(browser, provider)
    browser.cookies.set(oidc.BROWSER_COOKIE, "someone-elses", path="/admin/oidc")
    r = callback(browser, params)
    assert r.status_code == 400 and clears_browser_cookie(r)
    assert not signed_in(browser)
    assert provider.token_requests == [] and logins(db) == 0


# --------------------------------------------------------------------------- locked, and the provider down


def test_while_locked_both_routes_go_to_setup(config, lib, fake_cast, clock, admin_static, provider):
    app = _app(config, lib, fake_cast, clock, admin_static, provider)  # no password: DP-4
    browser = TestClient(app, headers={"Origin": ORIGIN})
    for path in ("/admin/oidc/start", "/admin/oidc/callback?code=x&state=y", "/admin/login"):
        r = browser.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/setup", path
    assert provider.paths == []


def test_the_provider_down_breaks_neither_the_app_nor_the_password(browser, provider, caplog):
    provider.down = True
    assert browser.get("/").status_code == 200
    with caplog.at_level(logging.WARNING):
        r = browser.get("/admin/oidc/start", follow_redirects=False)
    assert r.status_code == 502 and FAILED in r.text
    assert "could not start" in caplog.text
    assert sign_in(browser).status_code == 303
    assert signed_in(browser)


def test_sessions_from_oidc_end_when_the_password_changes(browser, provider, db):
    sign_in_with_oidc(browser, provider)
    auth.install_password(db, "a new password")
    assert not signed_in(browser)
