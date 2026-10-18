#!/usr/bin/env bash
# Every test suite, from scratch, in one command. Exit status is non-zero if any step fails.
#
#   tests/run_all.sh [SCRATCH_DIR]
#
# SCRATCH_DIR (default: a new directory under $TMPDIR or /tmp, removed afterwards) must not exist
# yet and must not be inside the repository. About 1 GB, a few minutes on 4 cores.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")
if [ -n "${1:-}" ]; then
  S=$1; keep=1
  [ -e "$S" ] && { echo "$S already exists" >&2; exit 2; }
  mkdir -p "$S"
else
  S=$(mktemp -d "${TMPDIR:-/tmp}/pal-restore-tests.XXXXXX"); keep=0
fi
[ "$keep" = 1 ] || trap 'rm -rf "$S"' EXIT
cd "$root"
fail=0
step() {                       # step NAME LOGFILE COMMAND...   (a non-zero exit or a FAIL line fails the run)
  local name=$1 log=$2; shift 2
  if "$@" > "$log" 2>&1 && ! grep -q '^FAIL' "$log"; then
    n=$(grep -c '^PASS' "$log"); if [ "$n" -gt 0 ]; then echo "ok   $name ($n checks passed)"; else echo "ok   $name"; fi
  else
    echo "FAIL $name (log: $log)"; grep -E '^FAIL|Error|error' "$log" | head -10; fail=1
  fi
}
restored_ok() { tail -1 "$1" | grep -qE ', 0 failed$'; }       # "N restored, M skipped, 0 failed"

step "doctor"             "$S/doctor.log"  ./pal_restore.py doctor
step "unit tests"         "$S/units.log"   tests/test_units.py

F=$S/main A=$S/adv
step "main fixtures"      "$S/fix.log"     tests/make_fixtures.sh "$F"
step "main restore"       "$S/restore.log" ./pal_restore.py restore "$F"
restored_ok "$S/restore.log" || { echo "FAIL main restore: not '0 failed': $(tail -1 "$S/restore.log")"; fail=1; }
step "main checks"        "$S/outputs.log" tests/check_outputs.sh "$F"
mkdir -p "$F/fail"
step "failure path"       "$S/failure.log" uv run -q --with numpy python tests/failure_path.py "$F" "$F/fail"

step "adversarial fixtures" "$S/afix.log"  tests/make_adversarial.sh "$A"
./pal_restore.py restore "$A" > "$A/restore.log" 2>&1
restored_ok "$A/restore.log" && echo "ok   adversarial restore ($(tail -1 "$A/restore.log"))" \
  || { echo "FAIL adversarial restore: $(tail -1 "$A/restore.log")"; fail=1; }
step "adversarial checks" "$S/adv.log"     tests/check_adversarial.sh "$A"

step "command line"       "$S/cli.log"     tests/check_cli.sh "$S/cli"

if [ "$fail" = 0 ]; then echo "ALL PASSED"; else echo "FAILED"; fi
[ "$keep" = 0 ] || echo "logs and fixtures kept in $S"
exit "$fail"
