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
| Sidecar `.srt` / `.ass` / `.ssa` / `.vtt` next to the input | Timestamps scaled by 25025/24000 (SRT/VTT: only cue timing lines; ASS/SSA: only event Start/End fields, located by the `[Events]` Format line) | No (text edit; encoding, byte-order mark and line endings preserved) |

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
- A restored file and its retimed sidecars are moved into place together, only after all of them pass verification. A failed file leaves nothing behind (an interrupted or killed run included).
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

`doctor` must print `OK`. It checks `ffmpeg`, `ffprobe`, `mkvmerge`; that FFmpeg's `aresample` supports the `soxr` resampler; that the `h264_metadata`, `mpeg2_metadata`, `filter_units` and `trace_headers` bitstream filters exist (the first two rewrite the bitstream frame rate, the last two let verification read it back); that the `flac` encoder exists (and reports whether `libopus` does, which only `--audio-codec opus` needs); and that `mkvmerge` supports `--sync`, `--chapter-sync`, `--track-order` and `--display-dimensions`. Resolve every reported problem before continuing.

---

# 5. How the restoration works

For each input:

1. **Probe.** Files whose video is not 25 fps are skipped. Probing reads up to 100 s / 100 MB, so streams that start late (a second DVD audio track) are found and described.
   **Working file.** A Matroska input that starts at 0 (within 0.2 s) is used as it is. Any other input, including Matroska whose tracks all start later, (MP4, MPEG-TS, MPEG-PS/VOB, AVI, …) is first copied by FFmpeg, every stream unchanged, into a Matroska working file in the temporary directory, and every later step reads only that file. FFmpeg shifts all streams by the same amount so the file starts at 0 (capture clocks start anywhere, often at tens of thousands of seconds), gives packets without a presentation time one (AVI with B-frames), and carries DVB and DVD subpicture subtitles, which mkvmerge cannot read from MPEG-TS/PS. MP4 text subtitles become SRT text (Matroska cannot hold them as they are; mkvmerge stores them the same way), Blu-ray and DVD LPCM audio (`pcm_bluray`, `pcm_dvd`) becomes plain PCM of the same bit depth, and MP4 cover art becomes a Matroska `cover.png`/`cover.jpg` attachment instead of a video track. Without this step, tracks mkvmerge read from a transport stream (which it rebases) and tracks FFmpeg copied (which keep the capture clock) could land hours apart.
2. **Audio.** Each audio track is decoded and passed through:
   ```text
   aresample=50050:resampler=soxr:precision=28,asetrate=48000,aresample=<original rate>:resampler=soxr:precision=28
   ```
   Resampling to 50050 Hz and relabelling as 48000 Hz slows the audio by exactly 50050/48000 = 25025/24000 and lowers the pitch by the same ratio. The two integer rates are required because `asetrate` only accepts integers; the ratio is exact for every source sample rate. The relabelled rate is the smallest multiple of 24000 not below the source rate (48000 for sources up to 48 kHz; 96000, i.e. `aresample=100100 … asetrate=96000`, for 96 kHz), so the first resample never narrows the band. The final resample returns to the track's original sample rate.
   The existing offset between audio and video is preserved and scaled. Audio that started before the first video frame is trimmed sample-accurately in FFmpeg, because a negative `mkvmerge --sync` can only drop whole codec blocks.
   Language (with region, e.g. `pt-BR`), track name, and the default, forced, commentary, hearing-impaired, visual-impaired, original and text-descriptions flags are carried over. With `--audio-codec opus`, a 5.1(side) or 5.0(side) layout is relabelled 5.1 / 5.0 (same speakers, the label libopus accepts; no remixing); any other layout outside the Vorbis set (2.1, 3.1, 4.0, 6.0, 7.1(wide), …) is encoded as discrete channels (Opus mapping family 255), every channel kept, none remixed, without speaker labels.
