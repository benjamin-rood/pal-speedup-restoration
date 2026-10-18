#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26"]
# ///
"""Undo PAL speedup: restore 23.976 fps material that was played back at 25 fps.

Every original frame is still present in a PAL-speedup release; only the timing and the
audio were changed. This tool:

  * stretches the video timestamps by 25025/24000 and rewrites the frame rate in H.264 and HEVC
    parameter sets and MPEG-2 sequence headers to 24000/1001, without re-encoding (frames are
    untouched),
  * resamples every audio track by exactly 24000/25025 (restores both speed and pitch),
  * scales embedded subtitle and chapter times by 25025/24000,
  * scales sidecar subtitle files (.srt/.ass/.ssa/.vtt) next to the input,
  * verifies frame rate (container and bitstream), frame count, track and timeline offsets,
    durations, A/V offset, subtitle/chapter timing, sidecar cues and pitch before publishing.

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
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import numpy as np

SOURCE_FPS = Fraction(25)
VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".mpg", ".mpeg", ".vob", ".ts", ".m2ts", ".mts", ".webm"}
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
OUT_DIR_NAME = "restored"
TMP_PREFIX = "pal-restore-"
ENV = dict(os.environ, LANG="C.UTF-8", LC_ALL="C.UTF-8")
# Streams that start late (a second DVD audio track, a subtitle) are only described if the
# prober reads far enough; 100 s / 100 MB covers DVD and broadcast captures.
PROBE = ["-analyzeduration", "100M", "-probesize", "100M"]


# ---------------------------------------------------------------- helpers

def run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, env=ENV, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): {p.stderr.strip()[-2000:]}")


def capture(cmd: list[str]) -> str:
    p = subprocess.run(cmd, env=ENV, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): {p.stderr.strip()[-2000:]}")
    return p.stdout


def ffprobe(path: Path) -> dict:
    return json.loads(capture(["ffprobe", "-v", "error", *PROBE, "-show_streams", "-show_format",
                               "-show_chapters", "-of", "json", str(path)]))


def mkv_identify(path: Path) -> dict:
    return json.loads(capture(["mkvmerge", "-J", str(path)]))


def video_streams(probe: dict) -> list[dict]:
    return [s for s in probe["streams"] if s["codec_type"] == "video"
            and not s.get("disposition", {}).get("attached_pic")]


def audio_streams(probe: dict) -> list[dict]:
    return [s for s in probe["streams"] if s["codec_type"] == "audio"]


def subtitle_streams(probe: dict) -> list[dict]:
    return [s for s in probe["streams"] if s["codec_type"] == "subtitle"]


def stream_fps(stream: dict) -> Fraction | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        v = stream.get(key, "0/0")
        if v and not v.startswith("0/") and v != "0/0":
            return Fraction(v)
    return None


def packet_span(path: Path, selector: str) -> tuple[float, float, float, int]:
    """(first pts, last pts, end of last packet, packet count) for one stream, from demuxed
    packets. +genpts gives packets without a pts (AVI with B-frames) their presentation time."""
    out = capture(["ffprobe", "-v", "error", *PROBE, "-fflags", "+genpts", "-select_streams", selector,
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
    """Presentation time of the first decoded frame (after decoder priming/skip handling). Reads
    eight packets: the first ones can be all encoder priming, which the decoder discards."""
    out = capture(["ffprobe", "-v", "error", *PROBE, "-select_streams", selector, "-read_intervals", "%+#8",
                   "-show_entries", "frame=pts_time,best_effort_timestamp_time", "-of", "json", str(path)])
    for f in json.loads(out).get("frames", []):
        for key in ("pts_time", "best_effort_timestamp_time"):
            if f.get(key) not in (None, "N/A"):
                return float(f[key])
    raise RuntimeError(f"{path}: stream {selector} has no decodable frame")


MPEG2_FRAME_RATES = {1: Fraction(24000, 1001), 2: Fraction(24), 3: Fraction(25), 4: Fraction(30000, 1001),
                     5: Fraction(30), 6: Fraction(50), 7: Fraction(60000, 1001), 8: Fraction(60)}
_TRACE_LINE = re.compile(r"^\[trace_headers[^]]*\]\s+\d+\s+(\w+)\s.*=\s*(-?\d+)\s*$")


def bitstream_rates(path: Path, vindex: int, codec: str) -> list[Fraction] | None:
    """Frame rate declared by every sequence-level header of one video track, in stream order.

    H.264: every SPS (in the codec private data and in-band) whose VUI carries timing info;
    rate = time_scale / (2 * num_units_in_tick). HEVC: every VPS and SPS VUI carrying timing
    info; rate = time_scale / num_units_in_tick. MPEG-2: every sequence header, with its
    sequence extension. Returns None for codecs whose headers are not checked.
    """
    if codec == "h264":
        units, start = "7", "seq_parameter_set_id"
    elif codec == "hevc":
        units, start = "32|33", "nal_unit_type"
    elif codec == "mpeg2video":
        units, start = "0xb3|0xb5", "frame_rate_code"
    else:
        return None
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "debug", *PROBE, "-i", str(path),
                        "-map", f"0:v:{vindex}", "-c", "copy",
                        "-bsf:v", f"filter_units=pass_types={units},trace_headers", "-f", "null", "-"],
                       env=ENV, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"reading bitstream headers of {path} failed: {p.stderr.strip()[-500:]}")
    headers: list[dict[str, int]] = []
    for line in p.stderr.splitlines():
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
        elif codec == "hevc":
            for pre in ("vps_", "vui_"):
                if h.get(f"{pre}timing_info_present_flag") and h.get(f"{pre}num_units_in_tick"):
                    rates.append(Fraction(h[f"{pre}time_scale"], h[f"{pre}num_units_in_tick"]))
        else:
            rate = MPEG2_FRAME_RATES.get(h["frame_rate_code"])
            if rate is not None:
                rates.append(rate * (h.get("frame_rate_extension_n", 0) + 1)
                             / (h.get("frame_rate_extension_d", 0) + 1))
    return rates


def _clock(t: str) -> float:
    h, m, sec = t.split(":")
    return (int(h) * 60 + int(m)) * 60 + float(sec)


def chapter_editions(path: Path, probe: dict) -> list[dict]:
    """Every chapter edition of a file, as {"default": bool, "chapters": [(start, end or None)]}
    with nested chapters flattened in order. Matroska: every edition, read with mkvextract
    (ffprobe shows only one). Other containers have one chapter list, read with ffprobe."""
    if "matroska" not in probe["format"]["format_name"]:
        chs = [(float(c["start_time"]), float(c["end_time"])) for c in probe.get("chapters", [])]
        return [{"default": True, "chapters": chs}] if chs else []
    xml = capture(["mkvextract", str(path), "chapters", "-"]).lstrip("\ufeff").strip()
    if not xml:
        return []
    editions = []
    for ed in ET.fromstring(xml).iter("EditionEntry"):
        chs = []
        for atom in ed.iter("ChapterAtom"):
            end = atom.findtext("ChapterTimeEnd")
            chs.append((_clock(atom.findtext("ChapterTimeStart")), _clock(end) if end else None))
        editions.append({"default": ed.findtext("EditionFlagDefault") == "1", "chapters": chs})
    return editions


def display_aspect(stream: dict) -> Fraction:
    sar = stream.get("sample_aspect_ratio", "1:1")
    sar = Fraction(*map(int, sar.split(":"))) if sar not in ("0:1", "N/A") else Fraction(1)
    return Fraction(int(stream["width"]), int(stream["height"])) * sar


_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2}),(\d{3}) --> (\d+):(\d{2}):(\d{2}),(\d{3})")


def decoded_cues(cmd_input: list[str]) -> list[tuple[float, float]] | None:
    """(start, end) of every cue FFmpeg decodes from the given input arguments, written as SRT
    on absolute timestamps (-copyts); None if FFmpeg cannot (bitmap subtitles, unparseable)."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-copyts", *cmd_input, "-f", "srt", "-"],
                       env=ENV, capture_output=True, text=True, errors="replace")
    if p.returncode != 0:
        return None
    cues = []
    for m in _SRT_TIME.finditer(p.stdout):
        g = list(map(int, m.groups()))
        cues.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                     g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000))
    return cues


