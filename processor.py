import pandas as pd

# 1. ヘッダーを自動判定させずに、まずは全データを読み込む
# 列数が多い場合、勝手に補完されて19列になっている可能性が高いです
df = pd.read_csv('20260616_1009売上明細表.csv', skiprows=3, encoding='cp932')

# 2. 現在の列名を確認して、必要なものだけを抽出する
# 実際の列名とずれている場合、まずは全列名を表示して確認しましょう
print("Pandasが認識した列名一覧:", df.columns.tolist())

# 3. 必要な列をマッピングする (これが重要！)
# '伝票日付' が存在することを確認してください
df = df.rename(columns={
    'コード': '得意先コード',
    '伝票日付': '日付',
    'コード.1': '商品コード',
    '商品名': '商品名',
    '数量': '数量'
})

# 4. 必要な列だけを残す
df = df[['日付', '得意先コード', '得意先名', '商品コード', '商品名', '数量']]

# 5. 日付と数量の整形
df['日付'] = pd.to_datetime(df['日付'], errors='coerce')
df['数量'] = pd.to_numeric(df['数量'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

# 6. 保存
df.to_csv('cleaned_sales_data.csv', index=False, encoding='utf-8-sig')
print("加工完了！")