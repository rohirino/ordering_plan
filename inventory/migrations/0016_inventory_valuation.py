from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0015_saleshistory_amounts_and_category'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductVariant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('state_code', models.CharField(max_length=3, verbose_name='状態コード')),
                ('state_name', models.CharField(blank=True, default='', max_length=100, verbose_name='状態名')),
                ('current_cost', models.IntegerField(default=0, verbose_name='現在原価')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='inventory.product', verbose_name='商品')),
            ],
            options={
                'verbose_name': '状態別SKU',
                'verbose_name_plural': '状態別SKU',
                'unique_together': {('product', 'state_code')},
            },
        ),
        migrations.CreateModel(
            name='ProductVariantCostHistory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('effective_date', models.DateField(verbose_name='適用日')),
                ('unit_cost', models.IntegerField(default=0, verbose_name='原価')),
                ('source', models.CharField(blank=True, default='棚卸CSV', max_length=50, verbose_name='取込元')),
                ('product_variant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='inventory.productvariant', verbose_name='状態別SKU')),
            ],
            options={
                'verbose_name': '状態別SKU原価履歴',
                'verbose_name_plural': '状態別SKU原価履歴',
                'unique_together': {('product_variant', 'effective_date')},
            },
        ),
        migrations.CreateModel(
            name='InventoryValuationSnapshot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('inventory_date', models.DateField(verbose_name='棚卸日')),
                ('quantity', models.IntegerField(default=0, verbose_name='数量')),
                ('unit_cost', models.IntegerField(default=0, verbose_name='原価')),
                ('amount', models.IntegerField(default=0, verbose_name='在庫金額')),
                ('owner_company', models.CharField(choices=[('IKUJI', '日本育児'), ('SELECT', 'ペットセレクト')], default='IKUJI', max_length=20, verbose_name='所有会社')),
                ('product_variant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='inventory.productvariant', verbose_name='状態別SKU')),
                ('warehouse', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='inventory.warehouse', verbose_name='倉庫')),
            ],
            options={
                'verbose_name': '棚卸資産評価',
                'verbose_name_plural': '棚卸資産評価',
                'unique_together': {('inventory_date', 'product_variant', 'warehouse', 'owner_company')},
            },
        ),
    ]
