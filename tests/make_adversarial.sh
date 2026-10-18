#!/usr/bin/env bash
# Generate adversarial 25 fps inputs in the directory given as $1 (40 s each). Each one is a
# case that an earlier version of pal_restore.py got wrong; docs/decisions.md section 6 says which.
# Sidecar cases live in $1/sidecars/, apart from the media cases, because they need their own
# film.mkv / film.part2.mkv pair.
set -euo pipefail
out=${1:?usage: make_adversarial.sh DIR}
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$out/sidecars"
D=40
CHORD="aevalsrc='0.2*sin(2*PI*220*t)*(1+0.3*sin(2*PI*0.5*t))+0.15*sin(2*PI*277.18*t)+0.12*sin(2*PI*329.63*t)+0.08*sin(2*PI*440*t)+0.05*sin(2*PI*1320*t)':s=48000:d=$D"
VID="testsrc2=size=720x576:rate=25:duration=$D"
SMALL="testsrc2=size=320x240:rate=25:duration=$D"
X264=(-c:v libx264 -preset veryfast -crf 30 -pix_fmt yuv420p)
SRT='1\n00:00:05,000 --> 00:00:07,500\nFirst\n\n2\n00:00:20,000 --> 00:00:22,000\nMid\n\n3\n00:00:30,000 --> 00:00:32,000\nLast\n'
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
python3 "$here/mkvobsub.py" "$tmp/vs" 5 12 20 30
printf "$SRT" > "$tmp/s.srt"
printf ';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=20000\ntitle=One\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=20000\nEND=40000\ntitle=Two\n' > "$tmp/chap.meta"

# mixed.ts: H.264 and HEVC video and MP2 audio, timestamps starting at 50000 s.
ffmpeg -v error -y -f lavfi -i "$SMALL" -f lavfi -i "testsrc=size=320x240:rate=25:duration=$D" -f lavfi -i "$CHORD" \
  -map 0:v -map 1:v -map 2:a -c:v:0 libx264 -preset ultrafast -c:v:1 libx265 -preset ultrafast \
  -x265-params log-level=error -c:a mp2 -output_ts_offset 50000 -f mpegts "$out/mixed.ts" &

# hdr.mkv: 10-bit HEVC tagged HDR10 (BT.2020, PQ, mastering display and content light level):
# the VPS/VUI frame rate must be rewritten and every HDR tag kept.
ffmpeg -v error -y -f lavfi -i "$SMALL" -f lavfi -i "$CHORD" -pix_fmt yuv420p10le -c:v libx265 -preset ultrafast \
  -x265-params "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:hdr10=1:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400" \
  -c:a flac "$out/hdr.mkv" &

# dvb.ts / dvbbig.ts: broadcast capture with DVB bitmap subtitles; the second starts at 50000 s.
for c in dvb:0 dvbbig:50000; do
  ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -i "$tmp/vs.idx" -map 0:v -map 1:a -map 2:s \
    "${X264[@]}" -x264-params repeat-headers=1 -c:a mp2 -c:s dvbsub -output_ts_offset "${c#*:}" \
    -f mpegts "$out/${c%%:*}.ts" &
done

# vobsub.vob: DVD shape, MPEG-2 video, two AC-3 tracks, DVD subpictures.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -f lavfi -i "sine=f=500:d=$D:sample_rate=48000" \
  -i "$tmp/vs.idx" -map 0:v -map 1:a -map 2:a -map 3:s -c:v mpeg2video -b:v 3M -c:a ac3 -b:a 192k \
  -c:s copy -f vob "$out/vobsub.vob" &

# late2.vob: a second AC-3 track that starts 20 s in (found only by a deep probe).
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -itsoffset 20 -f lavfi -i "sine=f=600:d=20:sample_rate=48000" \
  -map 0:v -map 1:a -map 2:a -c:v mpeg2video -b:v 3M -c:a ac3 -b:a 192k -f vob "$out/late2.vob" &

# dvdrip.mkv: H.264, AC-3 and VobSub subtitles in Matroska.
( ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" "${X264[@]}" -c:a ac3 "$tmp/r.mkv" &&
  mkvmerge -q -o "$out/dvdrip.mkv" "$tmp/r.mkv" --language 0:eng "$tmp/vs.idx" ) &

