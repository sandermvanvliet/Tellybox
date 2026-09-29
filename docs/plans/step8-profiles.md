# Step 8: Kid profiles (v2, PR-1..PR-4)

Status: approved 2026-09-29; merged as PR #1 and deployed. The playback checks on the real TV are open in `docs/plans/step8-device-checks.md`.

## Decisions (owner, 2026-09-29)

- **Who's watching:** each device remembers its pick. It asks again after the 04:00 reset or after 30 minutes without use. An avatar button on every screen switches kids at any time.
- **Session max (WT-3) per profile:** each kid has their own viewing session and 15-minute break. If A has watched 90 minutes, B can still start. When A joins B, both count.
- **Pictures (PR-1):** a set of bundled avatars in the sky style, plus an optional photo upload.
- **Household profile:** the admin renames it into the first kid, and it keeps its history and continue watching. A profile can be deleted with a confirm, but the last one can't.

These become PRD assumptions A-12..A-15 in part 0.

## What already exists

- The data model has had profiles since v1: `profile` (name, `picture_path`, allowance, counting mode, max session), with `daily_usage`, `watch_session_profile`, `playback_position` and `override_log` all per profile. No history migration is needed.
- `WatchTimer` (`tellybox/timer/watch_timer.py`) already keeps usage per profile. It charges every profile (PR-4, `_accrue`) and takes the minimum remaining time (`_remaining_s`). **The gap:** "watching" means *all* profiles. The viewing session and exhaustion are timer-wide.
- `CastController.override(kind, value, profile_id)` and the cast API already take a profile. `store.save_position` and `open_watch_session` take a list of profiles.
- Admin: the settings page loops over every profile, and `_profiles_context` on the dashboard is per profile. `library._FILE_REFS` already includes `profile.picture_path`. `tellybox/images.py` handles uploads (5 MB cap, JPEG/PNG/WebP, re-encoded).
- The kid API uses `household_profile_id()` everywhere; `_fraction_left` has a v2 branch for several profiles.

## Part 0: contract (controller, before the subagents)

1. Migration `005_profiles.sql`:
   - `profile.avatar TEXT` (a built-in avatar key, nullable);
   - `profile.sort_order INTEGER NOT NULL DEFAULT 0`, backfilled with the id.

   The picture rule: an uploaded photo (`picture_path`) wins over `avatar`. With neither, the frontend draws a placeholder.
2. `tellybox/avatars.py`: `AVATARS: tuple[str, ...]` with 8 keys (fox, bear, rabbit, owl, cat, dog, frog, penguin). The files are `tellybox/web/static/avatars/{key}.svg`.
3. Timer API stubs (signatures and docstrings only) in `watch_timer.py` and `models.py`; see part A.
4. The cast state and API shape, in `docs/cast-api.md` (new) and `docs/kid-api.md`; see parts A and B.
5. PRD: add A-12..A-15, and update `docs/PROGRESS.md` ("step 8 in progress").

## Part A: timer and cast service (subagent A)

**Timer**
- Add the idea of *watchers*: the profiles watching the current pick. Only watchers accrue time (`_accrue`), and `remaining_s`, the session max, blocked and grace all come from the watchers.
- Per-profile viewing session: each profile gets its own `session_start`/`inactive_since`. A profile that stops watching (replaced by another group) starts its break at that moment. Exhaustion (`_exhausted`) is kept per profile.
- New and changed API (`profile_ids=None` means every profile, so the single-profile behaviour and the existing tests keep working):
  - `on_pick(now, profile_ids=None) -> Decision`: if the group may start, it becomes the watchers and their sessions start or extend.
  - `set_watchers(now, profile_ids) -> Decision`: for restart re-attach.
  - `watchers: frozenset[int]`.
  - `group_decision(now, profile_ids) -> Decision`: whether this group could start, with no side effects.
  - `profile_status(now, profile_id) -> ProfileStatus(remaining_s, can_start, reason, session_elapsed_s)`.
- A group can start only if every member has time left and none is blocked or out of session (PR-4). Blocking a profile that isn't watching doesn't stop the TV.
- Snapshot version 2 stores the watchers, the per-profile sessions and the per-profile exhaustion. A v1 snapshot restores with every profile treated as a watcher.

**Controller and API**
- `CastController.play(episode_id, profile_ids)`: the ids are checked against the `profile` table. The list must not be empty, and `PlayRefused` carries the group decision.
  - Autoplay keeps `Current.profile_ids`.
  - Restart recovery calls `set_watchers` with the recovered profiles.
- Resume position for a group: the most recently updated unfinished position among its members. It is saved for every member, as today.
- Profiles deleted while watching are dropped from `Current.profile_ids` and from the watchers on the next `persist`, so there are no FK errors.
- Cast API `POST /play {"episode_id", "profile_ids": [..]}`, validated with 1–20 ids.
- `state()["timer"]["profiles"][]` gains `remaining_s`, `can_start`, `reason` and `watching`, and `now_playing` gains `profile_ids`. The top-level timer fields describe the current watchers.

## Part B: web backend, kid API and admin (subagent B)

**Kid API** (`tellybox/web/kid.py`, per `docs/kid-api.md`)
- `GET /api/kid/profiles` returns `[{profile_id, name, picture, avatar, time_up, fraction_left, last_five, unlimited}]` in `sort_order`. `name` is only a screen-reader label.
  - `picture` is `/img/profile/{id}.jpg` when a photo exists, otherwise null.
  - `avatar` is the avatar key, or null.
