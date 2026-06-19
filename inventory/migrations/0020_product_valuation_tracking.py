from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0019_productvariant_include_in_planning_inventory'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='created_from_valuation',
            field=models.BooleanField(default=False, verbose_name='棚卸CSVから自動生成'),
        ),
        migrations.AddField(
            model_name='product',
            name='last_valuation_inventory_date',
            field=models.DateField(blank=True, null=True, verbose_name='棚卸CSV対象棚卸日'),
        ),
        migrations.AddField(
            model_name='product',
            name='last_valuation_name_updated',
            field=models.BooleanField(default=False, verbose_name='棚卸CSVで商品名更新'),
        ),
        migrations.AddField(
            model_name='product',
            name='last_valuation_synced_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='棚卸CSV最終反映日時'),
        ),
    ]
