"""AI providers and models: reference entities an AI agent's models_used
points at. Writes are validated, scoped to the organization and audited in one transaction; Editors and Admins
make them (the viewsets declare it), anyone reads.

A provider with models, or a model an agent uses (archived agents included), can't be deleted: the 409 names what
uses it, like the other reference data (catalog/admin_services.delete_named)."""

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Count, Q

from core import limits
from core.problems import Conflict, FieldErrors

from .models import AgentModel, AIModel, AIProvider, Lifecycle, ModelModality, Resource
from .services import _in_org, audit

NAME_MAX = 200
VERSION_MAX = 100
REGION_MAX = 100
URL_MAX = 2000
URL_MESSAGE = "Enter a full web address starting with http:// or https://."


def _text(data, field, errors, limit, required=False):
    raw = data.get(field)
    if raw not in (None, "") and not isinstance(raw, str):
        errors[field] = ["Send text."]
        return ""
    value = (raw or "").strip()
    if required and not value:
        errors[field] = ["Enter a name."]
    elif len(value) > limit:
        errors[field] = [f"Use {limit:,} characters or fewer."]
    return value


# ---- providers ----------------------------------------------------------------------------------------------------


def _provider_values(org, data, provider=None):
    """The provider fields present in data (a new provider needs its name), validated; another provider's name,
    ignoring case, is a field error."""
    errors: dict[str, list[str]] = {}
    values: dict[str, str] = {}
    if "name" in data or provider is None:
        values["name"] = _text(data, "name", errors, NAME_MAX, required=True)
        clash = AIProvider.objects.filter(organization=org, name__iexact=values["name"])
        if provider is not None:
            clash = clash.exclude(id=provider.id)
        other = clash.first() if values["name"] and "name" not in errors else None
        if other is not None:
            errors["name"] = [f"An AI provider named {other.name} already exists."]
    if "website" in data:
        values["website"] = _text(data, "website", errors, URL_MAX)
        if values["website"] and "website" not in errors:
            try:
                URLValidator(schemes=["http", "https"])(values["website"])
            except ValidationError:
                errors["website"] = [URL_MESSAGE]
    if errors:
        raise FieldErrors(errors)
    return values


@transaction.atomic
def create_provider(org, actor, data):
    provider = AIProvider(organization=org, **_provider_values(org, data))
    limits.check_room(org, limits.OTHER_RECORDS)
    provider.save()
    audit(org, actor, "ai_provider.created", provider, {"name": provider.name})
    return provider


@transaction.atomic
def update_provider(org, actor, provider, data):
    values = _provider_values(org, data, provider)
    changes = {k: [getattr(provider, k), v] for k, v in values.items() if getattr(provider, k) != v}
    for k, v in values.items():
        setattr(provider, k, v)
    provider.save()
    audit(org, actor, "ai_provider.updated", provider, {"name": provider.name, **changes})
    return provider


def _refuse(obj_name, names, singular, plural, key):
    refs = sorted(names, key=str.lower)
    if refs:
        shown = ", ".join(refs[:3]) + (f" and {len(refs) - 3} more" if len(refs) > 3 else "")
        uses = f"1 {singular} uses it" if len(refs) == 1 else f"{len(refs)} {plural} use it"
        raise Conflict(
            f"{obj_name} can't be deleted: {uses} ({shown}). {key}",
            extra={"blocking_references": {plural: len(refs)}, "blocking_names": refs[:10]},
        )


@transaction.atomic
def delete_provider(org, actor, provider):
    """Refused while any model belongs to the provider."""
    names = [m.name + (f" {m.version}" if m.version else "") for m in AIModel.objects.filter(provider=provider)]
    _refuse(provider.name, names, "AI model", "AI models", "Delete or move its models first.")
    audit(org, actor, "ai_provider.deleted", provider, {"name": provider.name})
    provider.delete()


def provider_usage(org):
    """{provider_id: number of models}, in one query."""
    rows = AIModel.objects.filter(organization=org).values("provider_id").annotate(n=Count("pk"))
    return {row["provider_id"]: row["n"] for row in rows}


# ---- models -------------------------------------------------------------------------------------------------------


