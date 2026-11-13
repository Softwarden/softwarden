"""Ticket links: Jira, ServiceNow and HaloITSM tickets linked to a change
assessment or a renewal (and optionally one of the renewal's cycles). Softwarden is not a ticketing tool: a link
records which ticket tracks the work, and its status when a ticket connection can read it.

- **TicketLink** (tenant): the ticket's system, key, https URL, title and normalised status. An Editor pastes the
  URL; the system and key are parsed from known URL shapes (tickets/parsing.py). Written by tickets/services.py,
  audited. tickets 0001 adds the composite foreign keys to the assessment, the renewal and the cycle.
- **TicketConnection** (tenant, a core Connection): how Softwarden reads a system's ticket status (and, only when
  `create_enabled` is on, creates a ticket). Jira (email + API token) and ServiceNow (user + password) keep their own
  Fernet secret slot; a Halo ticket connection points at the organization's HaloConnection and uses its
  client credentials. One per organization and system. Admin only (tickets/views.py, TicketConnectionViewSet)."""

from django.conf import settings
from django.db import models
from django.db.models import Q

from catalog.models import ChangeAssessment
from core.models import Connection, HaloConnection, OrgScoped
from money.models import RenewalCycle, RenewalObligation

URL_MAX = 2048
KEY_MAX = 100
TITLE_MAX = 300
EXTERNAL_STATUS_MAX = 100
ERROR_MAX = 300


class TicketSystem(models.TextChoices):
    JIRA = "jira", "Jira"
    SERVICENOW = "servicenow", "ServiceNow"
    HALO = "halo", "HaloITSM"
    OTHER = "other", "Other"


# The systems a connection can read; "other" links are manual only.
CONNECTED_SYSTEMS = (TicketSystem.JIRA, TicketSystem.SERVICENOW, TicketSystem.HALO)


class TicketStatus(models.TextChoices):
    OPEN = "open", "Open"
    IN_PROGRESS = "in_progress", "In progress"
    RESOLVED = "resolved", "Resolved"
    CLOSED = "closed", "Closed"
    UNKNOWN = "unknown", "Unknown"


# Links the refresh keeps reading: anything not finished.
LIVE_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.UNKNOWN)


class StatusSource(models.TextChoices):
    MANUAL = "manual", "Set by hand"
    REFRESHED = "refreshed", "Read from the system"


class TicketLink(OrgScoped):
    """A ticket on an assessment or a renewal: exactly one of `assessment` and `obligation` is set; `cycle` only with
    `obligation`, and then one of that renewal's own cycles (the services check it, and since tickets 0002 so does the
    database: a composite key from (organization, obligation, cycle) to the cycle's (organization, obligation, id)).
    Deleted with its assessment or renewal.

    `status` is the normalised status, set by hand (`status_source` manual) or read through the organization's
    ticket connection (`refreshed`, at `status_checked_at`; `external_status` is the system's own status name, e.g.
    "In Review"). `status_error` is the last refresh's failure in Softwarden's words ("" after a success); it never
    holds a server's text, a token or a secret. `created_in_system` marks a ticket Softwarden created."""

    assessment = models.ForeignKey(
        ChangeAssessment, null=True, blank=True, on_delete=models.CASCADE, related_name="ticket_links"
    )
    obligation = models.ForeignKey(
        RenewalObligation, null=True, blank=True, on_delete=models.CASCADE, related_name="ticket_links"
    )
    cycle = models.ForeignKey(
        RenewalCycle, null=True, blank=True, on_delete=models.CASCADE, related_name="ticket_links"
    )
    system = models.CharField(max_length=20, choices=TicketSystem.choices)
    external_key = models.CharField(max_length=KEY_MAX, blank=True)
    url = models.CharField(max_length=URL_MAX)
    title = models.CharField(max_length=TITLE_MAX, blank=True)
    status = models.CharField(max_length=20, choices=TicketStatus.choices, default=TicketStatus.UNKNOWN)
    status_source = models.CharField(max_length=20, choices=StatusSource.choices, default=StatusSource.MANUAL)
    external_status = models.CharField(max_length=EXTERNAL_STATUS_MAX, blank=True)
    status_checked_at = models.DateTimeField(null=True, blank=True)
    status_error = models.CharField(max_length=ERROR_MAX, blank=True)
    created_in_system = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        ordering = ["created_at", "id"]
        verbose_name = "ticket link"
        constraints = OrgScoped.Meta.constraints + [
            models.CheckConstraint(
                condition=(Q(assessment__isnull=False) & Q(obligation__isnull=True))
                | (Q(assessment__isnull=True) & Q(obligation__isnull=False)),
                name="tickets_ticketlink_one_target",
            ),
            models.CheckConstraint(
                condition=Q(cycle__isnull=True) | Q(obligation__isnull=False),
                name="tickets_ticketlink_cycle_needs_renewal",
            ),
            models.UniqueConstraint(
                fields=["organization", "assessment", "url"],
                condition=Q(assessment__isnull=False),
                name="tickets_ticketlink_assessment_url",
            ),
            models.UniqueConstraint(
                fields=["organization", "obligation", "url"],
                condition=Q(obligation__isnull=False),
                name="tickets_ticketlink_renewal_url",
            ),
        ]
        indexes = [models.Index(fields=["organization", "system", "status"], name="tickets_link_refresh")]

    def __str__(self) -> str:
        return self.external_key or self.url


class TicketConnection(Connection):
    """How Softwarden reads one system's tickets for an organization (one per system).

    - Jira Cloud: `base_url` (https://yourcompany.atlassian.net), `username` the Atlassian account email, the secret an
      API token (HTTP basic auth). `create_target` is the project key, `create_issue_type` the issue type name.
    - ServiceNow: `base_url` (https://instance.service-now.com), `username` a user with read access to the task
      tables, the secret its password (HTTP basic auth). `create_target` is the table (default incident).
    - HaloITSM: `halo_connection` (the HaloConnection, whose scope must include read:tickets, and edit:tickets
      to create); no address or secret of its own. `create_target` is the ticket type ID.

    `create_enabled` (off by default) lets Editors create a ticket through this connection; every attempt is audited.
    `revision` moves with the address, the user name, the Halo connection and the secret (core Connection.save)."""

    CONNECTION_FIELDS = ("system", "base_url", "username", "halo_connection_id")

    system = models.CharField(max_length=20, choices=[(s.value, s.label) for s in CONNECTED_SYSTEMS])
    base_url = models.CharField(max_length=300, blank=True)
    username = models.CharField(max_length=254, blank=True)
    halo_connection = models.ForeignKey(
        HaloConnection, null=True, blank=True, on_delete=models.PROTECT, related_name="ticket_connections"
    )
    create_enabled = models.BooleanField(default=False)
    create_target = models.CharField(max_length=100, blank=True)
    create_issue_type = models.CharField(max_length=60, blank=True)

    class Meta(Connection.Meta):
        verbose_name = "ticket connection"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["organization", "system"], name="tickets_ticketconnection_system"),
            models.CheckConstraint(
                condition=Q(system="halo", halo_connection__isnull=False)
                | (~Q(system="halo") & Q(halo_connection__isnull=True)),
                name="tickets_ticketconnection_halo_pair",
            ),
        ]
