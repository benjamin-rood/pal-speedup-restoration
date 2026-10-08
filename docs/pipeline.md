# Arch Linux PAL Speedup Restoration Pipeline

## Objective

Restore a set of videos that were converted from 23.976 fps to 25 fps by **PAL speedup**: every original frame was kept and simply played 4.27 % faster, and the audio was sped up with it, raising its pitch by about 0.72 semitones (−72 cents to undo).

Because no frames were added, dropped or blended, nothing needs to be inverse-telecined, decimated or deblended. The restoration is exact:

| Component | Operation | Re-encoded? |
|---|---|---|
| Video | Timestamps relabelled 25 → 24000/1001 fps; MPEG-2 and H.264 bitstream frame-rate fields rewritten to match | **No.** Every frame is bit-identical to the input. |
| Audio (every track) | Resampled by exactly 24000/25025, restoring original speed **and** pitch together | Yes, to FLAC (lossless from the decoded audio) by default |
| Embedded subtitles (text and image) | Timestamps scaled by 25025/24000 | No |
| Chapters | Times scaled by 25025/24000 | No |
| Sidecar `.srt` / `.ass` / `.ssa` / `.vtt` next to the input | Timestamps scaled by 25025/24000 (ASS: only event Start/End fields) | No (byte-level edit, encoding preserved) |

The duration of each file increases by a factor of 25025/24000 ≈ 1.042708 (a 96:00 film becomes 100:06).

Everything runs locally on the CPU. No GPU, ROCm or network services are involved.

The machine is expected to have:

- Arch Linux
- A normal user account with `sudo` access
- Internet access for installing packages

If you are an agent executing this document: never run interactive or never-terminating commands in the foreground.

---

# 1. Operating principles

- Never modify, move or overwrite an input file.
- Never re-encode video.
- Outputs go to a `restored/` directory next to each input (or a directory given with `--out-dir`), as `<base>.mkv`, keeping the input's base name so other tools (for example the subtitle pipeline's `<base>.<lang>.srt` convention) continue to match.
- A restored file is only moved into place after its verification passes. A failed file leaves nothing behind.
- Existing outputs are never overwritten; the file is reported as skipped.
- Record every deviation from this document and the reason.

---

# 2. Inspection

```bash
uname -r
cat /etc/os-release
pacman -Q ffmpeg mkvtoolnix-cli uv 2>/dev/null || true
df -h "$HOME"
```

Pick one representative input file and confirm it is 25 fps:

```bash
ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,avg_frame_rate,r_frame_rate,field_order \
    -of default=nw=1 "/path/to/sample.mkv"
ffprobe -v error -show_entries stream=index,codec_type,codec_name,sample_rate,channels:stream_tags=language,title \
    -of compact "/path/to/sample.mkv"
```

`avg_frame_rate` must be `25/1`. A `field_order` of `tt`/`bb` does not matter: PAL film releases are often flagged interlaced while carrying progressive frames, and the restoration only changes timing.

Disk space: outputs are roughly the size of the input plus the growth from FLAC audio (about 1–2.5 GB per two hours depending on channel count). Use `--audio-codec opus` (section 7) if space matters more than lossless audio.

---

# 3. Install dependencies

```bash
sudo pacman -S --needed ffmpeg mkvtoolnix-cli uv
```

- `ffmpeg` provides decoding, the SoX resampler (`soxr`), and the `mpeg2_metadata` bitstream filter.
- `mkvtoolnix-cli` provides `mkvmerge`, which retimes video, subtitles and chapters without re-encoding.
- `uv` runs the restoration script with its single Python dependency (`numpy`, used for the pitch verification) in an isolated, cached environment. Nothing is installed into the system Python.

---

# 4. Install the restoration script

```bash
mkdir -p "$HOME/ai/pal-restore"
cp /path/to/pal_restore.py "$HOME/ai/pal-restore/pal_restore.py"
chmod +x "$HOME/ai/pal-restore/pal_restore.py"
```

The script starts with `#!/usr/bin/env -S uv run --script` and declares its dependencies inline, so it runs directly:

```bash
"$HOME/ai/pal-restore/pal_restore.py" --help
"$HOME/ai/pal-restore/pal_restore.py" doctor
```

`doctor` must print `OK`. It checks `ffmpeg`, `ffprobe`, `mkvmerge`, that FFmpeg's `aresample` supports the `soxr` resampler, that the `mpeg2_metadata` bitstream filter exists, and that `mkvmerge` supports `--chapter-sync`, `--default-duration` and `--fix-bitstream-timing-information`. Resolve every reported problem before continuing.

---

# 5. How the restoration works

For each input:

1. **Probe.** Files whose video is not 25 fps are skipped.
2. **Audio.** Each audio track is decoded and passed through:
   ```text
   aresample=50050:resampler=soxr:precision=28,asetrate=48000,aresample=<original rate>:resampler=soxr:precision=28
   ```
   Resampling to 50050 Hz and relabelling as 48000 Hz slows the audio by exactly 50050/48000 = 25025/24000 and lowers the pitch by the same ratio. The two integer rates are required because `asetrate` only accepts integers; the ratio is exact for every source sample rate (48 kHz, 44.1 kHz, 32 kHz and so on). The final resample returns to the track's original sample rate.
   The existing offset between audio and video is preserved and scaled. Audio that started before the first video frame is trimmed sample-accurately in FFmpeg, because a negative `mkvmerge --sync` can only drop whole codec blocks.
   Language, track title and default flag are carried over.
3. **Video.** `mkvmerge --default-duration <tid>:24000/1001p` relabels the frame timing. For H.264, `--fix-bitstream-timing-information` also rewrites the SPS timing. For MPEG-2 (DVD sources), the stream is first passed through FFmpeg's `mpeg2_metadata` bitstream filter to set the sequence-header frame rate to 24000/1001, without decoding.
4. **Subtitles and chapters.** `mkvmerge --sync <tid>:0,25025/24000` scales every embedded subtitle track; `--chapter-sync 0,25025/24000` scales the chapters. Attachments (fonts) and global tags are copied.
5. **Verify** (section 6). Only on success is the output moved from its temporary directory into place.
6. **Sidecars.** Files next to the input named `<base>.srt`, `<base>.<anything>.srt` (and `.ass`, `.ssa`, `.vtt`) are retimed into the output directory under the same names.

---

# 6. Verification performed on every file

The script refuses to publish an output unless all of these hold:

| Check | Requirement |
|---|---|
| Frame rate | Output video reports 24000/1001 |
| Frame count | Identical to the input |
| Video duration | Input duration × 25025/24000, within 50 ms |
| A/V sync | Each audio track's end, measured from the video start, equals the input's × 25025/24000 within 100 ms |
| Audio start | Input start offset × 25025/24000 (or 0 if it was trimmed), within 50 ms |
| Pitch | Measured shift between input and output audio is −72.4 cents ± 8 cents (log-spectrum cross-correlation over three excerpts) |
| Subtitles | Same event count per track; first and last events scaled within 10 ms |
| Chapters | Same count; each start scaled within 10 ms |

The pitch check confirms the audio was slowed in the right direction by the right amount. It cannot judge whether the original is musically correct; that is what section 8's listening check is for.

Re-run verification on an existing pair at any time:

```bash
"$HOME/ai/pal-restore/pal_restore.py" verify "/path/to/original.mkv" "/path/to/restored/original.mkv"
```

---

# 7. Options

```text
restore [--target-fps 24000/1001] [--audio-codec flac|opus] [--opus-kbps-per-channel 96]
        [--out-dir DIR] [--dry-run] PATH...
```

- `PATH` may be files or directories. Directories are searched recursively for `.mkv .mp4 .m4v .mov .avi .mpg .mpeg .vob .ts .m2ts .mts .webm`; existing `restored/` directories are not descended into.
- `--target-fps` defaults to `24000/1001`. Use `24` only for material known to come from a true 24.000 fps master.
- `--audio-codec flac` (default) is lossless relative to the decoded source audio. `opus` uses `--opus-kbps-per-channel` × channels (default 96 kb/s per channel, 576 kb/s for 5.1) and outputs 48 kHz.
- `--out-dir` writes all outputs to one directory instead of `restored/` next to each input.
- `--dry-run` lists what would be written without doing anything.