def _model_values(org, data, model=None):
    """The model fields present in data (a new model needs its provider and name), validated. Name and version are
    unique per provider, ignoring case; the merged result is checked, so a PATCH of the version alone is judged too."""
    errors: dict[str, list[str]] = {}
    values: dict = {}
    if "provider" in data or model is None:
        values["provider"] = _in_org(AIProvider, org, data.get("provider"), "provider", errors)
        if values["provider"] is None and "provider" not in errors:
            errors["provider"] = ["Choose a provider."]
    if "name" in data or model is None:
        values["name"] = _text(data, "name", errors, NAME_MAX, required=True)
    if "version" in data:
        values["version"] = _text(data, "version", errors, VERSION_MAX)
    if "hosting_region" in data:
        values["hosting_region"] = _text(data, "hosting_region", errors, REGION_MAX)
    if "modality" in data:
        modality = data.get("modality")
        modality = "" if modality is None else modality  # null clears; 0, false or [] are refused
        if not isinstance(modality, str) or (modality not in ModelModality.values and modality != ""):
            errors["modality"] = [f"Use one of: {', '.join(ModelModality.values)}, or leave it empty."]
        values["modality"] = modality
    provider = values.get("provider", model.provider if model else None)
    if not errors and provider is not None:
        name = values.get("name", model.name if model else "")
        version = values.get("version", model.version if model else "")
        clash = AIModel.objects.filter(organization=org, provider=provider, name__iexact=name, version__iexact=version)
        if model is not None:
            clash = clash.exclude(id=model.id)
        if clash.exists():
            label = f"{name} {version}".strip()
            errors["name"] = [f"{provider.name} already has a model named {label}."]
    if errors:
        raise FieldErrors(errors)
    return values


def _audited(model):
    return {
        "provider": str(model.provider_id),
        "name": model.name,
        "version": model.version,
        "modality": model.modality,
        "hosting_region": model.hosting_region,
    }


@transaction.atomic
def create_model(org, actor, data):
    model = AIModel(organization=org, **_model_values(org, data))
    limits.check_room(org, limits.OTHER_RECORDS)
    model.save()
    audit(org, actor, "ai_model.created", model, {k: v for k, v in _audited(model).items() if v})
    return model


@transaction.atomic
def update_model(org, actor, model, data):
    before = _audited(model)
    for field, value in _model_values(org, data, model).items():
        setattr(model, field, value)
    model.save()
    after = _audited(model)
    changes = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    audit(org, actor, "ai_model.updated", model, {"name": model.name, **changes})
    return model


@transaction.atomic
def delete_model(org, actor, model):
    """Refused while any agent uses it, archived agents included (their history names it)."""
    names = list(
        Resource.objects.filter(organization=org, ai_agent__model_links__model=model).values_list("name", flat=True)
    )
    _refuse(model.name, names, "AI agent", "AI agents", "Remove it from them first.")
    audit(org, actor, "ai_model.deleted", model, {"name": model.name, "provider": str(model.provider_id)})
    model.delete()


def model_usage(org):
    """{model_id: number of agents using it}, archived agents included, in one query."""
    rows = AgentModel.objects.filter(organization=org).values("model_id").annotate(n=Count("pk"))
    return {row["model_id"]: row["n"] for row in rows}


def models_in_use(org):
    """The "models in use" report: every model at least one unarchived agent uses, with how many agents use it
    and how many of those are live (Active or Deprecated), most used first, then by name. Models no agent uses are
    left out; archived agents don't count."""
    live = Q(agents__resource__archived_at__isnull=True)
    rows = (
        AIModel.objects.filter(organization=org)
        .annotate(
            agent_count=Count("agents", filter=live),
            live_count=Count(
                "agents",
                filter=live & Q(agents__resource__lifecycle__in=[Lifecycle.ACTIVE, Lifecycle.DEPRECATED]),
            ),
        )
        .filter(agent_count__gt=0)
        .select_related("provider")
        .order_by("-agent_count", "name", "version", "id")
    )
    return [
        {
            "id": str(m.id),
            "name": m.name,
            "version": m.version,
            "modality": m.modality,
            "hosting_region": m.hosting_region,
            "provider": {"id": str(m.provider_id), "name": m.provider.name},
            "agents": m.agent_count,
            "live_agents": m.live_count,
        }
        for m in rows
    ]
