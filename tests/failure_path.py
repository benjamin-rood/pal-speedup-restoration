"""A file that fails verification leaves nothing behind (docs/pipeline.md section 1).

Loads pal_restore.py, forces verify() to report a problem, restores one fixture into a fresh
output directory, and checks the directory does not exist afterwards; then repeats into an
existing directory holding another file and checks only that file remains.

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
pr.verify = lambda *a, **k: ["forced failure"]

fixtures, scratch = Path(sys.argv[1]), Path(sys.argv[2])
src = fixtures / "h264.mkv"                                  # has sidecars: none may be written
ok = True

fresh = scratch / "fresh-out"
assert not fresh.exists(), f"{fresh} must not exist before the test"
try:
    pr.restore_one(src, fresh, Fraction(24000, 1001), "flac", 96, False)
    print("FAIL fresh dir: restore_one did not raise")
    ok = False
except RuntimeError as e:
    print(f"raised as expected: {e}")
if fresh.exists():
    print(f"FAIL fresh dir: {fresh} left behind: {sorted(p.name for p in fresh.iterdir())}")
    ok = False
else:
    print("PASS fresh dir: nothing left behind")

existing = scratch / "existing-out"
existing.mkdir(exist_ok=True)
(existing / "keep.txt").write_text("unrelated file")
try:
    pr.restore_one(src, existing, Fraction(24000, 1001), "flac", 96, False)
    ok = False
except RuntimeError:
    pass
left = sorted(p.name for p in existing.iterdir())
if left == ["keep.txt"]:
    print("PASS existing dir: only the unrelated file remains")
else:
    print(f"FAIL existing dir: {left}")
    ok = False

sys.exit(0 if ok else 1)
