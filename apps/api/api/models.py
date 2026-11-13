"""API tokens. Moved from the catalog app in the split; the table and constraint names keep their
`catalog_*` names (api/migrations/0001_initial.py)."""

from django.conf import settings
from django.db import models

from core.models import CATALOG_ORG_KEY, OrgScoped


class ApiToken(OrgScoped):
    """A script's credential. Only the SHA-256 of the secret is stored; `prefix` is shown to
    tell tokens apart. It acts as its owner, so never above the owner's role."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=12)
    read_only = models.BooleanField(default=True)
    expires_on = models.DateField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        db_table = "catalog_apitoken"
        ordering = ["-created_at"]
        constraints = [CATALOG_ORG_KEY]
