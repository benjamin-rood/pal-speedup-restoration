#!/usr/bin/env bash
# Independent checks on a restored fixture directory (made by make_fixtures.sh and then
# `pal_restore.py restore`), beyond what pal_restore.py verifies itself:
#   * decoded video frames are bit-identical (no re-encode), per section 8 of docs/pipeline.md
#   * the frame rate inside the H.264 / MPEG-2 bitstream itself is 24000/1001
#   * sidecar subtitles are scaled exactly, and ASS text / non-event lines are untouched
# Prints one PASS/FAIL line per check and exits non-zero if any failed.
set -uo pipefail
F=${1:?usage: check_outputs.sh FIXTURE_DIR}
R=${2:-$F/restored}
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] expected [$3]"; fail=1; fi; }

frames() { ffmpeg -v error -i "$1" -map 0:v:0 -f framemd5 - | grep -v '^#' | cut -d, -f6 | md5sum; }
for pair in aac.mp4 dvd.mpg h264.mkv r441.mkv capture.ts late.mkv subbed.mp4 angles.mkv; do
  base=${pair%.*}
  [ -f "$R/$base.mkv" ] || { echo "FAIL $base: no output"; fail=1; continue; }
  check "$base frames identical" "$(frames "$F/$pair")" "$(frames "$R/$base.mkv")"
done

# Header fields of the first video packet, as "name = value" lines, read with trace_headers.
headers() { ffmpeg -hide_banner -loglevel debug -i "$1" -map 0:v:0 -frames:v 1 -c copy \
  -bsf:v trace_headers -f null - 2>&1 | sed -n 's/^\[trace_headers[^]]*\] *[0-9]* *//p' \
  | grep -E "^($2) " | awk '{print $1"="$NF}' | sort -u | paste -sd' '; }
# H.264 VUI: frame rate = time_scale / (2 * num_units_in_tick), so 24000/1001 fps is 1001/48000.
for base in aac h264 r441 capture late subbed; do
  check "$base H.264 SPS timing" "$(headers "$R/$base.mkv" 'num_units_in_tick|time_scale')" \
    "num_units_in_tick=1001 time_scale=48000"
done
# MPEG-2: frame_rate_code 1 is 24000/1001 (3 is 25); the extension fields must stay 0.
check "dvd MPEG-2 sequence header" \
  "$(headers "$R/dvd.mkv" 'frame_rate_code|frame_rate_extension_n|frame_rate_extension_d')" \
  "frame_rate_code=1 frame_rate_extension_d=0 frame_rate_extension_n=0"

# 5.000 s x 25025/24000 = 5.213541.. ; 115.04 s -> 119.953..
check "srt first cue" "$(sed -n 2p "$R/h264.srt")" "00:00:05,214 --> 00:00:07,820"
check "srt last cue" "$(sed -n 10p "$R/h264.srt")" "00:01:54,698 --> 00:01:59,953"
check "srt text untouched" "$(sed -n 11p "$R/h264.srt")" "Last line"
check "vtt short-form cue" "$(sed -n 3p "$R/h264.en.vtt")" "00:05.214 --> 00:07.820"
check "vtt last cue" "$(sed -n 6p "$R/h264.en.vtt")" "01:54.698 --> 01:59.953"
check "ass dialogue times scaled, text untouched" "$(grep -a '^Dialogue' "$R/h264.ass" | tr -d '\r')" \
  'Dialogue: 0,0:00:05.21,0:00:07.82,Default,,0,0,0,,Said at 0:00:05.00 {\t(0,500,\fs20)}hi'
check "ass comment times scaled" "$(grep -a '^Comment' "$R/h264.ass" | cut -d, -f2,3)" "1:04:28.45,1:04:33.70"
check "ass CRLF line endings kept" "$(grep -ac $'\r$' "$R/h264.ass")" "$(grep -ac $'\r$' "$F/h264.ass")"

exit $fail
