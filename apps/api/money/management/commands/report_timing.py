"""Time the dashboard and the spend reports in-process (Django test client, no server, no network) on the synthetic
scale organization (seed_scale), topped up to `--contracts` contracts with two lines each. Fictional data only.

  manage.py seed_scale                                  once: the 1,000-resource organization
  manage.py report_timing --contracts 1500              add contracts up to 1,500 (about 3,000 lines), then time

Like seed_scale, the contracts are bulk-inserted and BYPASS the service layer (no audit events); their derived values
come from the same money/contracts.derive the services use. The random seed is fixed, so every run builds the same
shape. Before timing it ANALYZEs the application's tables, so the numbers don't depend on whether autovacuum has
caught up with the rows just inserted. The target is p95 under 300 ms for reads.

It writes only into seed_scale's organization: exactly one organization with that name, with seed_scale's synthetic
Admin as a member; anything else is refused. With SOFTWARDEN_DEBUG off (a production setting) it refuses unless
--synthetic-only-i-understand is passed."""

import random
import statistics
import time
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import Client

from catalog.management.commands.seed_scale import ADMIN_EMAIL, ORG_NAME, refresh_statistics
from catalog.models import Resource, Vendor
from core.models import Membership, Organization
from money import contracts
from money.models import AutoRenew, BillingFrequency, Contract, ContractItem, ContractStatus

READ_P95_MS = 300
UNDERSTAND = "--synthetic-only-i-understand"
PREFIX = "Timing contract"
VENDORS = 120
LINES = 2
ENDPOINTS = {
    "dashboard": "/api/v1/dashboard",
    "spend by vendor": "/api/v1/reports/spend?by=vendor",
    "spend by department": "/api/v1/reports/spend?by=department",
    "spend by category": "/api/v1/reports/spend?by=category",
    "spend by cost_center": "/api/v1/reports/spend?by=cost_center",
    "spend export.csv": "/api/v1/reports/spend/export.csv",
}
FREQUENCIES = [
    BillingFrequency.MONTHLY,
    BillingFrequency.QUARTERLY,
    BillingFrequency.SEMIANNUAL,
    BillingFrequency.ANNUAL,
    BillingFrequency.MULTI_YEAR,
    BillingFrequency.ONE_TIME,
    BillingFrequency.USAGE_BASED,
]


def seed_contracts(org, total: int, seed: int = 20261105) -> int:
    """Add synthetic contracts (two lines each: one at 60 %, one sharing the rest) until the organization has `total`
    of them. Returns how many were added."""
    have = Contract.objects.filter(organization=org, name__startswith=PREFIX).count()
    if have >= total:
        return 0
    rng = random.Random(seed + have)
    records = list(Resource.objects.filter(organization=org, archived_at__isnull=True).values_list("id", flat=True))
    if len(records) < LINES:
        raise CommandError(f"{org.name} needs records first: run seed_scale.")
    vendors = []
    for i in range(VENDORS):
        vendor, _ = Vendor.objects.get_or_create(organization=org, name=f"Timing Vendor {i:03d}")
        vendors.append(vendor)
    made: list[Contract] = []
    lines: list[ContractItem] = []
    for i in range(have, total):
        year = rng.choice([2025, 2026, 2026, 2027])
        start = date(year, rng.randint(1, 12), 1)
        end = date(year + 1, rng.randint(1, 12), 28)
        c = Contract(
            id=uuid.UUID(int=rng.getrandbits(128), version=4),
            organization=org,
            name=f"{PREFIX} {i:05d}",
            vendor=vendors[i % VENDORS] if i % 25 else None,
            contract_number=f"T-{i:05d}",
            amount=Decimal(rng.choice([1200, 4800, 12000, 50000, 250000])),
            currency=rng.choice(["USD", "EUR", "GBP"]),
            billing_frequency=rng.choice(FREQUENCIES),
            start_date=start,
            end_date=end,
            auto_renew=rng.choice([AutoRenew.YES, AutoRenew.NO, AutoRenew.UNKNOWN]),
            notice_value=rng.choice([30, 60, 90]),
            notice_unit="days",
            status=ContractStatus.SIGNED if i % 20 == 0 else ContractStatus.ACTIVE,
            cost_center=f"CC-{i % 12}" if i % 7 else "",
        )
        contracts.derive(c, org.renewal_lead_days)
        made.append(c)
        first, second = rng.sample(records, LINES)
        lines.append(ContractItem(organization=org, contract=c, resource_id=first, allocation_percent=Decimal(60)))
        lines.append(ContractItem(organization=org, contract=c, resource_id=second, allocation_percent=None))
    with transaction.atomic():
        Contract.objects.bulk_create(made, batch_size=500)
        ContractItem.objects.bulk_create(lines, batch_size=500)
    return len(made)


