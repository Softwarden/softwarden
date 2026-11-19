"""Audit events read back as the Audit log and the History tabs show them: a
label per action, the event's target and a one-line summary. Events are append-only: written by the services
(catalog/services.audit) and never changed or removed through the API.

Events store references as IDs. These helpers resolve them to current names when read, so an anonymized Person
shows as "Former person #n" everywhere, including old events."""

import copy
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Q

from core.dates import describe
from core.models import AuditEvent
from money.models import Contract, LicensePool, RenewalObligation

from . import customfields
from .models import (
    AgentAuthMode,
    AgentChannel,
    AgentPlatform,
    AgentRisk,
    AIModel,
    AIProvider,
    ApplicationInstance,
    AttachmentCategory,
    ChangeAssessment,
    CustomFieldType,
    DataCategory,
    Department,
    Integration,
    IntegrationAuthMethod,
    IntegrationDirection,
    Person,
    Relationship,
    RelationshipType,
    Resource,
    SoftwareCategory,
    Team,
    Vendor,
)
from .state import CONFIRMED_FIELDS

LABELS = {
    "resource.created": "Record created",
    "resource.updated": "Fields changed",
    "resource.archived": "Archived",
    "resource.restored": "Restored",
    "resource.deleted": "Deleted",
    "resource.undo_refused": "Undo refused",
    "review.recorded": "Review recorded",
    "relationship.created": "Relationship added",
    "relationship.updated": "Relationship changed",
    "relationship.deleted": "Relationship removed",
    "link.confirmed": "Link confirmed",
    "assessment.created": "Assessment created",
    "assessment.updated": "Assessment changed",
    "assessment.status_changed": "Assessment status changed",
    "assessment.snapshot_refreshed": "Snapshot refreshed",
    "assessment.stale": "Snapshot flagged stale",
    "renewal.created": "Renewal recorded",
    "renewal.updated": "Renewal changed",
    "renewal.archived": "Renewal archived",
    "renewal.restored": "Renewal restored",
    "renewal.deleted": "Renewal deleted",
    "renewal_cycle.updated": "Cycle terms changed",
    "renewal_cycle.decided": "Decision recorded",
    "renewal_cycle.completed": "Cycle completed",
    "renewal_cycle.established": "Next cycle established",
    "notification.snoozed": "Notifications snoozed",
    "notification.acknowledged": "Notification acknowledged",
    "notification.unsnoozed": "Snooze removed",
    "preference.updated": "Digest preference changed",
    "digest.retried": "Digest sent again",
    "instance.created": "Instance added",
    "tracked_version.created": "End-of-life tracking added",
    "tracked_version.updated": "End-of-life tracking changed",
    "tracked_version.deleted": "End-of-life tracking removed",
    "instance.updated": "Instance changed",
    "instance.deleted": "Instance deleted",
    "person.created": "Person added",
    "person.updated": "Person changed",
    "person.anonymized": "Person anonymized",
    "person.reassigned": "Responsibilities reassigned",  # offboarding (offboarding/reassign.py)
    "team.created": "Team added",
    "team.updated": "Team changed",
    "team.deleted": "Team deleted",
    "department.created": "Department added",
    "department.updated": "Department changed",
    "department.deleted": "Department deleted",
    "vendor.created": "Vendor added",
    "vendor.updated": "Vendor changed",
    "vendor.deleted": "Vendor deleted",
    "category.created": "Category added",
    "category.updated": "Category changed",
    "category.deleted": "Category deleted",
    "tag.updated": "Tag changed",
    "tag.created": "Tag added",  # catalog-as-code: a Tag file for a tag no record has yet
    "catalog_code.exported": "Catalog exported as code",
    "catalog_code.applied": "Catalog-as-code files applied",
    "organization.updated": "Settings changed",
    "organization.edition_changed": "Edition changed",
    "organization.export_requested": "Data export requested",
    "organization.export_ready": "Data export ready",
    "organization.export_failed": "Data export failed",
    "organization.export_expired": "Data export expired",
    "organization.export_downloaded": "Data export downloaded",
    "organization.deletion_requested": "Organization deletion scheduled",
    "organization.deletion_cancelled": "Organization deletion canceled",
    "organization.deletion_export_retried": "Final data export tried again",
    "organization.deletion_postponed": "Organization deletion postponed",
    # Platform admin console: the service operator acted (catalog/platform_admin.py)
    "organization.suspended": "Organization suspended by the operator",
    "organization.unsuspended": "Organization unsuspended by the operator",
    "organization.capability_granted": "Capability granted by the operator",
    "organization.capability_revoked": "Capability revoked by the operator",
    "organization.capability_override_removed": "Capability override removed by the operator",
    "organization.limit_set": "Limit set by the operator",
    "organization.limit_override_removed": "Limit override removed by the operator",
    "organization.backups_pruned": "Older backups removed",
    "impersonation.started": "Operator started impersonating a member",
    "impersonation.stopped": "Operator stopped impersonating",
    "impersonation.expired": "Operator impersonation expired",
    "credentials.rotated": "Stored secrets re-encrypted with a new key",
    "user.created": "Account created",
    "user.address_released": "Retired account's address reused",
    "user.address_reclaimed": "Account retired: its address's owner signed up or accepted an invitation",
    "user.invited": "User invited",
    "user.updated": "User or role changed",
    "user.password_reset": "Password reset by Admin",
    "user.password_changed": "Password changed",
    "user.password_reset_requested": "Password reset link emailed",
    "user.password_reset_completed": "Password reset by email link",
    "user.login": "Sign-in",
    "user.login_failed": "Sign-in failed",
    "user.password_check_failed": "Current password refused",
    "user.organization_switched": "Switched organization",
    "user.removed": "Removed from organization",
    "export": "Exported",
    "import.uploaded": "Import uploaded",
    "import.committed": "Import committed",
    "import.refused": "Import refused",
    "import.staging_updated": "Import choices updated",
    "import.cancelled": "Import canceled",
    "import.purged": "Import file purged",
    "import_source.created": "Import source added",
    "import_source.updated": "Import source renamed",
    "import_source.deleted": "Import source deleted",
    "import_source.mapping_saved": "Column mapping saved",
    "saved_filter.created": "Saved view added",
    "saved_filter.updated": "Saved view changed",
    "saved_filter.deleted": "Saved view deleted",
    "saved_view_subscription.created": "Saved view subscription added",
    "saved_view_subscription.updated": "Saved view subscription changed",
    "saved_view_subscription.deleted": "Saved view subscription removed",
    "saved_view_subscription.tested": "Saved view email test sent",
    "token.created": "API token created",
    "token.revoked": "API token revoked",
    "calendar_feed.created": "Calendar feed created",
    "calendar_feed.rotated": "Calendar feed rotated",
    "calendar_feed.revoked": "Calendar feed revoked",
    "reminder.snoozed": "Reminder snoozed",
    "reminder.acknowledged": "Reminder acknowledged",
    "reminder.reopened": "Reminder reopened",
    "reminder.resolved": "Reminder resolved",
    "email_settings.updated": "Email settings changed",
    "email_settings.deleted": "Email settings removed",
    "email_settings.tested": "Email settings tested",
    "teams_destination.created": "Teams destination added",
    "teams_destination.updated": "Teams destination changed",
    "teams_destination.deleted": "Teams destination deleted",
    "teams_destination.tested": "Teams destination tested",
    "webhook_destination.created": "Webhook destination added",
    "webhook_destination.updated": "Webhook destination changed",
    "webhook_destination.deleted": "Webhook destination deleted",
    "webhook_destination.tested": "Webhook destination tested",
    "webhook_destination.secret_rotated": "Webhook signing secret rotated",
    "webhook_destination.disabled": "Webhook turned off after failures",
    "notification.resent": "Notification resent",
    "notification_rule.created": "Notification rule added",
    "notification_rule.updated": "Notification rule changed",
    "notification_rule.deleted": "Notification rule deleted",
    "notification_rule.tested": "Notification rule tested",
    "user.identity_linked": "Single sign-on linked",
    "user.identity_unlinked": "Single sign-on unlinked",
    # Access and provisioning (catalog/invitations.py, core/oidc.request_access)
    "invitation.revoked": "Invitation revoked",
    "invitation.resent": "Invitation link renewed",
    "invitation.accepted": "Invitation accepted",
    "pending_access.created": "Access requested through single sign-on",
    "pending_access.updated": "Access request's email changed",
    "pending_access.approved": "Access request approved",
    "pending_access.declined": "Access request declined",
    # SCIM provisioning (the optional ee package): written with no actor and `via: scim` plus the token's name
    "scim_token.created": "SCIM token created",
    "scim_token.revoked": "SCIM token revoked",
    "scim.user_created": "User provisioned (SCIM)",
    "scim.user_updated": "Provisioned user changed (SCIM)",
    "scim.user_deleted": "Provisioned user deleted (SCIM)",
    "scim.user_linked": "Provisioned user linked to an account",
    "scim.group_created": "Group provisioned (SCIM)",
    "scim.group_updated": "Provisioned group changed (SCIM)",
    "scim.group_deleted": "Provisioned group deleted (SCIM)",
    "scim.role_kept": "Role kept despite SCIM group mapping",
    "scim.refused": "SCIM change refused",
    # Backups (core/backup.py): an Admin confirmed the generated backup key is copied off the host
    "backup.key_acknowledged": "Backup key copy acknowledged",
    # Single sign-on settings (core/sso.py)
    "sso_settings.updated": "Sign-in settings draft changed",
    "sso_settings.tested": "Sign-in settings tested",
    "sso_settings.activated": "Sign-in settings activated",
    "sso_settings.disabled": "Single sign-on turned off",
    "sso_settings.legacy_restored": "Single sign-on handed back to the installation's settings",
    "connector.created": "Connector added",
    "connector.updated": "Connector changed",
    "connector.deleted": "Connector deleted",
    "connector.secret_set": "Connector secret replaced",  # older events; secrets are on connections now
    "connector.run_requested": "Connector run requested",
    "connector.access_checked": "Connector access checked",
    "microsoft_connection.created": "Microsoft connection added",
    "microsoft_connection.updated": "Microsoft connection changed",
    "microsoft_connection.deleted": "Microsoft connection deleted",
    "microsoft_connection.tested": "Microsoft connection tested",
    "halo_connection.created": "Halo connection added",
    "halo_connection.updated": "Halo connection changed",
    "halo_connection.deleted": "Halo connection deleted",
    "halo_connection.tested": "Halo connection tested",
    # Ticket links (tickets/services.py)
    "ticket_link.created": "Ticket linked",
    "ticket_link.updated": "Ticket link changed",
    "ticket_link.deleted": "Ticket link removed",
    "ticket_link.status_changed": "Ticket status changed",
    "ticket_link.ticket_created": "Ticket created and linked",
    "ticket_link.create_failed": "Ticket creation failed",
    "ticket_connection.created": "Ticket connection added",
    "ticket_connection.updated": "Ticket connection changed",
    "ticket_connection.deleted": "Ticket connection deleted",
    "ticket_connection.tested": "Ticket connection tested",
    "okta_connection.created": "Okta connection added",
    "okta_connection.updated": "Okta connection changed",
    "okta_connection.deleted": "Okta connection deleted",
    "okta_connection.tested": "Okta connection tested",
    "google_workspace_connection.created": "Google Workspace connection added",
    "google_workspace_connection.updated": "Google Workspace connection changed",
    "google_workspace_connection.deleted": "Google Workspace connection deleted",
    "google_workspace_connection.tested": "Google Workspace connection tested",
    "observation.linked": "Discovery candidate linked",
    "observation.created": "Record created from discovery",
    "observation.dismissed": "Discovery candidate dismissed",
    "observation.reopened": "Discovery decision undone",
    "observation.drift_ignored": "Source difference ignored",
    "observation.missing_resolved": "No longer seen in source: resolved",
    # Assistant (discovery/assistant.py)
    "assistant_settings.updated": "Assistant settings changed",
    "assistant_settings.key_refused": "Assistant API key refused; assistant turned off",
    "assistant_batch.requested": "Assistant suggestions requested",
    "assistant_batch.finished": "Assistant batch finished",
    "assistant_suggestion.recorded": "Decision recorded on an assistant suggestion",
    "assistant_suggestion.rejected": "Assistant suggestion rejected",
    "ai_provider.created": "AI provider added",
    "ai_provider.updated": "AI provider changed",
    "ai_provider.deleted": "AI provider deleted",
    "ai_model.created": "AI model added",
    "ai_model.updated": "AI model changed",
    "ai_model.deleted": "AI model deleted",
    "custom_field.created": "Custom field added",
    "custom_field.updated": "Custom field changed",
    "custom_field.deleted": "Custom field deleted",
    "relationship_type.created": "Relationship type added",
    "relationship_type.updated": "Relationship type changed",
    "relationship_type.deleted": "Relationship type deleted",
    "attachment.created": "Attachment added",
    "attachment.updated": "Attachment changed",
    "attachment.deleted": "Attachment removed",
    "quality_exception.created": "Check marked not applicable",
    "quality_exception.updated": "Not-applicable check changed",
    "quality_exception.deleted": "Not-applicable check removed",
    # Vendor risk file (catalog/vendor_risk.py)
    "vendor_risk.created": "Vendor risk file started",
    "vendor_risk.updated": "Vendor risk file changed",
    "vendor_risk.deleted": "Vendor risk file removed",
    "vendor_risk_document.created": "Risk document added",
    "vendor_risk_document.updated": "Risk document changed",
    "vendor_risk_document.deleted": "Risk document removed",
    # Contracts and licence pools
    "contract.created": "Contract added",
    "contract.updated": "Contract changed",
    "contract.archived": "Contract archived",
    "contract.restored": "Contract restored",
    "contract.deleted": "Contract deleted",
    "contract_item.created": "Contract line added",
    "contract_item.updated": "Contract line changed",
    "contract_item.deleted": "Contract line removed",
    "license_pool.created": "License pool added",
    "license_pool.updated": "License pool changed",
    "license_pool.deleted": "License pool deleted",
    # Renewal and licences
    "contract.decided": "Renewal decided",
    "license_pool.synced": "Seat counts synced",
    "license_pool.seat_count_dismissed": "Proposed seat count dismissed",
}
FIELD_LABELS = dict(
    CONFIRMED_FIELDS,
    name="Name",
    description="Description",
    it_steward="IT steward",
    purpose="Purpose",
    vendor="Vendor",
    built_by="Built by",
    license_model="License model",
    documentation_url="Documentation URL",
    support_url="Support URL",
    component_type="Component type",
    host_runtime="Host / runtime",
    repository_url="Repository URL",
    environment="Environment",
    url="URL",
    status="Status",
    data_purpose="Data purpose",
    protocol="Protocol",
    schedule="Schedule",
    source="Source",
    target="Target",
    source_instance="Source instance",
    target_instance="Target instance",
    type="Type",
    inverse_name="Inverse name",
    is_dependency="Counts as a dependency",
    role="Role",
    active="Active",
    title="Title",
    owner="Owner",
    target_date="Target date",
    ticket_url="Ticket URL",
    findings="Findings",
    change_type="Type",
    responsible="Responsible",
    followup="IT follow-up",
    auto_renew="Auto-renewal",
    renewal_date="Renewal date",
    notice="Notice period",
    explicit_deadline="Entered deadline",
    term="Term",
    applications="Covered applications",
    decision="Decision",
    tags="Tags",
    department="Department",
    cost_center="Cost center",
    data_classification="Data classification",
    category="Category",
    deployment_model="Deployment model",
    version="Version",
    primary_url="Primary URL",
    admin_url="Admin URL",
    authentication_method="Authentication method",
    sso_enabled="SSO enabled",
    scim_provisioning="SCIM provisioning",
    entra_app_id="Entra app ID",
    service_principal_id="Service principal ID",
    hosting="Hosting",
    runbook_url="Runbook URL",
    schedule_or_trigger="Schedule or trigger",
    azure_resource_id="Azure resource ID",
    pp_environment_id="Power Platform environment ID",
    external_id="External ID",
    is_gxp="GxP",
    validation_status="Validation status",
    validation_doc_url="Validation document URL",
    last_periodic_review="Last periodic review",
    next_periodic_review="Next periodic review",
    risk_tier="Risk tier",
    website="Website",
    support_email="Support email",
    support_phone="Support phone",
    portal_url="Portal URL",
    account_manager="Account manager",
    notes="Notes",
    members="Members",
    color="Color",
    compliance_profile_enabled="Compliance profile",
    # AI agents
    platform="Platform",
    models_used="Models",
    channels="Channels",
    knowledge_sources="Knowledge sources",
    tools_connectors="Tools and connectors",
    auth_mode="Authentication",
    run_as_identity="Runs as",
    data_access_scope="Data access scope",
    human_in_loop="Human in the loop",
    actions_can_write="Actions can write",
    risk_classification="Risk classification",
    repo_url="Repository URL",
    provider="Provider",
    modality="Modality",
    # Integration fields
    direction="Direction",
    auth_method="Integration authentication",  # authentication_method (people signing in) is "Authentication method"
    monitoring_url="Monitoring URL",
    data_categories="Data categories",  # data-flow map
    hosting_region="Hosting region",
    # Custom fields (a definition's own fields; values are labelled by their field, see Names.label)
    label="Label",
    choices="Choices",
    required="Required",
    default="Default",
    applies_to="Applies to",
    weight="Order",
    is_reminder_source="Reminder source",
    # Attachments (an attachment's own fields; organization limits)
    filename="File name",
    content_type="File type",
    size="Size",
    sha256="SHA-256",
    uploaded_by="Uploaded by",
    attachment_max_size_mb="Largest attachment (MB)",
    attachment_extensions="Attachment file types",
    attachment_quota_mb="Attachment quota (MB)",
    # Data quality (an exception's own fields; the organization's turned-off rules)
    rule="Check",
    accountable="Accountable",
    created_by="Added by",
    reason="Reason",
    expires_on="Until",
    data_quality_disabled_rules="Data-quality checks turned off",
    inactive_seat_days="Days without a sign-in before a license is inactive",
    # Contracts and licence pools
    contract_number="Contract number",
    po_number="PO number",
    amount="Amount",
    currency="Currency",
    billing_frequency="Billing frequency",
    annualized_cost="Annualized cost",
    start_date="Start date",
    end_date="End date",
    renewal_owner="Renewal owner",
    renewal_obligation="Renewal",
    predecessor="Previous term",
    contract="Contract",
    record="Record",
    allocation_percent="Allocation (%)",
    sku_name="SKU name",
    sku_id="SKU ID",
    seats_purchased="Seats purchased",
    seats_assigned="Seats assigned",
    seat_sync="Sync seats from Entra",
    last_synced="Last synced",
)
FIELD_LABELS["edition"] = "Edition"
# Choice fields whose values read badly as words (m365_copilot), summarised by their labels.
CHOICE_LABELS: dict[str, dict[str, str]] = {
    "channels": dict(AgentChannel.choices),
    "platform": dict(AgentPlatform.choices),
    "auth_mode": dict(AgentAuthMode.choices),
    "risk_classification": dict(AgentRisk.choices),
    "direction": dict(IntegrationDirection.choices),
    "auth_method": dict(IntegrationAuthMethod.choices),
    "data_categories": dict(DataCategory.choices),
}  # organization.edition_changed
TEXT_FIELDS = (
    "description",
    "purpose",
    "findings",
    "notes",
    "knowledge_sources",
    "tools_connectors",
    "data_access_scope",
)  # long text: the summary says it changed, not the text
PERSON_FIELDS = {
    "business_owner",
    "technical_owner",
    "confirmed_by",
    "responsible",
    "account_manager",
    "members",
    "accountable",  # a data-quality exception's owner (catalog/quality.py)
    "renewal_owner",  # a contract's
    "reassigned_to",  # who took over a leaving person's roles (offboarding/reassign.py)
}


