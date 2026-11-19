"""Create a synthetic demo organization. Fictional data only; safe to run repeatedly."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from catalog import admin_services, ai, assessments, customfields, quality, saved_filters, services
from catalog.models import (
    AIProvider,
    CustomField,
    Department,
    Person,
    QualityException,
    Resource,
    SoftwareCategory,
    Tag,
    Team,
    Vendor,
    VendorRiskProfile,
)
from core import connections, simulation
from core.demo import DEMO_PASSWORD
from core.models import Membership, Organization, Role
from discovery import engine as discovery
from discovery.models import Connector, Observation
from imports import importer as imports
from imports.models import ImportSource
from money import contracts, renewals
from money.models import Contract, RenewalObligation

USERS = [
    ("admin@example.test", "Alex", "Admin", Role.ADMIN),
    ("editor@example.test", "Sam", "Editor", Role.EDITOR),
    ("viewer@example.test", "Vic", "Viewer", Role.VIEWER),
]
# A platform staff account with no membership (the console only), demo password, made by --accounts-only
# only (the Playwright suite's start) and only on a throwaway `_e2e` database (the e2e routes' database condition,
# api/urls.e2e_enabled). Platform staff reach every organization on the installation, so neither the full demo nor
# --accounts-only on a development or demo database ever makes one.
PLATFORM_STAFF = ("platform@example.test", "Pat", "Platform")


def e2e_database() -> bool:
    """Whether this is a Playwright database (its name ends in _e2e; apps/api/e2e_db.py makes only those)."""
    from django.conf import settings

    return str(settings.DATABASES["default"]["NAME"]).endswith("_e2e")


PEOPLE = [
    ("Dana Ortiz", "dana.ortiz@example.test", "IT Applications", True),
    ("Priya Raman", "priya.raman@example.test", "HR", True),
    ("Lee Chen", "lee.chen@example.test", "Finance", True),
    ("Former person #3", "", "IT Integrations", False),
]
TEAMS = ["IT Applications", "IT Integrations", "Service Desk"]
# Vendor support details, set when the vendor is first made. All addresses are synthetic.
VENDORS = {
    "Workday Inc.": {
        "website": "https://workday.example.test",
        "support_url": "https://workday.example.test/support",
        "support_email": "support@workday.example.test",
        "support_phone": "+1 555 0100",
        "portal_url": "https://workday.example.test/customer-portal",
        "notes": "Contract renews every March; raise severity 1 issues by phone.",
    },
    "Zoom": {
        "website": "https://zoom.example.test",
        "support_url": "https://zoom.example.test/support",
        "support_email": "help@zoom.example.test",
    },
    "Zylker Software": {"website": "https://zylker.example.test", "support_phone": "+1 555 0142"},
}
# Team members: optional, a team is never an owner.
TEAM_MEMBERS = {"IT Applications": ["Dana Ortiz"], "IT Integrations": ["Dana Ortiz", "Lee Chen"]}
TAG_COLORS = {"Finance": ("green", "Finance and month-end close."), "HR": ("violet", "People data.")}
# Fields per record: category by name, the shared department and classification, and kind fields.
RECORD_FIELDS: dict[str, dict[str, object]] = {
    "Workday HR": {
        "category": "HR and people",
        "department": "HR",
        "cost_center": "CC-2100",
        "data_classification": "restricted",
        "deployment_model": "saas",
        "version": "2026R2",
        "primary_url": "https://contoso.workday.example.test",
        "admin_url": "https://contoso.workday.example.test/admin",
        "authentication_method": "saml",
        "sso_enabled": True,
        "scim_provisioning": True,
        "entra_app_id": "6f1d4c2a-3b5e-4a7f-9c8d-1e2f3a4b5c6d",
    },
    "Finance Ledger": {
        "category": "ERP and finance",
        "department": "Finance",
        "cost_center": "CC-3100",
        "data_classification": "confidential",
        "deployment_model": "hybrid",
        "authentication_method": "oidc",
        "sso_enabled": True,
        "scim_provisioning": False,
    },
    "Budget Planner": {"category": "ERP and finance", "department": "Finance", "deployment_model": "desktop"},
    # No documentation URL: the data-quality scenario marks that check not applicable on it.
    "Executive Dashboard": {
        "category": "Data and analytics",
        "data_classification": "confidential",
        "documentation_url": "",
    },
    "Zoom Meetings": {
        "category": "Collaboration",
        "data_classification": "internal",
        "deployment_model": "saas",
        "authentication_method": "saml",
        "sso_enabled": True,
    },
    "Employee Timesheets": {"category": "HR and people", "department": "HR", "deployment_model": "saas"},
    "Service Desk Portal": {
        "category": "IT service management",
        "authentication_method": "local",
        "sso_enabled": False,
    },
    "Nightly Payroll Export": {
        "department": "IT Applications",
        "data_classification": "restricted",
        "hosting": "on_prem",
        "environment": "prod",
        "schedule_or_trigger": "Daily at 01:00",
        "runbook_url": "https://wiki.example.test/runbooks/nightly-payroll-export",
        "external_id": "TASK-PAYROLL-EXPORT",
    },
}
# AI agents: providers with their models, and three agents. The HR assistant is the complete one; the
# invoice agent has no human-in-loop decision and the service desk helper no owner, so both show under AI agent gaps.
AI_PROVIDERS = {
    "Microsoft": (
        "https://azure.microsoft.com/products/ai-services/openai-service",
        [
            ("GPT-4o", "2024-11-20", "multimodal", "Sweden Central"),
            ("text-embedding-3-large", "1", "embedding", "Sweden Central"),
        ],
    ),
    "Anthropic": ("https://www.anthropic.com", [("Claude Sonnet", "4.5", "multimodal", "EU")]),
    "OpenAI": ("https://openai.com", [("GPT-4o mini", "2024-07-18", "multimodal", "US")]),
}
# (name, lifecycle, criticality, business owner, technical owner, team, models, fields)
AI_AGENTS: list[tuple[str, str, str, str | None, str | None, str, list[str], dict[str, object]]] = [
    (
        "HR Policy Assistant",
        "active",
        "medium",
        "Priya Raman",
        "Dana Ortiz",
        "IT Applications",
        ["GPT-4o", "text-embedding-3-large"],
        {
            "purpose": "Answers HR policy questions from employees in Teams and Microsoft 365 Copilot.",
            "platform": "copilot_studio",
            "environment": "prod",
            "pp_environment_id": "3f2a9c1e-5b7d-4e8f-a1c2-d3e4f5a6b7c8",
            "channels": ["teams", "m365_copilot"],
            "knowledge_sources": "SharePoint: HR Policies site (https://contoso.sharepoint.example.test/sites/hr)",
            "tools_connectors": "Workday HR connector (read only)",
            "auth_mode": "user",
            "run_as_identity": "The signed-in employee",
            "data_access_scope": "HR policy documents only; no employee records.",
            "human_in_loop": True,
            "actions_can_write": False,
            "risk_classification": "limited",
            "external_id": "cr1a2_hrPolicyAssistant",
            "data_classification": "internal",
            "department": "HR",
        },
    ),
    (
        "Invoice Triage Agent",
        "active",
        "high",
        "Lee Chen",
        None,
        "IT Integrations",
        ["Claude Sonnet"],
        {
            "purpose": "Reads vendor invoices from the AP mailbox and drafts ledger entries for review.",
            "platform": "claude",
            "environment": "prod",
            "channels": ["email", "api"],
            "knowledge_sources": "AP shared mailbox; vendor master data",
            "tools_connectors": "Finance Ledger API (draft entries)",
            "auth_mode": "service_principal",
            "run_as_identity": "svc-invoice-triage",
            "data_access_scope": "Vendor invoices and the vendor master in Finance Ledger.",
            "actions_can_write": True,
            "risk_classification": "high",
            "repo_url": "https://git.example.test/contoso/invoice-triage-agent",
            "department": "Finance",
        },
    ),
    (
        "Service Desk Helper",
        "active",
        "low",
        None,
        None,
        "Service Desk",
        ["GPT-4o mini"],
        {
            "purpose": "Suggests knowledge articles on the service desk portal.",
            "platform": "openai",
            "environment": "prod",
            "channels": ["web"],
            "auth_mode": "api_key",
            "data_access_scope": "Published knowledge articles.",
            "human_in_loop": False,
            "actions_can_write": False,
            "risk_classification": "minimal",
        },
    ),
]
# What the agents depend on, when those records exist: (agent, record).
AI_DEPENDS_ON = [("HR Policy Assistant", "Workday HR"), ("Service Desk Helper", "Service Desk Portal")]
# Custom fields: none required, so records made without them (the Playwright suite, imports) still save.
# "Business case" is the custom field the agent journey checks; the certificate expiry date is a reminder source.
CUSTOM_FIELDS: list[dict[str, object]] = [
    {
        "name": "business_case",
        "label": "Business case",
        "type": "longtext",
        "applies_to": ["application", "ai_agent"],
        "weight": 10,
        "description": "Why the organization runs it, in a few sentences.",
    },
    {
        "name": "annual_cost_eur",
        "label": "Annual cost (EUR)",
        "type": "decimal",
        "applies_to": ["application", "vendor"],
        "weight": 20,
    },
    {
        "name": "sox_scope",
        "label": "SOX scope",
        "type": "boolean",
        "applies_to": ["application", "component", "integration"],
        "weight": 30,
    },
    {
        "name": "hosting_regions",
        "label": "Hosting regions",
        "type": "multiselect",
        "choices": ["US", "EU", "APAC"],
        "applies_to": ["application", "ai_agent"],
        "weight": 40,
    },
    {
        "name": "certificate_expiry",
        "label": "Certificate expiry",
        "type": "date",
        "applies_to": ["application", "integration"],
        "weight": 50,
        "is_reminder_source": True,
        "description": "When the SSO or integration certificate expires.",
    },
]
# Values on the scenario's records, set through the record services as an Editor would: {record: {field: value}}.
CUSTOM_VALUES: dict[str, dict[str, object]] = {
    "HR Policy Assistant": {
        "business_case": "Answers routine HR policy questions so the HR team can focus on cases; expected to cut "
        "policy tickets by a third.",
        "hosting_regions": ["EU"],
    },
    "Workday HR": {
        "business_case": "System of record for employees, payroll inputs and absences.",
        "annual_cost_eur": 184000,
        "sox_scope": True,
        "hosting_regions": ["EU", "US"],
        "certificate_expiry": "2027-03-31",
    },
    "Finance Ledger": {"annual_cost_eur": 92500.5, "sox_scope": True},
    "Nightly Payroll Export": {"sox_scope": True},
}
SAMPLE_CSV = Path(__file__).parent / "sample_csv" / "applications.csv"
# Contracts and licence pools, dated from today so the dashboard and reports always have something to show:
# (name, vendor, amount, currency, billing, start offset, end offset, auto-renew, notice, status, cost centre, lines,
# create a renewal). Zoom's deadline falls within 30 days; ServiceDesk Plus doesn't auto-renew and its renewal action
# is overdue; the Finance Ledger platform has no vendor (a spend gap), spreads a three-year amount and drives a renewal
# made for it; Executive Dashboard analytics is signed and starts next month (a future commitment).
CONTRACTS: list[tuple] = [
    ("Workday HCM subscription", "Workday Inc.", "120000", "USD", "annual", -200, 165, "yes", (2, "months"),
     "active", "CC-2100", [("Workday HR", "80"), ("Employee Timesheets", None)], False),
    ("Zoom Business", "Zoom", "1500", "USD", "monthly", -325, 40, "yes", (30, "days"), "active", "",
     [("Zoom Meetings", None)], False),
    ("ServiceDesk Plus license", "Zylker Software", "2400", "EUR", "quarterly", -290, 75, "no", None, "active", "",
     [("Service Desk Portal", None)], False),
    ("Finance Ledger platform", None, "270000", "USD", "multi_year", -400, 694, "yes", (90, "days"), "active",
     "CC-3100", [("Finance Ledger", "60"), ("Budget Planner", None), ("Nightly Payroll Export", None)], True),
    ("Executive Dashboard analytics", None, "18000", "USD", "annual", 30, 394, "unknown", None, "signed", "",
     [("Executive Dashboard", None)], False),
]  # fmt: skip
# (application, contract, SKU name, SKU ID, purchased, assigned): Zoom is over-assigned (overuse is shown, not refused).
POOLS = [
    ("Workday HR", "Workday HCM subscription", "Workday HCM worker", "", 500, 462),
    ("Zoom Meetings", "Zoom Business", "Zoom Business license", "", 150, 163),
]
# Tags on demo records: the Finance tag backs the shared "Finance apps" filter.
TAGS = {
    "Workday HR": ["Finance", "HR"],
    "Finance Ledger": ["Finance"],
    "Employee Timesheets": ["HR"],
    "Zoom Meetings": ["Collaboration"],
}

# The dependency scenario. Everything the dependency screens show can be found in it:
#   Workday HR <- Nightly Payroll Export (a component) <- Finance Ledger <- Executive Dashboard is a chain with a
#   component in the path; Finance Ledger and Budget Planner depend on each other (a cycle); Legacy Timesheets is a
#   retired dependent of Workday HR and Employee Timesheets replaces it; "Workday to Finance Ledger payroll feed" is
#   an integration implemented by the export job; "Workday to Zoom user provisioning" is vendor-native.
# (name, kind, lifecycle, criticality, business owner, technical owner, support team, vendor, purpose)
# Data-quality exceptions the demo marks (rule, record, reason): records the scenario leaves failing that rule.
QUALITY_EXCEPTIONS = [
    ("vendor", "Budget Planner", "Free tool maintained by its community; there is no supplier to name (synthetic)."),
    (
        "documentation",
        "Executive Dashboard",
        "A self-describing report: its definitions are on the report's own glossary page (synthetic).",
    ),
]

RECORDS = [
    (
        "Workday HR",
        "application",
        "active",
        "critical",
        "Priya Raman",
        "Dana Ortiz",
        "IT Applications",
        "Workday Inc.",
        "System of record for employees, positions and payroll inputs.",
    ),
    (
        "Nightly Payroll Export",
        "component",
        "active",
        "high",
        None,
        "Dana Ortiz",
        "IT Integrations",
        None,
        "Exports approved payroll from Workday HR to the ledger every night.",
    ),
    (
        "Finance Ledger",
        "application",
        "active",
        "critical",
        "Lee Chen",
        "Dana Ortiz",
        "IT Applications",
        "Zylker Software",
        "General ledger and month-end close.",
    ),
    (
        "Budget Planner",
        "application",
        "active",
        "medium",
        "Lee Chen",
        "Lee Chen",
        "IT Applications",
        None,
        "Department budgets; reads ledger actuals and feeds it budget codes.",
    ),
    (
        "Executive Dashboard",
        "application",
        "active",
        "medium",
        "Lee Chen",
        "Dana Ortiz",
        "IT Applications",
        None,
        "Monthly headcount and spend for leadership.",
    ),
    (
        "Zoom Meetings",
        "application",
        "active",
        "low",
        "Priya Raman",
        "Dana Ortiz",
        "Service Desk",
        "Zoom",
        "Video meetings; accounts are provisioned from Workday HR.",
    ),
    (
        "Employee Timesheets",
        "application",
        "active",
        "medium",
        "Priya Raman",
        None,
        "IT Applications",
        None,
        "Timesheets for hourly staff. No technical owner yet, so it shows in Needs attention.",
    ),
    (
        "Legacy Timesheets",
        "application",
        "retired",
        "low",
        "Priya Raman",
        "Dana Ortiz",
        "IT Applications",
        None,
        "The old timesheet tool, replaced by Employee Timesheets. Still linked to Workday HR.",
    ),
    (
        "Service Desk Portal",
        "application",
        "active",
        "medium",
        None,
        None,
        "Service Desk",
        "Zylker Software",
        "Ticket intake for staff. Has no owners recorded.",
    ),
]
# (source, type, target, purpose, source instance, target instance)
LINKS = [
    ("Nightly Payroll Export", "depends_on", "Workday HR", "Reads approved payroll", None, "Production"),
    ("Finance Ledger", "depends_on", "Nightly Payroll Export", "Loads payroll journals from the export", None, None),
    ("Executive Dashboard", "depends_on", "Finance Ledger", "Reads actuals", None, None),
    ("Budget Planner", "depends_on", "Finance Ledger", "Reads actuals", None, None),
    ("Finance Ledger", "depends_on", "Budget Planner", "Reads budget codes", None, None),
    ("Legacy Timesheets", "depends_on", "Workday HR", "Reads employee lists", None, None),
    ("Employee Timesheets", "replaces", "Legacy Timesheets", "Takes over its users", None, None),
]
# (name, source, source instance, target, target instance, protocol, schedule, data purpose)
INTEGRATIONS = [
    (
        "Workday to Finance Ledger payroll feed",
        "Workday HR",
        "Production",
        "Finance Ledger",
        "Production",
        "SFTP",
        "Nightly 01:00",
        "Approved payroll journals",
    ),
    (
        "Workday to Zoom user provisioning",
        "Workday HR",
        None,
        "Zoom Meetings",
        None,
        "SCIM",
        "Continuous",
        "Joiners, movers and leavers",
    ),
]
# Integration fields, by integration name.
INTEGRATION_FIELDS = {
    "Workday to Finance Ledger payroll feed": {
        "direction": "one_way",
        "auth_method": "ssh_key",
        "data_classification": "restricted",
        "monitoring_url": "https://monitoring.example.test/jobs/payroll-feed",
        "data_categories": ["personal", "financial"],  # data-flow map
    },
    "Workday to Zoom user provisioning": {
        "direction": "one_way",
        "auth_method": "oauth",
        "data_classification": "confidential",
        "data_categories": ["personal"],
    },
}


class Command(BaseCommand):
    help = (
        "Create the synthetic Contoso demo organization, accounts and, unless --accounts-only, a readable dependency "
        "scenario (records, links, integrations, confirmations and two change assessments), a renewal scenario and a discovery scenario (an Entra connector "
        "on the simulated tenant, run once, with two candidates linked and one dismissed) and an imports scenario (an "
        '"IT spreadsheet" import source, tags on a few records and a shared "Finance apps" filter; the sample file '
        "to import is sample_csv/applications.csv). The scenarios are made through the service layer, so they have "
        "audit history, and each is skipped if the organization already has its data (records, renewals, a connector, "
        "an import source)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--accounts-only",
            action="store_true",
            help="Only the organization, accounts, people, teams and vendors (what the Playwright suite starts from), "
            "plus platform@example.test, platform staff with no membership, on an _e2e database only. Like the full "
            "demo, it runs only where simulators are allowed or on an _e2e database, and never resets the password "
            "of an account that already exists.",
        )

    def handle(self, *args, accounts_only=False, **options):
        # The discovery scenario runs a connector on the simulated tenant: refuse before writing anything rather than
        # leave a half-seeded demo. Every mode refuses on a server that allows no
        # simulators, unless the database is a Playwright one (`_e2e`): the demo accounts' password is published.
        if not (simulation.allowed() or e2e_database()):
            if accounts_only:
                raise CommandError(
                    "seed_demo makes accounts whose password is published, and this server doesn't allow the demo "
                    f"(DEBUG is off, {simulation.FLAG} is unset and the database isn't a Playwright _e2e one). Nothing "
                    f"was changed. On a demo or test server, set {simulation.FLAG}=1 and run it again."
                )
            raise CommandError(simulation.command_refused("seed_demo"))
        org, _ = Organization.objects.get_or_create(name="Contoso (synthetic)")
        User = get_user_model()
        for email, first, last, role in USERS:
            user, created = User.objects.get_or_create(username=email, defaults={"email": email})
            user.first_name, user.last_name, user.email = first, last, email
            if created:  # A rerun never resets a password someone has changed
                user.set_password(DEMO_PASSWORD)
            user.save()
            # A rerun never changes a membership that exists: an Admin may have changed the role, or made
            # the account change its password.
            Membership.objects.get_or_create(
                user=user, organization=org, defaults={"role": role, "must_change_password": False}
            )
            if role == Role.ADMIN and org.coordinator_id is None:
                org.coordinator = user
                org.save(update_fields=["coordinator"])
        for name, email, dept, active in PEOPLE:
            department, _ = Department.objects.get_or_create(organization=org, name=dept)
            anonymized = name.startswith("Former person #")  # anonymized people keep no department
            Person.objects.update_or_create(
                organization=org,
                name=name,
                defaults={
                    "email": email,
                    "department": None if anonymized else department,
                    "active": active,
                    "anonymized_at": timezone.now() if anonymized else None,
                },
            )
        for name, details in VENDORS.items():
            Vendor.objects.get_or_create(organization=org, name=name, defaults=details)
        for name in TEAMS:
            Team.objects.get_or_create(organization=org, name=name)
        if accounts_only:
            self.platform_staff()
        if not accounts_only:
            if Resource.objects.filter(organization=org).exists():
                self.stdout.write("The organization already has records; the dependency scenario was left alone.")
            else:
                self.scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write("Added the dependency scenario: open Dependencies and choose Workday HR.")
            if Connector.objects.filter(organization=org).exists():
                self.stdout.write("The organization already has a connector; the discovery scenario was left alone.")
            else:
                self.discovery_scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write("Added the discovery scenario: open Discovery, or Needs attention as the Editor.")
            if RenewalObligation.objects.filter(organization=org).exists():
                self.stdout.write("The organization already has renewals; the renewal scenario was left alone.")
            else:
                self.renewal_scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write(
                    "Added the renewal scenario: open Renewals, or Needs attention as the Editor or Admin."
                )
            if AIProvider.objects.filter(organization=org).exists():
                self.stdout.write("The organization already has AI providers; the AI agents scenario was left alone.")
            else:
                self.ai_scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write("Added the AI agents scenario: open AI agents, or Needs attention as the Editor.")
            if CustomField.objects.filter(organization=org).exists():
                self.stdout.write(
                    "The organization already has custom fields; the custom fields scenario was left alone."
                )
            else:
                self.custom_fields_scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write("Added custom fields: open an AI agent or Workday HR to see them.")
            if QualityException.objects.filter(organization=org).exists():
                self.stdout.write("The organization already has data-quality exceptions; that scenario was left alone.")
            else:
                made = self.quality_scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write(f"Added {made} data-quality exceptions: open Data quality, or a record's checks.")
            if Contract.objects.filter(organization=org).exists():
                self.stdout.write("The organization already has contracts; the contracts scenario was left alone.")
            else:
                made = self.contracts_scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write(f"Added {made} contracts: open Contracts, the dashboard or Reports.")
            if VendorRiskProfile.objects.filter(organization=org).exists():
                self.stdout.write("The organization already has vendor risk files; that scenario was left alone.")
            else:
                self.vendor_risk_scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write(
                    "Added vendor risk files: open Workday Inc. or Zoom, or Needs attention as the Admin."
                )
            if ImportSource.objects.filter(organization=org).exists():
                self.stdout.write("The organization already has an import source; the imports scenario was left alone.")
            else:
                self.imports_scenario(org, User.objects.get(username="editor@example.test"))
                self.stdout.write(f"Added the imports scenario: try Imports with {SAMPLE_CSV}")
        self.stdout.write(self.style.SUCCESS(f"Seeded {org.name}. Sign in as editor@example.test / {DEMO_PASSWORD}"))

    def platform_staff(self):
        """PLATFORM_STAFF with the demo password, platform staff (audited through the platform service) and in no
        organization, on an _e2e database only. Made and granted once: an existing account is left exactly as it is,
        so a rerun never re-grants staff an operator revoked, nor resets its password."""
        from catalog import platform_admin

        email, first, last = PLATFORM_STAFF
        if not e2e_database():
            self.stdout.write(f"Skipped {email}: the platform staff demo account is made on an _e2e database only.")
            return
        User = get_user_model()
        if User.objects.filter(username=email).exists():
            return
        user = User(username=email, email=email, first_name=first, last_name=last)
        user.set_password(DEMO_PASSWORD)
        user.save()
        platform_admin.grant_staff(user)

    def ai_scenario(self, org, editor):
        """AI providers and models, three agents with models, channels and owners, a review of the complete one and
        its dependencies, all through the services as the Editor would have entered them."""
        people = {p.name: str(p.id) for p in Person.objects.filter(organization=org)}
        teams = {t.name: str(t.id) for t in Team.objects.filter(organization=org)}
        departments = {d.name: str(d.id) for d in Department.objects.filter(organization=org)}
        models: dict[str, str] = {}
        for provider_name, (website, rows) in AI_PROVIDERS.items():
            provider = ai.create_provider(org, editor, {"name": provider_name, "website": website})
            for name, version, modality, region in rows:
                model = ai.create_model(
                    org,
                    editor,
                    {
                        "provider": str(provider.id),
                        "name": name,
                        "version": version,
                        "modality": modality,
                        "hosting_region": region,
                    },
                )
                models[name] = str(model.id)
        agents = {}
        for name, lifecycle, criticality, business, technical, team, used, fields in AI_AGENTS:
            data: dict[str, object] = {
                "kind": "ai_agent",
                "name": name,
                "lifecycle": lifecycle,
                "criticality": criticality,
                "business_owner": people.get(business) if business else None,
                "technical_owner": people.get(technical) if technical else None,
                "support_team": teams.get(team),
                "models_used": [models[m] for m in used],
                **fields,
            }
            if "department" in data:
                data["department"] = departments.get(str(data["department"]))
            agents[name] = services.create_resource(org, editor, data)
        services.record_review(
            org,
            editor,
            agents["HR Policy Assistant"],
            {"confirmed_by": people["Priya Raman"], "evidence": "Agent review meeting with HR"},
        )
        targets = [target for _, target in AI_DEPENDS_ON]
        records = {r.name: r for r in Resource.objects.filter(organization=org, name__in=targets)}
        for agent, target in AI_DEPENDS_ON:
            if target in records:
                services.create_relationship(
                    org,
                    editor,
                    {
                        "type": "depends_on",
                        "source": str(agents[agent].id),
                        "target": str(records[target].id),
                        "purpose": "Reads from it",
                    },
                )

    def custom_fields_scenario(self, org, editor):
        """Five custom fields defined by the Admin, with values on a few records set by the Editor."""
        admin = get_user_model().objects.get(username="admin@example.test")
        for definition in CUSTOM_FIELDS:
            customfields.create_field(org, admin, definition)
        records = {r.name: r for r in Resource.objects.filter(organization=org, name__in=CUSTOM_VALUES)}
        for name, data in CUSTOM_VALUES.items():
            if name in records:
                services.update_resource(org, editor, records[name], {"custom_field_data": data})

    def quality_scenario(self, org, editor):
        """Two reasoned "not applicable" marks (data quality): no vendor on Budget Planner and no documentation
        on Executive Dashboard, so Data quality shows an excepted check next to failing ones. Returns how many were
        made (a record the scenario didn't create, or a rule that is off, is skipped)."""
        owner = Person.objects.get(organization=org, name="Priya Raman")
        made = 0
        for rule_id, name, reason in QUALITY_EXCEPTIONS:
            record = Resource.objects.filter(organization=org, name=name).first()
            if record is None or quality.status(quality.BY_ID[rule_id], org)[0] != quality.ON:
                continue
            quality.create_exception(
                org, editor, {"record": str(record.id), "rule": rule_id, "reason": reason, "owner": str(owner.id)}
            )
            made += 1
        return made

    def vendor_risk_scenario(self, org, editor):
        """Vendor risk files through catalog/vendor_risk.py: Workday is critical with a SOC 2 Type II report
        that is expiring, a signed DPA, a current penetration test and an insurance certificate about to expire; Zoom
        is high risk and processes personal data, with an expired SOC 2 report and no DPA (two gaps); Zylker is low
        risk. Vendors the demo didn't create are skipped."""
        from catalog import vendor_risk

        today = services.org_today(org)
        vendors = {v.name: v for v in Vendor.objects.filter(organization=org)}

        def day(offset):
            return (today + timedelta(days=offset)).isoformat()

        workday, zoom, zylker = (vendors.get(n) for n in ("Workday Inc.", "Zoom", "Zylker Software"))
        if workday is not None:
            vendor_risk.update_profile(
                org,
                editor,
                workday,
                {
                    "risk_tier": "critical",
                    "processes_personal_data": True,
                    "questionnaire_status": "reviewed",
                    "questionnaire_requested_on": day(-90),
                    "questionnaire_reviewed_on": day(-60),
                    "assurance_type": "soc2_type2",
                    "assurance_issued_on": day(-300),
                    "dpa_status": "yes",
                    "dpa_signed_on": day(-700),
                    "subprocessor_url": "https://workday.example.test/trust/subprocessors",
                    "subprocessors_reviewed_on": day(-60),
                    "notes": "Annual review with Security every spring (synthetic).",
                },
            )
            vendor_risk.create_document(
                org,
                editor,
                workday,
                {"kind": "pen_test", "title": "2026 external test", "issued_on": day(-165), "validity_months": 12},
            )
            vendor_risk.create_document(
                org, editor, workday, {"kind": "insurance", "title": "Cyber liability", "expires_on": day(20)}
            )
        if zoom is not None:
            vendor_risk.update_profile(
                org,
                editor,
                zoom,
                {"risk_tier": "high", "processes_personal_data": True, "questionnaire_status": "requested"},
            )
            vendor_risk.create_document(
                org,
                editor,
                zoom,
                {"kind": "soc2", "title": "SOC 2 Type II", "issued_on": day(-375), "validity_months": 12},
            )
        if zylker is not None:
            vendor_risk.update_profile(
                org, editor, zylker, {"risk_tier": "low", "questionnaire_status": "not_required"}
            )

    def contracts_scenario(self, org, editor):
        """A few contracts with lines and two licence pools, through the contract services, so the dashboard,
        the spend report and the renewals calendar have data on a fresh demo. A record the scenario didn't create is
        left off its line. Returns how many contracts were made."""
        today = services.org_today(org)
        vendors = {v.name: str(v.id) for v in Vendor.objects.filter(organization=org)}
        records = {r.name: r for r in Resource.objects.filter(organization=org, kind__in=contracts.ITEM_KINDS)}
        owner = Person.objects.filter(organization=org, name="Lee Chen").first()
        made: dict[str, Contract] = {}
        for name, vendor, amount, currency, billing, start, end, auto, notice, status, cost, lines, link in CONTRACTS:
            data = {
                "name": name,
                "vendor": vendors.get(vendor) if vendor else None,
                "amount": amount,
                "currency": currency,
                "billing_frequency": billing,
                "start_date": (today + timedelta(days=start)).isoformat(),
                "end_date": (today + timedelta(days=end)).isoformat(),
                "auto_renew": auto,
                "status": status,
                "cost_center": cost,
                "renewal_owner": str(owner.id) if owner else None,
            }
            if notice:
                data["notice_value"], data["notice_unit"] = notice
            contract = contracts.create_contract(org, editor, data)
            wanted = [(records[r], Decimal(p) if p else None) for r, p in lines if r in records]
            if wanted:
                contracts.set_items(org, editor, contract, wanted)
            if link:
                contracts.update_contract(org, editor, contract, {"create_renewal_obligation": True})
            made[name] = contract
        for app, contract_name, sku, sku_id, bought, used in POOLS:
            if app in records:
                contracts.create_pool(
                    org,
                    editor,
                    {
                        "application": str(records[app].id),
                        "contract": str(made[contract_name].id),
                        "sku_name": sku,
                        "sku_id": sku_id,
                        "seats_purchased": bought,
                        "seats_assigned": used,
                    },
                )
        return len(made)

    def imports_scenario(self, org, editor):
        """An application import source, tags on a few records and a shared filter on one of them. No import
        is run: the demo user uploads sample_csv/applications.csv, which has new applications with external keys, an
        owner and a team that don't exist yet, a possible match for Budget Planner and one row that is an error."""
        admin = get_user_model().objects.get(username="admin@example.test")
        imports.create_source(org, admin, {"name": "IT spreadsheet", "kind": "application"})
        records = {r.name: r for r in Resource.objects.filter(organization=org, name__in=TAGS)}
        for name, names in TAGS.items():
            if name in records:
                services.update_resource(org, editor, records[name], {"tags": names})
        for tag in Tag.objects.filter(organization=org, name__in=TAG_COLORS):
            color, description = TAG_COLORS[tag.name]
            admin_services.update_tag(org, editor, tag, {"color": color, "description": description})
        saved_filters.create(
            org, editor, {"kind": "application", "name": "Finance apps", "shared": True, "query": {"tag": "Finance"}}
        )

    def discovery_scenario(self, org, editor):
        """An Entra connector on the simulated tenant, run once, with three decisions made through the
        services: Workday and Zoom linked to their records and a developer's test registration dismissed. The rest
        stay as candidates, so Discovery and Needs attention have something to show."""
        admin = get_user_model().objects.get(username="admin@example.test")  # connectors are an Admin's
        connector = discovery.create_connector(
            org,
            admin,
            {
                "name": "Microsoft Entra ID",
                "tenant_id": "simulated",
                "secret": "synthetic-demo-client-secret",  # the simulated tenant never uses it
                "reviewer": editor.id,
            },
        )
        connections.run_test(org, admin, connector.microsoft_connection)  # a run needs a tested connection
        discovery.run(connector, requested_by=admin)
        records = {r.name: r for r in Resource.objects.filter(organization=org, kind="application")}
        observations = {o.display_name: o for o in Observation.objects.filter(connector=connector)}
        for source_name, record in (("Workday", "Workday HR"), ("Zoom", "Zoom Meetings")):
            discovery.link(org, editor, observations[source_name], {"resource": str(records[record].id)})
        discovery.dismiss(
            org, editor, observations["Graph Explorer Test"], {"reason": "A developer's test registration"}
        )

    def renewal_scenario(self, org, editor):
        """Renewals for the renewal screens: one per deadline label (Workday's March 31 renewal with a two-month
        notice shows the month-end rule), an overdue action, an entered deadline without a renewal date, one with
        nothing to work from, Unknown auto-renewal, all covered applications retired, a decision without completion,
        and a renewal with two completed cycles in its history. The coordinator gets the unassigned ones."""
        people = {p.name: str(p.id) for p in Person.objects.filter(organization=org)}
        vendors = {v.name: str(v.id) for v in Vendor.objects.filter(organization=org)}
        apps = {r.name: str(r.id) for r in Resource.objects.filter(organization=org, kind="application")}
        admin = org.coordinator
        today = services.org_today(org)

        def day(offset):
            return (today + timedelta(days=offset)).isoformat()

        def covers(*names):
            return [apps[n] for n in names if n in apps]

        def make(name, **data):
            return renewals.create_renewal(org, editor, {"name": name, **data})

        def complete(obligation, decision, decided, completed, evidence):
            cycle = obligation.cycles.get(completed_on__isnull=True)
            renewals.record_decision(org, editor, obligation, cycle.id, {"decision": decision, "decided_on": decided})
            renewals.record_completion(
                org, editor, obligation, cycle.id, {"completed_on": completed, "completion_evidence": evidence}
            )

        # Two completed cycles, then the current one: renewal date March 31, notice two months, so the action deadline
        # is January 31 (Calculated). The next renewal date comes from the one-year term.
        march = today.year + (1 if (today.month, today.day) > (1, 31) else 0)
        workday = make(
            "Workday subscription",
            vendor=vendors.get("Workday Inc."),
            responsible=people["Priya Raman"],
            followup=editor.id,
            auto_renew="yes",
            renewal_date=f"{march - 2}-03-31",
            notice_value=2,
            notice_unit="months",
            term_value=1,
            term_unit="years",
            applications=covers("Workday HR", "Employee Timesheets"),
        )
        complete(workday, "renew", f"{march - 2}-01-10", f"{march - 2}-01-20", "PO 39012 signed")
        renewals.establish_next_cycle(org, editor, workday, {})
        complete(workday, "renew", f"{march - 1}-01-12", f"{march - 1}-01-22", "PO 41877 signed")
        renewals.establish_next_cycle(org, editor, workday, {})
        make(
            "Zoom Pro",
            vendor=vendors.get("Zoom"),
            responsible=people["Lee Chen"],
            followup=editor.id,
            auto_renew="yes",
            renewal_date=day(27),
            explicit_deadline=day(-3),
            applications=covers("Zoom Meetings"),
        )
        make(
            "ServiceDesk Plus",
            vendor=vendors.get("Zylker Software"),
            auto_renew="unknown",
            renewal_date=day(130),
            applications=covers("Service Desk Portal"),
        )
        make(
            "Budget Planner license",
            responsible=people["Dana Ortiz"],
            followup=admin.id if admin else None,
            auto_renew="no",
            renewal_date=day(180),
            notice_value=30,
            notice_unit="days",
            applications=covers("Budget Planner"),
        )
        make("Adobe Sign", responsible=people["Lee Chen"], followup=editor.id, auto_renew="yes")
        make(
            "Legacy Timeclock maintenance",
            responsible=people["Dana Ortiz"],
            followup=editor.id,
            auto_renew="yes",
            renewal_date=day(200),
            notice_value=30,
            notice_unit="days",
            applications=covers("Legacy Timesheets"),
        )
        ledger = make(
            "Finance Ledger support",
            responsible=people["Lee Chen"],
            followup=editor.id,
            auto_renew="no",
            explicit_deadline=day(-10),
            renewal_date=day(20),
            applications=covers("Finance Ledger"),
        )
        renewals.record_decision(
            org, editor, ledger, ledger.cycles.get().id, {"decision": "renew", "decision_notes": "Finance wants it."}
        )
        make(
            "Payroll connector license",
            responsible=people["Priya Raman"],
            auto_renew="yes",
            explicit_deadline=day(20),
            applications=covers("Workday HR"),
        )

    def scenario(self, org, editor):
        """Records, links, integrations, confirmations and assessments, entered as the Editor would have."""
        people = {p.name: p for p in Person.objects.filter(organization=org)}
        teams = {t.name: t for t in Team.objects.filter(organization=org)}
        vendors = {v.name: v for v in Vendor.objects.filter(organization=org)}
        categories = {c.name: c for c in SoftwareCategory.objects.filter(organization=org)}
        departments = {d.name: d for d in Department.objects.filter(organization=org)}
        departments.setdefault(
            "IT Applications", Department.objects.get_or_create(organization=org, name="IT Applications")[0]
        )
        today = services.org_today(org)
        admin = get_user_model().objects.get(username="admin@example.test")
        for team, names in TEAM_MEMBERS.items():
            admin_services.update_named(Team, org, editor, teams[team], {"members": [str(people[n].id) for n in names]})
        workday = vendors["Workday Inc."]
        if workday.account_manager_id is None:
            admin_services.update_named(Vendor, org, admin, workday, {"account_manager": str(people["Lee Chen"].id)})

        def pid(name):
            return str(people[name].id) if name else None

        by_name: dict[str, Resource] = {}
        for name, kind, lifecycle, criticality, business, technical, team, vendor, purpose in RECORDS:
            data = {
                "kind": kind,
                "name": name,
                "lifecycle": lifecycle,
                "criticality": criticality,
                "business_owner": pid(business),
                "technical_owner": pid(technical),
                "support_team": str(teams[team].id),
                "purpose": purpose,
            }
            if kind == "application":
                data["vendor"] = str(vendors[vendor].id) if vendor else None
                data["documentation_url"] = f"https://docs.example.test/{name.lower().replace(' ', '-')}"
            else:
                data["host_runtime"] = "Windows Server scheduled task"
                data["component_type"] = "scheduled_job"
                data["repository_url"] = "https://git.example.test/contoso/nightly-payroll-export"
            extra: dict[str, object] = dict(RECORD_FIELDS.get(name, {}))
            if "category" in extra:
                extra["category"] = str(categories[str(extra["category"])].id)
            if "department" in extra:
                extra["department"] = str(departments[str(extra["department"])].id)
            by_name[name] = services.create_resource(org, editor, {**data, **extra})
        instances = {}
        for app, names in {"Workday HR": ["Production", "Sandbox"], "Finance Ledger": ["Production"]}.items():
            for name in names:
                instances[app, name] = services.create_instance(
                    org, editor, by_name[app], {"name": name, "environment": name, "status": "operational"}
                )

        def instance_id(app, name):
            return str(instances[app, name].id) if name else None

        links = {}
        for source, rel_type, target, purpose, source_instance, target_instance in LINKS:
            links[source, target] = services.create_relationship(
                org,
                editor,
                {
                    "type": rel_type,
                    "source": str(by_name[source].id),
                    "target": str(by_name[target].id),
                    "purpose": purpose,
                    "source_instance": instance_id(source, source_instance),
                    "target_instance": instance_id(target, target_instance),
                },
            )
        feeds = {}
        for name, source, source_instance, target, target_instance, protocol, schedule, purpose in INTEGRATIONS:
            feeds[name] = services.create_integration(
                org,
                editor,
                {
                    "name": name,
                    "lifecycle": "active",
                    "technical_owner": pid("Dana Ortiz"),
                    "support_team": str(teams["IT Integrations"].id),
                    "source": str(by_name[source].id),
                    "source_instance": instance_id(source, source_instance),
                    "target": str(by_name[target].id),
                    "target_instance": instance_id(target, target_instance),
                    "protocol": protocol,
                    "schedule": schedule,
                    "data_purpose": purpose,
                    **INTEGRATION_FIELDS.get(name, {}),
                },
            )
        # The export job implements the payroll feed; nothing implements the Zoom one (it is vendor-native).
        services.create_relationship(
            org,
            editor,
            {
                "type": "implements",
                "source": str(by_name["Nightly Payroll Export"].id),
                "target": str(feeds["Workday to Finance Ledger payroll feed"].id),
                "purpose": "Runs the nightly transfer",
            },
        )

        # Record reviews (a separate clock from link confirmations), then link confirmations in each state.
        dana = pid("Dana Ortiz")
        for name, days in (("Workday HR", 20), ("Finance Ledger", 35), ("Zoom Meetings", 10)):
            services.record_review(
                org,
                editor,
                by_name[name],
                {
                    "confirmed_by": dana,
                    "evidence": "Checked with the owner in the quarterly review.",
                    "confirmation_date": (today - timedelta(days=days)).isoformat(),
                },
            )

        def confirm(link, days, evidence):
            services.confirm_link(
                org,
                editor,
                link,
                {
                    "confirmed_by": dana,
                    "evidence": evidence,
                    "confirmed_on": (today - timedelta(days=days)).isoformat(),
                },
            )

        confirm(links["Nightly Payroll Export", "Workday HR"], 12, "Confirmed against the job's configuration.")
        confirm(links["Executive Dashboard", "Finance Ledger"], 5, "Confirmed by the dashboard owner.")
        confirm(
            links["Finance Ledger", "Nightly Payroll Export"],
            org.review_interval_days + 30,
            "Confirmed at the last close; it is now past the interval.",
        )
        confirm(feeds["Workday to Finance Ledger payroll feed"].integration, 3, "Checked the SFTP schedule.")

        # An upgrade assessment, then a change to the export job's technical owner: that flags the assessment stale
        # (the job is in its snapshot) and makes the job's link to Workday HR need verification.
        assessments.create_assessment(
            org,
            editor,
            {
                "target": str(by_name["Workday HR"].id),
                "change_type": "change",
                "title": "Workday 2027R1 upgrade",
                "description": "Vendor release upgrade in the sandbox first, then production on a weekend.",
                "status": "in_review",
                "target_date": (today + timedelta(days=45)).isoformat(),
                "ticket_url": "https://tickets.example.test/CHG-1042",
            },
        )
        services.update_resource(org, editor, by_name["Nightly Payroll Export"], {"technical_owner": pid("Lee Chen")})
        # A retirement assessment made after that change, so its snapshot is current.
        assessments.create_assessment(
            org,
            editor,
            {
                "target": str(by_name["Budget Planner"].id),
                "change_type": "retirement",
                "title": "Retire Budget Planner",
                "description": "Budgets move into the ledger module; check what still reads from the planner.",
                "status": "draft",
            },
        )
