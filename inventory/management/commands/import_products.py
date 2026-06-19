import os

from django.core.management.base import BaseCommand

from inventory.product_importer import import_product_file_path


class Command(BaseCommand):
    help = '共通商品・A版発注設定CSVからインポートします（文字コード・ヘッダー行自動判別付き）'

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

        self.stdout.write('共通商品・A版発注設定のインポートを開始します（文字コード自動判別中）...')

        stats = import_product_file_path(
            csv_file_path,
            current_company=options['company'],
            dry_run=options['dry_run'],
        )

        mode = '確認完了' if options['dry_run'] else 'マスタインポート完了'
        self.stdout.write(self.style.SUCCESS(
            f"{mode}！ 取込対象: {stats['imported']}件 / "
            f"状態違い重複スキップ: {stats['duplicate_variants']}件 / "
            f"その他スキップ: {stats['skipped']}件"
        ))
