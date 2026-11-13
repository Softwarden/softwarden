"""The platform staff second factor (catalog/staff_second_factor.py): the TOTP secret as a
credentials slot on PlatformStaff (`totp_ciphertext`, `totp_key_id`, `totp_set_at`; Fernet, core/credentials.py), when
its enrollment was confirmed, and the last accepted step (no replay). Nullable or empty columns: existing staff start
not enrolled. Forward-only."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_rate_counters'),
    ]

    operations = [
        migrations.AddField(
            model_name='platformstaff',
            name='totp_ciphertext',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='platformstaff',
            name='totp_confirmed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformstaff',
            name='totp_key_id',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='platformstaff',
            name='totp_last_step',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformstaff',
            name='totp_set_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
