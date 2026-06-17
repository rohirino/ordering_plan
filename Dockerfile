# 軽量なPython3.13のイメージをベースにする
FROM python:3.13-slim

# ログをリアルタイムで見られるようにする設定
ENV PYTHONUNBUFFERED=1

# コンテナ内の作業部屋を決める
WORKDIR /app

# ライブラリをインストール
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 残りのプログラム一式をコンテナ内にコピー
COPY . /app/

# 8000番ポートを開放
EXPOSE 8000

# サーバー起動コマンド（外部アクセス受付用）
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]