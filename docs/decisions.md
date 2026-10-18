# Decisions

Settled implementation decisions for `pal_restore.py`, each with the evidence that settled it.
Deviations from the draft script and from `docs/pipeline.md` as first written are recorded here
(pipeline.md section 1: "Record every deviation from this document and the reason"); the
pipeline document itself has been corrected in place to describe what the code does.

Evidence was produced with the synthetic fixtures from `tests/make_fixtures.sh` on
FFmpeg 9.0.1 and mkvmerge 101.0 (2026-10-09).

## 1. Video timestamps are stretched with `mkvmerge --sync`, not relabelled with `--default-duration`

The draft retimed video with `mkvmerge --default-duration <tid>:24000/1001p`. Measured on the
fixtures (first-to-last frame plus one period; the right answer is 125.125 s):

| Input | `--default-duration` only | `--sync 0,25025/24000` only | both |
|---|---|---|---|
| Matroska (H.264) | 125.125 s | 125.125 s | 130.468 s (stretched twice) |
| MPEG-TS (H.264) | **120.002 s** (unchanged) | 125.126 s | 125.126 s |

`--default-duration` rewrites timestamps for Matroska/MP4 input and ignores them for MPEG-TS
input, so on a `.ts` capture the draft produced 25 fps video under 23.976 fps audio — a 5 s
drift over two minutes. The draft's own duration check (`frames / 25` vs `frames / 23.976`)
is computed from the frame count and passed it anyway. The `--sync` stretch is the same exact
linear map the pipeline already used for subtitles, behaves identically for every container, and
makes mkvmerge record the 1001/24000 s default duration (41 708 333 ns) by itself.

## 2. Bitstream frame rate is rewritten by FFmpeg bitstream filters, for H.264 as well as MPEG-2

Without `--default-duration`, mkvmerge's `--fix-bitstream-timing-information` wrote 1/100
(50 fps) into the Matroska H.264 SPS and left all 13 in-band SPS of the TS input at 1/50
(25 fps). So the SPS rewrite moved to FFmpeg's `h264_metadata=tick_rate=48000/1001` (H.264
timing counts fields: rate = time_scale / (2 × num_units_in_tick)), mirroring the draft's
`mpeg2_metadata` for MPEG-2. Both write into an intermediate Matroska file with `-copyts`,
which keeps every source timestamp (checked: first frame, count and 40 ms spacing identical for
TS, MPEG-PS and MP4 sources). The draft wrote MPEG-2 to a raw elementary stream, which discards
the source timestamps; the intermediate keeps them, so any gap in the source survives the
stretch instead of being flattened.

## 3. Audio is placed relative to where the video actually landed

The draft computed each audio delay from the source video's `start_time`, assuming the output's
first video frame sits at 0. With the stretch that is not true in general (a video starting at
0.5 s lands at 0.521 s). The mux is now two passes: pass 1 writes video, subtitles, chapters,
attachments and tags; the first video timestamp of that file is measured; pass 2 adds each
audio track at that position plus its original lead over the video × 25025/24000. Audio before
the first video frame is still trimmed sample-accurately, as pipeline.md section 5 step 2 says.

## 4. Verification checks properties, not proxies

- **Video duration** is measured from timestamps. The draft's version derived both durations
  from the frame count, so it could not fail when counts matched (it passed the TS defect in 1).
- **Bitstream frame rate** is read back: `filter_units` keeps only SPS (H.264 NAL type 7) or
  sequence headers/extensions (MPEG-2 0xB3/0xB5), and `trace_headers` prints their fields; every
  header declaring a rate must declare the target. Reading the whole stream this way takes about
  a second per two minutes of video. Covers in-band headers, which only checking the codec
  private data would miss.
- **All video tracks** are checked, not only the first (multi-angle MPEG-2 fixture).
- **Display aspect ratio** is compared, because rewritten tracks pass through an intermediate
  file; `track_options` carries `display_dimensions`, language, name, default, forced and
  accessibility flags across.
- **Text subtitles** are compared by decoded cues (FFmpeg → SRT): MP4 text tracks hold empty
  "clear" samples between cues (6 packets for 3 cues in the fixture) which mkvmerge drops, so the
  draft's packet count failed a correct output. Every cue is now checked, not just first/last.
  Image subtitles keep the packet comparison. Subtitle times are compared relative to the first
  video frame, which is what they sync to.

