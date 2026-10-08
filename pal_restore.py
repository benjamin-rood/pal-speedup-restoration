#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26"]
# ///
"""Undo PAL speedup: restore 23.976 fps material that was played back at 25 fps.

Every original frame is still present in a PAL-speedup release; only the timing and the
audio were changed. This tool:

  * stretches the video timestamps by 25025/24000 and rewrites the frame rate in H.264 SPS and
    MPEG-2 sequence headers to 24000/1001, without re-encoding (frames are untouched),
  * resamples every audio track by exactly 24000/25025 (restores both speed and pitch),
  * scales embedded subtitle and chapter times by 25025/24000,
  * scales sidecar subtitle files (.srt/.ass/.ssa/.vtt) next to the input,
  * verifies frame rate (container and bitstream), frame count, durations, A/V offset,
    subtitle/chapter timing and pitch before publishing anything.

See docs/pipeline.md for the procedure and docs/decisions.md for why it works as it does.

Subcommands:
  doctor                          check required tools
  restore [options] PATH...       restore files (directories are searched recursively)
  verify ORIGINAL RESTORED        re-run verification on an existing pair
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import numpy as np

SOURCE_FPS = Fraction(25)
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".mpg", ".mpeg", ".vob", ".ts", ".m2ts", ".mts", ".webm"}
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
ENV = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8")


# ---------------------------------------------------------------- helpers

def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, env=ENV)


def capture(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, env=ENV, capture_output=True, text=True).stdout


def ffprobe(path: Path) -> dict:
    return json.loads(capture(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                               "-show_chapters", "-of", "json", str(path)]))


def mkv_identify(path: Path) -> dict:
    return json.loads(capture(["mkvmerge", "-J", str(path)]))


def video_streams(probe: dict) -> list[dict]:
    return [s for s in probe["streams"] if s["codec_type"] == "video"
            and not s.get("disposition", {}).get("attached_pic")]


def audio_streams(probe: dict) -> list[dict]:
    return [s for s in probe["streams"] if s["codec_type"] == "audio"]


def stream_fps(stream: dict) -> Fraction | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        v = stream.get(key, "0/0")
        if v and not v.startswith("0/") and v != "0/0":
            return Fraction(v)
    return None


def packet_span(path: Path, selector: str) -> tuple[float, float, float, int]:
    """(first pts, last pts, end of last packet, packet count) for one stream, from demuxed packets."""
    out = capture(["ffprobe", "-v", "error", "-select_streams", selector,
                   "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)])
    pts, ends = [], []
    for line in out.splitlines():
        parts = line.strip().strip(",").split(",")
        if not parts or parts[0] in ("", "N/A"):
            continue
        p = float(parts[0])
        d = float(parts[1]) if len(parts) > 1 and parts[1] not in ("", "N/A") else 0.0
        pts.append(p)
        ends.append(p + d)
    if not pts:
        return (math.nan, math.nan, math.nan, 0)
    return (min(pts), max(pts), max(ends), len(pts))


def first_decoded_pts(path: Path, selector: str) -> float:
    """Presentation time of the first decoded frame (after decoder priming/skip handling)."""
    out = capture(["ffprobe", "-v", "error", "-select_streams", selector, "-read_intervals", "%+#1",
                   "-show_entries", "frame=pts_time,best_effort_timestamp_time", "-of", "json", str(path)])
    frames = json.loads(out).get("frames", [])
    for f in frames:
        for key in ("pts_time", "best_effort_timestamp_time"):
            if f.get(key) not in (None, "N/A"):
                return float(f[key])
    return 0.0


MPEG2_FRAME_RATES = {1: Fraction(24000, 1001), 2: Fraction(24), 3: Fraction(25), 4: Fraction(30000, 1001),
                     5: Fraction(30), 6: Fraction(50), 7: Fraction(60000, 1001), 8: Fraction(60)}
_TRACE_LINE = re.compile(r"^\[trace_headers[^]]*\]\s+\d+\s+(\w+)\s.*=\s*(-?\d+)\s*$")


def bitstream_rates(path: Path, vindex: int, codec: str) -> list[Fraction] | None:
    """Frame rate declared by every sequence-level header of one video track, in stream order.

    H.264: every SPS (in the codec private data and in-band) whose VUI carries timing info;
    rate = time_scale / (2 * num_units_in_tick). MPEG-2: every sequence header, with its
    sequence extension. Returns None for codecs whose headers are not checked.
    """
    if codec == "h264":
        units, start = "7", "seq_parameter_set_id"
    elif codec == "mpeg2video":
        units, start = "0xb3|0xb5", "frame_rate_code"
    else:
        return None
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "debug", "-i", str(path),
                          "-map", f"0:v:{vindex}", "-c", "copy",
                          "-bsf:v", f"filter_units=pass_types={units},trace_headers", "-f", "null", "-"],
                         env=ENV, capture_output=True, text=True, check=True).stderr
    headers: list[dict[str, int]] = []
    for line in out.splitlines():
        m = _TRACE_LINE.match(line)
        if not m:
            continue
        name, value = m.group(1), int(m.group(2))
        if name == start:
            headers.append({})
        if headers:
            headers[-1][name] = value
    rates = []
    for h in headers:
        if codec == "h264":
            if h.get("timing_info_present_flag") and h.get("num_units_in_tick"):
                rates.append(Fraction(h["time_scale"], 2 * h["num_units_in_tick"]))
        else:
            rate = MPEG2_FRAME_RATES.get(h["frame_rate_code"])
            if rate is not None:
                rates.append(rate * (h.get("frame_rate_extension_n", 0) + 1)
                             / (h.get("frame_rate_extension_d", 0) + 1))
    return rates


def display_aspect(stream: dict) -> Fraction:
    sar = stream.get("sample_aspect_ratio", "1:1")
    sar = Fraction(*map(int, sar.split(":"))) if sar not in ("0:1", "N/A") else Fraction(1)
    return Fraction(int(stream["width"]), int(stream["height"])) * sar


_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2}),(\d{3}) --> (\d+):(\d{2}):(\d{2}),(\d{3})")


def text_cues(path: Path, k: int) -> list[tuple[float, float]] | None:
    """(start, end) of every cue of subtitle stream k, decoded by FFmpeg; None if the stream is
    not text (FFmpeg cannot convert bitmap subtitles to SRT)."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", f"0:s:{k}",
                        "-f", "srt", "-"], env=ENV, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        return None
    cues = []
    for m in _SRT_TIME.finditer(p.stdout):
        g = list(map(int, m.groups()))
        cues.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                     g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000))
    return cues


