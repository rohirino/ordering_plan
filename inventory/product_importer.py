import csv
import io
from itertools import zip_longest

from inventory.models import Inventory, Product


ENCODINGS = ('cp932', 'utf-8-sig', 'utf-8', 'shift_jis')


def decode_csv_bytes(binary_data):
    for encoding in ENCODINGS:
        try:
            return binary_data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return binary_data.decode('cp932', errors='replace')


def read_csv_rows_from_path(csv_file_path):
    with open(csv_file_path, 'rb') as f:
        return read_csv_rows_from_bytes(f.read())


def read_csv_rows_from_bytes(binary_data):
    text = decode_csv_bytes(binary_data).replace('\ufeff', '')
    return list(csv.reader(io.StringIO(text)))


def find_header_index(rows):
    """先頭数行から商品マスタのヘッダー行を探す"""
    for index, row in enumerate(rows[:20]):
        normalized = [cell.strip() for cell in row]
        if '商品名' in normalized and ('商品コード' in normalized or 'コード' in normalized):
            return index
    return 0


def normalize_price(value):
    if not value:
        return 0
    try:
        return int(float(str(value).replace(',', '').strip()))
    except (ValueError, TypeError):
        return 0


def normalize_int(value, default):
    if value in (None, ''):
        return default
    try:
        return int(float(str(value).replace(',', '').strip()))
    except (ValueError, TypeError):
        return default


def get_row_value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def iter_dict_rows(rows):
    header_index = find_header_index(rows)
    headers = [header.strip() for header in rows[header_index]]

    for data_row in rows[header_index + 1:]:
        yield {
            header: value
            for header, value in zip_longest(headers, data_row, fillvalue='')
            if header
        }


def build_product_data(row, seen_codes, current_company='IKUJI'):
    raw_code = get_row_value(row, '商品コード', 'コード')
    if not raw_code:
        return None, 'empty'

    # JUSTDBのフィールドID行をスキップ
    if raw_code.startswith('field_'):
        return None, 'field_id'

    digit_code = ''.join(ch for ch in raw_code if ch.isdigit())
    if len(digit_code) == 10:
        product_code = digit_code[:7]
    elif digit_code and len(digit_code) < 7:
        product_code = digit_code.zfill(7)
    else:
        product_code = raw_code.strip()

    if not product_code:
        return None, 'empty'

    if product_code in seen_codes:
        return None, 'duplicate_variant'

    name = get_row_value(row, '商品名')
    if not name:
        return None, 'empty_name'

    rule = get_row_value(row, '超過時ルール') or 'ROUND_UP_LOT'
    if rule not in ['ROUND_UP_LOT', 'MIN_LOT_ONLY']:
        rule = 'ROUND_UP_LOT'

    has_discontinued_column = '廃盤フラグ' in row or '廃盤' in row
    seen_codes.add(product_code)
    product_data = {
        'code': product_code,
        'name': name,
        'price': normalize_price(get_row_value(row, '標準原価', '固定原価')),
        'supplier': get_row_value(row, '仕入先名【仕入先マスタ 一覧】', '仕入先') or '仕入先未設定',
        'lead_time': normalize_int(get_row_value(row, 'リードタイム'), 30),
        'order_lot': normalize_int(get_row_value(row, '発注ロット'), 1),
        'lot_rule': rule,
        'trend_days': normalize_int(get_row_value(row, '長期トレンド日数'), 90),
        'is_excluded': get_row_value(row, '管理外フラグ').upper() == 'TRUE',
        'owner_company': current_company,
        'demand_source': current_company,
    }
    if has_discontinued_column:
        product_data['is_discontinued'] = get_row_value(row, '廃盤フラグ', '廃盤').upper() == 'TRUE'
    return product_data, None


def import_product_rows(rows, current_company='IKUJI', dry_run=False):
    stats = {
        'imported': 0,
        'duplicate_variants': 0,
        'skipped': 0,
    }
    seen_codes = set()

    for row in iter_dict_rows(rows):
        product_data, skip_reason = build_product_data(row, seen_codes, current_company)
        if skip_reason == 'duplicate_variant':
            stats['duplicate_variants'] += 1
            continue
        if product_data is None:
            stats['skipped'] += 1
            continue

        if not dry_run:
            defaults = {
                'name': product_data['name'],
                'price': product_data['price'],
                'supplier': product_data['supplier'],
                'lead_time': product_data['lead_time'],
                'order_lot': product_data['order_lot'],
                'lot_rule': product_data['lot_rule'],
                'trend_days': product_data['trend_days'],
                'is_excluded': product_data['is_excluded'],
                'owner_company': product_data['owner_company'],
                'demand_source': product_data['demand_source'],
            }
            if 'is_discontinued' in product_data:
                defaults['is_discontinued'] = product_data['is_discontinued']
            product, _ = Product.objects.update_or_create(
                code=product_data['code'],
                defaults=defaults
            )
            Inventory.objects.get_or_create(
                product=product,
                defaults={
                    'current_quantity': 0,
                    'safety_stock': 20,
                }
            )
        stats['imported'] += 1

    return stats


def import_product_file_path(csv_file_path, current_company='IKUJI', dry_run=False):
    rows = read_csv_rows_from_path(csv_file_path)
    return import_product_rows(rows, current_company=current_company, dry_run=dry_run)


def import_uploaded_product_file(uploaded_file, current_company='IKUJI'):
    rows = read_csv_rows_from_bytes(uploaded_file.read())
    return import_product_rows(rows, current_company=current_company)