LONG_VALUE = 100  # characters: a deleted custom field's text longer than this is "changed", not quoted


def is_custom(field) -> bool:
    """A custom field value's key in snapshots and payloads: custom_field_data.<name> (catalog/customfields.py)."""
    return str(field).startswith(customfields.PREFIX)


def custom_text(value) -> str:
    """A custom field value in a summary, exactly as entered (choices keep their underscores)."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, float):
        return customfields.decimal_text(value)
    return str(value)


class Names:
    """Current names for every ID a page of events mentions, loaded in a few queries."""

    def __init__(self, org, events):
        self.org = org
        self._custom: dict[str, Any] | None = None
        self._link_types: dict[str, tuple[str, str]] | None = None
        self._type_slugs: dict[str, str] = {}
        ids: dict[str, set] = {
            "person": set(),
            "team": set(),
            "vendor": set(),
            "user": set(),
            "resource": set(),
            "instance": set(),
            "relationship": set(),
            "assessment": set(),
            "renewal": set(),
            "department": set(),
            "category": set(),
            "ai_model": set(),
            "ai_provider": set(),
            "contract": set(),
            "license_pool": set(),
        }
        buckets = {
            "Person": "person",
            "Resource": "resource",
            "Relationship": "relationship",
            "ChangeAssessment": "assessment",
            "RenewalObligation": "renewal",
            "Vendor": "vendor",
            "ApplicationInstance": "instance",
            "Contract": "contract",
            "LicensePool": "license_pool",
        }
        for e in events:
            for kind, ref in ((e.target_type, e.target_id), (e.related_target_type, e.related_target_id)):
                if kind in buckets and ref:
                    ids[buckets[kind]].add(ref)
            if e.target_type in ("Resource", "Relationship"):  # ends as the snapshots saved them (integrations, links)
                for snap in (e.prechange, e.postchange):
                    for end in ("source", "target"):
                        end_ref = snap.get(end) if isinstance(snap, dict) else None
                        if isinstance(end_ref, dict) and is_identity(end_ref):
                            ids["resource"].add(str(end_ref["id"]))
            ack = e.changes.get("retirement_ack")
            if isinstance(ack, dict):
                ids["resource"] |= {str(i) for i in ack.get("dependents", [])}
            for key, value in e.changes.items():
                values = value if isinstance(value, list) else [value]
                bucket = (
                    "person"
                    if key in PERSON_FIELDS
                    else {
                        "support_team": "team",
                        "vendor": "vendor",
                        "department": "department",
                        "category": "category",
                        "provider": "ai_provider",
                        "it_steward": "user",
                        "user": "user",
                        "owner": "user",
                        "followup": "user",
                        "renewal_obligation": "renewal",
                        "contract": "contract",
                    }.get(key)
                )
                if key == "members":  # [before IDs, after IDs]
                    values = [x for v in values for x in (v if isinstance(v, list) else [v])]
                if key in ("application", "source", "target", "applications", "record"):
                    values = [x for v in values for x in (v if isinstance(v, list) else [v])]
                    bucket = "resource"
                if key in ("source_instance", "target_instance"):
                    bucket = "instance"
                if key == "models_used":  # [before IDs, after IDs]
                    values = [x for v in values for x in (v if isinstance(v, list) else [v])]
                    bucket = "ai_model"
                if bucket:
                    ids[bucket] |= {str(v) for v in values if v not in (None, "")}

        def valid(values, numeric=False):
            return [v for v in values if (str(v).isdigit() if numeric else len(str(v)) == 36)]

        self.person = {str(p.id): p.name for p in Person.objects.filter(organization=org, id__in=valid(ids["person"]))}
        self.team = {str(t.id): t.name for t in Team.objects.filter(organization=org, id__in=valid(ids["team"]))}
        self.vendor = {str(v.id): v.name for v in Vendor.objects.filter(organization=org, id__in=valid(ids["vendor"]))}
        self.department = {
            str(d.id): d.name for d in Department.objects.filter(organization=org, id__in=valid(ids["department"]))
        }
        self.category = {
            str(c.id): c.name for c in SoftwareCategory.objects.filter(organization=org, id__in=valid(ids["category"]))
        }
        self.ai_model = {
            str(m.id): f"{m.name} {m.version}".strip()
            for m in AIModel.objects.filter(organization=org, id__in=valid(ids["ai_model"]))
        }
        self.ai_provider = {
            str(p.id): p.name for p in AIProvider.objects.filter(organization=org, id__in=valid(ids["ai_provider"]))
        }
        # User IDs are read only from this organization's own events, so they are named even after the person was
        # removed from the organization (no membership filter); another organization's users never reach here.
        self.user = {
            str(u.id): u.get_full_name() or u.email
            for u in get_user_model().objects.filter(id__in=valid(ids["user"], numeric=True))
        }
        # A link or an assessment is named by the records it joins or targets, so they are read before the records.
        self.relationship = {
            str(r.id): r
            for r in Relationship.objects.filter(organization=org, id__in=valid(ids["relationship"]))
            .select_related("type")
            .only("type__slug", "source_id", "target_id")
        }
        self.assessment = {
            str(a.id): a
            for a in ChangeAssessment.objects.filter(organization=org, id__in=valid(ids["assessment"])).only(
                "title", "target_id", "change_type"
            )
        }
        self.renewal = {
            str(r.id): r.name
            for r in RenewalObligation.objects.filter(organization=org, id__in=valid(ids["renewal"])).only("name")
        }
        self.contract = {
            str(c.id): c.name
            for c in Contract.objects.filter(organization=org, id__in=valid(ids["contract"])).only("name")
        }
        self.license_pool = {
            str(p.id): p.sku_name
            for p in LicensePool.objects.filter(organization=org, id__in=valid(ids["license_pool"])).only("sku_name")
        }
        ids["resource"] |= {str(r.source_id) for r in self.relationship.values()}
        ids["resource"] |= {str(r.target_id) for r in self.relationship.values()}
        ids["resource"] |= {str(a.target_id) for a in self.assessment.values()}
        self.resource = {str(r.id): r for r in Resource.objects.filter(organization=org, id__in=valid(ids["resource"]))}
        self.instance = {
            str(i.id): i.name
            for i in ApplicationInstance.objects.filter(organization=org, id__in=valid(ids["instance"]))
        }

    @property
    def custom(self) -> dict[str, Any]:
        """{name: CustomField} of the organization, read the first time a page's event names a custom field."""
        if self._custom is None:
            self._custom = {cf.name: cf for cf in customfields.definitions(self.org)}
        return self._custom

    def label(self, field) -> str:
        """A field's label; a custom field's is its current label, or its name in words once it is deleted."""
        if is_custom(field):
            name = str(field)[len(customfields.PREFIX) :]
            found = self.custom.get(name)
            return found.label if found is not None else name.replace("_", " ").capitalize()
        return FIELD_LABELS.get(field, field)

    def long(self, field, pair=None) -> bool:
        """Text the summary names as changed rather than quoting: long text and the custom long text and objects. A
        deleted custom field goes by its values' shape (`pair`: [before, after]): an object, or text over 100
        characters."""
        if is_custom(field):
            found = self.custom.get(str(field)[len(customfields.PREFIX) :])
            if found is not None:
                return found.type in (CustomFieldType.LONGTEXT, CustomFieldType.OBJECT)
            values = pair if isinstance(pair, list) else [pair]
            return any(isinstance(v, dict) or (isinstance(v, str) and len(v) > LONG_VALUE) for v in values)
        return field in TEXT_FIELDS

    def value(self, field, value):
        if value in (None, "", []):
            return "none" if field == "applications" else "not set"
        if is_custom(field):
            return custom_text(value)
        if field in ("notice", "term") and isinstance(value, list) and len(value) == 2:
            return describe(*value)
        if field == "applications" and isinstance(value, list):
            return ", ".join(self.value("source", v) for v in value)
        if isinstance(value, list):  # a set such as tags: its entries in words, not a Python list
            if field == "tags":  # tag names are user text; show them exactly as written
                return ", ".join(str(v) for v in value)
            return ", ".join(self.value(field, v) for v in value)
        lookup = self.lookup(field)
        if lookup is not None:
            return lookup.get(str(value), "a deleted record")
        if field in CHOICE_LABELS:  # an AI agent's choices, by their labels ("Microsoft 365 Copilot")
            return CHOICE_LABELS[field].get(str(value), str(value))
        return str(value).replace("_", " ")

    def lookup(self, field):
        """{id: current name} for a field that holds a reference in `changes`, else None."""
        return {
            **dict.fromkeys(PERSON_FIELDS, self.person),
            "support_team": self.team,
            "vendor": self.vendor,
            "department": self.department,
            "category": self.category,
            "models_used": self.ai_model,
            "provider": self.ai_provider,
            "it_steward": self.user,
            "user": self.user,
            "source": {k: r.name for k, r in self.resource.items()},
            "target": {k: r.name for k, r in self.resource.items()},
            "owner": self.user,
            "followup": self.user,
            "source_instance": self.instance,
            "target_instance": self.instance,
            "renewal_obligation": self.renewal,
            "contract": self.contract,
            "application": {k: r.name for k, r in self.resource.items()},
            "record": {k: r.name for k, r in self.resource.items()},
        }.get(field)

    def link(self, e):
        """(type, source id, target id) of the link a relationship event is about: the row if it still exists, else
        what the event saved when the link was removed (or confirmed, for older events without it)."""
        c = e.changes

        def first(v):
            return v[0] if isinstance(v, list) and v else v

        saved = (first(c.get("type")), first(c.get("source")), first(c.get("target")))
        if e.action in ("relationship.created", "relationship.deleted") and saved[1] and saved[2]:
            return saved  # these two saved the whole link; the row may have been changed since
        row = self.relationship.get(str(link_id(e)))
        if row is not None:
            return row.type.slug, str(row.source_id), str(row.target_id)
        if not (saved[1] and saved[2]):  # a change to a link removed since: its snapshot names the ends and type
            ends = [_snap(e, k) for k in ("type", "source", "target")]
            if all(is_identity(v) for v in ends):
                return self.type_slug(ends[0]["id"]), str(ends[1]["id"]), str(ends[2]["id"])
        return saved

    def type_slug(self, type_id) -> str:
        """The slug of a relationship type by its ID (snapshots store the type as {id, display})."""
        self._load_types()
        return self._type_slugs.get(str(type_id), "")

    def link_phrase(self, slug, inverse=False) -> str:
        """How a link of this type reads ("depends on"): relationship_types.phrase of the type's current name, as the
        digest and staleness reasons read it (a renamed core type reads by its new name), or the slug in words once
        an added type is deleted. `inverse`: read from the target ("is needed by"), "" when the type has no inverse
        name. One query, the first time a page needs it."""
        found = self._load_types().get(str(slug))
        if found is None:
            return "" if inverse else str(slug or "").replace("_", " ")
        return found[1] if inverse else found[0]

    def _load_types(self) -> dict[str, tuple[str, str]]:
        if self._link_types is None:
            from .relationship_types import phrase, phrase_of

            types = list(RelationshipType.objects.filter(organization=self.org))
            self._link_types = {t.slug: (phrase(t), phrase_of(t.inverse_name) if t.inverse_name else "") for t in types}
            self._type_slugs = {str(t.id): t.slug for t in types}
        return self._link_types

    def link_text(self, e):
        """The link in words, such as "Portal depends on Sync", from its ends as they are now."""
        link_type, source, target = self.link(e)
        if not (source and target):
            return "a removed link"
        return f"{self.value('source', source)} {self.link_phrase(link_type)} {self.value('target', target)}"


