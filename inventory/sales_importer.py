import csv
import datetime
import hashlib
import io
from collections import defaultdict
from itertools import zip_longest

from django.utils import timezone

from inventory.models import Inventory, Product, SalesHistory


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
    for index, row in enumerate(rows[:20]):
        normalized = [cell.strip() for cell in row]
        if '伝票日付' in normalized and '商品名' in normalized and '数量' in normalized:
            return index
        if '伝票日付' in normalized and '商品コード' in normalized:
            return index
    return 0


def parse_number(value):
    if value in (None, ''):
        return 0
    text = str(value).replace(',', '').strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def parse_date(value):
    text = str(value or '').strip()
    if not text:
        return None
    text = ' '.join(text.split())
    for fmt in ('%Y/%m/%d', '%Y-%m-%d', '%Y年 %m月 %d日', '%Y年%m月%d日'):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_product_code(raw_code):
    raw_text = str(raw_code or '').strip()
    if raw_text.endswith('.0') and raw_text[:-2].isdigit():
        raw_text = raw_text[:-2]
    digit_code = ''.join(ch for ch in raw_text if ch.isdigit())
    if len(digit_code) >= 10:
        return digit_code[:7]
    if len(digit_code) in (8, 9):
        return digit_code.zfill(10)[:7]
    if digit_code and len(digit_code) < 7:
        return digit_code.zfill(7)
    return digit_code or raw_text


def get_row_value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def make_sales_id(company, sold_date, customer_code, product_code, sales_category):
    key = f'{company}|{sold_date:%Y%m%d}|{customer_code}|{product_code}|{sales_category}'
    return 'SALES-' + hashlib.sha1(key.encode('utf-8')).hexdigest()


def iter_sales_rows(rows):
    header_index = find_header_index(rows)
    headers = [header.strip() for header in rows[header_index]]

    # 販売管理ソフト出力形式: 3行目に重複した「コード」列がある
    if headers.count('コード') >= 2 and '得意先名' in headers and '商品名' in headers:
        customer_code_idx = headers.index('コード')
        product_code_idx = [idx for idx, header in enumerate(headers) if header == 'コード'][1]
        product_name_idx = headers.index('商品名')
        date_idx = headers.index('伝票日付')
        category_idx = headers.index('区分')
        quantity_idx = headers.index('数量')
        tax_idx = headers.index('税抜金額')
        gross_idx = headers.index('粗利金額')

        for source_row_number, row in enumerate(rows[header_index + 1:], header_index + 2):
            if not any(cell.strip() for cell in row):
                continue
            padded = list(row) + [''] * max(0, len(headers) - len(row))
            yield {
                '元データ行': source_row_number,
                '伝票日付': padded[date_idx],
                '得意先コード': padded[customer_code_idx],
                '商品コード': padded[product_code_idx],
                '商品名': padded[product_name_idx],
                '区分': padded[category_idx],
                '数量': padded[quantity_idx],
                '税抜金額': padded[tax_idx],
                '粗利金額': padded[gross_idx],
            }
        return

    # 従来の加工済みCSV形式
    for source_row_number, data_row in enumerate(rows[header_index + 1:], header_index + 2):
        row = {
            header: value
            for header, value in zip_longest(headers, data_row, fillvalue='')
            if header
        }
        if row.get('伝票日付', '').startswith('field_'):
            continue
        yield {
            '元データ行': source_row_number,
            '伝票日付': get_row_value(row, '伝票日付'),
            '得意先コード': get_row_value(row, '得意先コード', '得意先名'),
            '商品コード': get_row_value(row, '商品コード'),
            '商品名': get_row_value(row, '商品名'),
            '区分': get_row_value(row, '区分') or '売上',
            '数量': get_row_value(row, '数量', '販売数', '合計 / 数量'),
            '税抜金額': get_row_value(row, '税抜金額', '合計 / 税抜'),
            '粗利金額': get_row_value(row, '粗利金額', '合計 / 粗利'),
        }


def aggregate_sales_rows(rows, current_company='IKUJI'):
    aggregated = defaultdict(lambda: {
        'quantity': 0,
        'tax_excluded_amount': 0,
        'gross_profit_amount': 0,
        'source_rows': [],
        'source_product_codes': [],
        'product_name': '',
    })
    skipped_rows = []

    for row in iter_sales_rows(rows):
        sold_date = parse_date(row.get('伝票日付'))
        customer_code = str(row.get('得意先コード') or '').strip()
        product_code = normalize_product_code(row.get('商品コード'))
        sales_category = str(row.get('区分') or '売上').strip() or '売上'

        if not sold_date or not customer_code or not product_code:
            reasons = []
            raw_date = str(row.get('伝票日付') or '').strip()
            if not sold_date:
                reasons.append('伝票日付未入力' if not raw_date else '伝票日付形式不正')
            if not customer_code:
                reasons.append('得意先コード未入力')
            if not product_code:
                reasons.append('商品コード未入力')
            skipped_rows.append({
                'source_rows': str(row.get('元データ行') or ''),
                'reason': ' / '.join(reasons),
                'sold_date_text': raw_date,
                'customer_code': customer_code,
                'source_product_code': str(row.get('商品コード') or '').strip(),
                'normalized_product_code': product_code,
                'product_name': str(row.get('商品名') or '').strip(),
                'sales_category': sales_category,
                'quantity_text': str(row.get('数量') or '').strip(),
                'tax_excluded_amount_text': str(row.get('税抜金額') or '').strip(),
                'gross_profit_amount_text': str(row.get('粗利金額') or '').strip(),
            })
            continue

        key = (current_company, sold_date, customer_code, product_code, sales_category)
        aggregated[key]['quantity'] += parse_number(row.get('数量'))
        aggregated[key]['tax_excluded_amount'] += parse_number(row.get('税抜金額'))
        aggregated[key]['gross_profit_amount'] += parse_number(row.get('粗利金額'))
        aggregated[key]['source_rows'].append(str(row.get('元データ行') or ''))
        aggregated[key]['source_product_codes'].append(str(row.get('商品コード') or '').strip())
        if not aggregated[key]['product_name']:
            aggregated[key]['product_name'] = str(row.get('商品名') or '').strip()

    return aggregated, skipped_rows


