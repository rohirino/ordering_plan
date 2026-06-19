from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0020_product_valuation_tracking'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='is_discontinued',
            field=models.BooleanField(default=False, verbose_name='廃盤フラグ'),
        ),
    ]