def ack_text(c, names):
    """The retirement acknowledgment on a resource or assessment event: who it covered and the reason."""
    ack = c.get("retirement_ack")
    if not isinstance(ack, dict):
        return ""
    dependents = [str(i) for i in ack.get("dependents", [])]
    shown = ", ".join(names.value("source", i) for i in dependents[:5]) + (
        f" and {len(dependents) - 5} more" if len(dependents) > 5 else ""
    )
    count = f"{len(dependents)} active {'dependent' if len(dependents) == 1 else 'dependents'}"
    return f"Retirement acknowledged for {count}{f' ({shown})' if shown else ''}. Reason: {ack.get('reason', '')}"


def oidc_label(issuer: str) -> str:
    from core.oidc_discovery import provider_label

    return provider_label(issuer)


def summary(e, names):
    """One line for the Details column. An event a SCIM token wrote (no actor; ee/scim) ends naming it."""
    text = _summary(e, names)
    c = e.changes if isinstance(e.changes, dict) else {}
    if c.get("via") == "scim":
        by = f"by SCIM ({c['scim_client']})" if isinstance(c.get("scim_client"), str) else "by SCIM"
        return f"{text} ({by})" if text else by[0].upper() + by[1:]
    if c.get("via") == "offboarding" and e.action != "person.reassigned":  # one row of a reassignment
        return f"{text} (offboarding)" if text else "Offboarding"
    if isinstance(c.get("api_client"), str):  # written by a request an API token authenticated (api/tokens.py)
        by = f"with API token {c['api_client']}"
        return f"{text} ({by})" if text else by[0].upper() + by[1:]
    return text


