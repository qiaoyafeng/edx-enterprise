# Generated by Django 2.2.14 on 2020-07-28 18:52

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('enterprise', '0103_remove_marked_done'),
    ]

    operations = [
        migrations.AddField(
            model_name='enterprisecustomercatalog',
            name='sync_enterprise_catalog_query',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='historicalenterprisecustomercatalog',
            name='sync_enterprise_catalog_query',
            field=models.BooleanField(default=False),
        ),
    ]