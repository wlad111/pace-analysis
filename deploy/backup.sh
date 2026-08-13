#!/usr/bin/env bash
#
# Nightly backup of the Pace Analysis database.
#
# Copying pace.db with `cp` while the API is running can capture a torn file:
# SQLite in WAL mode keeps recent pages in pace.db-wal, so a plain copy is a
# database missing its newest transactions. `sqlite3 .backup` takes a proper
# online snapshot instead, consistent and safe against a live writer.
#
# Install on the server:
#   sudo install -m 755 deploy/backup.sh /usr/local/bin/pace-backup
#   sudo crontab -e
#   17 4 * * *  /usr/local/bin/pace-backup >>/var/log/pace-backup.log 2>&1

set -euo pipefail

PROJECT="${PACE_PROJECT:-pace-analysis}"
DEST="${PACE_BACKUP_DIR:-/var/backups/pace-analysis}"
KEEP_DAYS="${PACE_BACKUP_KEEP_DAYS:-30}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$DEST"

# The api container carries no sqlite3 binary, so run the snapshot through
# Python, which ships one in its standard library.
docker compose -p "$PROJECT" exec -T api python - <<'PY' > "$DEST/pace-$STAMP.db"
import sqlite3, sys

source = sqlite3.connect("file:/data/pace.db?mode=ro", uri=True)
snapshot = sqlite3.connect(":memory:")
source.backup(snapshot)
sys.stdout.buffer.write(snapshot.serialize())
PY

if [ ! -s "$DEST/pace-$STAMP.db" ]; then
    echo "backup produced an empty file, keeping the previous ones" >&2
    rm -f "$DEST/pace-$STAMP.db"
    exit 1
fi

# Verify the snapshot opens and carries data before trusting it.
python3 - "$DEST/pace-$STAMP.db" <<'PY'
import sqlite3, sys

path = sys.argv[1]
db = sqlite3.connect(path)
sessions = db.execute("SELECT count(*) FROM session").fetchone()[0]
laps = db.execute("SELECT count(*) FROM lap").fetchone()[0]
if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    raise SystemExit(f"{path}: integrity check failed")
print(f"{path}: ok, {sessions} sessions, {laps} laps")
PY

gzip -f "$DEST/pace-$STAMP.db"
find "$DEST" -name 'pace-*.db.gz' -mtime "+$KEEP_DAYS" -delete
echo "kept backups: $(find "$DEST" -name 'pace-*.db.gz' | wc -l)"
