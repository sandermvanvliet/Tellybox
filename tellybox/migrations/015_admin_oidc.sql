-- Admin sign-in with OIDC (AD-6): one row per sign-in in progress, deleted on the callback or after 10 minutes.
CREATE TABLE oidc_login (
    state_hash TEXT PRIMARY KEY,   -- SHA-256 of the state parameter; never the state itself
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,   -- PKCE; single use, sent once to the token endpoint
    next TEXT NOT NULL,            -- where to go after sign-in (already passed through _safe_next)
    browser_hash TEXT NOT NULL,    -- SHA-256 of the tb_oidc cookie: only the browser that started it can finish
    created_at TEXT NOT NULL
);