# lead.mkv: audio starts 0.5 s before the video; text subtitles.
( ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" "${X264[@]}" -c:a flac "$tmp/l.mkv" &&
  mkvmerge -q -o "$out/lead.mkv" --sync 0:500 "$tmp/l.mkv" "$tmp/s.srt" ) &

# hires.mkv: 96 kHz 24-bit FLAC with content up to 48 kHz (white noise plus the chord).
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -f lavfi -i "anoisesrc=r=96000:a=0.05:d=$D" \
  -filter_complex "[1:a]aresample=96000[c];[c][2:a]amix=inputs=2:normalize=0[a]" -map 0:v -map "[a]" \
  "${X264[@]}" -c:a flac -sample_fmt s32 -bits_per_raw_sample 24 "$out/hires.mkv" &

# xvid.avi: MPEG-4 Part 2 with B-frames (packets carry no presentation time) and MP3.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -c:v mpeg4 -bf 2 -q:v 4 -c:a libmp3lame -b:a 192k "$out/xvid.avi" &

# edit.mp4: video offset 2 s by an edit list, MP4 text subtitles, chapters.
ffmpeg -v error -y -itsoffset 2 -f lavfi -i "$VID" -f lavfi -i "$CHORD" -i "$tmp/s.srt" -i "$tmp/chap.meta" \
  -map 0:v -map 1:a -map 2:s -map_chapters 3 "${X264[@]}" -c:a aac -c:s mov_text "$out/edit.mp4" &

# flags.mkv: regional language tag and commentary flag on the audio tracks.
( ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -f lavfi -i "$CHORD" -map 0:v -map 1:a -map 2:a \
    "${X264[@]}" -c:a ac3 "$tmp/f.mkv" &&
  mkvmerge -q -o "$out/flags.mkv" --language 1:pt-BR --track-name 1:Main --default-track-flag 1:no \
    --language 2:en --commentary-flag 2:yes --default-track-flag 2:yes "$tmp/f.mkv" ) &

# cover.mp4: cover art stored as an attached picture video stream.
( ffmpeg -v error -y -f lavfi -i "testsrc=size=300x300:d=1" -frames:v 1 "$tmp/c.png" &&
  ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -i "$tmp/c.png" -map 0:v -map 1:a -map 2:v \
    "${X264[@]}" -c:a aac -c:v:1 png -disposition:v:1 attached_pic "$out/cover.mp4" ) &

# vp9.webm: VP9 and Opus.
ffmpeg -v error -y -f lavfi -i "$SMALL" -f lavfi -i "$CHORD" -c:v libvpx-vp9 -deadline realtime -cpu-used 8 \
  -b:v 300k -c:a libopus "$out/vp9.webm" &

# bluray.m2ts: Blu-ray shape, interlaced MPEG-2, AC-3 and LPCM (pcm_bluray, which Matroska
# cannot hold as it is), clock starting at 600 s.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -f lavfi -i "$CHORD" -map 0:v -map 1:a -map 2:a \
  -c:v mpeg2video -b:v 3M -flags +ilme+ildct -c:a:0 ac3 -c:a:1 pcm_bluray -ac:a:1 2 \
  -mpegts_m2ts_mode 1 -output_ts_offset 600 -f mpegts "$out/bluray.m2ts" &

# offset10.mkv: Matroska whose every track starts at 10 s.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" "${X264[@]}" -c:a flac -output_ts_offset 10 "$out/offset10.mkv" &

# chapters.mkv: Matroska with chapters (the video and audio intermediates must not add editions).
( ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -i "$tmp/chap.meta" -map 0:v -map 1:a -map_chapters 2 \
    "${X264[@]}" -c:a ac3 "$out/chapters.mkv" ) &

# l21.mkv: 2.1 AC-3, a layout outside the Vorbis set (for an --audio-codec opus run).
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -filter_complex "[1:a]pan=2.1|FL=c0|FR=c0|LFE=c0[a]" \
  -map 0:v -map "[a]" "${X264[@]}" -c:a ac3 "$out/l21.mkv" &