## 5. Smaller fixes

- `--chapter-sync` is passed only when the input has chapters (it otherwise produced
  "track ID -2 was requested but not found" warnings; −2 is mkvmerge's chapter pseudo-track).
- Every MPEG-2 video track is rewritten, not only a sole one: the draft silently skipped the
  rewrite when there was more than one video track, leaving 25 fps in the bitstream.
- A failed file leaves nothing behind, including the `restored/` directory if this run created
  it and it is still empty (pipeline.md section 1). `tests/failure_path.py` checks both cases.
- `--audio-codec opus`: libopus rejected 5.1(side) audio ("Invalid channel layout"). It accepts
  only the Vorbis layouts, whose surrounds are labelled back; 5.1(side)/5.0(side) are relabelled
  with `channelmap` (same speakers, no remix).
- mkvmerge's stdout is captured: warnings are printed per line, and a hard failure's message is
  included in the `FAILED:` line instead of being lost.
- `doctor` checks what the pipeline now uses (`h264_metadata`, `mpeg2_metadata`,
  `filter_units`, `trace_headers`, the `flac` encoder, `libopus` as optional, and mkvmerge
  `--sync`, `--chapter-sync`, `--track-order`, `--display-dimensions`).
- Unused variables `video_src` and `video_tid_map` removed.

## Known limits (not defects; recorded so they are not rediscovered)

- **VC-1, MPEG-4 Part 2** video: timestamps and container frame rate are restored, but any
  frame rate inside the bitstream is left as it is. (HEVC was in this list until section 8.)
- **Opus for layouts outside the Vorbis set** is discrete-channel (mapping family 255): every
  channel and its order are kept, but the file carries no speaker labels, so a player may not
  place them. FLAC (the default) keeps every layout with its labels.
- **Later parts of a DVD VOB set** start mid-GOP and fail verification loudly; pipeline.md
  section 11 says to concatenate the set first.
- **Opus encoder delay** shows as a ~7 ms earlier first packet timestamp (Opus pre-skip, 312
  samples). Decoded audio is placed correctly; it is well inside the 50 ms start tolerance.
- **Language codes**: mkvmerge writes the legacy Matroska form (`ger` for `deu`) next to the
  IETF tag; same language.
- **A sidecar whose cue text contains a complete timing line** (`12:30:00,000 --> 12:31:00,000`
  inside a cue) is read by FFmpeg as an extra cue but is not retimed (it is text). Sidecar
  verification sees the disagreement and the file fails, publishing nothing. The file is
  ambiguous; refusing it is the right outcome.
- **WebVTT `X-TIMESTAMP-MAP`** is left as it is: it maps cue time to an HLS stream's MPEG-TS
  clock, which has no meaning for a sidecar next to a Matroska file.

## 6. Red-team round 1 (2026-10-09): one time base, sidecars as part of the published unit

An independent red-team agent ran 40 adversarial inputs against commit `dc3ffa2`. Every finding
below was confirmed by running it, except where marked. Each is now a permanent test:
`tests/make_adversarial.sh` + `tests/check_adversarial.sh` (media and sidecars), `tests/check_cli.sh`
(command line), `tests/failure_path.py` (publishing). Run against `dc3ffa2`'s outputs,
`check_adversarial.sh` reports 23 failures; against the current code, none.

1. **Tracks on different time bases.** mkvmerge rebases MPEG-TS/PS tracks it reads itself;
   the bitstream-rewritten intermediates (FFmpeg, `-copyts`) kept the capture clock. A TS with
   H.264 and HEVC video starting at 50 000 s gave video tracks 52 136.9 s apart and passed
   verification (only video 0 was checked); DVB subtitles came out 1.53 s early; a TS starting
   at 50 000 s gave an output timeline starting at 52 136 s.
   **Decision:** one time base. A non-Matroska input is first copied by FFmpeg, streams
   unchanged, into a Matroska working file; every later step reads only that file
   (`working_file`). This also fixes 5 and 10 below. Matroska input is used as is (both tools
   keep its timestamps). Verification now also checks each further video track's start against
   video 0, and the timeline start (pipeline.md section 6). The timeline check allows 0.2 s,
   because Matroska cannot store negative timestamps and FFmpeg shifts a B-frame source later as
   a whole by its reorder delay (80 ms measured on `cover.mp4`; x264's deepest is 120 ms).
2. **Sidecars.** Not retimed: `.` before the milliseconds, 1–2 fraction digits, UTF-16 files,
   ASS with a reordered Format line. Wrongly scaled: SRT/VTT hours ≥ 100, ASS hours ≥ 10.
   Wrongly changed: timestamp-like text in cues, `NOTE` blocks, `X-TIMESTAMP-MAP`. Nothing
   verified sidecars at all.
   **Decision:** the retimer works on decoded text lines, not a byte regex over the file:
   only cue timing lines (SRT/VTT) and the Start/End fields named by the `[Events]` Format line
   (ASS/SSA) change; encoding is taken from the byte-order mark. Fractions of 1–3 digits are
   read as a millisecond count, which is what FFmpeg does (confirmed: FFmpeg-decoded original
   and retimed cues agree within 10 ms on the fixture). Every sidecar is verified by decoding
   both files with FFmpeg (`verify_sidecar`).
3. **96 kHz audio band-limited to 24 kHz.** The intermediate rate was fixed at 50 050 Hz.
   Measured energy above 30 kHz: −35.8 dB in, −62.7 dB out. **Decision:** the relabelled rate
   is the smallest multiple of 24 000 not below the source rate; now −35.8 → −37.1 dB.
4. **Embedded text subtitles failed correct files** when the video was not the earliest
   stream (audio leading by 0.5 s; MP4 edit list offsetting video by 2 s): FFmpeg's SRT output
   was rebased to the earliest stream while packet times were absolute. **Decision:** cues are
   decoded with `-copyts`, on the same absolute timestamps as everything else.
5. **DVD subpictures in VOB/MPEG-PS always failed**: mkvmerge cannot read them from MPEG-PS.
   **Decision:** fixed by 1 (FFmpeg copies them into the working file; mkvmerge reads them
   from Matroska as VobSub).
6. **Sidecars were outside the published unit**: a sidecar failure left the MKV published, and
   a re-run never retimed sidecars that were missing or added later. **Decision:** sidecars are
   retimed and verified in the temporary directory and published with the MKV, sidecars first
   and MKV last; a re-run over an existing MKV retimes only the missing sidecars.
7. **SIGTERM/SIGKILL left `pal-restore-*` directories**, and a later run with `--out-dir`
   inside the input tree collected the stale intermediates as inputs. **Decision:** SIGTERM
   raises `SystemExit`, taking Ctrl-C's cleanup path (exit 143, nothing left, child FFmpeg
   killed by `subprocess.run`); `collect` never descends into `pal-restore-*`, `restored/` or
   the `--out-dir` directory. SIGKILL cannot be caught; its leftover directory is now ignored.
8. **Symlinked inputs** were resolved to the target's name and directory, losing the link's
   sidecars. **Decision:** `.absolute()` instead of `.resolve()`.
9. **Pitch excerpts were placed from 0**, not within the track, so late-starting tracks were
   "not measurable". **Decision:** excerpts at 25/50/75 % of the track's own span, mapped to
   the output through the same linear map as everything else, read with `-copyts -ss`.
10. **AVI with B-frames**: packets carry no presentation time, the video start was taken as
    0.08 s and 80 ms of audio was trimmed. **Decision:** fixed by 1 (`-fflags +genpts` in the
    working file) and `packet_span` also generates missing times. The video start is now 0.04 s,
    which is where FFmpeg's decoder presents the first frame, so the 40 ms of audio before it is
    trimmed as pipeline.md section 5 step 2 requires.
11. **Command line**: a missing input exited 0; two inputs writing the same output (`x.mkv` and
    `x.mp4`, or one base name in two directories under one `--out-dir`) left the second
    reported as "skipped: already exists", silently never restored; `verify` on a missing file and
    a bad `--target-fps` (`abc`, `0`) gave tracebacks. **Decision:** a missing path counts as a
    failed file; colliding inputs both fail with a message naming the other; `--target-fps`
    must lie strictly between 0 and 25; `verify` reports a missing file and exits 2.
12. **Smaller items.** A second DVD audio track starting 20 s in was probed with sample rate 0
    (`aresample=0`): probing now reads 100 s / 100 MB, and an unknown sample rate is an explicit
    error. `pt-BR` was written as `pt`: audio properties now come from mkvmerge's
    `language_ietf` through the same `track_options` as video, which also carries the
    commentary and other accessibility flags. `film.part2.srt` was claimed by `film.mkv`
    (found by inspection): a sidecar belongs to the sibling video with the longest matching
    base name.

Found while fixing these, also corrected:

- `first_decoded_pts` read one packet; when that packet is all AAC encoder priming the decoder
  outputs nothing, and the old code silently returned 0.0. It now reads eight packets and raises
  if none decodes.
- MP4 text subtitles cannot be copied into Matroska as they are; the working file converts them
  to SRT text (timing unchanged; mkvmerge stores them the same way when it reads MP4 itself).
- MP4 cover art would be copied as a real video track; it becomes a Matroska `cover.png`/`cover.jpg`
  attachment.
- `tests/failure_path.py` passed whatever the code did once `restore_one` took the destination
  file instead of the directory; it was rewritten against the new signature and extended to
  the sidecar failure and the re-run cases.

## 7. Red-team round 2 (2026-10-09, the last round allowed): chapters, sidecar encodings, edges

A second independent red-team agent ran about 60 inputs against commit `74178ed` (about 40
minutes). All findings were confirmed by running, except LPCM from DVD (`pcm_dvd`) and DVB
teletext, which it judged by inspection. Each fixed finding is now a permanent test in the
same scripts as section 6; run against `74178ed`'s outputs, `check_adversarial.sh` fails every
new check.

1. **Extra chapter editions with the old 25 fps times, flagged default** (silent: verification
   read only ffprobe's one edition). `-map_metadata -1` does not drop chapters, so FFmpeg copied
   the source chapters into every video and audio intermediate and mkvmerge merged them as
   further editions; players pick a default edition. **Decision:** `-map_chapters -1` on both
   intermediates and `--no-chapters` before them in mkvmerge; verification now compares the
   property itself: every edition (read with `mkvextract`), its default flag, its chapter count,
   every start and end (`chapter_editions`).
2. **An ASS line left unretimed** (silent). `str.splitlines()` also splits at U+0085, and a
   UTF-8 file read as Latin-1 has byte 0x85 in many Cyrillic and Polish letters, so a Name such
   as `Михаил` before Start cut the line in two. FFmpeg decodes nothing from a reordered Format
   line, so the sidecar check compared 0 cues with 0. **Decision:** lines end only at CR LF, LF
   or CR; ASS/SSA are also verified by an independent read of every event's Start and End by the
   Format line (`ass_event_times`), so a line left unretimed or unreadable fails.
3. **A legacy 8-bit sidecar failed the whole file**: FFmpeg refuses non-UTF-8 text.
   **Decision:** a sidecar without a byte-order mark that is not valid UTF-8 is decoded by FFmpeg
   as Latin-1 for verification (`_sidecar_input`); only times are compared.
4. **`--out-dir` equal to the input's directory**: each sidecar's output was its own source and
   was skipped as "already exists", leaving the original timing next to the restored MKV.
   **Decision:** refused with a message (`restore_one`).
5. **Blu-ray LPCM (`pcm_bluray`) failed**: Matroska cannot store it. **Decision:** the working
   file converts `pcm_bluray`/`pcm_dvd` to `pcm_s16le`/`pcm_s24le` of the same depth (lossless).
   DVB teletext subtitles (by inspection only; FFmpeg cannot make a test file) would still fail
   the file loudly.
6. **`--audio-codec opus` rejected 2.1, 3.0(back), 3.1, 4.0, quad(side), 6.0, hexagonal,
   7.1(wide)**. **Decision:** mapping family 255 (discrete channels) for any layout outside the
   Vorbis set; see Known limits.
7. **SSA/ASS without a Format line**: End was not retimed (the three-name default list left End
   joined to the rest of the line). **Decision:** the full ten-field v4/v4+ default.
8. **Matroska starting at 10 s was rejected** by the timeline check. **Decision:** such input
   goes through the working file like every other container, so every output timeline starts at
   0 (one rule); `TIMELINE_SLACK` (0.2 s) names the reorder allowance used by both places.
9. **SIGHUP left the temporary directory.** **Decision:** SIGHUP takes the SIGTERM path (exit 129).
10. **A failure under a new nested `--out-dir` left its empty parents.** **Decision:** every
    directory level the run created is removed, leaf first, while empty.
11. **HEVC bitstream frame rate unchanged**: outside the design's scope; recorded in Known limits
    and put to the architect.
12. **Pitch not measured on audio shorter than about 10 s** (excerpts of 20 % of the span were
    under the 2.05 s the analysis needs), reported as "silent". **Decision:** excerpts of at least
    2.1 s placed inside the span; a track shorter than that is reported as too short.
13. **A later part of a DVD VOB set fails** (starts mid-GOP). **Decision:** documented in
    pipeline.md section 11 (concatenate the set first).

Found while fixing these, also corrected: pitch excerpts on a transport stream whose clock starts
far from 0 (the Blu-ray fixture, 600 s) selected no samples, because input `-ss` with `-copyts`
selects nothing there; the pitch check silently did not run on such files. Excerpts are now
sought at their time minus the file's start time, without `-copyts`.

## 8. HEVC bitstream frame rate (2026-10-18, the architect's decision)

Round-2 finding 11 (section 7): an HEVC output's VPS and SPS VUI still declared 25 fps. Asked
whether to convert HEVC to MPEG-2, retime, and convert back, the answer given and accepted was
no: two lossy encodes break the contract that video is never re-encoded (a test round trip
changed the decoded frames and lost 10-bit, since MPEG-2 output was 8-bit), and the rewrite is
available in place. The architect approved extending the rewrite to HEVC.

**Decision:** `hevc_metadata=tick_rate=24000/1001` (HEVC timing counts frames, unlike H.264's
fields) in `BITSTREAM_FILTERS`; `bitstream_rates` reads every VPS and SPS VUI that carries
timing, so verification fails an HEVC output still declaring 25 fps (confirmed by running: an
output retimed by timestamps only reports "bitstream headers declare 25 fps"); `doctor` checks
the filter. Tested on a 10-bit HDR10 HEVC file: decoded frames bit-identical, colour
description, mastering-display and content-light-level metadata kept. Permanent tests:
`hdr.mkv` in the adversarial set, and the HEVC second track of `mixed.ts`.

---

# 9. Test coverage added (2026-10-18)

The architect asked whether test coverage could be added and approved four additions.

- **Unit tests** (`tests/test_units.py`, 32 tests, under a second): the subtitle retiming
  (`retime_text`), millisecond scaling and clock formatting (`_scale_ms`, `_fmt_hms`), line splitting
  and encoding detection (`_split_lines`, `_read_text`, `retime_sidecar`), the ASS event reader
  (`ass_event_times`), the resampling filter string (`audio_filter`, for eleven source rates) and the
  bitstream frame-rate parsing. Expected values are computed in the tests from exact fractions.
  To make the parsing testable apart from FFmpeg, `bitstream_rates` now hands the `trace_headers` log
  to a new pure function, `rates_from_trace`; behaviour is unchanged.
- **A defect the unit tests found: ASS times were rounded twice.** A cue end of 2.000 s scales to
  2.0854 s, which is 208.54 centiseconds and must print as `0:00:02.09`. The script rounded to 2085 ms
  first, then rounded 208.5 centiseconds to even, printing `0:00:02.08`: an error of 5.4 ms where
  rounding once gives at most 5 ms. It was invisible to every verification (the tolerance is 10 ms) and
  to the suites. Fix: `_scale_ms` takes the stored precision (`unit`, 10 ms for ASS) and rounds once
  from the exact value; `_fmt_hms` no longer rounds centiseconds, so its carry branch for 995 to 999 ms,
  which can no longer be reached, is removed. The existing sidecar checks still pass unchanged.
- **One command** (`tests/run_all.sh`) builds the fixtures, restores them and runs every check, and a
  GitHub Actions workflow (`.github/workflows/tests.yml`) runs it on every push. The workflow has not
  been run on GitHub when this was written; it was written to the Ubuntu 24.04 package names.
- **Two fixtures**: `eac3.mkv` (E-AC-3 5.1, the audio codec of a real WEB-DL episode set, which no
  fixture used) and `hevc8.mkv` (8-bit HEVC; the earlier HEVC cases were 10-bit HDR10 and the HEVC
  track of `mixed.ts`). The adversarial set is now 22 files.
- The new checks can fail: the unit tests run against the previous `pal_restore.py` give 4 failures
  (the ASS rounding) and 8 errors (functions that did not exist); the `hevc8.mkv` input itself reads
  `vui_time_scale=25`, so its timing check cannot pass on unrestored video.
