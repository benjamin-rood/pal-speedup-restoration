# pal-speedup-restoration

Undo **PAL speedup**: films and TV shows made at 23.976 fps were sold in PAL regions at 25 fps by
playing every frame 4.27 % faster. Nothing was added, dropped or blended, so the restoration is exact.
The video plays at its original speed again, and the audio returns to its original pitch (the speedup
raised it by about 0.72 semitones, 72 cents).

`pal_restore.py` is a single-file tool that does this to a file or a whole directory tree.

| Part | What it does | Re-encoded? |
|---|---|---|
| Video | Retimed from 25 to 24000/1001 fps; the frame rate stored inside the H.264, HEVC or MPEG-2 stream is rewritten to match | **No.** Every decoded frame is bit-identical to the input. |
| Audio, every track | Resampled by exactly 24000/25025, which restores speed and pitch together | Yes: to FLAC (lossless from the decoded audio) by default, or Opus |
| Embedded subtitles and chapters | Times scaled by 25025/24000 | No |
| Sidecar `.srt` `.ass` `.ssa` `.vtt` files | Times scaled by 25025/24000; only timing is edited, encoding and line endings are kept | No |

A 96-minute film becomes 100 minutes 6 seconds long.

## Safe by construction

- **Every output is verified before it is published.** The tool checks the frame rate (container and
  bitstream), frame count, durations, track and timeline offsets, audio start and end, the measured
  pitch shift, subtitle and chapter timing, and each sidecar's cues. A file that fails verification
  leaves nothing behind.
- Outputs are never overwritten and inputs are never modified. Outputs go to `restored/` next to each
  input, or to `--out-dir`.
- Stopping the run (Ctrl-C, kill, closing the terminal) leaves nothing of the file in progress behind.
  Re-running only does the files whose outputs do not exist yet.

## Requirements

Arch Linux (or any Linux with the same tools): FFmpeg (with `soxr`, plus `libopus` for Opus),
MKVToolNix (`mkvmerge` and `mkvextract`) and [uv](https://docs.astral.sh/uv/), which supplies the one
Python dependency (`numpy`, used to measure pitch) automatically. All work runs on the CPU.

```bash
sudo pacman -S ffmpeg mkvtoolnix-cli uv
```

## Use

```bash
./pal_restore.py doctor                                # checks the tools; must print OK
./pal_restore.py restore --dry-run /path/to/videos     # lists what would be written
./pal_restore.py restore /path/to/videos               # restores into restored/ next to each input
./pal_restore.py restore --audio-codec opus FILE.mkv   # Opus instead of FLAC
./pal_restore.py verify ORIGINAL.mkv RESTORED.mkv      # re-run verification on an existing pair
```

Inputs are searched recursively for `.mkv .mp4 .m4v .mov .avi .mpg .mpeg .vob .ts .m2ts .mts .webm`
and written as Matroska. Files that are not 25 fps are skipped. Options and the full procedure are in
[docs/pipeline.md](docs/pipeline.md).

Only the **sound** of the original can be judged by ear: the pitch check confirms the audio was slowed
by the right amount, not that the source was really sped up. Listen to one restored file before running
a batch. If voices sound too low, that source was already pitch-corrected and should not be restored.

## Documents

- [docs/pipeline.md](docs/pipeline.md): the design: what is done, how, what is verified, options,
  failure handling and what is not handled (VC-1, MPEG-4 Part 2, VP9 and AV1 keep their internal frame
  rate field; a later part of a split DVD VOB set must be concatenated first).
- [docs/decisions.md](docs/decisions.md): why it works as it does: every review finding and the
  decision taken, and the known limits.
- [tests/README.md](tests/README.md): the test suites. All use synthetic media generated locally;
  nothing is downloaded.

## Tests

```bash
tests/run_all.sh        # every suite from scratch, a few minutes; exits non-zero on any failure
tests/test_units.py     # just the unit tests of the pure functions, under a second
```

All use synthetic media generated locally; see [tests/README.md](tests/README.md) for each suite and
what every fixture exercises. The same command runs on every push in GitHub Actions
(`.github/workflows/tests.yml`).

## Status

Tested on synthetic media (all suites pass) and on the first episode of a real 1080p HEVC release,
which passed every check and was confirmed by ear.

## Licence

[MIT](LICENSE). FFmpeg and MKVToolNix are run as separate programs and are not part of this repository;
each has its own licence.