def text_cues(path: Path, k: int) -> list[tuple[float, float]] | None:
    """Cues of embedded subtitle stream k, or None if the stream is not text."""
    return decoded_cues(["-i", str(path), "-map", f"0:s:{k}"])


def audio_filter(ratio: Fraction, src_rate: int, out_rate: int) -> str:
    """Slow down by `ratio` (>1) with matching pitch drop, exactly.

    Resample to B, relabel as C, resample to the output rate, where B/C == ratio exactly and
    both are integers (asetrate only accepts integer rates). C is the smallest multiple of the
    ratio's denominator not below the source rate, so the resample to B never narrows the band
    (96 kHz in, 100100 -> 96000; 48 kHz in, 50050 -> 48000).
    """
    k = math.ceil(src_rate / ratio.denominator)
    b, c = ratio.numerator * k, ratio.denominator * k
    q = "resampler=soxr:precision=28"
    return f"aresample={b}:{q},asetrate={c},aresample={out_rate}:{q}"


# ---------------------------------------------------------------- pitch measurement

def _log_spectrum(path: Path, astream: int, start: float, dur: float) -> np.ndarray | None:
    # `start` is an absolute timestamp (packet_span's frame of reference). Input -ss is a position
    # from the file's start time, so that is subtracted; -copyts with -ss selects nothing on a
    # transport stream whose clock starts far from 0 (a Blu-ray m2ts at 600 s).
    origin = float(ffprobe(path)["format"].get("start_time") or 0.0)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(start - origin, 0.0):.3f}",
                          "-t", f"{dur:.3f}", *PROBE, "-i", str(path), "-map", f"0:a:{astream}",
                          "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         check=True, env=ENV, capture_output=True).stdout
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


# An excerpt needs four 8192-sample analysis windows at 16 kHz: 2.048 s.
MIN_EXCERPT = 2.1


def measure_pitch_ratio(orig: Path, restored: Path, astream: int, a_first: float, a_end: float,
                        to_restored) -> tuple[float | None, str]:
    """(median measured frequency ratio restored/original, "") over three excerpts inside the
    track's own span [a_first, a_end] (original time), or (None, reason). `to_restored` maps an
    original time to the restored one."""
    found = []
    span = a_end - a_first
    if span < MIN_EXCERPT:
        return None, f"track too short ({span:.2f}s)"
    dur = min(60.0, max(span * 0.2, MIN_EXCERPT))
    for frac in (0.25, 0.5, 0.75):
        t = a_first + (span - dur) * frac
        a = _log_spectrum(orig, astream, t, dur)
        b = _log_spectrum(restored, astream, to_restored(t), to_restored(t + dur) - to_restored(t))
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
    return (float(np.median(found)), "") if found else (None, "the excerpts are silent")


