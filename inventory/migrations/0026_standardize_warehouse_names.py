from django.db import migrations


WAREHOUSE_NAME_ALIASES = {
    'ニチイク在庫': 'ﾆﾁｲｸ物流',
    '岸和田在庫': '岸和田倉庫',
    '西松屋預託': '西松屋',
}


def standardize_warehouse_names(apps, schema_editor):
    Warehouse = apps.get_model('inventory', 'Warehouse')
    for old_name, new_name in WAREHOUSE_NAME_ALIASES.items():
        old_warehouse = Warehouse.objects.filter(name=old_name, owner_company='IKUJI').first()
        if not old_warehouse:
            continue
        if Warehouse.objects.filter(name=new_name, owner_company='IKUJI').exists():
            continue
        old_warehouse.name = new_name
        old_warehouse.save(update_fields=['name'])


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0025_warehouse_is_active'),
    ]

    operations = [
        migrations.RunPython(standardize_warehouse_names, migrations.RunPython.noop),
    ]
