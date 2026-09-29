"""Bearer-token guard for the admin API (HA-1). No cookies are involved, so there is no Origin check."""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException, Request

from tellybox import api_tokens
from tellybox.clock import Clock


class TokenGuard:
    """FastAPI dependency: `Depends(TokenGuard(conn, clock, "read"))`.

    401 (with `WWW-Authenticate: Bearer`) for a missing, malformed, unknown or revoked token, 403 when the
    token lacks the scope. The token never appears in a log line or an error message. Async so the shared
    sqlite connection is only used from the event loop.
    """

    def __init__(self, conn: sqlite3.Connection, clock: Clock, scope: str) -> None:
        self.conn = conn
        self.clock = clock
        self.scope = scope

    async def __call__(self, request: Request) -> api_tokens.ApiToken:
        scheme, _, secret = request.headers.get("authorization", "").partition(" ")
        token = None
        if scheme.lower() == "bearer":
            token = api_tokens.authenticate(self.conn, secret.strip(), self.clock.now())
        if token is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if not token.allows(self.scope):
            raise HTTPException(403, "forbidden")
        request.state.api_token = token
        return token
