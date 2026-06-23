import datetime
import math
import io
import csv
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.http import HttpResponse
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .models import Product, Inventory, Warehouse, WarehouseInventory, SalesHistory, Order, ShipmentSchedule, ArrivalSchedule, ProductVariant, InventoryState, InventoryValuationSnapshot, ImportLog, SalesImportSkip
from .product_importer import import_uploaded_product_file
from .sales_importer import import_uploaded_sales_file
from .valuation_service import (
    available_inventory_dates,
    import_inventory_state_master,
    import_valuation_snapshot,
    normalize_warehouse_name,
    parse_inventory_date,
    sync_snapshot_to_planning_inventory,
    valuation_context,
)
from django.db.models import Sum, Case, When, IntegerField, Q, Max
from datetime import timedelta

def _get_csv_reader(uploaded_file):
    binary_data = uploaded_file.read()
    decoded_text = None
    for encoding in ('utf-8-sig', 'utf-8', 'cp932', 'shift_jis'):
        try:
            decoded_text = binary_data.decode(encoding)
            break
        except UnicodeDecodeError: continue
    if decoded_text is None:
        decoded_text = binary_data.decode('cp932', errors='replace')
        if '商品コード' not in decoded_text: decoded_text = binary_data.decode('utf-8', errors='replace')
    decoded_text = decoded_text.replace('\ufeff', '')
    lines = decoded_text.splitlines()
    if lines:
        header_index = 0
        for idx, line in enumerate(lines[:20]):
            headers = [h.strip() for h in next(csv.reader([line]))]
            if '商品コード' in headers:
                header_index = idx
                break
        cleaned_headers = [h.strip() for h in next(csv.reader([lines[header_index]]))]
        return csv.DictReader(io.StringIO("\n".join(lines[header_index + 1:])), fieldnames=cleaned_headers)
    return csv.DictReader(io.StringIO(decoded_text))

def _normalize_inventory_product_code(raw_code):
    digit_code = ''.join(ch for ch in str(raw_code or '') if ch.isdigit())
    if len(digit_code) >= 10:
        return digit_code[:7]
    if digit_code and len(digit_code) < 7:
        return digit_code.zfill(7)
    return digit_code or str(raw_code or '').strip()

def _parse_inventory_quantity(value):
    if value in (None, ''):
        return 0, False
    text = str(value).replace(',', '').strip()
    if not text:
        return 0, False
    try:
        return int(float(text)), False
    except (ValueError, TypeError):
        return 0, True

def _normalize_product_code(raw_code):
    return _normalize_inventory_product_code(raw_code)

def _parse_csv_quantity(value):
    return _parse_inventory_quantity(value)

def _parse_csv_date(value):
    text = str(value or '').strip()
    if not text:
        return None
    text = ' '.join(text.split())
    if '/' in text:
        parts = [part.strip() for part in text.split('/')]
        if len(parts) == 3 and all(parts):
            text = '/'.join(parts)
    for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%Y年 %m月 %d日', '%Y年%m月%d日'):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

def _pick_first_value(row, candidates, default=''):
    for key in candidates:
        if key in row and str(row.get(key, '')).strip():
            return row.get(key)
    return default

def _normalize_arrival_status(value):
    raw_status = str(value or '').strip()
    if raw_status in ['確定', '高確度', '希望']:
        return raw_status, False
    if raw_status in ['入港決定', '入荷決定', '入港決定/入荷決定', '決定']:
        return '確定', False
    if raw_status in ['未定', '予定', '希望納期']:
        return '希望', False
    return '確定', bool(raw_status)

def _uploaded_filename(request):
    uploaded = request.FILES.get('csv_file')
    return uploaded.name if uploaded else ''

def _log_import(dashboard, import_type, status, summary, request=None, company='', details='', error_count=0, warning_count=0, filename=''):
    return ImportLog.objects.create(
        dashboard=dashboard,
        import_type=import_type,
        status=status,
        company=company,
        filename=filename or (_uploaded_filename(request) if request else ''),
        summary=summary[:255],
        details=details,
        error_count=error_count,
        warning_count=warning_count,
    )

def _recent_import_logs(dashboard, limit=8):
    return ImportLog.objects.filter(dashboard=dashboard).order_by('-created_at')[:limit]

def _status_from_counts(error_count=0, warning_count=0):
    if error_count:
        return 'warning'
    if warning_count:
        return 'warning'
    return 'success'


def _pagination_context(request, page_obj, button_class='btn'):
    query_params = request.GET.copy()
    query_params.pop('page', None)
    query = query_params.urlencode()
    return {
        'pagination_query': f'{query}&' if query else '',
        'pagination_button_class': button_class,
        'page_jump_back': max(1, page_obj.number - 10),
        'page_jump_forward': min(page_obj.paginator.num_pages, page_obj.number + 10),
    }

def _build_arrival_map(queryset):
    arr_map = {}
    for a in queryset:
        arr_map.setdefault(a.product_id, {}).setdefault(a.arrival_date, {}).setdefault(a.status, 0)
        arr_map[a.product_id][a.arrival_date][a.status] += a.quantity
    return arr_map

def _build_order_arrival_map(queryset):
    order_map = {}
    for order in queryset:
        if order.expected_arrival_date:
            order_map.setdefault(order.product_id, {}).setdefault(order.expected_arrival_date, 0)
            order_map[order.product_id][order.expected_arrival_date] += order.quantity
    return order_map

def _get_planning_base_date(target_company='IKUJI'):
    latest_sales_date = SalesHistory.objects.filter(
        company=target_company,
        is_advance_order=False,
        sold_date__lte=datetime.date.today(),
    ).aggregate(latest=Max('sold_date'))['latest']
    if latest_sales_date:
        # Start forecasting after the last actual sales day, not after the stocktake date.
        return latest_sales_date + timedelta(days=1)
    return datetime.date.today()

def _recalculate_abc_ranks(target_company='IKUJI'):
    """【現実的改修】各商品の個別設定「長期トレンド日数」に100%自動連動してABCを評価する"""
    base_date = _get_planning_base_date(target_company)
    
    # 1. 会社に所属する全マスタをロード
    products = Product.objects.filter(owner_company=target_company, is_discontinued=False)
    
    # 2. 期間ごとの売上（90, 120, 150, 180日）をデータベースから一括アノテーション取得（高速化）
    sales_summary = SalesHistory.objects.filter(
        company=target_company, is_advance_order=False,
        sold_date__gte=base_date - timedelta(days=180), sold_date__lte=base_date
    ).values('product_id').annotate(
        sum_90=Sum(Case(When(sold_date__gte=base_date - timedelta(days=90), then='quantity'), default=0, output_field=IntegerField())),
        sum_120=Sum(Case(When(sold_date__gte=base_date - timedelta(days=120), then='quantity'), default=0, output_field=IntegerField())),
        sum_150=Sum(Case(When(sold_date__gte=base_date - timedelta(days=150), then='quantity'), default=0, output_field=IntegerField())),
        sum_180=Sum(Case(When(sold_date__gte=base_date - timedelta(days=180), then='quantity'), default=0, output_field=IntegerField())),
    )
    sales_map = {item['product_id']: item for item in sales_summary}
    
    # 3. 各商品が「自身の長期ベース設定」で稼いだ売上数をマッピング
    scored_products = []
    for p in products:
        s_data = sales_map.get(p.id, {'sum_90': 0, 'sum_120': 0, 'sum_150': 0, 'sum_180': 0})
        tdays = p.trend_days
        
        # マスタのプルダウン設定（90, 120, 150, 180）に応じて、参照する売上実績の引き出しを動的にスイッチ！
        if tdays == 120: qty = s_data['sum_120']
        elif tdays == 150: qty = s_data['sum_150']
        elif tdays == 180: qty = s_data['sum_180']
        else: qty = s_data['sum_90'] # デフォルトは90日
        
        scored_products.append({'product': p, 'qty': qty or 0})
        
    # 各自の評価期間売上の総和を分母にする
    total_sales = sum(item['qty'] for item in scored_products)
    
    # 売上数が多い順にソートして比率分配
    scored_products.sort(key=lambda x: x['qty'], reverse=True)
    
    running_sum = 0
    for item in scored_products:
        p = item['product']
        if item['qty'] > 0 and total_sales > 0:
            running_sum += item['qty']
            ratio = running_sum / total_sales
            if ratio <= 0.70: p.abc_rank = 'A'
            elif ratio <= 0.95: p.abc_rank = 'B'
            else: p.abc_rank = 'C'
        else:
            p.abc_rank = 'C'
            
    # 4. 「自身の設定期間内」で売上が0、かつ現在庫が残っているものを「DEAD（処分推奨）」に上書き
    p_dict = {item['product'].id: item['product'] for item in scored_products}
    for inv in Inventory.objects.filter(current_quantity__gt=0, product__owner_company=target_company):
        p = p_dict.get(inv.product_id)
        if p:
            s_data = sales_map.get(p.id, {'sum_90': 0, 'sum_120': 0, 'sum_150': 0, 'sum_180': 0})
            tdays = p.trend_days
            p_qty = s_data['sum_120'] if tdays == 120 else s_data['sum_150'] if tdays == 150 else s_data['sum_180'] if tdays == 180 else s_data['sum_90']
            if (p_qty or 0) == 0:
                p.abc_rank = 'DEAD'
                
    Product.objects.bulk_update(p_dict.values(), ['abc_rank'])