# ---------------------------------------------------------------- sidecars
#
# Text is decoded by its byte-order mark (UTF-8, UTF-16LE/BE) and re-encoded the same way.
# Without a BOM it is handled as Latin-1, which maps every byte to one character and back, so
# UTF-8 and legacy 8-bit encodings pass through byte for byte. Only timing is rewritten:
#   * SRT/VTT: the two timestamps of a cue timing line ("start --> end ..."); cue text, NOTE
#     blocks and the VTT header (X-TIMESTAMP-MAP) are untouched. Fractions of 1-3 digits are
#     read as FFmpeg reads them (a millisecond count) and written as 3 digits.
#   * ASS/SSA: the Start and End fields of Dialogue/Comment lines in [Events], located by that
#     section's Format line.
# Lines end at CR LF, LF or CR only: str.splitlines() also breaks at U+0085 and others, and a
# Latin-1 decoded UTF-8 file is full of byte 0x85 (it is in many Cyrillic and Polish letters).
# Every retimed sidecar is verified by decoding input and output with FFmpeg, and ASS/SSA also
# by reading the event fields named by the Format line (FFmpeg reads Start/End by position, so
# it decodes nothing from a reordered Format line).

_BOMS = [(b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
_CUE_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})([,.])(\d{1,3})"
_TIMING_LINE = re.compile(rf"^(\s*){_CUE_TS}(\s*-->\s*){_CUE_TS}(.*)$")
_ASS_TS = re.compile(r"^(\s*)(\d+):(\d{2}):(\d{2})\.(\d{1,3})(\s*)$")
_LINES = re.compile(r"[^\r\n]*(?:\r\n|\n|\r|$)")
# v4+ (Layer) and v4 (Marked) Events fields, in the order used when a file has no Format line.
ASS_DEFAULT_FIELDS = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]


def _split_lines(text: str) -> list[str]:
    return [m for m in _LINES.findall(text) if m]


def _read_text(path: Path) -> tuple[str, bytes, str]:
    data = path.read_bytes()
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data[len(bom):].decode(enc), bom, enc
    return data.decode("latin-1"), b"", "latin-1"


def _scale_ms(ms: int, ratio: Fraction) -> int:
    return round(ms * ratio)


def _fmt_hms(ms: int, frac_digits: int, sep: str, min_hour_digits: int, hourless: bool) -> str:
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, f = divmod(rem, 1000)
    frac = f"{f:03}" if frac_digits == 3 else f"{round(f / 10):02}"
    if frac_digits == 2 and frac == "100":                      # 995..999 ms round up a second
        return _fmt_hms(ms - f + 1000, frac_digits, sep, min_hour_digits, hourless)
    if hourless and h == 0:
        return f"{m:02}:{s:02}{sep}{frac}"
    return f"{h:0{min_hour_digits}}:{m:02}:{s:02}{sep}{frac}"


def _retime_cue_line(m: re.Match, ratio: Fraction, vtt: bool) -> str:
    g = m.groups()

    def one(h, mi, s, sep, frac):
        ms = ((int(h or 0) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(frac)
        return _fmt_hms(_scale_ms(ms, ratio), 3, sep, 2, vtt and h is None)

    return g[0] + one(*g[1:6]) + g[6] + one(*g[7:12]) + g[12]


def retime_text(ext: str, text: str, ratio: Fraction) -> str:
    lines = _split_lines(text)
    out = []
    if ext in (".srt", ".vtt"):
        for line in lines:
            body = line.rstrip("\r\n")
            m = _TIMING_LINE.match(body)
            out.append(_retime_cue_line(m, ratio, ext == ".vtt") + line[len(body):] if m else line)
        return "".join(out)
    # ASS/SSA
    section, fields = "", None
    for line in lines:
        body = line.rstrip("\r\n")
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section, fields = stripped.lower(), None
        elif section == "[events]" and stripped.lower().startswith("format:"):
            fields = [f.strip().lower() for f in stripped.split(":", 1)[1].split(",")]
        elif section == "[events]" and re.match(r"^(Dialogue|Comment)\s*:", body):
            kind, rest = body.split(":", 1)
            names = fields or ASS_DEFAULT_FIELDS
            parts = rest.split(",", len(names) - 1)
            for name in ("start", "end"):
                if name in names and names.index(name) < len(parts):
                    i = names.index(name)
                    tm = _ASS_TS.match(parts[i])
                    if tm:
                        lead, h, mi, s, frac, trail = tm.groups()
                        ms = ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(frac.ljust(3, "0"))
                        parts[i] = lead + _fmt_hms(_scale_ms(ms, ratio), 2, ".", 1, False) + trail
            line = kind + ":" + ",".join(parts) + line[len(body):]
        out.append(line)
    return "".join(out)


def retime_sidecar(src: Path, dst: Path, ratio: Fraction) -> None:
    text, bom, enc = _read_text(src)
    dst.write_bytes(bom + retime_text(src.suffix.lower(), text, ratio).encode(enc))


def ass_event_times(text: str) -> list[tuple[float, float] | None]:
    """(start, end) of every Dialogue/Comment line of an ASS/SSA text, read by the fields its
    [Events] Format line names; None for a line whose times cannot be read. Written apart from
    retime_text so that it checks it rather than repeats it."""
    events = re.search(r"(?ims)^\[events\][^\r\n]*$(.*?)(?=^\[|\Z)", text)
    if not events:
        return []
    body = events.group(1)
    fmt = re.search(r"(?im)^format\s*:([^\r\n]*)", body)
    names = [f.strip().lower() for f in fmt.group(1).split(",")] if fmt else ASS_DEFAULT_FIELDS
    times = []
    for m in re.finditer(r"(?m)^(?:Dialogue|Comment)\s*:([^\r\n]*)", body):
        parts = m.group(1).split(",", len(names) - 1)
        got = []
        for name in ("start", "end"):
            i = names.index(name) if name in names else len(parts)
            tm = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})\.(\d{1,3})\s*", parts[i]) if i < len(parts) else None
            if tm:
                h, mi, sec, frac = tm.groups()
                got.append((int(h) * 60 + int(mi)) * 60 + int(sec) + int(frac.ljust(3, "0")) / 1000)
        times.append(tuple(got) if len(got) == 2 else None)
    return times


