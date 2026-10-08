# Tests

All tests run on synthetic 25 fps media generated locally; nothing is downloaded.

```bash
F=/some/scratch/dir                      # must not be inside the repository
tests/make_fixtures.sh "$F"              # ~10 s, about 200 MB
./pal_restore.py restore "$F"            # expect: 8 restored, 1 skipped, 0 failed
tests/check_outputs.sh "$F"              # independent checks; every line PASS, exit 0
mkdir -p "$F/fail" && uv run -q --with numpy python tests/failure_path.py "$F" "$F/fail"
```

| Fixture | Exercises |
|---|---|
| `h264.mkv` (+ `.srt`, `.en.srt`, `.en.vtt`, `.ass` sidecars) | H.264, two audio tracks (16-bit FLAC, 5.1 AC-3), embedded SRT, chapters, sidecar retiming |
| `dvd.mpg` | MPEG-2 program stream, AC-3 starting 0.3 s after the video |
| `aac.mp4` | MP4, AAC whose priming puts audio before the first frame (trim path) |
| `r441.mkv` | 44.1 kHz 24-bit FLAC |
| `capture.ts` | MPEG-TS, H.264 with SPS repeated in-band, MP2 audio |
| `late.mkv` | video starting 0.5 s after the audio |
| `subbed.mp4` | MP4 text subtitles with empty "clear" samples |
| `angles.mkv` | two MPEG-2 video tracks, font attachment |
| `film.mkv` | 23.976 fps input, must be skipped |

`check_outputs.sh` checks what `pal_restore.py` does not check about itself: decoded frames
bit-identical to the input (no re-encode), the H.264 SPS / MPEG-2 sequence header timing
fields, and exact sidecar timestamps (including that ASS dialogue text and CRLF line endings
are untouched).