def _execute_single_order_plan(product, inventory, base_date, future_end_date, sales_map_select, sales_map_ikuji, ship_map, arr_map, existing_order_map=None):
    pid = product.id
    sales_data = sales_map_select.get(pid, {'sum_30': 0, 'sum_long': 0}) if product.demand_source == 'SELECT' else sales_map_ikuji.get(pid, {'sum_30': 0, 'sum_long': 0})
    tdays = product.trend_days if product.trend_days in [90, 120, 150, 180] else 90
    daily_long = sales_data.get('sum_long', 0) / tdays
    daily_short = sales_data.get('sum_30', 0) / 30
    raw_trend = daily_short / daily_long if daily_long > 0 else 1.0
    applied_trend = max(product.trend_min, min(raw_trend, product.trend_max))
    daily_demand = daily_long * applied_trend
    order_point = (daily_demand * product.lead_time) + inventory.safety_stock
    product_ships = ship_map.get(pid, {})
    product_arrivals = arr_map.get(pid, {})
    existing_arrivals = (existing_order_map or {}).get(pid, {})
    horizon_days = (future_end_date - base_date).days

    def forecast(order_arrivals):
        running_stock = inventory.current_quantity
        stocks = {}
        for i in range(horizon_days):
            day = base_date + timedelta(days=i)
            scheduled_arrival = order_arrivals.get(day, 0)
            normal_arrival = product_arrivals.get(day, {}).get('確定', 0) + product_arrivals.get(day, {}).get('高確度', 0)
            running_stock += scheduled_arrival + normal_arrival - max(product_ships.get(day, 0), daily_demand)
            stocks[day] = running_stock
        return stocks

    def rounded_quantity(shortage):
        lot = max(1, product.order_lot)
        return max(lot, math.ceil(shortage)) if product.lot_rule == 'MIN_LOT_ONLY' else math.ceil(shortage / lot) * lot

    interval_days = max(0, product.order_interval_days or 0)
    if interval_days == 0:
        stocks = forecast(existing_arrivals)
        min_stock = min(stocks.values(), default=inventory.current_quantity)
        shortage = order_point - min_stock if min_stock < order_point else 0
        if shortage <= 0:
            return 0
        Order.objects.create(
            product=product,
            quantity=rounded_quantity(shortage),
            order_date=base_date,
            expected_arrival_date=base_date + timedelta(days=product.lead_time),
            status='計画中',
        )
        return 1

    created_count = 0
    planned_arrivals = {date: quantity for date, quantity in existing_arrivals.items()}
    for offset in range(0, horizon_days, interval_days):
        order_date = base_date + timedelta(days=offset)
        arrival_date = order_date + timedelta(days=product.lead_time)
        if arrival_date > future_end_date - timedelta(days=1):
            continue
        stocks = forecast(planned_arrivals)
        projected_stock = stocks.get(arrival_date, inventory.current_quantity)
        target_stock = order_point + (daily_demand * interval_days)
        shortage = target_stock - projected_stock
        if shortage <= 0:
            continue
        quantity = rounded_quantity(shortage)
        planned_arrivals[arrival_date] = planned_arrivals.get(arrival_date, 0) + quantity
        Order.objects.create(
            product=product,
            quantity=quantity,
            order_date=order_date,
            expected_arrival_date=arrival_date,
            status='計画中',
        )
        created_count += 1
    return created_count

