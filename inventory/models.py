import datetime
from django.db import models

class Product(models.Model):
    """共通商品マスタ"""
    LOT_RULE_CHOICES = (
        ('ROUND_UP_LOT', '常にロット単位での積み上げ（ケース単位切り上げ）'),
        ('MIN_LOT_ONLY', '最低ロット満たした後は1個単位での積み上げ（バラ混載可）'),
    )
    
    TREND_DAYS_CHOICES = (
        (90, '90日間（標準）'),
        (120, '120日間（約4ヶ月）'),
        (150, '150日間（約5ヶ月）'),
        (180, '180日間（半年）'),
    )

    ABC_RANK_CHOICES = (
        ('A', 'Aランク（主力・最重要）'),
        ('B', 'Bランク（定番・中動態）'),
        ('C', 'Cランク（準定番・低動態）'),
        ('DEAD', '処分推奨（不動在庫）'),
    )

    # ★新設：マルチカンパニー対応の会社定義
    COMPANY_CHOICES = (
        ('IKUJI', '日本育児'),
        ('SELECT', 'ペットセレクト'),
    )

    code = models.CharField(verbose_name="商品コード", max_length=50, unique=True)
    name = models.CharField(verbose_name="商品名", max_length=255)
    
    # ★新設：マスタがどちらの会社に所属するか
    owner_company = models.CharField(verbose_name="所有会社", max_length=20, choices=COMPANY_CHOICES, default='IKUJI')
    # ★新設：需要予測の計算時にどちらの会社の販売履歴をベースにするか
    demand_source = models.CharField(verbose_name="需要参照元売上", max_length=20, choices=COMPANY_CHOICES, default='IKUJI')

    price = models.IntegerField(verbose_name="標準原価", null=True, blank=True, default=0)
    supplier = models.CharField(verbose_name="仕入先", max_length=100, blank=True, null=True)
    lead_time = models.IntegerField(verbose_name="リードタイム（日数）", default=90)
    order_lot = models.IntegerField(verbose_name="発注ロット", default=1)
    lot_rule = models.CharField(verbose_name="超過時積み上げルール", max_length=20, choices=LOT_RULE_CHOICES, default='ROUND_UP_LOT')
    trend_days = models.IntegerField(verbose_name="長期トレンド計算日数", choices=TREND_DAYS_CHOICES, default=90)
    is_excluded = models.BooleanField(verbose_name="管理外フラグ", default=False)
    is_discontinued = models.BooleanField(verbose_name="廃盤フラグ", default=False)
    abc_rank = models.CharField(verbose_name="ABCランク評価", max_length=10, choices=ABC_RANK_CHOICES, default='C')
    allow_dead_order = models.BooleanField(verbose_name="処分品発注許可フラグ", default=False)
    created_from_valuation = models.BooleanField(verbose_name="棚卸CSVから自動生成", default=False)
    created_from_sales_history = models.BooleanField(verbose_name="販売履歴CSVから自動生成", default=False)
    first_sales_history_synced_at = models.DateTimeField(verbose_name="販売履歴CSV初回生成日時", null=True, blank=True)
    last_valuation_synced_at = models.DateTimeField(verbose_name="棚卸CSV最終反映日時", null=True, blank=True)
    last_valuation_inventory_date = models.DateField(verbose_name="棚卸CSV対象棚卸日", null=True, blank=True)
    last_valuation_name_updated = models.BooleanField(verbose_name="棚卸CSVで商品名更新", default=False)
    
    trend_max = models.FloatField(verbose_name="トレンド上限", default=2.0)
    trend_min = models.FloatField(verbose_name="トレンド下限", default=0.5)

    def __str__(self):
        return f"[{self.get_owner_company_display()}] [{self.code}] {self.name}"

    class Meta:
        verbose_name = "共通商品マスタ"
        verbose_name_plural = "共通商品マスタ"


class Warehouse(models.Model):
    """倉庫マスタ"""
    COMPANY_CHOICES = (('IKUJI', '日本育児'), ('SELECT', 'ペットセレクト'))
    
    # ★修正：unique=True を削除
    name = models.CharField(verbose_name="倉庫名", max_length=100)
    is_transit = models.BooleanField(verbose_name="移動中フラグ", default=False)
    is_active = models.BooleanField(verbose_name="運用中フラグ", default=True)
    owner_company = models.CharField(verbose_name="所有会社", max_length=20, choices=COMPANY_CHOICES, default='IKUJI')

    def __str__(self):
        return f"[{self.get_owner_company_display()}] {self.name}"

    class Meta:
        verbose_name = "倉庫マスタ"
        verbose_name_plural = "倉庫マスタ"
        # ★新設：会社と倉庫名の組み合わせで重複を判定する制約へ変更
        unique_together = ('name', 'owner_company')

