#!/usr/bin/env bash
# Regenerate the Tellybox brand files from their SVG sources.
#   docs/images/brand/mark.svg, mark-dark.svg      hand-written (the mark)
#   docs/images/brand/src/*.svg                    lockup and social preview sources, text as <text>
#   tellybox/web/static/favicon.svg, icon.svg      hand-written (favicon and home-screen icon)
# Outputs: logo.svg, logo-dark.svg (text outlined, so they render without the font), social-preview.png,
# favicon.ico and the home-screen icon PNGs. Needs Inkscape, ImageMagick and the Fredoka font (OFL,
# https://fonts.google.com/specimen/Fredoka).
set -euo pipefail
cd "$(dirname "$0")/.."
B=docs/images/brand
S=tellybox/web/static
fc-list : family | grep Fredoka >/dev/null || { echo "Install the Fredoka font first" >&2; exit 1; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

inner() { sed -e '1d' -e '$d' -e '/<!--/,/-->/d' "$1" | tr -d '\n'; }   # mark body without <svg> wrapper
fill() { # src mark ink out
  python3 - "$@" <<'PY'
import sys
src, mark, ink, out = sys.argv[1:]
body = open(src).read().replace("@MARK@", mark).replace("@INK@", ink)
open(out, "w").write(body)
PY
}
outline() { inkscape "$1" --export-text-to-path --export-plain-svg --export-filename="$2" 2>/dev/null; }

fill $B/src/logo.svg "$(inner $B/mark.svg)" "#1E2A5A" "$tmp/logo.svg"
fill $B/src/logo.svg "$(inner $B/mark-dark.svg | sed 's/id="screen"/id="screen-d"/; s/#screen)/#screen-d)/')" "#FFFFFF" "$tmp/logo-dark.svg"
fill $B/src/social.svg "$(inner $B/mark.svg)" "" "$tmp/social.svg"
outline "$tmp/logo.svg" $B/logo.svg
outline "$tmp/logo-dark.svg" $B/logo-dark.svg
inkscape "$tmp/social.svg" -w 1280 -h 640 -o $B/social-preview.png 2>/dev/null

cp $B/mark-dark.svg $S/mark-dark.svg   # the admin header's mark

for s in 16 32 48; do inkscape $S/favicon.svg -w $s -h $s -o "$tmp/fav-$s.png" 2>/dev/null; done
magick "$tmp"/fav-16.png "$tmp"/fav-32.png "$tmp"/fav-48.png $S/favicon.ico
for s in 192 512; do inkscape $S/icon.svg -w $s -h $s -o $S/icon-$s.png 2>/dev/null; done
echo "Brand files regenerated."
