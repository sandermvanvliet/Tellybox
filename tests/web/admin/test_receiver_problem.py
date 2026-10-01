"""The dashboard's TV receiver line: the last problem and the refusal hint (CR-6)."""

from __future__ import annotations

import re

import pytest

from tests.web.admin.test_receiver_admin import _line


def _problem(response) -> str:
    return re.search(r'<span id="receiver-problem">(.*?)</span>', response.text, re.S).group(1)


def _hint_hidden(response) -> bool:
    return 'id="receiver-hint" hidden' in response.text


def _receiver(**extra):
    base = {"kind": "tellybox", "configured": True, "fallback_until": None, "last_error": None,
            "failures_24h": 0, "launches_24h": 0, "last_failure": None, "refused": False}
    return {**base, **extra}


def _failure(kind="launch_failed", detail=None, at="2026-09-28T12:02:00.000+00:00"):
    return {"kind": kind, "detail": detail, "at": at}


def test_dashboard_shows_the_last_problem_in_local_time(admin, admin_env, mkstate):
    # NOW is 14:00 UTC = 16:00 in Amsterdam, so 12:02 UTC is 14:02 there.
    admin_env.cast.current = mkstate(receiver=_receiver(
        failures_24h=2, last_failure=_failure(detail="attempt 2: launch timed out")))
    r = admin.get("/admin")
    assert _line(r) == "Tellybox receiver"
    assert _problem(r) == ("&middot; Last problem: The Tellybox receiver did not start: "
                           "attempt 2: launch timed out at 14:02")
    assert _hint_hidden(r)


@pytest.mark.parametrize("kind, text", [
    ("refused", "The Chromecast refused the Tellybox receiver"),
    ("lost", "The Tellybox receiver disappeared during an episode"),
    ("recover_failed", "The Tellybox receiver could not be restarted during an episode"),
    ("page_error", "The Tellybox receiver page reported a problem"),
])
def test_dashboard_problem_wording_per_kind(admin, admin_env, mkstate, kind, text):
    admin_env.cast.current = mkstate(receiver=_receiver(failures_24h=1, last_failure=_failure(kind)))
    assert _problem(admin.get("/admin")) == f"&middot; Last problem: {text} at 14:02"


def test_dashboard_problem_detail_is_escaped(admin, admin_env, mkstate):
    admin_env.cast.current = mkstate(receiver=_receiver(
        failures_24h=1, last_failure=_failure("page_error", "<b>boom</b>")))
    assert "&lt;b&gt;boom&lt;/b&gt;" in _problem(admin.get("/admin"))


def test_dashboard_has_no_problem_line_without_recent_failures(admin, admin_env, mkstate):
    admin_env.cast.current = mkstate(receiver=_receiver(
        failures_24h=0, last_failure=_failure("lost", "x", "2026-09-20T12:02:00.000+00:00")))
    assert _problem(admin.get("/admin")) == ""
    admin_env.cast.current = mkstate(receiver=_receiver())
    assert _problem(admin.get("/admin")) == ""


def test_dashboard_copes_with_a_cast_service_that_predates_the_new_fields(admin, admin_env, mkstate):
    admin_env.cast.current = mkstate(receiver={"kind": "tellybox", "configured": True,
                                               "fallback_until": None, "last_error": None})
    r = admin.get("/admin")
    assert _problem(r) == "" and _hint_hidden(r)


def test_dashboard_explains_a_refusal_while_the_fallback_lasts(admin, admin_env, mkstate):
    admin_env.cast.current = mkstate(receiver=_receiver(
        kind="default", fallback_until="2026-09-28T14:30:00+00:00", last_error="launch failed: CANCELLED",
        refused=True, failures_24h=1, last_failure=_failure("refused", "x", "2026-09-28T13:59:00.000+00:00")))
    r = admin.get("/admin")
    assert "Default Media Receiver (Tellybox receiver unavailable until 16:30" in _line(r)
    assert not _hint_hidden(r)
    assert "restarting the Chromecast usually fixes it" in r.text
    admin_env.cast.current = mkstate(receiver=_receiver(  # the fallback is over: no hint
        kind="default", fallback_until="2026-09-28T13:30:00+00:00", refused=True))
    assert _hint_hidden(admin.get("/admin"))


def test_dashboard_problem_is_translated(admin, admin_env, mkstate):
    admin_env.cast.current = mkstate(receiver=_receiver(failures_24h=1, last_failure=_failure("refused")))
    for lang, word in (("nl", "Laatste probleem"), ("de", "Letztes Problem")):
        assert word in _problem(admin.get("/admin", headers={"Accept-Language": lang}))