def _scim_summary(action: str, c: dict, names) -> str:
    """ee/scim events: who (the account or the masked userName), then what changed."""
    who = names.value("user", c["user"]) if c.get("user") else str(c.get("user_name") or c.get("name") or "")
    if action.startswith("scim_token."):
        return f"{c.get('name', '')} ({c.get('prefix', '')}…), expires {str(c.get('expires_at', ''))[:10]}"
    if action == "scim.user_updated":
        fields = ", ".join(str(f).replace("_", " ") for f in c.get("fields", []))
        active = c.get("active")
        state = f"; active {active[0]} → {active[1]}" if isinstance(active, list) and len(active) == 2 else ""
        return f"Changed {fields}{state}"
    if action == "scim.user_linked":
        fresh = ", new account" if c.get("new_account") else ""
        return f"{who} as {c.get('role')}{fresh}"
    if action == "scim.role_kept":
        reason = "the last active Admin" if c.get("reason") == "last_admin" else "the IT coordinator"
        return f"{who} stays {c.get('role')} (wanted {c.get('wanted')}): {reason}"
    if action == "scim.refused":
        return f"{who}: {c.get('operation')} refused. {c.get('detail', '')}".strip()
    if action.startswith("scim.group_"):
        name = c.get("display_name")
        parts = [f"{name[0]} → {name[1]}" if isinstance(name, list) else str(name or c.get("name") or "")]
        if c.get("members_added"):
            parts.append(f"{c['members_added']} added")
        if c.get("members_removed"):
            parts.append(f"{c['members_removed']} removed")
        if c.get("unmapped"):
            parts.append("no longer mapped to " + ", ".join(c["unmapped"]))
        return "; ".join(filter(None, parts))
    return who


def _credentials_rotated_summary(c: dict) -> str:
    """core/credentials.rotate_all: how many moved to the new key and, when some couldn't be read, that they must
    be entered again and where. Counts and page names only."""
    count = c.get("count", 0)
    text = f"{count} {'secret' if count == 1 else 'secrets'} re-encrypted"
    unreadable = c.get("unreadable")
    if unreadable:
        pages = c.get("reenter_at") or []
        where = "; ".join(str(p) for p in pages) if pages else "where it was set"
        text += (
            f"; {unreadable} can't be read with any configured key: enter {'it' if unreadable == 1 else 'them'} again"
        )
        text += f" at {where}" if pages else f" {where}"
    return text


def _summary(e, names):
    c = e.changes
    if e.action == "credentials.rotated":
        return _credentials_rotated_summary(c)
    if e.action.startswith(("scim.", "scim_token.")):
        return _scim_summary(e.action, c, names)
    if e.action in (
        "resource.updated",
        "assessment.updated",
        "assessment.status_changed",
        "contract.updated",
        "license_pool.updated",
    ):
        parts = [
            f"{names.label(k)}: {names.value(k, v[0])} → {names.value(k, v[1])}"
            for k, v in c.items()
            if isinstance(v, list) and len(v) == 2 and not names.long(k, v)
        ]
        parts += [f"{FIELD_LABELS[k]} changed" for k in TEXT_FIELDS if k in c]
        parts += [f"{names.label(k)} changed" for k, v in c.items() if is_custom(k) and names.long(k, v)]
        parts += [ack_text(c, names)] if "retirement_ack" in c else []
        text = ("Undo. " if c.get("undo") else "") + ("; ".join(filter(None, parts)) or "No field changed")
        if isinstance(c.get("discovery"), dict):  # a source difference applied (discovery/drift.py)
            text += f" (from {c['discovery'].get('connector', 'discovery')})"
        return f"{text} (from the term dates)" if c.get("by") else text
    if e.action == "assessment.created":
        what = "Retirement" if c.get("change_type") == "retirement" else "Change"
        parts = [f"{what} of {names.value('target', c.get('target'))}, {names.value('status', c.get('status'))}"]
        parts += [ack_text(c, names)] if "retirement_ack" in c else []
        return ". ".join(parts)
    if e.action in ("renewal.updated", "renewal_cycle.updated"):
        changed = [
            f"{FIELD_LABELS.get(k, k)}: {names.value(k, v[0])} → {names.value(k, v[1])}"
            for k, v in c.items()
            if isinstance(v, list) and len(v) == 2 and k != "cycle"
        ]
        return "; ".join(changed) or "No field changed"
    if e.action in ("renewal.created", "renewal_cycle.established"):
        parts = [f"Cycle {c.get('sequence')}"] if e.action == "renewal_cycle.established" else []
        parts += [f"{FIELD_LABELS[k]} {names.value(k, c.get(k))}" for k in ("renewal_date", "notice") if c.get(k)]
        if c.get("explicit_deadline"):
            parts.append(f"Entered deadline {c['explicit_deadline']}")
        parts.append(f"Covers {names.value('applications', c.get('applications'))}")
        return "; ".join(parts)
    if e.action == "renewal_cycle.decided":
        before, after = (c.get("decision") or [None, None])[:2]
        text = f"Decision: {names.value('decision', before or 'none')} → {names.value('decision', after or 'none')}"
        return text + terms_text(c.get("terms_change"))
    if e.action == "contract.created" and isinstance(c.get("renewed_from"), str):
        return f"{c.get('name', '')}, renewed from {c['renewed_from']}"
    if e.action == "contract.decided":
        decision = str(names.value("decision", c.get("decision"))).capitalize()
        nxt = f": next term {c['successor_name']}" if isinstance(c.get("successor_name"), str) else ""
        return f"{decision}{nxt}"
    if e.action == "license_pool.synced":
        parts = [
            f"{FIELD_LABELS.get(k, k)}: {v[0]} → {v[1]}" for k, v in c.items() if isinstance(v, list) and len(v) == 2
        ]
        return f"From {c.get('source', 'discovery')}: " + ("; ".join(parts) or "no change")
    if e.action == "license_pool.seat_count_dismissed":
        proposed = c.get("proposed") if isinstance(c.get("proposed"), dict) else {}
        said = ", ".join(f"{FIELD_LABELS.get(k, k)} {v}" for k, v in proposed.items())
        return f"{c.get('source', 'A source')} proposed {said}. Reason: {c.get('reason', '')}"
    if e.action == "renewal_cycle.completed":
        return f"{names.value('decision', c.get('decision'))}, completed {c.get('completed_on', '')}".capitalize()
    if e.action == "digest.retried":
        outcome = "sent" if c.get("sent") else "failed again"
        return f"Digest of {c.get('for_date', '')} for {names.value('user', c.get('user'))}: {outcome}"
    if e.action.startswith("notification."):
        until = f" until {c['until']}" if c.get("until") else ""
        return f"{str(c.get('subject_type', '')).replace('_', ' ').capitalize()}{until}"
    if e.action.startswith("assistant_"):  # discovery/assistant.py: never a key, a prompt or a token count
        return _assistant_summary(e.action, c)
    if e.action == "observation.drift_ignored":
        fields = c.get("fields") if isinstance(c.get("fields"), dict) else {}
        kept = ", ".join(FIELD_LABELS.get(k, k) for k in fields)
        return f"{c.get('source_name', '')}: kept the record's {kept or 'value'}"
    if e.action == "observation.missing_resolved":
        what = "archived the record" if c.get("resolution") == "archived" else "kept the record"
        reason = f": {c['reason']}" if c.get("reason") else ""
        return f"{c.get('source_name', '')} no longer seen in {c.get('connector', 'its source')}; {what}{reason}"
    if e.action.startswith("observation."):
        reason = f": {c['reason']}" if c.get("reason") else ""
        archived = " (record archived)" if c.get("archived") else ""
        return f"{c.get('source_name', '')}{reason}{archived}"
    if e.action == "preference.updated":
        return "Daily digest " + ("on" if c.get("digest_enabled") else "off")
    if e.action == "assessment.snapshot_refreshed":
        return f"New snapshot, {c.get('nodes', 0)} affected" + (". It was flagged stale" if c.get("was_stale") else "")
    if e.action == "assessment.stale":
        return "; ".join(str(r) for r in c.get("reasons", []))
    if e.action == "link.confirmed":
        confirmed = f"Confirmed by {names.value('confirmed_by', c.get('confirmed_by'))} on {c.get('confirmed_on', '')}"
        # On a relationship the sentence also says which link (the event sits on the link, not on a record).
        return f"{confirmed}: {names.link_text(e)}" if link_id(e) else confirmed
    if e.action in ("relationship.created", "relationship.deleted"):
        link_type, source, target = names.link(e)
        ends = f"{names.value('source', source)} → {names.value('target', target)}"
        return f"{names.link_phrase(link_type)}: {ends}"
    if e.action == "relationship.updated":
        parts = [
            f"{FIELD_LABELS.get(k, k)}: {names.value(k, v[0])} → {names.value(k, v[1])}"
            for k, v in c.items()
            if isinstance(v, list) and len(v) == 2 and k not in TEXT_FIELDS
        ]
        parts += [f"{FIELD_LABELS[k]} changed" for k in TEXT_FIELDS if k in c]
        return f"{names.link_text(e)}. " + ("; ".join(parts) or "No field changed")
    if e.action == "review.recorded":
        return f"Confirmed by {names.value('confirmed_by', c.get('confirmed_by'))}"
    if e.action == "resource.undo_refused":
        return "Not undone: " + ", ".join(FIELD_LABELS.get(f, f) for f in c.get("fields", [])) + " had changed"
    if e.action.startswith("instance."):
        changed = [k for k in c if k not in ("application", "name")]
        return " · ".join(
            filter(
                None,
                [
                    c.get("name") if isinstance(c.get("name"), str) else None,
                    ", ".join(FIELD_LABELS.get(k, k) for k in changed),
                ],
            )
        )
    if e.action in ("invitation.accepted", "pending_access.approved") and "user" in c:
        role = f" as {c['role']}" if isinstance(c.get("role"), str) else ""
        fresh = ", new account" if c.get("new_account") else ""
        via = f" through {c['provider']}" if isinstance(c.get("provider"), str) else ""
        return f"{names.value('user', c['user'])}{role}{via}{fresh}"
    if e.action.startswith(("invitation.", "pending_access.")) or (e.action == "user.invited" and "user" not in c):
        # the email is stored masked (j***@example.com); the role and the provider as they were
        bits = [str(c["email"])] if isinstance(c.get("email"), str) and c["email"] else []
        if isinstance(c.get("email"), list) and len(c["email"]) == 2:  # pending_access.updated
            bits = [f"email {c['email'][0]} → {c['email'][1]}"]
        if isinstance(c.get("role"), str):
            bits.append(f"as {c['role']}")
        if isinstance(c.get("provider"), str):
            bits.append(f"through {c['provider']}")
        if c.get("replaced"):
            bits.append("replacing an earlier invitation")
        return " ".join(bits)
    if e.action.startswith("user.") and "user" in c:
        extra = [
            f"{FIELD_LABELS.get(k, k)}: {v[0]} → {v[1]}"
            for k, v in c.items()
            if isinstance(v, list) and len(v) == 2 and k != "issuers"
        ]
        if isinstance(c.get("issuers"), list) and c["issuers"]:  # user.identity_unlinked (core/oidc, invitations)
            extra.append(", ".join(oidc_label(str(i)) for i in c["issuers"]))
        role = f" as {c['role']}" if isinstance(c.get("role"), str) else ""
        return "; ".join([f"{names.value('user', c['user'])}{role}", *extra])
    if e.action == "export":  # counts and filters only, never contents
        fmt = {"xlsx": "Excel", "jsonl": "JSON lines"}.get(str(c.get("format")), "CSV")
        rows = c.get("rows", 0)
        noun = str(c.get("kind") or "record").replace("ai_agent", "AI agent").replace("_", " ")
        return f"{fmt}, {rows} {noun}{'' if rows == 1 else 's'}"
    if e.action.startswith("tracked_version."):
        return tracked_version_text(e.action, c)
    if e.action.startswith("attachment."):
        return attachment_text(c)
    if e.action.startswith(("vendor_risk.", "vendor_risk_document.")):
        return vendor_risk_text(e.action, c)
    if e.action.startswith("contract_item."):
        return item_text(e.action, c)
    if e.action.startswith("quality_exception."):
        return quality_exception_text(e.action, c, names)
    if e.action == "custom_field.deleted":
        cleared = c.get("cleared") or 0
        return (
            f"{c.get('label') or c.get('name', '')}; values removed from {cleared} record{'' if cleared == 1 else 's'}"
        )
    if e.action == "person.updated":
        return "Changed: " + ", ".join(FIELD_LABELS.get(f, f) for f in c.get("fields", []))
    if e.action == "person.reassigned":  # offboarding/reassign.py: how many rows, to whom, which roles
        n = c.get("changed", 0)
        roles = ", ".join(str(r).replace("_", " ") for r in c.get("roles", []))
        return f"{n} row{'' if n == 1 else 's'} to {names.value('reassigned_to', c.get('reassigned_to'))} ({roles})"
    if e.action in (
        "team.updated",
        "vendor.updated",
        "category.updated",
        "tag.updated",
        "ai_provider.updated",
        "ai_model.updated",
        "custom_field.updated",
        "relationship_type.updated",
    ):
        name = c.get("name")
        parts = [f"{name[0]} → {name[1]}" if isinstance(name, list) else str(name or "")]
        parts += [
            f"{FIELD_LABELS.get(k, k)}: {names.value(k, v[0])} → {names.value(k, v[1])}"
            for k, v in c.items()
            if k != "name" and isinstance(v, list) and len(v) == 2 and k not in TEXT_FIELDS
        ]
        parts += [f"{FIELD_LABELS[k]} changed" for k in TEXT_FIELDS if k in c]
        return "; ".join(filter(None, parts))
    if e.action.split(".")[0] in (
        "microsoft_connection",
        "halo_connection",
        "okta_connection",
        "google_workspace_connection",
    ):
        # core/connections.py: the settings changed (values in clear), the secret replaced (never a value), a test
        parts = [str(c["name"])] if isinstance(c.get("name"), str) else []
        changed = [k for k, v in c.items() if isinstance(v, list) and len(v) == 2 and k not in ("revision",)]
        if changed:
            parts.append("changed " + ", ".join(k.replace("_", " ") for k in changed))
        if c.get("replaced"):
            parts.append("replaced the client secret")
        if "ok" in c:
            outcome = "test passed" if c["ok"] else f"test failed{' (' + str(c['code']) + ')' if c.get('code') else ''}"
            parts.append(outcome)
        return "; ".join(parts)
    if e.action.startswith("sso_settings."):  # core/sso.py: never a secret, an email or a provider's text
        parts = []
        changed = [k for k, v in c.items() if isinstance(v, list) and len(v) == 2 and k not in ("revision", "status")]
        if changed:
            parts.append("changed " + ", ".join(k.replace("_", " ") for k in changed))
        if c.get("replaced"):
            parts.append("replaced the client secret")
        if "ok" in c:
            outcome = "test passed" if c["ok"] else f"test failed{' (' + str(c['code']) + ')' if c.get('code') else ''}"
            parts.append(outcome + ("" if c.get("current", True) else " for an older draft"))
        if isinstance(c.get("status"), list) and len(c["status"]) == 2:
            parts.append(f"{c['status'][0]} → {c['status'][1]}")
        if c.get("via") == "cli":
            parts.append("from the host (sso_recovery)")
        return "; ".join(parts)
    if e.action.startswith("notification_rule."):  # notify/rule_services.py
        parts = [str(c.get("name") or "")]
        if c.get("fields"):
            parts.append("changed " + ", ".join(str(f).replace("_", " ") for f in c["fields"]))
        if "ok" in c:
            parts.append(f"test {'sent' if c['ok'] else 'not sent'}")
        return "; ".join(filter(None, parts))
    if e.action.split(".")[0] in ("email_settings", "teams_destination", "webhook_destination"):
        # notify/channel_services.py: which settings changed and which secrets were replaced (never a value)
        parts = [str(c["name"])] if isinstance(c.get("name"), str) else []
        if c.get("fields"):
            parts.append("changed " + ", ".join(str(f).replace("_", " ") for f in c["fields"]))
        if c.get("replaced"):
            parts.append("replaced " + ", ".join(str(f).replace("_", " ") for f in c["replaced"]))
        if "ok" in c:
            parts.append(f"{c.get('test', 'test')} {'succeeded' if c['ok'] else 'failed'}")
        if "grace_period_hours" in c:  # notify/webhooks.rotate_secret
            hours = c["grace_period_hours"]
            parts.append(f"the previous secret signs for {hours} more hours" if hours else "no grace period")
        if isinstance(c.get("reason"), str):  # notify/webhooks.auto_disable
            parts.append(c["reason"])
        return "; ".join(parts)
    if e.action == "notification.resent":
        return f"{str(c.get('channel', '')).capitalize()} delivery resent"
    if e.action.startswith("reminder."):  # notify/services.py: what it is about, then what was done
        parts = [f"{c.get('label')} {c.get('due_date')}: {c.get('title')}"]
        if c.get("until"):
            parts.append(f"until {c['until']}")
        if c.get("resolution_kind"):
            parts.append(str(c["resolution_kind"]).replace("_", " "))
        return ", ".join(parts)
    if c.get("via") == "platform":  # the service operator acted (catalog/platform_admin.py)
        return _platform_summary(e.action, c, names)
    if isinstance(c.get("name"), list):
        return f"{c['name'][0]} → {c['name'][1]}"
    if isinstance(c.get("name"), str):
        return c["name"]
    return ""