def audio_filter(ratio: Fraction, src_rate: int, out_rate: int) -> str:
    """Slow down by `ratio` (>1) with matching pitch drop, exactly.

    Resample to B, relabel as C, resample to the output rate, where B/C == ratio exactly and
    both are integers (asetrate only accepts integer rates).
    """
    k = math.ceil(48000 / ratio.denominator)
    b, c = ratio.numerator * k, ratio.denominator * k
    q = "resampler=soxr:precision=28"
    return f"aresample={b}:{q},asetrate={c},aresample={out_rate}:{q}"


# ---------------------------------------------------------------- pitch measurement

def _log_spectrum(path: Path, astream: int, start: float, dur: float) -> np.ndarray | None:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                          "-i", str(path), "-map", f"0:a:{astream}", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], check=True, env=ENV, capture_output=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    n, hop = 8192, 4096
    if len(x) < n * 4 or np.sqrt(np.mean(x ** 2)) < 1e-4:
        return None
    win = np.hanning(n)
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * win
    mag = np.abs(np.fft.rfft(frames, axis=1)).mean(axis=0)
    freqs = np.fft.rfftfreq(n, 1 / 16000)
    grid = 2 ** np.arange(np.log2(80), np.log2(6000), 1 / 1200)       # 1-cent steps
    spec = np.log(np.interp(grid, freqs, mag) + 1e-9)
    spec -= np.convolve(spec, np.ones(301) / 301, mode="same")          # remove spectral tilt
    return (spec - spec.mean()) / (spec.std() + 1e-12)


