# Step 19: profile fields in the admin API (v11, HA-11)

Status: approved by the owner on 2026-10-06 (Home Assistant plans 6 and 7, delivered together). Branch: `step19/profile-fields`.

The Home Assistant integration wants each kid's picture (plan 6) and two settings, `watch_in_app` and `ui_mode` (plan 7). All three exist in the `profile` table and on `GET /api/kid/profiles`, but not in the admin state. This step adds them there, read-only. `pytellybox` 0.4.0 and `ha-tellybox` follow in their own repositories.

## Decisions (owner, 2026-10-06)

- Kid photos as Home Assistant image entities: yes. `watch_in_app` rides on the same change.
- One requirement for all three fields: HA-11 ("Should", read-only, outside HA-8, like HA-9 and HA-10). A-39 records that the photo is served without login and may appear in Home Assistant.
- `ui_mode` ships as a diagnostic in Home Assistant; that is a client decision, not part of this step.
- Additive: `api` stays 1, `/api/info` capabilities don't change. No endpoints to change the fields, no migration.

## Contract

Per item of `profiles[]` in `GET /api/admin/state` and every `/api/admin/events` event:

```json
{"avatar": "fox" | null,
 "picture": "/img/profile/1.jpg" | null,
 "watch_in_app": false,
 "ui_mode": "icons" | "text"}
```

`picture` is non-null exactly when the profile has an uploaded photo (the expression of `kid.py`'s `/api/kid/profiles`). The avatar key stays.

## What already exists

- `profile.picture_path`, `ui_mode` and `watch_in_app` columns; the admin settings page writes the last two.
- `/img/profile/<id>.jpg` and `/static/avatars/<key>.svg` are public routes (no login), as in the kid app.
- `state._profiles` already reads `visible_shows` from the database (HA-10), so these fields are right in the cold-start shape and while the cast service is unreachable.
- The admin hub re-reduces every 10 s (`ADMIN_REFRESH_S`) and on inbox changes; the settings page does not trigger a refresh.

## Work

- `tellybox/web/api/state.py::_profiles`: select `picture_path, ui_mode, watch_in_app` and add the three keys.
- `docs/admin-api.md`: type block and a note. `docs/PROGRESS.md`. The PRD already has HA-11.

## Tests (`tests/web/api/test_state.py`, HA-11)

- Photo gives `"/img/profile/<id>.jpg"`, none gives null, `avatar` still present.
- `watch_in_app` false by default, true when set; `ui_mode` `icons` by default, `text` when set.
- Cold start and unreachable cast service carry all three.
- A settings change is published to stream subscribers by the next hub refresh, not before (up to 10 s).
- The existing exact-shape test is updated for the new keys.