class Inventory(models.Model):
    """現在庫データ"""
    product = models.OneToOneField(Product, on_delete=models.CASCADE, verbose_name="商品")
    current_quantity = models.IntegerField(verbose_name="現在庫数（全倉庫合算）", default=0)
    safety_stock = models.IntegerField(verbose_name="安全在庫数", default=20)
    inventory_date = models.DateField(verbose_name="棚卸日", null=True, blank=True, default=datetime.date(2026, 5, 31))
    updated_at = models.DateTimeField(verbose_name="データ更新日時", auto_now=True)

    class Meta:
        verbose_name = "現在庫"
        verbose_name_plural = "現在庫"


class WarehouseInventory(models.Model):
    """倉庫別在庫"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, verbose_name="倉庫")
    quantity = models.IntegerField(verbose_name="在庫数", default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "倉庫別在庫"
        verbose_name_plural = "倉庫別在庫"
        unique_together = ('product', 'warehouse')


class ProductVariant(models.Model):
    """状態コード別SKU"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    state_code = models.CharField(verbose_name="状態コード", max_length=3)
    state_name = models.CharField(verbose_name="状態名", max_length=100, blank=True, default='')
    current_cost = models.IntegerField(verbose_name="現在原価", default=0)
    include_in_planning_inventory = models.BooleanField(verbose_name="発注計画在庫へ反映", default=True)

    def __str__(self):
        return f"[{self.product.code}-{self.state_code}] {self.product.name} {self.state_name}"

    class Meta:
        verbose_name = "状態別SKU"
        verbose_name_plural = "状態別SKU"
        unique_together = ('product', 'state_code')


class InventoryState(models.Model):
    """在庫状態マスタ"""
    state_code = models.CharField(verbose_name="状態コード", max_length=3, unique=True)
    state_name = models.CharField(verbose_name="状態名", max_length=100)

    def __str__(self):
        return f"[{self.state_code}] {self.state_name}"

    class Meta:
        verbose_name = "在庫状態マスタ"
        verbose_name_plural = "在庫状態マスタ"


class ProductVariantCostHistory(models.Model):
    """状態別SKUの原価履歴"""
    product_variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, verbose_name="状態別SKU")
    effective_date = models.DateField(verbose_name="適用日")
    unit_cost = models.IntegerField(verbose_name="原価", default=0)
    source = models.CharField(verbose_name="取込元", max_length=50, blank=True, default='棚卸CSV')

    class Meta:
        verbose_name = "状態別SKU原価履歴"
        verbose_name_plural = "状態別SKU原価履歴"
        unique_together = ('product_variant', 'effective_date')


class InventoryValuationSnapshot(models.Model):
    """棚卸資産評価スナップショット"""
    COMPANY_CHOICES = (('IKUJI', '日本育児'), ('SELECT', 'ペットセレクト'))
    inventory_date = models.DateField(verbose_name="棚卸日")
    product_variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, verbose_name="状態別SKU")
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, verbose_name="倉庫")
    quantity = models.IntegerField(verbose_name="数量", default=0)
    unit_cost = models.IntegerField(verbose_name="原価", default=0)
    amount = models.IntegerField(verbose_name="在庫金額", default=0)
    owner_company = models.CharField(verbose_name="所有会社", max_length=20, choices=COMPANY_CHOICES, default='IKUJI')

    class Meta:
        verbose_name = "棚卸資産評価"
        verbose_name_plural = "棚卸資産評価"
        unique_together = ('inventory_date', 'product_variant', 'warehouse', 'owner_company')