def import_sales_rows(rows, current_company='IKUJI', dry_run=False):
    aggregated, skipped_rows = aggregate_sales_rows(rows, current_company=current_company)
    product_dict = {
        product.code: product
        for product in Product.objects.filter(owner_company=current_company)
    }
    target_dates = {key[1] for key in aggregated}

    existing_sales = {}
    if target_dates:
        for sales in SalesHistory.objects.filter(company=current_company, sold_date__in=target_dates).select_related('product'):
            existing_sales[(sales.company, sales.sold_date, sales.customer or '', sales.product.code, sales.sales_category or '売上')] = sales

    create_list = []
    update_list = []
    missing_product_rows = []
    auto_created_products = 0
    other_company_product_rows = 0

    for key, totals in aggregated.items():
        company, sold_date, customer_code, product_code, sales_category = key
        product = product_dict.get(product_code)
        if not product:
            other_company_product = Product.objects.filter(code=product_code).first()
            if other_company_product:
                other_company_product_rows += 1
                missing_product_rows.append({
                    'source_rows': ', '.join(filter(None, totals['source_rows'])),
                    'reason': '他社商品マスタ登録済',
                    'sold_date_text': sold_date.strftime('%Y/%m/%d'),
                    'customer_code': customer_code,
                    'source_product_code': ', '.join(dict.fromkeys(filter(None, totals['source_product_codes']))),
                    'normalized_product_code': product_code,
                    'product_name': totals['product_name'],
                    'sales_category': sales_category,
                    'quantity_text': str(totals['quantity']),
                    'tax_excluded_amount_text': str(totals['tax_excluded_amount']),
                    'gross_profit_amount_text': str(totals['gross_profit_amount']),
                })
                continue

            auto_created_products += 1
            if dry_run:
                continue
            product = Product.objects.create(
                code=product_code,
                name=totals['product_name'] or '名称未設定',
                owner_company=current_company,
                demand_source=current_company,
                created_from_sales_history=True,
                first_sales_history_synced_at=timezone.now(),
            )
            Inventory.objects.get_or_create(
                product=product,
                defaults={'current_quantity': 0, 'safety_stock': 20},
            )
            product_dict[product_code] = product

        if key in existing_sales:
            sales = existing_sales[key]
            changed = (
                sales.quantity != totals['quantity']
                or sales.tax_excluded_amount != totals['tax_excluded_amount']
                or sales.gross_profit_amount != totals['gross_profit_amount']
            )
            if changed:
                sales.quantity = totals['quantity']
                sales.tax_excluded_amount = totals['tax_excluded_amount']
                sales.gross_profit_amount = totals['gross_profit_amount']
                update_list.append(sales)
        else:
            create_list.append(SalesHistory(
                sales_id=make_sales_id(company, sold_date, customer_code, product_code, sales_category),
                product=product,
                sold_date=sold_date,
                quantity=totals['quantity'],
                customer=customer_code,
                sales_category=sales_category,
                tax_excluded_amount=totals['tax_excluded_amount'],
                gross_profit_amount=totals['gross_profit_amount'],
                company=company,
            ))

    if not dry_run:
        if create_list:
            SalesHistory.objects.bulk_create(create_list)
        if update_list:
            SalesHistory.objects.bulk_update(
                update_list,
                ['quantity', 'tax_excluded_amount', 'gross_profit_amount'],
            )

    return {
        'aggregated': len(aggregated),
        'created': len(create_list),
        'updated': len(update_list),
        'auto_created_products': auto_created_products,
        'other_company_products': other_company_product_rows,
        'missing_products': len(missing_product_rows),
        'skipped': len(skipped_rows),
        'skip_rows': skipped_rows + missing_product_rows,
    }


def import_sales_file_path(csv_file_path, current_company='IKUJI', dry_run=False):
    rows = read_csv_rows_from_path(csv_file_path)
    return import_sales_rows(rows, current_company=current_company, dry_run=dry_run)


def import_uploaded_sales_file(uploaded_file, current_company='IKUJI'):
    rows = read_csv_rows_from_bytes(uploaded_file.read())
    return import_sales_rows(rows, current_company=current_company)
