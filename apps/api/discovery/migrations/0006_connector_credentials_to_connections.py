"""Every Entra connector's credentials move to a new MicrosoftConnection in its organization, named after the
connector (a numbered suffix if the name is taken). `config.tenant_id` and `config.client_id` become the connection's
fields and leave `config`; the secret slot is copied as it is stored (ciphertext, key id, set at), so it stays
encrypted throughout and is never decrypted here.

The connection starts at revision 1. It counts as tested at that revision only when the connector's latest run
succeeded and neither the secret nor the connector (whose old form held the tenant and client) changed after that run
started: that run is the test it would otherwise need. So working connectors keep running after the upgrade and the
rest must be tested first. Nothing is audited (a migration has no actor; the row says where
it came from in `test_result.carried_over`). A separate migration from 0005's schema change: PostgreSQL refuses the
deferred index creation of an AddField in a transaction that has already written rows under deferred constraints.
The connector's old secret columns are dropped in 0007. Forward-only."""

from django.db import migrations
from django.db.models.functions import Lower

NAME_MAX = 100
CARRIED_OVER = "Carried over from the connector: its latest run succeeded with these credentials."


def _free_name(Connection, org_id, wanted):
    taken = {
        n.lower()
        for n in Connection.objects.filter(organization_id=org_id).annotate(low=Lower("name")).values_list("low", flat=True)
    }
    base = (wanted or "Microsoft connection").strip()[:NAME_MAX] or "Microsoft connection"
    name, n = base, 2
    while name.lower() in taken:
        suffix = f" ({n})"
        name, n = base[: NAME_MAX - len(suffix)] + suffix, n + 1
    return name


def connections_from_connectors(apps, schema_editor):
    Connector = apps.get_model("discovery", "Connector")
    ConnectorRun = apps.get_model("discovery", "ConnectorRun")
    MicrosoftConnection = apps.get_model("core", "MicrosoftConnection")
    for connector in Connector.objects.filter(source="entra", microsoft_connection__isnull=True).order_by("created_at", "id"):
        config = dict(connector.config or {})
        tenant = str(config.pop("tenant_id", "") or "").strip()[:253]
        client = str(config.pop("client_id", "") or "").strip()[:36]
        last = ConnectorRun.objects.filter(connector_id=connector.id).order_by("-started_at").first()
        # Tested only if that run used exactly these credentials: no secret replaced and no connector edit (the old
        # form saved tenant and client with the connector) after it started. Run bookkeeping uses .update(), so
        # updated_at moves only on real edits; any doubt leaves the connection untested.
        tested = (
            bool(connector.secret_ciphertext)
            and last is not None
            and last.status == "succeeded"
            and (connector.secret_set_at is None or connector.secret_set_at <= last.started_at)
            and connector.updated_at <= last.started_at
        )
        connection = MicrosoftConnection.objects.create(
            organization_id=connector.organization_id,
            name=_free_name(MicrosoftConnection, connector.organization_id, connector.name),
            tenant_id=tenant,
            client_id=client,
            secret_ciphertext=connector.secret_ciphertext or "",
            secret_key_id=connector.secret_key_id or "",
            secret_set_at=connector.secret_set_at,
            revision=1,
            tested_revision=1 if tested else None,
            tested_at=(last.finished_at or last.started_at) if tested else None,
            test_ok=True if tested else None,
            test_result=(
                {"ok": True, "problem": "", "code": "", "detail": CARRIED_OVER, "carried_over": True} if tested else {}
            ),
        )
        connector.microsoft_connection_id = connection.id
        connector.config = config
        connector.save(update_fields=["microsoft_connection", "config"])



class Migration(migrations.Migration):

    dependencies = [
        ("discovery", "0005_connector_connections"),
    ]

    operations = [
        migrations.RunPython(connections_from_connectors, migrations.RunPython.noop, elidable=False),
    ]
