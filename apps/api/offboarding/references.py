"""Where a Person, or the user account linked to them, can appear: every foreign key and many-to-many field to Person,
to the user model and to Membership, classified.

Each reference is either part of the footprint (the sections it feeds) or deliberately left out with a reason.
`introspected` reads the model graph, so a new field pointing at a Person or an account shows up there; the guard in
tests/test_offboarding.py fails until that field is placed in one of the registries below. Text fields that name
someone (an AI agent's `run_as_identity`, the Entra owners in a discovery observation's data) aren't foreign keys and
are matched in footprint.py.

Custom fields have no person type (catalog.models.CustomFieldType), so they hold no references to classify."""

from dataclasses import dataclass

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import models

from catalog.models import Person
from core.models import Membership

# Footprint sections, in the order the response lists them: key -> label. footprint.py builds each one.
SECTIONS: dict[str, str] = {
    "records": "Applications and components they own",
    "ai_agents": "AI agents they own or that run as them",
    "integrations": "Integrations they own",
    "it_steward": "Records they are IT steward of",
    "teams": "Teams they are on",
    "vendors": "Vendors they are account manager for",
    "contracts": "Contracts they are renewal owner of",
    "renewals": "Renewals they are responsible for or follow up",
    "reviews": "Reviews they confirmed",
    "assessments": "Change assessments they own",
    "quality_exceptions": "Data-quality exceptions they answer for",
    "notification_rules": "Notification rules that name them",
    "discovery": "Discovery sources they review",
    "entra_owners": "Discovered apps that list them as an owner",
    "licences": "Licenses assigned to them",
    "account": "Their account",
}

# Why a reference is left out of the footprint.
HISTORY = "history: who did something once, not a responsibility; the audit log keeps it"
PLUMBING = "account plumbing that goes with the account, not a responsibility to hand over"
LINK = "the link between the Person and their account itself"


@dataclass(frozen=True)
class Ref:
    sections: tuple[str, ...] = ()  # footprint sections it feeds; empty: left out
    reason: str = ""  # why it is left out

    def __post_init__(self):
        assert bool(self.sections) != bool(self.reason), "a reference is in the footprint or left out with a reason"
        assert set(self.sections) <= set(SECTIONS), f"unknown section in {self.sections}"


def included(*sections: str) -> Ref:
    return Ref(sections=sections)


def excluded(reason: str) -> Ref:
    return Ref(reason=reason)


# Fields pointing at a Person.
PERSON_REFERENCES: dict[str, Ref] = {
    "catalog.Resource.business_owner": included("records", "ai_agents"),  # integrations have none
    "catalog.Resource.technical_owner": included("records", "ai_agents", "integrations"),
    "catalog.Team.members": included("teams"),
    "catalog.TeamMember.person": included("teams"),
    "catalog.Vendor.account_manager": included("vendors"),
    "catalog.RecordReview.confirmed_by": included("reviews"),
    "catalog.LinkConfirmation.confirmed_by": included("reviews"),
    "catalog.QualityException.owner": included("quality_exceptions"),
    "money.RenewalObligation.responsible": included("renewals"),
    "money.Contract.renewal_owner": included("contracts"),
    "notify.NotificationRule.people": included("notification_rules"),
    "notify.NotificationRule.escalation_people": included("notification_rules"),
    "notify.NotificationRule.fallback_person": included("notification_rules"),
    "notify.NotificationRulePerson.person": included("notification_rules"),
    "notify.NotificationRuleEscalationPerson.person": included("notification_rules"),
    # Licence waste: the licensed user a licence usage run linked to the Person (discovery.LicensedUser).
    "discovery.LicensedUser.person": included("licences"),
}