def planning_dashboard(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']: current_company = 'IKUJI'
    if current_company == 'SELECT': Product.objects.filter(owner_company='SELECT', demand_source='IKUJI').update(demand_source='SELECT')
    _recalculate_abc_ranks(current_company)
    ikuji_warehouses = Warehouse.objects.filter(owner_company='IKUJI', is_active=True)
    shared_wh_ids = [int(x) for x in request.GET.getlist('shared_whs') if x.isdigit()]
    active_filter = request.GET.get('active_filter', 'active')
    if active_filter not in ['active', 'all']: active_filter = 'active'
    active_months = request.GET.get('active_months', '12')
    try: months_val = int(active_months)
    except ValueError: months_val = 12
    search_query = request.GET.get('search_query', '').strip()
    abc_filter = request.GET.get('abc_filter', '').strip()
    supplier_code = request.GET.get('supplier_code', '').strip()
    show_discontinued = request.GET.get('show_discontinued') == '1'
    base_date = _get_planning_base_date(current_company)
    thirty_days_ago = base_date - timedelta(days=30)
    long_check_date = base_date - timedelta(days=months_val * 30)
    sales_summary = SalesHistory.objects.filter(is_advance_order=False, sold_date__gte=base_date - timedelta(days=180), sold_date__lte=base_date).values('product_id', 'company').annotate(
        sum_30=Sum(Case(When(sold_date__gte=thirty_days_ago, then='quantity'), default=0, output_field=IntegerField())),
        sum_45=Sum(Case(When(sold_date__gte=base_date - timedelta(days=45), then='quantity'), default=0, output_field=IntegerField())),
        sum_60=Sum(Case(When(sold_date__gte=base_date - timedelta(days=60), then='quantity'), default=0, output_field=IntegerField())),
        sum_75=Sum(Case(When(sold_date__gte=base_date - timedelta(days=75), then='quantity'), default=0, output_field=IntegerField())),
        sum_90=Sum(Case(When(sold_date__gte=base_date - timedelta(days=90), then='quantity'), default=0, output_field=IntegerField())),
        sum_120=Sum(Case(When(sold_date__gte=base_date - timedelta(days=120), then='quantity'), default=0, output_field=IntegerField())),
        sum_150=Sum(Case(When(sold_date__gte=base_date - timedelta(days=150), then='quantity'), default=0, output_field=IntegerField())),
        sum_180=Sum(Case(When(sold_date__gte=base_date - timedelta(days=180), then='quantity'), default=0, output_field=IntegerField())),
        sum_long=Sum(Case(When(sold_date__gte=long_check_date, then='quantity'), default=0, output_field=IntegerField()))
    )
    sales_map_ikuji, sales_map_select = {}, {}
    for item in sales_summary:
        if item['company'] == 'IKUJI': sales_map_ikuji[item['product_id']] = item
        else: sales_map_select[item['product_id']] = item
    inventories = Inventory.objects.select_related('product').filter(product__is_excluded=False, product__owner_company=current_company).order_by('product__code')
    if not show_discontinued:
        inventories = inventories.filter(product__is_discontinued=False)
    if abc_filter in ['A', 'B', 'C', 'DEAD']: inventories = inventories.filter(product__abc_rank=abc_filter)
    if supplier_code: inventories = inventories.filter(product__code__startswith=supplier_code)
    if search_query: inventories = inventories.filter(Q(product__code__icontains=search_query) | Q(product__name__icontains=search_query))
    elif not abc_filter and not supplier_code:
        if active_filter == 'active':
            active_pids = []
            for pid, item in (sales_map_ikuji.items() if current_company=='IKUJI' else sales_map_select.items()):
                if item['sum_long'] > 0: active_pids.append(pid)
            inventories = inventories.filter(product_id__in=active_pids)
    kpi_shortage_cnt, kpi_order_point_cnt = 0, 0
    paginator = Paginator(inventories, 50)
    page_number = request.GET.get('page', 1)
    try: page_obj = paginator.page(page_number)
    except PageNotAnInteger: page_obj = paginator.page(1)
    except EmptyPage: page_obj = paginator.page(paginator.num_pages)
    future_end_date = base_date + timedelta(days=120)
    shipments = ShipmentSchedule.objects.filter(
        shipment_date__gte=base_date,
        shipment_date__lte=future_end_date,
    ).order_by('shipment_date', 'id')
    ship_map, shipment_detail_map = {}, {}
    for s in shipments:
        ship_map.setdefault(s.product_id, {}).setdefault(s.shipment_date, 0)
        ship_map[s.product_id][s.shipment_date] += s.quantity
        shipment_detail_map.setdefault(s.product_id, []).append(s)
    arrivals = ArrivalSchedule.objects.filter(arrival_date__gte=base_date, arrival_date__lte=future_end_date)
    arr_map = _build_arrival_map(arrivals)
    active_orders = Order.objects.select_related('product').filter(
        product__owner_company=current_company,
    ).order_by('order_date', 'created_at')
    planned_order_map = _build_order_arrival_map(
        active_orders.filter(status__in=['計画中', '発注済'], expected_arrival_date__gte=base_date, expected_arrival_date__lte=future_end_date)
    )
    date_list = [base_date + timedelta(days=i) for i in range(120)]
    all_warehouses = Warehouse.objects.filter(owner_company=current_company, is_active=True)
    wh_inv_records = WarehouseInventory.objects.select_related('warehouse', 'product').filter(
        Q(warehouse__owner_company=current_company, warehouse__is_active=True)
        | Q(warehouse_id__in=shared_wh_ids, warehouse__is_active=True)
    )
    wh_stock_map, cross_company_wh_stock, cross_company_stock = {}, {}, {}
    for rec in wh_inv_records:
        if rec.warehouse.owner_company == current_company: wh_stock_map.setdefault(rec.product_id, {})[rec.warehouse_id] = rec.quantity
        elif current_company == 'SELECT' and rec.warehouse_id in shared_wh_ids:
            cross_company_wh_stock.setdefault(rec.product.code, {})[rec.warehouse_id] = rec.quantity
            cross_company_stock[rec.product.code] = cross_company_stock.get(rec.product.code, 0) + rec.quantity
    all_inventories_for_kpi = Inventory.objects.select_related('product').filter(product__is_excluded=False, product__owner_company=current_company)
    if not show_discontinued:
        all_inventories_for_kpi = all_inventories_for_kpi.filter(product__is_discontinued=False)
    for k_item in all_inventories_for_kpi:
        k_pid = k_item.product.id
        k_sales = sales_map_select.get(k_pid, {'sum_30': 0, 'sum_long': 0}) if k_item.product.demand_source == 'SELECT' else sales_map_ikuji.get(k_pid, {'sum_30': 0, 'sum_long': 0})
        k_tdays = k_item.product.trend_days if k_item.product.trend_days in [90, 120, 150, 180] else 90
        k_daily_demand = (k_sales.get('sum_long', 0) / k_tdays) * max(k_item.product.trend_min, min(((k_sales.get('sum_30', 0) / 30) / (k_sales.get('sum_long', 0) / k_tdays) if k_sales.get('sum_long', 0) > 0 else 1.0), k_item.product.trend_max))
        k_order_point = (k_daily_demand * k_item.product.lead_time) + k_item.safety_stock
        k_stock = sum(wh_stock_map.get(k_pid, {}).values()) + cross_company_stock.get(k_item.product.code, 0) if current_company == 'SELECT' else sum(wh_stock_map.get(k_pid, {}).values())
        k_ships = ship_map.get(k_pid, {}); k_arrs = arr_map.get(k_pid, {}); k_orders = planned_order_map.get(k_pid, {}); k_has_shortage, k_has_op = False, False
        for i in range(120):
            d = base_date + timedelta(days=i)
            k_stock = k_stock + k_orders.get(d, 0) + k_arrs.get(d, {}).get('確定', 0) + k_arrs.get(d, {}).get('高確度', 0) - max(k_ships.get(d, 0), k_daily_demand)
            if k_stock <= 0: k_has_shortage = True
            elif k_stock <= k_order_point: k_has_op = True
        if k_item.product.abc_rank != 'DEAD' or k_item.product.allow_dead_order:
            if k_has_shortage: kpi_shortage_cnt += 1
            elif k_has_op: kpi_order_point_cnt += 1
    inventory_date_choices = []
    current_target = datetime.date.today().replace(day=1)
    for _ in range(4):
        m_end = current_target - timedelta(days=1)
        inventory_date_choices.append({'value': m_end.strftime('%Y-%m-%d'), 'display': m_end.strftime('%Y年%m月末')})
        current_target = m_end.replace(day=1)
    visible_inventories = []
    for item in page_obj:
        pid = item.product.id; pcode = item.product.code
        sales_data = sales_map_select.get(pid, {'sum_30': 0, 'sum_45': 0, 'sum_60': 0, 'sum_75': 0, 'sum_90': 0, 'sum_120': 0, 'sum_150': 0, 'sum_180': 0, 'sum_long': 0}) if item.product.demand_source == 'SELECT' else sales_map_ikuji.get(pid, {'sum_30': 0, 'sum_45': 0, 'sum_60': 0, 'sum_75': 0, 'sum_90': 0, 'sum_120': 0, 'sum_150': 0, 'sum_180': 0, 'sum_long': 0})
        item.demand_30 = round(sales_data['sum_30'], 1)
        item.mid_days = (item.product.trend_days if item.product.trend_days in [90, 120, 150, 180] else 90) // 2
        item.demand_mid = round((sales_data.get(f'sum_{item.mid_days}', 0) or 0) * 30 / item.mid_days, 1)
        tdays = item.product.trend_days
        total_long_sales = sales_data['sum_120'] if tdays == 120 else sales_data['sum_150'] if tdays == 150 else sales_data['sum_180'] if tdays == 180 else sales_data['sum_90']
        if tdays not in [120, 150, 180]: tdays = 90
        daily_long = total_long_sales / tdays; daily_short = sales_data['sum_30'] / 30
        item.demand_long_display = round(daily_long * 30, 1)
        raw_trend = daily_short / daily_long if daily_long > 0 else 1.0
        applied_trend = max(item.product.trend_min, min(raw_trend, item.product.trend_max))
        item.raw_trend, item.applied_trend = round(raw_trend, 2), round(applied_trend, 2)
        daily_demand = daily_long * applied_trend
        item.order_point = round((daily_demand * item.product.lead_time) + item.safety_stock, 1)
        item.suggested_safety_stock = max(10, min(math.ceil(1.65 * max(abs(daily_short - daily_long), daily_demand * 0.3) * math.sqrt(item.product.lead_time)), 300))
        item.warehouse_breakdown = []
        product_wh_data = wh_stock_map.get(pid, {})
        for wh in all_warehouses: item.warehouse_breakdown.append({'name': wh.name, 'quantity': product_wh_data.get(wh.id, 0), 'is_transit': wh.is_transit})
        shared_total = 0
        if current_company == 'SELECT' and shared_wh_ids:
            shared_total = cross_company_stock.get(pcode, 0); shared_wh_data = cross_company_wh_stock.get(pcode, {})
            for wh in ikuji_warehouses:
                if wh.id in shared_wh_ids: item.warehouse_breakdown.append({'name': f"{wh.name}(育)", 'quantity': shared_wh_data.get(wh.id, 0), 'is_transit': wh.is_transit})
        item.current_quantity = sum(product_wh_data.values()) + shared_total
        item.forecast_timeline = []
        item.future_shipments = shipment_detail_map.get(pid, [])
        running_stock = item.current_quantity; has_shortage_risk, has_order_point_risk = False, False
        product_ships, product_arrivals, product_orders = ship_map.get(pid, {}), arr_map.get(pid, {}), planned_order_map.get(pid, {})
        first_shortage_date = None
        for d in date_list:
            day_ship = product_ships.get(d, 0); day_arr_dict = product_arrivals.get(d, {})
            planned_order_total = product_orders.get(d, 0)
            arr_total = day_arr_dict.get('確定', 0) + day_arr_dict.get('高確度', 0) + planned_order_total
            running_stock = running_stock + arr_total - max(day_ship, daily_demand)
            item.forecast_timeline.append({'date': d, 'stock': round(running_stock, 1), 'ship': day_ship, 'planned_order': planned_order_total, 'arr_kakutei': day_arr_dict.get('確定', 0), 'arr_koukaku': day_arr_dict.get('高確度', 0), 'arr_kibou': day_arr_dict.get('希望', 0)})
            if running_stock <= 0:
                has_shortage_risk = True
                if not first_shortage_date: first_shortage_date = d
            elif running_stock <= item.order_point: has_order_point_risk = True
        item.days_to_deadline = (first_shortage_date - timedelta(days=item.product.lead_time) - base_date).days if first_shortage_date else None
        suffix = "(処分許可)" if (item.product.abc_rank == 'DEAD' and item.product.allow_dead_order) else ""
        if item.product.abc_rank == 'DEAD' and not item.product.allow_dead_order: item.status_alert, item.needs_order = "🪵 処分推奨", False
        else:
            item.status_alert = f"🚨 欠品リスク{suffix}" if has_shortage_risk else f"⚠️ 発注点割れ{suffix}" if has_order_point_risk else f"正常{suffix}"
            item.needs_order = has_shortage_risk or has_order_point_risk
        visible_inventories.append(item)
    return render(request, 'inventory/dashboard.html', {
        'inventories': visible_inventories, 'page_obj': page_obj, 'date_list': date_list,
        'active_months': active_months, 'search_query': search_query,
        'inventory_date_choices': inventory_date_choices, 'active_orders': active_orders,
        'abc_filter': abc_filter, 'supplier_code': supplier_code,
        'show_discontinued': show_discontinued, 'current_company': current_company,
        'active_filter': active_filter, 'all_warehouses': all_warehouses,
        'ikuji_warehouses': ikuji_warehouses, 'shared_wh_ids': shared_wh_ids,
        'kpi_shortage_cnt': kpi_shortage_cnt, 'kpi_order_point_cnt': kpi_order_point_cnt,
        'import_logs': _recent_import_logs('planning'),
        **_pagination_context(request, page_obj, button_class='page-btn'),
    })

def product_master_dashboard(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    search_query = request.GET.get('search_query', '').strip()
    state_search_query = request.GET.get('state_search_query', '').strip()
    product_prefix = ''.join(ch for ch in request.GET.get('product_prefix', '').strip() if ch.isdigit())[:3]
    show_discontinued = request.GET.get('show_discontinued') == '1'
    product_sort = request.GET.get('product_sort', 'code')
    product_order = request.GET.get('product_order', 'asc')
    products = Product.objects.filter(owner_company=current_company)
    if not show_discontinued:
        products = products.filter(is_discontinued=False)
    if product_prefix:
        products = products.filter(code__startswith=product_prefix)
    if search_query:
        products = products.filter(Q(code__icontains=search_query) | Q(name__icontains=search_query) | Q(supplier__icontains=search_query))
    sort_fields = {
        'code': 'code',
        'name': 'name',
        'supplier': 'supplier',
        'price': 'price',
        'lead_time': 'lead_time',
        'order_lot': 'order_lot',
        'order_interval_days': 'order_interval_days',
        'safety_stock': 'inventory__safety_stock',
        'trend_days': 'trend_days',
        'demand_source': 'demand_source',
        'source': 'created_from_valuation',
        'discontinued': 'is_discontinued',
    }
    sort_field = sort_fields.get(product_sort, 'code')
    if product_order == 'desc':
        sort_field = '-' + sort_field
    products = products.order_by(sort_field, 'code')
    paginator = Paginator(products, 100)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    product_rows = []
    for product in page_obj:
        inventory, _ = Inventory.objects.get_or_create(product=product)
        product.safety_stock = inventory.safety_stock
        product_rows.append(product)
    inventory_states = InventoryState.objects.all().order_by('state_code')
    if state_search_query:
        inventory_states = inventory_states.filter(
            Q(state_code__icontains=state_search_query) | Q(state_name__icontains=state_search_query)
        )
    return render(request, 'inventory/product_master_dashboard.html', {
        'current_company': current_company,
        'search_query': search_query,
        'state_search_query': state_search_query,
        'product_prefix': product_prefix,
        'show_discontinued': show_discontinued,
        'products': product_rows,
        'inventory_states': inventory_states[:80],
        'inventory_state_count': inventory_states.count(),
        'page_obj': page_obj,
        'product_sort': product_sort,
        'product_order': product_order,
        'lot_rule_choices': Product.LOT_RULE_CHOICES,
        'trend_days_choices': Product.TREND_DAYS_CHOICES,
        'company_choices': Product.COMPANY_CHOICES,
        'import_logs': _recent_import_logs('product_master'),
        **_pagination_context(request, page_obj),
    })

def arrivals_dashboard(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    search_query = request.GET.get('search_query', '').strip()
    arrivals = ArrivalSchedule.objects.select_related('product').filter(product__owner_company=current_company).order_by('arrival_date', 'product__code', 'status')
    if search_query:
        arrivals = arrivals.filter(Q(product__code__icontains=search_query) | Q(product__name__icontains=search_query))
    paginator = Paginator(arrivals, 100)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    return render(request, 'inventory/arrivals_dashboard.html', {
        'current_company': current_company,
        'search_query': search_query,
        'arrivals': page_obj,
        'page_obj': page_obj,
        'status_choices': ArrivalSchedule.STATUS_CHOICES,
        'import_logs': _recent_import_logs('arrivals'),
        **_pagination_context(request, page_obj),
    })

def sales_history_dashboard(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    search_query = request.GET.get('search_query', '').strip()
    product_prefix = ''.join(ch for ch in request.GET.get('product_prefix', '').strip() if ch.isdigit())[:3]
    sales_category = request.GET.get('sales_category', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    summary_period = request.GET.get('summary_period', 'all')
    if summary_period == 'all' and (date_from or date_to):
        summary_period = 'custom'
    if summary_period in {'last_7', 'last_30', 'this_month', 'previous_month'}:
        today = datetime.date.today()
        if summary_period == 'last_7':
            period_start, period_end = today - timedelta(days=6), today
        elif summary_period == 'last_30':
            period_start, period_end = today - timedelta(days=29), today
        elif summary_period == 'this_month':
            period_start, period_end = today.replace(day=1), today
        else:
            current_month_start = today.replace(day=1)
            period_end = current_month_start - timedelta(days=1)
            period_start = period_end.replace(day=1)
        date_from, date_to = period_start.isoformat(), period_end.isoformat()
    elif summary_period not in {'all', 'custom'}:
        summary_period = 'all'
    sales = SalesHistory.objects.select_related('product').filter(company=current_company).order_by(
        '-sold_date', 'customer', 'product__code', 'sales_category'
    )
    if search_query:
        sales = sales.filter(
            Q(product__code__icontains=search_query)
            | Q(product__name__icontains=search_query)
            | Q(customer__icontains=search_query)
        )
    if product_prefix:
        sales = sales.filter(product__code__startswith=product_prefix)
    if sales_category:
        sales = sales.filter(sales_category=sales_category)
    parsed_from = _parse_csv_date(date_from)
    parsed_to = _parse_csv_date(date_to)
    if parsed_from:
        sales = sales.filter(sold_date__gte=parsed_from)
    if parsed_to:
        sales = sales.filter(sold_date__lte=parsed_to)
    totals = sales.aggregate(
        quantity=Sum('quantity'),
        tax_excluded_amount=Sum('tax_excluded_amount'),
        gross_profit_amount=Sum('gross_profit_amount'),
    )
    categories = list(
        SalesHistory.objects.filter(company=current_company)
        .exclude(sales_category='')
        .values_list('sales_category', flat=True)
        .distinct()
        .order_by('sales_category')
    )
    paginator = Paginator(sales, 100)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)
    latest_skip_log = ImportLog.objects.filter(
        dashboard='sales_history', company=current_company, sales_skips__isnull=False,
    ).distinct().order_by('-created_at').first()
    return render(request, 'inventory/sales_history_dashboard.html', {
        'current_company': current_company,
        'search_query': search_query,
        'product_prefix': product_prefix,
        'sales_category': sales_category,
        'date_from': date_from,
        'date_to': date_to,
        'summary_period': summary_period,
        'categories': categories,
        'sales': page_obj,
        'page_obj': page_obj,
        'totals': totals,
        'import_logs': _recent_import_logs('sales_history'),
        'latest_skip_log': latest_skip_log,
        **_pagination_context(request, page_obj),
    })

@require_POST
def update_sales_history_advance_order(request, sales_id):
    current_company = request.POST.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    sale = get_object_or_404(SalesHistory, id=sales_id, company=current_company)
    sale.is_advance_order = 'is_advance_order' in request.POST
    sale.save(update_fields=['is_advance_order'])
    _recalculate_abc_ranks(current_company)
    messages.success(
        request,
        '先付け受注として需要計算から除外しました。' if sale.is_advance_order else '通常販売として需要計算へ戻しました。'
    )
    return redirect(request.META.get('HTTP_REFERER', f'/sales-history/?current_company={current_company}'))

def _filter_valuation_variant_rows(variant_rows, params):
    variant_rows = list(variant_rows)
    variant_total_count = len(variant_rows)
    state_code_filter = params.get('state_code', '').strip()
    state_name_filter = params.get('state_name', '').strip()
    planning_filter = params.get('planning_filter', '').strip()
    variant_sort = params.get('variant_sort', 'product_code')
    variant_order = params.get('variant_order', 'asc')
    if state_code_filter:
        variant_rows = [row for row in variant_rows if state_code_filter in row['product_variant__state_code']]
    if state_name_filter:
        variant_rows = [row for row in variant_rows if state_name_filter in (row['product_variant__state_name'] or '')]
    if planning_filter == 'included':
        variant_rows = [row for row in variant_rows if row['product_variant__include_in_planning_inventory']]
    elif planning_filter == 'excluded':
        variant_rows = [row for row in variant_rows if not row['product_variant__include_in_planning_inventory']]
    sort_fields = {
        'product_code': 'product_variant__product__code',
        'product_name': 'product_variant__product__name',
        'state_code': 'product_variant__state_code',
        'state_name': 'product_variant__state_name',
        'planning': 'product_variant__include_in_planning_inventory',
        'unit_cost': 'unit_cost',
        'quantity': 'quantity',
        'amount': 'amount',
    }
    sort_key = sort_fields.get(variant_sort, 'product_variant__product__code')
    variant_rows.sort(key=lambda row: (row.get(sort_key) is None, row.get(sort_key)), reverse=(variant_order == 'desc'))
    return variant_rows, {
        'state_code_filter': state_code_filter,
        'state_name_filter': state_name_filter,
        'planning_filter': planning_filter,
        'variant_sort': variant_sort,
        'variant_order': variant_order,
        'variant_total_count': variant_total_count,
        'variant_filtered_count': len(variant_rows),
    }

def valuation_dashboard(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']: current_company = 'IKUJI'
    selected_date = parse_inventory_date(request.GET.get('inventory_date'), current_company)
    ctx = valuation_context(selected_date, current_company)
    variant_rows, variant_filter_context = _filter_valuation_variant_rows(ctx['variant_summary'], request.GET)
    ctx['variant_summary'] = variant_rows
    cleanup_candidates = []
    for warehouse in Warehouse.objects.filter(owner_company=current_company, is_active=True).order_by('name'):
        planning_quantity = WarehouseInventory.objects.filter(warehouse=warehouse).aggregate(total=Sum('quantity'))['total'] or 0
        valuation_quantity = InventoryValuationSnapshot.objects.filter(
            warehouse=warehouse,
            inventory_date=selected_date,
            owner_company=current_company,
        ).aggregate(total=Sum('quantity'))['total'] or 0
        if planning_quantity == 0 and valuation_quantity == 0:
            cleanup_candidates.append(warehouse)
    ctx.update({
        'current_company': current_company,
        'selected_date': selected_date,
        'available_dates': available_inventory_dates(current_company),
        'import_logs': _recent_import_logs('valuation'),
        'warehouse_cleanup_candidates': cleanup_candidates,
    })
    ctx.update(variant_filter_context)
    return render(request, 'inventory/valuation_dashboard.html', ctx)

@require_POST
def create_product(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    try:
        code = _normalize_product_code(request.POST.get('code') or request.POST.get('product_code'))
        if not code:
            messages.error(request, "商品コードを入力してください。")
            return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))
        if Product.objects.filter(code=code).exists():
            messages.error(request, f"商品コードは既に登録されています: {code}")
            return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))
        product = Product.objects.create(
            code=code,
            name=(request.POST.get('name') or '商品名未設定').strip(),
            owner_company=current_company,
            demand_source=request.POST.get('demand_source', current_company),
            price=int(request.POST.get('price') or 0),
            supplier=(request.POST.get('supplier') or '').strip(),
            lead_time=int(request.POST.get('lead_time') or 90),
            order_lot=int(request.POST.get('order_lot') or 1),
            lot_rule=request.POST.get('lot_rule', 'ROUND_UP_LOT'),
            order_interval_days=max(0, int(request.POST.get('order_interval_days') or 0)),
            trend_days=int(request.POST.get('trend_days') or 90),
            is_excluded='is_excluded' in request.POST,
            is_discontinued='is_discontinued' in request.POST,
            allow_dead_order='allow_dead_order' in request.POST,
        )
        Inventory.objects.create(product=product, safety_stock=int(request.POST.get('safety_stock') or 20))
        _recalculate_abc_ranks(current_company)
        messages.success(request, f"商品を登録しました: {code}")
    except Exception as e:
        messages.error(request, f"商品登録エラー: {e}")
    return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))