def measure_pitch_ratio(orig: Path, restored: Path, astream: int, ratio: Fraction,
                        duration: float) -> float | None:
    """Median measured frequency ratio restored/original over three excerpts."""
    found = []
    for frac in (0.25, 0.5, 0.75):
        t = duration * frac
        dur = min(60.0, duration * 0.2)
        a = _log_spectrum(orig, astream, t, dur)
        b = _log_spectrum(restored, astream, t * float(ratio), dur * float(ratio))
        if a is None or b is None:
            continue
        m = len(a)
        lags = np.arange(-200, 201)
        cc = np.array([np.dot(a[max(0, -l):m - max(0, l)], b[max(0, l):m - max(0, -l)])
                       / (m - abs(l)) for l in lags])
        i = int(np.argmax(cc))
        if 0 < i < len(cc) - 1:                                            # parabolic refinement
            y0, y1, y2 = cc[i - 1], cc[i], cc[i + 1]
            off = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2) if (y0 - 2 * y1 + y2) != 0 else 0
        else:
            off = 0
        found.append(2 ** ((lags[i] + off) / 1200))
    return float(np.median(found)) if found else None


# ---------------------------------------------------------------- sidecars

_TS_PATTERNS = {
    ".srt": re.compile(rb"(\d{1,2}):(\d{2}):(\d{2}),(\d{3})"),
    ".vtt": re.compile(rb"(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})"),
    ".ass": re.compile(rb"(\d):(\d{2}):(\d{2})\.(\d{2})"),
}
_TS_PATTERNS[".ssa"] = _TS_PATTERNS[".ass"]


def retime_sidecar(src: Path, dst: Path, ratio: Fraction) -> None:
    ext = src.suffix.lower()
    pat = _TS_PATTERNS[ext]
    data = src.read_bytes()

    def scale(m: re.Match) -> bytes:
        h = int(m.group(1) or 0)
        mi, s = int(m.group(2)), int(m.group(3))
        frac = m.group(4)
        unit = 10 ** len(frac)                              # 1000 for ms, 100 for cs
        t = Fraction(((h * 60 + mi) * 60 + s) * unit + int(frac)) * ratio
        t = round(t)
        h, rem = divmod(t, 3600 * unit)
        mi, rem = divmod(rem, 60 * unit)
        s, f = divmod(rem, unit)
        if ext == ".srt":
            return f"{h:02}:{mi:02}:{s:02},{f:03}".encode()
        if ext == ".vtt":
            return (f"{h:02}:" if (m.group(1) or h) else "").encode() + f"{mi:02}:{s:02}.{f:03}".encode()
        return f"{h}:{mi:02}:{s:02}.{f:02}".encode()

    if ext in (".ass", ".ssa"):                             # only the Start and End fields
        def event(m: re.Match) -> bytes:
            fields = m.group(2).split(b",", 3)              # Layer/Marked, Start, End, rest
            if len(fields) == 4:
                fields[1] = pat.sub(scale, fields[1])
                fields[2] = pat.sub(scale, fields[2])
            return m.group(1) + b",".join(fields)
        out = re.sub(rb"(?m)^((?:Dialogue|Comment):[ \t]*)([^\r\n]*)", event, data)
    else:
        out = pat.sub(scale, data)
    dst.write_bytes(out)


def sidecars_for(video: Path) -> list[Path]:
    stem = video.stem
    return sorted(p for p in video.parent.iterdir()
                  if p.is_file() and p.suffix.lower() in SIDECAR_EXTS
                  and (p.stem == stem or p.stem.startswith(stem + ".")))


# ---------------------------------------------------------------- restore
#
# Timing and bitstream are two separate jobs, done by two separate tools:
#   * every timestamp (video, subtitles, chapters) is multiplied by 25025/24000 by mkvmerge's
#     linear --sync stretch, which behaves identically for every input container;
#   * the frame rate written inside H.264 SPS and MPEG-2 sequence headers is rewritten by
#     FFmpeg's *_metadata bitstream filters, without decoding, into an intermediate Matroska
#     file that keeps the source timestamps (-copyts).
# mkvmerge's own --default-duration is not used to retime: it rewrites the timestamps of
# Matroska/MP4 input but not of MPEG-TS input, so combined with the stretch it either
# double-stretches or does nothing depending on the container.
#
# Audio is added in a second mux pass, at an offset measured from where the first video frame
# actually landed in the first pass, so the original A/V offset (scaled) holds whatever start
# time mkvmerge or FFmpeg chose for the video. Audio before the first video frame is trimmed.