def _sidecar_input(path: Path) -> list[str]:
    """FFmpeg input arguments for a sidecar. FFmpeg refuses text that is not UTF-8; a file with
    no byte-order mark that is not UTF-8 is a legacy 8-bit encoding, read as Latin-1 (every byte
    decodes; only timing is compared)."""
    data = path.read_bytes()
    if not any(data.startswith(bom) for bom, _ in _BOMS):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return ["-sub_charenc", "ISO-8859-1", "-i", str(path)]
    return ["-i", str(path)]


def _compare_cues(name: str, how: str, a: list, b: list, ratio: Fraction) -> str | None:
    if len(a) != len(b):
        return f"{name}: {len(a)} cues -> {len(b)} ({how})"
    for i, (x, y) in enumerate(zip(a, b)):
        if x is None or y is None:
            return f"{name}: event {i + 1} has unreadable times ({how})"
        if abs(y[0] - x[0] * float(ratio)) > 0.01 or abs(y[1] - x[1] * float(ratio)) > 0.01:
            return (f"{name}: cue {i + 1} at {y[0]:.3f}-{y[1]:.3f}s, expected "
                    f"{x[0] * float(ratio):.3f}-{x[1] * float(ratio):.3f}s ({how})")
    return None


def verify_sidecar(src: Path, dst: Path, ratio: Fraction) -> str | None:
    """A problem description, or None if every cue FFmpeg reads from `src` appears in `dst`
    scaled by `ratio` (within 10 ms; ASS stores centiseconds), and for ASS/SSA also every event
    read by its Format line."""
    a = decoded_cues(_sidecar_input(src))
    b = decoded_cues(_sidecar_input(dst))
    if a is None or b is None:
        return f"{src.name}: FFmpeg cannot read {'the original' if a is None else 'the retimed file'}"
    problem = _compare_cues(src.name, "decoded by FFmpeg", a, b, ratio)
    if problem or src.suffix.lower() not in (".ass", ".ssa"):
        return problem
    ea, eb = ass_event_times(_read_text(src)[0]), ass_event_times(_read_text(dst)[0])
    return _compare_cues(src.name, "events read by the Format line", ea, eb, ratio)


def sidecars_for(video: Path) -> list[Path]:
    """Sidecars named <base>.<ext> or <base>.<anything>.<ext>. A sidecar that also matches a
    sibling video with a longer base name (film.part2.srt next to film.part2.mkv) is that
    video's, not this one's."""
    siblings = [p.stem for p in video.parent.iterdir()
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS]

    def owner(stem: str) -> str | None:
        matches = [b for b in siblings if stem == b or stem.startswith(b + ".")]
        return max(matches, key=len) if matches else None

    return sorted(p for p in video.parent.iterdir()
                  if p.is_file() and p.suffix.lower() in SIDECAR_EXTS and owner(p.stem) == video.stem)


# ---------------------------------------------------------------- restore
#
# One time base. A non-Matroska input is first copied, every stream unchanged, into a
# Matroska working file by FFmpeg, which shifts all streams by the same amount so the file
# starts at 0, generates missing presentation timestamps (AVI with B-frames) and carries
# subtitle streams mkvmerge cannot read from MPEG-PS (DVD subpictures). Every later step reads
# only that file, so every track shares one time base. (mkvmerge rebases MPEG-TS/PS tracks on
# its own; mixing tracks it read from the source with tracks FFmpeg copied left them up to
# hours apart.) Matroska input is used as it is: both tools keep its timestamps.
#
# Timing and bitstream are two separate jobs, done by two separate tools:
#   * every timestamp (video, subtitles, chapters) is multiplied by 25025/24000 by mkvmerge's
#     linear --sync stretch, which behaves identically for every input container;
#   * the frame rate written inside H.264 SPS and MPEG-2 sequence headers is rewritten by
#     FFmpeg's *_metadata bitstream filters, without decoding, into an intermediate Matroska
#     file that keeps the working file's timestamps (-copyts).
# mkvmerge's own --default-duration is not used to retime: it rewrites the timestamps of
# Matroska/MP4 input but not of MPEG-TS input, so combined with the stretch it either
# double-stretches or does nothing depending on the container.
#
# Audio is added in a second mux pass, at an offset measured from where the first video frame
# actually landed in the first pass, so the original A/V offset (scaled) holds whatever start
# time mkvmerge or FFmpeg chose for the video. Audio before the first video frame is trimmed.

# H.264 VUI timing counts fields: frame rate = time_scale / (2 * num_units_in_tick).
# HEVC VPS and VUI timing count frames: frame rate = time_scale / num_units_in_tick.
BITSTREAM_FILTERS = {
    "h264": lambda t: f"h264_metadata=tick_rate={2 * t.numerator}/{t.denominator}",
    "hevc": lambda t: f"hevc_metadata=tick_rate={t.numerator}/{t.denominator}",
    "mpeg2video": lambda t: f"mpeg2_metadata=frame_rate={t.numerator}/{t.denominator}",
}


def run_mkvmerge(cmd: list[str]) -> None:
    p = subprocess.run(cmd, env=ENV, capture_output=True, text=True, errors="replace")
    if p.returncode == 1:                                    # warnings only
        for line in p.stdout.splitlines():
            print(f"  mkvmerge: {line}")
    elif p.returncode != 0:
        raise RuntimeError(f"mkvmerge failed ({p.returncode}): {p.stdout.strip()}")


# How far before 0 an output may start, and how late a Matroska input may start and still be
# used as it is: Matroska cannot store negative timestamps, so a source whose B-frames give it
# negative decode times is shifted later as a whole by up to its reorder delay (x264's deepest,
# three frames, is 120 ms at 25 fps).
TIMELINE_SLACK = 0.2


