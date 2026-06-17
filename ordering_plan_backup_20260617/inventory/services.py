import csv
import datetime
import io
import math
from datetime import timedelta

from django.db.models import Case, IntegerField, Sum, When
from django.utils import timezone

from .models import (
    ArrivalSchedule,
    Inventory,
    Order,
    Product,
    SalesHistory,
    ShipmentSchedule,
    Warehouse,
    WarehouseInventory,
)


VALID_COMPANIES = ('IKUJI', 'SELECT')
VALID_TREND_DAYS = (90, 120, 150, 180)


def current_company(value):
    return value if value in VALID_COMPANIES else 'IKUJI'


def planning_base_date():
    return timezone.localdate()


def last_month_end():
    today = timezone.localdate()
    first_day = today.replace(day=1)
    return first_day - timedelta(days=1)


def get_csv_reader(uploaded_file):
    binary_data = uploaded_file.read()
    decoded_text = None
    for encoding in ('utf-8-sig', 'utf-8', 'cp932', 'shift_jis'):
        try:
            decoded_text = binary_data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded_text is None:
        decoded_text = binary_data.decode('cp932', errors='replace')
        if '商品コード' not in decoded_text:
            decoded_text = binary_data.decode('utf-8', errors='replace')
    decoded_text = decoded_text.replace('\ufeff', '')
    lines = decoded_text.splitlines()
    if lines:
        cleaned_headers = [h.strip() for h in next(csv.reader([lines[0]]))]
        return csv.DictReader(io.StringIO("\n".join(lines[1:])), fieldnames=cleaned_headers)
    return csv.DictReader(io.StringIO(decoded_text))


def recalculate_abc_ranks(target_company='IKUJI', base_date=None):
    target_company = current_company(target_company)
    base_date = base_date or planning_base_date()

    products = Product.objects.filter(owner_company=target_company)
    sales_summary = SalesHistory.objects.filter(
        company=target_company,
        sold_date__gte=base_date - timedelta(days=180),
        sold_date__lte=base_date,
    ).values('product_id').annotate(
        sum_90=Sum(Case(When(sold_date__gte=base_date - timedelta(days=90), then='quantity'), default=0, output_field=IntegerField())),
        sum_120=Sum(Case(When(sold_date__gte=base_date - timedelta(days=120), then='quantity'), default=0, output_field=IntegerField())),
        sum_150=Sum(Case(When(sold_date__gte=base_date - timedelta(days=150), then='quantity'), default=0, output_field=IntegerField())),
        sum_180=Sum(Case(When(sold_date__gte=base_date - timedelta(days=180), then='quantity'), default=0, output_field=IntegerField())),
    )
    sales_map = {item['product_id']: item for item in sales_summary}

    scored_products = []
    for product in products:
        sales_data = sales_map.get(product.id, {'sum_90': 0, 'sum_120': 0, 'sum_150': 0, 'sum_180': 0})
        if product.trend_days == 120:
            qty = sales_data['sum_120']
        elif product.trend_days == 150:
            qty = sales_data['sum_150']
        elif product.trend_days == 180:
            qty = sales_data['sum_180']
        else:
            qty = sales_data['sum_90']
        scored_products.append({'product': product, 'qty': qty or 0})

    total_sales = sum(item['qty'] for item in scored_products)
    scored_products.sort(key=lambda x: x['qty'], reverse=True)

    running_sum = 0
    for item in scored_products:
        product = item['product']
        if item['qty'] > 0 and total_sales > 0:
            running_sum += item['qty']
            ratio = running_sum / total_sales
            if ratio <= 0.70:
                product.abc_rank = 'A'
            elif ratio <= 0.95:
                product.abc_rank = 'B'
            else:
                product.abc_rank = 'C'
        else:
            product.abc_rank = 'C'

    product_map = {item['product'].id: item['product'] for item in scored_products}
    for inventory in Inventory.objects.filter(current_quantity__gt=0, product__owner_company=target_company):
        product = product_map.get(inventory.product_id)
        if product:
            sales_data = sales_map.get(product.id, {'sum_90': 0, 'sum_120': 0, 'sum_150': 0, 'sum_180': 0})
            if product.trend_days == 120:
                product_qty = sales_data['sum_120']
            elif product.trend_days == 150:
                product_qty = sales_data['sum_150']
            elif product.trend_days == 180:
                product_qty = sales_data['sum_180']
            else:
                product_qty = sales_data['sum_90']
            if (product_qty or 0) == 0:
                product.abc_rank = 'DEAD'

    if product_map:
        Product.objects.bulk_update(product_map.values(), ['abc_rank'])