# H.264 VUI timing counts fields: frame rate = time_scale / (2 * num_units_in_tick).
BITSTREAM_FILTERS = {
    "h264": lambda t: f"h264_metadata=tick_rate={2 * t.numerator}/{t.denominator}",
    "mpeg2video": lambda t: f"mpeg2_metadata=frame_rate={t.numerator}/{t.denominator}",
}


def run_mkvmerge(cmd: list[str]) -> None:
    p = subprocess.run(cmd, env=ENV, capture_output=True, text=True)
    if p.returncode == 1:                                    # warnings only
        for line in p.stdout.splitlines():
            print(f"  mkvmerge: {line}")
    elif p.returncode != 0:
        raise RuntimeError(f"mkvmerge failed ({p.returncode}): {p.stdout.strip()}")


def rewrite_video_bitstream(src: Path, k: int, codec: str, target: Fraction, tmp: Path) -> Path | None:
    """Copy video stream k with its bitstream frame-rate fields set to `target`, or None if the
    codec has no such rewrite. Packets and timestamps are copied unchanged."""
    bsf = BITSTREAM_FILTERS.get(codec)
    if bsf is None:
        return None
    out = tmp / f"v{k}.mkv"
    run(["ffmpeg", "-v", "error", "-nostdin", "-copyts", "-i", str(src), "-map", f"0:v:{k}",
         "-c", "copy", "-bsf:v", bsf(target), "-map_metadata", "-1", "-f", "matroska", str(out)])
    return out


def video_track_options(props: dict) -> list[str]:
    """mkvmerge options carrying a source video track's properties onto track 0 of another file."""
    opts = []
    if props.get("language"):
        opts += ["--language", f"0:{props['language']}"]
    if props.get("track_name"):
        opts += ["--track-name", f"0:{props['track_name']}"]
    if "default_track" in props:
        opts += ["--default-track-flag", f"0:{'yes' if props['default_track'] else 'no'}"]
    if props.get("forced_track"):
        opts += ["--forced-display-flag", "0:yes"]
    if props.get("display_dimensions"):
        opts += ["--display-dimensions", f"0:{props['display_dimensions']}"]
    return opts


# libopus only accepts the Vorbis channel layouts, whose surrounds are labelled "back". The
# side-surround variants are the same speakers under another label: relabel, never remix.
OPUS_LAYOUTS = {"5.1(side)": "5.1", "5.0(side)": "5.0"}


def encode_audio(src: Path, k: int, a: dict, ratio: Fraction, lead: float, video_at: float,
                 tmp: Path, audio_codec: str, opus_kbps_per_ch: int) -> tuple[Path, int]:
    """Slow audio stream k by `ratio` and encode it. `lead` is how far its first sample comes
    after the first video frame, already scaled; `video_at` is where that frame sits in the
    output. Audio before the first video frame (negative lead) is trimmed off here,
    sample-accurately, as docs/pipeline.md section 5 step 2 requires.
    Returns the file and the mkvmerge delay in milliseconds."""
    sr = int(a["sample_rate"])
    out_rate = 48000 if audio_codec == "opus" else sr
    chain = "asetpts=PTS-STARTPTS," + audio_filter(ratio, sr, out_rate)
    if lead < 0:
        chain += f",atrim=start={-lead:.6f},asetpts=PTS-STARTPTS"
        lead = 0.0
    delay_ms = round((video_at + lead) * 1000)
    af = tmp / f"a{k}.mka"
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-map", f"0:a:{k}",
           "-af", chain, "-map_metadata", "-1"]
    if audio_codec == "flac":
        fmt = a.get("sample_fmt", "")
        bits = int(a.get("bits_per_raw_sample") or 0)
        if fmt.startswith("s16") or (0 < bits <= 16):
            cmd += ["-sample_fmt", "s16"]
        else:
            cmd += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
        cmd += ["-c:a", "flac", "-compression_level", "8"]
    else:
        ch = int(a.get("channels", 2))
        layout = OPUS_LAYOUTS.get(a.get("channel_layout", ""))
        if layout:
            cmd[cmd.index("-af") + 1] += f",channelmap=channel_layout={layout}"
        cmd += ["-c:a", "libopus", "-b:a", f"{opus_kbps_per_ch * ch}k"]
    run(cmd + [str(af)])
    return af, delay_ms


