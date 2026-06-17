import csv
import os
from django.core.management.base import BaseCommand
from inventory.models import Product, Inventory

class Command(BaseCommand):
    help = 'JUSTDBの商品マスタCSVからインポートします（文字コード自動判別付き）'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='CSVファイルのパス')

    def open_csv_with_encoding(self, csv_file_path):
        """文字コードを自動判別してファイルオブジェクトを返す関数"""
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

        if not os.path.exists(csv_file_path):
            self.stdout.write(self.style.ERROR(f'ファイルが見つかりません: {csv_file_path}'))
            return

        self.stdout.write('商品マスタのインポートを開始します（文字コード自動判別中）...')

        with self.open_csv_with_encoding(csv_file_path) as f:
            reader = csv.DictReader(f)
            
            for row in reader:
                # 2行目のフィールドID行（field_xxx）をスキップ
                if row['商品コード'] and row['商品コード'].startswith('field_'):
                    continue
                
                if not row['商品コード']:
                    continue

                try:
                    raw_price = row['標準原価']
                    price_val = int(float(raw_price)) if raw_price else 0
                except (ValueError, TypeError):
                    price_val = 0

                # 商品マスタの登録または更新（重複時は上書き）
                product, created = Product.objects.update_or_create(
                    code=row['商品コード'],
                    defaults={
                        'name': row['商品名'],
                        'price': price_val,
                        'supplier': row['仕入先名【仕入先マスタ 一覧】'] or '仕入先未設定',
                    }
                )
                
                # 同時に在庫データの枠も自動作成（初期値: 現在庫0, 安全在庫20）
                Inventory.objects.get_or_create(
                    product=product,
                    defaults={
                        'current_quantity': 0,
                        'safety_stock': 20, 
                    }
                )
                row_count += 1

        self.stdout.write(self.style.SUCCESS(f'マスタインポート完了！ 総件数: {row_count}件'))