IMPERSONATION_ENDS = {
    "stopped": "stopped by the operator",
    "signed_out": "the operator signed out",
    "expired": "time limit reached",
    "staff_unavailable": "the operator's access ended",
    "member_unavailable": "the member is no longer available",
}


def _platform_summary(action: str, c: dict, names) -> str:
    parts = []
    if isinstance(c.get("edition"), list) and len(c["edition"]) == 2:
        parts.append(f"{c['edition'][0]} → {c['edition'][1]}")
    if c.get("capability"):
        parts.append(str(c["capability"]) + (f" until {c['expires_at']}" if c.get("expires_at") else ""))
    if action.startswith("impersonation."):
        parts.append(names.value("user", c.get("user")))
        if action == "impersonation.started" and c.get("expires_at"):
            parts.append(f"until {c['expires_at']}")
        elif c.get("reason") in IMPERSONATION_ENDS:
            return "; ".join([*parts, IMPERSONATION_ENDS[c["reason"]]])
    if isinstance(c.get("reason"), str) and c["reason"]:
        parts.append(f"Reason: {c['reason']}")
    return "; ".join(parts)


def terms_text(terms) -> str:
    """What a contract renewal decision changed (a cycle's terms_change), in words: ". 1200.00 USD a year → 1500.00 USD
    a year; ends 2027-03-31 → 2028-03-31"; empty without terms."""
    if not isinstance(terms, dict):
        return ""

    def cost(side: str) -> str:
        value = terms.get(f"{side}_annualized_cost")
        return "no amount" if value in (None, "") else f"{value} {terms.get(f'{side}_currency') or ''} a year".strip()

    parts = []
    if any(terms.get(f"new_{k}") not in (None, "") for k in ("amount", "end_date", "start_date")):
        parts.append(f"{cost('old')} → {cost('new')}")
        parts.append(f"ends {terms.get('old_end_date') or 'unknown'} → {terms.get('new_end_date') or 'unknown'}")
    else:
        parts.append(f"{cost('old')}, ending {terms.get('old_end_date') or 'unknown'}")
    return ". " + "; ".join(parts)


def percent_text(value) -> str:
    """An allocation in words: "40%", or "shares the rest" for a line without a fixed percentage."""
    if value in (None, ""):
        return "shares the rest"
    text = str(value)
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "%"


def item_text(action, c) -> str:
    """A contract line event in words: the record and its allocation, or how the allocation changed."""
    name = str(c.get("name") or "a record")
    pair = c.get("allocation_percent")
    if action == "contract_item.updated" and isinstance(pair, list) and len(pair) == 2:
        return f"{name}: {percent_text(pair[0])} → {percent_text(pair[1])}"
    return f"{name}, {percent_text(pair)}"


def size_text(size) -> str:
    """A file size in words: bytes, KB or MB (1024-based, one decimal)."""
    if not isinstance(size, int):
        return ""
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


ATTACHMENT_CATEGORIES = dict(AttachmentCategory.choices)


def tracked_version_text(action, c) -> str:
    """End-of-life tracking: 'postgresql, cycle 16 (explicit), version 16.4', or what changed."""

    def now(value):
        return value[1] if isinstance(value, list) and len(value) == 2 else value

    product = str(c.get("product") or "")
    if action == "tracked_version.updated":
        parts = [
            f"{key.replace('_', ' ')}: {c[key][0] or 'none'} → {c[key][1] or 'none'}"
            for key in ("product", "version", "cycle")
            if isinstance(c.get(key), list) and len(c[key]) == 2
        ]
        return "; ".join(parts) or product
    cycle, version = now(c.get("cycle")), now(c.get("version"))
    parts = [product]
    if cycle:
        parts.append(f"cycle {cycle}" + (" (explicit)" if now(c.get("cycle_explicit")) else ""))
    if version:
        parts.append(f"version {version}")
    return ", ".join(p for p in parts if p)


def attachment_text(c) -> str:
    """An attachment event in words: its title, and what it is (a file's name and size, or "link"), or what changed."""
    title = c.get("title")
    if isinstance(title, list):  # updated: the title changed
        parts = [f"{title[0]} → {title[1]}"]
    else:
        parts = [str(title or "")]
    category = c.get("category")
    if isinstance(category, list) and len(category) == 2:
        before, after = (ATTACHMENT_CATEGORIES.get(str(v), str(v)) for v in category)
        parts.append(f"Category: {before} → {after}")
    if isinstance(c.get("url"), list):
        parts.append("Link address changed")
    if c.get("kind") == "file":
        parts.append(", ".join(filter(None, [str(c.get("filename") or ""), size_text(c.get("size"))])))
    elif c.get("kind") == "link":
        parts.append("link")
    return "; ".join(filter(None, parts))