def build(src: Path, probe: dict, tmp: Path, target: Fraction, audio_codec: str,
          opus_kbps_per_ch: int) -> Path:
    """Write the restored file into `tmp` and return its path (unverified)."""
    ratio = SOURCE_FPS / target
    r = f"{ratio.numerator}/{ratio.denominator}"
    vids = video_streams(probe)
    tracks = mkv_identify(src)["tracks"]
    vtids = [t["id"] for t in tracks if t["type"] == "video"]
    stids = [t["id"] for t in tracks if t["type"] == "subtitles"]
    props = {t["id"]: t.get("properties", {}) for t in tracks}
    # ffprobe's video streams and mkvmerge's video tracks are both in file order: the k-th of
    # one is the k-th of the other.
    if len(vtids) != len(vids):
        raise RuntimeError(f"ffprobe sees {len(vids)} video streams, mkvmerge {len(vtids)}")

    # ---- pass 1: video (stretched, bitstream rewritten), subtitles, chapters, attachments, tags
    rewritten = {}                                           # mkvmerge tid -> intermediate file
    for k, (tid, v) in enumerate(zip(vtids, vids)):
        path = rewrite_video_bitstream(src, k, v.get("codec_name", ""), target, tmp)
        if path is not None:
            rewritten[tid] = path
    kept = [tid for tid in vtids if tid not in rewritten]
    vmkv = tmp / "video.mkv"
    cmd = ["mkvmerge", "-q", "-o", str(vmkv)]
    if probe.get("chapters"):
        cmd += ["--chapter-sync", f"0,{r}"]
    cmd += ["--video-tracks", ",".join(map(str, kept))] if kept else ["--no-video"]
    cmd += ["--no-audio"]
    for tid in kept + stids:
        cmd += ["--sync", f"{tid}:0,{r}"]
    cmd += [str(src)]
    fileno = {}
    for i, (tid, path) in enumerate(rewritten.items(), start=1):
        cmd += video_track_options(props[tid]) + ["--sync", f"0:0,{r}", str(path)]
        fileno[tid] = i
    order = [f"{fileno[tid]}:0" if tid in fileno else f"0:{tid}" for tid in vtids]
    order += [f"0:{tid}" for tid in stids]
    run_mkvmerge(cmd[:1] + ["--track-order", ",".join(order)] + cmd[1:])

    # ---- pass 2: add the audio, each track placed relative to where the video landed
    v_start = packet_span(src, "v:0")[0]
    w_start = packet_span(vmkv, "v:0")[0]
    vtracks = mkv_identify(vmkv)["tracks"]
    out = tmp / "out.mkv"
    cmd = ["mkvmerge", "-q", "-o", str(out), str(vmkv)]
    order = [f"0:{t['id']}" for t in vtracks if t["type"] == "video"]
    for k, a in enumerate(audio_streams(probe)):
        lead = (first_decoded_pts(src, f"a:{k}") - v_start) * float(ratio)
        af, delay_ms = encode_audio(src, k, a, ratio, lead, w_start, tmp, audio_codec, opus_kbps_per_ch)
        tags = a.get("tags", {})
        if delay_ms:
            cmd += ["--sync", f"0:{delay_ms}"]
        if tags.get("language"):
            cmd += ["--language", f"0:{tags['language']}"]
        if tags.get("title"):
            cmd += ["--track-name", f"0:{tags['title']}"]
        default = bool(a.get("disposition", {}).get("default"))
        cmd += ["--default-track-flag", f"0:{'yes' if default else 'no'}", str(af)]
        order.append(f"{k + 1}:0")
    order += [f"0:{t['id']}" for t in vtracks if t["type"] == "subtitles"]
    run_mkvmerge(cmd[:1] + ["--track-order", ",".join(order)] + cmd[1:])
    return out


