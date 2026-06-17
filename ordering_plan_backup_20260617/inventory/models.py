import datetime
from django.db import models


def last_month_end():
    today = datetime.date.today()
    first_day = today.replace(day=1)
    return first_day - datetime.timedelta(days=1)

class Product(models.Model):
    """商品マスタ"""
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
    supplier = models.CharField(verbose_name="仕仕入先", max_length=100, blank=True, null=True)
    lead_time = models.IntegerField(verbose_name="リードタイム（日数）", default=30)
    order_lot = models.IntegerField(verbose_name="発注ロット", default=1)
    lot_rule = models.CharField(verbose_name="超過時積み上げルール", max_length=20, choices=LOT_RULE_CHOICES, default='ROUND_UP_LOT')
    trend_days = models.IntegerField(verbose_name="長期トレンド計算日数", choices=TREND_DAYS_CHOICES, default=90)
    is_excluded = models.BooleanField(verbose_name="管理外フラグ", default=False)
    abc_rank = models.CharField(verbose_name="ABCランク評価", max_length=10, choices=ABC_RANK_CHOICES, default='C')
    allow_dead_order = models.BooleanField(verbose_name="処分品発注許可フラグ", default=False)
    
    trend_max = models.FloatField(verbose_name="トレンド上限", default=2.0)
    trend_min = models.FloatField(verbose_name="トレンド下限", default=0.5)

    def __str__(self):
        return f"[{self.get_owner_company_display()}] [{self.code}] {self.name}"

    class Meta:
        verbose_name = "商品マスタ"
        verbose_name_plural = "商品マスタ"


class Warehouse(models.Model):
    """倉庫マスタ"""
    COMPANY_CHOICES = (('IKUJI', '日本育児'), ('SELECT', 'ペットセレクト'))
    
    # ★修正：unique=True を削除
    name = models.CharField(verbose_name="倉庫名", max_length=100)
    is_transit = models.BooleanField(verbose_name="移動中フラグ", default=False)
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
    inventory_date = models.DateField(verbose_name="棚卸日", null=True, blank=True, default=last_month_end)
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


class SalesHistory(models.Model):
    """日次販売履歴データ"""
    COMPANY_CHOICES = (('IKUJI', '日本育児'), ('SELECT', 'ペットセレクト'))
    sales_id = models.CharField(verbose_name="日次販売履歴ID", max_length=50, unique=True, null=True, blank=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    sold_date = models.DateField(verbose_name="伝票日付")
    quantity = models.IntegerField(verbose_name="販売数")
    customer = models.CharField(verbose_name="得意先名", max_length=100, blank=True, null=True)
    # ★新設：売上データがどちらの会社の実績か
    company = models.CharField(verbose_name="データ所属会社", max_length=20, choices=COMPANY_CHOICES, default='IKUJI')

    class Meta:
        verbose_name = "日次販売履歴"
        verbose_name_plural = "日次販売履歴"


class ShipmentSchedule(models.Model):
    """出荷予定データ"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    shipment_date = models.DateField(verbose_name="出荷予定日")
    quantity = models.IntegerField(verbose_name="出荷予定数量")


class ArrivalSchedule(models.Model):
    """入荷予定データ（発注残）"""
    STATUS_CHOICES = (('確定', '確定'), ('高確度', '高確度'), ('希望', '希望'))
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    arrival_date = models.DateField(verbose_name="入荷予定日")
    quantity = models.IntegerField(verbose_name="入荷予定数量")
    status = models.CharField(verbose_name="確度ステータス", max_length=10, choices=STATUS_CHOICES)


class Order(models.Model):
    """発注計画データ"""
    STATUS_CHOICES = (('計画中', '計画中'), ('発注済', '発注済'), ('入庫済', '入庫済'))
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    quantity = models.IntegerField(verbose_name="発注数量")
    status = models.CharField(verbose_name="ステータス", max_length=10, choices=STATUS_CHOICES, default='計画中')
    created_at = models.DateTimeField(verbose_name="作成日時", auto_now_add=True)