def split_sales_summary_by_company(sales_summary):
    sales_map_ikuji, sales_map_select = {}, {}
    for item in sales_summary:
        if item['company'] == 'IKUJI':
            sales_map_ikuji[item['product_id']] = item
        else:
            sales_map_select[item['product_id']] = item
    return sales_map_ikuji, sales_map_select


def build_schedule_maps(base_date, future_end_date):
    ship_map = {}
    for shipment in ShipmentSchedule.objects.filter(shipment_date__gte=base_date, shipment_date__lte=future_end_date):
        ship_map.setdefault(shipment.product_id, {})[shipment.shipment_date] = (
            ship_map.setdefault(shipment.product_id, {}).get(shipment.shipment_date, 0) + shipment.quantity
        )

    arr_map = {}
    for arrival in ArrivalSchedule.objects.filter(arrival_date__gte=base_date, arrival_date__lte=future_end_date):
        arr_map.setdefault(arrival.product_id, {}).setdefault(arrival.arrival_date, {})[arrival.status] = (
            arr_map.setdefault(arrival.product_id, {}).setdefault(arrival.arrival_date, {}).get(arrival.status, 0)
            + arrival.quantity
        )
    return ship_map, arr_map


def build_order_sales_maps(base_date, trend_days=None):
    annotations = {
        'sum_30': Sum(Case(When(sold_date__gte=base_date - timedelta(days=30), then='quantity'), default=0, output_field=IntegerField())),
    }
    if trend_days:
        annotations['sum_long'] = Sum(Case(When(sold_date__gte=base_date - timedelta(days=trend_days), then='quantity'), default=0, output_field=IntegerField()))
    else:
        annotations.update({
            'sum_90': Sum(Case(When(sold_date__gte=base_date - timedelta(days=90), then='quantity'), default=0, output_field=IntegerField())),
            'sum_120': Sum(Case(When(sold_date__gte=base_date - timedelta(days=120), then='quantity'), default=0, output_field=IntegerField())),
            'sum_150': Sum(Case(When(sold_date__gte=base_date - timedelta(days=150), then='quantity'), default=0, output_field=IntegerField())),
            'sum_180': Sum(Case(When(sold_date__gte=base_date - timedelta(days=180), then='quantity'), default=0, output_field=IntegerField())),
        })
    sales_summary = SalesHistory.objects.filter(
        sold_date__gte=base_date - timedelta(days=180),
        sold_date__lte=base_date,
    ).values('product_id', 'company').annotate(**annotations)
    return split_sales_summary_by_company(sales_summary)


def execute_single_order_plan(product, inventory, base_date, sales_map_select, sales_map_ikuji, ship_map, arr_map):
    product_id = product.id
    sales_data = (
        sales_map_select.get(product_id, {'sum_30': 0, 'sum_long': 0})
        if product.demand_source == 'SELECT'
        else sales_map_ikuji.get(product_id, {'sum_30': 0, 'sum_long': 0})
    )
    trend_days = product.trend_days if product.trend_days in VALID_TREND_DAYS else 90
    long_sales = sales_data.get('sum_long')
    if long_sales is None:
        long_sales = sales_data.get(f'sum_{trend_days}', 0)
    daily_long = (long_sales or 0) / trend_days
    daily_short = sales_data.get('sum_30', 0) / 30
    raw_trend = daily_short / daily_long if daily_long > 0 else 1.0
    applied_trend = max(product.trend_min, min(raw_trend, product.trend_max))
    daily_demand = daily_long * applied_trend
    order_point = (daily_demand * product.lead_time) + inventory.safety_stock
    product_ships = ship_map.get(product_id, {})
    product_arrivals = arr_map.get(product_id, {})
    running_stock = inventory.current_quantity
    min_stock = running_stock

    for i in range(120):
        day = base_date + timedelta(days=i)
        day_ship = product_ships.get(day, 0)
        arr_total = product_arrivals.get(day, {}).get('確定', 0) + product_arrivals.get(day, {}).get('高確度', 0)
        running_stock = running_stock + arr_total - max(day_ship, daily_demand)
        if running_stock < min_stock:
            min_stock = running_stock

    shortage = order_point - min_stock if min_stock < order_point else 0
    if shortage <= 0:
        return False

    lot = product.order_lot if product.order_lot > 0 else 1
    qty = max(lot, math.ceil(shortage)) if product.lot_rule == 'MIN_LOT_ONLY' else math.ceil(shortage / lot) * lot
    Order.objects.update_or_create(product=product, status='計画中', defaults={'quantity': qty})
    return True


