# Generated for planning stock source priority.

import datetime

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0033_alter_akachanorderline_unique_together_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='inventory',
            name='inventory_date',
            field=models.DateField(blank=True, default=datetime.date(2026, 5, 31), null=True, verbose_name='在庫基準日'),
        ),
        migrations.AddField(
            model_name='inventory',
            name='stock_source',
            field=models.CharField(
                choices=[('ACTUAL', '実在庫CSV'), ('VALUATION', '棚卸資産反映')],
                default='VALUATION',
                max_length=20,
                verbose_name='在庫登録元',
            ),
        ),
    ]
