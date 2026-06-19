from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0014_alter_warehouse_name_alter_warehouse_unique_together'),
    ]

    operations = [
        migrations.AddField(
            model_name='saleshistory',
            name='sales_category',
            field=models.CharField(blank=True, default='売上', max_length=20, verbose_name='区分'),
        ),
        migrations.AddField(
            model_name='saleshistory',
            name='tax_excluded_amount',
            field=models.IntegerField(default=0, verbose_name='税抜金額'),
        ),
        migrations.AddField(
            model_name='saleshistory',
            name='gross_profit_amount',
            field=models.IntegerField(default=0, verbose_name='粗利金額'),
        ),
    ]
