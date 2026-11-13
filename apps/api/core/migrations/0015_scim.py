"""SCIM (enterprise, ee/scim): ScimToken (a hashed bearer token per organization for the identity provider's
provisioning service), ScimUser and ScimGroup (users and groups as the provider provisioned them) and ScimGroupMember
(the through table). Composite foreign keys keep a provisioned user's membership and invitation, and a group
member's group and user, in the row's organization. New tables only; forward-only."""

import django.db.models.deletion
import django.db.models.functions.text
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_saml'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ScimGroup',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('display_name', models.CharField(max_length=300)),
                ('external_id', models.CharField(blank=True, default='', max_length=255)),
                ('version', models.PositiveIntegerField(default=1)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='ScimUser',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('external_id', models.CharField(blank=True, default='', max_length=255)),
                ('user_name', models.CharField(max_length=255)),
                ('given_name', models.CharField(blank=True, default='', max_length=150)),
                ('family_name', models.CharField(blank=True, default='', max_length=150)),
                ('formatted_name', models.CharField(blank=True, default='', max_length=300)),
                ('display_name', models.CharField(blank=True, default='', max_length=300)),
                ('emails', models.JSONField(blank=True, default=list)),
                ('active', models.BooleanField(default=True)),
                ('version', models.PositiveIntegerField(default=1)),
                ('invitation', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='scim_users', to='core.invitation')),
                ('membership', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='scim_users', to='core.membership')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='scim_users', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['created_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='ScimGroupMember',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='member_links', to='core.scimgroup')),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='group_links', to='core.scimuser')),
            ],
        ),
        migrations.AddField(
            model_name='scimgroup',
            name='members',
            field=models.ManyToManyField(related_name='scim_groups', through='core.ScimGroupMember', to='core.scimuser'),
        ),
        migrations.CreateModel(
            name='ScimToken',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=100)),
                ('token_hash', models.CharField(max_length=64, unique=True)),
                ('prefix', models.CharField(db_index=True, max_length=16)),
                ('expires_at', models.DateTimeField()),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='core.organization')),
            ],
            options={
                'ordering': ['-created_at'],
                'constraints': [models.UniqueConstraint(fields=('organization', 'id'), name='core_scimtoken_org_id')],
            },
        ),
        migrations.AddIndex(
            model_name='scimuser',
            index=models.Index(fields=['organization', 'external_id'], name='core_scimuser_external_id'),
        ),
        migrations.AddConstraint(
            model_name='scimuser',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='core_scimuser_org_id'),
        ),
        migrations.AddConstraint(
            model_name='scimuser',
            constraint=models.UniqueConstraint(models.F('organization'), django.db.models.functions.text.Lower('user_name'), name='core_scimuser_user_name'),
        ),
        migrations.AddIndex(
            model_name='scimgroupmember',
            index=models.Index(fields=['organization', 'user'], name='core_scimgroupmember_user'),
        ),
        migrations.AddConstraint(
            model_name='scimgroupmember',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='core_scimgroupmember_org_id'),
        ),
        migrations.AddConstraint(
            model_name='scimgroupmember',
            constraint=models.UniqueConstraint(fields=('group', 'user'), name='core_scimgroupmember_unique'),
        ),
        migrations.AddConstraint(
            model_name='scimgroup',
            constraint=models.UniqueConstraint(fields=('organization', 'id'), name='core_scimgroup_org_id'),
        ),
        migrations.AddConstraint(
            model_name='scimgroup',
            constraint=models.UniqueConstraint(models.F('organization'), django.db.models.functions.text.Lower('display_name'), name='core_scimgroup_display_name'),
        ),
        migrations.AddConstraint(
            model_name='scimuser',
            constraint=models.UniqueConstraint(condition=models.Q(('membership__isnull', False)), fields=('organization', 'membership'), name='core_scimuser_one_per_membership'),
        ),
        migrations.RunSQL(
            sql="ALTER TABLE core_scimuser ADD CONSTRAINT core_scimuser_org_membership_fk FOREIGN KEY (organization_id, membership_id) "
            "REFERENCES catalog_membership (organization_id, id) DEFERRABLE INITIALLY DEFERRED;",
            reverse_sql="ALTER TABLE core_scimuser DROP CONSTRAINT core_scimuser_org_membership_fk;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE core_scimuser ADD CONSTRAINT core_scimuser_org_invitation_fk FOREIGN KEY (organization_id, invitation_id) "
            "REFERENCES core_invitation (organization_id, id) DEFERRABLE INITIALLY DEFERRED;",
            reverse_sql="ALTER TABLE core_scimuser DROP CONSTRAINT core_scimuser_org_invitation_fk;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE core_scimgroupmember ADD CONSTRAINT core_scimgroupmember_org_group_fk FOREIGN KEY (organization_id, group_id) "
            "REFERENCES core_scimgroup (organization_id, id) DEFERRABLE INITIALLY DEFERRED;",
            reverse_sql="ALTER TABLE core_scimgroupmember DROP CONSTRAINT core_scimgroupmember_org_group_fk;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE core_scimgroupmember ADD CONSTRAINT core_scimgroupmember_org_user_fk FOREIGN KEY (organization_id, user_id) "
            "REFERENCES core_scimuser (organization_id, id) DEFERRABLE INITIALLY DEFERRED;",
            reverse_sql="ALTER TABLE core_scimgroupmember DROP CONSTRAINT core_scimgroupmember_org_user_fk;",
        ),
    ]