3. **Video.** Two separate operations:
   - *Bitstream frame rate.* H.264 and MPEG-2 tracks are copied (no decoding) through FFmpeg's `h264_metadata=tick_rate=48000/1001` or `mpeg2_metadata=frame_rate=24000/1001` bitstream filter into an intermediate Matroska file that keeps the source timestamps (`-copyts`). This rewrites every SPS / sequence header, including those repeated in-band in transport streams. Other codecs are taken from the source as they are.
   - *Timestamps.* `mkvmerge --sync <tid>:0,25025/24000` multiplies every video timestamp by the ratio, exactly as for subtitles; mkvmerge then also records the 1001/24000 s default frame duration.
   `mkvmerge --default-duration` is deliberately not used to retime: it rewrites the timestamps of Matroska/MP4 input but not of MPEG-TS input (which would leave TS video at 25 fps under 23.976 fps audio), and combined with `--sync` it stretches Matroska input twice. See `docs/decisions.md`.
4. **Subtitles and chapters.** `mkvmerge --sync <tid>:0,25025/24000` scales every embedded subtitle track; `--chapter-sync 0,25025/24000` scales the chapters (passed only when the input has chapters). The video and audio intermediates are written without chapters and added with `--no-chapters`, so the only chapters in the output are the scaled ones. Attachments (fonts) and global tags are copied.
   Steps 3 and 4 are one mkvmerge pass. The audio from step 2 is added in a second pass, each track placed relative to where the first video frame actually landed in the first pass.
5. **Verify** (section 6).
6. **Sidecars.** Files next to the input named `<base>.srt`, `<base>.<anything>.srt` (and `.ass`, `.ssa`, `.vtt`, any letter case) are retimed into the temporary directory and each is verified: FFmpeg decodes the original and the retimed file, and every cue must be the original's × 25025/24000 within 10 ms. A sidecar that also matches a sibling video with a longer base name (`film.part2.srt` next to `film.part2.mkv`) belongs to that video only.
   Text is decoded by its byte-order mark (UTF-8, UTF-16) and written back the same way; without one, every byte passes through unchanged (UTF-8 and legacy 8-bit encodings alike), and lines end only at CR LF, LF or CR. A file that is not UTF-8 is verified by telling FFmpeg it is Latin-1 (only times are compared). ASS/SSA files are also verified by reading every `Dialogue`/`Comment` line's Start and End by the Format line (FFmpeg reads them by position and decodes nothing from a reordered Format line); without a Format line, the standard v4/v4+ field order applies. Only timing is changed: in SRT/VTT the two timestamps of a cue timing line (`,` or `.` before the fraction, 1–3 fraction digits read as FFmpeg reads them, any number of hour digits); cue text, `NOTE` blocks and the WebVTT header are untouched. In ASS/SSA, the Start and End fields of `Dialogue`/`Comment` lines, located by the `[Events]` Format line.
7. **Publish.** Only when the MKV and every sidecar pass are they moved into place: sidecars first, the MKV last, so an interruption leaves no MKV and a re-run redoes the file. If the MKV already exists, a re-run still retimes any sidecar whose output is missing (one added later, or one that failed before).

---

# 6. Verification performed on every file

The script refuses to publish an output unless all of these hold:

