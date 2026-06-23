import csv
import datetime
import io
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db.models import Sum
from django.utils import timezone

from inventory.models import (
    Inventory,
    InventoryState,
    InventoryValuationSnapshot,
    Product,
    ProductVariant,
    ProductVariantCostHistory,
    Warehouse,
    WarehouseInventory,
)


ENCODINGS = ('cp932', 'utf-8-sig', 'utf-8', 'shift_jis')
SELECT_ASSET_STATE_CODES = {'400', '401', '404'}
METADATA_COLUMNS = {
    '商品コード', 'コード', '商品名', '状態コード', '状態名',
    '状態別評価原価', '原価', '固定原価', '標準原価', '単価',
}


def decode_csv_bytes(binary_data):
    for encoding in ENCODINGS:
        try:
            return binary_data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return binary_data.decode('cp932', errors='replace')


def read_csv_rows(uploaded_file):
    text = decode_csv_bytes(uploaded_file.read()).replace('\ufeff', '')
    return list(csv.DictReader(io.StringIO(text)))


def parse_number(value):
    if value in (None, ''):
        return 0, False
    text = str(value).replace(',', '').strip()
    if not text:
        return 0, False
    try:
        return int(float(text)), False
    except (ValueError, TypeError):
        return 0, True


def parse_cost(value):
    """評価原価は円単位へ四捨五入し、数量の整数変換とは分けて扱う。"""
    if value in (None, ''):
        return 0, False
    text = str(value).replace(',', '').strip()
    if not text:
        return 0, False
    try:
        return int(Decimal(text).quantize(Decimal('1'), rounding=ROUND_HALF_UP)), False
    except (InvalidOperation, ValueError, TypeError):
        return 0, True


def split_variant_code(raw_code):
    digit_code = ''.join(ch for ch in str(raw_code or '') if ch.isdigit())
    if len(digit_code) >= 10:
        return digit_code[:7], digit_code[7:10]
    if len(digit_code) == 7:
        return digit_code, '000'
    if digit_code and len(digit_code) < 7:
        return digit_code.zfill(7), '000'
    return '', ''


def load_state_name_map():
    return {
        state.state_code: state.state_name
        for state in InventoryState.objects.all()
    }


def resolve_asset_company(state_code, current_company):
    if state_code in SELECT_ASSET_STATE_CODES:
        return 'SELECT'
    return current_company


def get_row_value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def import_valuation_snapshot(uploaded_file, inventory_date, current_company='IKUJI'):
    rows = read_csv_rows(uploaded_file)
    if not rows:
        return {'snapshots': 0, 'variants': 0, 'warehouses': 0, 'skipped': 0, 'quantity_errors': 0}

    headers = list(rows[0].keys())
    warehouse_headers = [header for header in headers if header and header not in METADATA_COLUMNS]
    snapshots = []
    touched_variants = set()
    touched_companies = set()
    skipped = 0
    quantity_errors = 0
    state_name_map = load_state_name_map()

    for row in rows:
        product_code, state_code = split_variant_code(get_row_value(row, '商品コード', 'コード'))
        if not product_code:
            skipped += 1
            continue
        asset_company = resolve_asset_company(state_code, current_company)

        synced_at = timezone.now()
        product, created = Product.objects.get_or_create(
            code=product_code,
            defaults={
                'name': get_row_value(row, '商品名') or '名称未設定',
                'owner_company': asset_company,
                'demand_source': asset_company,
                'created_from_valuation': True,
                'last_valuation_synced_at': synced_at,
                'last_valuation_inventory_date': inventory_date,
                'last_valuation_name_updated': False,
            },
        )
        product_name = get_row_value(row, '商品名')
        update_fields = []
        name_updated = False
        if product_name and product.name != product_name:
            product.name = product_name
            update_fields.append('name')
            name_updated = True
        if not created:
            product.last_valuation_synced_at = synced_at
            product.last_valuation_inventory_date = inventory_date
            product.last_valuation_name_updated = name_updated
            update_fields.extend([
                'last_valuation_synced_at',
                'last_valuation_inventory_date',
                'last_valuation_name_updated',
            ])
        if update_fields:
            product.save(update_fields=update_fields)

        unit_cost, cost_error = parse_cost(get_row_value(row, '状態別評価原価', '原価', '固定原価', '標準原価', '単価'))
        if cost_error:
            unit_cost = 0

        variant, _ = ProductVariant.objects.update_or_create(
            product=product,
            state_code=state_code,
            defaults={
                'state_name': get_row_value(row, '状態名') or state_name_map.get(state_code, state_code),
                'current_cost': unit_cost,
            },
        )
        ProductVariantCostHistory.objects.update_or_create(
            product_variant=variant,
            effective_date=inventory_date,
            defaults={'unit_cost': unit_cost, 'source': '棚卸CSV'},
        )
        touched_variants.add(variant.id)
        touched_companies.add(asset_company)

        for warehouse_name in warehouse_headers:
            warehouse = Warehouse.objects.get_or_create(
                name=warehouse_name,
                owner_company=asset_company,
                defaults={'is_transit': '移動中' in warehouse_name},
            )[0]
            quantity, had_error = parse_number(row.get(warehouse_name))
            if had_error:
                quantity_errors += 1
            if quantity == 0 and unit_cost == 0:
                continue
            snapshots.append(InventoryValuationSnapshot(
                inventory_date=inventory_date,
                product_variant=variant,
                warehouse=warehouse,
                quantity=quantity,
                unit_cost=unit_cost,
                amount=quantity * unit_cost,
                owner_company=asset_company,
            ))

    if snapshots:
        InventoryValuationSnapshot.objects.filter(inventory_date=inventory_date, owner_company__in=touched_companies).delete()
        InventoryValuationSnapshot.objects.bulk_create(snapshots)

    return {
        'snapshots': len(snapshots),
        'variants': len(touched_variants),
        'warehouses': len(warehouse_headers),
        'skipped': skipped,
        'quantity_errors': quantity_errors,
        'select_asset_rows': sum(1 for snapshot in snapshots if snapshot.owner_company == 'SELECT'),
    }