@require_POST
def update_product_variant_planning_flag(request, variant_id):
    variant = get_object_or_404(ProductVariant, id=variant_id)
    variant.include_in_planning_inventory = 'include_in_planning_inventory' in request.POST
    variant.save(update_fields=['include_in_planning_inventory'])
    messages.success(
        request,
        f"状態SKU［{variant.product.code}-{variant.state_code}］の発注計画反映設定を更新しました。"
    )
    return redirect(request.META.get('HTTP_REFERER', 'valuation_dashboard'))

@require_POST
def bulk_update_product_variant_planning_flags(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    selected_date = parse_inventory_date(request.POST.get('inventory_date'), current_company)
    ctx = valuation_context(selected_date, current_company)
    variant_rows, _ = _filter_valuation_variant_rows(ctx['variant_summary'], request.POST)
    variant_ids = [row['product_variant_id'] for row in variant_rows if row.get('product_variant_id')]
    include_value = request.POST.get('bulk_action') == 'include'
    updated_count = 0
    if variant_ids:
        updated_count = ProductVariant.objects.filter(id__in=variant_ids).update(
            include_in_planning_inventory=include_value
        )
    action_label = '含める' if include_value else '除外'
    messages.success(request, f"表示中の状態SKU {updated_count} 件を発注計画反映「{action_label}」に更新しました。")
    redirect_url = (
        f"/valuation/?current_company={current_company}"
        f"&inventory_date={selected_date:%Y-%m-%d}"
        f"&state_code={request.POST.get('state_code', '')}"
        f"&state_name={request.POST.get('state_name', '')}"
        f"&planning_filter={request.POST.get('planning_filter', '')}"
        f"&variant_sort={request.POST.get('variant_sort', 'product_code')}"
        f"&variant_order={request.POST.get('variant_order', 'asc')}"
    )
    return redirect(redirect_url)

@require_POST
def update_product_config(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    try:
        if 'name' in request.POST:
            product.name = (request.POST.get('name') or product.name).strip()
        if 'price' in request.POST:
            product.price = int(request.POST.get('price') or 0)
        if 'supplier' in request.POST:
            product.supplier = (request.POST.get('supplier') or '').strip()
        product.lead_time = int(request.POST.get('lead_time', '90'))
        product.order_lot = int(request.POST.get('order_lot', '1'))
        product.lot_rule = request.POST.get('lot_rule', 'ROUND_UP_LOT')
        product.order_interval_days = max(0, int(request.POST.get('order_interval_days', '0')))
        product.trend_days = int(request.POST.get('trend_days', '90'))
        product.is_excluded = 'is_excluded' in request.POST
        product.is_discontinued = 'is_discontinued' in request.POST
        product.allow_dead_order = 'allow_dead_order' in request.POST
        product.demand_source = request.POST.get('demand_source', product.owner_company)
        product.save()
        inventory, _ = Inventory.objects.get_or_create(product=product)
        inventory.safety_stock = int(request.POST.get('safety_stock', '20'))
        inventory.save()
        _recalculate_abc_ranks(product.owner_company)
        messages.success(request, "A版発注設定を即時更新しました！")
    except Exception as e: messages.error(request, f"更新エラー: {e}")
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def bulk_update_products(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    product_ids = [pid for pid in request.POST.getlist('product_ids') if str(pid).isdigit()]
    products = Product.objects.filter(id__in=product_ids, owner_company=current_company)
    update_fields = []
    try:
        if not product_ids:
            messages.error(request, "一括更新する商品にチェックを入れてください。")
            return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))

        product_updates = {}
        if 'bulk_lead_time_enabled' in request.POST:
            product_updates['lead_time'] = int(request.POST.get('bulk_lead_time') or 0)
            update_fields.append('LT')
        if 'bulk_order_lot_enabled' in request.POST:
            product_updates['order_lot'] = max(1, int(request.POST.get('bulk_order_lot') or 1))
            update_fields.append('ロット')
        if 'bulk_order_interval_days_enabled' in request.POST:
            product_updates['order_interval_days'] = max(0, int(request.POST.get('bulk_order_interval_days') or 0))
            update_fields.append('発注間隔')
        if 'bulk_trend_days_enabled' in request.POST:
            product_updates['trend_days'] = int(request.POST.get('bulk_trend_days') or 90)
            update_fields.append('長期ベース')
        if 'bulk_lot_rule_enabled' in request.POST:
            product_updates['lot_rule'] = request.POST.get('bulk_lot_rule', 'ROUND_UP_LOT')
            update_fields.append('超過ルール')
        if 'bulk_demand_source_enabled' in request.POST:
            product_updates['demand_source'] = request.POST.get('bulk_demand_source', current_company)
            update_fields.append('需要参照元')
        if 'bulk_is_excluded_enabled' in request.POST:
            product_updates['is_excluded'] = request.POST.get('bulk_is_excluded') == 'true'
            update_fields.append('管理外')
        if 'bulk_is_discontinued_enabled' in request.POST:
            product_updates['is_discontinued'] = request.POST.get('bulk_is_discontinued') == 'true'
            update_fields.append('廃盤')
        if 'bulk_allow_dead_order_enabled' in request.POST:
            product_updates['allow_dead_order'] = request.POST.get('bulk_allow_dead_order') == 'true'
            update_fields.append('処分品発注')

        safety_stock_enabled = 'bulk_safety_stock_enabled' in request.POST
        if not product_updates and not safety_stock_enabled:
            messages.error(request, "一括更新する項目にチェックを入れてください。")
            return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))

        updated_count = products.update(**product_updates) if product_updates else products.count()

        if safety_stock_enabled:
            safety_stock = int(request.POST.get('bulk_safety_stock') or 0)
            for product in products:
                inventory, _ = Inventory.objects.get_or_create(product=product)
                inventory.safety_stock = safety_stock
                inventory.save(update_fields=['safety_stock', 'updated_at'])
            update_fields.append('安全在庫')

        _recalculate_abc_ranks(current_company)
        messages.success(request, f"チェックした商品 {updated_count} 件を一括更新しました（{', '.join(update_fields)}）。")
    except Exception as e:
        messages.error(request, f"一括更新エラー: {e}")
    return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))

