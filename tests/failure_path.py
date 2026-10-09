"""A file that fails leaves nothing behind (docs/pipeline.md section 1), and the MKV and its
sidecars are published as one unit.

Loads pal_restore.py and restores tests fixture h264.mkv (which has four sidecars):
  1. verify() forced to fail, fresh output directory: the directory does not exist afterwards;
  2. verify() forced to fail, nested new output directory (deep-out/a/b): no level remains;
     and an existing directory holding another file: only that file remains;
  3. sidecar verification forced to fail after the MKV verified: nothing is published;
  4. a real restore, then one sidecar output deleted and the restore re-run: only that sidecar
     is written again, the MKV is not rebuilt (its modification time is unchanged).

    uv run --with numpy python tests/failure_path.py FIXTURE_DIR SCRATCH_DIR
"""
import importlib.util
import sys
from fractions import Fraction
from pathlib import Path

here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pal_restore", here.parent / "pal_restore.py")
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)
real_verify, real_verify_sidecar = pr.verify, pr.verify_sidecar
T = Fraction(24000, 1001)

fixtures, scratch = Path(sys.argv[1]), Path(sys.argv[2])
src = fixtures / "h264.mkv"
ok = True


def check(cond: bool, what: str) -> None:
    global ok
    print(("PASS " if cond else "FAIL ") + what)
    ok &= cond


def fails(dst: Path) -> bool:
    try:
        pr.restore_one(src, dst, T, "flac", 96, False)
        return False
    except RuntimeError as e:
        print(f"raised as expected: {e}")
        return True


pr.verify = lambda *a, **k: ["forced failure"]
fresh = scratch / "fresh-out"
assert not fresh.exists(), f"{fresh} must not exist before the test"
check(fails(fresh / "h264.mkv") and not fresh.exists(), "fresh dir: nothing left behind")

deep = scratch / "deep-out"
check(fails(deep / "a" / "b" / "h264.mkv") and not deep.exists(), "nested new out-dir: every level removed")

existing = scratch / "existing-out"
existing.mkdir()
(existing / "keep.txt").write_text("unrelated file")
check(fails(existing / "h264.mkv") and sorted(p.name for p in existing.iterdir()) == ["keep.txt"],
      "existing dir: only the unrelated file remains")

pr.verify = real_verify
pr.verify_sidecar = lambda s, d, r: f"{s.name}: forced failure" if s.suffix == ".vtt" else None
unit = scratch / "unit-out"
check(fails(unit / "h264.mkv") and not unit.exists(), "sidecar failure: neither MKV nor any sidecar published")

pr.verify_sidecar = real_verify_sidecar
again = scratch / "rerun-out"
pr.restore_one(src, again / "h264.mkv", T, "flac", 96, False)
names = sorted(p.name for p in again.iterdir())
check(names == ["h264.ass", "h264.en.srt", "h264.en.vtt", "h264.mkv", "h264.srt"], f"full restore wrote {names}")
mtime = (again / "h264.mkv").stat().st_mtime_ns
(again / "h264.en.vtt").unlink()
msg = pr.restore_one(src, again / "h264.mkv", T, "flac", 96, False)
check((again / "h264.en.vtt").exists() and (again / "h264.mkv").stat().st_mtime_ns == mtime,
      f"re-run added the missing sidecar only ({msg})")
check(pr.restore_one(src, again / "h264.mkv", T, "flac", 96, False).startswith("skipped"),
      "third run skips")

sys.exit(0 if ok else 1)