def working_file(src: Path, probe: dict, tmp: Path) -> Path:
    """The file every later step reads: the input itself if it is Matroska starting at 0,
    otherwise a Matroska copy of its video, audio, subtitle and attachment streams on one time
    base starting at 0."""
    start = float(probe["format"].get("start_time") or 0.0)
    if "matroska" in probe["format"]["format_name"] and start <= TIMELINE_SLACK:
        return src
    work = tmp / "work.mkv"
    # Matroska cannot hold MP4 text subtitles (mov_text) as they are: they become SRT text,
    # timing unchanged, as mkvmerge itself would store them.
    convert = []
    for k, s in enumerate(subtitle_streams(probe)):
        if s.get("codec_name") == "mov_text":
            convert += [f"-c:s:{k}", "srt"]
    # Nor Blu-ray / DVD LPCM: it becomes plain PCM of the same depth, sample for sample.
    for k, a in enumerate(audio_streams(probe)):
        if a.get("codec_name") in ("pcm_bluray", "pcm_dvd"):
            deep = int(a.get("bits_per_raw_sample") or 0) > 16 or a.get("sample_fmt", "").startswith("s32")
            convert += [f"-c:a:{k}", "pcm_s24le" if deep else "pcm_s16le"]
    # Cover art (an attached-picture video stream in MP4) would be copied as a real video
    # track; it becomes a Matroska attachment named cover.<ext>, the Matroska convention.
    covers = []
    for n, s in enumerate(x for x in probe["streams"] if x.get("disposition", {}).get("attached_pic")):
        ext, mime = ("png", "image/png") if s.get("codec_name") == "png" else ("jpg", "image/jpeg")
        pic = tmp / f"cover{n + 1 if n else ''}.{ext}"
        run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-map", f"0:{s['index']}", "-c", "copy",
             "-frames:v", "1", "-f", "image2", "-update", "1", str(pic)])
        covers += ["-attach", str(pic), f"-metadata:s:t:{n}", f"mimetype={mime}"]
    vmaps = [a for s in video_streams(probe) for a in ("-map", f"0:{s['index']}")]
    run(["ffmpeg", "-v", "error", "-nostdin", "-fflags", "+genpts", *PROBE, "-i", str(src), *covers,
         *vmaps, "-map", "0:a?", "-map", "0:s?", "-map", "0:t?", "-c", "copy", *convert,
         "-f", "matroska", str(work)])
    return work


def rewrite_video_bitstream(work: Path, k: int, codec: str, target: Fraction, tmp: Path) -> Path | None:
    """Copy video stream k with its bitstream frame-rate fields set to `target`, or None if the
    codec has no such rewrite. Packets and timestamps are copied unchanged."""
    bsf = BITSTREAM_FILTERS.get(codec)
    if bsf is None:
        return None
    out = tmp / f"v{k}.mkv"
    run(["ffmpeg", "-v", "error", "-nostdin", "-copyts", "-i", str(work), "-map", f"0:v:{k}",
         "-c", "copy", "-bsf:v", bsf(target), "-map_metadata", "-1", "-map_chapters", "-1",
         "-f", "matroska", str(out)])
    return out


_FLAGS = [("flag_commentary", "--commentary-flag"), ("flag_hearing_impaired", "--hearing-impaired-flag"),
          ("flag_visual_impaired", "--visual-impaired-flag"), ("flag_original", "--original-flag"),
          ("flag_text_descriptions", "--text-descriptions-flag")]


def track_options(props: dict, video: bool) -> list[str]:
    """mkvmerge options carrying a track's properties (as mkvmerge -J reports them) onto track 0
    of another file: language with region, name, default/forced/accessibility flags, and for
    video the display dimensions."""
    opts = []
    lang = props.get("language_ietf") or props.get("language")
    if lang and lang != "und":
        opts += ["--language", f"0:{lang}"]
    if props.get("track_name"):
        opts += ["--track-name", f"0:{props['track_name']}"]
    opts += ["--default-track-flag", f"0:{'yes' if props.get('default_track') else 'no'}"]
    if props.get("forced_track"):
        opts += ["--forced-display-flag", "0:yes"]
    for key, opt in _FLAGS:
        if props.get(key):
            opts += [opt, "0:yes"]
    if video and props.get("display_dimensions"):
        opts += ["--display-dimensions", f"0:{props['display_dimensions']}"]
    return opts


# libopus only accepts the Vorbis channel layouts, whose surrounds are labelled "back". The
# side-surround variants are the same speakers under another label: relabel, never remix.
# Layouts outside the Vorbis set (2.1, 4.0, 6.0, 7.1(wide), ...) have no Opus speaker mapping:
# they are encoded as discrete channels (mapping family 255), every channel kept, none remixed.
OPUS_LAYOUTS = {"5.1(side)": "5.1", "5.0(side)": "5.0"}
OPUS_VORBIS_LAYOUTS = {"mono", "stereo", "3.0", "quad", "5.0", "5.1", "6.1", "7.1"}