def restore_one(src: Path, out_dir: Path, target: Fraction, audio_codec: str,
                opus_kbps_per_ch: int, dry_run: bool) -> str:
    ratio = SOURCE_FPS / target                              # duration factor, 25025/24000
    probe = ffprobe(src)
    vids = video_streams(probe)
    if not vids:
        return "skipped: no video stream"
    fps = stream_fps(vids[0])
    if fps is None or abs(float(fps) - 25.0) > 0.01:
        return f"skipped: video is {fps} fps, not 25"
    dst = out_dir / (src.stem + ".mkv")
    if dst.exists():
        return f"skipped: {dst} already exists"
    if dry_run:
        return f"would write {dst}"

    created = not out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="pal-restore-", dir=out_dir) as tmpd:
            out = build(src, probe, Path(tmpd), target, audio_codec, opus_kbps_per_ch)
            problems = verify(src, out, target, quiet=False)
            if problems:
                raise RuntimeError("verification failed: " + "; ".join(problems))
            os.replace(out, dst)
    except BaseException:
        # a failed file leaves nothing behind, not even the directory made for it
        if created and not any(out_dir.iterdir()):
            out_dir.rmdir()
        raise

    for sc in sidecars_for(src):
        out_sc = out_dir / sc.name
        if out_sc.exists():
            print(f"  sidecar exists, not overwritten: {out_sc}")
            continue
        retime_sidecar(sc, out_sc, ratio)
        print(f"  sidecar retimed: {out_sc.name}")
    return f"restored -> {dst}"


# ---------------------------------------------------------------- verify

