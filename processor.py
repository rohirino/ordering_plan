import pandas as pd

# 1. 読み込み (ヘッダーのスキップは2行)
df = pd.read_csv('raw_sales_data.csv', skiprows=2, encoding='cp932')  # Shift_JISのエンコードで読み込む場合はcp932を指定

# 2. カラム名を予測システム用に統一
df = df.rename(columns={'コード': '得意先コード', 'コード.1': '商品コード', '伝票日付': '日付'})

# 3. 日付変換と数量の掃除
df['日付'] = pd.to_datetime(df['日付'], format='%Y年 %m月 %d日', errors='coerce')
df['数量'] = pd.to_numeric(df['数量'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

# 4. 必要なデータだけ抽出
df = df[['日付', '得意先コード', '得意先名', '商品コード', '商品名', '数量']]

# 5. 保存
df.to_csv('cleaned_sales_data.csv', index=False, encoding='utf-8-sig')
print("加工完了！cleaned_sales_data.csv ができました。") 