"""Create a synthetic organization at scale (1,000 resources by default) for timing checks. Fictional data only."""

import random
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.text import slugify

from catalog import services
from catalog.models import (
    AgentChannel,
    AgentModel,
    AgentPlatform,
    AgentRisk,
    AIAgent,
    AIModel,
    AIProvider,
    Application,
    ApplicationInstance,
    Attachment,
    BuiltBy,
    ChangeAssessment,
    Component,
    ComponentType,
    Criticality,
    Department,
    InstanceStatus,
    Integration,
    Kind,
    LicenseModel,
    Lifecycle,
    LinkConfirmation,
    ModelModality,
    Person,
    QualityException,
    RecordReview,
    Relationship,
    RelationshipKind,
    RelationshipType,
    Resource,
    Team,
    Vendor,
)
from catalog.state import confirmed_snapshot, integration_signature, relationship_signature
from core import connections, dates, simulation
from core.models import AuditEvent, Membership, MicrosoftConnection, Organization, Role
from discovery import engine as discovery
from discovery.models import Connector, Observation, ReconciliationDecision, RunStatus
from money.models import RenewalCycle, RenewalCycleApplication, RenewalObligation
from notify import rule_services
from notify.models import Digest, DigestRun, NotificationPreference, NotificationRule, NotificationSnooze

ORG_NAME = "Scale Test (synthetic)"
EMAIL_DOMAIN = "scale.example.test"
PASSWORD = "softwarden-scale-2026"  # local synthetic accounts only
ADMIN_EMAIL = f"admin@{EMAIL_DOMAIN}"
EDITOR_EMAIL = f"editor01@{EMAIL_DOMAIN}"

FIRST = [
    "Avery",
    "Blake",
    "Casey",
    "Drew",
    "Emery",
    "Finley",
    "Gray",
    "Harper",
    "Indigo",
    "Jules",
    "Kai",
    "Logan",
    "Morgan",
    "Nico",
    "Oakley",
    "Parker",
    "Quinn",
    "Reese",
    "Sage",
    "Tatum",
]
LAST = [
    "Testwell",
    "Samplewood",
    "Mockridge",
    "Faux",
    "Placeholder",
    "Dummyfield",
    "Synth",
    "Notreal",
    "Examplar",
    "Stubbs",
]
COMPANIES = [
    "Zylker",
    "Contoso",
    "Fabrikam",
    "Northwind",
    "Tailspin",
    "Wingtip",
    "Adatum",
    "Litware",
    "Proseware",
    "Lucerne",
    "Woodgrove",
    "Alpine",
]
APP_NOUNS = [
    "Payroll",
    "Ledger",
    "Helpdesk",
    "Timesheets",
    "Expenses",
    "Recruiting",
    "Onboarding",
    "Inventory",
    "Procurement",
    "Contracts",
    "CRM",
    "Billing",
    "Scheduler",
    "Learning Portal",
    "Asset Tracker",
    "Booking",
    "Directory",
    "Forms",
    "Survey Hub",
    "Compliance Desk",
    "Fleet Manager",
    "Document Vault",
    "Intranet",
    "Wiki",
    "Chat",
    "Meetings",
    "Badge Access",
    "Visitor Log",
    "Telemetry",
    "Budgeting",
]
COMPONENT_NOUNS = [
    "Nightly Export",
    "Invoice Sync",
    "Badge Import",
    "User Cleanup",
    "Report Mailer",
    "Log Shipper",
    "License Audit",
    "Backup Check",
    "Address Lookup",
    "Token Broker",
    "Queue Relay",
    "Cache Warmer",
    "Directory Sync",
    "Ticket Router",
    "Metrics Rollup",
    "Config Linter",
    "Cert Watcher",
    "Data Masker",
    "Alert Bridge",
    "Form Webhook",
]
DEPARTMENTS = ["IT Applications", "IT Integrations", "Service Desk", "HR", "Finance", "Facilities", "Legal", "Sales"]
TEAMS = [
    "Applications",
    "Integrations",
    "Service Desk",
    "Infrastructure",
    "Data",
    "Security",
    "Finance Systems",
    "People Systems",
    "Facilities IT",
    "Sales Ops",
    "Platform",
    "Support",
]
ENVIRONMENTS = ["Production", "Staging", "Test", "Sandbox", "Development", "Training"]
RUNTIMES = ["Windows Server task", "Linux cron", "Azure Functions", "AWS Lambda", "Kubernetes CronJob"]
BATCH = 500