VENDOR_RISK_FIELDS = {
    "risk_tier": "Risk tier",
    "processes_personal_data": "Processes personal data",
    "questionnaire_status": "Questionnaire",
    "questionnaire_requested_on": "Questionnaire requested",
    "questionnaire_reviewed_on": "Questionnaire reviewed",
    "assurance_type": "Assurance report",
    "assurance_issued_on": "Report issued",
    "assurance_validity_months": "Valid for (months)",
    "assurance_expires_on": "Report expires",
    "assurance_attachment": "Report attachment",
    "dpa_status": "DPA",
    "dpa_signed_on": "DPA signed",
    "dpa_attachment": "DPA attachment",
    "subprocessor_url": "Subprocessor list",
    "subprocessor_attachment": "Subprocessor attachment",
    "subprocessors_reviewed_on": "Subprocessors reviewed",
    "kind": "Kind",
    "title": "Title",
    "issued_on": "Issued",
    "validity_months": "Valid for (months)",
    "expires_on": "Expires",
    "attachment": "Attachment",
}
VENDOR_RISK_IDS = {"assurance_attachment", "dpa_attachment", "subprocessor_attachment", "attachment"}


def _risk_value(key, value) -> str:
    if value in (None, ""):
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key in VENDOR_RISK_IDS:
        return "an attachment"
    return str(value).replace("_", " ")


def vendor_risk_text(action, c) -> str:
    """A vendor risk file's event (catalog/vendor_risk.py): what changed, never the notes' text."""
    if action == "vendor_risk.deleted":
        return "Removed with the vendor"
    if action.startswith("vendor_risk_document.") and not action.endswith(".updated"):
        kind = _risk_value("kind", c.get("kind"))
        parts = [kind[:1].upper() + kind[1:]]
        if c.get("title"):
            parts.append(f"'{c['title']}'")
        if c.get("expires_on"):
            parts.append(f"expires {c['expires_on']}")
        return ", ".join(parts)
    parts = [
        f"{label}: {_risk_value(k, c[k][0])} → {_risk_value(k, c[k][1])}"
        for k, label in VENDOR_RISK_FIELDS.items()
        if isinstance(c.get(k), list) and len(c[k]) == 2
    ]
    if action == "vendor_risk.created":
        parts = [f"{label}: {_risk_value(k, c[k])}" for k, label in VENDOR_RISK_FIELDS.items() if k in c]
    if "notes" in c:
        parts.append("Notes changed")
    return "; ".join(parts) or "No field changed"


def quality_exception_text(action, c, names) -> str:
    """A data-quality exception in words: which check, and until when, or what changed."""
    from .quality import BY_ID

    rule = BY_ID.get(str(c.get("rule")))
    label = c.get("rule_label") or (rule.label if rule else str(c.get("rule") or ""))
    if action == "quality_exception.updated":
        parts = [str(label)]
        if isinstance(c.get("expires_on"), list):
            parts.append(f"Until: {c['expires_on'][0]} → {c['expires_on'][1]}")
        if isinstance(c.get("accountable"), list):
            before, after = (names.value("accountable", v) for v in c["accountable"])
            parts.append(f"Accountable: {before} → {after}")
        if "reason" in c:
            parts.append("Reason changed")
        return "; ".join(parts)
    until = f" until {c['expires_on']}" if isinstance(c.get("expires_on"), str) else ""
    return f"{label}{until}"


def subject(e):
    """(type, id) of what the event is listed under: the related target for a child row (an instance, a review, a
    link confirmation, a renewal cycle), else the target itself."""
    if e.related_target_type and e.related_target_id:
        return e.related_target_type, e.related_target_id
    return e.target_type, e.target_id


def link_id(e):
    """The relationship a relationship or link-confirmation event is about, or None."""
    kind, ref = subject(e)
    return ref if kind == "Relationship" else None


def target_of(e, names):
    """What the event is about, named as it is now: a record, person, link, assessment, renewal or vendor; None for
    account and settings events. A child row's event (an instance, a review, a confirmation, a cycle, an attachment)
    names its parent."""
    target = None
    kind, ref = subject(e)
    if kind == "Resource" and str(ref) in names.resource:
        r = names.resource[str(ref)]
        target = {"kind": r.kind, "id": str(r.id), "name": r.name}
    elif kind == "Person" and ref:
        target = {"kind": "person", "id": str(ref), "name": names.person.get(str(ref), "")}
    elif kind == "Relationship" and ref:
        # Named by what it joins; `record` is where its page is (the source record's Relationships tab), if it exists.
        _, source, _ = names.link(e)
        record = names.resource.get(str(source))
        target = {
            "kind": "relationship",
            "id": str(ref),
            "name": names.link_text(e),
            "record": record and {"kind": record.kind, "id": str(record.id)},
        }
    elif kind == "ChangeAssessment" and ref:
        found = names.assessment.get(str(ref))
        target = {
            "kind": "assessment",
            "id": str(ref),
            "name": found.title if found else "a deleted assessment",
        }
    elif kind == "RenewalObligation" and ref:
        target = {
            "kind": "renewal",
            "id": str(ref),
            "name": names.renewal.get(str(ref)) or e.changes.get("name") or "a deleted renewal",
        }
    elif kind == "Vendor" and ref:
        target = {"kind": "vendor", "id": str(ref), "name": names.vendor.get(str(ref), "a deleted vendor")}
    elif kind == "Contract" and ref:
        name = names.contract.get(str(ref)) or (e.changes.get("name") if e.target_type == "Contract" else None)
        target = {"kind": "contract", "id": str(ref), "name": name if isinstance(name, str) else "a deleted contract"}
    elif kind == "LicensePool" and ref:
        name = names.license_pool.get(str(ref)) or e.changes.get("name")
        target = {
            "kind": "license_pool",
            "id": str(ref),
            "name": name if isinstance(name, str) else "a deleted license pool",
        }
    elif e.action.startswith("instance.") and str(e.changes.get("application")) in names.resource:
        r = names.resource[str(e.changes["application"])]
        target = {"kind": r.kind, "id": str(r.id), "name": r.name}
    return target


def link_events(resource):
    """Events about the links this record is an end of: relationship created, changed and removed, and link
    confirmations of a relationship. Found by the link as it is now, and by the ends the event saved (a removed link,
    or the old end of one that was re-pointed, is no longer in the table)."""
    rid = str(resource.id)
    saved = Q()
    for field in ("source", "target"):
        saved |= (
            Q(**{f"changes__{field}": rid}) | Q(**{f"changes__{field}__0": rid}) | Q(**{f"changes__{field}__1": rid})
        )
    now = Relationship.objects.filter(organization_id=resource.organization_id).filter(
        Q(source=resource) | Q(target=resource)
    )
    # The ends each event's snapshots saved: a change to a link removed since (whose payload holds only the changed
    # fields) still shows, and only in the History of a record that was an end when the change was made (a link moved
    # off this record and edited afterwards doesn't).
    ends = Q()
    for field in ("source", "target"):
        ends |= Q(**{f"prechange__{field}__id": rid}) | Q(**{f"postchange__{field}__id": rid})
    # Events written before snapshots have only their payload: for those, the links that once joined this
    # record by a saved end.
    joined = AuditEvent.objects.filter(
        organization_id=resource.organization_id, target_type="Relationship", action__startswith="relationship."
    ).filter(saved)
    return (Q(action__startswith="relationship.") | Q(action="link.confirmed")) & (
        saved
        | ends
        | Q(target_type="Relationship", target_id__in=now.values("id"))
        | Q(related_target_type="Relationship", related_target_id__in=now.values("id"))
        | Q(
            target_type="Relationship",
            target_id__in=joined.values("target_id"),
            prechange__isnull=True,
            postchange__isnull=True,
        )
    )


def integration_end_events(resource) -> Q:
    """The own events (created, changed, archived, restored, deleted) of the integrations this record is an end of:
    found by the integrations as they are now, and by the ends an event saved (a re-pointed end in `changes`, a
    created or deleted integration's ends in its snapshots), so a removed integration still shows."""
    rid = str(resource.id)
    now = Integration.objects.filter(organization_id=resource.organization_id).filter(
        Q(source_resource=resource) | Q(target_resource=resource)
    )
    saved = Q()
    for field in ("source", "target"):
        saved |= Q(**{f"changes__{field}__0": rid}) | Q(**{f"changes__{field}__1": rid})
        saved |= Q(**{f"prechange__{field}__id": rid}) | Q(**{f"postchange__{field}__id": rid})
    return Q(action__startswith="resource.", target_type="Resource") & (
        Q(target_id__in=now.values("resource_id")) | saved
    )


def record_events(resource, contracts: bool = True) -> Q:
    """An application's, component's or AI agent's own events, its child rows' (instances, reviews, attachments,
    data-quality exceptions: related_target), the links it is an end of, the integrations it is an end of and
    the contract lines that put it on a contract (only with `contracts`: the organization's edition has the
    contracts capability). Instance events written before related_target are found by the application they saved."""
    return (
        Q(target_id=resource.id)
        | Q(related_target_type="Resource", related_target_id=resource.id)
        | Q(action__startswith="instance.", related_target_id=None, changes__application=str(resource.id))
        | (Q(action__startswith="contract_item.", changes__record=str(resource.id)) if contracts else Q(pk__in=[]))
        | link_events(resource)
        | integration_end_events(resource)
    )


def contract_events(contract) -> Q:
    """A contract's own events and its children's (its lines and attachments: related_target)."""
    return Q(target_type="Contract", target_id=contract.id) | Q(
        related_target_type="Contract", related_target_id=contract.id
    )


def integration_events(resource) -> Q:
    """An integration's own events (create, edit, archive), its children (confirmations, attachments: related_target)
    and the relationship events that name it, such as a component being recorded as implementing it."""
    return (
        Q(target_id=resource.id)
        | Q(related_target_type="Resource", related_target_id=resource.id)
        | link_events(resource)
    )


def renewal_events(obligation) -> Q:
    """Every event of a renewal and its cycles: the renewal's own, and its cycles' (related_target; cycle events
    written before related_target targeted the renewal)."""
    return Q(target_type="RenewalObligation", target_id=obligation.id) | Q(
        related_target_type="RenewalObligation", related_target_id=obligation.id
    )


# Child events that older code wrote with the parent itself as the target (before related_target): a review
# and an integration's confirmation targeted the record, a cycle's events the renewal.
CHILD_ACTIONS_ON_PARENT = ("review.recorded", "link.confirmed")
CHILD_PREFIXES_ON_PARENT = ("renewal_cycle.",)