def verify(orig: Path, restored: Path, target: Fraction, quiet: bool = False) -> list[str]:
    ratio = SOURCE_FPS / target
    rf = float(ratio)
    problems: list[str] = []

    def say(msg: str) -> None:
        if not quiet:
            print("  " + msg)

    po, pr = ffprobe(orig), ffprobe(restored)
    vos, vrs = video_streams(po), video_streams(pr)
    if len(vos) != len(vrs):
        problems.append(f"video track count changed: {len(vos)} -> {len(vrs)}")
    v0 = w0 = 0.0
    for k, (vo, vr) in enumerate(zip(vos, vrs)):
        tag = "video" if len(vos) == 1 else f"video {k}"
        fr = stream_fps(vr)
        if fr is None or abs(float(fr) - float(target)) > 0.001:
            problems.append(f"{tag}: restored video reports {fr} fps, expected {target}")
        say(f"{tag} fps: {stream_fps(vo)} -> {fr}")
        dar_o, dar_r = display_aspect(vo), display_aspect(vr)
        if dar_o != dar_r:
            problems.append(f"{tag}: display aspect ratio changed: {dar_o} -> {dar_r}")

        rates = bitstream_rates(restored, k, vr.get("codec_name", ""))
        if rates is not None:
            wrong = sorted({str(x) for x in rates if x != target})
            if wrong:
                problems.append(f"{tag}: bitstream headers declare {', '.join(wrong)} fps, expected {target}")
            if not rates and bitstream_rates(orig, k, vo.get("codec_name", "")):
                problems.append(f"{tag}: bitstream frame-rate fields were lost")
            say(f"{tag} bitstream: {len(rates)} header(s) declaring a frame rate, "
                f"{'all ' + str(target) if rates and not wrong else 'see above' if wrong else 'none'}")

        # Duration from the timestamps themselves: first to last frame, plus one frame period.
        o_first, o_last, _, vn = packet_span(orig, f"v:{k}")
        r_first, r_last, _, wn = packet_span(restored, f"v:{k}")
        if k == 0:
            v0, w0 = o_first, r_first                         # audio offsets are measured from here
        if vn != wn:
            problems.append(f"{tag}: frame count changed: {vn} -> {wn}")
        say(f"{tag} frames: {vn} -> {wn}")
        vdur_o = o_last - o_first + 1 / float(SOURCE_FPS)
        vdur_r = r_last - r_first + 1 / float(target)
        if abs(vdur_r - vdur_o * rf) > 0.05:
            problems.append(f"{tag}: duration {vdur_r:.3f}s, expected {vdur_o * rf:.3f}s")
        say(f"{tag} duration: {vdur_o:.3f}s -> {vdur_r:.3f}s (x{vdur_r / vdur_o:.6f}, expected x{rf:.6f})")

    ao, ar = audio_streams(po), audio_streams(pr)
    if len(ao) != len(ar):
        problems.append(f"audio track count changed: {len(ao)} -> {len(ar)}")
    for k in range(min(len(ao), len(ar))):
        a0, _, a1, _ = packet_span(orig, f"a:{k}")
        b0, _, b1, _ = packet_span(restored, f"a:{k}")
        span_o = a1 - a0
        # Sync: the audio end, relative to the video start, must scale exactly by the ratio.
        # (Audio that started before the video is trimmed, so only the end is comparable.)
        end_o, end_r = a1 - v0, b1 - w0
        if abs(end_r - end_o * rf) > 0.1:
            problems.append(f"audio {k}: ends at {end_r:.3f}s, expected {end_o * rf:.3f}s (A/V drift)")
        start_o, start_r = a0 - v0, b0 - w0
        exp_start = max(start_o * rf, 0.0)
        if abs(start_r - exp_start) > 0.05:
            problems.append(f"audio {k}: starts at {start_r:+.3f}s, expected {exp_start:+.3f}s")
        say(f"audio {k}: end {end_o:.3f}s -> {end_r:.3f}s (expected {end_o * rf:.3f}s), "
            f"start {start_o:+.3f}s -> {start_r:+.3f}s")
        pitch = measure_pitch_ratio(orig, restored, k, ratio, span_o)
        expected = 1 / rf
        if pitch is None:
            say(f"audio {k}: pitch not measurable (too quiet)")
        else:
            cents = 1200 * math.log2(pitch)
            exp_c = 1200 * math.log2(expected)
            say(f"audio {k}: pitch shift {cents:+.1f} cents (expected {exp_c:+.1f})")
            if abs(cents - exp_c) > 8:
                problems.append(f"audio {k}: pitch shift {cents:+.1f} cents, expected {exp_c:+.1f}")

    so = [s for s in po["streams"] if s["codec_type"] == "subtitle"]
    sr = [s for s in pr["streams"] if s["codec_type"] == "subtitle"]
    if len(so) != len(sr):
        problems.append(f"subtitle track count changed: {len(so)} -> {len(sr)}")
    # Subtitle times are compared relative to the first video frame, which is what they sync to.
    for k in range(min(len(so), len(sr))):
        co_, cr_ = text_cues(orig, k), text_cues(restored, k)
        if co_ is not None and cr_ is not None:
            # text: every decoded cue's start and end (empty "clear" samples are not cues)
            if len(co_) != len(cr_):
                problems.append(f"subtitle {k}: cue count {len(co_)} -> {len(cr_)}")
            off = [max(abs((b0 - w0) - (a0 - v0) * rf), abs((b1 - w0) - (a1 - v0) * rf))
                   for (a0, a1), (b0, b1) in zip(co_, cr_)]
            if off and max(off) > 0.01:
                i = off.index(max(off))
                problems.append(f"subtitle {k}: cue {i + 1} at {cr_[i][0]:.3f}s, expected "
                                f"{w0 + (co_[i][0] - v0) * rf:.3f}s")
            first = f"first {co_[0][0]:.3f}s -> {cr_[0][0]:.3f}s" if co_ and cr_ else "empty"
            say(f"subtitle {k}: {len(co_)} cues, {first}, largest timing error "
                f"{max(off, default=0) * 1000:.1f} ms")
        else:
            # image (PGS, VobSub, DVB): packets, first and last
            s0, _, s1, n0 = packet_span(orig, f"s:{k}")
            t0, _, t1, n1 = packet_span(restored, f"s:{k}")
            if n0 != n1:
                problems.append(f"subtitle {k}: event count {n0} -> {n1}")
            e0, e1 = w0 + (s0 - v0) * rf, w0 + (s1 - v0) * rf
            if n0 and (abs(t0 - e0) > 0.01 or abs(t1 - e1) > 0.01):
                problems.append(f"subtitle {k}: first/last {t0:.3f}/{t1:.3f}s, expected {e0:.3f}/{e1:.3f}s")
            say(f"subtitle {k}: {n0} events, first {s0:.3f}s -> {t0:.3f}s")

    co, cr = po.get("chapters", []), pr.get("chapters", [])
    if len(co) != len(cr):
        problems.append(f"chapter count changed: {len(co)} -> {len(cr)}")
    for i, (c, d) in enumerate(zip(co, cr)):
        if abs(float(d["start_time"]) - float(c["start_time"]) * rf) > 0.01:
            problems.append(f"chapter {i}: {d['start_time']}s, expected {float(c['start_time']) * rf:.3f}s")
    if co:
        say(f"chapters: {len(co)} retimed")

    say("PASS" if not problems else "FAIL: " + "; ".join(problems))
    return problems