- `GET /api/kid/home?profiles=1,2` and `/api/kid/shows/{id}?profiles=…` take a group:
  - continue watching merges the members' lists by recency, with no duplicates;
  - tile progress is the most recent position among the members.
  - Unknown or missing ids give 400.
- `POST /api/kid/play {"episode_id", "profile_ids"}` returns 409 when any member is out of time.
- KidState gains `profiles: {id: {fraction_left, last_five, unlimited, time_up}}`, `watching: [ids]` and `day` (the timer day, which the client uses for the 04:00 expiry). `sky` and `time_up` stay and describe the current watchers. The hub is unchanged, since each client reduces the shared state for its own group.
- `GET /img/profile/{id}.jpg` serves the photo (with the same path checks as the other images).

**Admin**
- New `/admin/profiles` page (and a nav entry):
  - list with avatar or photo, name and time today; add, rename, reorder up/down;
  - an avatar picker showing the 8 SVGs as radio tiles;
  - photo upload and remove through `images.py`, stored under `media/profiles/`;
  - delete with a confirm. It's refused for the last profile, and while the profile is watching (checked through the cast state).
- The settings page keeps the per-profile allowance, mode and max session (it already loops over profiles) and shows each avatar next to the name.
- Dashboard (AD-3): per-profile rows show the avatar, a "watching" badge, and the override buttons per profile plus "everyone". History (AD-4): the avatars/names on each row, and a profile filter (`?profile=`).
- `cast_client.play(episode_id, profile_ids)`.
- i18n: the new admin strings go through `_()`/`t()` + `js_strings.py`. Run `scripts/i18n.sh` and translate into nl and de.

## Part C: kid app frontend (subagent C)

- Eight avatar SVGs in `tellybox/web/static/avatars/`: a simple flat animal head on a round sky-coloured disc, readable at 64 px, matching the style of the existing `icons.js` and `sky.js`.
- A new `#/who` route, the who's-watching screen (PR-2):
  - large round profile pictures, with no visible text;
  - tapping one toggles it, with a clear selected ring and a check icon, and several can be selected (watching together);
  - a big "go" button (arrow icon) appears once at least one kid is selected;
  - profiles whose time is up show at night (dimmed, with a moon) and can't be selected.
- The selection is kept in `localStorage` as `{profiles, day, last_used}` (inside try/catch). The picker shows when there is no selection, the `day` changed, it's been more than 30 minutes since `last_used`, or a selected profile no longer exists.
- An avatar button (the selected pictures, stacked) in the top corner of home and show pages goes to `#/who`.
- Every API call passes the group. The sky and the time-up screen are reduced from `state.profiles` for the device's own group: the lowest fraction, and time up if any member is out of time.
- The now-playing bar shows while anyone is watching; the pause button works for anyone (as today).
- Screen-reader labels go in `static/i18n.js` ("Who's watching?", "Go", "Change who's watching"), in en, nl and de.
- `scripts/kid_mock_server.py`: add profiles (one out of time), the picker, and watching together. Screenshots of every state at three sizes go to the controller for review.

## Execution (at most 3 subagents, cheaper model)

1. **Controller (Opus):** part 0 on `step8/profiles`, committed, so all three start from the same contract.
2. **Three parallel subagents (Sonnet),** each in its own git worktree off the contract commit, with the briefs in `docs/plans/step8-handoff.md`:
   - A touches `tellybox/timer/`, `tellybox/cast/` and their tests;
   - B touches `tellybox/web/` (not `static/`), the admin, `tellybox/locale/` and the web tests;
   - C touches only `tellybox/web/static/` and `scripts/kid_mock_server.py`.

   B and C code against the contract, with the existing fake cast client and mock server.
3. **Controller:** merge in the order A, B, C, then run the full suite, `scripts/i18n.sh` and `tests/test_i18n.py`.
   - Review the diff for: XSS in profile names (admin and kid labels), path checks on profile photos, the timer against the WT/PR tests, and deleting a profile while it's watching.
   - Review the screenshots, then run a real-TV smoke test.

## Tests

- Timer (fake clock, subagent A):
  - only watchers accrue; a group needs every member to have time (PR-4);
  - a per-profile session max, with B starting after A's 90 minutes;
  - A joining B, and A's break starting when replaced;
  - blocking a non-watcher leaves playback alone, and blocking a watcher stops it now;
  - grace per profile;
  - the v1 → v2 snapshot, restore with watchers, and the rollover at 04:00 across a group.
- Controller (fake Chromecast): play with a group, autoplay keeps the group, group resume position, a refusal carrying the right reason, recovery re-attaching watchers, and a profile deleted mid-play.
- Kid API: the profiles list and its picture rules; home and shows for a group (merge, progress); a 400 on bad ids; a 409 when one member is out of time; the per-profile KidState; hidden content still never shows.
- Admin: profile CRUD, the avatar key validated against `AVATARS`, photo upload and remove, the delete guards, per-profile overrides, and the history filter.
- The existing 743 tests keep passing.

## Real-device checks (owner, on the home server)

1. In admin, rename Household to kid A and give them an avatar. Add kid B with a photo. Continue watching and history for kid A still show the v1 data.
2. On the phone, check that the who's-watching screen shows both pictures. Pick A and play: only A's time counts (dashboard).
3. On a second device, pick B and play a different episode. It replaces A's (A-5), and from then on only B's time counts.
4. Pick A+B together: the time counts for both. Give A 1 minute left: the group finishes the episode and stops, and B alone can then still start.
5. Block B while A is watching alone: A's playback continues. On B's device, B's avatar shows night.
6. Leave a device for 30 minutes and check that it asks who's watching again.
