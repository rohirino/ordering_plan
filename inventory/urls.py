from django.urls import path
from . import views

urlpatterns = [
    path('', views.planning_dashboard, name='planning_dashboard'),
    path('product-master/', views.product_master_dashboard, name='product_master_dashboard'),
    path('arrivals/', views.arrivals_dashboard, name='arrivals_dashboard'),
    path('sales-history/', views.sales_history_dashboard, name='sales_history_dashboard'),
    path('valuation/', views.valuation_dashboard, name='valuation_dashboard'),
    path('about/', views.about_app, name='about_app'),
    path('guide/', views.operation_guide, name='operation_guide'),
    
    # 商品マスタの画面直接更新・削除
    path('create-product/', views.create_product, name='create_product'),
    path('update-product-config/<int:product_id>/', views.update_product_config, name='update_product_config'),
    path('bulk-update-products/', views.bulk_update_products, name='bulk_update_products'),
    path('create-inventory-state/', views.create_inventory_state, name='create_inventory_state'),
    path('update-inventory-state/<int:state_id>/', views.update_inventory_state, name='update_inventory_state'),
    path('update-product-variant-planning/<int:variant_id>/', views.update_product_variant_planning_flag, name='update_product_variant_planning_flag'),
    path('bulk-update-product-variant-planning/', views.bulk_update_product_variant_planning_flags, name='bulk_update_product_variant_planning_flags'),
    path('delete-product/<int:product_id>/', views.delete_product, name='delete_product'),
    
    # 発注計画のステータス更新・削除
    path('update-order-status/<int:order_id>/', views.update_order_status, name='update_order_status'),
    path('delete-order-plan/<int:order_id>/', views.delete_order_plan, name='delete_order_plan'),
    path('bulk-create-order-plan/', views.bulk_create_order_plan, name='bulk_create_order_plan'),
    
    # 入荷予定の画面直接更新・個別削除
    path('create-arrival-schedule/', views.create_arrival_schedule, name='create_arrival_schedule'),
    path('update-arrival-schedule/<int:arrival_id>/', views.update_arrival_schedule, name='update_arrival_schedule'),
    path('delete-arrival-schedule/<int:arrival_id>/', views.delete_arrival_schedule, name='delete_arrival_schedule'),
    path('create-shipment-schedule/<int:product_id>/', views.create_shipment_schedule, name='create_shipment_schedule'),
    path('delete-shipment-schedule/<int:shipment_id>/', views.delete_shipment_schedule, name='delete_shipment_schedule'),
    
    # 各種CSVインポート・エクスポート
    path('import-products/', views.import_products_csv, name='import_products_csv'),
    path('import-inventory/', views.import_inventory_csv, name='import_inventory_csv'),
    path('import-sales/', views.import_sales_csv, name='import_sales_csv'),
    path('sales-import-skips/<int:import_log_id>/', views.download_sales_import_skips, name='download_sales_import_skips'),
    path('update-sales-history-advance-order/<int:sales_id>/', views.update_sales_history_advance_order, name='update_sales_history_advance_order'),
    path('import-arrivals/', views.import_arrivals_csv, name='import_arrivals_csv'),
    path('import-valuation/', views.import_valuation_csv, name='import_valuation_csv'),
    path('import-inventory-states/', views.import_inventory_state_csv, name='import_inventory_state_csv'),
    path('sync-valuation-to-planning/', views.sync_valuation_to_planning, name='sync_valuation_to_planning'),
    path('delete-warehouse/<int:warehouse_id>/', views.delete_warehouse, name='delete_warehouse'), # ★この行を追加
    
    path('download-template/<str:template_type>/', views.download_csv_template, name='download_csv_template'),
    path('export-products/', views.export_products_csv, name='export_products_csv'),
    path('export-inventory/', views.export_inventory_csv, name='export_inventory_csv'),
    path('export-sales/', views.export_sales_csv, name='export_sales_csv'),
    path('export-arrivals/', views.export_arrivals_csv, name='export_arrivals_csv'),
    path('export-order-plans-csv/', views.export_order_plans_csv, name='export_order_plans_csv'),
    path('export-order-plans-excel/', views.export_order_plans_excel, name='export_order_plans_excel'),
    path('export-inventory-states/', views.export_inventory_states_csv, name='export_inventory_states_csv'),
    path('download-inventory-state-template/', views.download_inventory_state_template, name='download_inventory_state_template'),
    path('export-valuation-excel/', views.export_valuation_excel, name='export_valuation_excel'),
    path('download-valuation-template/', views.download_valuation_template, name='download_valuation_template'),
    path('export-valuation-pdf/', views.export_valuation_pdf, name='export_valuation_pdf'),
    
    path('create-order/<int:product_id>/', views.create_order_plan, name='create_order_plan'),
    path('products/', views.product_list, name='product_list'),
    path('delete-sales-period/', views.delete_sales_history_period, name='delete_sales_history_period'),
]