def _normalize_state_code(raw_code):
    digit_code = ''.join(ch for ch in str(raw_code or '') if ch.isdigit())
    if not digit_code:
        return ''
    return digit_code[-3:].zfill(3)

@require_POST
def create_inventory_state(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    state_code = _normalize_state_code(request.POST.get('state_code'))
    state_name = (request.POST.get('state_name') or '').strip()
    if not state_code or not state_name:
        messages.error(request, "状態コードと状態名を入力してください。")
        return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))
    InventoryState.objects.update_or_create(
        state_code=state_code,
        defaults={'state_name': state_name},
    )
    updated_variants = ProductVariant.objects.filter(state_code=state_code).update(state_name=state_name)
    messages.success(request, f"状態コード {state_code} を登録/更新しました。状態別SKU更新: {updated_variants}件")
    return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))

@require_POST
def update_inventory_state(request, state_id):
    current_company = request.POST.get('current_company', 'IKUJI')
    state = get_object_or_404(InventoryState, id=state_id)
    state_name = (request.POST.get('state_name') or '').strip()
    if not state_name:
        messages.error(request, "状態名を入力してください。")
        return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))
    state.state_name = state_name
    state.save(update_fields=['state_name'])
    updated_variants = ProductVariant.objects.filter(state_code=state.state_code).update(state_name=state_name)
    messages.success(request, f"状態コード {state.state_code} を更新しました。状態別SKU更新: {updated_variants}件")
    return redirect(request.META.get('HTTP_REFERER', f'/product-master/?current_company={current_company}'))