def percentile(sorted_ms: list[float], p: float) -> float:
    """Nearest-rank percentile of an ascending list."""
    rank = max(1, -(-len(sorted_ms) * p // 100))
    return sorted_ms[int(rank) - 1]


def synthetic_org():
    """seed_scale's organization and its synthetic Admin. Names aren't unique, so it must be the only organization with
    that name and have seed_scale's Admin as a member; anything else is refused rather than written into."""
    user = get_user_model().objects.filter(username=ADMIN_EMAIL).first()
    if user is None:
        raise CommandError(f"No user {ADMIN_EMAIL}: run seed_scale first.")
    orgs = list(Organization.objects.filter(name=ORG_NAME)[:2])
    if not orgs:
        raise CommandError("Run seed_scale first.")
    if len(orgs) > 1:
        raise CommandError(f"More than one organization is named '{ORG_NAME}'; refusing to pick one.")
    org = orgs[0]
    if not Membership.objects.filter(organization=org, user=user).exists():
        raise CommandError(
            f"'{ORG_NAME}' doesn't have seed_scale's {ADMIN_EMAIL} as a member; refusing to write to it."
        )
    return org, user


class Command(BaseCommand):
    help = (
        "Time the dashboard and the spend reports in-process on the seed_scale organization, first adding synthetic "
        "contracts (two lines each, bulk-inserted, no audit events) up to --contracts. Fictional data only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--contracts", type=int, default=1500, help="Contracts to have first (default 1500).")
        parser.add_argument("--samples", type=int, default=40, help="Timed requests per endpoint (default 40).")
        parser.add_argument("--strict", action="store_true", help=f"Exit 1 if any p95 is {READ_P95_MS} ms or more.")
        parser.add_argument(
            UNDERSTAND,
            action="store_true",
            help="Run with SOFTWARDEN_DEBUG off too (it writes synthetic contracts into the scale organization).",
        )

    def handle(self, *args, contracts: int, samples: int, strict: bool, **options):
        if not settings.DEBUG and not options["synthetic_only_i_understand"]:
            raise CommandError(
                "SOFTWARDEN_DEBUG is off, so this may be a production database. report_timing writes synthetic "
                f"contracts into '{ORG_NAME}'; pass {UNDERSTAND} if this is a test database."
            )
        org, user = synthetic_org()
        added = seed_contracts(org, contracts)
        # Time against current planner statistics, not the ones from before the bulk insert (seed_scale's
        # refresh_statistics): otherwise the first minute after seeding measures autovacuum's lag, not the code.
        refresh_statistics()
        host = next((h for h in settings.ALLOWED_HOSTS if h and h != "*" and not h.startswith(".")), "localhost")
        client = Client(HTTP_HOST=host)
        client.force_login(user)
        count = Contract.objects.filter(organization=org).count()
        lines = ContractItem.objects.filter(organization=org).count()
        self.stdout.write(f"{org.name}: {count} contracts ({added} added), {lines} lines; {samples} samples each")
        self.stdout.write(f"{'endpoint':24} {'p50':>6} {'p95':>6} {'max':>6}  (ms, in-process)")
        slow = []
        for label, url in ENDPOINTS.items():
            for _ in range(3):  # warm up
                client.get(url)
            took = []
            for _ in range(samples):
                started = time.perf_counter()
                response = client.get(url)
                took.append((time.perf_counter() - started) * 1000)
                if response.status_code != 200:
                    raise CommandError(f"{label}: {response.status_code}")
            took.sort()
            p95 = percentile(took, 95)
            if p95 >= READ_P95_MS:
                slow.append(label)
            self.stdout.write(f"{label:24} {statistics.median(took):6.0f} {p95:6.0f} {took[-1]:6.0f}")
        client.logout()
        if slow and strict:
            raise CommandError(f"p95 at or over {READ_P95_MS} ms: {', '.join(slow)}")
