"""Fake yt-dlp CLI for tests: mimics only the arguments tellybox.ytdlp uses. No network."""
import json
import re
import sys
import time
from pathlib import Path

import yt_dlp

FIXTURE = Path(yt_dlp.__file__).with_name("video.json")
PLAYLIST = Path(yt_dlp.__file__).with_name("playlist_flat.json")


def fail(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# SB-1: segments the fake SponsorBlock "knows" for URLs containing "sponsored".
SEGMENTS = [
    {"start_time": 30.0, "end_time": 60.0, "category": "sponsor", "title": "Sponsor", "type": "skip"},
    {"start_time": 55.0, "end_time": 70.0, "category": "selfpromo", "title": "Unpaid/Self Promotion", "type": "skip"},
    {"start_time": 100.0, "end_time": 101.0, "category": "poi_highlight", "title": "Highlight", "type": "poi"},
    {"start_time": 700.0, "end_time": 710.0, "category": "intro", "title": "Intro", "type": "skip"},
]


def sponsorblock(argv, url, info):
    """Mimic SponsorBlockPP (after_filter, before download) and ModifyChaptersPP."""
    flag = next((f for f in ("--sponsorblock-remove", "--sponsorblock-mark") if f in argv), None)
    if flag is None:
        return None
    if "sbdown" in url:
        fail("ERROR: Preprocessing: Unable to communicate with SponsorBlock API: "
             "HTTP Error 503: Service Unavailable. Aborting.")
    cats = argv[argv.index(flag) + 1].split(",")
    found = [dict(s) for s in SEGMENTS if "sponsored" in url and (s["category"] in cats or s["type"] == "poi")]
    info["sponsorblock_chapters"] = found
    return flag


def render(template, fields, info):
    template = template.replace("%()j", json.dumps(info).replace("%", "%%"))
    template = re.sub(r"%\((\w+)\)j", lambda m: json.dumps(info.get(m.group(1))).replace("%", "%%"), template)
    return template % fields


def main(argv):
    if "--version" in argv:
        if yt_dlp.__version__ == "broken":
            fail("ImportError: something is broken")
        print(yt_dlp.__version__)
        return
    url = argv[argv.index("--") + 1]
    info = json.loads(FIXTURE.read_text())
    if "unavailable" in url:
        fail("WARNING: [youtube] abc: some warning\nERROR: [youtube] abc: Video unavailable")
    if "flaky" in url:
        fail("ERROR: unable to download video data: HTTP Error 503: Service Unavailable")
    if "slow" in url:
        time.sleep(30)
    if "nochannel" in url:
        del info["channel"]
    if "live" in url:
        info.update(is_live=True, live_status="is_live")
    if "--flat-playlist" in argv:  # CI-7 preview: the recorded flat listing, honouring --playlist-end
        info = json.loads(PLAYLIST.read_text())
        if "--playlist-end" in argv:
            info["entries"] = info["entries"][: int(argv[argv.index("--playlist-end") + 1])]
        if "watch" in url:  # a plain video URL: yt-dlp answers with the video itself
            info = json.loads(FIXTURE.read_text())
    elif "playlist" in url:
        info = {"_type": "playlist", "id": "PL1", "entries": []}
    if "-J" in argv:
        print(json.dumps(info))
        return
    if "nochapters" in url:
        info["chapters"] = None
    original_chapters = info.get("chapters")
    sb = sponsorblock(argv, url, info)
    if "--simulate" in argv:  # SB-3 re-check: --print without a stage prints at the video stage
        for p in (argv[i + 1] for i, a in enumerate(argv) if a == "--print"):
            print(render(p, {}, info), flush=True)
        return

    opts = {}
    prints = []
    it = iter(argv)
    for a in it:
        if a == "-o":
            v = next(it)
            kind, _, tmpl = v.partition(":") if v.startswith("thumbnail:") else ("default", "", v)
            opts[kind] = tmpl
        elif a == "--print":
            prints.append(next(it))
        elif a in ("--progress-template", "-f", "--convert-thumbnails"):
            opts[a] = next(it)
    stage_prints = lambda stage: [p.split(":", 1)[1] for p in prints if p.startswith(stage + ":")]
    for p in stage_prints("before_dl"):
        print(render(p, {"format_id": info["format_id"]}, {**info, "chapters": original_chapters}), flush=True)
    tmpl = opts["--progress-template"].split(":", 1)[1]
    for fmt, total, est in (("136", 1000, "NA"), ("140", "NA", 200)):
        size = total if total != "NA" else est
        for done in range(0, size + 1, size // 50):
            fields = {"info.format_id": fmt, "progress.downloaded_bytes": done,
                      "progress.total_bytes": total, "progress.total_bytes_estimate": est}
            print(render(tmpl, fields, info), flush=True)
        if "stall" in url:
            time.sleep(30)
    video = Path(opts["default"].replace("%(ext)s", "mp4"))
    video.write_bytes(b"fake mp4")
    if "nothumb" not in url:
        Path(opts["thumbnail"].replace("%(ext)s", opts["--convert-thumbnails"])).write_bytes(b"jpg")
    print("[Merger] Merging formats into " + str(video), flush=True)
    info["filepath"] = str(video)
    if sb == "--sponsorblock-remove" and any(c["type"] == "skip" for c in info["sponsorblock_chapters"]):
        removed = [c for c in info["sponsorblock_chapters"] if c["type"] == "skip"]
        if not info.get("chapters"):  # like ModifyChapters: one chapter for the whole video
            info["chapters"] = [{"start_time": 0.0, "end_time": info["duration"], "title": info["title"]}]
        info["duration"] -= max(c["end_time"] for c in removed) - min(c["start_time"] for c in removed)
    for p in stage_prints("after_move"):
        print(render(p, {"filepath": str(video)}, info), flush=True)


main(sys.argv[1:])
