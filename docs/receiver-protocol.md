# Tellybox receiver protocol (v7, CR-1..CR-8)

The Tellybox receiver is a Cast Web Receiver (static files in `tellybox/web/receiver/`). It plays the same signed MP4 files as the Default Media Receiver, through the standard media namespace (`urn:x-cast:com.google.cast.media`), so loading, pause, resume, stop and media status work exactly as before (PB-*, WT-9). On top of that, the cast service and the receiver talk JSON on one custom namespace:

```
urn:x-cast:tellybox
```

The receiver makes no network calls of its own apart from loading media and the images named in messages (CR-7). It shows no text and no numbers (KA-2).

## Receiver → cast service

```jsonc
{"type": "hello", "v": 1, "ua": "<navigator.userAgent>"}
// On every sender connect (SENDER_CONNECTED). The cast service answers with a full "state".

{"type": "stats", "v": 1, "dropped": 12, "total": 43200, "state": "PLAYING"}
// Every 60 s while media is loaded: dropped and total video frames since the page started (CR-8).
// The cast service logs it.

{"type": "log", "v": 1, "level": "info" | "error", "msg": "..."}
// Optional diagnostics (image failed, player error). The cast service logs it.
```

## Cast service → receiver

One message type, always the complete state, so a receiver that missed messages is correct after the next one:

```jsonc
{
  "type": "state", "v": 1,
  "sky": {                                  // the current watchers' time (CR-2), like the kid app's KidState.sky
    "fraction_left": 0.62 | null,           // 0..1; null when unlimited
    "last_five": false,                     // dusk
    "unlimited": false                      // hide the corner sky
  },
  "time_up": false,                         // no more picks today, or blocked (CR-3)
  "loading": null | {                       // CR-4: set just before a load, cleared once PLAYING
    "artwork": "http://…/img/show/2.jpg" | null,
    "thumb": "http://…/img/episode/4.jpg"
  },
  "up_next": null | {                       // CR-5: set only when autoplay will continue after this episode
    "thumb": "http://…/img/episode/5.jpg"
  }
}
```

Image URLs are absolute, on the web service at `TELLYBOX_MEDIA_BASE_URL` (the same base as `/media/`). The cast service sends `state`:
- on `hello`;
- before each load (with `loading`);
- on a change of the sky phase, `time_up`, `loading` or `up_next`;
- when `fraction_left` moves by 0.01 or more;
- at least every 30 s while the Tellybox receiver is the running app.

## What the receiver shows

The receiver combines the last `state` with its own player state (from `PlayerManager` events). The first matching row wins:

| # | Condition | Screen |
|---|---|---|
| 1 | `loading` set, and the player is not yet PLAYING | **Loading** (CR-4): full screen, the show artwork large (or a plain sky if null) with the episode thumbnail, and a slow opacity pulse. It also shows between autoplay episodes, when the cast service sends `loading` for the next episode. |
| 2 | Player PLAYING, PAUSED or BUFFERING | **Video**, plus the **corner sky** (CR-2) in the top-right corner, about 12% of the screen width: the sun at `sunPosition()`, with the dusk colours when `last_five`. It's hidden when `sky.unlimited`. The **up-next card** (CR-5) shows in the bottom-right in the last 10 s of media when `up_next` is set. |
| 3 | `time_up` and the player is IDLE | **Night** (CR-3): full screen, a navy sky with the moon and a few stars over the hill. The cast service quits the app after 10 minutes. |
| 4 | Otherwise (IDLE) | **Idle**: a full-screen day sky with the sun, and nothing else. |

The sky states and colours are the kid app's (`tellybox/web/static/sky.js`, `app.css`), so kids recognise the same picture on the TV.

## Performance on the 1st-gen Chromecast (CR-8)

- The overlays are static: gradients drawn as backgrounds, and no `filter`, `backdrop-filter`, `box-shadow` or blur.
- The sun moves by `transform` only, at most every 30 s (on `state`), with no CSS transition or animation while video plays.
- Full-screen layers (loading, night, idle) are only visible when no video frame is showing. The loading pulse animates `opacity` only.
- The `stats` message reports dropped frames, so the real-device checks can compare with and without the overlay.

## Fallback (CR-6)

If the receiver app can't be launched (unregistered device, Pages down, the SDK failing) within `RECEIVER_LAUNCH_TIMEOUT_S`, the cast service plays the same pick on the Default Media Receiver and keeps using it for 30 minutes. With the Default Media Receiver, nothing is sent on this namespace, and time's up ends in the Chromecast backdrop as in v1.