def own_events(parent) -> Q:
    """Only the parent's own events: those that target it, less the child events older code targeted it with."""
    q = Q(target_id=parent.id) & ~Q(action__in=CHILD_ACTIONS_ON_PARENT)
    for prefix in CHILD_PREFIXES_ON_PARENT:
        q &= ~Q(action__startswith=prefix)
    return q


# ---- child events in words (a record's History) -----------------------------------------

CHILD_KINDS = {
    "attachment": "Attachment",
    "instance": "Instance",
    "relationship": "Relationship",
    "link_confirmation": "Link confirmation",
    "review": "Review",
    "quality_exception": "Data-quality exception",
    "integration": "Integration",
    "renewal_cycle": "Renewal cycle",
    "contract_item": "Contract line",
    "other": "Related",  # an action with no sentence of its own (a child type added later): its label and summary
}
CHILD_PREFIXES = (
    ("attachment.", "attachment"),
    ("instance.", "instance"),
    ("relationship.", "relationship"),
    ("quality_exception.", "quality_exception"),
    ("renewal_cycle.", "renewal_cycle"),
    ("contract_item.", "contract_item"),
    ("resource.", "integration"),  # another record's event in this History: an integration it is an end of
)


def child_kind(e, parent_id) -> str | None:
    """Which kind of child of `parent_id` the event is about, or None for the parent's own event."""
    action = e.action
    on_parent = str(e.target_id) == str(parent_id)
    if on_parent and action not in CHILD_ACTIONS_ON_PARENT:
        if not any(action.startswith(p) for p in CHILD_PREFIXES_ON_PARENT):
            return None
    if action == "link.confirmed":
        return "link_confirmation"
    if action == "review.recorded":
        return "review"
    return next((kind for prefix, kind in CHILD_PREFIXES if action.startswith(prefix)), "other")


def _snap(e, key):
    """A key of the event's snapshots: after the write if there is one, else before."""
    for snap in (e.postchange, e.prechange):
        if isinstance(snap, dict) and key in snap:
            return snap[key]
    return None


def _now(value):
    """The current half of a [before, after] pair, else the value itself."""
    return value[1] if isinstance(value, list) and len(value) == 2 else value


def _pairs(c, names, skip=()) -> str:
    """A payload's field changes in words: "Status: operational → offline; Purpose changed"."""
    parts, long = [], []
    for k, v in c.items():
        if k in skip or k in NOT_FIELDS or not (isinstance(v, list) and len(v) == 2):
            continue
        if names.long(k, v):
            long.append(f"{names.label(k)} changed")
        else:
            parts.append(f"{names.label(k)}: {names.value(k, v[0])} → {names.value(k, v[1])}")
    return "; ".join(parts + long)


def _other_end(e, names, parent_id):
    """(the other end's name, the link's phrase read from the parent's side) of a relationship or link-confirmation
    event; (None, None) when the parent isn't an end any more (the old end of a re-pointed link), and the phrase is
    None when the parent is the target and the type has no inverse name."""
    link_type, source, target = names.link(e)
    pid = str(parent_id)
    if str(source) == pid:
        return names.value("target", target), names.link_phrase(link_type)
    if str(target) == pid:
        return names.value("source", source), names.link_phrase(link_type, inverse=True) or None
    return None, None


def _relationship_sentence(e, names, parent_id) -> tuple[str, str]:
    other, phrase = _other_end(e, names, parent_id)
    whole = names.link_text(e)
    if e.action == "link.confirmed":
        who = names.value("confirmed_by", e.changes.get("confirmed_by"))
        link = f"Link to {other} ({phrase})" if other and phrase else f"Link '{whole}'"
        return other or whole, f"{link} confirmed by {who} on {e.changes.get('confirmed_on', '')}"
    details = _pairs(e.changes, names) if e.action == "relationship.updated" else ""
    if other is None or phrase is None:  # the whole link in words
        verb = {"relationship.created": "Linked", "relationship.deleted": "Unlinked"}.get(e.action, "Link changed")
        return whole, f"{verb}: {whole}" + (f". {details}" if details else "")
    if e.action == "relationship.created":
        return other, f"Linked to {other}: {phrase}"
    if e.action == "relationship.deleted":
        return other, f"Unlinked from {other}: {phrase}"
    return other, f"Link to {other} ({phrase}) changed" + (f": {details}" if details else "")


def _attachment_sentence(e) -> tuple[str, str]:
    c = e.changes
    title = str(_now(c.get("title")) or _snap(e, "title") or "an attachment")
    if e.action == "attachment.created":
        if c.get("kind") == "file":
            what = ", ".join(filter(None, [str(c.get("filename") or ""), size_text(c.get("size"))]))
        else:
            what = "link"
        return title, f"Added attachment '{title}'" + (f" ({what})" if what else "")
    if e.action == "attachment.deleted":
        return title, f"Removed attachment '{title}'"
    parts = []
    if isinstance(c.get("title"), list) and len(c["title"]) == 2:
        parts.append(f"Title: {c['title'][0]} → {c['title'][1]}")
    category = c.get("category")
    if isinstance(category, list) and len(category) == 2:
        before, after = (ATTACHMENT_CATEGORIES.get(str(v), str(v)) for v in category)
        parts.append(f"Category: {before} → {after}")
    if isinstance(c.get("url"), list):
        parts.append("Link address changed")
    return title, f"Changed attachment '{title}'" + (f": {'; '.join(parts)}" if parts else "")


def _instance_sentence(e, names) -> tuple[str, str]:
    c = e.changes
    name = str(_now(c.get("name")) or _snap(e, "name") or names.instance.get(str(e.target_id)) or "an instance")
    if e.action == "instance.created":
        return name, f"Added instance '{name}'"
    if e.action == "instance.deleted":
        return name, f"Deleted instance '{name}'"
    details = _pairs(c, names, skip=("application",))
    return name, f"Instance '{name}' changed" + (f": {details}" if details else "")


INTEGRATION_VERBS = {
    "resource.created": "Added",
    "resource.archived": "Archived",
    "resource.restored": "Restored",
    "resource.deleted": "Deleted",
}


def _integration_sentence(e, names, parent_id) -> tuple[str, str]:
    c = e.changes
    found = names.resource.get(str(e.target_id))
    name = found.name if found is not None else str(_now(c.get("name")) or _snap(e, "name") or "an integration")

    def end(key):
        ref = _snap(e, key)
        return (str(ref["id"]), ref.get("display")) if is_identity(ref) else (None, None)

    def named(key):
        ref, display = end(key)
        return names.value("source", ref) if ref in names.resource else display or "a removed record"

    direction = ""
    if end("source")[0] == str(parent_id):
        direction = f" to {named('target')}"
    elif end("target")[0] == str(parent_id):
        direction = f" from {named('source')}"
    verb = INTEGRATION_VERBS.get(e.action)
    if verb:
        return name, f"{verb} integration '{name}'{direction}"
    return name, f"Integration '{name}'{direction} changed: {summary(e, names)}"


def _quality_sentence(e, names) -> tuple[str, str]:
    from .quality import BY_ID

    c = e.changes
    rule = BY_ID.get(str(c.get("rule")))
    name = str(c.get("rule_label") or (rule.label if rule else c.get("rule") or "a check"))
    if e.action == "quality_exception.created":
        until = f" until {c['expires_on']}" if isinstance(c.get("expires_on"), str) else ""
        return name, f"Marked check '{name}' not applicable{until}"
    if e.action == "quality_exception.deleted":
        return name, f"Check '{name}' applies again: the not-applicable mark was removed"
    details = quality_exception_text(e.action, c, names).partition("; ")[2]
    return name, f"Changed not-applicable check '{name}'" + (f": {details}" if details else "")


def _item_sentence(e, names, parent_id) -> tuple[str, str]:
    """A contract line in the contract's History ("Added line 'Payroll' (40%)") or in the record's ("Added to
    contract 'Atlassian Cloud' (40%)")."""
    c = e.changes
    on_contract = str(c.get("contract") or e.related_target_id) == str(parent_id)
    if on_contract:
        name = str(c.get("name") or names.value("record", c.get("record")))
        what = f"line '{name}'"
    else:
        contract_id = str(c.get("contract") or e.related_target_id or "")
        name = names.contract.get(contract_id, "a deleted contract")
        what = f"contract '{name}'"
    pair = c.get("allocation_percent")
    if e.action == "contract_item.created":
        verb = "Added" if on_contract else "Added to"
        return name, f"{verb} {what} ({percent_text(pair)})"
    if e.action == "contract_item.deleted":
        verb = "Removed" if on_contract else "Removed from"
        return name, f"{verb} {what}"
    if isinstance(pair, list) and len(pair) == 2:
        change = f"{percent_text(pair[0])} → {percent_text(pair[1])}"
    else:
        change = "changed"
    return name, f"Allocation on {what}: {change}"


def child_of(e, parent_id, names) -> dict[str, Any] | None:
    """{kind, kind_label, id, name, sentence} for an event about a child of `parent_id` (an attachment, instance,
    link, link confirmation, review, data-quality exception, integration it is an end of, or renewal cycle) in the
    parent's History; None for the parent's own event. `id` is the child row's ID, null for an older event that
    targeted the parent itself; `sentence` names the child in words ("Added attachment 'Contract'")."""
    kind = child_kind(e, parent_id)
    if kind is None:
        return None
    c = e.changes
    if kind == "attachment":
        name, sentence = _attachment_sentence(e)
    elif kind == "instance":
        name, sentence = _instance_sentence(e, names)
    elif kind == "relationship" or (kind == "link_confirmation" and link_id(e)):
        name, sentence = _relationship_sentence(e, names, parent_id)
    elif kind == "link_confirmation":  # an integration's own confirmation
        who = names.value("confirmed_by", c.get("confirmed_by"))
        name, sentence = "Confirmation", f"Integration confirmed by {who} on {c.get('confirmed_on', '')}"
    elif kind == "review":
        who = names.value("confirmed_by", c.get("confirmed_by"))
        name, sentence = "Review", f"Review recorded: confirmed by {who}"
    elif kind == "quality_exception":
        name, sentence = _quality_sentence(e, names)
    elif kind == "integration":
        name, sentence = _integration_sentence(e, names, parent_id)
    elif kind == "contract_item":
        name, sentence = _item_sentence(e, names, parent_id)
    elif kind == "other":
        name = TARGET_TYPES.get(e.target_type, e.target_type or "Related")
        detail = summary(e, names)
        sentence = LABELS.get(e.action, e.action) + (f": {detail}" if detail else "")
    else:  # renewal_cycle
        sequence = _snap(e, "sequence") or c.get("sequence")
        name = f"Cycle {sequence}" if sequence else "A cycle"
        detail = summary(e, names)
        sentence = f"{name}: {LABELS.get(e.action, e.action)}" + (f". {detail}" if detail else "")
    child_id = None if str(e.target_id) == str(parent_id) else e.target_id
    return {
        "kind": kind,
        "kind_label": CHILD_KINDS[kind],
        "id": str(child_id) if child_id else None,
        "name": name,
        "sentence": sentence,
    }


# ---- field-level diffs ---------------------------------------------------------------------

