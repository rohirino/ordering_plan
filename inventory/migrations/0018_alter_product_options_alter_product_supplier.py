from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0017_inventory_state'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='product',
            options={'verbose_name': '共通商品マスタ', 'verbose_name_plural': '共通商品マスタ'},
        ),
        migrations.AlterField(
            model_name='product',
            name='supplier',
            field=models.CharField(blank=True, max_length=100, null=True, verbose_name='仕入先'),
        ),
    ]
