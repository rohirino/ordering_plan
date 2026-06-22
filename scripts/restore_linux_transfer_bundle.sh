#!/usr/bin/env bash
set -euo pipefail

# Restore a database dump created by create_linux_transfer_bundle.sh.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DUMP_PATH="${1:-}"

if [[ -z "$DUMP_PATH" || ! -f "$DUMP_PATH" ]]; then
  echo "使い方: ./scripts/restore_linux_transfer_bundle.sh ../postgres.dump" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が見つかりません。Python 3 をインストールしてください。" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose が利用できません。Docker をインストールして起動してください。" >&2
  exit 1
fi

cd "$ROOT_DIR"
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

POSTGRES_DB="${POSTGRES_DB:-ordering_plan}"
POSTGRES_USER="${POSTGRES_USER:-ordering_plan}"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  echo ".env を作成しました。必要に応じて POSTGRES_PASSWORD を変更してください。"
fi

echo "PostgreSQL を起動しています..."
docker compose up -d db

echo "PostgreSQL の準備を待っています..."
until docker compose exec -T db pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
  sleep 2
done

TABLE_COUNT="$(docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';")"
if [[ "$TABLE_COUNT" != "0" ]]; then
  echo "復元先の PostgreSQL には既に ${TABLE_COUNT} 個のテーブルがあります。" >&2
  echo "既存データを守るため、復元を中止しました。空の Docker ボリュームで実行してください。" >&2
  exit 1
fi

echo "PostgreSQL データを復元しています..."
docker compose exec -T db pg_restore \
  --no-owner \
  --no-privileges \
  --username="$POSTGRES_USER" \
  --dbname="$POSTGRES_DB" < "$DUMP_PATH"

if [[ ! -x "$ROOT_DIR/venv/bin/python" ]]; then
  echo "Linux 用 Python 仮想環境を作成しています..."
  python3 -m venv "$ROOT_DIR/venv"
fi

echo "Python パッケージをインストールしています..."
"$ROOT_DIR/venv/bin/python" -m pip install --upgrade pip
"$ROOT_DIR/venv/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"

echo "Django のマイグレーション状態を確認しています..."
set -a
source "$ROOT_DIR/.env"
set +a
"$ROOT_DIR/venv/bin/python" manage.py migrate
"$ROOT_DIR/venv/bin/python" manage.py check

echo
echo "復元が完了しました。起動コマンド:"
echo "set -a; source .env; set +a; venv/bin/python manage.py runserver 0.0.0.0:8000"