DIFF_LABELS = {
    **FIELD_LABELS,
    "kind": "Kind",
    "archived_at": "Archived",
    "tags": "Tags",
    "website": "Website",
    "department": "Department",
    "user": "User",
    "anonymized_at": "Anonymized",
    "application": "Application",
    "resource": "Record",
    "recorded_by": "Recorded by",
    "confirmation_date": "Confirmation date",
    "next_review_date": "Next review",
    "evidence": "Evidence",
    "inactive_reason": "Reason for an inactive person",
    "confirmed_values": "Confirmed values",
    "relationship": "Relationship",
    "integration": "Integration",
    "confirmed_on": "Confirmed on",
    "confirmed_signature": "Confirmed link",
    "retirement_ack_reason": "Retirement reason",
    "obligation": "Renewal",
    "sequence": "Cycle",
    "notice_value": "Notice period",
    "notice_unit": "Notice period unit",
    "term_value": "Term",
    "term_unit": "Term unit",
    "decision_notes": "Decision notes",
    "decided_on": "Decided on",
    "completed_on": "Completed on",
    "completion_evidence": "Completion evidence",
    "query": "Query",
    "timezone": "Timezone",
    "review_interval_days": "Review interval (days)",
    "renewal_lead_days": "Renewal lead time (days)",
    "reminder_thresholds": "Reminder thresholds",
    "digest_time": "Digest time",
    "coordinator": "Coordinator",
    "settings": "Settings",
    "enabled": "Enabled",
    "config": "Connection",
    "scope": "Scope",
    "reviewer": "Reviewer",
    "connector": "Connector",
    "microsoft_connection": "Microsoft connection",
    "halo_connection": "Halo connection",
    "okta_connection": "Okta connection",
    "google_workspace_connection": "Google Workspace connection",
    "tenant_id": "Tenant ID",
    "client_id": "Client ID",
    "base_url": "Address",
    "key_id": "Key ID",
    "admin_email": "Administrator",
    "service_account_email": "Service account",
    "service_account_client_id": "Service account client ID",
    "revision": "Revision",
    "tested_revision": "Tested revision",
    "test_ok": "Last test passed",
    "external_id": "Source ID",
    "display_name": "Name in source",
    "gone_at": "No longer seen",
    "column_mapping": "Column mapping",
    "previewed_at": "Previewed",
    "committed_at": "Committed",
    "purged_at": "Purged",
    "prefix": "Prefix",
    "read_only": "Read-only",
    "expires_on": "Expires",
    "revoked_at": "Revoked",
}
# Payload members that are not a field's [before, after] pair, so a diff built from `changes` skips them.
NOT_FIELDS = {"retirement_ack", "undo", "bulk", "import_run", "cycle", "fields", "purged", "discovery"}


def is_identity(value) -> bool:
    return isinstance(value, dict) and "id" in value and "display" in value


def is_identity_list(value) -> bool:
    return isinstance(value, list) and all(is_identity(v) for v in value)


def _blank(value) -> bool:
    return value in (None, "", [], {})


def _row(field, change, before, after):
    row = {"field": field, "label": DIFF_LABELS.get(field, field.replace("_", " ").capitalize()), "change": change}
    row["before"], row["after"] = before, after
    lists = [v for v in (before, after) if v not in (None, [])]
    if lists and all(isinstance(v, list) for v in lists):
        # A set (tags, covered applications): what joined and what left, by identity (or by value for plain lists).
        def key(v):
            return ("id", str(v["id"])) if is_identity(v) else ("value", str(v))

        was, now = {key(v): v for v in before or []}, {key(v): v for v in after or []}
        row["added"] = [v for k, v in now.items() if k not in was]
        row["removed"] = [v for k, v in was.items() if k not in now]
    return row


def snapshot_diff(pre, post) -> list[dict]:
    """Field rows between two snapshots: `added` (only after: a creation, or a field snapshots gained later),
    `removed` (only before: a deletion) and `changed`. A creation lists only the fields it set, a deletion only the
    fields that held something."""
    pre_keys = list(pre or {})
    keys = list(post or {}) + [k for k in pre_keys if k not in (post or {})]
    rows = []
    for k in keys:
        in_pre, in_post = pre is not None and k in pre, post is not None and k in post
        before, after = (pre or {}).get(k), (post or {}).get(k)
        if in_pre and in_post:
            if before != after:
                rows.append(_row(k, "changed", before, after))
        elif in_post and not _blank(after):
            rows.append(_row(k, "added", None, after))
        elif in_pre and not _blank(before):
            rows.append(_row(k, "removed", before, None))
    return rows


def changes_diff(e, names) -> list[dict]:
    """For an event without snapshots (written before them): the [before, after] pairs of its payload, references as
    identity entries named as they are now."""
    rows = []
    for k, v in e.changes.items():
        if k in NOT_FIELDS or not (isinstance(v, list) and len(v) == 2):
            continue
        lookup = names.lookup(k)
        before, after = v
        if lookup is not None:
            before, after = (None if _blank(x) else {"id": x, "display": lookup.get(str(x))} for x in (before, after))
        rows.append(_row(k, "changed", before, after))
    return rows


# Snapshot fields that reference a Person (a UUID) or a User (an integer), stored without a display name.
PEOPLE_REFS = {
    "business_owner",
    "technical_owner",
    "confirmed_by",
    "responsible",
    "it_steward",
    "user",
    "recorded_by",
    "owner",
    "followup",
    "coordinator",
    "reviewer",
    "uploaded_by",
    "created_by",
    "renewal_owner",
}


def resolve_people(org, rows) -> None:
    """Fill `display` of the identity entries snapshots store without one: a Person (UUID id) gets the current
    name, so an anonymized Person shows as "Former person #n"; a User (integer id) its name or email."""
    entries = []
    for row in rows:
        if row["field"] not in PEOPLE_REFS:
            continue
        for side in ("before", "after", "added", "removed"):
            value = row.get(side)
            values = value if isinstance(value, list) else [value]
            entries += [v for v in values if is_identity(v) and v["display"] is None]
    if not entries:
        return
    users = {v["id"] for v in entries if isinstance(v["id"], int)}
    persons = {v["id"] for v in entries if isinstance(v["id"], str)}
    names: dict = {}
    if persons:
        for p in Person.objects.filter(organization=org, id__in=persons).only("id", "name"):
            names[str(p.id)] = p.name
    if users:
        for u in get_user_model().objects.filter(id__in=users):
            names[u.id] = u.get_full_name() or u.email
    for v in entries:
        v["display"] = names.get(v["id"], "a deleted record")


def event_diff(e, names, org) -> tuple[str, list[dict]]:
    """("snapshots" | "changes" | "none", rows) for the API's diff view."""
    if e.prechange is not None or e.postchange is not None:
        # A copy: names are filled in below, and the stored snapshots are returned as stored.
        source, rows = "snapshots", copy.deepcopy(snapshot_diff(e.prechange, e.postchange))
    else:
        rows = changes_diff(e, names)
        source = "changes" if rows else "none"
    resolve_people(org, rows)
    for row in rows:
        if is_custom(row["field"]):  # the field's current label (catalog/customfields.py)
            row["label"] = names.label(row["field"])
    return source, rows


# The target types events are written with (AuditEvent.target_type / related_target_type), for the log's filter.
TARGET_TYPES = {
    "Resource": "Record",
    "ApplicationInstance": "Instance",
    "TrackedVersion": "End-of-life tracking",
    "RecordReview": "Review",
    "Relationship": "Relationship",
    "LinkConfirmation": "Link confirmation",
    "ChangeAssessment": "Assessment",
    "RenewalObligation": "Renewal",
    "RenewalCycle": "Renewal cycle",
    "Person": "Person",
    "Team": "Team",
    "Department": "Department",
    "Vendor": "Vendor",
    "VendorRiskProfile": "Vendor risk file",
    "VendorRiskDocument": "Vendor risk document",
    "SoftwareCategory": "Software category",
    "Tag": "Tag",
    "SavedFilter": "Saved view",
    "SavedViewSubscription": "Saved view subscription",
    "Organization": "Organization",
    "Connector": "Connector",
    "Observation": "Discovery candidate",
    "ImportSource": "Import source",
    "ImportRun": "Import",
    "ApiToken": "API token",
    "CalendarFeed": "Calendar feed",
    "Digest": "Digest",
    "AIProvider": "AI provider",
    "AIModel": "AI model",
    "CustomField": "Custom field",
    "RelationshipType": "Relationship type",
    "Attachment": "Attachment",
    "QualityException": "Data-quality exception",
    "Contract": "Contract",
    "ContractItem": "Contract line",
    "LicensePool": "License pool",
    "Reminder": "Reminder",
    "EmailSettings": "Email settings",
    "TeamsDestination": "Teams destination",
    "MicrosoftConnection": "Microsoft connection",
    "HaloConnection": "Halo connection",
    "TicketLink": "Ticket link",
    "TicketConnection": "Ticket connection",
    "OktaConnection": "Okta connection",
    "GoogleWorkspaceConnection": "Google Workspace connection",
    "WebhookDestination": "Webhook destination",
    "Delivery": "Notification delivery",
    "NotificationRule": "Notification rule",
    "AssistantSettings": "Assistant settings",
    "SsoSettings": "Sign-in settings",
    "Invitation": "Invitation",
    "OrgExport": "Data export",
    "PendingAccess": "Access request",
    "ScimToken": "SCIM token",
    "ScimUser": "Provisioned user",
    "ScimGroup": "Provisioned group",
    "Membership": "Membership",
    "AssistantBatch": "Assistant batch",
    "AssistantSuggestion": "Assistant suggestion",
}


def _assistant_summary(action: str, c: dict) -> str:
    if action == "assistant_settings.updated":
        parts = []
        if "enabled" in c:
            parts.append("turned " + ("on" if c["enabled"] else "off"))
        if "monthly_cap_usd" in c:
            parts.append(f"monthly cap {c['monthly_cap_usd']} USD")
        if c.get("replaced"):
            parts.append("API key replaced or removed")
        return "; ".join(parts) or "saved"
    if action == "assistant_settings.key_refused":
        return f"The {c.get('source', '')} API key was refused (HTTP {c.get('status')})"
    if action == "assistant_batch.requested":
        return f"For {c['connector']}" if c.get("connector") else "For every connector"
    if action == "assistant_batch.finished":
        stop = f", stopped: {str(c['stop_reason']).replace('_', ' ')}" if c.get("stop_reason") else ""
        return f"{c.get('suggested', 0)} suggested, {c.get('refused', 0)} declined, {c.get('failed', 0)} failed{stop}"
    if action == "assistant_suggestion.recorded":
        return f"{c.get('source_name', '')}: suggested {c.get('suggested', '')}, decided {c.get('decided', '')} ({c.get('outcome', '')})"
    if action == "assistant_suggestion.rejected":
        note = f": {c['note']}" if c.get("note") else ""
        return f"{c.get('source_name', '')}: suggested {c.get('suggested', '')}{note}"
    return ""
