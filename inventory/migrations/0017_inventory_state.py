from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0016_inventory_valuation'),
    ]

    operations = [
        migrations.CreateModel(
            name='InventoryState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('state_code', models.CharField(max_length=3, unique=True, verbose_name='状態コード')),
                ('state_name', models.CharField(max_length=100, verbose_name='状態名')),
            ],
            options={
                'verbose_name': '在庫状態マスタ',
                'verbose_name_plural': '在庫状態マスタ',
            },
        ),
    ]