# ---------------------------------------------------------------- CLI

def doctor() -> int:
    ok = True
    for tool in ("ffmpeg", "ffprobe", "mkvmerge"):
        path = shutil.which(tool)
        print(f"{tool:9} {path or 'MISSING'}")
        ok &= path is not None
    if shutil.which("ffmpeg"):
        test = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=0.1",
                               "-af", "aresample=50050:resampler=soxr", "-f", "null", "-"],
                              env=ENV, capture_output=True, text=True)
        print(f"soxr      {'ok' if test.returncode == 0 else 'MISSING: ' + test.stderr.strip()}")
        ok &= test.returncode == 0
        bsfs = set(capture(["ffmpeg", "-v", "error", "-bsfs"]).split())
        for bsf in ("h264_metadata", "mpeg2_metadata", "filter_units", "trace_headers"):
            print(f"ffmpeg bsf {bsf:27} {'ok' if bsf in bsfs else 'MISSING'}")
            ok &= bsf in bsfs
        encs = capture(["ffmpeg", "-v", "error", "-encoders"])
        for enc, needed in (("flac", True), ("libopus", False)):
            have = re.search(rf"^\s*A\S*\s+{enc}\s", encs, re.M) is not None
            print(f"ffmpeg encoder {enc:23} {'ok' if have else 'MISSING' if needed else 'missing (only for --audio-codec opus)'}")
            ok &= have or not needed
    if shutil.which("mkvmerge"):
        h = capture(["mkvmerge", "--help"])
        for opt in ("--sync", "--chapter-sync", "--track-order", "--display-dimensions"):
            print(f"mkvmerge {opt:29} {'ok' if opt in h else 'MISSING'}")
            ok &= opt in h
    print("OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def collect(paths: list[str], out_dir_name: str) -> list[Path]:
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.suffix.lower() in VIDEO_EXTS
                            and out_dir_name not in f.relative_to(p).parts[:-1])
        elif p.is_file():
            files.append(p)
        else:
            print(f"not found: {p}", file=sys.stderr)
    return [f.resolve() for f in files]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    r = sub.add_parser("restore")
    r.add_argument("paths", nargs="+")
    r.add_argument("--target-fps", default="24000/1001", help="original frame rate (default 24000/1001)")
    r.add_argument("--audio-codec", choices=("flac", "opus"), default="flac")
    r.add_argument("--opus-kbps-per-channel", type=int, default=96)
    r.add_argument("--out-dir", help="output directory (default: <input dir>/restored)")
    r.add_argument("--dry-run", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("original")
    v.add_argument("restored")
    v.add_argument("--target-fps", default="24000/1001")
    args = ap.parse_args()

    if args.cmd == "doctor":
        return doctor()
    target = Fraction(args.target_fps)
    if args.cmd == "verify":
        return 1 if verify(Path(args.original), Path(args.restored), target) else 0

    files = collect(args.paths, "restored")
    results = {"restored": 0, "planned": 0, "skipped": 0, "failed": 0}
    for f in files:
        out_dir = Path(args.out_dir).resolve() if args.out_dir else f.parent / "restored"
        print(f"{f}")
        try:
            msg = restore_one(f, out_dir, target, args.audio_codec, args.opus_kbps_per_channel, args.dry_run)
            key = "skipped" if msg.startswith("skipped") else "planned" if msg.startswith("would") else "restored"
            results[key] += 1
        except Exception as e:                               # keep going with the batch
            msg = f"FAILED: {e}"
            results["failed"] += 1
        print(f"  {msg}")
    print("\n" + ", ".join(f"{v} {k}" for k, v in results.items() if v or k in ("restored", "failed")))
    return 1 if results["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