def import_inventory_state_master(uploaded_file):
    rows = read_csv_rows(uploaded_file)
    imported = 0
    skipped = 0
    variants_updated = 0
    for row in rows:
        raw_code = get_row_value(row, '在庫状態コード', '状態コード')
        state_code = ''.join(ch for ch in str(raw_code or '') if ch.isdigit()).zfill(3)
        state_name = get_row_value(row, '在庫状態', '状態名')
        if not state_code or raw_code.startswith('field_') or not state_name:
            skipped += 1
            continue
        InventoryState.objects.update_or_create(
            state_code=state_code,
            defaults={'state_name': state_name},
        )
        variants_updated += ProductVariant.objects.filter(state_code=state_code).update(state_name=state_name)
        imported += 1
    return {'imported': imported, 'skipped': skipped, 'variants_updated': variants_updated}


def sync_snapshot_to_planning_inventory(inventory_date, current_company='IKUJI'):
    all_snapshots = InventoryValuationSnapshot.objects.filter(
        inventory_date=inventory_date,
        owner_company=current_company,
    ).select_related('product_variant__product', 'warehouse')
    snapshots = InventoryValuationSnapshot.objects.filter(
        inventory_date=inventory_date,
        owner_company=current_company,
        product_variant__include_in_planning_inventory=True,
    ).select_related('product_variant__product', 'warehouse')
    excluded_snapshot_count = InventoryValuationSnapshot.objects.filter(
        inventory_date=inventory_date,
        owner_company=current_company,
        product_variant__include_in_planning_inventory=False,
    ).count()

    product_totals = {
        snapshot.product_variant.product_id: 0
        for snapshot in all_snapshots
    }
    warehouse_totals = {}
    for snapshot in snapshots:
        product = snapshot.product_variant.product
        product_totals[product.id] = product_totals.get(product.id, 0) + snapshot.quantity
        key = (product.id, snapshot.warehouse_id)
        warehouse_totals[key] = warehouse_totals.get(key, 0) + snapshot.quantity

    products = Product.objects.filter(id__in=product_totals.keys())
    product_map = {product.id: product for product in products}

    for product_id, quantity in product_totals.items():
        product = product_map[product_id]
        Inventory.objects.update_or_create(
            product=product,
            defaults={'current_quantity': quantity, 'inventory_date': inventory_date},
        )
        WarehouseInventory.objects.filter(product=product, warehouse__owner_company=current_company).delete()

    for (product_id, warehouse_id), quantity in warehouse_totals.items():
        WarehouseInventory.objects.update_or_create(
            product_id=product_id,
            warehouse_id=warehouse_id,
            defaults={'quantity': quantity},
        )

    return {
        'products': len(product_totals),
        'warehouse_rows': len(warehouse_totals),
        'excluded_snapshots': excluded_snapshot_count,
    }


def valuation_context(inventory_date, current_company='IKUJI'):
    snapshots = InventoryValuationSnapshot.objects.filter(
        inventory_date=inventory_date,
        owner_company=current_company,
    ).select_related('product_variant__product', 'warehouse').order_by(
        'product_variant__product__code', 'product_variant__state_code', 'warehouse__name'
    )

    warehouse_summary = snapshots.values('warehouse__name').annotate(
        quantity=Sum('quantity'),
        amount=Sum('amount'),
    ).order_by('warehouse__name')
    product_summary = snapshots.values(
        'product_variant__product__code',
        'product_variant__product__name',
    ).annotate(quantity=Sum('quantity'), amount=Sum('amount')).order_by('product_variant__product__code')
    variant_summary = snapshots.values(
        'product_variant_id',
        'product_variant__product__code',
        'product_variant__product__name',
        'product_variant__state_code',
        'product_variant__state_name',
        'product_variant__include_in_planning_inventory',
        'unit_cost',
    ).annotate(quantity=Sum('quantity'), amount=Sum('amount')).order_by(
        'product_variant__product__code', 'product_variant__state_code'
    )

    return {
        'snapshots': snapshots,
        'warehouse_summary': warehouse_summary,
        'product_summary': product_summary,
        'variant_summary': variant_summary,
        'total_quantity': sum(row['quantity'] or 0 for row in warehouse_summary),
        'total_amount': sum(row['amount'] or 0 for row in warehouse_summary),
    }


def available_inventory_dates(current_company='IKUJI'):
    dates = InventoryValuationSnapshot.objects.filter(owner_company=current_company).values_list(
        'inventory_date', flat=True
    ).distinct().order_by('-inventory_date')
    return list(dates)


def parse_inventory_date(value, current_company='IKUJI'):
    if value:
        try:
            return datetime.datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            pass
    dates = available_inventory_dates(current_company)
    if dates:
        return dates[0]
    return datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