| Check | Requirement |
|---|---|
| Frame rate | Every output video track reports 24000/1001 |
| Bitstream frame rate | For H.264 and MPEG-2, every SPS / sequence header in the output (codec private data and in-band) that declares a frame rate declares 24000/1001; if the input declared one, the output still does |
| Frame count | Identical to the input, per video track |
| Display aspect ratio | Identical to the input, per video track |
| Video duration | Measured from the timestamps (first to last frame plus one frame period): input × 25025/24000, within 50 ms |
| A/V sync | Each audio track's end, measured from the video start, equals the input's × 25025/24000 within 100 ms |
| Audio start | Input start offset × 25025/24000 (or 0 if it was trimmed), within 50 ms |
| Pitch | Measured shift between input and output audio is −72.4 cents ± 8 cents (log-spectrum cross-correlation over three excerpts of at least 2.1 s within the track's own span; a track shorter than 2.1 s is reported as too short to measure) |
| Subtitles | Text tracks: decoded cues compared one by one — same cue count, every cue's start and end, relative to the first video frame, scaled within 10 ms (empty "clear" samples, which mkvmerge drops from MP4 text tracks, are not cues). Image tracks (PGS, VobSub, DVB): same packet count; first and last scaled within 10 ms |
| Chapters | Same number of editions (all of them, read with `mkvextract`; ffprobe shows one) with the same default flags; per edition the same chapter count; every chapter's start and end, relative to the first video frame, scaled within 10 ms |
| Video track offsets | Every further video track's start, relative to the first video track's, is the input's × 25025/24000 within 50 ms |
| Timeline start | The output starts at most the input's lead before its first video frame (× 25025/24000) plus 0.2 s (B-frame reorder delay); it never carries a capture's clock |
| Sidecars | Every cue FFmpeg decodes from the original appears in the retimed file × 25025/24000 within 10 ms; same cue count |

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

- `PATH` may be files or directories. Directories are searched recursively for `.mkv .mp4 .m4v .mov .avi .mpg .mpeg .vob .ts .m2ts .mts .webm`; existing `restored/` directories, the `--out-dir` directory and the script's own temporary directories (`pal-restore-*`) are not descended into. A path that does not exist counts as a failed file. A symbolic link is restored under its own name, next to itself, with its own sidecars.
- Two inputs that would write the same output (`x.mkv` and `x.mp4` in one directory, or the same base name in two directories with one `--out-dir`) both fail with a message naming the other; restore them separately with different `--out-dir`.
- `--target-fps` defaults to `24000/1001`. Use `24` only for material known to come from a true 24.000 fps master. It must be a number or fraction between 0 and 25.
- `--audio-codec flac` (default) is lossless relative to the decoded source audio. `opus` uses `--opus-kbps-per-channel` × channels (default 96 kb/s per channel, 576 kb/s for 5.1) and outputs 48 kHz.
- `--out-dir` writes all outputs to one directory instead of `restored/` next to each input. It may not be an input's own directory (outputs and sidecars would land on the originals); such a file fails with a message.
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

The batch continues past individual failures and ends with a summary line (`N restored, N skipped, N failed`). It exits non-zero if any file failed. Re-running the same command later only processes files whose outputs (MKV or sidecars) do not exist yet. Stopping it with Ctrl-C, `kill` or by closing the terminal (SIGINT, SIGTERM, SIGHUP) leaves nothing of the file in progress behind, including output directories the run created.

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
5. **Frame count changed** on a `VTS_01_2.VOB`-style file: it is a later part of a VOB set; see *Not handled* below.
6. **Pitch not measurable**: the excerpts were silent, or the track is shorter than 2.1 s; this is reported but does not fail the file.
7. **Pitch wrong**: do not adjust thresholds; report the measured value.

Not handled, by design:

- Sidecar VobSub (`.idx`/`.sub`) and other bitmap sidecars are not retimed and are reported by absence. Embedded image subtitles (PGS, VobSub inside MKV) **are** retimed.
- Inputs that are not 25 fps are skipped.
- **DVD VOB sets** (`VTS_01_1.VOB`, `VTS_01_2.VOB`, …) are split at arbitrary sector boundaries, so a later part starts mid-GOP and fails verification (frame count). Concatenate the set first (`cat VTS_01_1.VOB VTS_01_2.VOB … > VTS_01.VOB`) and restore that.
- **HEVC, VC-1, MPEG-4 Part 2** video: timestamps and container frame rate are restored; the frame rate written inside the bitstream is left as it is (section 5 step 3 rewrites H.264 and MPEG-2 only).

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