def encode_audio(work: Path, k: int, a: dict, ratio: Fraction, lead: float, video_at: float,
                 tmp: Path, audio_codec: str, opus_kbps_per_ch: int) -> tuple[Path, int]:
    """Slow audio stream k by `ratio` and encode it. `lead` is how far its first sample comes
    after the first video frame, already scaled; `video_at` is where that frame sits in the
    output. Audio before the first video frame (negative lead) is trimmed off here,
    sample-accurately, as docs/pipeline.md section 5 step 2 requires.
    Returns the file and the mkvmerge delay in milliseconds."""
    sr = int(a.get("sample_rate") or 0)
    if sr <= 0:
        raise RuntimeError(f"audio {k}: sample rate unknown (stream not probed)")
    out_rate = 48000 if audio_codec == "opus" else sr
    chain = "asetpts=PTS-STARTPTS," + audio_filter(ratio, sr, out_rate)
    if lead < 0:
        chain += f",atrim=start={-lead:.6f},asetpts=PTS-STARTPTS"
        lead = 0.0
    delay_ms = round((video_at + lead) * 1000)
    if audio_codec == "opus" and OPUS_LAYOUTS.get(a.get("channel_layout", "")):
        chain += f",channelmap=channel_layout={OPUS_LAYOUTS[a['channel_layout']]}"
    af = tmp / f"a{k}.mka"
    cmd = ["ffmpeg", "-v", "error", "-nostdin", *PROBE, "-i", str(work), "-map", f"0:a:{k}",
           "-af", chain, "-map_metadata", "-1", "-map_chapters", "-1"]
    if audio_codec == "flac":
        fmt = a.get("sample_fmt", "")
        bits = int(a.get("bits_per_raw_sample") or 0)
        if fmt.startswith("s16") or (0 < bits <= 16):
            cmd += ["-sample_fmt", "s16"]
        else:
            cmd += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
        cmd += ["-c:a", "flac", "-compression_level", "8"]
    else:
        ch = int(a.get("channels") or 2)
        layout = OPUS_LAYOUTS.get(a.get("channel_layout", ""), a.get("channel_layout", ""))
        cmd += ["-c:a", "libopus", "-b:a", f"{opus_kbps_per_ch * ch}k"]
        if layout and layout not in OPUS_VORBIS_LAYOUTS:
            cmd += ["-mapping_family", "255"]
    run(cmd + [str(af)])
    return af, delay_ms


def build(src: Path, probe: dict, tmp: Path, target: Fraction, audio_codec: str,
          opus_kbps_per_ch: int) -> Path:
    """Write the restored file into `tmp` and return its path (unverified)."""
    ratio = SOURCE_FPS / target
    r = f"{ratio.numerator}/{ratio.denominator}"
    work = working_file(src, probe, tmp)
    wprobe = probe if work == src else ffprobe(work)
    vids, auds = video_streams(wprobe), audio_streams(wprobe)
    tracks = mkv_identify(work)["tracks"]
    vt = [t for t in tracks if t["type"] == "video"]
    at = [t for t in tracks if t["type"] == "audio"]
    stids = [t["id"] for t in tracks if t["type"] == "subtitles"]
    # ffprobe's streams and mkvmerge's tracks of one type are both in file order: the k-th of
    # one is the k-th of the other.
    if len(vt) != len(vids) or len(at) != len(auds):
        raise RuntimeError(f"ffprobe sees {len(vids)} video / {len(auds)} audio streams, "
                           f"mkvmerge {len(vt)} / {len(at)}")

    # ---- pass 1: video (stretched, bitstream rewritten), subtitles, chapters, attachments, tags
    rewritten = {}                                           # mkvmerge tid -> intermediate file
    for k, (t, v) in enumerate(zip(vt, vids)):
        path = rewrite_video_bitstream(work, k, v.get("codec_name", ""), target, tmp)
        if path is not None:
            rewritten[t["id"]] = (path, t.get("properties", {}))
    vtids = [t["id"] for t in vt]
    kept = [tid for tid in vtids if tid not in rewritten]
    vmkv = tmp / "video.mkv"
    cmd = ["mkvmerge", "-q", "-o", str(vmkv)]
    if wprobe.get("chapters"):
        cmd += ["--chapter-sync", f"0,{r}"]
    cmd += ["--video-tracks", ",".join(map(str, kept))] if kept else ["--no-video"]
    cmd += ["--no-audio"]
    for tid in kept + stids:
        cmd += ["--sync", f"{tid}:0,{r}"]
    cmd += [str(work)]
    fileno = {}
    for i, (tid, (path, props)) in enumerate(rewritten.items(), start=1):
        cmd += track_options(props, video=True) + ["--no-chapters", "--sync", f"0:0,{r}", str(path)]
        fileno[tid] = i
    order = [f"{fileno[tid]}:0" if tid in fileno else f"0:{tid}" for tid in vtids]
    order += [f"0:{tid}" for tid in stids]
    run_mkvmerge(cmd[:1] + ["--track-order", ",".join(order)] + cmd[1:])

    # ---- pass 2: add the audio, each track placed relative to where the video landed
    v_start = packet_span(work, "v:0")[0]
    w_start = packet_span(vmkv, "v:0")[0]
    vtracks = mkv_identify(vmkv)["tracks"]
    out = tmp / "out.mkv"
    cmd = ["mkvmerge", "-q", "-o", str(out), str(vmkv)]
    order = [f"0:{t['id']}" for t in vtracks if t["type"] == "video"]
    for k, (a, t) in enumerate(zip(auds, at)):
        lead = (first_decoded_pts(work, f"a:{k}") - v_start) * float(ratio)
        af, delay_ms = encode_audio(work, k, a, ratio, lead, w_start, tmp, audio_codec, opus_kbps_per_ch)
        if delay_ms:
            cmd += ["--sync", f"0:{delay_ms}"]
        cmd += track_options(t.get("properties", {}), video=False) + ["--no-chapters", str(af)]
        order.append(f"{k + 1}:0")
    order += [f"0:{t['id']}" for t in vtracks if t["type"] == "subtitles"]
    run_mkvmerge(cmd[:1] + ["--track-order", ",".join(order)] + cmd[1:])
    return out


