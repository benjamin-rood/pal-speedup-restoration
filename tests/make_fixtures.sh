#!/usr/bin/env bash
# Generate synthetic 25 fps PAL-speedup-style test inputs in the directory given as $1.
# DUR (seconds, default 120) sets the length of the main fixtures.
set -euo pipefail
out=${1:?usage: make_fixtures.sh DIR}
mkdir -p "$out"
D=${DUR:-120}
# A chord with harmonics, amplitude-modulated so it is not a pure steady tone.
CHORD="aevalsrc='0.2*sin(2*PI*220*t)*(1+0.3*sin(2*PI*0.5*t))+0.15*sin(2*PI*277.18*t)+0.12*sin(2*PI*329.63*t)+0.08*sin(2*PI*440*t)+0.05*sin(2*PI*1320*t)':s=48000:d=$D"
VID="testsrc2=size=720x576:rate=25:duration=$D"
X264=(-c:v libx264 -preset veryfast -crf 30 -pix_fmt yuv420p)

cat > "$out/subs.srt" <<'S'
1
00:00:05,000 --> 00:00:07,500
First line

2
00:01:00,000 --> 00:01:02,000
Middle line

3
00:01:50,000 --> 00:01:55,040
Last line
S
cat > "$out/chapters.txt" <<'C'
CHAPTER01=00:00:00.000
CHAPTER01NAME=Start
CHAPTER02=00:00:40.000
CHAPTER02NAME=Middle
CHAPTER03=00:01:30.000
CHAPTER03NAME=End
C

# 1. H.264 MKV: mono 16-bit FLAC + 5.1 AC-3, embedded SRT, chapters, sidecars.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" \
  -filter_complex "[1:a]asplit=2[a][b];[b]pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0[b6]" \
  -map 0:v -map "[a]" -map "[b6]" "${X264[@]}" \
  -c:a:0 flac -sample_fmt:a:0 s16 -c:a:1 ac3 -b:a:1 448k \
  -metadata:s:a:0 language=eng -metadata:s:a:0 title="Main" \
  -metadata:s:a:1 language=deu -metadata:s:a:1 title="Surround" "$out/h264.tmp.mkv"
mkvmerge -q -o "$out/h264.mkv" --chapters "$out/chapters.txt" "$out/h264.tmp.mkv" \
  --language 0:eng "$out/subs.srt"
rm "$out/h264.tmp.mkv"
cp "$out/subs.srt" "$out/h264.srt"
cp "$out/subs.srt" "$out/h264.en.srt"
cat > "$out/h264.en.vtt" <<'V'
WEBVTT

00:05.000 --> 00:07.500
First line

01:50.000 --> 01:55.040
Last line
V
printf '[Script Info]\r\nScriptType: v4.00+\r\n\r\n[Events]\r\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\nDialogue: 0,0:00:05.00,0:00:07.50,Default,,0,0,0,,Said at 0:00:05.00 {\\t(0,500,\\fs20)}hi\r\nComment: 0,1:01:50.00,1:01:55.04,Default,,0,0,0,,note\r\n' > "$out/h264.ass"

# 2. MPEG-2 program stream (DVD-like) with AC-3 audio starting 0.3 s after the video.
ffmpeg -v error -y -f lavfi -i "$VID" -itsoffset 0.3 -f lavfi -i "$CHORD" \
  -map 0:v -map 1:a -c:v mpeg2video -b:v 3M -c:a ac3 -b:a 192k -f vob "$out/dvd.mpg"

# 3. MP4 with H.264 + AAC (encoder priming gives audio that starts before the video).
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" \
  "${X264[@]}" -c:a aac -b:a 160k "$out/aac.mp4"

# 4. MKV with 44.1 kHz 24-bit FLAC.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" "${X264[@]}" \
  -af aresample=44100 -c:a flac -sample_fmt s32 -bits_per_raw_sample 24 "$out/r441.mkv"

# 5. MPEG-TS with H.264 (SPS repeated in-band before every keyframe) and MP2 audio.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" "${X264[@]}" -x264-params repeat-headers=1 \
  -c:a mp2 -b:a 192k -f mpegts "$out/capture.ts"

# 6. MKV whose video starts 0.5 s after the audio (video delayed in the container).
mkvmerge -q -o "$out/late.mkv" --sync 0:500 "$out/r441.mkv"

# 7. MP4 with a mov_text subtitle.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "$CHORD" -f srt -i "$out/subs.srt" \
  -map 0:v -map 1:a -map 2:s "${X264[@]}" -c:a aac -b:a 128k -c:s mov_text "$out/subbed.mp4"

# 8. MKV with two MPEG-2 video tracks (angles), AC-3 audio, and a font attachment.
ffmpeg -v error -y -f lavfi -i "$VID" -f lavfi -i "testsrc=size=720x576:rate=25:duration=$D" \
  -f lavfi -i "$CHORD" -map 0:v -map 1:v -map 2:a -c:v mpeg2video -b:v 2M -c:a ac3 -b:a 192k \
  "$out/angles.tmp.mkv"
font=$(fc-match -f '%{file}' sans)
mkvmerge -q -o "$out/angles.mkv" --attachment-mime-type font/ttf --attach-file "$font" "$out/angles.tmp.mkv"
rm "$out/angles.tmp.mkv"

# 9. A 23.976 fps file that must be skipped.
ffmpeg -v error -y -f lavfi -i "testsrc2=size=320x240:rate=24000/1001:duration=5" \
  -c:v libx264 -preset veryfast "$out/film.mkv"

rm "$out/subs.srt" "$out/chapters.txt"
