#!/usr/bin/env bash
# Command-line behaviour of pal_restore.py, on small clips made here in the scratch directory
# given as $1 (must not exist). One PASS/FAIL line per check; exit non-zero if any failed.
set -uo pipefail
W=${1:?usage: check_cli.sh SCRATCH_DIR}
[ -e "$W" ] && { echo "$W must not exist"; exit 2; }
P=$(cd "$(dirname "$0")/.." && pwd)/pal_restore.py
mkdir -p "$W"
fail=0
ok() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1: [$2] is false"; fail=1; fi; }
clip() { ffmpeg -v error -y -f lavfi -i "testsrc2=size=320x240:rate=25:duration=${2:-6}" \
           -f lavfi -i "sine=f=440:d=${2:-6}" -c:v libx264 -preset ultrafast -c:a flac "$1"; }

# A missing input is a failure (exit 1), not a silent success.
"$P" restore "$W/nope.mkv" > "$W/missing.log" 2>&1; rc=$?
ok "missing input: exit 1, reported" "[ $rc = 1 ] && grep -q 'FAILED: not found' '$W/missing.log'"

# Two inputs that would write the same output: both refused, nothing written.
mkdir -p "$W/coll"; clip "$W/coll/x.mkv"; clip "$W/coll/x.mp4"
"$P" restore "$W/coll" > "$W/coll.log" 2>&1; rc=$?
ok "output collision: both refused, nothing written" \
  "[ $rc = 1 ] && [ \$(grep -c 'would also be written' '$W/coll.log') = 2 ] && [ ! -e '$W/coll/restored/x.mkv' ]"

# Bad --target-fps and verify on a missing file: a usage error, no traceback.
for v in abc 0 25 -1; do
  "$P" restore --target-fps "$v" "$W/coll" > "$W/fps.log" 2>&1; rc=$?
  ok "--target-fps $v: usage error, no traceback" "[ $rc = 2 ] && ! grep -q Traceback '$W/fps.log'"
done
"$P" verify "$W/nope.mkv" "$W/nope2.mkv" > "$W/verify.log" 2>&1; rc=$?
ok "verify missing file: exit 2, no traceback" "[ $rc = 2 ] && ! grep -q Traceback '$W/verify.log'"

# A symlinked input keeps its own name, directory and sidecars.
mkdir -p "$W/real" "$W/links"; clip "$W/real/target.mkv"
printf '1\n00:00:01,000 --> 00:00:02,000\nlink\n' > "$W/links/shown.srt"
ln -s "$W/real/target.mkv" "$W/links/shown.mkv"
"$P" restore "$W/links/shown.mkv" > "$W/link.log" 2>&1
ok "symlink: output and sidecar under the link's name and directory" \
  "[ -f '$W/links/restored/shown.mkv' ] && [ -f '$W/links/restored/shown.srt' ] && [ ! -e '$W/real/restored' ]"

# --out-dir inside the input tree, and a stale temporary directory: neither is collected.
mkdir -p "$W/tree/pal-restore-stale"; clip "$W/tree/a.mkv"; clip "$W/tree/pal-restore-stale/v0.mkv"
"$P" restore "$W/tree" --out-dir "$W/tree/out" > "$W/tree1.log" 2>&1
"$P" restore "$W/tree" --out-dir "$W/tree/out" > "$W/tree2.log" 2>&1
ok "out-dir inside input tree, stale temp dir: not collected" \
  "tail -1 '$W/tree1.log' | grep -qx '1 restored, 0 failed' && tail -1 '$W/tree2.log' | grep -qx '0 restored, 1 skipped, 0 failed'"

# --out-dir equal to the input's own directory: refused (the sidecar would be its own output).
mkdir -p "$W/same"; clip "$W/same/film.mp4"
printf '1\n00:00:01,000 --> 00:00:02,000\nsame\n' > "$W/same/film.srt"
cp "$W/same/film.srt" "$W/same.srt.orig"
"$P" restore --out-dir "$W/same" "$W/same/film.mp4" > "$W/same.log" 2>&1; rc=$?
ok "out-dir is the input directory: refused, nothing written or changed" \
  "[ $rc = 1 ] && grep -q \"the input's own directory\" '$W/same.log' && [ ! -e '$W/same/film.mkv' ] && cmp -s '$W/same/film.srt' '$W/same.srt.orig'"

# SIGTERM mid-run: exits, and leaves neither output, output directory nor temporary directory.
# The signal is sent as soon as the temporary directory exists, i.e. while building.
mkdir -p "$W/term"; clip "$W/term/long.mkv" 600
"$P" restore "$W/term/long.mkv" > "$W/term.log" 2>&1 &
pid=$!
for _ in $(seq 300); do compgen -G "$W/term/restored/pal-restore-*" > /dev/null && break; sleep 0.05; done
if compgen -G "$W/term/restored/pal-restore-*" > /dev/null && kill -TERM "$pid" 2>/dev/null; then
  wait "$pid"; rc=$?
  ok "SIGTERM: exit 143, nothing left behind" "[ $rc = 143 ] && [ ! -e '$W/term/restored' ]"
else
  echo "FAIL SIGTERM: the restore finished before the signal could be sent"; fail=1
fi
ok "no stray ffmpeg after SIGTERM" "! pgrep -f '$W/term/' > /dev/null"

# SIGHUP (terminal closed, SSH dropped): the same.
"$P" restore "$W/term/long.mkv" > "$W/hup.log" 2>&1 &
pid=$!
for _ in $(seq 300); do compgen -G "$W/term/restored/pal-restore-*" > /dev/null && break; sleep 0.05; done
if compgen -G "$W/term/restored/pal-restore-*" > /dev/null && kill -HUP "$pid" 2>/dev/null; then
  wait "$pid"; rc=$?
  ok "SIGHUP: exit 129, nothing left behind" "[ $rc = 129 ] && [ ! -e '$W/term/restored' ]"
else
  echo "FAIL SIGHUP: the restore finished before the signal could be sent"; fail=1
fi

exit $fail