def refresh_statistics() -> None:
    """ANALYZE every table of this application, so the planner sees the rows just bulk-inserted. Without it, a timing
    run started within a minute of seeding (before autovacuum's analyze) plans the new organization's queries from
    statistics that predate it: the dashboard took 557 ms instead of 121 at this size (second perf fix).
    It changes the planner's statistics only, never data."""
    tables = connection.introspection.django_table_names(only_existing=True, include_views=False)
    if tables:
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE " + ", ".join(connection.ops.quote_name(t) for t in sorted(tables)))


def pick(rng, weighted):
    """weighted: list of (value, weight)."""
    return rng.choices([v for v, _ in weighted], weights=[w for _, w in weighted])[0]


class Command(BaseCommand):
    help = (
        "Create a synthetic organization at NFR scale (1,000 resources, 2,000 instances, 5,000 reviews, 2,000 "
        "relationships, 300 integrations, 60 AI agents on 12 models, 50 users and people, and an Entra connector on the simulated tenant whose "
        "one run yields about 2,000 observations), for 'nfr_timing'. Fictional data only. For speed this bulk-inserts rows and BYPASSES the "
        "service layer, so it writes no per-row audit events; it adds 50,000 synthetic ones instead. Refuses to run if the organization already has "
        "resources unless --reset is passed. The random seed is fixed so every run builds the same shape."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scale", type=float, default=1.0, help="Multiplier on the NFR counts (default 1.0; tests use 0.05)."
        )
        parser.add_argument(
            "--reset", action="store_true", help="Delete the organization's existing seeded data first."
        )
        parser.add_argument("--seed", type=int, default=20261101, help="Random seed (default 20261101).")

    def handle(self, *args, scale, reset, seed, **options):
        if scale <= 0:
            raise CommandError("--scale must be greater than zero.")
        if not simulation.allowed():
            raise CommandError(simulation.command_refused("seed_scale"))
        org, _ = Organization.objects.get_or_create(name=ORG_NAME)
        if Resource.objects.filter(organization=org).exists():
            if not reset:
                raise CommandError(f"{org.name} already has resources. Pass --reset to delete and rebuild them.")
        rng = random.Random(seed)
        with transaction.atomic():
            if reset:
                self.reset(org)
            counts = self.build(org, rng, scale)
        refresh_statistics()
        self.summary(org, counts)

    # ---- reset ---------------------------------------------------------------

    def reset(self, org):
        ChangeAssessment.objects.filter(organization=org).delete()  # nfr_timing writes some; they PROTECT their records
        for notification_model in (Digest, DigestRun, NotificationSnooze, NotificationPreference):
            notification_model.objects.filter(organization=org).delete()
        # Rules may name teams, people and vendors (PROTECT): start again from the built-ins.
        NotificationRule.objects.filter(organization=org).delete()
        rule_services.seed(org)
        ReconciliationDecision.objects.filter(organization=org).delete()  # before the records they link to
        Connector.objects.filter(organization=org).delete()  # runs and observations cascade
        MicrosoftConnection.objects.filter(organization=org).delete()  # after the connectors that PROTECT them
        RenewalObligation.objects.filter(organization=org).delete()  # cycles, coverage and reminders cascade
        RecordReview.objects.filter(organization=org).delete()
        LinkConfirmation.objects.filter(organization=org).delete()
        Relationship.objects.filter(organization=org).delete()
        Integration.objects.filter(organization=org).delete()  # the ends are PROTECTed; the rows go first
        ApplicationInstance.objects.filter(organization=org).delete()
        Attachment.objects.filter(organization=org).delete()  # none are seeded; any stored file is swept as an orphan
        QualityException.objects.filter(organization=org).delete()  # they PROTECT their records
        Resource.objects.filter(organization=org).delete()  # extension rows (and agents' model links) cascade
        AIModel.objects.filter(organization=org).delete()
        AIProvider.objects.filter(organization=org).delete()
        for model in (Vendor, Team, Person, Department):  # a vendor names its account manager, a person a department
            model.objects.filter(organization=org).delete()
        AuditEvent.objects.filter(organization=org).purge()  # append-only: the one removal path (core.models)
        Membership.objects.filter(organization=org).delete()
        get_user_model().objects.filter(username__endswith="@" + EMAIL_DOMAIN).delete()

    # ---- build ---------------------------------------------------------------

    def build(self, org, rng, scale):
        n = lambda base, low=1: max(low, round(base * scale))  # noqa: E731
        n_resources, n_instances, n_reviews = n(1000), round(2000 * scale), round(5000 * scale)
        user_model = get_user_model()

        # users (hash the shared synthetic password once) and memberships
        n_users = n(50, 3)
        n_editors = min(max(1, n_users // 5), 12, n_users - 2)
        hashed = make_password(PASSWORD)
        emails = [ADMIN_EMAIL] + [f"editor{i:02d}@{EMAIL_DOMAIN}" for i in range(1, n_editors + 1)]
        emails += [f"viewer{i:02d}@{EMAIL_DOMAIN}" for i in range(1, n_users - len(emails) + 1)]
        roles = [Role.ADMIN] + [Role.EDITOR] * n_editors + [Role.VIEWER] * (n_users - n_editors - 1)
        users = user_model.objects.bulk_create(
            [
                user_model(
                    username=e, email=e, first_name=e.split("@")[0].title(), last_name="(synthetic)", password=hashed
                )
                for e in emails
            ]
        )
        Membership.objects.bulk_create(
            [
                Membership(user=u, organization=org, role=r, must_change_password=False)
                for u, r in zip(users, roles, strict=True)
            ]
        )
        org.coordinator = users[0]
        org.save(update_fields=["coordinator"])
        writers = [u for u, r in zip(users, roles, strict=True) if r != Role.VIEWER]

        # reference data
        departments = Department.objects.bulk_create(
            [
                Department(id=self.uid(rng), organization=org, name=name)
                for name in DEPARTMENTS[: n(len(DEPARTMENTS), 3)]
            ]
        )
        teams = Team.objects.bulk_create(
            [Team(id=self.uid(rng), organization=org, name=name) for name in TEAMS[: n(len(TEAMS), 3)]]
        )
        vendors = Vendor.objects.bulk_create(
            [
                Vendor(
                    id=self.uid(rng),
                    organization=org,
                    name=f"{COMPANIES[i % len(COMPANIES)]} Software {i + 1:02d}",
                    website=f"https://vendor{i + 1:02d}.example.com",
                )
                for i in range(n(40, 3))
            ]
        )
        people = []
        for i in range(n_users):
            first, last = FIRST[i % len(FIRST)], LAST[(i // len(FIRST)) % len(LAST)]
            people.append(
                Person(
                    id=self.uid(rng),
                    organization=org,
                    name=f"{first} {last}",
                    email=f"{first}.{last}.{i + 1:02d}@example.test".lower(),
                    department=rng.choice(departments),
                    active=rng.random() > 0.08,
                )
            )
        people = Person.objects.bulk_create(people)
        active_people = [p for p in people if p.active]

        # resources
        today = services.org_today(org)
        n_apps = round(n_resources * 0.6)
        resources: list = []
        taken: set[str] = set()
        for i in range(n_resources):
            kind = Kind.APPLICATION if i < n_apps else Kind.COMPONENT
            name = self.unique_name(rng, kind, taken)
            resources.append(
                Resource(
                    id=self.uid(rng),
                    organization=org,
                    kind=kind,
                    name=name,
                    slug=slugify(name),
                    description=f"Synthetic {kind} record for scale testing.",
                    lifecycle=pick(
                        rng,
                        [
                            (Lifecycle.ACTIVE, 62),
                            (Lifecycle.DEPRECATED, 10),
                            (Lifecycle.EVALUATING, 8),
                            (Lifecycle.PROPOSED, 5),
                            (Lifecycle.RETIRED, 15),
                        ],
                    ),
                    criticality="" if rng.random() < 0.08 else rng.choice(Criticality.values),
                    business_owner=None if rng.random() < 0.10 else rng.choice(people),
                    technical_owner=None if rng.random() < 0.12 else rng.choice(people),
                    support_team=None if rng.random() < 0.10 else rng.choice(teams),
                    it_steward=None if rng.random() < 0.15 else rng.choice(writers),
                    archived_at=None,
                )
            )
        archived = rng.sample(resources, round(n_resources * 0.08))
        for r in archived:
            r.archived_at = timezone.now() - timedelta(days=rng.randint(1, 200))
        Resource.objects.bulk_create(resources, batch_size=BATCH)
        Application.objects.bulk_create(
            [
                Application(
                    resource=r,
                    organization=org,
                    purpose=f"Synthetic purpose for {r.name}.",
                    vendor=None if rng.random() < 0.2 else rng.choice(vendors),
                    built_by=rng.choice(BuiltBy.values),
                    license_model=rng.choice(LicenseModel.values),
                    documentation_url=f"https://docs.example.com/{r.slug}",
                    support_url=f"https://support.example.com/{r.slug}",
                )
                for r in resources
                if r.kind == Kind.APPLICATION
            ],
            batch_size=BATCH,
        )
        Component.objects.bulk_create(
            [
                Component(
                    resource=r,
                    organization=org,
                    component_type=rng.choice(ComponentType.values),
                    purpose=f"Synthetic purpose for {r.name}.",
                    host_runtime=rng.choice(RUNTIMES),
                    repository_url=f"https://git.example.com/scale/{r.slug}",
                    documentation_url=f"https://docs.example.com/{r.slug}",
                    built_by=rng.choice(BuiltBy.values),
                    license_model=rng.choice(LicenseModel.values),
                )
                for r in resources
                if r.kind == Kind.COMPONENT
            ],
            batch_size=BATCH,
        )

        # instances: spread round-robin over applications, one distinct environment name per position
        apps = [r for r in resources if r.kind == Kind.APPLICATION]
        instances = []
        for i in range(n_instances if apps else 0):
            app, env = apps[i % len(apps)], ENVIRONMENTS[(i // len(apps)) % len(ENVIRONMENTS)]
            instances.append(
                ApplicationInstance(
                    id=self.uid(rng),
                    organization=org,
                    application=app,
                    name=env,
                    environment=env,
                    url=f"https://{app.slug}-{env.lower()}.example.com",
                    status=pick(
                        rng,
                        [
                            (InstanceStatus.OPERATIONAL, 80),
                            (InstanceStatus.DEGRADED, 8),
                            (InstanceStatus.OFFLINE, 4),
                            (InstanceStatus.UNKNOWN, 8),
                        ],
                    ),
                )
            )
        ApplicationInstance.objects.bulk_create(instances, batch_size=BATCH)

        # reviews: ~80% of resources are reviewed; the rest stay unreviewed
        reviewed = rng.sample(resources, min(round(n_resources * 0.8), n_reviews))
        per_resource = {r.id: 1 for r in reviewed}
        for _ in range(max(0, n_reviews - len(reviewed))):
            per_resource[rng.choice(reviewed).id] += 1
        interval = org.review_interval_days
        reviews = []
        for r in reviewed:
            snapshot = confirmed_snapshot(r)
            outcome = pick(rng, [("current", 55), ("overdue", 25), ("changed", 20)])
            if outcome == "current":
                confirmed = today - timedelta(days=rng.randint(1, interval - 10))
            else:
                confirmed = today - timedelta(days=rng.randint(interval + 10, 300))
            for k in range(per_resource[r.id]):
                values = dict(snapshot)
                if k == 0 and outcome == "changed":
                    values["business_owner"] = str(rng.choice(people).id) if values["business_owner"] is None else None
                elif k > 0:
                    values["lifecycle"] = rng.choice(Lifecycle.values)
                reviews.append(
                    RecordReview(
                        id=self.uid(rng),
                        organization=org,
                        resource=r,
                        confirmed_by=rng.choice(active_people or people),
                        recorded_by=rng.choice(writers),
                        confirmation_date=confirmed,
                        next_review_date=confirmed + timedelta(days=interval),
                        evidence=f"Synthetic confirmation {k + 1} by email thread #{rng.randint(1000, 9999)}",
                        confirmed_values=values,
                    )
                )
                confirmed -= timedelta(days=interval + rng.randint(0, 20))  # older history, strictly earlier
        RecordReview.objects.bulk_create(reviews, batch_size=BATCH)
        links = self.build_links(org, rng, scale, resources, apps, instances, people, writers, today)
        discovered = self.build_discovery(org, scale, writers[0])
        renewal_counts = self.build_renewals(org, rng, scale, apps, active_people, vendors, writers, today)
        audit_events = self.build_audit(org, rng, scale, resources, instances, reviews, users, writers, people)
        # AI agents, last and from their own random stream, so everything above keeps its seeded shape.
        agent_counts = self.build_agents(org, random.Random(rng.getrandbits(32)), scale, people, teams, writers)
        # Data quality: a few exceptions, from their own random stream too.
        exceptions = self.build_quality_exceptions(org, random.Random(rng.getrandbits(32)), resources, people, today)
        return {
            "users": len(users),
            "people": len(people),
            "departments": len(departments),
            "teams": len(teams),
            "vendors": len(vendors),
            "applications": n_apps,
            "components": n_resources - n_apps,
            "archived": len(archived),
            "instances": len(instances),
            "reviews": len(reviews),
            "reviewed resources": len(reviewed),
            **links,
            **discovered,
            **renewal_counts,
            "audit events": audit_events,
            **agent_counts,
            "data-quality exceptions": exceptions,
        }

    def build_quality_exceptions(self, org, rng, resources, people, today):
        """About 4% of the applications and components marked "No documentation" not applicable, and 2% of the
        applications "No vendor" (data quality), with expiries spread over the year and a few already past,
        so the scale timing pays for exception lookups as a real organization would."""
        active = [p for p in people if p.active] or people
        rows = []
        for rule, kinds, share in (
            ("documentation", (Kind.APPLICATION, Kind.COMPONENT), 0.04),
            ("vendor", (Kind.APPLICATION,), 0.02),
        ):
            pool = [r for r in resources if r.kind in kinds]
            for r in rng.sample(pool, max(1, round(len(pool) * share))):
                rows.append(
                    QualityException(
                        id=self.uid(rng),
                        organization=org,
                        resource=r,
                        rule=rule,
                        reason=f"Synthetic: checked with the owner in ticket #{rng.randint(1000, 9999)}.",
                        owner=rng.choice(active),
                        expires_on=today + timedelta(days=rng.randint(-30, 365)),
                    )
                )
        QualityException.objects.bulk_create(rows, batch_size=BATCH)
        return len(rows)

    def build_agents(self, org, rng, scale, people, teams, writers):
        """AI agents: 60 at full scale on 12 models from 4 providers, one to three models each, with a share
        of missing owners, scopes and human-in-loop decisions so the AI agent gaps queue has rows. No reviews."""
        providers = AIProvider.objects.bulk_create(
            [
                AIProvider(id=self.uid(rng), organization=org, name=name, website=f"https://{slug}.example.com")
                for name, slug in (
                    ("Fabrikam AI", "fabrikam-ai"),
                    ("Contoso Models", "contoso-models"),
                    ("Northwind Labs", "northwind-labs"),
                    ("Litware Cloud", "litware-cloud"),
                )
            ]
        )
        models = AIModel.objects.bulk_create(
            [
                AIModel(
                    id=self.uid(rng),
                    organization=org,
                    provider=providers[i % len(providers)],
                    name=f"Synthetic model {i + 1:02d}",
                    version=f"{rng.randint(1, 4)}.{rng.randint(0, 9)}",
                    modality=rng.choice(ModelModality.values),
                    hosting_region=rng.choice(["EU", "US", "Sweden Central", "West Europe", ""]),
                )
                for i in range(12)
            ]
        )
        n_agents = max(3, round(60 * scale))
        agents, rows, links = [], [], []
        for i in range(n_agents):
            name = f"{rng.choice(COMPANIES)} assistant {i + 1:03d}"
            resource = Resource(
                id=self.uid(rng),
                organization=org,
                kind=Kind.AI_AGENT,
                name=name,
                slug=slugify(name),
                description="Synthetic AI agent for scale testing.",
                lifecycle=pick(rng, [(Lifecycle.ACTIVE, 70), (Lifecycle.EVALUATING, 15), (Lifecycle.RETIRED, 15)]),
                criticality=rng.choice(Criticality.values),
                business_owner=None if rng.random() < 0.15 else rng.choice(people),
                technical_owner=None if rng.random() < 0.15 else rng.choice(people),
                support_team=None if rng.random() < 0.2 else rng.choice(teams),
                it_steward=None if rng.random() < 0.15 else rng.choice(writers),
            )
            agents.append(resource)
            rows.append(
                AIAgent(
                    resource=resource,
                    organization=org,
                    purpose=f"Synthetic purpose for {name}.",
                    platform=rng.choice(AgentPlatform.values),
                    channels=sorted(rng.sample(AgentChannel.values, rng.randint(1, 3)), key=AgentChannel.values.index),
                    data_access_scope="" if rng.random() < 0.2 else "Synthetic scope: one SharePoint site.",
                    human_in_loop=None if rng.random() < 0.25 else rng.random() < 0.6,
                    actions_can_write=rng.random() < 0.3,
                    risk_classification=rng.choice(AgentRisk.values),
                )
            )
            links += [
                AgentModel(id=self.uid(rng), organization=org, agent_id=resource.id, model=m)
                for m in rng.sample(models, rng.randint(1, 3))
            ]
        Resource.objects.bulk_create(agents, batch_size=BATCH)
        AIAgent.objects.bulk_create(rows, batch_size=BATCH)
        AgentModel.objects.bulk_create(links, batch_size=BATCH)
        return {"AI providers": len(providers), "AI models": len(models), "AI agents": len(agents)}

    def build_discovery(self, org, scale, reviewer):
        """Discovery size: one Entra connector on the simulated tenant, run once, which yields about 2,000 observations
        (the simulated tenant's own apps plus `simulated_size` generated ones, about 10% of which the default scope filters out), all left as candidates."""
        connector = discovery.create_connector(
            org,
            reviewer,
            {
                "name": "Microsoft Entra ID (scale)",
                "tenant_id": "simulated",
                "secret": "synthetic-scale-client-secret",  # the simulated tenant never uses it
                "reviewer": reviewer.id,
            },
        )
        connector.config = {**connector.config, "simulated_size": round(2200 * scale)}
        connector.save(update_fields=["config"])
        connections.run_test(org, reviewer, connector.microsoft_connection)  # a run needs a tested connection
        run_ = discovery.run(connector, requested_by=reviewer)
        if run_ is None or run_.status != RunStatus.SUCCEEDED:
            raise CommandError(f"The simulated discovery run failed: {run_.error if run_ else 'another run active'}")
        return {"connectors": 1, "observations": Observation.objects.filter(connector=connector).count()}

    def build_renewals(self, org, rng, scale, apps, people, vendors, writers, today):
        """Renewals size: 300 obligations and about 1,000 cycles (history included), each covering one to three
        applications, with every deadline basis and a share of gaps, overdue actions and decisions."""
        n_obligations = max(1, round(300 * scale))
        obligations, cycles, coverage = [], [], []
        for i in range(n_obligations):
            o = RenewalObligation(
                id=self.uid(rng),
                organization=org,
                name=f"Renewal {i + 1:03d} (synthetic)",
                vendor=rng.choice(vendors) if rng.random() < 0.85 else None,
                responsible=rng.choice(people) if rng.random() < 0.9 else None,
                followup=rng.choice(writers) if rng.random() < 0.8 else None,
                auto_renew=rng.choices(["yes", "no", "unknown"], weights=[6, 3, 1])[0],
            )
            obligations.append(o)
            history = rng.choices([0, 1, 2, 3, 4, 5], weights=[2, 3, 3, 2, 1, 1])[0]
            covered = rng.sample(apps, k=min(len(apps), rng.randint(1, 3))) if rng.random() < 0.95 else []
            current = today + timedelta(days=rng.randint(-30, 400))
            for seq in range(1, history + 2):
                years_back = history + 1 - seq
                renewal_date = dates.shift(current, -years_back, "years") or current  # a few years back: in range
                done = seq <= history
                notice = rng.random() < 0.7
                cycle = RenewalCycle(
                    id=self.uid(rng),
                    organization=org,
                    obligation=o,
                    sequence=seq,
                    renewal_date=renewal_date if rng.random() < 0.95 or done else None,
                    notice_value=rng.choice([30, 60, 90]) if notice else None,
                    notice_unit="days" if notice else "",
                    explicit_deadline=renewal_date - timedelta(days=45) if rng.random() < 0.15 else None,
                    term_value=1 if rng.random() < 0.8 else None,
                    term_unit="years",
                    decision=rng.choice(["renew", "cancel", "renegotiate"]) if done or rng.random() < 0.2 else "",
                    decided_on=renewal_date - timedelta(days=60) if done else None,
                    completed_on=renewal_date - timedelta(days=50) if done else None,
                    completion_evidence=f"PO {rng.randint(10000, 99999)}" if done else "",
                    deadline_set_on=today - timedelta(days=rng.randint(0, 200)),
                )
                if cycle.term_value is None:
                    cycle.term_unit = ""
                if cycle.decision and not done:
                    cycle.decided_on = today
                cycles.append(cycle)
                coverage += [RenewalCycleApplication(organization=org, cycle=cycle, application=a) for a in covered]
        RenewalObligation.objects.bulk_create(obligations, batch_size=BATCH)
        RenewalCycle.objects.bulk_create(cycles, batch_size=BATCH)
        RenewalCycleApplication.objects.bulk_create(coverage, batch_size=BATCH)
        return {"renewals": len(obligations), "renewal cycles": len(cycles), "covered applications": len(coverage)}

    def build_links(self, org, rng, scale, resources, apps, instances, people, writers, today):
        """Relationships (2,000 at full scale: `implements` links plus the rest as `depends_on`), integrations (300)
        and confirmations (the dependency size). Links lean towards a few hub records, so the graph has long chains, wide
        fans and cycles, as a real catalog does; the first resources are the hubs."""
        n_integrations, n_links = round(300 * scale), round(2000 * scale)
        records = resources
        instances_of: dict = {}
        for i in instances:
            instances_of.setdefault(i.application_id, []).append(i)

        def hub():
            return records[int(len(records) * rng.random() ** 2.5)]  # skewed towards the front of the list

        def ends():
            """Two different records, and an instance of each now and then (an application's own)."""
            source, target = rng.choice(records), hub()
            while target.id == source.id:
                target = hub()
            pick_instance = lambda r: (  # noqa: E731
                rng.choice(instances_of[r.id]).id if instances_of.get(r.id) and rng.random() < 0.25 else None
            )
            return source, pick_instance(source), target, pick_instance(target)

        # integrations: a resource row and its extension row, between two records; no criticality or business owner
        integration_resources, integration_rows = [], []
        for i in range(n_integrations):
            source, source_instance, target, target_instance = ends()
            name = f"{source.name} to {target.name} feed {i + 1}"
            resource = Resource(
                id=self.uid(rng),
                organization=org,
                kind=Kind.INTEGRATION,
                name=name,
                slug=slugify(name),
                description="Synthetic integration for scale testing.",
                lifecycle=pick(rng, [(Lifecycle.ACTIVE, 80), (Lifecycle.DEPRECATED, 8), (Lifecycle.RETIRED, 12)]),
                technical_owner=None if rng.random() < 0.15 else rng.choice(people),
                support_team=None,
                it_steward=None if rng.random() < 0.15 else rng.choice(writers),
            )
            integration_resources.append(resource)
            integration_rows.append(
                Integration(
                    resource=resource,
                    organization=org,
                    source_resource=source,
                    source_instance_id=source_instance,
                    target_resource=target,
                    target_instance_id=target_instance,
                    data_purpose="Synthetic data flow.",
                    protocol=rng.choice(["SFTP", "REST", "SOAP", "Message queue", "Database link"]),
                    schedule=rng.choice(["Nightly 01:00", "Hourly", "Weekly", "On demand"]),
                )
            )
        Resource.objects.bulk_create(integration_resources, batch_size=BATCH)
        Integration.objects.bulk_create(integration_rows, batch_size=BATCH)

        # relationships: ~60% of integrations are implemented by a component; the rest are `depends_on`
        components = [r for r in records if r.kind == Kind.COMPONENT]
        seen: set = set()
        relationships = []

        def add(rel_type, source, source_instance, target, target_instance):
            key = (rel_type.slug, source.id, source_instance, target.id, target_instance)
            if key in seen or (
                source.id == target.id and (source_instance is None or source_instance == target_instance)
            ):
                return
            seen.add(key)
            relationships.append(
                Relationship(
                    id=self.uid(rng),
                    organization=org,
                    type=rel_type,
                    source=source,
                    source_instance_id=source_instance,
                    target=target,
                    target_instance_id=target_instance,
                    purpose="Synthetic link.",
                    created_by=rng.choice(writers),
                )
            )

        core = {t.slug: t for t in RelationshipType.objects.filter(organization=org, is_core=True)}
        for resource in rng.sample(integration_resources, round(len(integration_resources) * 0.6)):
            if components:
                add(core[RelationshipKind.IMPLEMENTS], rng.choice(components), None, resource, None)
        while len(relationships) < n_links:
            add(core[RelationshipKind.DEPENDS_ON], *ends())
        Relationship.objects.bulk_create(relationships, batch_size=BATCH)

        # confirmations: some current, some older than the interval, some for a link that has since changed
        interval, by_id = org.review_interval_days, {r.id: r for r in records}
        confirmations = []

        def confirm(signature, **link):
            outcome = pick(rng, [("current", 40), ("overdue", 20), ("changed", 10), ("never", 30)])
            if outcome == "never":
                return
            if outcome == "changed":
                signature = dict(signature, owner=str(rng.choice(people).id))
            age = rng.randint(1, interval - 10) if outcome != "overdue" else rng.randint(interval + 10, 300)
            confirmations.append(
                LinkConfirmation(
                    id=self.uid(rng),
                    organization=org,
                    confirmed_by=rng.choice(people),
                    recorded_by=rng.choice(writers),
                    confirmed_on=today - timedelta(days=age),
                    evidence=f"Synthetic confirmation by email thread #{rng.randint(1000, 9999)}",
                    confirmed_signature=signature,
                    **link,
                )
            )

        for rel in relationships:
            confirm(relationship_signature(rel, by_id[rel.source_id].technical_owner_id), relationship=rel)
        for row in integration_rows:
            confirm(integration_signature(row, row.resource.technical_owner_id), integration=row)
        LinkConfirmation.objects.bulk_create(confirmations, batch_size=BATCH)
        return {
            "integrations": len(integration_rows),
            "relationships": len(relationships),
            "link confirmations": len(confirmations),
        }

    def build_audit(self, org, rng, scale, resources, instances, reviews, users, writers, people):
        """The audit log at scale: 50,000 audit events over the last two years, shaped like the real ones
        (record edits with snapshots, instance and review events with their record as related target, link
        additions, sign-ins and exports), one to three events per request id. Bulk-inserted (append-only allows
        inserts) with their own timestamps, so the log, its date filters and each record's History have depth."""
        n_events = round(50000 * scale)
        now = timezone.now()
        relationships = list(
            Relationship.objects.filter(organization=org)
            .select_related("type")
            .only("id", "type__slug", "source_id", "target_id")
        )
        lifecycles = Lifecycle.values

        def snapshot(r, lifecycle):
            return {
                "kind": r.kind,
                "name": r.name,
                "description": r.description,
                "lifecycle": lifecycle,
                "criticality": r.criticality,
                "business_owner": {"id": str(r.business_owner_id), "display": None} if r.business_owner_id else None,
                "technical_owner": {"id": str(r.technical_owner_id), "display": None} if r.technical_owner_id else None,
                "support_team": None,
                "it_steward": None,
                "archived_at": None,
                "purpose": f"Synthetic purpose for {r.name}.",
                "documentation_url": f"https://docs.example.com/{r.slug}",
                "built_by": "vendor",
                "license_model": "commercial",
                "tags": [],
            }

        events, request_id, left_in_request = [], None, 0
        for _ in range(n_events):
            if left_in_request == 0:
                request_id, left_in_request = self.uid(rng), rng.randint(1, 3)
            left_in_request -= 1
            at = now - timedelta(seconds=rng.randint(0, 730 * 86400))
            row = AuditEvent(
                id=self.uid(rng),
                organization=org,
                actor=rng.choice(writers),
                request_id=request_id,
                created_at=at,
            )
            kind = pick(
                rng, [("edit", 40), ("instance", 15), ("review", 10), ("link", 10), ("login", 15), ("export", 10)]
            )
            if kind == "edit":
                r = rng.choice(resources)
                before, after = rng.sample(lifecycles, 2)
                row.action, row.target_type, row.target_id = "resource.updated", "Resource", r.id
                row.changes = {"lifecycle": [before, after]}
                row.prechange, row.postchange = snapshot(r, before), snapshot(r, after)
            elif kind == "instance" and instances:
                i = rng.choice(instances)
                row.action, row.target_type, row.target_id = "instance.updated", "ApplicationInstance", i.id
                row.related_target_type, row.related_target_id = "Resource", i.application_id
                row.changes = {"application": str(i.application_id), "status": ["unknown", i.status]}
                pre = {
                    "application": {"id": str(i.application_id), "display": None},
                    "name": i.name,
                    "status": "unknown",
                }
                row.prechange, row.postchange = pre, {**pre, "status": i.status}
            elif kind == "review" and reviews:
                v = rng.choice(reviews)
                row.action, row.target_type, row.target_id = "review.recorded", "RecordReview", v.id
                row.related_target_type, row.related_target_id = "Resource", v.resource_id
                row.changes = {"review": str(v.id), "confirmed_by": str(v.confirmed_by_id)}
                row.postchange = {"resource": {"id": str(v.resource_id), "display": None}, "evidence": v.evidence}
            elif kind == "link" and relationships:
                rel = rng.choice(relationships)
                row.action, row.target_type, row.target_id = "relationship.created", "Relationship", rel.id
                row.changes = {"type": rel.type.slug, "source": str(rel.source_id), "target": str(rel.target_id)}
            elif kind == "login":
                user = rng.choice(users)
                row.actor, row.action, row.changes = user, "user.login", {"user": user.id}
            else:
                row.action, row.changes = "export", {"kind": "application", "rows": rng.randint(1, 1000), "filters": {}}
            events.append(row)
        # created_at is auto_now_add, which would stamp every row with now; the synthetic history needs its own times.
        # The patch turns that off on the field for this one bulk insert only, in this process; nothing else writes
        # audit events here meanwhile, and real events always get the database time.
        created_at = AuditEvent._meta.get_field("created_at")
        with mock.patch.object(created_at, "auto_now_add", False):
            AuditEvent.objects.bulk_create(events, batch_size=BATCH)
        return len(events)

    @staticmethod
    def uid(rng):
        """A deterministic version-4 UUID, so the same seed builds the same IDs."""
        return uuid.UUID(int=rng.getrandbits(128), version=4)

    @staticmethod
    def unique_name(rng, kind, taken):
        nouns = APP_NOUNS if kind == Kind.APPLICATION else COMPONENT_NOUNS
        base = f"{rng.choice(COMPANIES)} {rng.choice(nouns)}"
        name, n = base, 1
        while (kind, slugify(name)) in taken:
            n += 1
            name = f"{base} {n}"
        taken.add((kind, slugify(name)))
        return name

    def summary(self, org, counts):
        self.stdout.write(self.style.SUCCESS(f"Seeded {org.name}:"))
        for label, value in counts.items():
            self.stdout.write(f"  {label}: {value}")
        self.stdout.write(f"Sign in as {ADMIN_EMAIL} or {EDITOR_EMAIL} / {PASSWORD}")