@require_POST
def delete_product(request, product_id):
    get_object_or_404(Product, id=product_id).delete()
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def create_arrival_schedule(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    try:
        code = _normalize_product_code(request.POST.get('商品コード') or request.POST.get('product_code'))
        product = Product.objects.get(code=code, owner_company=current_company)
        arrival_date = _parse_csv_date(request.POST.get('arrival_date'))
        quantity, quantity_error = _parse_csv_quantity(request.POST.get('quantity'))
        status = request.POST.get('status', '確定')
        if status not in ['確定', '高確度', '希望']:
            status = '確定'
        if not arrival_date or quantity_error:
            messages.error(request, "入荷予定日または数量を確認してください。")
            return redirect(request.META.get('HTTP_REFERER', '/?current_company=' + current_company))

        arrival, created = ArrivalSchedule.objects.update_or_create(
            product=product,
            arrival_date=arrival_date,
            status=status,
            defaults={'quantity': quantity},
        )
        action = "追加" if created else "上書き更新"
        messages.success(request, f"入荷予定を{action}しました。")
    except Product.DoesNotExist:
        messages.error(request, f"商品コードが見つかりません: {request.POST.get('商品コード') or request.POST.get('product_code')}")
    except Exception as e:
        messages.error(request, f"入荷予定追加エラー: {e}")
    return redirect(request.META.get('HTTP_REFERER', '/?current_company=' + current_company))

@require_POST
def update_arrival_schedule(request, arrival_id):
    arrival = get_object_or_404(ArrivalSchedule, id=arrival_id)
    try:
        arrival.arrival_date = datetime.datetime.strptime(request.POST.get('arrival_date'), '%Y-%m-%d').date()
        arrival.quantity = int(request.POST.get('quantity', '0'))
        arrival.status = request.POST.get('status', '確定')
        arrival.save()
    except: pass
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def delete_arrival_schedule(request, arrival_id):
    get_object_or_404(ArrivalSchedule, id=arrival_id).delete()
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def create_shipment_schedule(request, product_id):
    current_company = request.POST.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    product = get_object_or_404(Product, id=product_id, owner_company=current_company)
    try:
        shipment_date = datetime.datetime.strptime(request.POST.get('shipment_date', ''), '%Y-%m-%d').date()
        destination = (request.POST.get('destination') or '').strip()
        quantity = int(request.POST.get('quantity') or 0)
        if not destination:
            raise ValueError('向け先を入力してください。')
        if quantity <= 0:
            raise ValueError('数量は1以上で入力してください。')
        ShipmentSchedule.objects.create(
            product=product,
            shipment_date=shipment_date,
            destination=destination,
            quantity=quantity,
        )
        messages.success(request, f"商品［{product.code}］の先付け受注を登録しました。")
    except ValueError as exc:
        messages.error(request, f"先付け受注の登録エラー: {exc}")
    return redirect(request.META.get('HTTP_REFERER', f'/?current_company={current_company}'))

@require_POST
def delete_shipment_schedule(request, shipment_id):
    current_company = request.POST.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    shipment = get_object_or_404(ShipmentSchedule, id=shipment_id, product__owner_company=current_company)
    product_code = shipment.product.code
    shipment.delete()
    messages.info(request, f"商品［{product_code}］の先付け受注を削除しました。")
    return redirect(request.META.get('HTTP_REFERER', f'/?current_company={current_company}'))

@require_POST
def create_order_plan(request, product_id):
    product = get_object_or_404(Product, id=product_id); inventory = Inventory.objects.get(product=product)
    base_date = _get_planning_base_date(product.owner_company); future_end_date = base_date + timedelta(days=120)
    sales_summary = SalesHistory.objects.filter(is_advance_order=False, sold_date__gte=base_date - timedelta(days=180), sold_date__lte=base_date).values('product_id', 'company').annotate(sum_30=Sum(Case(When(sold_date__gte=base_date-timedelta(days=30), then='quantity'), default=0, output_field=IntegerField())), sum_long=Sum(Case(When(sold_date__gte=base_date-timedelta(days=product.trend_days), then='quantity'), default=0, output_field=IntegerField())))
    s_map_ikuji, s_map_select = {}, {}
    for item in sales_summary:
        if item['company'] == 'IKUJI': s_map_ikuji[item['product_id']] = item
        else: s_map_select[item['product_id']] = item
    ship_map = {s['product_id']: {s['shipment_date']: s['total']} for s in ShipmentSchedule.objects.filter(shipment_date__gte=base_date, shipment_date__lte=future_end_date).values('product_id', 'shipment_date').annotate(total=Sum('quantity'))}
    arr_map = _build_arrival_map(ArrivalSchedule.objects.filter(arrival_date__gte=base_date, arrival_date__lte=future_end_date))
    existing_order_map = _build_order_arrival_map(Order.objects.filter(product=product, status__in=['計画中', '発注済']))
    created_count = _execute_single_order_plan(product, inventory, base_date, future_end_date, s_map_select, s_map_ikuji, ship_map, arr_map, existing_order_map)
    if created_count: messages.success(request, f"商品［{product.code}］の発注計画を {created_count} 件作成しました。")
    else: messages.info(request, "発注基準を満たしていないためスキップしました。")
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def bulk_create_order_plan(request):
    current_company = request.POST.get('current_company', 'IKUJI'); selected_pids = request.POST.getlist('selected_products')
    if not selected_pids:
        messages.warning(request, "商品が選択されていません。")
        return redirect('/?current_company=' + current_company)
    base_date = _get_planning_base_date(current_company); future_end_date = base_date + timedelta(days=120)
    products = list(Product.objects.filter(id__in=selected_pids, owner_company=current_company))
    if not products:
        messages.warning(request, "選択した商品は現在の会社の発注対象ではありません。")
        return redirect('/?current_company=' + current_company)

    # 商品ごとの長期ベース日数に合わせて、90〜180日の集計から必要な販売数を選ぶ。
    sales_summary = SalesHistory.objects.filter(is_advance_order=False, sold_date__gte=base_date - timedelta(days=180), sold_date__lte=base_date).values('product_id', 'company').annotate(
        sum_30=Sum(Case(When(sold_date__gte=base_date-timedelta(days=30), then='quantity'), default=0, output_field=IntegerField())),
        sum_90=Sum(Case(When(sold_date__gte=base_date-timedelta(days=90), then='quantity'), default=0, output_field=IntegerField())),
        sum_120=Sum(Case(When(sold_date__gte=base_date-timedelta(days=120), then='quantity'), default=0, output_field=IntegerField())),
        sum_150=Sum(Case(When(sold_date__gte=base_date-timedelta(days=150), then='quantity'), default=0, output_field=IntegerField())),
        sum_180=Sum(Case(When(sold_date__gte=base_date-timedelta(days=180), then='quantity'), default=0, output_field=IntegerField())),
    )
    products_by_id = {product.id: product for product in products}
    s_map_ikuji, s_map_select = {}, {}
    for item in sales_summary:
        product = products_by_id.get(item['product_id'])
        if not product:
            continue
        trend_days = product.trend_days if product.trend_days in [90, 120, 150, 180] else 90
        item['sum_long'] = item.get(f'sum_{trend_days}', 0) or 0
        if item['company'] == 'IKUJI': s_map_ikuji[item['product_id']] = item
        else: s_map_select[item['product_id']] = item
    ship_map, arr_map = {}, {}
    for s in ShipmentSchedule.objects.filter(shipment_date__gte=base_date, shipment_date__lte=future_end_date): ship_map.setdefault(s.product_id, {})[s.shipment_date] = ship_map.setdefault(s.product_id, {}).get(s.shipment_date, 0) + s.quantity
    arr_map = _build_arrival_map(ArrivalSchedule.objects.filter(arrival_date__gte=base_date, arrival_date__lte=future_end_date))
    existing_order_map = _build_order_arrival_map(Order.objects.filter(product_id__in=products_by_id, status__in=['計画中', '発注済']))
    inventories = {inv.product_id: inv for inv in Inventory.objects.filter(product_id__in=products_by_id)}
    created_count = 0
    for p in products:
        inv = inventories.get(p.id)
        if inv:
            created_count += _execute_single_order_plan(p, inv, base_date, future_end_date, s_map_select, s_map_ikuji, ship_map, arr_map, existing_order_map)
    messages.success(request, f"一括発注計算完了！（{created_count}件の計画を新規生成）")
    return redirect('/?current_company=' + current_company)

@require_POST
def update_order_status(request, order_id):
    order = get_object_or_404(Order, id=order_id); ns = request.POST.get('status')
    if ns in ['計画中', '発注済', '入庫済']: order.status = ns; order.save()
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def delete_order_plan(request, order_id):
    get_object_or_404(Order, id=order_id).delete()
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

def operation_guide(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    return render(request, 'inventory/guide.html', {'current_company': current_company})

def import_inventory_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            reader = _get_csv_reader(request.FILES['csv_file']); headers = reader.fieldnames
            if not headers or '商品コード' not in headers: return redirect('/?current_company=' + current_company)
            warehouse_headers = [wh_name for wh_name in headers if wh_name != '商品コード']
            wh_map = {}
            for source_warehouse_name in warehouse_headers:
                wh_name = normalize_warehouse_name(source_warehouse_name)
                warehouse, _ = Warehouse.objects.get_or_create(
                    name=wh_name,
                    owner_company=current_company,
                    defaults={'is_transit': "移動中" in wh_name},
                )
                if not warehouse.is_active:
                    warehouse.is_active = True
                    warehouse.save(update_fields=['is_active'])
                wh_map[source_warehouse_name] = warehouse
            products = {p.code: p for p in Product.objects.filter(owner_company=current_company)}
            try: selected_date = datetime.datetime.strptime(request.POST.get('inventory_date', ''), '%Y-%m-%d').date()
            except: selected_date = datetime.date.today()
            row_count, unknown_count, quantity_error_count = 0, 0, 0
            seen_codes = set()
            error_samples = []
            for row in reader:
                code = _normalize_inventory_product_code(row.get('商品コード', ''))
                if not code: continue
                if code not in products:
                    unknown_count += 1
                    if len(error_samples) < 20:
                        error_samples.append(f"商品未登録: {code}")
                    continue
                seen_codes.add(code)
                p_obj, tot = products[code], 0
                WarehouseInventory.objects.filter(product=p_obj, warehouse__owner_company=current_company).delete()
                for w_name, w_obj in wh_map.items():
                    qty, had_error = _parse_inventory_quantity(row.get(w_name, '0'))
                    if had_error:
                        quantity_error_count += 1
                        if len(error_samples) < 20:
                            error_samples.append(f"数量エラー: 商品{code} / {w_name} / 値={row.get(w_name, '')}")
                    WarehouseInventory.objects.create(product=p_obj, warehouse=w_obj, quantity=qty)
                    tot += qty
                Inventory.objects.update_or_create(product=p_obj, defaults={'current_quantity': tot, 'inventory_date': selected_date})
                row_count += 1
            _recalculate_abc_ranks(current_company)
            unchanged_count = max(len(products) - len(seen_codes), 0)
            issue_count = unknown_count + quantity_error_count
            _log_import(
                'planning',
                '在庫CSV',
                _status_from_counts(issue_count),
                f"更新: {row_count}件 / 未更新: {unchanged_count}件 / 商品未登録: {unknown_count}件 / 数量エラー: {quantity_error_count}件",
                request=request,
                company=current_company,
                details="\n".join(error_samples),
                error_count=issue_count,
            )
            messages.success(
                request,
                f"実在庫を商品単位で洗替し再評価しました！ "
                f"(更新: {row_count}件 / 未更新: {unchanged_count}件 / 商品未登録: {unknown_count}件 / 数量エラー: {quantity_error_count}件)"
            )
        except Exception as e:
            _log_import('planning', '在庫CSV', 'error', f"取込失敗: {e}", request=request, company=current_company, error_count=1)
            messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def download_csv_template(request, template_type):
    current_company = request.GET.get('current_company', 'IKUJI'); scode = "0040006" if current_company == 'SELECT' else "5100299"
    if template_type == 'inventory':
        current_warehouses = Warehouse.objects.filter(owner_company=current_company, is_active=True)
        cols = ",".join([wh.name for wh in current_warehouses]) if current_warehouses else "ペットセレクト倉庫,ペットセレクト移動中" if current_company == 'SELECT' else "ﾆﾁｲｸ物流,岸和田倉庫,西松屋,西松屋移動中"
        ccnt = current_warehouses.count() if current_warehouses else 2 if current_company == 'SELECT' else 4
        content = f"商品コード,{cols}\n{scode},{','.join(['0']*ccnt)}\n"
    else:
        templates = {
            'products': (
                "共通商品・A版発注設定一覧表,雛形\n"
                "《コード順》\n"
                "コード,JANｺｰﾄﾞ(1),商品名,分類,分類,分類グループ,分類グループ,固定原価,容量,関税,仕入先,廃盤フラグ\n"
                ",,,,,,,,,,,\n"
                f"{scode}001,,サンプル商品名称,000,サンプル分類,00,サンプル分類グループ,1000,  0.0000,  0.0 %,000001,FALSE\n"
            ),
            'sales': (
                "\"売上明細表,雛形\",,,,,,,,,\n"
                "\"※ 必須列: 伝票日付、得意先名、商品コード（2つ目のコード）、商品名、区分、数量、税抜金額、粗利金額。その他の列は無視して取り込みます。\",,,,,,,,,\n"
                "\"【日付期間：2026年 6月 1日 ～ 2026年 6月 1日】\",,,,,,,,,\n"
                "コード,得意先名,伝票日付,区分,仕入先,コード,商品名,数量,税抜金額,粗利金額\n"
                f"000013,サンプル得意先,2026年 6月 1日,売上,000001,{scode}001,サンプル商品名称,6,7920,3300\n"
            ),
            'arrivals': f"商品コード,入荷予定日,入荷予定数量,確度ステータス\n{scode},2026/06/15,100,確定\n"
        }
        content = templates.get(template_type, "")
    response = HttpResponse(content.encode('cp932', errors='replace'), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="template_{template_type}.csv"'; return response

def import_products_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            stats = import_uploaded_product_file(request.FILES['csv_file'], current_company=current_company)
            _recalculate_abc_ranks(current_company)
            issue_count = stats['duplicate_variants'] + stats['skipped']
            _log_import(
                'product_master',
                '商品マスタCSV',
                _status_from_counts(issue_count),
                f"取込対象: {stats['imported']}件 / 状態違い重複スキップ: {stats['duplicate_variants']}件 / その他スキップ: {stats['skipped']}件",
                request=request,
                company=current_company,
                error_count=issue_count,
            )
            messages.success(
                request,
                f"共通商品・A版発注設定の登録完了！ 取込対象: {stats['imported']}件 / "
                f"状態違い重複スキップ: {stats['duplicate_variants']}件 / "
                f"その他スキップ: {stats['skipped']}件"
            )
        except Exception as e:
            _log_import('product_master', '商品マスタCSV', 'error', f"取込失敗: {e}", request=request, company=current_company, error_count=1)
            messages.error(request, f"エラー: {e}")
    return redirect(request.META.get('HTTP_REFERER', '/product-master/?current_company=' + current_company))

def import_sales_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            stats = import_uploaded_sales_file(request.FILES['csv_file'], current_company=current_company)
            _recalculate_abc_ranks(current_company)
            issue_count = stats['missing_products'] + stats['skipped']
            import_log = _log_import(
                'sales_history',
                '販売履歴CSV',
                _status_from_counts(issue_count),
                f"集計行: {stats['aggregated']}件 / 新規: {stats['created']}件 / 更新: {stats['updated']}件 / 商品自動登録: {stats['auto_created_products']}件 / 共通マスタ利用: {stats['shared_master_products']}件 / その他スキップ: {stats['skipped']}件",
                request=request,
                company=current_company,
                error_count=issue_count,
            )
            SalesImportSkip.objects.bulk_create([
                SalesImportSkip(import_log=import_log, **skip_row)
                for skip_row in stats['skip_rows']
            ])
            messages.success(
                request,
                f"販売履歴の集計更新完了（集計行: {stats['aggregated']}件 / "
                f"新規: {stats['created']}件 / 更新: {stats['updated']}件 / "
                f"商品自動登録: {stats['auto_created_products']}件 / "
                f"共通マスタ利用: {stats['shared_master_products']}件 / "
                f"その他スキップ: {stats['skipped']}件）"
            )
        except Exception as e:
            _log_import('sales_history', '販売履歴CSV', 'error', f"取込失敗: {e}", request=request, company=current_company, error_count=1)
            messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)


def download_sales_import_skips(request, import_log_id):
    current_company = request.GET.get('current_company', 'IKUJI')
    import_log = get_object_or_404(
        ImportLog,
        id=import_log_id,
        dashboard='sales_history',
        company=current_company,
    )
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="sales_import_skips_{current_company}_{import_log.created_at:%Y%m%d_%H%M%S}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        '元データ行', 'スキップ理由', '伝票日付', '得意先コード', '元商品コード',
        '正規化商品コード', '商品名', '区分', '数量', '税抜金額', '粗利金額',
    ])
    for row in import_log.sales_skips.all():
        writer.writerow([
            row.source_rows, row.reason, row.sold_date_text, row.customer_code,
            row.source_product_code, row.normalized_product_code, row.product_name,
            row.sales_category, row.quantity_text, row.tax_excluded_amount_text,
            row.gross_profit_amount_text,
        ])
    response.charset = 'cp932'
    response.content = response.content.decode('utf-8').encode('cp932', errors='replace')
    return response