Output is always Matroska (`.mkv`), whatever the input container.

---

# 8. First test

Run on one file and read the full report:

```bash
"$HOME/ai/pal-restore/pal_restore.py" restore "/path/to/sample.mkv"
```

Expected report shape:

```text
  video fps: 25 -> 24000/1001
  video frames: N -> N
  video duration: D -> D×1.042708
  audio 0: end ... (expected ...), start ...
  audio 0: pitch shift -72.x cents (expected -72.4)
  subtitle 0: ...
  chapters: ...
  PASS
```

Confirm the video was not re-encoded (decoded frames must be identical):

```bash
cmp <(ffmpeg -v error -i "/path/to/sample.mkv" -map 0:v:0 -f framemd5 - | grep -v '^#' | cut -d, -f6) \
    <(ffmpeg -v error -i "/path/to/restored/sample.mkv" -map 0:v:0 -f framemd5 - | grep -v '^#' | cut -d, -f6) \
  && echo "frames identical"
```

Then confirm by ear (a human task): play the same scene from the original and the restored file and check that voices sound natural and music is at concert pitch. If the person reports the restored audio sounds too low, the source audio was already pitch-corrected, which contradicts the premise of this document; stop and report rather than adjusting anything.

---

# 9. Batch run

Preview, then run:

```bash
"$HOME/ai/pal-restore/pal_restore.py" restore --dry-run "/path/to/videos"
"$HOME/ai/pal-restore/pal_restore.py" restore "/path/to/videos" 2>&1 | tee "$HOME/ai/pal-restore/run-$(date +%Y%m%d-%H%M%S).log"
```

The batch continues past individual failures and ends with a summary line (`N restored, N skipped, N failed`). It exits non-zero if any file failed. Re-running the same command later only processes files whose outputs do not exist yet.

---

# 10. Order relative to the subtitle pipeline

Run this restoration **before** the WhisperX/llm-subs subtitle pipeline. Transcribe the restored file, not the 25 fps original: WhisperX then hears natural-pitch audio, and the resulting `<base>.<lang>.srt` is timed to the restored video.

Subtitles that already exist next to the original are retimed automatically into the `restored/` directory (section 5, step 6).

---

# 11. Failure handling

If a file fails:

1. Read the `FAILED: verification failed: …` line; it names the check.
2. Run `restore` again on that file alone to see the full report.
3. **Frame count changed** or **fps wrong**: inspect the input's video track with `mkvmerge -J`; record the codec and container.
4. **A/V drift**: compare `ffprobe -show_entries stream=start_time` for video and audio in the input; a broken or discontinuous input timeline (common in damaged `.ts` captures) should be remuxed to MKV first with `mkvmerge -o fixed.mkv input.ts` and retried. Record it.
5. **Pitch not measurable**: the excerpts were silent; this is reported but does not fail the file.
6. **Pitch wrong**: do not adjust thresholds; report the measured value.

Not handled, by design:

- Sidecar VobSub (`.idx`/`.sub`) and other bitmap sidecars are not retimed and are reported by absence. Embedded image subtitles (PGS, VobSub inside MKV) **are** retimed.
- Inputs that are not 25 fps are skipped.

---

# 12. Final report

### System
Arch version, kernel, `ffmpeg`, `mkvmerge` and `uv` versions, `doctor` output.

### Run
Number of files restored, skipped (with reasons) and failed (with the failing check), plus the log file path.

### Per file (for the first test file, and any failure)
Input codec/container, frame count, input → output duration, audio tracks (codec, rate, channels → FLAC/Opus), measured pitch shift, subtitle and chapter counts, sidecars retimed.

### Test

```text
doctor:                                PASS/FAIL
First file restore + verification:     PASS/FAIL
Frames bit-identical (no re-encode):   PASS/FAIL
Batch:                                 N restored / N skipped / N failed
```

List every deviation from this document, what was changed, and why.
