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
  file; `video_track_options` carries `display_dimensions`, language, name, default and forced
  flags across.
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

- **HEVC, VC-1, MPEG-4 Part 2** video: timestamps and container frame rate are restored, but
  bitstream timing fields (HEVC VPS/VUI) are left as they are. pipeline.md scopes the bitstream
  rewrite to MPEG-2 and H.264; `hevc_metadata` could extend it if such sources appear.
- **Opus encoder delay** shows as a ~7 ms earlier first packet timestamp (Opus pre-skip, 312
  samples). Decoded audio is placed correctly; it is well inside the 50 ms start tolerance.
- **Language codes**: mkvmerge writes the legacy Matroska form (`ger` for `deu`); same language.
