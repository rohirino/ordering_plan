import pandas as pd

# 1. 読み込み (まずはヘッダーを読み込まず、後で列を指定する方式)
# ヘッダーは3行目にあるので、ヘッダーなしで読み込み、後で整理します
df = pd.read_csv('raw_sales_data.csv', skiprows=3, header=None, encoding='cp932')

# 2. 列に名前をつける (列の順番が決まっているならこれが確実です)
# CSVの列順: コード, 得意先名, 伝票日付, 区分, 仕入先, コード.1, 商品名, 数量, 税抜金額, 粗利金額
df.columns = ['得意先コード', '得意先名', '日付', '区分', '仕入先', '商品コード', '商品名', '数量', '税抜金額', '粗利金額']

# 3. 日付変換 (形式を指定せず自動判定させる)
df['日付'] = pd.to_datetime(df['日付'], errors='coerce')

# 4. 数量の掃除
df['数量'] = pd.to_numeric(df['数量'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

# 5. 抽出
df = df[['日付', '得意先コード', '得意先名', '商品コード', '商品名', '数量']]

# 6. 保存
df.to_csv('cleaned_sales_data.csv', index=False, encoding='utf-8-sig')
print("加工完了！cleaned_sales_data.csv ができました。")