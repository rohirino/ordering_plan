from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0018_alter_product_options_alter_product_supplier'),
    ]

    operations = [
        migrations.AddField(
            model_name='productvariant',
            name='include_in_planning_inventory',
            field=models.BooleanField(default=True, verbose_name='発注計画在庫へ反映'),
        ),
    ]
