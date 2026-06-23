from django.contrib import admin
from .models import Product, Inventory, Warehouse, WarehouseInventory, SalesHistory, ShipmentSchedule, ArrivalSchedule, Order, ImportLog

class WarehouseInventoryInline(admin.TabularInline):
    model = WarehouseInventory
    extra = 1

@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'is_transit')
    search_fields = ('name',)

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'supplier', 'price', 'lead_time', 'order_lot', 'order_interval_days')
    search_fields = ('code', 'name', 'supplier')
    inlines = [WarehouseInventoryInline]

@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    # ★ 一覧列に「inventory_date(棚卸日)」を追加
    list_display = ('get_code', 'get_name', 'current_quantity', 'safety_stock', 'inventory_date', 'updated_at')
    search_fields = ('product__code', 'product__name')

    def get_code(self, obj): return obj.product.code
    get_code.short_description = '商品コード'
    def get_name(self, obj): return obj.product.name
    get_name.short_description = '商品名'

@admin.register(SalesHistory)
class SalesHistoryAdmin(admin.ModelAdmin):
    list_display = ('sales_id', 'sold_date', 'get_code', 'get_name', 'quantity', 'customer')
    list_filter = ('sold_date',)
    search_fields = ('product__code', 'product__name', 'customer')

    def get_code(self, obj): return obj.product.code
    get_code.short_description = '商品コード'
    def get_name(self, obj): return obj.product.name
    get_name.short_description = '商品名'

@admin.register(ArrivalSchedule)
class ArrivalScheduleAdmin(admin.ModelAdmin):
    list_display = ('arrival_date', 'get_code', 'get_name', 'quantity', 'status')
    list_filter = ('status', 'arrival_date')
    search_fields = ('product__code', 'product__name')

    def get_code(self, obj): return obj.product.code
    get_code.short_description = '商品コード'
    def get_name(self, obj): return obj.product.name
    get_name.short_description = '商品名'

@admin.register(ShipmentSchedule)
class ShipmentScheduleAdmin(admin.ModelAdmin):
    list_display = ('shipment_date', 'get_code', 'get_name', 'destination', 'quantity')
    list_filter = ('shipment_date',)
    search_fields = ('product__code', 'product__name', 'destination')

    def get_code(self, obj): return obj.product.code
    get_code.short_description = '商品コード'
    def get_name(self, obj): return obj.product.name
    get_name.short_description = '商品名'

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'order_date', 'expected_arrival_date', 'get_code', 'get_name', 'quantity', 'status')
    list_filter = ('status',)
    search_fields = ('product__code', 'product__name')

    def get_code(self, obj): return obj.product.code
    get_code.short_description = '商品コード'
    def get_name(self, obj): return obj.product.name
    get_name.short_description = '商品名'

@admin.register(ImportLog)
class ImportLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'dashboard', 'import_type', 'status', 'company', 'filename', 'error_count', 'warning_count', 'summary')
    list_filter = ('dashboard', 'import_type', 'status', 'company', 'created_at')
    search_fields = ('filename', 'summary', 'details')
    readonly_fields = ('created_at',)
