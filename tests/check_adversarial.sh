#!/usr/bin/env bash
# Independent checks on the adversarial set (make_adversarial.sh, then `pal_restore.py restore`
# on that directory), beyond what pal_restore.py verifies itself. Each check names the defect
# it guards against (docs/decisions.md section 6). One PASS/FAIL line per check; exit non-zero
# if any failed.
set -uo pipefail
F=${1:?usage: check_adversarial.sh ADVERSARIAL_DIR}
R=$F/restored
S=$F/sidecars/restored
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] expected [$3]"; fail=1; fi; }
ok() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1: [$2] is false"; fail=1; fi; }

frames() { ffmpeg -v error -i "$1" -map "0:v:$2" -f framemd5 - | grep -v '^#' | cut -d, -f6 | md5sum; }
for pair in mixed.ts dvb.ts dvbbig.ts vobsub.vob late2.vob dvdrip.mkv lead.mkv hires.mkv xvid.avi \
            edit.mp4 flags.mkv cover.mp4 vp9.webm bluray.m2ts offset10.mkv chapters.mkv l21.mkv hdr.mkv eac3.mkv hevc8.mkv; do
  base=${pair%.*}
  [ -f "$R/$base.mkv" ] || { echo "FAIL $base: no output"; fail=1; continue; }
  check "$base frames identical" "$(frames "$F/$pair" 0)" "$(frames "$R/$base.mkv" 0)"
done
check "mixed second video track frames identical" "$(frames "$F/mixed.ts" 1)" "$(frames "$R/mixed.mkv" 1)"

first() { ffprobe -v error -select_streams "$2" -show_entries packet=pts_time -of csv=p=0 "$1" | grep -v N/A | sort -g | head -1; }
start() { ffprobe -v error -show_entries format=start_time -of csv=p=0 "$1"; }
# One time base: tracks read by different tools used to land hours apart (H.264 vs HEVC).
ok "mixed: both video tracks start together" \
  "python3 -c 'import sys; sys.exit(abs($(first "$R/mixed.mkv" v:0) - $(first "$R/mixed.mkv" v:1)) > 0.05)'"
# A capture whose clock starts at 50000 s gives a timeline starting near 0, not at 52135 s.
ok "dvbbig: timeline starts near 0" "python3 -c 'import sys; sys.exit($(start "$R/dvbbig.mkv") > 0.2)'"
# DVB and DVD subpicture tracks survive (mkvmerge could not read them from MPEG-TS/PS).
check "dvb: subtitle track kept" "$(ffprobe -v error -select_streams s -show_entries stream=codec_name -of csv=p=0 "$R/dvb.mkv")" "dvb_subtitle"
check "vobsub: subtitle track kept" "$(ffprobe -v error -select_streams s -show_entries stream=codec_name -of csv=p=0 "$R/vobsub.mkv")" "dvd_subtitle"
# The late second DVD audio track was found and probed (sample rate known).
check "late2: both audio tracks, 48 kHz" "$(ffprobe -v error -select_streams a -show_entries stream=sample_rate -of csv=p=0 "$R/late2.mkv" | paste -sd,)" "48000,48000"

# HEVC: VPS and SPS VUI timing rewritten to 24000/1001 (frame rate = time_scale / num_units_in_tick;
# 25 fps is 1/25). Read independently of pal_restore.py, from every header of the first packet.
hevc_timing() { ffmpeg -hide_banner -loglevel debug -i "$1" -map "0:v:$2" -frames:v 1 -c copy \
  -bsf:v trace_headers -f null - 2>&1 | sed -n 's/^\[trace_headers[^]]*\] *[0-9]* *//p' \
  | grep -E '^(vps|vui)_(num_units_in_tick|time_scale) ' | awk '{print $1"="$NF}' | sort -u | paste -sd' '; }
check "hdr: HEVC VPS/VUI timing" "$(hevc_timing "$R/hdr.mkv" 0)" \
  "vps_num_units_in_tick=1001 vps_time_scale=24000 vui_num_units_in_tick=1001 vui_time_scale=24000"
check "mixed: HEVC second track VPS/VUI timing" "$(hevc_timing "$R/mixed.mkv" 1)" \
  "vps_num_units_in_tick=1001 vps_time_scale=24000 vui_num_units_in_tick=1001 vui_time_scale=24000"
check "hevc8: HEVC VPS/VUI timing" "$(hevc_timing "$R/hevc8.mkv" 0)" \
  "vps_num_units_in_tick=1001 vps_time_scale=24000 vui_num_units_in_tick=1001 vui_time_scale=24000"
check "hevc8: pixel format kept" "$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt -of csv=p=0 "$R/hevc8.mkv")" "yuv420p"
# E-AC-3 5.1 becomes 6-channel 48 kHz FLAC with its subtitle track restored too.
check "eac3: audio is 6-channel 48 kHz FLAC" \
  "$(ffprobe -v error -select_streams a -show_entries stream=codec_name,sample_rate,channels -of csv=p=0 "$R/eac3.mkv")" "flac,48000,6"