def publish_sidecars(sidecars: list[tuple[Path, Path]], tmp: Path, ratio: Fraction) -> list[Path]:
    """Retime each (source, destination) sidecar into `tmp` and verify it; raises on the first
    problem. Returns the staged files, in the same order, ready to be moved into place."""
    staged = []
    for sc, _ in sidecars:
        st = tmp / sc.name
        retime_sidecar(sc, st, ratio)
        problem = verify_sidecar(sc, st, ratio)
        if problem:
            raise RuntimeError(f"sidecar verification failed: {problem}")
        staged.append(st)
    return staged


def restore_one(src: Path, dst: Path, target: Fraction, audio_codec: str,
                opus_kbps_per_ch: int, dry_run: bool) -> str:
    ratio = SOURCE_FPS / target                              # duration factor, 25025/24000
    out_dir = dst.parent
    probe = ffprobe(src)
    vids = video_streams(probe)
    if not vids:
        return "skipped: no video stream"
    fps = stream_fps(vids[0])
    if fps is None or abs(float(fps) - 25.0) > 0.01:
        return f"skipped: video is {fps} fps, not 25"
    sidecars = [(sc, out_dir / sc.name) for sc in sidecars_for(src)]
    pending = [(sc, d) for sc, d in sidecars if not d.exists()]
    if dst.exists() and not pending:
        return f"skipped: {dst} already exists"
    if dry_run:
        what = [] if dst.exists() else [str(dst)]
        return "would write " + ", ".join(what + [str(d) for _, d in pending])

    if out_dir.resolve() == src.parent.resolve():
        raise RuntimeError(f"output directory {out_dir} is the input's own directory: outputs and "
                           f"sidecars would land on the originals; choose another --out-dir")
    created = []                                             # directories this run makes, leaf first
    d = out_dir
    while not d.exists():
        created.append(d)
        d = d.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=TMP_PREFIX, dir=out_dir) as tmpd:
            tmp = Path(tmpd)
            out = None
            if not dst.exists():
                out = build(src, probe, tmp, target, audio_codec, opus_kbps_per_ch)
                problems = verify(src, out, target, quiet=False)
                if problems:
                    raise RuntimeError("verification failed: " + "; ".join(problems))
            staged = publish_sidecars(pending, tmp, ratio)
            # Everything is built and verified; publish sidecars first and the MKV last, so an
            # interruption here leaves no MKV and a re-run redoes it.
            for st, (_, d) in zip(staged, pending):
                os.replace(st, d)
                print(f"  sidecar retimed: {d.name}")
            if out is not None:
                os.replace(out, dst)
    except BaseException:
        # a failed file leaves nothing behind, not even the directories made for it
        for d in created:
            if not d.exists() or any(d.iterdir()):
                break
            d.rmdir()
        raise
    if out is None:
        return f"skipped: {dst} already exists (missing sidecars added)"
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
    if not vos or len(vos) != len(vrs):
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
            v0, w0 = o_first, r_first                         # every offset is measured from here
        elif abs((r_first - w0) - (o_first - v0) * rf) > 0.05:
            problems.append(f"{tag}: starts {r_first - w0:+.3f}s from video 0, "
                            f"expected {(o_first - v0) * rf:+.3f}s")
        if vn != wn:
            problems.append(f"{tag}: frame count changed: {vn} -> {wn}")
        say(f"{tag} frames: {vn} -> {wn}")
        vdur_o = o_last - o_first + 1 / float(SOURCE_FPS)
        vdur_r = r_last - r_first + 1 / float(target)
        if abs(vdur_r - vdur_o * rf) > 0.05:
            problems.append(f"{tag}: duration {vdur_r:.3f}s, expected {vdur_o * rf:.3f}s")
        say(f"{tag} duration: {vdur_o:.3f}s -> {vdur_r:.3f}s (x{vdur_r / vdur_o:.6f}, expected x{rf:.6f})")

    # Timeline: the output starts at 0 (within TIMELINE_SLACK), no further before its first
    # video frame than the input did (scaled); audio trimmed off before the video can only bring
    # the start closer. It catches a timeline carried over from a capture's arbitrary clock.
    in_start = float(po["format"].get("start_time") or 0.0)
    out_start = float(pr["format"].get("start_time") or 0.0)
    lead_in = max(0.0, (v0 - in_start) * rf)
    if not (-0.05 <= w0 - out_start <= lead_in + 0.05) or out_start > lead_in + TIMELINE_SLACK:
        problems.append(f"timeline: output starts at {out_start:.3f}s with its first video frame at "
                        f"{w0:.3f}s, expected a start within {lead_in:.3f}s of 0")

    ao, ar = audio_streams(po), audio_streams(pr)
    if len(ao) != len(ar):
        problems.append(f"audio track count changed: {len(ao)} -> {len(ar)}")
    for k in range(min(len(ao), len(ar))):
        a0, _, a1, _ = packet_span(orig, f"a:{k}")
        b0, _, b1, _ = packet_span(restored, f"a:{k}")
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
        pitch, why = measure_pitch_ratio(orig, restored, k, a0, a1, lambda t: w0 + (t - v0) * rf)
        exp_c = 1200 * math.log2(1 / rf)
        if pitch is None:
            say(f"audio {k}: pitch not measurable ({why})")
        else:
            cents = 1200 * math.log2(pitch)
            say(f"audio {k}: pitch shift {cents:+.1f} cents (expected {exp_c:+.1f})")
            if abs(cents - exp_c) > 8:
                problems.append(f"audio {k}: pitch shift {cents:+.1f} cents, expected {exp_c:+.1f}")

    so, sr = subtitle_streams(po), subtitle_streams(pr)
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

    # Chapters: every edition, every chapter's start and end, relative to the first video frame.
    eo, er = chapter_editions(orig, po), chapter_editions(restored, pr)
    if len(eo) != len(er):
        problems.append(f"chapter edition count changed: {len(eo)} -> {len(er)}")
    for n, (a, b) in enumerate(zip(eo, er)):
        tag = "chapter" if len(eo) == 1 else f"edition {n} chapter"
        if "matroska" in po["format"]["format_name"] and a["default"] != b["default"]:
            problems.append(f"edition {n}: default flag changed: {a['default']} -> {b['default']}")
        if len(a["chapters"]) != len(b["chapters"]):
            problems.append(f"edition {n}: chapter count changed: {len(a['chapters'])} -> {len(b['chapters'])}")
        for i, ((s0, s1), (t0, t1)) in enumerate(zip(a["chapters"], b["chapters"])):
            for x, y, what in ((s0, t0, "start"), (s1, t1, "end")):
                if x is None or y is None:
                    if (x is None) != (y is None):
                        problems.append(f"{tag} {i}: {what} {'lost' if y is None else 'added'}")
                    continue
                exp = w0 + (x - v0) * rf
                if abs(y - exp) > 0.01:
                    problems.append(f"{tag} {i}: {what} {y:.3f}s, expected {exp:.3f}s")
    if eo:
        say(f"chapters: {sum(len(e['chapters']) for e in eo)} in {len(eo)} edition(s) retimed")

    say("PASS" if not problems else "FAIL: " + "; ".join(problems))
    return problems


