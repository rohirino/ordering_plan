# Generated for product-specific stockout period adjustments.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0034_inventory_stock_source'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductStockoutPeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('start_date', models.DateField(verbose_name='欠品開始日')),
                ('end_date', models.DateField(verbose_name='欠品終了日')),
                ('note', models.CharField(blank=True, default='', max_length=100, verbose_name='メモ')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='登録日時')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stockout_periods', to='inventory.product', verbose_name='商品')),
            ],
            options={
                'verbose_name': '商品別欠品期間',
                'verbose_name_plural': '商品別欠品期間',
                'ordering': ['-start_date', '-end_date'],
            },
        ),
    ]
