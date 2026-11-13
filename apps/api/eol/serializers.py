"""OpenAPI shapes of the end-of-life endpoints (eol/views.py). The views build plain data (eol/reads.py); these
serializers describe it, and the input ones document the bodies eol/services.py validates."""

from rest_framework import serializers

from catalog.models import Criticality, Kind

from .reads import UNKNOWN_REASONS
from .status import STATUSES

STATUS_HELP = (
    "supported; security_only (active support over, security fixes only); end_of_life (the end-of-life date is "
    "today or past); unknown (no data for the product yet, or no cycle: see `why`)."
)


class TrackedPersonSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class TrackedRecordSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.ChoiceField([Kind.APPLICATION, Kind.COMPONENT])
    criticality = serializers.ChoiceField(Criticality.choices, allow_null=True)
    business_owner = TrackedPersonSerializer(allow_null=True)
    technical_owner = TrackedPersonSerializer(allow_null=True)


class TrackedInstanceSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class TrackedVersionSerializer(serializers.Serializer):
    """One tracked version and its derived status (eol/status.py)."""

    id = serializers.UUIDField()
    record = TrackedRecordSerializer()
    instance = TrackedInstanceSerializer(allow_null=True, help_text="Null for the record's own row.")
    product = serializers.CharField(help_text="The endoflife.date product name, such as postgresql.")
    product_label = serializers.CharField(help_text="Its label on endoflife.date, else the name.")
    product_link = serializers.CharField(help_text="Its page on endoflife.date, or empty before the data is cached.")
    product_renamed_to = serializers.CharField(
        allow_null=True,
        help_text="The name endoflife.date renamed the product to, if it did: re-map to it. Null otherwise.",
    )
    version = serializers.CharField(help_text="The version the cycle is derived from, or empty.")
    version_source = serializers.ChoiceField(
        ["tracking", "application", ""],
        help_text="tracking: set here; application: the application's own version field; empty: none.",
    )
    cycle = serializers.CharField(help_text="The release cycle, such as 16 or 22.04; empty when unknown.")
    cycle_source = serializers.ChoiceField(["explicit", "derived", ""])
    status = serializers.ChoiceField(list(STATUSES), help_text=STATUS_HELP)
    unknown_reason = serializers.ChoiceField(
        list(UNKNOWN_REASONS),
        allow_null=True,
        help_text="Why an unknown status is unknown; null otherwise. The installation's: not_downloaded (the product's "
        "data comes with the next refresh), download_failed (the last download failed: see why), downloads_off "
        "(SOFTWARDEN_EOL_ENABLED is off and no snapshot is imported). The record's own: cycle_not_listed, "
        "no_version_match, no_version.",
    )
    why = serializers.CharField(help_text="The status in words, or why it is unknown.")
    eol_date = serializers.DateField(allow_null=True, help_text="The cycle's end-of-life date, when known.")
    days_remaining = serializers.IntegerField(
        allow_null=True, help_text="Days from today (organization-local) to eol_date: negative once past."
    )
    support_until = serializers.DateField(
        allow_null=True, help_text="End of active support, for products that have that phase."
    )
    extended_support_until = serializers.DateField(
        allow_null=True, help_text="End of extended (often paid) support, for products that offer it."
    )
    lts = serializers.BooleanField(allow_null=True, help_text="A long-term support cycle; null when unknown.")
    latest = serializers.CharField(allow_null=True, help_text="The latest release of the cycle.")
    latest_date = serializers.DateField(allow_null=True)
    release_date = serializers.DateField(allow_null=True)
    checked_at = serializers.DateTimeField(
        allow_null=True, help_text="When the product's data was last checked against endoflife.date."
    )


class RecordEndOfLifeSerializer(TrackedVersionSerializer):
    """The record's own tracked version, with its instances' rows (applications)."""

    instances = TrackedVersionSerializer(many=True)


class TrackInputSerializer(serializers.Serializer):
    product = serializers.CharField(
        required=False,
        help_text="An endoflife.date product name or alias (GET /end-of-life/products). Required the first time.",
    )
    version = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="The version deployed, such as 16.4. Empty: an application uses its own version field.",
    )
    cycle = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="The release cycle, such as 16. Empty or null: derived from the version.",
    )


class TrackInstanceInputSerializer(serializers.Serializer):
    version = serializers.CharField(required=False, allow_blank=True, help_text="The version this instance runs.")
    cycle = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, help_text="Its release cycle; empty: derived."
    )


class CacheStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(help_text="SOFTWARDEN_EOL_ENABLED: whether the daily refresh runs.")
    source = serializers.ChoiceField(  # type: ignore[assignment]  # the API field is called source
        ["api", "import"], allow_null=True, help_text="Where the data last came from."
    )
    products = serializers.IntegerField(help_text="Products in the cached list.")
    last_run_at = serializers.DateTimeField(allow_null=True)
    last_success_at = serializers.DateTimeField(allow_null=True)
    imported_at = serializers.DateTimeField(allow_null=True)
    last_error = serializers.CharField(allow_null=True)


class StatusCountsSerializer(serializers.Serializer):
    supported = serializers.IntegerField()
    security_only = serializers.IntegerField()
    end_of_life = serializers.IntegerField()
    unknown = serializers.IntegerField()


class OverviewSerializer(serializers.Serializer):
    today = serializers.DateField(help_text="The organization's today, which days_remaining counts from.")
    cache = CacheStatusSerializer()
    counts = StatusCountsSerializer(help_text="Rows per status, after the filters.")
    results = TrackedVersionSerializer(many=True)


class ProductSerializer(serializers.Serializer):
    name = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    category = serializers.CharField()
    aliases = serializers.ListField(child=serializers.CharField())
    link = serializers.CharField()
    tracked = serializers.BooleanField(help_text="Whether this organization tracks the product.")
    releases_cached = serializers.BooleanField(
        allow_null=True,
        help_text="For a product this organization tracks: whether its release cycles are cached yet. Null otherwise "
        "(the cache is shared; whether another organization tracks a product isn't shown).",
    )
    renamed_to = serializers.CharField(
        allow_null=True,
        help_text="For a product this organization tracks: the name endoflife.date renamed it to, if it did. Re-map "
        "the records to that name. Null otherwise.",
    )


class ReleaseSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="The cycle, as `cycle` takes it.")
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    codename = serializers.CharField()
    release_date = serializers.DateField(allow_null=True)
    lts = serializers.BooleanField()
    eol_date = serializers.DateField(allow_null=True)
    support_until = serializers.DateField(allow_null=True)
    extended_support_until = serializers.DateField(allow_null=True)
    latest = serializers.CharField()
    status = serializers.ChoiceField([s for s in STATUSES if s != "unknown"], help_text=STATUS_HELP)


class ProductDetailSerializer(ProductSerializer):
    checked_at = serializers.DateTimeField(allow_null=True, help_text="Only for a product this organization tracks.")
    error = serializers.CharField(
        allow_null=True, help_text="Why the last fetch failed, if it did; only for a product this organization tracks."
    )
    releases = ReleaseSerializer(
        many=True, help_text="Newest first; only for a product this organization tracks (empty otherwise)."
    )