# ---------------------------------------------------------------- CLI

def doctor() -> int:
    ok = True
    for tool in ("ffmpeg", "ffprobe", "mkvmerge", "mkvextract"):
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
        for bsf in ("h264_metadata", "hevc_metadata", "mpeg2_metadata", "filter_units", "trace_headers"):
            print(f"ffmpeg bsf {bsf:27} {'ok' if bsf in bsfs else 'MISSING'}")
            ok &= bsf in bsfs
        encs = capture(["ffmpeg", "-v", "error", "-encoders"])
        for enc, needed in (("flac", True), ("libopus", False)):
            have = re.search(rf"^\s*A\S*\s+{enc}\s", encs, re.M) is not None
            print(f"ffmpeg encoder {enc:23} {'ok' if have else 'MISSING' if needed else 'missing (only for --audio-codec opus)'}")
            ok &= have or not needed
    if shutil.which("mkvmerge"):
        h = capture(["mkvmerge", "--help"])
        for opt in ("--sync", "--chapter-sync", "--track-order", "--display-dimensions", "--no-chapters"):
            print(f"mkvmerge {opt:29} {'ok' if opt in h else 'MISSING'}")
            ok &= opt in h
    print("OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def collect(paths: list[str], exclude: Path | None) -> tuple[list[Path], list[str]]:
    """Input files (absolute, symlinks kept as named) and the paths that were not found.
    Directories are searched recursively, never descending into an output directory
    (`restored/`, or `exclude` given by --out-dir) or a temporary directory of this tool."""
    files, missing = [], []
    for p in map(Path, paths):
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                parts = f.relative_to(p).parts[:-1]
                if (f.is_file() and f.suffix.lower() in VIDEO_EXTS
                        and OUT_DIR_NAME not in parts
                        and not any(x.startswith(TMP_PREFIX) for x in parts)
                        and not (exclude and f.absolute().is_relative_to(exclude))):
                    files.append(f)
        elif p.is_file():
            files.append(p)
        else:
            missing.append(str(p))
    return [f.absolute() for f in files], missing


def fps_arg(s: str) -> Fraction:
    try:
        t = Fraction(s)
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError(f"not a frame rate: {s!r}")
    if not 0 < t < SOURCE_FPS:
        raise argparse.ArgumentTypeError(f"must be between 0 and 25 (exclusive): {s}")
    return t


def _terminate(signum, frame):                               # SIGTERM/SIGHUP take Ctrl-C's cleanup path
    raise SystemExit(128 + signum)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    r = sub.add_parser("restore")
    r.add_argument("paths", nargs="+")
    r.add_argument("--target-fps", type=fps_arg, default=Fraction(24000, 1001),
                   help="original frame rate (default 24000/1001)")
    r.add_argument("--audio-codec", choices=("flac", "opus"), default="flac")
    r.add_argument("--opus-kbps-per-channel", type=int, default=96)
    r.add_argument("--out-dir", help=f"output directory (default: <input dir>/{OUT_DIR_NAME})")
    r.add_argument("--dry-run", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("original")
    v.add_argument("restored")
    v.add_argument("--target-fps", type=fps_arg, default=Fraction(24000, 1001))
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)                 # terminal closed, SSH dropped

    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "verify":
        for p in (args.original, args.restored):
            if not Path(p).is_file():
                print(f"not found: {p}", file=sys.stderr)
                return 2
        return 1 if verify(Path(args.original), Path(args.restored), args.target_fps) else 0

    out_root = Path(args.out_dir).absolute() if args.out_dir else None
    files, missing = collect(args.paths, out_root)
    results = {"restored": 0, "planned": 0, "skipped": 0, "failed": 0}
    for m in missing:
        print(f"{m}\n  FAILED: not found")
        results["failed"] += 1
    dests = {f: (out_root or f.parent / OUT_DIR_NAME) / (f.stem + ".mkv") for f in files}
    claimants: dict[Path, list[Path]] = {}
    for f, d in dests.items():
        claimants.setdefault(d, []).append(f)
    for f in files:
        print(f"{f}")
        others = [str(o) for o in claimants[dests[f]] if o != f]
        try:
            if others:
                raise RuntimeError(f"output {dests[f]} would also be written for {', '.join(others)}; "
                                   f"restore them separately with different --out-dir")
            msg = restore_one(f, dests[f], args.target_fps, args.audio_codec,
                              args.opus_kbps_per_channel, args.dry_run)
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
