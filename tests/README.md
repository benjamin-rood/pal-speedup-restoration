# Tests

All tests run on synthetic 25 fps media generated locally; nothing is downloaded. Scratch
directories must not be inside the repository.

```bash
F=/some/scratch/dir
# Main set: the common shapes of PAL-speedup releases.
tests/make_fixtures.sh "$F"              # ~10 s, about 200 MB
./pal_restore.py restore "$F"            # expect: 8 restored, 1 skipped, 0 failed
tests/check_outputs.sh "$F"              # independent checks; every line PASS, exit 0
mkdir -p "$F/fail" && uv run -q --with numpy python tests/failure_path.py "$F" "$F/fail"

# Adversarial set: cases earlier versions got wrong (docs/decisions.md sections 6 and 7).
A=/some/other/scratch/dir
tests/make_adversarial.sh "$A"           # ~5 s
./pal_restore.py restore "$A" > "$A/restore.log" 2>&1   # expect: 20 restored, 0 failed
tests/check_adversarial.sh "$A"          # reads $A/restore.log, runs one Opus restore; every line PASS

# Command line: missing inputs, collisions, bad options, symlinks, --out-dir = input dir, SIGTERM, SIGHUP.
tests/check_cli.sh /some/new/scratch/dir # must not exist; every line PASS, exit 0
```

| Fixture (main set) | Exercises |
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

| Fixture (adversarial set) | Exercises |
|---|---|
| `mixed.ts` | H.264 + HEVC video, clock starting at 50 000 s: both tracks must start together |
| `dvb.ts`, `dvbbig.ts` | DVB bitmap subtitles; the second with a clock at 50 000 s (timeline must start near 0) |
| `vobsub.vob` | DVD shape: MPEG-2, two AC-3, DVD subpictures in MPEG-PS |
| `late2.vob` | second AC-3 track starting 20 s in (deep probe) |
| `dvdrip.mkv` | VobSub in Matroska |
| `lead.mkv` | audio 0.5 s before the video, embedded text subtitles |
| `hires.mkv` | 96 kHz 24-bit FLAC with content to 48 kHz (band must be kept) |
| `xvid.avi` | MPEG-4 Part 2 with B-frames, no presentation times in the packets |
| `edit.mp4` | video offset 2 s by an edit list, MP4 text subtitles, chapters |
| `flags.mkv` | `pt-BR` language, track name, commentary and default flags |
| `cover.mp4` | cover art (must become an attachment, not a video track) |
| `vp9.webm` | VP9 + Opus |
| `bluray.m2ts` | Blu-ray shape: interlaced MPEG-2, AC-3 and LPCM, clock at 600 s (LPCM conversion, pitch measured on a far-off clock) |
| `offset10.mkv` | Matroska whose tracks all start at 10 s (timeline must start at 0) |
| `chapters.mkv` | chapters (exactly one edition, scaled) |
| `hdr.mkv` | 10-bit HEVC tagged HDR10 (bitstream frame rate rewritten, HDR tags kept) |
| `l21.mkv` | 2.1 AC-3, restored with `--audio-codec opus` (layout outside the Vorbis set) |
| `sidecars/` | `film.mkv`, `film.part2.mkv` and eleven adversarial sidecars: `.` separator, short fractions, UTF-16, hours 1/10/100, timestamp in text, ASS reordered Format, ASS hours 10, SSA `Marked=`, VTT header/NOTE/cue settings, upper-case extension, `film.part2.srt` ownership, Cyrillic name before Start in a reordered Format line, Windows-1252 SRT, SSA without a Format line |

`tests/mkvobsub.py` writes the small VobSub pair the bitmap-subtitle fixtures are made from.

`check_outputs.sh` and `check_adversarial.sh` check what `pal_restore.py` does not check about
itself: decoded frames bit-identical to the input (no re-encode), bitstream timing fields, exact
sidecar text, audio band, track properties, and the specific defects each adversarial case was
made for. Run against the outputs of commit `ff4f58a` (before the round-1 fixes) it reports 23 failures,
and against `abd51f6` (before the round-2 fixes) every check added in round 2 fails, so its
checks can fail.