def create_order_plan_for_product(product, inventory):
    base_date = planning_base_date()
    future_end_date = base_date + timedelta(days=120)
    sales_map_ikuji, sales_map_select = build_order_sales_maps(base_date, product.trend_days)
    ship_map, arr_map = build_schedule_maps(base_date, future_end_date)
    return execute_single_order_plan(product, inventory, base_date, sales_map_select, sales_map_ikuji, ship_map, arr_map)


def bulk_create_order_plans(selected_product_ids):
    base_date = planning_base_date()
    future_end_date = base_date + timedelta(days=120)
    sales_map_ikuji, sales_map_select = build_order_sales_maps(base_date)
    ship_map, arr_map = build_schedule_maps(base_date, future_end_date)
    products = Product.objects.filter(id__in=selected_product_ids)
    inventories = {inv.product_id: inv for inv in Inventory.objects.filter(product_id__in=selected_product_ids)}

    success_count = 0
    for product in products:
        inventory = inventories.get(product.id)
        if inventory and execute_single_order_plan(product, inventory, base_date, sales_map_select, sales_map_ikuji, ship_map, arr_map):
            success_count += 1
    return success_count


def import_inventory_csv(uploaded_file, target_company, selected_date=None):
    target_company = current_company(target_company)
    reader = get_csv_reader(uploaded_file)
    headers = reader.fieldnames
    if not headers or '商品コード' not in headers:
        return 0

    wh_map = {
        wh_name: Warehouse.objects.get_or_create(
            name=wh_name,
            owner_company=target_company,
            defaults={'is_transit': '移動中' in wh_name},
        )[0]
        for wh_name in headers
        if wh_name != '商品コード'
    }
    products = {p.code: p for p in Product.objects.filter(owner_company=target_company)}
    selected_date = selected_date or last_month_end()
    row_count = 0

    for row in reader:
        code = str(row.get('商品コード', '')).strip()
        if code.isdigit() and len(code) < 7:
            code = code.zfill(7)
        if code not in products:
            continue
        product = products[code]
        total = 0
        for warehouse_name, warehouse in wh_map.items():
            try:
                qty = int(float(row.get(warehouse_name, '0')))
            except (TypeError, ValueError):
                qty = 0
            WarehouseInventory.objects.update_or_create(product=product, warehouse=warehouse, defaults={'quantity': qty})
            total += qty
        Inventory.objects.update_or_create(product=product, defaults={'current_quantity': total, 'inventory_date': selected_date})
        row_count += 1

    recalculate_abc_ranks(target_company)
    return row_count


