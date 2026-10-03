# Admin sign-in with OIDC (AD-6), issue #28

Status: approved by the owner on 2026-10-02 (issue #28). Branch `oidc/admin-login`, one PR; see "Changed while building" for where it differs from this plan.

## Context

Issue #28: "Instead of setting a single password I want to have the option to use OIDC for authentication." Today the admin pages need the password (AD-1), from the environment or chosen in the browser with a setup code (DP-4). Households with an identity provider (Pocket ID, Authentik, Keycloak, Authelia, …) sign in everywhere else with it, and putting the provider in front of `/admin` at the reverse proxy means signing in twice.

## Decisions (confirmed by the owner, 2026-10-02)

- **The password stays.** OIDC is an extra way in, never the only one, so the admin is reachable when the provider is down.
- **DP-4 is unchanged.** Without a password, the setup code is still logged and every admin page still goes to `/admin/setup`, even with OIDC configured. OIDC sign-in works once a password exists. `install_password` and `is_locked` don't change.
- **One button, generic text.** The sign-in page shows "Sign in with OIDC" above the password form, only when OIDC is configured. No provider name or logo setting.
- **Group required.** Only members of one group (`TELLYBOX_OIDC_ADMIN_GROUP`) are admitted. If OIDC is configured without a group, the services refuse to start with a clear message, rather than letting every account at the provider in. This keeps the "single admin" model: the group is the admin.
- **Configuration through environment variables only**, like the password (`TELLYBOX_OIDC_*`, with `_FILE` for the secret). Nothing in the settings page.
- **An explicit redirect URI.** `TELLYBOX_OIDC_REDIRECT_URI` is required. Deriving it from the request depends on the proxy's `Host` and `X-Forwarded-*` headers, and it must match the provider's registration exactly; like `TELLYBOX_MEDIA_BASE_URL`, an explicit value avoids surprises.
- **Sign-out is local.** `POST /admin/logout` ends the Tellybox session the same way for both kinds of sign-in. There is no RP-initiated logout at the provider.
- **Sessions are the same sessions.** A successful OIDC sign-in calls `auth.create_session`, so the 30-day sliding expiry (AD-1), the cookie and `AdminGuard` are shared. Changing the env password still ends every session, OIDC ones included.
- **API tokens are unaffected** (A-16).

These become PRD assumptions A-24..A-28 in the PR.

## What already exists

- **Sign-in** (`tellybox/web/admin/__init__.py`, `tellybox/auth.py`):
  - `GET/POST /admin/login` renders `login.html` (one password field) and calls `auth.check_password`, then `auth.create_session` and `set_session_cookie` (`tb_admin`, path `/admin`, `SameSite=Strict`, `Secure` when `is_https`).
  - `_safe_next` limits the post-sign-in redirect to `/admin…`.
  - `throttle_wait_s`/`record_failure` slow down password guessing per client address.
  - `POST /admin/logout` ends the session.
- **`AdminGuard`** (`admin/common.py`): `AdminLocked` while no password exists, the Origin check on unsafe methods, then `touch_session`.
- **Config** (`tellybox/config.py`): `Config.from_env`, and `admin_password_from_env` for the `_FILE` pattern. `Config` is shared by web, cast and worker.
- **Home Assistant add-on**: `OPTION_ENV` in `tellybox/entrypoint.py` maps add-on options to env vars.
- **Purge** (`tellybox/purge.py`): already removes expired sessions and stale `login_throttle` rows.
- **httpx** is already a dependency (web → cast API). `argon2-cffi` is the only auth dependency.
- The latest migration is `014_profile_limits.sql`.

## Library

**Authlib** (BSD-3-Clause; maintained, the usual choice for OIDC in Starlette/FastAPI), with **joserfc** (same author, the JOSE library Authlib now builds on) for the ID token:
- `authlib.integrations.httpx_client.AsyncOAuth2Client`: authorization URL with PKCE (S256), and the code exchange. It's built on httpx, which we already ship.
- `joserfc.jwt` + `joserfc.jwk.KeySet`: signature against the provider's JWKS, then `iss`, `aud`, `exp`/`iat` (with 60 s leeway) and `nonce`.
- **Not** Authlib's Starlette integration (`authlib.integrations.starlette_client`). It needs Starlette's `SessionMiddleware` (and `itsdangerous`), a second cookie-session mechanism next to ours. Login state goes in SQLite instead, like our sessions.

Alternative if the owner prefers fewer dependencies: plain httpx for discovery and the token request, and joserfc alone for the ID token. That is about 40 more lines of our own code; Authlib is the safer default for the protocol details.

## Configuration

OIDC is on when `TELLYBOX_OIDC_ISSUER` is set. Then the variables marked required must be set too, or `Config.from_env` raises with the missing names.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELLYBOX_OIDC_ISSUER` | none | The provider's issuer URL, e.g. `https://id.example.org`. Discovery comes from `<issuer>/.well-known/openid-configuration`. Setting it turns OIDC on. |
| `TELLYBOX_OIDC_CLIENT_ID` | none | Required. |
| `TELLYBOX_OIDC_CLIENT_SECRET_FILE` | none | A file containing the client secret (preferred over the variable below). Leave both unset for a public client; PKCE is always used. |
| `TELLYBOX_OIDC_CLIENT_SECRET` | none | The client secret itself. |
| `TELLYBOX_OIDC_REDIRECT_URI` | none | Required. `https://<your Tellybox name>/admin/oidc/callback`, exactly as registered at the provider. Must end in `/admin/oidc/callback`. |
| `TELLYBOX_OIDC_ADMIN_GROUP` | none | Required. Members of this group may use the admin pages. |
| `TELLYBOX_OIDC_GROUPS_CLAIM` | `groups` | The claim holding the user's groups (a list, or one string). |
| `TELLYBOX_OIDC_SCOPES` | `openid profile email groups` | Scopes to request. Some providers need a different scope for the groups claim. |

- `Config` gets one `oidc: OidcConfig | None` field (frozen dataclass; the secret has `repr=False`), filled by `oidc_from_env(env)`, in the style of `admin_password_from_env`.
- `OPTION_ENV` gets `oidc_issuer`, `oidc_client_id`, `oidc_client_secret`, `oidc_redirect_uri`, `oidc_admin_group`; the add-on's own `config.yaml` schema (separate repository) follows later.
- A bad configuration fails at start; an unreachable provider does not. Discovery and JWKS are fetched on the first OIDC sign-in, so the kid app and password sign-in work with the provider down.

## Design

### Migration `015_admin_oidc.sql`

```sql
-- Admin sign-in with OIDC (AD-6): one row per sign-in in progress, deleted on the callback or after 10 minutes.
CREATE TABLE oidc_login (
    state_hash TEXT PRIMARY KEY,   -- SHA-256 of the state parameter; never the state itself
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,   -- PKCE; single use, sent once to the token endpoint
    next TEXT NOT NULL,            -- where to go after sign-in (already passed through _safe_next)
    created_at TEXT NOT NULL
);
```

The verifier and nonce are stored as they are: they must be sent or compared later, and the row lives for at most 10 minutes. `purge.py` deletes rows older than 10 minutes, next to `admin_session`.

### `tellybox/oidc.py`

- `OidcClient(config.oidc, transport=None)`: lazy discovery and JWKS (cached in memory; JWKS refetched once on an unknown `kid`). A `transport` argument, like `CastClient`, so tests use `httpx.MockTransport`.
- `begin(conn, next, now) -> str`: makes state, nonce and verifier, stores the row, returns the authorization URL.
- `finish(conn, code, state, now) -> Identity`: takes and deletes the row (single use), exchanges the code, validates the ID token, and returns the subject, a display name (`preferred_username`, `name` or `email`) and the groups.
  - If the groups claim isn't in the ID token, it asks the userinfo endpoint.
  - Raises `OidcError` with a reason the page can show.
- `is_admin(identity, group) -> bool`: a pure function.

### Routes (in `admin/__init__.py`, public router)

- `GET /admin/oidc/start?next=…`: redirects to the provider. 404 when OIDC is off; to `/admin/setup` while locked (DP-4).
- `GET /admin/oidc/callback?code=…&state=…`:
  - **Success and in the group:** `create_session`, then a 200 page that sets the cookie and moves on to `next` itself (see the next bullet and "Changed while building"). Logs `admin signed in with OIDC as <name> (<sub>)`.
  - **Not in the group:** 403, the sign-in page with "Your account may not use the Tellybox admin." and the password form. Logged with the name and sub.
  - **Provider error** (`error=` in the query), **unknown or expired state**, **invalid token:** 400, the sign-in page with "Sign-in with OIDC failed. Try again." The detail goes to the log only.
- The callback is a top-level GET that a cross-site page (the provider) started. Browsers treat every hop of such a redirect chain as cross-site, so a redirect to `next` would arrive without the new `SameSite=Strict` cookie and bounce to the sign-in page. The callback therefore answers with a small page that sets the cookie and then navigates to `next` itself (meta refresh, plus a link); that navigation is same-site and carries the cookie. The cookie stays Strict (AD-1).
- No password throttle on these routes: there's no password to guess, and the state is single-use.

### Sign-in page

`login.html`: when `oidc_enabled`, a `btn primary` link "Sign in with OIDC" to `/admin/oidc/start?next=…`, then an "or" divider, then the password form (its button becomes secondary). An `error` block for the OIDC messages. All new text through `_()`, translated in `nl` and `de` (NF-13).

## Tests

`tests/web/admin/test_oidc.py`, with a fake provider on `httpx.MockTransport` (discovery, JWKS from a joserfc RSA key, token and userinfo endpoints) and the fake clock:
- the button only shows with OIDC configured; start redirects with `state`, `nonce`, `code_challenge` (S256);
- a member of the group gets a session and lands on `next`; `_safe_next` rules apply;
- not in the group, no groups claim, and groups only from userinfo;
- wrong `iss`, `aud`, signature, `nonce`; expired token; unknown `kid` with one JWKS refetch;
- state unknown, reused, or older than 10 minutes; the provider's `error=` parameter;
- locked (no password) still goes to setup; logout ends an OIDC session;
- the provider unreachable at start doesn't break the app or password sign-in.

`tests/test_config.py` (new): `oidc_from_env` (off, complete, each missing required variable, `_FILE`, scopes). `tests/test_entrypoint.py`: the new `OPTION_ENV` entries. `tests/test_purge.py`: `oidc_login` cleanup. `tests/test_i18n.py` covers the new strings.

## Docs and bookkeeping (same PR)

- **PRD:**
  - **AD-6** | "Optionally, the admin signs in with an OpenID Connect provider instead of the password; only members of one configured group are admitted, and the password stays available." | Should | (release: owner to choose).
  - Assumptions A-24..A-28 from the decisions above.
- **CLAUDE.md:** "Single admin, password login." becomes "Single admin; password login, plus optional OIDC sign-in for one group (AD-6)."
- **`docs/installation.md`:** a "Sign in with OIDC" section (register the client, redirect URI, groups claim, an example for one provider) and the configuration table rows. `deploy/.env.example`: the variables, commented out. `deploy/platforms/unraid/tellybox.xml`: the variables as advanced, optional settings.
- **`docs/PROGRESS.md`:** an entry, and "Resume here" updated.
- **`pyproject.toml`:** `"authlib>=1.6"` and `"joserfc>=1.0"`, with an `# admin sign-in with OIDC (AD-6)` comment.

## Changed while building

- **No Authlib; the plan's fallback instead.** Authlib 1.8.0 (the newest release) deprecates its httpx integration: it now wants `httpx2`, a separate HTTP library, and warns at every start when it falls back to httpx. Adding a second HTTP stack for one sign-in route is a poor trade, so the protocol is plain httpx (discovery, token request, userinfo, JWKS), and joserfc alone checks the ID token. `pyproject.toml` gets only `"joserfc>=1.7.5"` (checked against 1.7.5).
- **Signatures:** only asymmetric algorithms (RS, PS, ES, EdDSA) are accepted, so an ID token signed with the client secret (HS256) or unsigned is refused. With several audiences, `azp` must be our client id.
- **`begin` is async** (it fetches discovery for the authorization endpoint), and **`finish` returns the identity and `next`** together, from the same stored row.
- **The token request** sends the secret with HTTP Basic, or in the form when the provider lists only `client_secret_post`; a public client sends just its id.
- **The routes are only mounted with OIDC configured**, which gives the 404 when it's off. When the provider can't be reached at start, `/admin/oidc/start` answers 502 with the failure message.
- **Config** also refuses an issuer that isn't a URL and scopes without `openid`.
- **Unraid template:** also `TELLYBOX_OIDC_GROUPS_CLAIM` and `TELLYBOX_OIDC_SCOPES` (empty means the default), but no `_FILE` variable.
- **AD-6's release** is left as "—" for the owner to fill in.
- **A signed-in page, not a redirect, after the callback.** The callback is a navigation that a cross-site page (the provider) started, and browsers treat every hop of such a redirect chain as cross-site (RFC 6265bis), so a redirect to `next` would arrive without the Strict session cookie. The callback answers 200 with `signed_in.html` ("Signed in. Continuing…"), which sets the cookie and moves on to `next` with a meta refresh and a visible link; that navigation is same-site. The page has `Cache-Control: no-store` and `Referrer-Policy: no-referrer`, so the code and state in its URL don't end up in a cache or a Referer. The cookie stays `SameSite=Strict`.
- **Each sign-in is bound to its browser.** Found by `state` alone, a leaked callback URL could be finished in another browser, and an attacker could sign a victim in to the attacker's account (login CSRF). `/admin/oidc/start` sets a `tb_oidc` cookie with a fresh random value (HttpOnly, `SameSite=Lax`, `path=/admin/oidc`, 10 minutes, `Secure` over HTTPS), and `oidc_login` stores its SHA-256 in a `browser_hash` column. `finish` requires the cookie and compares the hashes in constant time; a missing or wrong cookie fails the sign-in and still uses up the row. Every callback outcome clears the cookie. Lax rather than Strict, because the provider sends the browser back with a cross-site top-level GET, which carries Lax cookies but not Strict ones; the session cookie stays Strict.

## Checks after merge

- Sign in against a real provider (Pocket ID) behind a reverse proxy, as a member and as a non-member of the group.
- Provider stopped: password sign-in still works; the OIDC button shows the failure message.
