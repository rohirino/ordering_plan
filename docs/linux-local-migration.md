# ChromeOS Linux へのローカル移行

GitHub を経由せず、アプリ本体と PostgreSQL のデータを一緒に移行する手順です。仮想環境は OS ごとに作り直すため、移行対象には含めません。

## Mac 側: 移行アーカイブを作成

プロジェクトのルートで実行します。

```bash
./scripts/create_linux_transfer_bundle.sh
```

`backups/ordering_plan-linux-transfer-YYYYMMDD-HHMMSS.tar.gz` が作成されます。この 1 ファイルを USB メモリ、共有フォルダなどで Linux 側へコピーします。

このアーカイブには以下を含めます。

- プロジェクト本体（`.git` を含む）
- PostgreSQL の全データ（`postgres.dump`）

以下は含めません。

- `venv` / `.venv`
- Python のキャッシュ
- 過去に出力した帳票・一時データ（`outputs/`）
- `.env`（環境ごとのパスワード設定）

## ChromeOS Linux 側: 展開と復元

Linux で Docker と Python 3 を準備してから、アーカイブを展開します。

```bash
tar -xzf ordering_plan-linux-transfer-YYYYMMDD-HHMMSS.tar.gz
cd ordering_plan-linux-transfer-YYYYMMDD-HHMMSS/ordering_plan
./scripts/restore_linux_transfer_bundle.sh ../postgres.dump
```

復元スクリプトは、空の PostgreSQL データベースにだけ復元します。すでに別のデータが存在する場合は停止するため、誤って上書きしません。

完了後は次で起動します。

```bash
cd ~/ordering_plan-linux-transfer-YYYYMMDD-HHMMSS/ordering_plan
set -a; source .env; set +a
venv/bin/python manage.py runserver 0.0.0.0:8000
```

ChromeOS のブラウザからは通常 `http://penguin.linux.test:8000/`、またはターミナルに表示された URL を開きます。

## 補足

Linux 側で復元後に `.env` の `POSTGRES_PASSWORD` を変更する場合は、`docker-compose.yml` 側の PostgreSQL 初期化設定と合わせる必要があります。まずは移行直後の値で起動・確認し、パスワード変更は別作業として行うのが安全です。