def import_products_csv(uploaded_file, target_company):
    target_company = current_company(target_company)
    reader = get_csv_reader(uploaded_file)
    row_count = 0

    for row in reader:
        code = str(row.get('商品コード', '')).strip()
        if not code or code.startswith('field_') or code.lower() == 'none':
            continue
        if code.isdigit() and len(code) < 7:
            code = code.zfill(7)
        try:
            lead_time = int(float(row.get('リードタイム', '30')))
        except (TypeError, ValueError):
            lead_time = 30
        try:
            order_lot = int(float(row.get('発注ロット', '1')))
        except (TypeError, ValueError):
            order_lot = 1
        lot_rule = row.get('超過時ルール', 'ROUND_UP_LOT').strip()
        if lot_rule not in ['ROUND_UP_LOT', 'MIN_LOT_ONLY']:
            lot_rule = 'ROUND_UP_LOT'
        try:
            trend_days = int(float(row.get('長期トレンド日数', '90')))
        except (TypeError, ValueError):
            trend_days = 90
        is_excluded = row.get('管理外フラグ', 'FALSE').strip().upper() == 'TRUE'

        product, _ = Product.objects.update_or_create(
            code=code,
            defaults={
                'name': row.get('商品名', '名称未設定'),
                'price': 0,
                'supplier': '仕入先未設定',
                'lead_time': lead_time,
                'order_lot': order_lot,
                'lot_rule': lot_rule,
                'trend_days': trend_days,
                'is_excluded': is_excluded,
                'owner_company': target_company,
                'demand_source': target_company,
            },
        )
        Inventory.objects.get_or_create(
            product=product,
            defaults={'current_quantity': 0, 'safety_stock': 20, 'inventory_date': last_month_end()},
        )
        row_count += 1

    recalculate_abc_ranks(target_company)
    return row_count


def import_sales_csv(uploaded_file, target_company):
    target_company = current_company(target_company)
    rows = list(get_csv_reader(uploaded_file))
    if not rows:
        return 0, 0

    product_dict = {p.code: p for p in Product.objects.filter(owner_company=target_company)}
    date_set, parsed_rows = set(), []
    for row in rows:
        if row.get('伝票日付', '').startswith('field_'):
            continue
        code = str(row.get('商品コード', '')).strip()
        if code.isdigit() and len(code) < 7:
            code = code.zfill(7)
        if code not in product_dict:
            continue
        try:
            raw_date = row['伝票日付'].split()[0]
            sold_date = (
                datetime.datetime.strptime(raw_date, '%Y/%m/%d').date()
                if '/' in raw_date
                else datetime.datetime.strptime(raw_date, '%Y-%m-%d').date()
            )
            quantity = int(float(row['合計 / 数量']))
        except (KeyError, TypeError, ValueError):
            continue
        date_set.add(sold_date)
        parsed_rows.append({
            'sold_date': sold_date,
            'product': product_dict[code],
            'code': code,
            'quantity': quantity,
            'customer': row.get('得意先コード', '').strip(),
        })

    existing_sales = {}
    if date_set:
        for sales in SalesHistory.objects.filter(company=target_company, sold_date__in=date_set).select_related('product'):
            existing_sales[(sales.sold_date, sales.product.code, sales.customer)] = sales

    create_list, update_list = [], []
    for row in parsed_rows:
        key = (row['sold_date'], row['code'], row['customer'])
        if key in existing_sales:
            sales = existing_sales[key]
            if sales.quantity != row['quantity']:
                sales.quantity = row['quantity']
                update_list.append(sales)
        else:
            create_list.append(SalesHistory(
                sales_id=None,
                product=row['product'],
                sold_date=row['sold_date'],
                quantity=row['quantity'],
                customer=row['customer'],
                company=target_company,
            ))

    if create_list:
        SalesHistory.objects.bulk_create(create_list)
    if update_list:
        SalesHistory.objects.bulk_update(update_list, ['quantity'])
    recalculate_abc_ranks(target_company)
    return len(create_list), len(update_list)


def import_arrivals_csv(uploaded_file, target_company):
    target_company = current_company(target_company)
    ArrivalSchedule.objects.filter(product__owner_company=target_company).delete()
    reader = get_csv_reader(uploaded_file)
    products = {p.code: p for p in Product.objects.filter(owner_company=target_company)}
    row_count = 0

    for row in reader:
        code = str(row.get('商品コード', '')).strip()
        if code.isdigit() and len(code) < 7:
            code = code.zfill(7)
        if code not in products:
            continue
        date_key = '入荷予定日' if '入荷予定日' in row else '予定日'
        qty_key = '入荷予定数量' if '入荷予定数量' in row else '入荷数量'
        try:
            raw_date = row[date_key].split()[0]
            arrival_date = (
                datetime.datetime.strptime(raw_date, '%Y/%m/%d').date()
                if '/' in raw_date
                else datetime.datetime.strptime(raw_date, '%Y-%m-%d').date()
            )
            quantity = int(float(row[qty_key]))
        except (KeyError, TypeError, ValueError):
            continue
        status = row.get('確度ステータス', '確定')
        if status not in ['確定', '高確度', '希望']:
            status = '確定'
        ArrivalSchedule.objects.create(
            product=products[code],
            arrival_date=arrival_date,
            quantity=quantity,
            status=status,
        )
        row_count += 1
    return row_count


