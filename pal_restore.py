#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26"]
# ///
"""Undo PAL speedup: restore 23.976 fps material that was played back at 25 fps.

Every original frame is still present in a PAL-speedup release; only the timing and the
audio were changed. This tool:

  * relabels the video timing 25 -> 24000/1001 without re-encoding (video is untouched),
  * resamples every audio track by exactly 24000/25025 (restores both speed and pitch),
  * scales embedded subtitle and chapter times by 25025/24000,
  * scales sidecar subtitle files (.srt/.ass/.ssa/.vtt) next to the input,
  * verifies frame count, durations, A/V offset, subtitle/chapter timing and pitch.

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


def packet_span(path: Path, selector: str) -> tuple[float, float, int]:
    """(first pts, end of last packet, packet count) for one stream, from demuxed packets."""
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
        return (math.nan, math.nan, 0)
    return (min(pts), max(ends), len(pts))


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

    ident = mkv_identify(src)
    mkv_tracks = ident["tracks"]
    vtids = [t["id"] for t in mkv_tracks if t["type"] == "video"]
    stids = [t["id"] for t in mkv_tracks if t["type"] == "subtitles"]
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pal-restore-", dir=out_dir) as tmpd:
        tmp = Path(tmpd)
        # ---- audio
        # Each track is written starting at 0 and placed with an explicit delay relative to
        # the video start, scaled by the ratio, so existing A/V offsets survive the retime.
        v_start = float(vids[0].get("start_time") or 0.0)
        afiles = []
        for k, a in enumerate(audio_streams(probe)):
            sr = int(a["sample_rate"])
            out_rate = 48000 if audio_codec == "opus" else sr
            a_start = first_decoded_pts(src, f"a:{k}")
            delay = (a_start - v_start) * float(ratio)
            chain = "asetpts=PTS-STARTPTS," + audio_filter(ratio, sr, out_rate)
            if delay < 0:
                # Audio starts before the video: trim it here, sample-accurately. (A negative
                # mkvmerge --sync can only drop whole codec blocks.)
                chain += f",atrim=start={-delay:.6f},asetpts=PTS-STARTPTS"
                delay_ms = 0
            else:
                delay_ms = round(delay * 1000)
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
                cmd += ["-c:a", "libopus", "-b:a", f"{opus_kbps_per_ch * ch}k"]
            cmd += [str(af)]
            run(cmd)
            tags = a.get("tags", {})
            afiles.append((af, tags.get("language"), tags.get("title"),
                           bool(a.get("disposition", {}).get("default")), delay_ms))

        # ---- video (optionally fix the MPEG-2 sequence-header frame rate)
        video_src, video_tid_map = src, {tid: tid for tid in vtids}
        mpeg2 = vids[0].get("codec_name") == "mpeg2video"
        extra_inputs = []
        if mpeg2 and len(vtids) == 1:
            es = tmp / "v.m2v"
            run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-map", "0:v:0", "-c", "copy",
                 "-bsf:v", f"mpeg2_metadata=frame_rate={target.numerator}/{target.denominator}",
                 "-f", "mpeg2video", str(es)])
            extra_inputs.append(es)

        # ---- mux
        r = f"{ratio.numerator}/{ratio.denominator}"
        fps_s = f"{target.numerator}/{target.denominator}p"
        cmd = ["mkvmerge", "-q", "-o", str(tmp / "out.mkv"), "--chapter-sync", f"0,{r}"]
        order = []
        if extra_inputs:                                     # MPEG-2: video from rewritten ES
            cmd += ["--no-video"]
        else:
            for tid in vtids:
                cmd += ["--default-duration", f"{tid}:{fps_s}"]
                codec = next(t for t in mkv_tracks if t["id"] == tid)["codec"]
                if "AVC" in codec or "H.264" in codec:
                    cmd += ["--fix-bitstream-timing-information", f"{tid}:1"]
                order.append(f"0:{tid}")
        cmd += ["--no-audio"]
        for tid in stids:
            cmd += ["--sync", f"{tid}:0,{r}"]
        cmd += [str(src)]
        fileno = 1
        for es in extra_inputs:
            # carry the original track's language/name over to the rewritten stream
            vt = next(t for t in mkv_tracks if t["id"] == vtids[0])["properties"]
            if vt.get("language"):
                cmd += ["--language", f"0:{vt['language']}"]
            if vt.get("track_name"):
                cmd += ["--track-name", f"0:{vt['track_name']}"]
            cmd += ["--default-duration", f"0:{fps_s}", str(es)]
            order.insert(0, f"{fileno}:0")
            fileno += 1
        for af, lang, title, default, delay_ms in afiles:
            if delay_ms:
                cmd += ["--sync", f"0:{delay_ms}"]
            if lang:
                cmd += ["--language", f"0:{lang}"]
            if title:
                cmd += ["--track-name", f"0:{title}"]
            cmd += ["--default-track-flag", f"0:{'yes' if default else 'no'}", str(af)]
            order.append(f"{fileno}:0")
            fileno += 1
        order += [f"0:{tid}" for tid in stids]
        cmd[1:1] = ["--track-order", ",".join(order)]
        rc = subprocess.run(cmd, env=ENV).returncode
        if rc not in (0, 1):                                 # 1 = warnings only
            raise RuntimeError(f"mkvmerge failed with exit code {rc}")

        # ---- verify before publishing
        problems = verify(src, tmp / "out.mkv", target, quiet=False)
        if problems:
            raise RuntimeError("verification failed: " + "; ".join(problems))
        os.replace(tmp / "out.mkv", dst)

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
    vo, vr = video_streams(po)[0], video_streams(pr)[0]
    fr = stream_fps(vr)
    if fr is None or abs(float(fr) - float(target)) > 0.001:
        problems.append(f"restored video reports {fr} fps, expected {target}")
    say(f"video fps: {stream_fps(vo)} -> {fr}")

    v0, v1, vn = packet_span(orig, "v:0")
    w0, w1, wn = packet_span(restored, "v:0")
    if vn != wn:
        problems.append(f"video frame count changed: {vn} -> {wn}")
    say(f"video frames: {vn} -> {wn}")
    vdur_o, vdur_r = vn / 25.0, wn / float(target)
    if abs(vdur_r - vdur_o * rf) > 0.05:
        problems.append(f"video duration {vdur_r:.3f}s, expected {vdur_o * rf:.3f}s")
    say(f"video duration: {vdur_o:.3f}s -> {vdur_r:.3f}s (x{vdur_r / vdur_o:.6f}, expected x{rf:.6f})")

    ao, ar = audio_streams(po), audio_streams(pr)
    if len(ao) != len(ar):
        problems.append(f"audio track count changed: {len(ao)} -> {len(ar)}")
    for k in range(min(len(ao), len(ar))):
        a0, a1, _ = packet_span(orig, f"a:{k}")
        b0, b1, _ = packet_span(restored, f"a:{k}")
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
    for k in range(min(len(so), len(sr))):
        s0, s1, n0 = packet_span(orig, f"s:{k}")
        t0, t1, n1 = packet_span(restored, f"s:{k}")
        if n0 != n1:
            problems.append(f"subtitle {k}: event count {n0} -> {n1}")
        if n0 and (abs(t0 - s0 * rf) > 0.01 or abs(t1 - s1 * rf) > 0.01):
            problems.append(f"subtitle {k}: first/last {t0:.3f}/{t1:.3f}s, expected {s0 * rf:.3f}/{s1 * rf:.3f}s")
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
        bsfs = capture(["ffmpeg", "-v", "error", "-bsfs"])
        print(f"mpeg2_metadata bsf {'ok' if 'mpeg2_metadata' in bsfs else 'MISSING'}")
        ok &= "mpeg2_metadata" in bsfs
    if shutil.which("mkvmerge"):
        h = capture(["mkvmerge", "--help"])
        for opt in ("--chapter-sync", "--default-duration", "--fix-bitstream-timing-information"):
            print(f"mkvmerge {opt:36} {'ok' if opt in h else 'MISSING'}")
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
