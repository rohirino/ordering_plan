import csv
import os
from django.core.management.base import BaseCommand
from inventory.models import Product, SalesHistory
from datetime import datetime

class Command(BaseCommand):
    help = 'JUSTDBの日次販売履歴CSVから重複を回避しつつ高速インポートします'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='CSVファイルのパス')

    def open_csv_with_encoding(self, csv_file_path):
        for encoding in ('cp932', 'utf-8', 'utf-8-sig'):
            try:
                with open(csv_file_path, 'r', encoding=encoding) as f:
                    f.read(1024)
                return open(csv_file_path, 'r', encoding=encoding, newline='')
            except UnicodeDecodeError:
                continue
        return open(csv_file_path, 'r', encoding='utf-8', newline='', errors='replace')

    def handle(self, *args, **options):
        csv_file_path = options['csv_file']
        row_count = 0
        skip_count = 0

        if not os.path.exists(csv_file_path):
            self.stdout.write(self.style.ERROR(f'ファイルが見つかりません: {csv_file_path}'))
            return

        # 高速化のために商品マスタを一括でメモリに読み込む
        self.stdout.write('商品マスタの同期中...')
        product_dict = {p.code: p for p in Product.objects.all()}
        
        # すでにデータベースに登録されている「日次販売履歴ID」をすべて取得（重複を瞬時に見つけるための設定）
        self.stdout.write('登録済みの販売履歴IDをチェック中...')
        existing_ids = set(SalesHistory.objects.values_list('sales_id', flat=True))
        
        insert_list = []
        self.stdout.write('受注履歴のインポートを開始します...')

        with self.open_csv_with_encoding(csv_file_path) as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                # 2行目のフィールドID行（field_xxx）があればスキップ
                if row.get('伝票日付') and row['伝票日付'].startswith('field_'):
                    continue
                
                # CSVの「日次販売履歴ID」を取得して、既にある場合は完全にスキップ（重複防止）
                sales_id = row.get('日次販売履歴ID')
                if not sales_id or sales_id in existing_ids:
                    skip_count += 1
                    continue
                
                # 商品コードがない、または商品マスタにない商品はスキップ
                code = row.get('商品コード')
                if not code or code not in product_dict:
                    continue

                try:
                    # 日付のフォーマット変換
                    raw_date = row['伝票日付'].split()[0]
                    sold_date = datetime.strptime(raw_date, '%Y/%m/%d').date()
                    quantity = int(float(row['販売数']))
                except (ValueError, TypeError, KeyError):
                    continue

                # 保存用オブジェクトを作成
                history = SalesHistory(
                    sales_id=sales_id,
                    product=product_dict[code],
                    sold_date=sold_date,
                    quantity=quantity,
                    customer=row.get('得意先名', '')
                )
                insert_list.append(history)
                row_count += 1
                
                # 1万件たまったら一括保存して進捗を表示
                if len(insert_list) >= 10000:
                    SalesHistory.objects.bulk_create(insert_list)
                    insert_list = []
                    self.stdout.write(f'--- {row_count} 件 インポート完了 (重複スキップ: {skip_count}件) ---')

            # 残りのデータを最後に一括保存
            if insert_list:
                SalesHistory.objects.bulk_create(insert_list)

        self.stdout.write(self.style.SUCCESS(
            f'インポートが完了しました！\n'
            f'新規登録: {row_count}件 / 重複スキップ: {skip_count}件'
        ))