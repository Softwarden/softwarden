"""Response shapes of the licence waste report (discovery/licence_waste.py builds them). The views render
the built dictionaries through these, so money is a string with two decimals and dates are YYYY-MM-DD."""

from rest_framework import serializers

from core.drf import ReadOnlySerializer

from .licence_waste import GAPS, NO_SKU_ID, STATUSES, USER_TYPES

GAP_CODES = [*GAPS, NO_SKU_ID["code"]]


def money(**kwargs) -> serializers.DecimalField:
    """Money as a string with two decimals."""
    return serializers.DecimalField(max_digits=18, decimal_places=2, **kwargs)


class LicenceWasteRefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class LicenceWasteGapSerializer(ReadOnlySerializer):
    code = serializers.ChoiceField(GAP_CODES)
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label


class LicenceWastePriceSerializer(ReadOnlySerializer):
    currency = serializers.CharField(help_text="The contracts' currency (ISO 4217); never converted.")
    annual_cost = money(help_text="The contracts' annualized cost together.")
    seats = serializers.IntegerField(help_text="The seats purchased in those contracts' pools of this SKU.")
    annual_unit_price = money(help_text="annual_cost ÷ seats, to the cent.")
    contracts = LicenceWasteRefSerializer(many=True)
    some_pools_unpriced = serializers.BooleanField(
        help_text="Some pools of this SKU couldn't be priced (no contract, ...); the price is from the others."
    )


class LicenceWastePoolSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField(help_text="The pool's SKU name.")
    application = LicenceWasteRefSerializer()
    contract = LicenceWasteRefSerializer(allow_null=True)


class LicenceWasteCountsSerializer(ReadOnlySerializer):
    assigned = serializers.IntegerField(help_text="Licensed members (not guests, not service accounts).")
    active = serializers.IntegerField(help_text="Members who signed in successfully within the last `days` days.")
    new = serializers.IntegerField(help_text="Members who never signed in, created within the last `days` days.")
    inactive = serializers.IntegerField(
        help_text="Enabled members with no successful sign-in within `days` days (never_signed_in included)."
    )
    never_signed_in = serializers.IntegerField(help_text="Inactive members with no sign-in recorded at all.")
    disabled = serializers.IntegerField(help_text="Members whose account is disabled but still holds the license.")
    unknown = serializers.IntegerField(
        help_text="Enabled members whose last sign-in couldn't be read (needs Entra ID P1 / AuditLog.Read.All)."
    )
    reclaimable = serializers.IntegerField(help_text="inactive + disabled: the seats the money counts.")
    guests = serializers.IntegerField(help_text="Licensed guests, counted apart and never in the money.")
    guests_inactive = serializers.IntegerField(help_text="Guests disabled, or inactive as members are.")
    service_accounts = serializers.IntegerField(help_text="Users the connector's scope names as service accounts.")
    group_reclaimable = serializers.IntegerField(
        help_text="Reclaimable seats held only through a group: reclaimed by changing group membership."
    )


class LicenceWasteRowSerializer(LicenceWasteCountsSerializer):
    sku_id = serializers.CharField(help_text="The SKU's skuId (a GUID, lower case).")
    sku_part_number = serializers.CharField(
        help_text="From the Entra licenses source, else the bound pool's SKU name, else the SKU ID."
    )
    seats_purchased = serializers.IntegerField(
        allow_null=True, help_text="prepaidUnits.enabled from the Entra licenses source; null without one."
    )
    seats_assigned = serializers.IntegerField(
        allow_null=True, help_text="consumedUnits from the Entra licenses source."
    )
    seats_unassigned = serializers.IntegerField(allow_null=True, help_text="Purchased and assigned to nobody.")
    applications = LicenceWasteRefSerializer(many=True, help_text="The applications of the pools bound to this SKU.")
    vendors = LicenceWasteRefSerializer(
        many=True, help_text="The pools' contracts' vendors and their applications' vendors."
    )
    pools = LicenceWastePoolSerializer(many=True)
    price = LicenceWastePriceSerializer(allow_null=True)
    gap = LicenceWasteGapSerializer(allow_null=True, help_text="Why there is no price; null when there is one.")
    annual_waste = money(allow_null=True, help_text="Reclaimable seats at the price, per year; null with a gap.")
    annual_unassigned = money(
        allow_null=True,
        help_text="Unassigned seats at the price, per year; null with a gap or no seat counts.",
    )


class LicenceWasteTotalSerializer(ReadOnlySerializer):
    currency = serializers.CharField()
    skus = serializers.IntegerField()
    reclaimable = serializers.IntegerField()
    annual_waste = money()
    annual_unassigned = money()


class LicenceWasteUnpricedSerializer(ReadOnlySerializer):
    skus = serializers.IntegerField()
    reclaimable = serializers.IntegerField(help_text="Reclaimable seats of SKUs with a gap: shown, never priced at 0.")


class LicenceWasteSummarySerializer(LicenceWasteCountsSerializer):
    """Seat counts (reclaimable among them) over every row, priced or not, free SKUs included; only `totals` is money."""

    skus = serializers.IntegerField()
    totals = LicenceWasteTotalSerializer(many=True, help_text="Per currency, never converted.")
    unpriced = LicenceWasteUnpricedSerializer()


class LicenceWasteConnectorSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    seen_at = serializers.DateTimeField(help_text="When its last successful run read the users.")
    sign_in_known = serializers.BooleanField(help_text="Whether that run read the last sign-in.")


class LicenceWasteReportSerializer(ReadOnlySerializer):
    days = serializers.IntegerField(help_text="N: no successful sign-in within this many days is inactive.")
    cutoff = serializers.DateField(help_text="Today (organization time) minus `days`: the first day that is active.")
    as_of = serializers.DateTimeField(allow_null=True, help_text="When the newest license usage run read the users.")
    sign_in_activity = serializers.ChoiceField(
        ["known", "unknown", "partial", "none"],
        help_text="Whether the connectors read the last sign-in: all, none, some, or there is no license usage data.",
    )
    connectors = LicenceWasteConnectorSerializer(many=True)
    notes = serializers.ListField(child=serializers.CharField())
    summary = LicenceWasteSummarySerializer()
    rows = LicenceWasteRowSerializer(many=True)


class LicenceWasteUserSerializer(ReadOnlySerializer):
    name = serializers.CharField(help_text="The linked person's name, else “Unlinked user”.")
    ref = serializers.CharField(help_text="A short, stable hash telling unlinked users apart (never the object ID).")
    person = LicenceWasteRefSerializer(allow_null=True)
    status = serializers.ChoiceField(STATUSES)
    never_signed_in = serializers.BooleanField()
    last_sign_in_on = serializers.DateField(allow_null=True, help_text="The UTC date of the last successful sign-in.")
    last_sign_in = serializers.CharField(
        help_text="The date, “never signed in”, “no successful sign-in recorded”, or unknown (needs Entra ID P1 / "
        "AuditLog.Read.All)."
    )
    days_since = serializers.IntegerField(allow_null=True)
    account_enabled = serializers.BooleanField()
    guest = serializers.BooleanField()
    group_assigned = serializers.BooleanField(
        help_text="The user holds this license only through a group: reclaim it by changing the group membership."
    )
    connector = LicenceWasteRefSerializer()


class LicenceWasteUsersSerializer(ReadOnlySerializer):
    sku_id = serializers.CharField()
    sku_part_number = serializers.CharField()
    days = serializers.IntegerField()
    cutoff = serializers.DateField()
    statuses = serializers.ListField(child=serializers.ChoiceField(STATUSES))
    user_type = serializers.ChoiceField(USER_TYPES)
    total = serializers.IntegerField(help_text="Users matching; at most 5,000 are listed (`truncated`).")
    truncated = serializers.BooleanField()
    users = LicenceWasteUserSerializer(many=True)


class LicenceWastePoolDetailSerializer(ReadOnlySerializer):
    pool = LicenceWasteRefSerializer()
    sku_id = serializers.CharField(allow_null=True)
    days = serializers.IntegerField()
    row = LicenceWasteRowSerializer(allow_null=True, help_text="The SKU's report row; null when nothing reports it.")
    gap = LicenceWasteGapSerializer(allow_null=True, help_text="no_sku_id when the pool isn't bound to a SKU.")


class LicenceWasteContractRefSerializer(LicenceWasteRefSerializer):
    currency = serializers.CharField()


class LicenceWasteContractShareSerializer(ReadOnlySerializer):
    seats = serializers.IntegerField(help_text="This contract's seats of the SKU (its pools' seats purchased).")
    of_seats = serializers.IntegerField(help_text="The seats of every contract pricing the SKU.")
    annual_cost = money(help_text="This contract's annualized cost.")
    annual_waste = money(
        help_text="annual_cost × the row's reclaimable ÷ of_seats: weighted by this contract's own cost, so the parts of "
        "every pricing contract add up to the row's annual_waste (to the cent, each rounded once)."
    )
    annual_unassigned = money(allow_null=True, help_text="annual_cost × the row's seats_unassigned ÷ of_seats.")


class LicenceWasteContractRowSerializer(LicenceWasteRowSerializer):
    contract_share = LicenceWasteContractShareSerializer(
        allow_null=True, help_text="This contract's part of the row's money; null when it doesn't price the SKU."
    )


class LicenceWasteContractTotalsSerializer(ReadOnlySerializer):
    currency = serializers.CharField()
    skus = serializers.IntegerField(help_text="SKUs this contract prices.")
    annual_waste = money()
    annual_unassigned = money()


class LicenceWasteContractSerializer(ReadOnlySerializer):
    contract = LicenceWasteContractRefSerializer()
    days = serializers.IntegerField()
    contract_totals = LicenceWasteContractTotalsSerializer(
        help_text="This contract's share of the money, summed over its SKUs (weighted by its own cost when several "
        "contracts price one SKU)."
    )
    summary = LicenceWasteSummarySerializer(help_text="The SKUs' whole rows, whichever contracts price them.")
    rows = LicenceWasteContractRowSerializer(many=True, help_text="The SKUs its pools are bound to.")