def csv_template_content(template_type, target_company):
    target_company = current_company(target_company)
    sample_code = '0040006' if target_company == 'SELECT' else '5100299'
    if template_type == 'inventory':
        current_warehouses = Warehouse.objects.filter(owner_company=target_company)
        cols = (
            ','.join([wh.name for wh in current_warehouses])
            if current_warehouses
            else 'ペットセレクト倉庫,ペットセレクト移動中'
            if target_company == 'SELECT'
            else 'ニチイク在庫,岸和田在庫,西松屋預託,西松屋移動中'
        )
        column_count = current_warehouses.count() if current_warehouses else 2 if target_company == 'SELECT' else 4
        return f"商品コード,{cols}\n{sample_code},{','.join(['0'] * column_count)}\n"

    templates = {
        'products': '商品コード,商品名,リードタイム,発注ロット,超過時ルール,長期トレンド日数,管理外フラグ\n0040006,サンプル商品名称,90,100,ROUND_UP_LOT,90,FALSE\n',
        'sales': f'伝票日付,得意先コード,商品コード,状態コード,合計 / 粗利,合計 / 税抜,合計 / 数量\n2026/06/01,000013,{sample_code},001,3300,7920,6\n',
        'arrivals': f'商品コード,入荷予定日,入荷予定数量,確度ステータス\n{sample_code},2026/06/15,100,確定\n',
    }
    return templates.get(template_type, '')


def export_products_rows(target_company):
    rows = [['商品コード', '商品名', '標準原価', '仕入先名', 'リードタイム', '発注ロット', '超過時ルール', '長期トレンド日数', '管理外フラグ']]
    for product in Product.objects.filter(owner_company=current_company(target_company)):
        rows.append([
            product.code,
            product.name,
            product.price,
            product.supplier,
            product.lead_time,
            product.order_lot,
            product.lot_rule,
            product.trend_days,
            product.is_excluded,
        ])
    return rows


def export_inventory_rows(target_company):
    target_company = current_company(target_company)
    warehouses = Warehouse.objects.filter(owner_company=target_company)
    inventories = Inventory.objects.select_related('product').filter(product__owner_company=target_company)
    stock_map = {}
    for record in WarehouseInventory.objects.filter(warehouse__owner_company=target_company):
        stock_map.setdefault(record.product_id, {})[record.warehouse_id] = record.quantity

    rows = [['商品コード', '商品名', '現在庫数（合算）', '安全在庫数'] + [warehouse.name for warehouse in warehouses]]
    for inventory in inventories:
        row = [inventory.product.code, inventory.product.name, inventory.current_quantity, inventory.safety_stock]
        for warehouse in warehouses:
            row.append(stock_map.get(inventory.product.id, {}).get(warehouse.id, 0))
        rows.append(row)
    return rows


def export_sales_rows(target_company):
    rows = [['伝票日付', '商品コード', '商品名', '販売数', '得意先名']]
    for sales in SalesHistory.objects.select_related('product').filter(company=current_company(target_company)):
        rows.append([
            sales.sold_date.strftime('%Y/%m/%d'),
            sales.product.code,
            sales.product.name,
            sales.quantity,
            sales.customer,
        ])
    return rows


def export_arrivals_rows(target_company):
    rows = [['商品コード', '商品名', '入荷予定日', '入荷予定数量', '確度ステータス']]
    for arrival in ArrivalSchedule.objects.select_related('product').filter(product__owner_company=current_company(target_company)):
        rows.append([
            arrival.product.code,
            arrival.product.name,
            arrival.arrival_date.strftime('%Y/%m/%d'),
            arrival.quantity,
            arrival.status,
        ])
    return rows


def rows_to_cp932_csv(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return buffer.getvalue().encode('cp932', errors='replace')
