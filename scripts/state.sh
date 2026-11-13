#!/bin/bash
# Encrypted state (the SQLite DB) kept in the orphan branch `state` of this public repo.
#
#   scripts/state.sh pack     data/jobradar.sqlite3 -> state.db.enc  (gzip + AES-256, key: $STATE_KEY)
#   scripts/state.sh unpack   state.db.enc -> data/jobradar.sqlite3
#   scripts/state.sh pull     fetch origin/state and unpack it locally (old DB kept as .bak)
#   scripts/state.sh push     pack and force-push a single commit to origin/state
#
# STATE_KEY comes from the environment or from .env. Without it the state cannot be read.
set -euo pipefail
cd "$(dirname "$0")/.."

DB=data/jobradar.sqlite3
ENC=state.db.enc

if [[ -z "${STATE_KEY:-}" && -f .env ]]; then
  STATE_KEY=$(grep -E '^STATE_KEY=' .env | head -1 | cut -d= -f2- || true)
fi
[[ -n "${STATE_KEY:-}" ]] || { echo "STATE_KEY is not set" >&2; exit 1; }
export STATE_KEY

OSSL=(openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -pass env:STATE_KEY)

pack() {
  [[ -f "$DB" ]] || { echo "no $DB to pack" >&2; exit 1; }
  # consistent snapshot even if the DB has a WAL/journal
  local snap; snap=$(mktemp)
  python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()' "$DB" "$snap"
  gzip -c "$snap" | "${OSSL[@]}" -salt -out "$ENC"
  rm -f "$snap"
  echo "packed $(wc -c < "$ENC" | tr -d ' ') bytes -> $ENC"
}

unpack() {
  [[ -f "$ENC" ]] || { echo "no $ENC to unpack" >&2; exit 1; }
  mkdir -p data
  "${OSSL[@]}" -d -in "$ENC" | gunzip -c > "$DB.tmp"
  python3 -c 'import sqlite3,sys; r=sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0]; sys.exit(r!="ok")' "$DB.tmp" \
    || { echo "decrypted state failed integrity check" >&2; rm -f "$DB.tmp"; exit 1; }
  mv "$DB.tmp" "$DB"
  echo "unpacked -> $DB"
}

case "${1:-}" in
  pack) pack ;;
  unpack) unpack ;;
  pull)
    git fetch -q --depth=1 origin state
    git show FETCH_HEAD:"$ENC" > "$ENC"
    [[ -f "$DB" ]] && cp "$DB" "$DB.bak"
    unpack
    rm -f "$ENC"
    ;;
  push)
    pack
    # one parentless commit with just the encrypted file; the working tree is not touched
    blob=$(git hash-object -w "$ENC")
    tree=$(printf '100644 blob %s\t%s\n' "$blob" "$ENC" | git mktree)
    commit=$(git -c user.name="jobradar-bot" -c user.email="jobradar-bot@users.noreply.github.com" \
      commit-tree "$tree" -m "state $(date -u +%FT%TZ)")
    git push -qf origin "$commit:refs/heads/state"
    rm -f "$ENC"
    echo "pushed encrypted state to origin/state"
    ;;
  *) echo "usage: $0 pack|unpack|pull|push" >&2; exit 2 ;;
esac
