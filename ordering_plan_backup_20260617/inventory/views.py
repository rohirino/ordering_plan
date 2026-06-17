import datetime
import math
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.http import HttpResponse
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from .models import Product, Inventory, Warehouse, WarehouseInventory, SalesHistory, Order, ShipmentSchedule, ArrivalSchedule
from django.db.models import Sum, Case, When, IntegerField, Q
from datetime import timedelta
from .services import (
    VALID_TREND_DAYS,
    bulk_create_order_plans,
    create_order_plan_for_product,
    csv_template_content,
    current_company as _current_company,
    export_arrivals_rows,
    export_inventory_rows,
    export_products_rows,
    export_sales_rows,
    import_arrivals_csv as service_import_arrivals_csv,
    import_inventory_csv as service_import_inventory_csv,
    import_products_csv as service_import_products_csv,
    import_sales_csv as service_import_sales_csv,
    planning_base_date as _planning_base_date,
    recalculate_abc_ranks as _recalculate_abc_ranks,
    rows_to_cp932_csv,
)

def planning_dashboard(request):
    current_company = _current_company(request.GET.get('current_company', 'IKUJI'))
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
    base_date = _planning_base_date()
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
        k_tdays = k_item.product.trend_days if k_item.product.trend_days in VALID_TREND_DAYS else 90
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
def recalculate_abc_ranks(request):
    current_company = _current_company(request.POST.get('current_company', 'IKUJI'))
    _recalculate_abc_ranks(current_company)
    messages.success(request, "ABCランクを最新基準日で再計算しました。")
    return redirect('/?current_company=' + current_company)

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
        product.demand_source = _current_company(request.POST.get('demand_source', product.owner_company))
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
    if create_order_plan_for_product(product, inventory): messages.success(request, f"商品［{product.code}］の発注計画を作成しました。")
    else: messages.info(request, "発注基準を満たしていないためスキップしました。")
    return redirect(request.META.get('HTTP_REFERER', 'planning_dashboard'))

@require_POST
def bulk_create_order_plan(request):
    current_company = _current_company(request.POST.get('current_company', 'IKUJI')); selected_pids = request.POST.getlist('selected_products')
    if not selected_pids:
        messages.warning(request, "商品が選択されていません。")
        return redirect('/?current_company=' + current_company)
    success_cnt = bulk_create_order_plans(selected_pids)
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
    current_company = _current_company(request.POST.get('current_company', 'IKUJI'))
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            try: selected_date = datetime.datetime.strptime(request.POST.get('inventory_date', ''), '%Y-%m-%d').date()
            except: selected_date = None
            row_count = service_import_inventory_csv(request.FILES['csv_file'], current_company, selected_date)
            messages.success(request, f"実在庫を上書き補正し再評価しました！ ({row_count}件)")
        except Exception as e: messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def download_csv_template(request, template_type):
    current_company = _current_company(request.GET.get('current_company', 'IKUJI'))
    content = csv_template_content(template_type, current_company)
    response = HttpResponse(content.encode('cp932', errors='replace'), content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="template_{template_type}.csv"'; return response

def import_products_csv(request):
    current_company = _current_company(request.POST.get('current_company', 'IKUJI'))
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            row_count = service_import_products_csv(request.FILES['csv_file'], current_company)
            messages.success(request, f"商品マスタ登録完了！（{row_count}件）")
        except Exception as e: messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def import_sales_csv(request):
    current_company = _current_company(request.POST.get('current_company', 'IKUJI'))
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            create_count, update_count = service_import_sales_csv(request.FILES['csv_file'], current_company)
            messages.success(request, f"販売履歴の差分更新完了（新規: {create_count}件 / 更新: {update_count}件）")
        except Exception as e: messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def import_arrivals_csv(request):
    current_company = _current_company(request.POST.get('current_company', 'IKUJI'))
    if request.method == 'POST' and request.FILES.get('csv_file'):
        try:
            row_count = service_import_arrivals_csv(request.FILES['csv_file'], current_company)
            messages.success(request, f"入荷予定を洗替更新しました！（{row_count}件）")
        except Exception as e: messages.error(request, f"エラー: {e}")
    return redirect('/?current_company=' + current_company)

def product_list(request): return render(request, 'inventory/product_list.html', {'products': Product.objects.all().order_by('code')})

def export_products_csv(request):
    cc = _current_company(request.GET.get('current_company', 'IKUJI')); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="products_{cc}.csv"'
    res.write(rows_to_cp932_csv(export_products_rows(cc))); return res

def export_inventory_csv(request):
    cc = _current_company(request.GET.get('current_company', 'IKUJI'))
    res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="inventory_{cc}.csv"'
    res.write(rows_to_cp932_csv(export_inventory_rows(cc))); return res

def export_sales_csv(request):
    cc = _current_company(request.GET.get('current_company', 'IKUJI')); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="sales_{cc}.csv"'
    res.write(rows_to_cp932_csv(export_sales_rows(cc))); return res

def export_arrivals_csv(request):
    cc = _current_company(request.GET.get('current_company', 'IKUJI')); res = HttpResponse(content_type='text/csv'); res['Content-Disposition'] = f'attachment; filename="arrivals_{cc}.csv"'
    res.write(rows_to_cp932_csv(export_arrivals_rows(cc))); return res

@require_POST
def delete_sales_history_period(request):
    cc = _current_company(request.POST.get('current_company', 'IKUJI'))
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
    current_company = _current_company(request.POST.get('current_company', 'IKUJI'))
    wh = get_object_or_404(Warehouse, id=warehouse_id); wh_name = wh.name; wh.delete()
    _recalculate_abc_ranks(current_company)
    messages.info(request, f"倉庫［{wh_name}］を完全に消去しました。")
    return redirect('/?current_company=' + current_company)

def about_app(request):
    current_company = _current_company(request.GET.get('current_company', 'IKUJI'))
    return render(request, 'inventory/about.html', {'current_company': current_company})
