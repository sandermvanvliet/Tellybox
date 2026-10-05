# Plan: Control which shows a profile can see (issue #40)

## Context
Issue #40: "With profiles being targeted at different age groups we need a way to control which shows a profile can see." This reverses A-8 ("every profile sees the whole approved library", owner, 2026-09-28). Proposed as step 18, after v9 (step 17), one branch/PR. The PRD goes first, then code. Migration number: 020 (019 is the latest).

## Decisions (from the grilling, 2026-10-05)
- **Model**: allow-list per profile, at show level only (profile x show). Episodes inherit from their show. No ratings, no deny-list.
- **Defaults**: a new show is hidden for every profile until the admin assigns it. A new profile starts with no shows; "copy from profile" at creation is a one-off copy of the assignments, not an inheritance.
- **Migration**: every existing profile is granted every existing show, so an upgrade changes nothing visible. Only shows and profiles created afterwards start hidden.
- **Groups (PR-2/PR-4)**: a group sees the intersection of its members' shows.
- **Enforcement**: on the server, on every path, never only in the listing: kid listing, pick (TV and in-app), autoplay-next, continue-watching, playlists (filtered at read time; visibility is never copied onto a playlist), session-scoped media URLs, and the cast service. A refused pick never moves current playback (as PR-4).
- **Revocation**: the episode in progress finishes (no mid-episode cut-off); autoplay-next stops; the show leaves the grid and continue-watching. For a group, losing access for any member stops autoplay for the group. "Stop now" remains the immediate tool.
- **Data**: positions, history and continue-watching rows are kept and only hidden from the kid app. Re-granting restores resume. Admin history still lists them.
- **Admin UI**: a show x profile matrix page (main, phone-friendly); a "Visible to" checklist on the show settings page; an optional profile checklist when approving from the inbox (CS-*) or adding by URL (default none). Subscriptions still never auto-approve (A-9). The dashboard flags profiles with zero visible shows.
- **Kid app**: a picture-only empty state (for example a sleepy-TV icon) for a profile with no shows, and for a group whose intersection is empty. No text dependency (KA-2).
- **Admin API**: read-only. The admin state carries the visible-show count per profile. No write endpoints; assignment is a deliberate admin action and HA-8 is about playback.

## Tasks
1. **PRD and docs first**: new requirements (PR-5..: allow-list, defaults, groups, enforcement, revocation; AD-8..: matrix, per-show checklist, approval-time assignment, zero-show flag; KA-15: empty state; HA-10: visible-show count). Reword A-8 and add an assumption recording the reversal (owner, 2026-10-05); A-7 stays. Add step 18 to the build order, update the Profile/Show data model and the risks (a forgotten assignment leaves a new show invisible). `docs/PROGRESS.md` entry; `docs/admin-api.md` and `docs/kid-api.md`.
2. **Schema**: migration `020_profile_show_access.sql`: `profile_show(profile_id, show_id)` primary key on both, cascade deletes; backfill all existing pairs. A helper module (for example `tellybox/show_access.py`): `visible_show_ids(profile_ids)` (intersection), `can_watch(profile_ids, show_id)`, `grant`/`revoke`, `copy_from`.
3. **Enforcement**, each with a test: kid library/show/episode listing and continue-watching (`tellybox/web/kid.py`); playlists (`tellybox/library.py` and kid view); pick endpoints for TV and device; the cast service `play` and the `/device/play` path, plus autoplay-next in both (`tellybox/cast/controller.py`, `device_sessions.py`); the scoped media route (`tellybox/web/app.py`). Revocation takes effect at the next autoplay decision and on the next listing.
4. **Admin**: matrix page, show settings checklist, approval-time checklist (inbox and add-by-URL), "copy from profile" on profile creation, dashboard zero-show flag, `AdminState` field. Strings via `_()`, `t()` (with `js_strings.py`) and `i18n.js`, then `scripts/i18n.sh` and nl/de translations.
5. **Kid app**: the empty state icon in `icons.js` and the grid; no new text.

## Verification
- Unit and API tests: intersection for groups; each enforcement path refuses a hidden show; revoke mid-episode finishes the episode and stops autoplay; revoke then re-grant restores resume; migration backfill; new show and new profile start hidden; copy-from is a one-off. Timer-related tests use the fake clock and fake Chromecast. `tests/test_i18n.py` must pass.
- Run the app (`run` skill) and drive the admin matrix and the kid app in a browser.
- Owner-in-the-loop checks on the real Chromecast and phone: assign a show to one profile only; the other profile's grid lacks it on TV and in-app; a group shows only the shared shows; revoke during playback; empty-state screen.

## Out of scope
Ratings or age groups, per-episode visibility, a PIN (A-7), write endpoints in the admin API, a post-upgrade review banner.
