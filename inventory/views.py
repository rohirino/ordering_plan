import datetime
import math
import io
import csv
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.http import HttpResponse
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .models import Product, Inventory, Warehouse, WarehouseInventory, SalesHistory, Order, ShipmentSchedule, ArrivalSchedule
from django.db.models import Sum, Case, When, IntegerField, Q
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
        cleaned_headers = [h.strip() for h in next(csv.reader([lines[0]]))]
        return csv.DictReader(io.StringIO("\n".join(lines[1:])), fieldnames=cleaned_headers)
    return csv.DictReader(io.StringIO(decoded_text))

def _recalculate_abc_ranks(target_company='IKUJI'):
    """【現実的改修】各商品の個別設定「長期トレンド日数」に100%自動連動してABCを評価する"""
    base_date = datetime.date(2026, 6, 1)
    
    # 1. 会社に所属する全マスタをロード
    products = Product.objects.filter(owner_company=target_company)
    
    # 2. 期間ごとの売上（90, 120, 150, 180日）をデータベースから一括アノテーション取得（高速化）
    sales_summary = SalesHistory.objects.filter(
        company=target_company, sold_date__gte=base_date - timedelta(days=180), sold_date__lte=base_date
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

def _execute_single_order_plan(product, inventory, base_date, future_end_date, sales_map_select, sales_map_ikuji, ship_map, arr_map):
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
    running_stock = inventory.current_quantity
    min_stock = running_stock
    for i in range(120):
        d = base_date + timedelta(days=i)
        day_ship = product_ships.get(d, 0)
        arr_total = product_arrivals.get(d, {}).get('確定', 0) + product_arrivals.get(d, {}).get('高確度', 0)
        running_stock = running_stock + arr_total - max(day_ship, daily_demand)
        if running_stock < min_stock: min_stock = running_stock
    shortage = order_point - min_stock if min_stock < order_point else 0
    if shortage > 0:
        lot = product.order_lot if product.order_lot > 0 else 1
        qty = max(lot, math.ceil(shortage)) if product.lot_rule == 'MIN_LOT_ONLY' else math.ceil(shortage / lot) * lot
        Order.objects.create(product=product, quantity=qty, status='計画中')
        return True
    return False

def planning_dashboard(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    if current_company not in ['IKUJI', 'SELECT']: current_company = 'IKUJI'
    if current_company == 'SELECT': Product.objects.filter(owner_company='SELECT', demand_source='IKUJI').update(demand_source='SELECT')
    _recalculate_abc_ranks(current_company)
    ikuji_warehouses = Warehouse.objects.filter(owner_company='IKUJI')
    shared_wh_ids = [int(x) for x in request.GET.getlist('shared_whs') if x.isdigit()]
    active_filter = request.GET.get('active_filter', 'active')
    if active_filter not in ['active', 'all']: active_filter = 'active'
    active_months = request.GET.get('active_months', '12')
    try: months_val = int(active_months)
    except ValueError: months_val = 12
    search_query = request.GET.get('search_query', '').strip()
    abc_filter = request.GET.get('abc_filter', '').strip()
    supplier_code = request.GET.get('supplier_code', '').strip()
    base_date = datetime.date(2026, 6, 1)
    thirty_days_ago = base_date - timedelta(days=30)
    sixty_days_ago = base_date - timedelta(days=60)
    long_check_date = base_date - timedelta(days=months_val * 30)
    sales_summary = SalesHistory.objects.filter(sold_date__gte=base_date - timedelta(days=180), sold_date__lte=base_date).values('product_id', 'company').annotate(
        sum_30=Sum(Case(When(sold_date__gte=thirty_days_ago, then='quantity'), default=0, output_field=IntegerField())),
        sum_60=Sum(Case(When(sold_date__gte=sixty_days_ago, then='quantity'), default=0, output_field=IntegerField())),
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
    if abc_filter in ['A', 'B', 'C', 'DEAD']: inventories = inventories.filter(product__abc_rank=abc_filter)
    if supplier_code: inventories = inventories.filter(product__code__startswith=supplier_code)
    if search_query: inventories = inventories.filter(Q(product__code__icontains=search_query) | Q(product__name__icontains=search_query))
    elif not abc_filter and not supplier_code:
        if active_filter == 'active':
            active_pids = []
            for pid, item in (sales_map_ikuji.items() if current_company=='IKUJI' else sales_map_select.items()):
                if item['sum_long'] > 0: active_pids.append(pid)
            if inventories.filter(Q(current_quantity__gt=0) | Q(product_id__in=active_pids)).exists(): inventories = inventories.filter(Q(current_quantity__gt=0) | Q(product_id__in=active_pids))
    kpi_shortage_cnt, kpi_order_point_cnt = 0, 0
    paginator = Paginator(inventories, 50)
    page_number = request.GET.get('page', 1)
    try: page_obj = paginator.page(page_number)
    except PageNotAnInteger: page_obj = paginator.page(1)
    except EmptyPage: page_obj = paginator.page(paginator.num_pages)
    future_end_date = base_date + timedelta(days=120)
    shipments = ShipmentSchedule.objects.filter(shipment_date__gte=base_date, shipment_date__lte=future_end_date)
    ship_map = {}
    for s in shipments:
        ship_map.setdefault(s.product_id, {}).setdefault(s.shipment_date, 0)
        ship_map[s.product_id][s.shipment_date] += s.quantity
    arrivals = ArrivalSchedule.objects.filter(arrival_date__gte=base_date, arrival_date__lte=future_end_date)
    arr_map = {}
    for a in arrivals:
        arr_map.setdefault(a.product_id, {}).setdefault(a.arrival_date, {}).setdefault(a.status, 0)
        arr_map[a.product_id][a.arrival_date][a.status] += a.quantity
    date_list = [base_date + timedelta(days=i) for i in range(120)]
    all_warehouses = Warehouse.objects.filter(owner_company=current_company)
    wh_inv_records = WarehouseInventory.objects.select_related('warehouse', 'product').filter(Q(warehouse__owner_company=current_company) | Q(warehouse_id__in=shared_wh_ids))
    wh_stock_map, cross_company_wh_stock, cross_company_stock = {}, {}, {}
    for rec in wh_inv_records:
        if rec.warehouse.owner_company == current_company: wh_stock_map.setdefault(rec.product_id, {})[rec.warehouse_id] = rec.quantity
        elif current_company == 'SELECT' and rec.warehouse_id in shared_wh_ids:
            cross_company_wh_stock.setdefault(rec.product.code, {})[rec.warehouse_id] = rec.quantity
            cross_company_stock[rec.product.code] = cross_company_stock.get(rec.product.code, 0) + rec.quantity
    all_inventories_for_kpi = Inventory.objects.select_related('product').filter(product__is_excluded=False, product__owner_company=current_company)
    for k_item in all_inventories_for_kpi:
        k_pid = k_item.product.id
        k_sales = sales_map_select.get(k_pid, {'sum_30': 0, 'sum_long': 0}) if k_item.product.demand_source == 'SELECT' else sales_map_ikuji.get(k_pid, {'sum_30': 0, 'sum_long': 0})
        k_tdays = k_item.product.trend_days if k_item.product.trend_days in [90, 120, 150, 180] else 90
        k_daily_demand = (k_sales.get('sum_long', 0) / k_tdays) * max(k_item.product.trend_min, min(((k_sales.get('sum_30', 0) / 30) / (k_sales.get('sum_long', 0) / k_tdays) if k_sales.get('sum_long', 0) > 0 else 1.0), k_item.product.trend_max))
        k_order_point = (k_daily_demand * k_item.product.lead_time) + k_item.safety_stock
        k_stock = sum(wh_stock_map.get(k_pid, {}).values()) + cross_company_stock.get(k_item.product.code, 0) if current_company == 'SELECT' else sum(wh_stock_map.get(k_pid, {}).values())
        k_ships = ship_map.get(k_pid, {}); k_arrs = arr_map.get(k_pid, {}); k_has_shortage, k_has_op = False, False
        for i in range(120):
            d = base_date + timedelta(days=i)
            k_stock = k_stock + k_arrs.get(d, {}).get('確定', 0) + k_arrs.get(d, {}).get('高確度', 0) - max(k_ships.get(d, 0), k_daily_demand)
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
    active_orders = Order.objects.select_related('product').filter(product__owner_company=current_company).order_by('-created_at')
    active_arrivals = ArrivalSchedule.objects.select_related('product').filter(product__owner_company=current_company).order_by('arrival_date')
    visible_inventories = []
    for item in page_obj:
        pid = item.product.id; pcode = item.product.code
        sales_data = sales_map_select.get(pid, {'sum_30': 0, 'sum_60': 0, 'sum_90': 0, 'sum_120': 0, 'sum_150': 0, 'sum_180': 0, 'sum_long': 0}) if item.product.demand_source == 'SELECT' else sales_map_ikuji.get(pid, {'sum_30': 0, 'sum_60': 0, 'sum_90': 0, 'sum_120': 0, 'sum_150': 0, 'sum_180': 0, 'sum_long': 0})
        item.demand_30 = round(sales_data['sum_30'] / 1, 1); item.demand_60 = round(sales_data['sum_60'] / 2, 1)
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
        running_stock = item.current_quantity; has_shortage_risk, has_order_point_risk = False, False
        product_ships, product_arrivals = ship_map.get(pid, {}), arr_map.get(pid, {})
        first_shortage_date = None
        for d in date_list:
            day_ship = product_ships.get(d, 0); day_arr_dict = product_arrivals.get(d, {})
            arr_total = day_arr_dict.get('確定', 0) + day_arr_dict.get('高確度', 0)
            running_stock = running_stock + arr_total - max(day_ship, daily_demand)
            item.forecast_timeline.append({'date': d, 'stock': round(running_stock, 1), 'ship': day_ship, 'arr_kakutei': day_arr_dict.get('確定', 0), 'arr_koukaku': day_arr_dict.get('高確度', 0), 'arr_kibou': day_arr_dict.get('希望', 0)})
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
    return render(request, 'inventory/dashboard.html', {'inventories': visible_inventories, 'page_obj': page_obj, 'date_list': date_list, 'active_months': active_months, 'search_query': search_query, 'inventory_date_choices': inventory_date_choices, 'active_orders': active_orders, 'active_arrivals': active_arrivals, 'abc_filter': abc_filter, 'supplier_code': supplier_code, 'current_company': current_company, 'active_filter': active_filter, 'all_warehouses': all_warehouses, 'ikuji_warehouses': ikuji_warehouses, 'shared_wh_ids': shared_wh_ids, 'kpi_shortage_cnt': kpi_shortage_cnt, 'kpi_order_point_cnt': kpi_order_point_cnt})

@require_POST
def update_product_config(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    try:
        product.lead_time = int(request.POST.get('lead_time', '30'))
        product.order_lot = int(request.POST.get('order_lot', '1'))
        product.lot_rule = request.POST.get('lot_rule', 'ROUND_UP_LOT')
        product.trend_days = int(request.POST.get('trend_days', '90'))
        product.is_excluded = 'is_excluded' in request.POST
        product.allow_dead_order = 'allow_dead_order' in request.POST
        product.demand_source = request.POST.get('demand_source', product.owner_company)
        product.save()
        inventory, _ = Inventory.objects.get_or_create(product=product)
        inventory.safety_stock = int(request.POST.get('safety_stock', '20'))
        inventory.save()
        _recalculate_abc_ranks(product.owner_company)
        messages.success(request, "商品設定を即時更新しました！")
    except Exception as e: messages.error(request, f"更新エラー: {e}")
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def delete_product(request, product_id):
    get_object_or_404(Product, id=product_id).delete()
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

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
def create_order_plan(request, product_id):
    product = get_object_or_404(Product, id=product_id); inventory = Inventory.objects.get(product=product)
    base_date = datetime.date(2026, 6, 1); future_end_date = base_date + timedelta(days=120)
    sales_summary = SalesHistory.objects.filter(sold_date__gte=base_date - timedelta(days=180), sold_date__lte=base_date).values('product_id', 'company').annotate(sum_30=Sum(Case(When(sold_date__gte=base_date-timedelta(days=30), then='quantity'), default=0, output_field=IntegerField())), sum_long=Sum(Case(When(sold_date__gte=base_date-timedelta(days=product.trend_days), then='quantity'), default=0, output_field=IntegerField())))
    s_map_ikuji, s_map_select = {}, {}
    for item in sales_summary:
        if item['company'] == 'IKUJI': s_map_ikuji[item['product_id']] = item
        else: s_map_select[item['product_id']] = item
    ship_map = {s['product_id']: {s['shipment_date']: s['total']} for s in ShipmentSchedule.objects.filter(shipment_date__gte=base_date, shipment_date__lte=future_end_date).values('product_id', 'shipment_date').annotate(total=Sum('quantity'))}
    arr_map = {a['product_id']: {a['arrival_date']: {a['status']: a['total']}} for a in ArrivalSchedule.objects.filter(arrival_date__gte=base_date, arrival_date__lte=future_end_date).values('product_id', 'arrival_date', 'status').annotate(total=Sum('quantity'))}
    if _execute_single_order_plan(product, inventory, base_date, future_end_date, s_map_select, s_map_ikuji, ship_map, arr_map): messages.success(request, f"商品［{product.code}］の発注計画を作成しました。")
    else: messages.info(request, "発注基準を満たしていないためスキップしました。")
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def bulk_create_order_plan(request):
    current_company = request.POST.get('current_company', 'IKUJI'); selected_pids = request.POST.getlist('selected_products')
    if not selected_pids:
        messages.warning(request, "商品が選択されていません。")
        return redirect('/?current_company=' + current_company)
    base_date = datetime.date(2026, 6, 1); future_end_date = base_date + timedelta(days=120)
    sales_summary = SalesHistory.objects.filter(sold_date__gte=base_date - timedelta(days=180), sold_date__lte=base_date).values('product_id', 'company').annotate(sum_30=Sum(Case(When(sold_date__gte=base_date-timedelta(days=30), then='quantity'), default=0, output_field=IntegerField())), sum_long=Sum(Case(When(sold_date__gte=base_date-timedelta(days=90), then='quantity'), default=0, output_field=IntegerField())))
    s_map_ikuji, s_map_select = {}, {}
    for item in sales_summary:
        if item['company'] == 'IKUJI': s_map_ikuji[item['product_id']] = item
        else: s_map_select[item['product_id']] = item
    ship_map, arr_map = {}, {}
    for s in ShipmentSchedule.objects.filter(shipment_date__gte=base_date, shipment_date__lte=future_end_date): ship_map.setdefault(s.product_id, {})[s.shipment_date] = ship_map.setdefault(s.product_id, {}).get(s.shipment_date, 0) + s.quantity
    for a in ArrivalSchedule.objects.filter(arrival_date__gte=base_date, arrival_date__lte=future_end_date): arr_map.setdefault(a.product_id, {}).setdefault(a.arrival_date, {})[a.status] = arr_map.setdefault(a.product_id, {}).setdefault(a.arrival_date, {}).get(a.status, 0) + a.quantity
    products = Product.objects.filter(id__in=selected_pids); inventories = {inv.product_id: inv for inv in Inventory.objects.filter(product_id__in=selected_pids)}
    success_cnt = 0
    for p in products:
        inv = inventories.get(p.id)
        if inv and _execute_single_order_plan(p, inv, base_date, future_end_date, s_map_select, s_map_ikuji, ship_map, arr_map): success_cnt += 1
    messages.success(request, f"一括発注計算完了！（{success_cnt}件の計画を新規生成）")
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

def operation_guide(request): return render(request, 'inventory/guide.html')

def import_inventory_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            reader = _get_csv_reader(request.FILES['csv_file']); headers = reader.fieldnames
            if not headers or '商品コード' not in headers: return redirect('/?current_company=' + current_company)
            wh_map = {wh_name: Warehouse.objects.get_or_create(name=wh_name, owner_company=current_company, defaults={'is_transit': "移動中" in wh_name})[0] for wh_name in headers if wh_name != '商品コード'}
            products = {p.code: p for p in Product.objects.filter(owner_company=current_company)}
            try: selected_date = datetime.datetime.strptime(request.POST.get('inventory_date', ''), '%Y-%m-%d').date()
            except: selected_date = datetime.date(2026, 5, 31)
            row_count = 0
            for row in reader:
                code = str(row.get('商品コード', '')).strip()
                if code.isdigit() and len(code) < 7: code = code.zfill(7)
                if code not in products: continue
                p_obj, tot = products[code], 0
                for w_name, w_obj in wh_map.items():
                    try: qty = int(float(row.get(w_name, '0')))
                    except: qty = 0
                    WarehouseInventory.objects.update_or_create(product=p_obj, warehouse=w_obj, defaults={'quantity': qty})
                    tot += qty
                Inventory.objects.update_or_create(product=p_obj, defaults={'current_quantity': tot, 'inventory_date': selected_date})
                row_count += 1
            _recalculate_abc_ranks(current_company)
            messages.success(request, f"実在庫を上書き補正し再評価しました！ ({row_count}件)")
        except Exception as e: messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def download_csv_template(request, template_type):
    current_company = request.GET.get('current_company', 'IKUJI'); scode = "0040006" if current_company == 'SELECT' else "5100299"
    if template_type == 'inventory':
        current_warehouses = Warehouse.objects.filter(owner_company=current_company)
        cols = ",".join([wh.name for wh in current_warehouses]) if current_warehouses else "ペットセレクト倉庫,ペットセレクト移動中" if current_company == 'SELECT' else "ニチイク在庫,岸和田在庫,西松屋預託,西松屋移動中"
        ccnt = current_warehouses.count() if current_warehouses else 2 if current_company == 'SELECT' else 4
        content = f"商品コード,{cols}\n{scode},{','.join(['0']*ccnt)}\n"
    else:
        templates = {
            'products': "商品コード,商品名,リードタイム,発注ロット,超過時ルール,長期トレンド日数,管理外フラグ\n0040006,サンプル商品名称,90,100,ROUND_UP_LOT,90,FALSE\n",
            'sales': f"伝票日付,得意先コード,商品コード,状態コード,合計 / 粗利,合計 / 税抜,合計 / 数量\n2026/06/01,000013,{scode},001,3300,7920,6\n",
            'arrivals': f"商品コード,入荷予定日,入荷予定数量,確度ステータス\n{scode},2026/06/15,100,確定\n"
        }
        content = templates.get(template_type, "")
    response = HttpResponse(content.encode('cp932', errors='replace'), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="template_{template_type}.csv"'; return response

def import_products_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            reader = _get_csv_reader(request.FILES['csv_file']); row_count = 0
            for row in reader:
                code = str(row.get('商品コード', '')).strip()
                if not code or code.startswith('field_') or code.lower() == 'none': continue
                if code.isdigit() and len(code) < 7: code = code.zfill(7)
                try: lt = int(float(row.get('リードタイム', '30')))
                except: lt = 30
                try: lot = int(float(row.get('発注ロット', '1')))
                except: lot = 1
                rule = row.get('超過時ルール', 'ROUND_UP_LOT').strip()
                if rule not in ['ROUND_UP_LOT', 'MIN_LOT_ONLY']: rule = 'ROUND_UP_LOT'
                try: tdays = int(float(row.get('長期トレンド日数', '90')))
                except: tdays = 90
                exc = row.get('管理外フラグ', 'FALSE').strip().upper() == 'TRUE'
                p, _ = Product.objects.update_or_create(code=code, defaults={'name': row.get('商品名', '名称未設定'), 'price': 0, 'supplier': '仕入先未設定', 'lead_time': lt, 'order_lot': lot, 'lot_rule': rule, 'trend_days': tdays, 'is_excluded': exc, 'owner_company': current_company, 'demand_source': current_company})
                Inventory.objects.get_or_create(product=p, defaults={'current_quantity': 0, 'safety_stock': 20, 'inventory_date': datetime.date(2026, 5, 31)})
                row_count += 1
            _recalculate_abc_ranks(current_company)
            messages.success(request, f"商品マスタ登録完了！（{row_count}件）")
        except Exception as e: messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def import_sales_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            rows = list(_get_csv_reader(request.FILES['csv_file']))
            if not rows: return redirect('/?current_company=' + current_company)
            product_dict = {p.code: p for p in Product.objects.all()}
            date_set, parsed_rows = set(), []
            for row in rows:
                if row.get('伝票日付', '').startswith('field_'): continue
                code = str(row.get('商品コード', '')).strip()
                if code.isdigit() and len(code) < 7: code = code.zfill(7)
                if code not in product_dict: continue
                try:
                    rd = row['伝票日付'].split()[0]
                    sd = datetime.datetime.strptime(rd, '%Y/%m/%d').date() if '/' in rd else datetime.datetime.strptime(rd, '%Y-%m-%d').date()
                    qty = int(float(row['合計 / 数量']))
                except: continue
                date_set.add(sd)
                parsed_rows.append({'sold_date': sd, 'product': product_dict[code], 'code': code, 'quantity': qty, 'customer': row.get('得意先コード', '').strip()})
            existing_sales = {}
            if date_set:
                for sh in SalesHistory.objects.filter(company=current_company, sold_date__in=date_set).select_related('product'): existing_sales[(sh.sold_date, sh.product.code, sh.customer)] = sh
            create_list, update_list = [], []
            for r in parsed_rows:
                key = (r['sold_date'], r['code'], r['customer'])
                if key in existing_sales:
                    sh_obj = existing_sales[key]
                    if sh_obj.quantity != r['quantity']:
                        sh_obj.quantity = r['quantity']; update_list.append(sh_obj)
                else: create_list.append(SalesHistory(sales_id=None, product=r['product'], sold_date=r['sold_date'], quantity=r['quantity'], customer=r['customer'], company=current_company))
            if create_list: SalesHistory.objects.bulk_create(create_list)
            if update_list: SalesHistory.objects.bulk_update(update_list, ['quantity'])
            _recalculate_abc_ranks(current_company)
            messages.success(request, f"販売履歴の差分更新完了（新規: {len(create_list)}件 / 更新: {len(update_list)}件）")
        except Exception as e: messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def import_arrivals_csv(request):
    current_company = request.POST.get('current_company', 'IKUJI')
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            ArrivalSchedule.objects.filter(product__owner_company=current_company).delete()
            reader = _get_csv_reader(request.FILES['csv_file']); p_dict = {p.code: p for p in Product.objects.filter(owner_company=current_company)}; row_count = 0
            for row in reader:
                code = str(row.get('商品コード', '')).strip()
                if code.isdigit() and len(code) < 7: code = code.zfill(7)
                if code not in p_dict: continue
                dk = '入荷予定日' if '入荷予定日' in row else '予定日'; qk = '入荷予定数量' if '入荷予定数量' in row else '入荷数量'
                try:
                    rd = row[dk].split()[0]
                    ad = datetime.datetime.strptime(rd, '%Y/%m/%d').date() if '/' in rd else datetime.datetime.strptime(rd, '%Y-%m-%d').date()
                    qty = int(float(row[qk]))
                except: continue
                ArrivalSchedule.objects.create(product=p_dict[code], arrival_date=ad, quantity=qty, status=row.get('確度ステータス', '確定') if row.get('確度ステータス') in ['確定','高確度','希望'] else '確定')
                row_count += 1
            messages.success(request, f"入荷予定を洗替更新しました！（{row_count}件）")
        except Exception as e: messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def product_list(request): return render(request, 'inventory/product_list.html', {'products': Product.objects.all().order_by('code')})

def export_products_csv(request):
    cc = request.GET.get('current_company', 'IKUJI'); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="products_{cc}.csv"'
    buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['商品コード', '商品名', '標準原価', '仕入先名', 'リードタイム', '発注ロット', '超過時ルール', '長期トレンド日数', '管理外フラグ'])
    for p in Product.objects.filter(owner_company=cc): writer.writerow([p.code, p.name, p.price, p.supplier, p.lead_time, p.order_lot, p.lot_rule, p.trend_days, p.is_excluded])
    res.write(buf.getvalue().encode('cp932', errors='replace')); return res

def export_inventory_csv(request):
    cc = request.GET.get('current_company', 'IKUJI'); whs = Warehouse.objects.filter(owner_company=cc); invs = Inventory.objects.select_related('product').filter(product__owner_company=cc)
    w_map = {}
    for r in WarehouseInventory.objects.filter(warehouse__owner_company=cc): w_map.setdefault(r.product_id, {})[r.warehouse_id] = r.quantity
    res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="inventory_{cc}.csv"'
    buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['商品コード', '商品名', '現在庫数（合算）', '安全在庫数'] + [w.name for w in whs])
    for i in invs:
        row = [i.product.code, i.product.name, i.current_quantity, i.safety_stock]
        for w in whs: row.append(w_map.get(i.product.id, {}).get(w.id, 0))
        writer.writerow(row)
    res.write(buf.getvalue().encode('cp932', errors='replace')); return res

def export_sales_csv(request):
    cc = request.GET.get('current_company', 'IKUJI'); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="sales_{cc}.csv"'
    buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['伝票日付', '商品コード', '商品名', '販売数', '得意先名'])
    for s in SalesHistory.objects.select_related('product').filter(company=cc): writer.writerow([s.sold_date.strftime('%Y/%m/%d'), s.product.code, s.product.name, s.quantity, s.customer])
    res.write(buf.getvalue().encode('cp932', errors='replace')); return res

def export_arrivals_csv(request):
    cc = request.GET.get('current_company', 'IKUJI'); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="arrivals_{cc}.csv"'
    buf = io.StringIO(); writer = csv.writer(buf); writer.writerow(['商品コード', '商品名', '入荷予定日', '入荷予定数量', '確度ステータス'])
    for a in ArrivalSchedule.objects.select_related('product').filter(product__owner_company=cc): writer.writerow([a.product.code, a.product.name, a.arrival_date.strftime('%Y/%m/%d'), a.quantity, a.status])
    res.write(buf.getvalue().encode('cp932', errors='replace')); return res

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
    wh = get_object_or_404(Warehouse, id=warehouse_id); wh_name = wh.name; wh.delete()
    _recalculate_abc_ranks(current_company)
    messages.info(request, f"倉庫［{wh_name}］を完全に消去しました。")
    return redirect('/?current_company=' + current_company)

def about_app(request):
    current_company = request.GET.get('current_company', 'IKUJI')
    return render(request, 'inventory/about.html', {'current_company': current_company})