def import_arrivals_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            reader = _get_csv_reader(request.FILES['csv_file'])
            p_dict = {p.code: p for p in Product.objects.all()}
            aggregated = {}
            company_counts = {company_code: 0 for company_code, _ in Product.COMPANY_CHOICES}
            unknown_count, error_count, status_fixed_count = 0, 0, 0
            error_samples = []
            for row in reader:
                code = _normalize_product_code(row.get('商品コード', ''))
                if not code: continue
                if code not in p_dict:
                    unknown_count += 1
                    if len(error_samples) < 20:
                        error_samples.append(f"商品未登録: {code} / {row.get('商品名', '')}")
                    continue
                arrival_date_value = _pick_first_value(row, ['入荷予定日', '予定日', '入荷日', '入港日'])
                quantity_value = _pick_first_value(row, ['入荷予定数量', '入荷数量', '数量'])
                ad = _parse_csv_date(arrival_date_value)
                qty, quantity_error = _parse_csv_quantity(quantity_value)
                if not ad or quantity_error:
                    error_count += 1
                    if len(error_samples) < 20:
                        error_samples.append(f"日付数量エラー: 商品{code} / 日付={arrival_date_value} / 数量={quantity_value}")
                    continue
                status_value = _pick_first_value(row, ['確度ステータス', '確度', '決定'], default='確定')
                status, status_was_fixed = _normalize_arrival_status(status_value)
                if status_was_fixed:
                    status_fixed_count += 1
                key = (p_dict[code].id, ad, status)
                aggregated[key] = aggregated.get(key, 0) + qty

            if not aggregated:
                _log_import(
                    'arrivals',
                    '入荷予定CSV',
                    'error',
                    f"有効な入荷予定なし / 商品未登録: {unknown_count}件 / 日付数量エラー: {error_count}件",
                    request=request,
                    company='ALL',
                    details="\n".join(error_samples),
                    error_count=unknown_count + error_count,
                )
                messages.error(
                    request,
                    f"有効な入荷予定がありません。既存データは変更していません。"
                    f"（商品未登録: {unknown_count}件 / 日付数量エラー: {error_count}件）"
                )
                return redirect(request.META.get('HTTP_REFERER', '/arrivals/?current_company=' + current_company))

            ArrivalSchedule.objects.filter(product__owner_company__in=[company_code for company_code, _ in Product.COMPANY_CHOICES]).delete()
            products_by_id = {p.id: p for p in p_dict.values()}
            create_list = [
                ArrivalSchedule(product=products_by_id[product_id], arrival_date=ad, quantity=qty, status=status)
                for (product_id, ad, status), qty in aggregated.items()
            ]
            for schedule in create_list:
                company_counts[schedule.product.owner_company] = company_counts.get(schedule.product.owner_company, 0) + 1
            ArrivalSchedule.objects.bulk_create(create_list)
            company_summary = " / ".join(
                f"{label}: {company_counts.get(company_code, 0)}件"
                for company_code, label in Product.COMPANY_CHOICES
            )
            issue_count = unknown_count + error_count
            _log_import(
                'arrivals',
                '入荷予定CSV',
                _status_from_counts(issue_count),
                f"登録: {len(create_list)}件 / 商品未登録: {unknown_count}件 / 日付数量エラー: {error_count}件 / ステータス補正: {status_fixed_count}件 / {company_summary}",
                request=request,
                company='ALL',
                details="\n".join(error_samples),
                error_count=issue_count,
                warning_count=status_fixed_count,
            )
            messages.success(
                request,
                f"入荷予定を両社へ自動振分して洗替更新しました！"
                f"（登録: {len(create_list)}件 / 商品未登録: {unknown_count}件 / "
                f"日付数量エラー: {error_count}件 / ステータス補正: {status_fixed_count}件 / "
                f"{company_summary}）"
            )
        except Exception as e:
            _log_import('arrivals', '入荷予定CSV', 'error', f"取込失敗: {e}", request=request, company='ALL', error_count=1)
            messages.error(request, f"エラー: {e}")
    return redirect(request.META.get('HTTP_REFERER', '/arrivals/?current_company=' + current_company))

def product_list(request): return render(request, 'inventory/product_list.html', {'products': Product.objects.all().order_by('code')})

def export_products_csv(request):
    cc = request.GET.get('current_company', 'IKUJI'); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="products_{cc}.csv"'
    buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['商品コード', '商品名', '標準原価', '仕入先名', 'リードタイム', '発注ロット', '超過時ルール', '長期トレンド日数', '管理外フラグ', '廃盤フラグ'])
    for p in Product.objects.filter(owner_company=cc): writer.writerow([p.code, p.name, p.price, p.supplier, p.lead_time, p.order_lot, p.lot_rule, p.trend_days, p.is_excluded, p.is_discontinued])
    res.write(buf.getvalue().encode('cp932', errors='replace')); return res

def export_inventory_csv(request):
    cc = request.GET.get('current_company', 'IKUJI'); whs = Warehouse.objects.filter(owner_company=cc, is_active=True); invs = Inventory.objects.select_related('product').filter(product__owner_company=cc)
    w_map = {}
    for r in WarehouseInventory.objects.filter(warehouse__owner_company=cc, warehouse__is_active=True): w_map.setdefault(r.product_id, {})[r.warehouse_id] = r.quantity
    res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="inventory_{cc}.csv"'
    buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['商品コード', '商品名', '現在庫数（合算）', '安全在庫数'] + [w.name for w in whs])
    for i in invs:
        row = [i.product.code, i.product.name, i.current_quantity, i.safety_stock]
        for w in whs: row.append(w_map.get(i.product.id, {}).get(w.id, 0))
        writer.writerow(row)
    res.write(buf.getvalue().encode('cp932', errors='replace')); return res

def export_sales_csv(request):
    cc = request.GET.get('current_company', 'IKUJI'); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="sales_{cc}.csv"'
    buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['伝票日付', '得意先コード', '商品コード', '区分', '販売数', '税抜金額', '粗利金額'])
    for s in SalesHistory.objects.select_related('product').filter(company=cc): writer.writerow([s.sold_date.strftime('%Y/%m/%d'), s.customer, s.product.code, s.sales_category, s.quantity, s.tax_excluded_amount, s.gross_profit_amount])
    res.write(buf.getvalue().encode('cp932', errors='replace')); return res

def export_arrivals_csv(request):
    cc = request.GET.get('current_company', 'IKUJI'); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="arrivals_{cc}.csv"'
    buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['商品コード', '商品名', '入荷予定日', '入荷予定数量', '確度ステータス'])
    for a in ArrivalSchedule.objects.select_related('product').filter(product__owner_company=cc): writer.writerow([a.product.code, a.product.name, a.arrival_date.strftime('%Y/%m/%d'), a.quantity, a.status])
    res.write(buf.getvalue().encode('cp932', errors='replace')); return res

def export_inventory_states_csv(request):
    res = HttpResponse(content_type='text/csv')
    res['Content-Disposition'] = 'attachment; filename="inventory_states.csv"'
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['状態コード', '状態名'])
    for state in InventoryState.objects.order_by('state_code'):
        writer.writerow([state.state_code, state.state_name])
    res.write(buf.getvalue().encode('cp932', errors='replace'))
    return res

def download_inventory_state_template(request):
    res = HttpResponse(content_type='text/csv')
    res['Content-Disposition'] = 'attachment; filename="template_inventory_states.csv"'
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['状態コード', '状態名'])
    writer.writerow(['001', 'A品'])
    writer.writerow(['991', 'B品'])
    res.write(buf.getvalue().encode('cp932', errors='replace'))
    return res