check "eac3: subtitle track kept" "$(ffprobe -v error -select_streams s -show_entries stream=codec_name -of csv=p=0 "$R/eac3.mkv")" "subrip"
# HDR tags kept: pixel format, colour description, and the mastering display / light level SEI.
colour() { ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt,color_space,color_transfer,color_primaries \
  -of csv=p=0 "$1"; }
check "hdr: colour description kept" "$(colour "$R/hdr.mkv")" "yuv420p10le,bt2020nc,smpte2084,bt2020"
hdrsei() { ffprobe -v error -select_streams v:0 -read_intervals %+#1 -show_entries frame_side_data=side_data_type \
  -of csv=p=0 "$1" | grep -oE 'Mastering display metadata|Content light level metadata' | sort -u | paste -sd,; }
check "hdr: mastering display and light level kept" "$(hdrsei "$R/hdr.mkv")" \
  "Content light level metadata,Mastering display metadata"

# 96 kHz audio keeps its band: energy above 30 kHz within 3 dB of the input's (it was cut at 24 kHz).
hf() { ffmpeg -v info -nostdin -i "$1" -map 0:a:0 -af highpass=f=30000,highpass=f=30000,volumedetect -f null - 2>&1 \
       | sed -n 's/.*mean_volume: \(-*[0-9.]*\) dB/\1/p'; }
hin=$(hf "$F/hires.mkv"); hout=$(hf "$R/hires.mkv")
ok "hires: energy above 30 kHz kept ($hin dB -> $hout dB)" "python3 -c 'import sys; sys.exit(abs($hin - ($hout)) > 3)'"
check "hires: sample rate kept" "$(ffprobe -v error -select_streams a -show_entries stream=sample_rate -of csv=p=0 "$R/hires.mkv")" "96000"

