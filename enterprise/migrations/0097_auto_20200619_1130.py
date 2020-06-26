# Generated by Django 2.2.12 on 2020-06-19 16:30

import collections

import jsonfield.encoder
import jsonfield.fields

from django.db import migrations

import enterprise.validators


class Migration(migrations.Migration):

    dependencies = [
        ('enterprise', '0096_enterprise_catalog_admin_role'),
    ]

    operations = [
        migrations.AlterField(
            model_name='enterprisecatalogquery',
            name='content_filter',
            field=jsonfield.fields.JSONField(blank=True, default={}, dump_kwargs={'cls': jsonfield.encoder.JSONEncoder, 'indent': 4, 'separators': (',', ':')}, help_text="Query parameters which will be used to filter the discovery service's search/all endpoint results, specified as a JSON object. An empty JSON object means that all available content items will be included in the catalog.", load_kwargs={'object_pairs_hook': collections.OrderedDict}, null=True, validators=[enterprise.validators.validate_content_filter_fields]),
        ),
        migrations.AlterField(
            model_name='enterprisecustomercatalog',
            name='content_filter',
            field=jsonfield.fields.JSONField(blank=True, default={}, dump_kwargs={'cls': jsonfield.encoder.JSONEncoder, 'indent': 4, 'separators': (',', ':')}, help_text="Query parameters which will be used to filter the discovery service's search/all endpoint results, specified as a Json object. An empty Json object means that all available content items will be included in the catalog.", load_kwargs={'object_pairs_hook': collections.OrderedDict}, null=True, validators=[enterprise.validators.validate_content_filter_fields]),
        ),
        migrations.AlterField(
            model_name='historicalenterprisecustomercatalog',
            name='content_filter',
            field=jsonfield.fields.JSONField(blank=True, default={}, dump_kwargs={'cls': jsonfield.encoder.JSONEncoder, 'indent': 4, 'separators': (',', ':')}, help_text="Query parameters which will be used to filter the discovery service's search/all endpoint results, specified as a Json object. An empty Json object means that all available content items will be included in the catalog.", load_kwargs={'object_pairs_hook': collections.OrderedDict}, null=True, validators=[enterprise.validators.validate_content_filter_fields]),
        ),
    ]