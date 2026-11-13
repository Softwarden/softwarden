"""The Entra source now reads app registrations as well as enterprise applications and merges them
by application (client) ID, so an Entra observation's external ID becomes its `appId` (it was the service principal's
object ID, which a registration without one doesn't have). Data only, in place, so decisions and links stay on their
observations. Forward-only: the reverse does nothing.

- Re-keying: rows of one connector that share an appId (an app deleted and re-created keeps its appId) can't all have
  it. Within each (connector, appId) group the key goes to the row with a decision, else the one still seen, else the
  one seen last (then the lowest ID); the others keep their old key and the next run marks them no longer seen.
- Kind: a row becomes `ai_agent` only when its tags carry the Copilot Studio markers the new source uses;
  the earlier `ai_agent` flag (tags AIAgent, CopilotAgent) isn't carried over.
- Every Entra application row is marked `data["rekeyed"] = True`. Its fingerprint stays the earlier one (service principal
  owners only, by email); the first run after the upgrade moves a decision whose fingerprint equals it to the new
  fingerprint (registration and service principal owners by object ID), so dismissals don't come back
  (discovery/engine._apply), and replaces `data`, which clears the mark."""

from django.db import migrations

GUID_LENGTH = 36
AGENT_TAGS = {"agentcreatedby:copilotstudio", "agenticapp", "aiagentbuilder", "agenticinstance"}
AGENT_TAG_PREFIXES = ("power-virtual-agents-",)


def _agent(data):
    tags = [t.lower() for t in data.get("tags") or [] if isinstance(t, str)]
    return any(t in AGENT_TAGS or t.startswith(AGENT_TAG_PREFIXES) for t in tags)


def _rank(obs, decided):
    """Lower wins the appId: decided, then still seen, then seen last, then the lowest ID."""
    return (obs.id not in decided, obs.gone_at is not None, -obs.last_seen_at.timestamp(), str(obs.id))


def forward(apps, schema_editor):
    Observation = apps.get_model("discovery", "Observation")
    Decision = apps.get_model("discovery", "ReconciliationDecision")
    rows = list(Observation.objects.filter(connector__source="entra", kind="application"))
    if not rows:
        return
    decided = set(Decision.objects.filter(observation__in=rows).values_list("observation_id", flat=True))
    taken = set(
        Observation.objects.filter(connector__source="entra").values_list("connector_id", "kind", "external_id")
    )
    groups: dict = {}
    for obs in rows:
        data = obs.data if isinstance(obs.data, dict) else {}
        app_id = str(data.get("app_id") or "").strip().lower()
        if len(app_id) == GUID_LENGTH:
            groups.setdefault((obs.connector_id, app_id), []).append(obs)
    winners = {min(group, key=lambda o: _rank(o, decided)).id: app_id for (_, app_id), group in groups.items()}
    for obs in rows:
        data = dict(obs.data) if isinstance(obs.data, dict) else {}
        kind = "ai_agent" if _agent(data) else obs.kind
        external_id = winners.get(obs.id, obs.external_id)
        key = (obs.connector_id, kind, external_id)
        if (kind, external_id) != (obs.kind, obs.external_id) and key not in taken:
            taken.discard((obs.connector_id, obs.kind, obs.external_id))
            taken.add(key)
            obs.kind, obs.external_id = kind, external_id
        data["rekeyed"] = True
        obs.data = data
        obs.ai_agent = obs.kind == "ai_agent"
        obs.save(update_fields=["kind", "external_id", "data", "ai_agent"])


class Migration(migrations.Migration):

    dependencies = [
        ("discovery", "0009_sources_access_check"),
    ]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