# Track properties: regional language tag, name, default and commentary flags.
props() { mkvmerge -J "$1" | python3 -c 'import json,sys
for t in json.load(sys.stdin)["tracks"]:
    if t["type"] == "audio":
        p = t["properties"]
        print(p.get("language_ietf"), p.get("track_name"), p.get("default_track"), p.get("flag_commentary", False))'; }
check "flags: audio properties carried" "$(props "$R/flags.mkv" | paste -sd'|')" "pt-BR Main False False|en None True True"

# Cover art is an attachment, not a video track.
check "cover: one video track" "$(mkvmerge -J "$R/cover.mkv" | python3 -c 'import json,sys; print(sum(t["type"]=="video" for t in json.load(sys.stdin)["tracks"]))')" "1"
check "cover: attached as cover.png" "$(mkvmerge -J "$R/cover.mkv" | python3 -c 'import json,sys; print([a["file_name"] for a in json.load(sys.stdin)["attachments"]])')" "['cover.png']"

# AVI with B-frames: only the audio before the first decoded video frame is trimmed (the
# video start used to be taken as 80 ms, twice the decoder's 40 ms). Decoded audio length must
# be (input length - that lead) x 25025/24000, within 10 ms.
alen() { ffmpeg -v error -nostdin -i "$1" -map 0:a:0 -f s16le -ac 1 -ar 48000 - | wc -c; }
vlead=$(ffprobe -v error -select_streams v:0 -read_intervals %+#4 -show_entries frame=best_effort_timestamp_time \
        -of csv=p=0 "$F/xvid.avi" | grep -v N/A | head -1)
ain=$(alen "$F/xvid.avi"); aout=$(alen "$R/xvid.mkv")
ok "xvid: audio length scaled, only the $vlead s before the first frame trimmed ($ain -> $aout bytes)" \
  "python3 -c 'import sys; sys.exit(abs($aout / 96000 - ($ain / 96000 - $vlead) * 25025 / 24000) > 0.01)'"

# Chapters: exactly one edition (the video/audio intermediates used to add one each, with the
# unscaled 25 fps times, flagged default), and its times scaled.
editions() { mkvextract "$1" chapters - | grep -c '<EditionEntry>'; }
check "chapters.mkv: one chapter edition" "$(editions "$R/chapters.mkv")" "1"
check "edit.mp4: one chapter edition" "$(editions "$R/edit.mkv")" "1"
check "chapters.mkv: second chapter at 20.854 s" \
  "$(ffprobe -v error -show_entries chapter=start_time -of csv=p=0 "$R/chapters.mkv" | sed -n 2p)" "20.854167"
# Blu-ray LPCM: kept as PCM of the same depth; pitch actually measured on both tracks (the
# excerpts used to select nothing on a transport stream whose clock starts at 600 s).
check "bluray: audio codecs" "$(ffprobe -v error -select_streams a -show_entries stream=codec_name -of csv=p=0 "$R/bluray.mkv" | paste -sd,)" "flac,flac"
check "bluray: pitch measured on both tracks" "$(sed -n '/bluray.m2ts$/,/restored ->/p' "$F/restore.log" | grep -c 'pitch shift')" "2"
check "sidecars/film.mkv (6 s): pitch measured" "$(sed -n '/sidecars\/film.mkv$/,/restored ->/p' "$F/restore.log" | grep -c 'pitch shift')" "1"
# A Matroska input starting at 10 s gives a timeline starting at 0.
ok "offset10: timeline starts near 0" "python3 -c 'import sys; sys.exit($(start "$R/offset10.mkv") > 0.2)'"
# Opus with a layout outside the Vorbis set: encoded, every channel kept.
"$(dirname "$0")/../pal_restore.py" restore --audio-codec opus --out-dir "$F/opus" "$F/l21.mkv" > "$F/opus.log" 2>&1
check "opus 2.1: restored, 3 channels" "$(tail -1 "$F/opus.log"),$(ffprobe -v error -select_streams a -show_entries stream=channels -of csv=p=0 "$F/opus/l21.mkv" 2>/dev/null)" \
  "1 restored, 0 failed,3"

# Sidecars: exact text. 60 s -> 62.5625 s (written 62.562, rounded to even), 62 s -> 64.6479 s.
check "sidecar dot separator kept" "$(sed -n 2p "$S/film.dot.srt")" "00:01:02.562 --> 00:01:04.648"
check "sidecar short fractions read as ms" "$(sed -n 2p "$S/film.short.srt")" "00:01:02,615 --> 00:01:04,653"
check "sidecar hours 10" "$(sed -n 2p "$S/film.hours.srt")" "10:25:37,500 --> 10:25:38,543"
check "sidecar hours 100" "$(sed -n 6p "$S/film.hours.srt")" "104:16:15,000 --> 104:16:16,043"
check "sidecar hour 1" "$(sed -n 10p "$S/film.hours.srt")" "01:02:33,750 --> 01:02:34,793"
check "sidecar timestamp in text untouched" "$(sed -n 3p "$S/film.text.srt")" "Meet me at 12:30:00,000 sharp"
check "sidecar upper-case extension" "$(sed -n 2p "$S/film.upper.SRT")" "00:01:02,562 --> 00:01:04,648"
check "sidecar UTF-16 BOM kept" "$(head -c2 "$S/film.utf16.srt" | od -An -tx1 | tr -d ' \n')" "fffe"
check "sidecar UTF-16 retimed" "$(tail -c +3 "$S/film.utf16.srt" | iconv -f utf-16le -t utf-8 | sed -n 2p | tr -d '\r')" \
  "00:01:02,562 --> 00:01:04,648"
check "sidecar ASS reordered Format" "$(grep '^Dialogue' "$S/film.reorder.ass")" \
  "Dialogue: 0,Default,,0:01:02.56,0:01:04.65,0,0,0,,Reordered"
check "sidecar ASS hours 10" "$(grep '^Dialogue' "$S/film.h10.ass")" "Dialogue: 0,10:25:37.50,10:25:38.54,Default,,0,0,0,,ten hours"
check "sidecar SSA Marked and spaces" "$(grep '^Dialogue' "$S/film.marked.ssa")" \
  "Dialogue: Marked=0, 0:01:02.56, 0:01:04.65,Default,,0000,0000,0000,,Marked"
check "sidecar VTT header untouched" "$(sed -n 2p "$S/film.map.vtt")" "X-TIMESTAMP-MAP=LOCAL:00:00:10.000,MPEGTS:900000"
check "sidecar VTT hourless cue, settings kept" "$(sed -n 4p "$S/film.map.vtt")" "01:02.562 --> 01:04.648 align:start position:10%"
check "sidecar VTT hours 100" "$(sed -n 10p "$S/film.map.vtt")" "104:16:15.000 --> 104:16:16.043"
check "sidecar VTT NOTE untouched" "$(sed -n 13p "$S/film.map.vtt")" "NOTE written 00:00:01.000 --> 00:00:02.000"
check "sidecar Cyrillic name before Start retimed" "$(grep -a '^Dialogue' "$S/film.cyr.ass")" \
  "Dialogue: 0,Default,Михаил,0:01:02.56,0:01:04.65,0,0,0,,Привет"
check "sidecar Windows-1252 retimed" "$(sed -n 2p "$S/film.cp.srt")" "00:01:02,562 --> 00:01:04,648"
check "sidecar Windows-1252 text bytes kept" "$(sed -n 3p "$S/film.cp.srt" | od -An -tx1 | tr -d ' \n')" \
  "$(sed -n 3p "$F/sidecars/film.cp.srt" | od -An -tx1 | tr -d ' \n')"
check "sidecar SSA without Format line" "$(grep -a '^Dialogue' "$S/film.nofmt.ssa")" \
  "Dialogue: Marked=0,0:01:02.56,0:01:04.65,Default,,0000,0000,0000,,no format"
# Ownership: film.part2.srt belongs to film.part2.mkv (the longest matching base name).
owners() { sed -n "/^\/.*\/$1\.mkv\$/,/^\/\|^[0-9]/p" "$2" | grep -c 'sidecar retimed: film.part2.srt'; }
check "sidecar film.part2.srt retimed with film.part2.mkv, not film.mkv" \
  "$(owners film "$F/restore.log"),$(owners film.part2 "$F/restore.log")" "0,1"

exit $fail