def import_valuation_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            inventory_date = parse_inventory_date(request.POST.get('inventory_date'), current_company)
            stats = import_valuation_snapshot(request.FILES['csv_file'], inventory_date, current_company)
            issue_count = stats['skipped'] + stats['quantity_errors']
            _log_import(
                'valuation',
                '棚卸資産評価CSV',
                _status_from_counts(issue_count),
                f"明細: {stats['snapshots']}件 / 状態SKU: {stats['variants']}件 / 倉庫: {stats['warehouses']}件 / ペットセレクト資産振替: {stats['select_asset_rows']}件 / スキップ: {stats['skipped']}件 / 数量エラー: {stats['quantity_errors']}件",
                request=request,
                company=current_company,
                error_count=issue_count,
            )
            messages.success(
                request,
                f"棚卸資産評価を登録しました。明細: {stats['snapshots']}件 / 状態SKU: {stats['variants']}件 / "
                f"倉庫: {stats['warehouses']}件 / ペットセレクト資産振替: {stats['select_asset_rows']}件 / "
                f"スキップ: {stats['skipped']}件 / 数量エラー: {stats['quantity_errors']}件"
            )
            return redirect(f'/valuation/?current_company={current_company}&inventory_date={inventory_date:%Y-%m-%d}')
        except Exception as e:
            _log_import('valuation', '棚卸資産評価CSV', 'error', f"取込失敗: {e}", request=request, company=current_company, error_count=1)
            messages.error(request, f"棚卸資産評価登録エラー: {e}")
    return redirect('/valuation/?current_company=' + current_company)

def import_inventory_state_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            stats = import_inventory_state_master(request.FILES['csv_file'])
            _log_import(
                'valuation',
                '状態マスタCSV',
                _status_from_counts(stats['skipped']),
                f"登録/更新: {stats['imported']}件 / スキップ: {stats['skipped']}件 / 状態別SKU更新: {stats.get('variants_updated', 0)}件",
                request=request,
                company='ALL',
                error_count=stats['skipped'],
            )
            messages.success(
                request,
                f"在庫状態マスタを登録しました。登録/更新: {stats['imported']}件 / "
                f"スキップ: {stats['skipped']}件 / 状態別SKU更新: {stats.get('variants_updated', 0)}件"
            )
        except Exception as e:
            _log_import('valuation', '状態マスタCSV', 'error', f"取込失敗: {e}", request=request, company='ALL', error_count=1)
            messages.error(request, f"在庫状態マスタ登録エラー: {e}")
    return redirect(request.META.get('HTTP_REFERER', '/valuation/?current_company=' + current_company))

@require_POST
def sync_valuation_to_planning(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    inventory_date = parse_inventory_date(request.POST.get('inventory_date'), current_company)
    stats = sync_snapshot_to_planning_inventory(inventory_date, current_company)
    messages.success(
        request,
        f"棚卸資産評価から発注計画へ在庫数量を反映しました。商品: {stats['products']}件 / "
        f"倉庫別: {stats['warehouse_rows']}件 / 発注計画除外明細: {stats['excluded_snapshots']}件"
    )
    return redirect(f'/valuation/?current_company={current_company}&inventory_date={inventory_date:%Y-%m-%d}')

def export_valuation_excel(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    inventory_date = parse_inventory_date(request.GET.get('inventory_date'), current_company)
    ctx = valuation_context(inventory_date, current_company)
    rows = []
    rows.append('<html><head><meta charset="utf-8"></head><body>')
    rows.append(f'<h2>棚卸資産評価報告書 {inventory_date:%Y-%m-%d}</h2>')
    rows.append(f'<p>会社: {current_company} / 数量合計: {ctx["total_quantity"]} / 金額合計: {ctx["total_amount"]}</p>')
    rows.append('<h3>倉庫別</h3><table border="1"><tr><th>倉庫</th><th>数量</th><th>在庫金額</th></tr>')
    for row in ctx['warehouse_summary']:
        rows.append(f'<tr><td>{row["warehouse__name"]}</td><td>{row["quantity"] or 0}</td><td>{row["amount"] or 0}</td></tr>')
    rows.append('</table><h3>状態別明細</h3><table border="1"><tr><th>商品コード</th><th>商品名</th><th>状態</th><th>状態名</th><th>原価</th><th>数量</th><th>在庫金額</th></tr>')
    for row in ctx['variant_summary']:
        rows.append(
            f'<tr><td>{row["product_variant__product__code"]}</td><td>{row["product_variant__product__name"]}</td>'
            f'<td>{row["product_variant__state_code"]}</td><td>{row["product_variant__state_name"]}</td>'
            f'<td>{row["unit_cost"]}</td><td>{row["quantity"] or 0}</td><td>{row["amount"] or 0}</td></tr>'
        )
    rows.append('</table></body></html>')
    response = HttpResponse('\n'.join(rows).encode('utf-8'), content_type='application/vnd.ms-excel; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="valuation_{current_company}_{inventory_date:%Y%m%d}.xls"'
    return response

def download_valuation_template(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    default_warehouses = (
        ['ペットセレクト倉庫', 'ペットセレクト移動中']
        if current_company == 'SELECT'
        else ['ニチイク物流', '岸和田倉庫', 'PET SELECT', '西松屋', '本社ショールーム', '東京ショールーム', 'B品(ニチイク)', 'B品(岸和田)', '廃棄']
    )
    warehouses = list(Warehouse.objects.filter(owner_company=current_company, is_active=True).values_list('name', flat=True)) or default_warehouses
    sample_code = '0040007001' if current_company == 'IKUJI' else '0040006001'
    sample = [sample_code, 'サンプル商品名称', 'A品', '1000'] + ['0'] * len(warehouses)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['商品コード', '商品名', '状態名', '状態別評価原価'] + warehouses)
    writer.writerow(sample)
    response = HttpResponse(buf.getvalue().encode('cp932', errors='replace'), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="template_valuation_{current_company}.csv"'
    return response

def _simple_pdf_bytes(lines):
    escaped = [line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)') for line in lines]
    text_ops = ['BT /F1 10 Tf 40 800 Td']
    for idx, line in enumerate(escaped[:55]):
        if idx == 0:
            text_ops.append(f'({line}) Tj')
        else:
            text_ops.append(f'0 -14 Td ({line}) Tj')
    text_ops.append('ET')
    stream = '\n'.join(text_ops).encode('latin-1', errors='replace')
    objects = [
        b'1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj',
        b'2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj',
        b'3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj',
        b'4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj',
        b'5 0 obj << /Length ' + str(len(stream)).encode('ascii') + b' >> stream\n' + stream + b'\nendstream endobj',
    ]
    pdf = [b'%PDF-1.4\n']
    offsets = []
    for obj in objects:
        offsets.append(sum(len(part) for part in pdf))
        pdf.append(obj + b'\n')
    xref_pos = sum(len(part) for part in pdf)
    pdf.append(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode('ascii'))
    for offset in offsets:
        pdf.append(f'{offset:010d} 00000 n \n'.encode('ascii'))
    pdf.append(f'trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF'.encode('ascii'))
    return b''.join(pdf)

def export_valuation_pdf(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    inventory_date = parse_inventory_date(request.GET.get('inventory_date'), current_company)
    ctx = valuation_context(inventory_date, current_company)
    lines = [
        f'Inventory Valuation Report {inventory_date:%Y-%m-%d}',
        f'Company: {current_company}',
        f'Total quantity: {ctx["total_quantity"]}',
        f'Total amount: {ctx["total_amount"]}',
        '',
        'Warehouse summary',
    ]
    for row in ctx['warehouse_summary']:
        lines.append(f'{row["warehouse__name"]}: qty {row["quantity"] or 0}, amount {row["amount"] or 0}')
    lines.append('')
    lines.append('Variant summary')
    for row in list(ctx['variant_summary'])[:35]:
        lines.append(
            f'{row["product_variant__product__code"]}-{row["product_variant__state_code"]} '
            f'qty {row["quantity"] or 0} cost {row["unit_cost"]} amount {row["amount"] or 0}'
        )
    response = HttpResponse(_simple_pdf_bytes(lines), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="valuation_{current_company}_{inventory_date:%Y%m%d}.pdf"'
    return response

@require_POST
def delete_sales_history_period(request):
    cc = request.POST.get('current_company', 'IKUJI')
    try:
        sd = datetime.datetime.strptime(request.POST.get('start_date'), '%Y-%m-%d').date()
        ed = datetime.datetime.strptime(request.POST.get('end_date'), '%Y-%m-%d').date()
        cnt, _ = SalesHistory.objects.filter(company=cc, sold_date__gte=sd, sold_date__lte=ed).delete()
        _recalculate_abc_ranks(cc)
        messages.info(request, f"{request.POST.get('start_date')}～{request.POST.get('end_date')}の販売履歴を {cnt} 件削除しました。")
    except: pass
    return redirect('/?current_company=' + cc)

@require_POST
def delete_warehouse(request, warehouse_id):
    current_company = request.POST.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']:
        current_company = 'IKUJI'
    warehouse = get_object_or_404(Warehouse, id=warehouse_id, owner_company=current_company)
    inventory_date = parse_inventory_date(request.POST.get('inventory_date'), current_company)
    planning_quantity = WarehouseInventory.objects.filter(warehouse=warehouse).aggregate(total=Sum('quantity'))['total'] or 0
    valuation_quantity = InventoryValuationSnapshot.objects.filter(
        warehouse=warehouse,
        inventory_date=inventory_date,
        owner_company=current_company,
    ).aggregate(total=Sum('quantity'))['total'] or 0
    if planning_quantity != 0 or valuation_quantity != 0:
        messages.error(request, f"倉庫［{warehouse.name}］には在庫が残っているため、一覧から削除できません。")
    else:
        warehouse.is_active = False
        warehouse.save(update_fields=['is_active'])
        messages.success(request, f"倉庫［{warehouse.name}］を一覧と棚卸CSV雛形から削除しました。過去の棚卸履歴は保持されています。")
    return redirect(f'/valuation/?current_company={current_company}&inventory_date={inventory_date:%Y-%m-%d}')

def about_app(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    return render(request, 'inventory/about.html', {'current_company': current_company})
