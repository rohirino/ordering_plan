#!/usr/bin/env bash
set -euo pipefail

# Create a portable archive without the host-specific virtual environment.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_NAME="$(basename "$ROOT_DIR")"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$ROOT_DIR/backups"
WORK_DIR="$(mktemp -d)"
BUNDLE_DIR="$WORK_DIR/${PROJECT_NAME}-linux-transfer-${STAMP}"
ARCHIVE_PATH="$BACKUP_DIR/${PROJECT_NAME}-linux-transfer-${STAMP}.tar.gz"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose が利用できません。Docker Desktop を起動してから実行してください。" >&2
  exit 1
fi

cd "$ROOT_DIR"
if ! docker compose ps --status running --services | grep -qx "db"; then
  echo "PostgreSQL を起動します。"
  docker compose up -d db
fi

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

POSTGRES_DB="${POSTGRES_DB:-ordering_plan}"
POSTGRES_USER="${POSTGRES_USER:-ordering_plan}"

mkdir -p "$BACKUP_DIR" "$BUNDLE_DIR"

echo "PostgreSQL をバックアップしています..."
docker compose exec -T db pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" > "$BUNDLE_DIR/postgres.dump"

echo "プロジェクト本体をまとめています..."
tar -C "$(dirname "$ROOT_DIR")" \
  --exclude="${PROJECT_NAME}/venv" \
  --exclude="${PROJECT_NAME}/.venv" \
  --exclude="${PROJECT_NAME}/__pycache__" \
  --exclude="${PROJECT_NAME}/backups" \
  --exclude="${PROJECT_NAME}/outputs" \
  --exclude="${PROJECT_NAME}/.DS_Store" \
  --exclude="${PROJECT_NAME}/.env" \
  -cf - "$PROJECT_NAME" | tar -C "$BUNDLE_DIR" -xf -

cat > "$BUNDLE_DIR/README.txt" <<EOF
Ordering Plan Linux transfer bundle

This archive contains:
- ${PROJECT_NAME}/  Project source (virtual environments and generated output excluded)
- postgres.dump    PostgreSQL data backup in custom pg_dump format

On the Linux machine:
1. Extract this archive.
2. Change into ${PROJECT_NAME}.
3. Run ./scripts/restore_linux_transfer_bundle.sh ../postgres.dump

The restore script creates a Linux virtual environment, starts PostgreSQL with
Docker Compose, restores the database into an empty database, and runs Django
migrations.
EOF

tar -C "$WORK_DIR" -czf "$ARCHIVE_PATH" "$(basename "$BUNDLE_DIR")"

echo
echo "移行アーカイブを作成しました:"
echo "$ARCHIVE_PATH"
echo "Linux 側では展開後、プロジェクト内で restore_linux_transfer_bundle.sh を実行してください。"