# Fields pointing at the user model or at a Membership: the Person's account, when they have one.
ACCOUNT_REFERENCES: dict[str, Ref] = {
    # In the footprint
    "catalog.Resource.it_steward": included("it_steward"),
    "catalog.ChangeAssessment.owner": included("assessments"),
    "money.RenewalObligation.followup": included("renewals"),
    "discovery.Connector.reviewer": included("discovery"),
    "core.Organization.coordinator": included("account"),
    "core.Membership.user": included("account"),
    "api.ApiToken.user": included("account"),
    "money.CalendarFeed.user": included("account"),
    "catalog.SavedFilter.user": included("account"),
    "notify.SavedViewSubscription.membership": included("account"),
    # The link itself
    "catalog.Person.user": excluded(LINK),
    # Account plumbing
    "auth.User_groups.user": excluded(PLUMBING),
    "auth.User_user_permissions.user": excluded(PLUMBING),
    "core.UserIdentity.user": excluded(PLUMBING),
    "core.ScimUser.user": excluded(PLUMBING),
    "core.ScimUser.membership": excluded(PLUMBING),
    "core.PlatformStaff.user": excluded(PLUMBING),
    "core.PasswordReset.user": excluded(PLUMBING),  # reset links, an hour each
    "core.MailboxProof.user": excluded(PLUMBING),  # that the owner proved the mailbox
    "notify.NotificationPreference.membership": excluded(PLUMBING),
    "notify.Digest.user": excluded(PLUMBING),
    "notify.NotificationSnooze.user": excluded(PLUMBING),
    # History
    "core.Organization.deletion_requested_by": excluded(HISTORY),
    "core.Invitation.invited_by": excluded(HISTORY),
    "core.Invitation.accepted_by": excluded(HISTORY),
    "core.SsoSettings.activated_by": excluded(HISTORY),
    "core.SsoSettings.draft_tested_by": excluded(HISTORY),
    "core.ScimToken.created_by": excluded(HISTORY),
    "core.AuditEvent.actor": excluded(HISTORY),
    "core.AuditEvent.impersonated_by": excluded(HISTORY),
    "core.BackupState.key_acknowledged_by": excluded(HISTORY),
    "core.MicrosoftConnection.tested_by": excluded(HISTORY),
    "core.HaloConnection.tested_by": excluded(HISTORY),
    "core.OktaConnection.tested_by": excluded(HISTORY),
    "core.GoogleWorkspaceConnection.tested_by": excluded(HISTORY),
    "core.OrgExport.requested_by": excluded(HISTORY),
    "core.PlatformAuditEvent.actor": excluded(HISTORY),
    "core.CapabilityOverride.created_by": excluded(HISTORY),
    "core.OrganizationLimitOverride.created_by": excluded(HISTORY),
    "catalog.Relationship.created_by": excluded(HISTORY),
    "catalog.RecordReview.recorded_by": excluded(HISTORY),
    "catalog.LinkConfirmation.recorded_by": excluded(HISTORY),
    "catalog.AssessmentSnapshot.taken_by": excluded(HISTORY),
    "catalog.SavedFilter.created_by": excluded(HISTORY),
    "catalog.Attachment.uploaded_by": excluded(HISTORY),
    "catalog.QualityException.created_by": excluded(HISTORY),
    "money.RenewalObligation.created_by": excluded(HISTORY),
    "money.Contract.created_by": excluded(HISTORY),
    "money.SeatCountReview.decided_by": excluded(HISTORY),
    "notify.Reminder.acknowledged_by": excluded(HISTORY),
    "notify.Reminder.resolved_by": excluded(HISTORY),
    "notify.Delivery.requested_by": excluded(HISTORY),
    "notify.SavedViewSubscription.created_by": excluded(HISTORY),
    "discovery.Connector.run_requested_by": excluded(HISTORY),
    "discovery.Connector.access_checked_by": excluded(HISTORY),
    "discovery.ConnectorRun.requested_by": excluded(HISTORY),
    "discovery.ReconciliationDecision.decided_by": excluded(HISTORY),
    "discovery.ReconciliationDecision.gone_acknowledged_by": excluded(HISTORY),
    "discovery.SourceDifference.ignored_by": excluded(HISTORY),
    "discovery.AssistantBatch.requested_by": excluded(HISTORY),
    "discovery.AssistantSuggestion.outcome_by": excluded(HISTORY),
    "imports.ImportRun.uploaded_by": excluded(HISTORY),
    "api.ApiToken.created_by": excluded(HISTORY),
    "tickets.TicketConnection.tested_by": excluded(HISTORY),  # ticket links: who last tested the connection
    "tickets.TicketLink.created_by": excluded(HISTORY),  # who pasted or created the link; the ticket has its own owner
}


def introspected(*targets: type[models.Model]) -> set[str]:
    """ "<app>.<Model>.<field>" for every foreign key, one-to-one and many-to-many field of every installed model
    (auto-created through tables included) that points at one of `targets`."""
    found = set()
    for model in apps.get_models(include_auto_created=True):
        for field in model._meta.get_fields(include_hidden=True):
            if field.is_relation and not field.auto_created and field.related_model in targets:
                found.add(f"{model._meta.label}.{field.name}")
    return found


def person_references() -> set[str]:
    return introspected(Person)


def account_references() -> set[str]:
    return introspected(get_user_model(), Membership)


def sections_fed() -> set[str]:
    """The sections some classified reference feeds."""
    refs = (*PERSON_REFERENCES.values(), *ACCOUNT_REFERENCES.values())
    return {s for r in refs for s in r.sections}


# Sections fed by matching text rather than a foreign key, or by nothing Softwarden stores.
TEXT_SECTIONS = {
    "ai_agents": "AIAgent.run_as_identity matched to the Person's email or Entra object ID",
    "entra_owners": "Observation.data.owners matched by Entra object ID or email (discovery.engine.suggested_owners)",
    "licences": "not stored: per-user license assignment needs User.Read.All, which Softwarden doesn't request",
}
