"""Admin JSON API (step 9, HA-1..HA-8): see docs/admin-api.md."""

from tellybox.web.api.router import create_router
from tellybox.web.api.state import Counts, build_admin_state, unreachable

__all__ = ["Counts", "build_admin_state", "create_router", "unreachable"]
