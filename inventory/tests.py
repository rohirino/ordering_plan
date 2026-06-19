import csv
import io
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from inventory.models import (
    ArrivalSchedule,
    Inventory,
    InventoryValuationSnapshot,
    Product,
    ProductVariant,
    InventoryState,
    SalesHistory,
    Warehouse,
    WarehouseInventory,
)
from inventory.views import _get_planning_base_date


class ImportProductsCommandTests(TestCase):
    rows = [
        ['商品マスタ一覧表,2026年06月17日(水)'],
        ['《コード順》'],
        ['コード', 'JANｺｰﾄﾞ(1)', '商品名', '固定原価', '仕入先'],
        ['', '', '', '', ''],
        ['0040003001', '4955303300837', '最初の商品名', '2,512', '000004'],
        ['0040003002', '', '状態違いの商品名', '3,000', '000005'],
        ['0040004001', '', '別の商品', '250', '000006'],
    ]

    def csv_bytes(self, rows=None):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerows(rows or self.rows)
        return buffer.getvalue().encode('cp932')

    def write_csv(self, rows=None):
        tmp = tempfile.NamedTemporaryFile(mode='w', encoding='cp932', newline='', suffix='.csv', delete=False)
        with tmp:
            writer = csv.writer(tmp)
            writer.writerows(rows or self.rows)
        return Path(tmp.name)

    def test_imports_sales_management_master_with_third_row_header(self):
        csv_path = self.write_csv()

        try:
            call_command('import_products', str(csv_path))
        finally:
            csv_path.unlink()

        self.assertEqual(Product.objects.count(), 2)

        product = Product.objects.get(code='0040003')
        self.assertEqual(product.name, '最初の商品名')
        self.assertEqual(product.price, 2512)
        self.assertEqual(product.supplier, '000004')
        self.assertTrue(Inventory.objects.filter(product=product).exists())

        self.assertFalse(Product.objects.filter(code='0040003001').exists())

    def test_dashboard_product_upload_uses_same_import_logic(self):
        uploaded = SimpleUploadedFile(
            'products.csv',
            self.csv_bytes(),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_products_csv'),
            {'current_company': 'IKUJI', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Product.objects.count(), 2)
        self.assertEqual(Product.objects.get(code='0040003').name, '最初の商品名')

    def test_product_template_matches_sales_management_master_shape(self):
        response = self.client.get(reverse('download_csv_template', args=['products']))

        self.assertEqual(response.status_code, 200)
        lines = response.content.decode('cp932').splitlines()
        self.assertEqual(lines[0], '共通商品・A版発注設定一覧表,雛形')
        self.assertEqual(lines[1], '《コード順》')
        self.assertEqual(lines[2].split(',')[:3], ['コード', 'JANｺｰﾄﾞ(1)', '商品名'])
        self.assertTrue(lines[4].split(',')[0].endswith('001'))
        self.assertEqual(len(lines[4].split(',')[0]), 10)

    def test_product_upload_without_discontinued_column_preserves_manual_flag(self):
        Product.objects.create(code='0040003', name='旧商品', owner_company='IKUJI', is_discontinued=True)
        uploaded = SimpleUploadedFile(
            'products.csv',
            self.csv_bytes(),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_products_csv'),
            {'current_company': 'IKUJI', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Product.objects.get(code='0040003').is_discontinued)

    def test_dashboard_sales_upload_aggregates_sales_management_detail(self):
        Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        rows = [
            ['売上明細表,2026年06月17日(水)', '', '', '', '', '', '', '', '', ''],
            ['【日付期間：2026年 6月 1日 ～ 2026年 6月 1日】', '', '', '', '', '', '', '', '', ''],
            ['コード', '得意先名', '伝票日付', '区分', '仕入先', 'コード', '商品名', '数量', '税抜金額', '粗利金額'],
            ['012345', 'サンプル得意先', '2026年 6月 1日', '売上', '000646', '6460010001', 'グリップ シート', '12', '13,200', '5,232'],
            ['012345', 'サンプル得意先', '2026年 6月 1日', '売上', '000646', '6460010002', 'グリップ シート 状態違い', '3', '3,300', '1,308'],
            ['012345', 'サンプル得意先', '2026年 6月 1日', '返品', '000646', '6460010001', 'グリップ シート', '-1', '-1,100', '-436'],
        ]
        uploaded = SimpleUploadedFile(
            'sales.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_sales_csv'),
            {'current_company': 'IKUJI', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(SalesHistory.objects.count(), 2)

        sale = SalesHistory.objects.get(product__code='6460010', sales_category='売上')
        self.assertEqual(sale.customer, '012345')
        self.assertEqual(sale.quantity, 15)
        self.assertEqual(sale.tax_excluded_amount, 16500)
        self.assertEqual(sale.gross_profit_amount, 6540)

        return_sale = SalesHistory.objects.get(product__code='6460010', sales_category='返品')
        self.assertEqual(return_sale.quantity, -1)

    def test_sales_history_dashboard_lists_and_filters_sales(self):
        product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        other = Product.objects.create(code='9990001', name='別商品', owner_company='IKUJI')
        SalesHistory.objects.create(
            product=product,
            sold_date='2026-06-01',
            quantity=5,
            customer='012345',
            sales_category='売上',
            tax_excluded_amount=5000,
            gross_profit_amount=2000,
            company='IKUJI',
        )
        SalesHistory.objects.create(
            product=other,
            sold_date='2026-06-01',
            quantity=3,
            customer='999999',
            sales_category='売上',
            tax_excluded_amount=3000,
            gross_profit_amount=1000,
            company='IKUJI',
        )

        response = self.client.get(
            reverse('sales_history_dashboard'),
            {'current_company': 'IKUJI', 'product_prefix': '646'},
        )
        body = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('日次販売履歴Dashboard', body)
        self.assertIn('6460010', body)
        self.assertNotIn('9990001', body)
        self.assertIn('5,000', body)

    def test_sales_template_matches_sales_management_detail_shape(self):
        response = self.client.get(reverse('download_csv_template', args=['sales']))

        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode('cp932'))))
        self.assertEqual(rows[0][0], '売上明細表,雛形')
        self.assertEqual(rows[1][0], '【日付期間：2026年 6月 1日 ～ 2026年 6月 1日】')
        self.assertEqual(rows[2], ['コード', '得意先名', '伝票日付', '区分', '仕入先', 'コード', '商品名', '数量', '税抜金額', '粗利金額'])
        self.assertEqual(len(rows[3][5]), 10)

    def test_inventory_upload_normalizes_code_and_replaces_product_warehouse_stock(self):
        product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        untouched = Product.objects.create(code='6460011', name='未更新商品', owner_company='IKUJI')
        old_warehouse = Warehouse.objects.create(name='旧倉庫', owner_company='IKUJI')
        Inventory.objects.create(product=product, current_quantity=99)
        Inventory.objects.create(product=untouched, current_quantity=7)
        WarehouseInventory.objects.create(product=product, warehouse=old_warehouse, quantity=99)

        rows = [
            ['商品コード', 'ニチイク在庫', '岸和田在庫'],
            ['6460010001', '10', '2'],
            ['9999999001', '5', '1'],
        ]
        uploaded = SimpleUploadedFile(
            'inventory.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_inventory_csv'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(WarehouseInventory.objects.filter(product=product, warehouse=old_warehouse).exists())
        self.assertEqual(Inventory.objects.get(product=product).current_quantity, 12)
        self.assertEqual(Inventory.objects.get(product=product).inventory_date.isoformat(), '2026-05-31')
        self.assertEqual(Inventory.objects.get(product=untouched).current_quantity, 7)
        self.assertEqual(WarehouseInventory.objects.filter(product=product).count(), 2)

    def test_planning_base_date_uses_day_after_latest_inventory_date(self):
        product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        Inventory.objects.create(product=product, current_quantity=12, inventory_date='2026-05-31')

        self.assertEqual(_get_planning_base_date('IKUJI').isoformat(), '2026-06-01')

    def test_arrivals_upload_normalizes_code_and_aggregates_rows(self):
        product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        rows = [
            ['商品コード', '入荷予定日', '入荷予定数量', '確度ステータス'],
            ['6460010001', '2026/06/15', '10', '確定'],
            ['6460010002', '2026-06-15', '5', '確定'],
            ['6460010001', '2026/06/15', '3', '高確度'],
            ['9999999001', '2026/06/15', '7', '確定'],
            ['6460010001', 'bad-date', '1', '希望'],
            ['6460010001', '2026/06/16', '2', '不明'],
        ]
        uploaded = SimpleUploadedFile(
            'arrivals.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_arrivals_csv'),
            {'current_company': 'IKUJI', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ArrivalSchedule.objects.count(), 3)
        self.assertEqual(
            ArrivalSchedule.objects.get(product=product, arrival_date='2026-06-15', status='確定').quantity,
            15,
        )
        self.assertEqual(
            ArrivalSchedule.objects.get(product=product, arrival_date='2026-06-15', status='高確度').quantity,
            3,
        )
        self.assertEqual(
            ArrivalSchedule.objects.get(product=product, arrival_date='2026-06-16', status='確定').quantity,
            2,
        )

    def test_arrivals_upload_accepts_management_arrival_export_format(self):
        product = Product.objects.create(code='5011100', name='ペットゲート', owner_company='SELECT')
        rows = [
            ['輸入到着予定表,2026年06月19日(金)'],
            ['【入荷日：2026年 6月 1日 ～ 】【入港完了/入荷完了の明細は表示しない】'],
            ['発注日付', 'P/O No', 'ｻﾌﾞP/ONo', 'シートNo', '商品コード', '商品名', '数量', '備考', '仕入先ｺｰﾄﾞ', '仕入先名', '入港日', '納入場所', '入荷日', '決定', '備考１', '備考２', '備考３'],
            ['2026/ 2/ 4', '26-011', '', '26-06-002', '5011100001', 'ﾍﾟｯﾄｹﾞｰﾄ', '200', '', '000501', 'Bennington(Taiwan)', '2026/ 5/26', 'ニチイク物流', '2026/ 6/ 2', '入港決定/入荷決定', '', '', ''],
        ]
        uploaded = SimpleUploadedFile(
            'template_arrivals.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_arrivals_csv'),
            {'current_company': 'SELECT', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        arrival = ArrivalSchedule.objects.get(product=product)
        self.assertEqual(arrival.arrival_date.isoformat(), '2026-06-02')
        self.assertEqual(arrival.quantity, 200)
        self.assertEqual(arrival.status, '確定')

    def test_arrivals_upload_auto_routes_mixed_company_file(self):
        ikuji_product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        select_product = Product.objects.create(code='5011100', name='ペットゲート', owner_company='SELECT')
        old_ikuji = Product.objects.create(code='0000001', name='旧日本育児商品', owner_company='IKUJI')
        old_select = Product.objects.create(code='0000002', name='旧ペット商品', owner_company='SELECT')
        ArrivalSchedule.objects.create(product=old_ikuji, arrival_date='2026-06-01', quantity=1, status='確定')
        ArrivalSchedule.objects.create(product=old_select, arrival_date='2026-06-01', quantity=1, status='確定')
        rows = [
            ['商品コード', '入荷予定日', '入荷予定数量', '確度ステータス'],
            ['6460010001', '2026/06/15', '10', '確定'],
            ['5011100001', '2026/06/16', '20', '確定'],
        ]
        uploaded = SimpleUploadedFile(
            'mixed_arrivals.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_arrivals_csv'),
            {'current_company': 'IKUJI', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ArrivalSchedule.objects.filter(product__in=[old_ikuji, old_select]).exists())
        self.assertEqual(
            ArrivalSchedule.objects.get(product=ikuji_product, arrival_date='2026-06-15').quantity,
            10,
        )
        self.assertEqual(
            ArrivalSchedule.objects.get(product=select_product, arrival_date='2026-06-16').quantity,
            20,
        )

    def test_invalid_arrivals_upload_does_not_delete_existing_data(self):
        product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        ArrivalSchedule.objects.create(product=product, arrival_date='2026-06-15', quantity=10, status='確定')
        rows = [
            ['商品コード', '入荷予定日', '入荷予定数量', '確度ステータス'],
            ['9999999001', '2026/06/15', '7', '確定'],
            ['6460010001', 'bad-date', '1', '希望'],
        ]
        uploaded = SimpleUploadedFile(
            'arrivals.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_arrivals_csv'),
            {'current_company': 'IKUJI', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ArrivalSchedule.objects.count(), 1)
        self.assertEqual(ArrivalSchedule.objects.get().quantity, 10)

    def test_create_arrival_schedule_normalizes_code_and_overwrites_same_key(self):
        product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')

        response = self.client.post(
            reverse('create_arrival_schedule'),
            {
                'current_company': 'IKUJI',
                'product_code': '6460010001',
                'arrival_date': '2026-06-15',
                'quantity': '10',
                'status': '確定',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ArrivalSchedule.objects.get(product=product).quantity, 10)

        response = self.client.post(
            reverse('create_arrival_schedule'),
            {
                'current_company': 'IKUJI',
                'product_code': '6460010002',
                'arrival_date': '2026-06-15',
                'quantity': '12',
                'status': '確定',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(ArrivalSchedule.objects.count(), 1)
        self.assertEqual(ArrivalSchedule.objects.get(product=product).quantity, 12)

    def test_product_master_sort_and_bulk_update_checked_products(self):
        slow = Product.objects.create(code='0000001', name='低LT商品', owner_company='IKUJI', lead_time=10, order_lot=1)
        fast = Product.objects.create(code='0000002', name='高LT商品', owner_company='IKUJI', lead_time=30, order_lot=1)
        Inventory.objects.create(product=slow, safety_stock=5)
        Inventory.objects.create(product=fast, safety_stock=5)

        response = self.client.get(
            reverse('product_master_dashboard'),
            {'current_company': 'IKUJI', 'product_sort': 'lead_time', 'product_order': 'desc'},
        )
        body = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertLess(body.index('0000002'), body.index('0000001'))

        response = self.client.post(
            reverse('bulk_update_products'),
            {
                'current_company': 'IKUJI',
                'product_ids': [str(slow.id)],
                'bulk_lead_time_enabled': 'on',
                'bulk_lead_time': '45',
                'bulk_order_lot_enabled': 'on',
                'bulk_order_lot': '12',
                'bulk_safety_stock_enabled': 'on',
                'bulk_safety_stock': '30',
            },
        )

        slow.refresh_from_db()
        fast.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(slow.lead_time, 45)
        self.assertEqual(slow.order_lot, 12)
        self.assertEqual(Inventory.objects.get(product=slow).safety_stock, 30)
        self.assertEqual(fast.lead_time, 30)
        self.assertEqual(Inventory.objects.get(product=fast).safety_stock, 5)

    def test_discontinued_products_are_hidden_by_default_and_prefix_filter_works(self):
        active = Product.objects.create(code='0040001', name='稼働商品', owner_company='IKUJI')
        other_prefix = Product.objects.create(code='0050001', name='別Prefix商品', owner_company='IKUJI')
        discontinued = Product.objects.create(code='0040002', name='廃盤商品', owner_company='IKUJI', is_discontinued=True)
        Inventory.objects.create(product=active, current_quantity=1)
        Inventory.objects.create(product=other_prefix, current_quantity=1)
        Inventory.objects.create(product=discontinued, current_quantity=1)

        response = self.client.get(
            reverse('product_master_dashboard'),
            {'current_company': 'IKUJI', 'product_prefix': '004'},
        )
        body = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertIn('0040001', body)
        self.assertNotIn('0050001', body)
        self.assertNotIn('0040002', body)

        response = self.client.get(
            reverse('product_master_dashboard'),
            {'current_company': 'IKUJI', 'product_prefix': '004', 'show_discontinued': '1'},
        )
        body = response.content.decode('utf-8')

        self.assertIn('0040001', body)
        self.assertIn('0040002', body)
        self.assertNotIn('0050001', body)

        response = self.client.get(reverse('planning_dashboard'), {'current_company': 'IKUJI', 'active_filter': 'all'})
        body = response.content.decode('utf-8')
        self.assertIn('0040001', body)
        self.assertNotIn('0040002', body)

        response = self.client.get(
            reverse('planning_dashboard'),
            {'current_company': 'IKUJI', 'active_filter': 'all', 'show_discontinued': '1'},
        )
        self.assertIn('0040002', response.content.decode('utf-8'))

    def test_bulk_update_can_mark_product_as_discontinued(self):
        product = Product.objects.create(code='0040001', name='稼働商品', owner_company='IKUJI')
        Inventory.objects.create(product=product, safety_stock=5)

        response = self.client.post(
            reverse('bulk_update_products'),
            {
                'current_company': 'IKUJI',
                'product_ids': [str(product.id)],
                'bulk_is_discontinued_enabled': 'on',
                'bulk_is_discontinued': 'true',
            },
        )

        product.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(product.is_discontinued)

    def test_product_master_can_create_and_update_inventory_states(self):
        product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        variant = ProductVariant.objects.create(product=product, state_code='991', state_name='旧B品', current_cost=1000)

        response = self.client.post(
            reverse('create_inventory_state'),
            {'current_company': 'IKUJI', 'state_code': '991', 'state_name': 'B品'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventoryState.objects.get(state_code='991').state_name, 'B品')
        variant.refresh_from_db()
        self.assertEqual(variant.state_name, 'B品')

        state = InventoryState.objects.get(state_code='991')
        response = self.client.post(
            reverse('update_inventory_state', args=[state.id]),
            {'current_company': 'IKUJI', 'state_name': 'B品(箱不良)'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventoryState.objects.get(state_code='991').state_name, 'B品(箱不良)')
        variant.refresh_from_db()
        self.assertEqual(variant.state_name, 'B品(箱不良)')

    def test_inventory_state_template_and_export_are_available(self):
        InventoryState.objects.create(state_code='001', state_name='A品')

        template_response = self.client.get(reverse('download_inventory_state_template'))
        export_response = self.client.get(reverse('export_inventory_states_csv'))

        self.assertEqual(template_response.status_code, 200)
        self.assertEqual(export_response.status_code, 200)
        self.assertEqual(list(csv.reader(io.StringIO(template_response.content.decode('cp932'))))[0], ['状態コード', '状態名'])
        self.assertIn('001,A品', export_response.content.decode('cp932'))

    def test_valuation_upload_creates_variant_snapshots_and_syncs_to_planning(self):
        rows = [
            ['商品コード', '商品名', '状態名', '原価', 'ニチイク在庫', '岸和田在庫'],
            ['6460010001', 'グリップ シート', '良品', '1000', '10', '2'],
            ['6460010002', 'グリップ シート', '箱傷み', '800', '3', '1'],
        ]
        uploaded = SimpleUploadedFile(
            'valuation.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_valuation_csv'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(code='6460010')
        self.assertTrue(product.created_from_valuation)
        self.assertEqual(product.last_valuation_inventory_date.isoformat(), '2026-05-31')
        self.assertIsNotNone(product.last_valuation_synced_at)
        self.assertEqual(ProductVariant.objects.filter(product=product).count(), 2)
        self.assertEqual(InventoryValuationSnapshot.objects.count(), 4)
        self.assertEqual(sum(s.amount for s in InventoryValuationSnapshot.objects.all()), 15200)

        response = self.client.post(
            reverse('sync_valuation_to_planning'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Inventory.objects.get(product=product).current_quantity, 16)
        self.assertEqual(WarehouseInventory.objects.filter(product=product).count(), 2)

    def test_valuation_sync_excludes_variants_marked_not_for_planning_inventory(self):
        rows = [
            ['商品コード', '商品名', '状態名', '原価', 'ニチイク在庫', '岸和田在庫'],
            ['6460010001', 'グリップ シート', '良品', '1000', '10', '2'],
            ['6460010991', 'グリップ シート', 'B品(箱不良)', '800', '3', '1'],
        ]
        uploaded = SimpleUploadedFile(
            'valuation.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_valuation_csv'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31', 'csv_file': uploaded},
        )
        self.assertEqual(response.status_code, 302)

        ProductVariant.objects.filter(state_code='991').update(include_in_planning_inventory=False)
        response = self.client.post(
            reverse('sync_valuation_to_planning'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31'},
        )

        product = Product.objects.get(code='6460010')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Inventory.objects.get(product=product).current_quantity, 12)
        self.assertEqual(
            sum(row.quantity for row in WarehouseInventory.objects.filter(product=product)),
            12,
        )

    def test_bulk_update_valuation_planning_flag_uses_current_filters(self):
        rows = [
            ['商品コード', '商品名', '状態名', '原価', 'ニチイク在庫'],
            ['6460010001', 'グリップ シート', '良品', '1000', '10'],
            ['6460010991', 'グリップ シート', 'B品(箱不良)', '800', '3'],
        ]
        uploaded = SimpleUploadedFile(
            'valuation.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )
        self.client.post(
            reverse('import_valuation_csv'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31', 'csv_file': uploaded},
        )

        response = self.client.post(
            reverse('bulk_update_product_variant_planning_flags'),
            {
                'current_company': 'IKUJI',
                'inventory_date': '2026-05-31',
                'state_code': '',
                'state_name': 'B品',
                'planning_filter': '',
                'variant_sort': 'product_code',
                'variant_order': 'asc',
                'bulk_action': 'exclude',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(ProductVariant.objects.get(state_code='001').include_in_planning_inventory)
        self.assertFalse(ProductVariant.objects.get(state_code='991').include_in_planning_inventory)

    def test_valuation_upload_marks_existing_product_as_updated(self):
        Product.objects.create(code='6460010', name='旧商品名', owner_company='IKUJI')
        rows = [
            ['商品コード', '商品名', '状態名', '原価', 'ニチイク在庫'],
            ['6460010001', '新商品名', '良品', '1000', '10'],
        ]
        uploaded = SimpleUploadedFile(
            'valuation.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_valuation_csv'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31', 'csv_file': uploaded},
        )

        product = Product.objects.get(code='6460010')
        self.assertEqual(response.status_code, 302)
        self.assertFalse(product.created_from_valuation)
        self.assertEqual(product.name, '新商品名')
        self.assertTrue(product.last_valuation_name_updated)
        self.assertEqual(product.last_valuation_inventory_date.isoformat(), '2026-05-31')
        self.assertIsNotNone(product.last_valuation_synced_at)

    def test_valuation_exports_are_available(self):
        product = Product.objects.create(code='6460010', name='グリップ シート', owner_company='IKUJI')
        warehouse = Warehouse.objects.create(name='ニチイク在庫', owner_company='IKUJI')
        variant = ProductVariant.objects.create(product=product, state_code='001', state_name='良品', current_cost=1000)
        InventoryValuationSnapshot.objects.create(
            inventory_date='2026-05-31',
            product_variant=variant,
            warehouse=warehouse,
            quantity=10,
            unit_cost=1000,
            amount=10000,
            owner_company='IKUJI',
        )

        excel_response = self.client.get(reverse('export_valuation_excel'), {'current_company': 'IKUJI', 'inventory_date': '2026-05-31'})
        pdf_response = self.client.get(reverse('export_valuation_pdf'), {'current_company': 'IKUJI', 'inventory_date': '2026-05-31'})

        self.assertEqual(excel_response.status_code, 200)
        self.assertIn('application/vnd.ms-excel', excel_response['Content-Type'])
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response['Content-Type'], 'application/pdf')

    def test_valuation_upload_routes_select_asset_state_codes_to_select(self):
        rows = [
            ['商品コード', '商品名', '状態名', '原価', 'ニチイク在庫'],
            ['6460010400', 'グリップ シート', 'PET SELECT', '1000', '2'],
            ['6460010001', 'グリップ シート', 'A品', '1000', '3'],
        ]
        uploaded = SimpleUploadedFile(
            'valuation.csv',
            self.csv_bytes(rows),
            content_type='text/csv',
        )

        response = self.client.post(
            reverse('import_valuation_csv'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31', 'csv_file': uploaded},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            InventoryValuationSnapshot.objects.get(product_variant__state_code='400').owner_company,
            'SELECT',
        )
        self.assertEqual(
            InventoryValuationSnapshot.objects.get(product_variant__state_code='001').owner_company,
            'IKUJI',
        )

    def test_valuation_template_download(self):
        response = self.client.get(reverse('download_valuation_template'), {'current_company': 'IKUJI'})

        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode('cp932'))))
        self.assertEqual(rows[0][:4], ['商品コード', '商品名', '状態名', '原価'])
        self.assertEqual(len(rows[1][0]), 10)
        self.assertEqual(rows[1][2], 'A品')

    def test_inventory_state_master_import_fills_variant_state_name(self):
        state_rows = [
            ['差分更新方法', '在庫状態ID', '在庫状態コード', '在庫状態'],
            ['', 'field_id', 'field_code', 'field_name'],
            ['', '1', '991', 'B品(箱不良)'],
        ]
        state_file = SimpleUploadedFile('states.csv', self.csv_bytes(state_rows), content_type='text/csv')
        response = self.client.post(
            reverse('import_inventory_state_csv'),
            {'current_company': 'IKUJI', 'csv_file': state_file},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InventoryState.objects.get(state_code='991').state_name, 'B品(箱不良)')

        valuation_rows = [
            ['商品コード', '商品名', '原価', 'ニチイク在庫'],
            ['6460010991', 'グリップ シート', '1000', '2'],
        ]
        valuation_file = SimpleUploadedFile('valuation.csv', self.csv_bytes(valuation_rows), content_type='text/csv')
        response = self.client.post(
            reverse('import_valuation_csv'),
            {'current_company': 'IKUJI', 'inventory_date': '2026-05-31', 'csv_file': valuation_file},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductVariant.objects.get(state_code='991').state_name, 'B品(箱不良)')