class SalesHistory(models.Model):
    """日次販売履歴データ"""
    COMPANY_CHOICES = (('IKUJI', '日本育児'), ('SELECT', 'ペットセレクト'))
    sales_id = models.CharField(verbose_name="日次販売履歴ID", max_length=50, unique=True, null=True, blank=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    sold_date = models.DateField(verbose_name="伝票日付")
    quantity = models.IntegerField(verbose_name="販売数")
    customer = models.CharField(verbose_name="得意先名", max_length=100, blank=True, null=True)
    sales_category = models.CharField(verbose_name="区分", max_length=20, blank=True, default='売上')
    tax_excluded_amount = models.IntegerField(verbose_name="税抜金額", default=0)
    gross_profit_amount = models.IntegerField(verbose_name="粗利金額", default=0)
    is_advance_order = models.BooleanField(verbose_name="先付け受注（需要計算から除外）", default=False)
    # ★新設：売上データがどちらの会社の実績か
    company = models.CharField(verbose_name="データ所属会社", max_length=20, choices=COMPANY_CHOICES, default='IKUJI')

    class Meta:
        verbose_name = "日次販売履歴"
        verbose_name_plural = "日次販売履歴"


class ShipmentSchedule(models.Model):
    """先付け受注を含む出荷予定データ"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    shipment_date = models.DateField(verbose_name="出荷予定日")
    destination = models.CharField(verbose_name="向け先", max_length=100, blank=True, default='')
    quantity = models.IntegerField(verbose_name="出荷予定数量")


class ArrivalSchedule(models.Model):
    """入荷予定データ（発注残）"""
    STATUS_CHOICES = (('確定', '確定'), ('高確度', '高確度'), ('希望', '希望'))
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    arrival_date = models.DateField(verbose_name="入荷予定日")
    quantity = models.IntegerField(verbose_name="入荷予定数量")
    status = models.CharField(verbose_name="確度ステータス", max_length=10, choices=STATUS_CHOICES)


class ImportLog(models.Model):
    """CSV等の取込結果ログ"""
    STATUS_CHOICES = (
        ('success', '成功'),
        ('warning', '警告あり'),
        ('error', '失敗'),
    )
    DASHBOARD_CHOICES = (
        ('planning', '在庫・発注計画'),
        ('product_master', '商品マスタ登録'),
        ('sales_history', '日次販売履歴'),
        ('arrivals', '入荷予定・発注残'),
        ('valuation', '棚卸資産評価'),
    )

    dashboard = models.CharField(verbose_name="対象Dashboard", max_length=30, choices=DASHBOARD_CHOICES)
    import_type = models.CharField(verbose_name="取込種別", max_length=50)
    status = models.CharField(verbose_name="結果", max_length=10, choices=STATUS_CHOICES)
    company = models.CharField(verbose_name="対象会社", max_length=20, blank=True, default='')
    filename = models.CharField(verbose_name="ファイル名", max_length=255, blank=True, default='')
    summary = models.CharField(verbose_name="概要", max_length=255)
    details = models.TextField(verbose_name="詳細", blank=True, default='')
    error_count = models.IntegerField(verbose_name="エラー件数", default=0)
    warning_count = models.IntegerField(verbose_name="警告件数", default=0)
    created_at = models.DateTimeField(verbose_name="記録日時", auto_now_add=True)

    class Meta:
        verbose_name = "取込ログ"
        verbose_name_plural = "取込ログ"
        ordering = ['-created_at']


class SalesImportSkip(models.Model):
    """販売履歴CSVで登録されなかった明細を、取込ログ単位で保持する。"""
    import_log = models.ForeignKey(ImportLog, on_delete=models.CASCADE, related_name='sales_skips', verbose_name='取込ログ')
    source_rows = models.CharField(verbose_name='元データ行', max_length=100, blank=True, default='')
    reason = models.CharField(verbose_name='スキップ理由', max_length=100)
    sold_date_text = models.CharField(verbose_name='伝票日付', max_length=50, blank=True, default='')
    customer_code = models.CharField(verbose_name='得意先コード', max_length=100, blank=True, default='')
    source_product_code = models.CharField(verbose_name='元商品コード', max_length=100, blank=True, default='')
    normalized_product_code = models.CharField(verbose_name='正規化商品コード', max_length=50, blank=True, default='')
    product_name = models.CharField(verbose_name='商品名', max_length=255, blank=True, default='')
    sales_category = models.CharField(verbose_name='区分', max_length=50, blank=True, default='')
    quantity_text = models.CharField(verbose_name='数量', max_length=50, blank=True, default='')
    tax_excluded_amount_text = models.CharField(verbose_name='税抜金額', max_length=50, blank=True, default='')
    gross_profit_amount_text = models.CharField(verbose_name='粗利金額', max_length=50, blank=True, default='')

    class Meta:
        verbose_name = '販売履歴取込スキップ明細'
        verbose_name_plural = '販売履歴取込スキップ明細'
        ordering = ['id']


class Order(models.Model):
    """発注計画データ"""
    STATUS_CHOICES = (('計画中', '計画中'), ('発注済', '発注済'), ('入庫済', '入庫済'))
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    quantity = models.IntegerField(verbose_name="発注数量")
    status = models.CharField(verbose_name="ステータス", max_length=10, choices=STATUS_CHOICES, default='計画中')
    created_at = models.DateTimeField(verbose_name="作成日時", auto_now_add=True)
