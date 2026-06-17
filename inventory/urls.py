from django.urls import path
from . import views

urlpatterns = [
    path('', views.planning_dashboard, name='planning_dashboard'),
    path('about/', views.about_app, name='about_app'),
    path('guide/', views.operation_guide, name='operation_guide'),
    
    # 商品マスタの画面直接更新・削除
    path('update-product-config/<int:product_id>/', views.update_product_config, name='update_product_config'),
    path('delete-product/<int:product_id>/', views.delete_product, name='delete_product'),
    
    # 発注計画のステータス更新・削除
    path('update-order-status/<int:order_id>/', views.update_order_status, name='update_order_status'),
    path('delete-order-plan/<int:order_id>/', views.delete_order_plan, name='delete_order_plan'),
    path('bulk-create-order-plan/', views.bulk_create_order_plan, name='bulk_create_order_plan'),
    
    # 入荷予定の画面直接更新・個別削除
    path('update-arrival-schedule/<int:arrival_id>/', views.update_arrival_schedule, name='update_arrival_schedule'),
    path('delete-arrival-schedule/<int:arrival_id>/', views.delete_arrival_schedule, name='delete_arrival_schedule'),
    
    # 各種CSVインポート・エクスポート
    path('import-products/', views.import_products_csv, name='import_products_csv'),
    path('import-inventory/', views.import_inventory_csv, name='import_inventory_csv'),
    path('import-sales/', views.import_sales_csv, name='import_sales_csv'),
    path('import-arrivals/', views.import_arrivals_csv, name='import_arrivals_csv'),
    path('delete-warehouse/<int:warehouse_id>/', views.delete_warehouse, name='delete_warehouse'), # ★この行を追加
    
    path('download-template/<str:template_type>/', views.download_csv_template, name='download_csv_template'),
    path('export-products/', views.export_products_csv, name='export_products_csv'),
    path('export-inventory/', views.export_inventory_csv, name='export_inventory_csv'),
    path('export-sales/', views.export_sales_csv, name='export_sales_csv'),
    path('export-arrivals/', views.export_arrivals_csv, name='export_arrivals_csv'),
    
    path('create-order/<int:product_id>/', views.create_order_plan, name='create_order_plan'),
    path('products/', views.product_list, name='product_list'),
    path('delete-sales-period/', views.delete_sales_history_period, name='delete_sales_history_period'),
]