from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Inventory, Order, Product, SalesHistory


class PlanningViewTests(TestCase):
    def test_dashboard_get_does_not_mutate_select_product_demand_source(self):
        product = Product.objects.create(
            code='0000001',
            name='SELECT sample',
            owner_company='SELECT',
            demand_source='IKUJI',
        )
        Inventory.objects.create(product=product, current_quantity=10)

        response = self.client.get(reverse('planning_dashboard'), {'current_company': 'SELECT'})

        self.assertEqual(response.status_code, 200)
        product.refresh_from_db()
        self.assertEqual(product.demand_source, 'IKUJI')

    def test_create_order_plan_updates_existing_planned_order(self):
        product = Product.objects.create(
            code='0000002',
            name='Order sample',
            lead_time=30,
            order_lot=10,
            trend_days=90,
            owner_company='IKUJI',
            demand_source='IKUJI',
        )
        Inventory.objects.create(product=product, current_quantity=0, safety_stock=20)
        SalesHistory.objects.create(
            product=product,
            sold_date=timezone.localdate() - timedelta(days=1),
            quantity=90,
            customer='C001',
            company='IKUJI',
        )

        url = reverse('create_order_plan', args=[product.id])
        self.client.post(url)
        first_order = Order.objects.get(product=product, status='計画中')
        self.client.post(url)

        self.assertEqual(Order.objects.filter(product=product, status='計画中').count(), 1)
        first_order.refresh_from_db()
        self.assertGreater(first_order.quantity, 0)

    def test_sales_import_skips_products_owned_by_other_company(self):
        Product.objects.create(
            code='0000003',
            name='IKUJI only',
            owner_company='IKUJI',
            demand_source='IKUJI',
        )
        csv_data = (
            '伝票日付,得意先コード,商品コード,状態コード,合計 / 粗利,合計 / 税抜,合計 / 数量\n'
            f'{timezone.localdate():%Y/%m/%d},C001,0000003,001,0,0,5\n'
        ).encode('utf-8')
        upload = SimpleUploadedFile('sales.csv', csv_data, content_type='text/csv')

        response = self.client.post(
            reverse('import_sales_csv'),
            {'current_company': 'SELECT', 'csv_file': upload},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SalesHistory.objects.filter(company='SELECT').exists())
