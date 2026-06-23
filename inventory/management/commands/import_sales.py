import os

from django.core.management.base import BaseCommand

from inventory.sales_importer import import_sales_file_path


class Command(BaseCommand):
    help = '売上明細CSVを日付・得意先・商品・区分ごとに集計してインポートします'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='CSVファイルのパス')
        parser.add_argument(
            '--company',
            choices=['IKUJI', 'SELECT'],
            default='IKUJI',
            help='取り込み先の会社',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='DBへ保存せず、取り込み予定件数だけ確認します',
        )

    def handle(self, *args, **options):
        csv_file_path = options['csv_file']

        if not os.path.exists(csv_file_path):
            self.stdout.write(self.style.ERROR(f'ファイルが見つかりません: {csv_file_path}'))
            return

        self.stdout.write('売上明細の集計インポートを開始します...')

        stats = import_sales_file_path(
            csv_file_path,
            current_company=options['company'],
            dry_run=options['dry_run'],
        )

        mode = '確認完了' if options['dry_run'] else '販売履歴インポート完了'
        self.stdout.write(self.style.SUCCESS(
            f"{mode}！ 集計行: {stats['aggregated']}件 / "
            f"新規: {stats['created']}件 / 更新: {stats['updated']}件 / "
            f"商品自動登録: {stats['auto_created_products']}件 / "
            f"共通マスタ利用: {stats['shared_master_products']}件 / "
            f"その他スキップ: {stats['skipped']}件"
        ))
