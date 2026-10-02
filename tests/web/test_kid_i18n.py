"""NF-13: the kid app's screen-reader labels (tellybox/web/static/i18n.js)."""

from __future__ import annotations

import re
from pathlib import Path

from tellybox.i18n import LANGUAGES

STATIC = Path(__file__).resolve().parents[2] / "tellybox" / "web" / "static"
ENTRY = re.compile(r'^\s*("(?:[^"\\]|\\.)*")\s*:\s*("(?:[^"\\]|\\.)*"),\s*$')


def _dictionaries() -> dict[str, dict[str, str]]:
    """{lang: {key: label}} parsed from the LABELS object in i18n.js."""
    text = (STATIC / "i18n.js").read_text(encoding="utf-8")
    block = text[text.index("const LABELS = {"):text.index("\n};", text.index("const LABELS = {"))]
    dicts: dict[str, dict[str, str]] = {}
    current = None
    for line in block.splitlines()[1:]:
        if m := re.match(r"^  (\w+): \{$", line):
            current = dicts.setdefault(m.group(1), {})
        elif m := ENTRY.match(line):
            assert current is not None
            current[m.group(1)[1:-1]] = m.group(2)[1:-1]
    return dicts


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"%\((\w+)\)s", text))


def test_every_language_has_every_label():
    dicts = _dictionaries()
    assert set(dicts) == set(LANGUAGES)
    keys = set(dicts["en"])
    assert keys, "no labels parsed"
    for lang, labels in dicts.items():
        assert set(labels) == keys, lang
        for key, value in labels.items():
            assert value, (lang, key)
            assert _placeholders(value) == _placeholders(key), (lang, key)
    assert all(k == v for k, v in dicts["en"].items())


def test_every_label_used_is_in_the_dictionaries():
    keys = set(_dictionaries()["en"])
    used = set()
    for name in ("app.js", "sky.js", "profiles.js", "reader.js"):
        used |= set(re.findall(r'\btr\("((?:[^"\\]|\\.)*)"', (STATIC / name).read_text(encoding="utf-8")))
    used |= set(re.findall(r'aria-label="([^"]+)"', (STATIC / "index.html").read_text(encoding="utf-8")))
    assert used, "no labels found"
    assert used <= keys, used - keys


def test_reader_helpers_in_node(tmp_path):
    """KA-11/KA-12: the pure reader-UI helpers behave (run with node when it is installed)."""
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = tmp_path / "check.mjs"
    script.write_text(
        f"""
import assert from "node:assert/strict";
const {{ isReaderGroup, matchesQuery, timeLeftText }} = await import("{(STATIC / 'reader.js').as_uri()}");
const ps = [{{profile_id: 1, ui_mode: "text"}}, {{profile_id: 2, ui_mode: "icons"}}, {{profile_id: 3}}];
assert.equal(isReaderGroup(ps, [1]), true);
assert.equal(isReaderGroup(ps, [1, 2]), false);   // mixed group: icon UI
assert.equal(isReaderGroup(ps, [1, 3]), false);   // unknown mode: icon UI
assert.equal(isReaderGroup(ps, [9]), false);
assert.equal(isReaderGroup(ps, []), false);
assert.equal(matchesQuery("Peppa Pig", "pig"), true);
assert.equal(matchesQuery("Café Kids", "cafe"), true);
assert.equal(matchesQuery("Bluey", "xyz"), false);
assert.equal(matchesQuery("Bluey", "  "), true);
assert.equal(timeLeftText({{fraction_left: 0.5}}, false), "50% of today's time left");
assert.equal(timeLeftText({{unlimited: true}}, false), "No time limit");
assert.equal(timeLeftText({{fraction_left: 0.5}}, true), "Time's up for today");
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
