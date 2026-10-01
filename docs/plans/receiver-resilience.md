# Receiver resilience (CR-6 hardening), before step 11

## Context
On 2026-10-01 the owner found the TV on the Default Media Receiver from the very start of a pick: the Tellybox receiver had failed to launch, and the cast service fell back (CR-6). The reason is lost:
- the containers are recreated on each deploy, so the cast logs start at the last deploy;
- `receiver_error` lives only in memory.

The current behaviour, in `CastController.play` and `PyChromecastDevice._launch_receiver`:
- one launch attempt, with an 8 s timeout;
- any `ReceiverUnavailable` gives `fallback_until = now + 30 min`, so every pick and autoplay goes to the Default Media Receiver for 30 minutes;
- a receiver that vanishes mid-episode ends the episode as `TAKEN_OVER` or `DISCONNECTED`.

Approved by the owner on 2026-10-01 (all five items). Branch `receiver/resilience`. The receiver is a published app other households use, so `docs/receiver-protocol.md` stays backward compatible: fields may be added, never removed or changed.

## Design

### 1. Receiver event log (migration `011_receiver_events.sql`)
- `receiver_event (id, at TEXT, kind TEXT, detail TEXT, duration_ms INTEGER, episode_id INTEGER REFERENCES episode(id) ON DELETE SET NULL)`, indexed on `at`. The `kind` values are:
  - `launch_ok`: with `duration_ms`, and the attempt number in `detail`;
  - `launch_failed`: with the reason, the attempt and `duration_ms`;
  - `refused`: the device rejects the app outright (below);
  - `fallback`: the Default Media Receiver was used; `detail` says until when, or "this episode";
  - `lost`: our app vanished mid-episode;
  - `recovered` and `recover_failed`;
  - `page_error`: a receiver `log` message at level error, or the `hello` reporting SDK load retries.
- The cast service is the only writer (`store.record_receiver_event`), and a failed write never breaks playback.
- The worker's purge deletes rows older than 21 days (`purge.py`, alongside history).
- `store.receiver_summary(conn, now)` returns the last failure (kind, detail, at), the failures in the last 24 h, and the launches in the last 24 h. It is used for the cast state.

### 2. Retry before falling back
- `CastDevice.play(..., app_id=, launch_timeout_s=)`. The fake device and `PyChromecastDevice` take the launch timeout per call (default `RECEIVER_LAUNCH_TIMEOUT_S` = 8 s).
- `ReceiverUnavailable` gains `refused: bool`. It is true when `start_app` fails at once with `RequestFailed` (an unregistered app id fails in 0.02 s, spike S8) or the launch error is `CANCELLED` (seen at rollout until a reboot). Timeouts and other errors are not refusals.
- In `CastController.play`: when attempt 1 fails and wasn't refused, quit our half-started app if the receiver shows our app id (best effort), wait 1 s (the clock-aware sleep the controller already uses), then run attempt 2 with `launch_timeout_s = 15`. A refusal skips the retry.
- Each attempt records `launch_ok` or `launch_failed` with its duration.

### 3. Short fallback with backoff
- The controller's state is `receiver_failures` (consecutive failed picks; reset on any successful launch) and `fallback_until`.
- After both attempts fail:
  - the current pick plays on the Default Media Receiver;
  - `receiver_failures += 1`;
  - `fallback_until` follows the backoff: 1st failure → None (the next pick or autoplay episode tries our receiver again), 2nd → +5 min, 3rd → +15 min, 4th and later → +30 min;
  - a refusal sets +30 min at once.
- Record `fallback` with what was chosen.
- Moving back from the Default Media Receiver to ours at an autoplay boundary is a normal `play` with `app_id`. `_adopt_warm` already handles moving between our two receivers.

### 4. Mid-episode recovery
- In `_on_receiver`, when our cast session ends and the new receiver status shows **no app or the Backdrop** (`app_id` None or `E8C28D3C`), and not because of a reconnect, our own stop or quit, or the night hold, while an episode is current and not stopping:
  - treat it as our receiver vanishing, not a takeover, and record `lost`;
  - **once per episode** (a flag on `Current`), and only while the timer allows playback (`self._decision`, not time-up or blocked), relaunch through the normal load path at the current estimated position (the same position logic as resume), keeping the watch session and profiles; record `recovered` or `recover_failed`;
  - if recovery isn't allowed or fails, end the episode as today.
- A status with **another app id** (YouTube, …) is a takeover, as today (WT-9).

### 5. Receiver page (`tellybox/web/receiver/cast.js`)
- Load the CAF SDK with up to 3 attempts (2 s and 4 s apart). Each attempt is a fresh `<script>` with `onerror`.
- Count the retries, and add `sdk_attempts` and `load_ms` (time from page start to `ctx.start()`) to the `hello` message. That's an added field, so it's backward compatible; document it in `docs/receiver-protocol.md`.
- `hello` with `sdk_attempts > 1` records a `page_error` event; `load_ms` is logged.
- Old-Chrome-safe ES5 style, as the rest of the file.

### Dashboard and API
- The cast state's `receiver` block (`docs/cast-api.md`) gains:
  - `failures_24h`, `launches_24h`;
  - `last_failure: {kind, detail, at} | null`;
  - `refused: bool` (the last failure was a refusal and the fallback is active).
- The admin dashboard's "TV receiver" line shows the fallback with its time, the last failure ("Last problem: … at 14:02") and, when refused, "The Chromecast refuses the Tellybox receiver; restarting the Chromecast usually fixes it."
- nl and de.
- The admin API (`/api/admin/state`) passes the receiver block through unchanged, if it already does; don't widen the API otherwise.

## Tests (fake clock, fake Chromecast)
- Retry: a slow first launch that times out, then success on attempt 2, so we stay on our receiver (`launch_failed` then `launch_ok` recorded).
- A refusal: no retry, Default Media Receiver, 30-minute fallback, `refused` in the state.
- Backoff: failure 1 means the next pick tries our receiver again; then 5, 15 and 30 minutes; a success resets the count.
- The autoplay boundary after a one-off fallback goes back to our receiver.
- Mid-episode: the app vanishes (Backdrop), so we relaunch at the position and the watch session continues. A second vanish in the same episode ends it. Another app id is still `TAKEN_OVER`. No recovery when time is up, during our own stop, or during the night hold.
- The event rows and the summary; the purge after 21 days; migration 011.
- The dashboard rendering of the new fields; the receiver `hello` with `sdk_attempts`.
- `cast.js`: a node test of the SDK retry loader if it can be made pure; otherwise a manual check with `receiver/dev.html` is enough.

## Verification after merge (owner, on the TV)
1. A normal pick still starts on the Tellybox receiver, cold and warm.
2. Forced fallback: set a wrong receiver app ID in Settings, pick (Default Media Receiver; the dashboard shows the failure), restore the ID, pick again (back on the Tellybox receiver, no 30-minute wait).
3. Mid-episode: during an episode, stop the receiver app from another phone's Google Home ("Stop casting" only stops media; a reboot or `quit_app` from the dev box also works). The episode comes back at the same spot.
