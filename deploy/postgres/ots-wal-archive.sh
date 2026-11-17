#!/bin/bash
# PostgreSQL archive_command for OTS (M04 section 1):  ots-wal-archive.sh %p %f
#
#   $1 = %p  path of the WAL segment, relative to PGDATA (PostgreSQL runs us from there)
#   $2 = %f  the segment's file name
#
# Environment (all have a default except the key file):
#   OTS_WAL_LOCAL  plaintext, zstd-compressed copy kept on this machine for point-in-time restore tests
#   OTS_WAL_SPOOL  zstd + age encrypted copy that the worker ships to SharePoint and then deletes
#   OTS_AGE_PUB    file holding the age PUBLIC key(s); the private key never exists on this server
#   OTS_DATA_DIR   the disk the two folders live on (for the free-space guard)
#   OTS_DISK_FREE_MIN_PERCENT  refuse to archive below this much free space (default 5)
#
# No network calls. Exit 0 only when BOTH copies are safely on disk, otherwise PostgreSQL keeps the
# segment in pg_wal and tries again, so a failure here is never a lost segment.
set -euo pipefail
umask 077

WAL_LOCAL="${OTS_WAL_LOCAL:-/srv/data/wal-local}"
WAL_SPOOL="${OTS_WAL_SPOOL:-/srv/data/wal-spool}"
AGE_PUB="${OTS_AGE_PUB:-/etc/ots/backup.pub}"
DATA_DIR="${OTS_DATA_DIR:-/srv/data}"
MIN_FREE="${OTS_DISK_FREE_MIN_PERCENT:-5}"

fail() { echo "ots-wal-archive: $*" >&2; exit 1; }

[ "$#" -eq 2 ] || fail "usage: ots-wal-archive.sh %p %f"
SRC="$1"
NAME="$2"

# A WAL file name is 24 hex digits, optionally followed by .partial, or a .history / .backup label.
# Anything else (a slash, "..") must never become part of a path we write.
case "$NAME" in
  *[!0-9A-Za-z._-]* | "" | .* ) fail "refusing unsafe file name: $NAME" ;;
esac
[ -f "$SRC" ] || fail "segment not found: $SRC"
[ -r "$AGE_PUB" ] || fail "age public key file not readable: $AGE_PUB"
command -v zstd >/dev/null || fail "zstd is not installed"
command -v age >/dev/null || fail "age is not installed"
mkdir -p "$WAL_LOCAL" "$WAL_SPOOL/.shipped"

# --- disk guard: PostgreSQL will retain WAL in pg_wal and retry; the alert has already fired at 75% of the cap
total_kb=$(df -Pk "$DATA_DIR" | awk 'NR==2 {print $2}')
avail_kb=$(df -Pk "$DATA_DIR" | awk 'NR==2 {print $4}')
free_percent=$(( avail_kb * 100 / total_kb ))
[ "$free_percent" -ge "$MIN_FREE" ] || fail "only ${free_percent}% free on $DATA_DIR (minimum ${MIN_FREE}%)"

tmp_local="" ; tmp_spool=""
cleanup() { rm -f -- "$tmp_local" "$tmp_spool"; }
trap cleanup EXIT

flush() { sync "$1" 2>/dev/null || sync; }   # GNU sync takes paths; BSD/macOS only has the global one

# sha256sum on Linux (the database image), shasum on macOS (development)
if command -v sha256sum >/dev/null; then hash256() { sha256sum "$@"; }; else hash256() { shasum -a 256 "$@"; }; fi
sha_of_file()   { hash256 -- "$1" | cut -d' ' -f1; }
sha_of_stream() { hash256 | cut -d' ' -f1; }

# --- 1. local plaintext copy -------------------------------------------------------------------------------
target="$WAL_LOCAL/$NAME.zst"
if [ -e "$target" ]; then
  # PostgreSQL may call us again for a segment we already stored (a crash between our exit and its bookkeeping).
  # Identical content is fine; different content is a real problem and we never overwrite.
  existing=$(zstd -dc -- "$target" | sha_of_stream) || fail "existing copy is unreadable: $target"
  [ "$existing" = "$(sha_of_file "$SRC")" ] || fail "$target already exists with different content; not overwriting"
else
  tmp_local="$target.tmp.$$"
  zstd -q -3 -c -- "$SRC" > "$tmp_local"
  flush "$tmp_local"
  mv -- "$tmp_local" "$target"
  tmp_local=""
  flush "$WAL_LOCAL"
fi

# --- 2. encrypted spool copy ---------------------------------------------------------------------------------
target="$WAL_SPOOL/$NAME.zst.age"
if [ -e "$target" ] || [ -e "$WAL_SPOOL/.shipped/$NAME" ]; then
  exit 0   # already queued for shipping, or already shipped
fi
tmp_spool="$target.tmp.$$"
zstd -q -3 -c -- "$SRC" | age -R "$AGE_PUB" > "$tmp_spool"
flush "$tmp_spool"
mv -- "$tmp_spool" "$target"
tmp_spool=""
flush "$WAL_SPOOL"
exit 0