# eac3.mkv: E-AC-3 (Dolby Digital Plus) 5.1, the audio of most WEB-DL releases, with embedded SRT.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -filter_complex "[1:a]pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0[a]" \
  -map 0:v -map "[a]" "${X264[@]}" -c:a eac3 -b:a 384k "$tmp/e.mkv" &&
  mkvmerge -q -o "$out/eac3.mkv" "$tmp/e.mkv" --language 0:eng "$tmp/s.srt" &

# hevc8.mkv: 8-bit HEVC (the main-set and hdr.mkv cases are H.264, MPEG-2 and 10-bit HEVC).
ffmpeg -v error -y -f lavfi -i "$SMALL" -f lavfi -i "$CHORD" -pix_fmt yuv420p -c:v libx265 -preset ultrafast \
  -x265-params log-level=error -c:a flac "$out/hevc8.mkv" &

# sidecars/: film.mkv and film.part2.mkv (6 s) with adversarial sidecars.
for name in film film.part2; do
  ffmpeg -v error -y -f lavfi -i "testsrc2=size=320x240:rate=25:duration=6" -f lavfi -i "sine=f=440:d=6" \
    -c:v libx264 -preset ultrafast -c:a flac "$out/sidecars/$name.mkv" &
done
wait
python3 - "$out/sidecars" <<'PY'
import sys
from pathlib import Path
d = Path(sys.argv[1])
def w(name, text, enc="utf-8", bom=b""):
    (d / name).write_bytes(bom + text.encode(enc))
w("film.dot.srt", "1\n00:01:00.000 --> 00:01:02.000\nDot separator\n")
w("film.short.srt", "1\n00:01:00,50 --> 00:01:02,5\nShort fraction\n")
w("film.utf16.srt", "1\r\n00:01:00,000 --> 00:01:02,000\r\nUTF-16\r\n", "utf-16-le", b"\xff\xfe")
w("film.hours.srt", "1\n10:00:00,000 --> 10:00:01,000\nten\n\n2\n100:00:00,000 --> 100:00:01,000\nhundred\n\n"
                    "3\n1:00:00,000 --> 1:00:01,000\none digit\n")
w("film.text.srt", "1\n00:01:00,000 --> 00:01:02,000\nMeet me at 12:30:00,000 sharp\n")
w("film.reorder.ass", "[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Style, Name, Start, End, "
  "MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,Default,,0:01:00.00,0:01:02.00,0,0,0,,Reordered\n")
w("film.h10.ass", "[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Name, "
  "MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,10:00:00.00,10:00:01.00,Default,,0,0,0,,ten hours\n")
w("film.marked.ssa", "[Script Info]\nScriptType: v4.00\n\n[Events]\nFormat: Marked, Start, End, Style, Name, "
  "MarginL, MarginR, MarginV, Effect, Text\nDialogue: Marked=0, 0:01:00.00, 0:01:02.00,Default,,0000,0000,0000,,Marked\n")
w("film.map.vtt", "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:10.000,MPEGTS:900000\n\n"
  "01:00.000 --> 01:02.000 align:start position:10%\nhourless\n\n00:01:00.000 --> 00:01:02.000\nwith hours\n\n"
  "100:00:00.000 --> 100:00:01.000\nthree-digit hours\n\nNOTE written 00:00:01.000 --> 00:00:02.000\n")
w("film.upper.SRT", "1\n00:01:00,000 --> 00:01:02,000\nUpper\n")
w("film.part2.srt", "1\n00:01:00,000 --> 00:01:02,000\nPart two\n")
# UTF-8 without BOM: "х" is D1 85, and 0x85 must not end a line. Reordered Format line.
w("film.cyr.ass", "[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Style, Name, Start, End, "
  "MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,Default,Михаил,0:01:00.00,0:01:02.00,0,0,0,,Привет\n")
# Windows-1252 (not UTF-8): FFmpeg needs to be told the encoding to verify it.
w("film.cp.srt", "1\n00:01:00,000 --> 00:01:02,000\nWait\u2026 Caf\u00e9\n", "cp1252")
# SSA with no Format line: the default v4 field order applies.
w("film.nofmt.ssa", "[Script Info]\nScriptType: v4.00\n\n[Events]\n"
  "Dialogue: Marked=0,0:01:00.00,0:01:02.00,Default,,0000,0000,0000,,no format\n")
PY
