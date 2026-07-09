from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0035_productstockoutperiod'),
    ]

    operations = [
        migrations.CreateModel(
            name='ActualStockSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stock_date', models.DateField(verbose_name='実在庫日')),
                ('quantity', models.IntegerField(default=0, verbose_name='在庫数')),
                ('owner_company', models.CharField(choices=[('IKUJI', '日本育児'), ('SELECT', 'ペットセレクト')], default='IKUJI', max_length=20, verbose_name='資産会社')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('product_variant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='inventory.productvariant', verbose_name='状態SKU')),
                ('warehouse', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='inventory.warehouse', verbose_name='倉庫')),
            ],
            options={
                'verbose_name': '実在庫状態SKU明細',
                'verbose_name_plural': '実在庫状態SKU明細',
                'unique_together': {('stock_date', 'product_variant', 'warehouse')},
            },
        ),
    ]
