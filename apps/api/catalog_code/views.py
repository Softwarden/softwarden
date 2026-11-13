"""Catalog-as-code routes (Admin; capability catalog_code). docs/CATALOG_AS_CODE.md has the format.

GET  catalog-code/export.zip   the catalog as a zip of YAML files (?include_people=true adds People); audited,
                               refused while platform staff impersonate (bulk data leaving the organization)
POST catalog-code/plan         multipart: file (a zip of the tree), prune, allow_mass_delete; what applying would do.
                               Writes nothing.
POST catalog-code/apply        multipart: file, prune, allow_mass_delete, plan_hash (from the plan); applies it in one
                               transaction, or
                               nothing: 400 /problems/catalog-code-invalid when a file has an error, 409
                               /problems/catalog-code-stale when the catalog or the files changed since the plan"""

from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from core.capabilities import CATALOG_CODE
from core.drf import ADMIN, PROBLEMS, ProblemSerializer, RoleAPIView, policy, refuse_unknown_params
from core.openapi import PROBLEM_OR_LIMIT
from core.parsers import MultiPartParser
from core.problems import FieldErrors

from . import bundle, engine

TRUE = ("1", "true", "yes")


class TreeUploadSerializer(serializers.Serializer):
    file = serializers.FileField(
        help_text=f"A zip of the tree's YAML files (as export.zip gives, or a repository's zip), up to "
        f"{bundle.MAX_UPLOAD_BYTES // bundle.MB} MB, {bundle.MAX_FILES:,} YAML files and "
        f"{bundle.MAX_TOTAL_BYTES // bundle.MB} MB uncompressed. Other files are ignored."
    )
    prune = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Also delete records of the tree's kinds (catalog.yaml's `kinds`, else the kinds it has files for) "
        "that have no file. People and tags are never deleted.",
    )
    allow_mass_delete = serializers.BooleanField(
        required=False,
        default=False,
        help_text=f"With prune: allow deleting more than {engine.MASS_DELETE_MAX} records in all, more than "
        f"{engine.MASS_DELETE_SHARE:.0%} of a kind, or all of a kind. Without it such a prune is an error naming the "
        "count, so nothing is applied (a partial checkout or a truncated zip would delete what it is missing).",
    )


class ApplyUploadSerializer(TreeUploadSerializer):
    plan_hash = serializers.CharField(help_text="The plan_hash of the plan you reviewed.")


class ChangeFieldSerializer(serializers.Serializer):
    path = serializers.CharField(help_text="Where in the file: spec.lifecycle, metadata.tags, spec.instances[Prod].")
    before = serializers.JSONField(allow_null=True, help_text="The value now, as the file would write it; null: none.")
    after = serializers.JSONField(allow_null=True, help_text="The value the file gives; null: none.")


class ChangeSerializer(serializers.Serializer):
    file = serializers.CharField(allow_null=True, help_text="The file; null for a delete (prune).")
    kind = serializers.CharField(help_text="Application, Component, AIAgent, Integration, Relationship, Vendor, ...")
    key = serializers.CharField(help_text="The record's natural key: application:crm, a vendor's name, ...")
    action = serializers.ChoiceField(["create", "update", "delete"])
    fields = ChangeFieldSerializer(many=True)  # type: ignore[assignment]  # the member, not Serializer.fields


class IssueSerializer(serializers.Serializer):
    file = serializers.CharField(allow_null=True, help_text="The file; null for a problem with the whole tree.")
    line = serializers.IntegerField(allow_null=True, help_text="1-based line in the file, when there is one.")
    path = serializers.CharField(allow_null=True, help_text="The field: spec.lifecycle, metadata.slug, ...")
    message = serializers.CharField()
    mass_delete = serializers.IntegerField(
        required=False,
        help_text="Only on the prune cap's error: how many records the prune would delete (send allow_mass_delete).",
    )


class SummarySerializer(serializers.Serializer):
    # Members named like Serializer methods: the response's names, not create()/update()/errors.
    create = serializers.IntegerField()  # type: ignore[assignment]
    update = serializers.IntegerField()  # type: ignore[assignment]
    delete = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    errors = serializers.IntegerField()  # type: ignore[assignment]
    files = serializers.IntegerField(help_text="YAML files read.")


class PlanSerializer(serializers.Serializer):
    plan_hash = serializers.CharField(
        help_text="SHA-256 of the catalog as it is now, the files and prune. Send it to apply: if either changed, "
        "apply refuses (409 /problems/catalog-code-stale)."
    )
    prune = serializers.BooleanField()
    applied = serializers.BooleanField(help_text="Whether this request wrote the changes (only apply does).")
    summary = SummarySerializer()
    changes = ChangeSerializer(many=True, help_text="Every create, update and delete; unchanged records are counted.")
    errors = IssueSerializer(  # type: ignore[assignment]  # the member, not Serializer.errors
        many=True, help_text="Problems that stop apply, each with its file and line."
    )
    warnings = IssueSerializer(many=True, help_text="What apply would leave as it is (an instance without prune).")


def _files(request):
    upload = request.FILES.get("file")
    if upload is None:
        raise FieldErrors({"file": ["Choose a zip of the tree's YAML files."]})
    try:
        return bundle.read_zip(upload)
    except bundle.BundleRejected as e:
        raise FieldErrors({"file": [e.message]}) from None


def _flag(request, name: str) -> bool:
    value = request.data.get(name)
    if value in (None, ""):
        return False
    if str(value).lower() in (*TRUE, "0", "false", "no"):
        return str(value).lower() in TRUE
    raise FieldErrors({name: ["Use true or false."]})


class CatalogCodeExportView(RoleAPIView):
    role_policy = policy(ADMIN, ADMIN, capability=CATALOG_CODE)

    def perform_content_negotiation(self, request, force=False):
        return super().perform_content_negotiation(request, force=True)  # the zip, whatever the client accepts

    @extend_schema(
        summary="Export the catalog as YAML files (Admin)",
        description="A zip of one YAML file per record (docs/CATALOG_AS_CODE.md): the same records always give the "
        "same bytes. Never holds secrets, connector credentials, tokens or attachment files (only their metadata). "
        "People's names and emails only with include_people=true; without it owners are named by Softwarden ID. "
        "Audited; refused while platform staff impersonate.",
        parameters=[
            OpenApiParameter(
                "include_people", OpenApiTypes.BOOL, description="Add People files and name owners by email."
            )
        ],
        responses={
            (200, "application/zip"): OpenApiResponse(OpenApiTypes.BINARY, description="The tree as a zip"),
            400: PROBLEMS[400],
            403: PROBLEMS[403],
        },
    )
    def get(self, request):
        refuse_unknown_params(request, ("include_people",))
        raw = request.query_params.get("include_people", "")
        if raw.lower() not in ("", *TRUE, "0", "false", "no"):
            raise FieldErrors({"include_people": ["Use true or false."]})
        files = engine.export(self.organization, request.user, include_people=raw.lower() in TRUE, interface="api")
        response = HttpResponse(bundle.write_zip(files), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="softwarden-catalog-{self.organization.slug}.zip"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


class _TreeView(RoleAPIView):
    role_policy = policy(ADMIN, ADMIN, capability=CATALOG_CODE)
    parser_classes = [MultiPartParser]


class CatalogCodePlanView(_TreeView):
    @extend_schema(
        summary="Plan applying a catalog-as-code tree (Admin)",
        description="Reads the zip, matches every file to a record by its natural key and compares it field by field. "
        "Each change is tried through the same service functions apply uses, then rolled back, so `errors` are "
        "the ones apply would meet. Writes nothing. A tree that can't be read (not a zip, an unsafe path, a "
        "symbolic link, over a size limit) is a 400, and so is a tree that would create more than "
        f"{engine.API_MAX_CREATES:,} rows (apply a first load that large with manage.py catalog_apply).",
        request={"multipart/form-data": TreeUploadSerializer},
        responses={200: PlanSerializer, 400: PROBLEMS[400], 403: PROBLEMS[403]},
    )
    def post(self, request):
        files = _files(request)
        result = engine.execute(
            self.organization,
            request.user,
            files,
            prune=_flag(request, "prune"),
            max_creates=engine.API_MAX_CREATES,
            allow_mass_delete=_flag(request, "allow_mass_delete"),
        )
        return Response(PlanSerializer(result).data)


class CatalogCodeApplyView(_TreeView):
    @extend_schema(
        summary="Apply a catalog-as-code tree (Admin)",
        description="Applies the tree in one transaction through each record's service function (validation and an "
        "audit event per record, marked via catalog_code), or nothing. Send the plan_hash of the plan you reviewed: "
        "409 /problems/catalog-code-stale when the catalog or the files changed since. 400 "
        "/problems/catalog-code-invalid with `file_errors` when a file has an error; 409 /problems/limit-reached "
        "when the records it creates would pass the organization's limit.",
        request={"multipart/form-data": ApplyUploadSerializer},
        responses={
            200: PlanSerializer,
            400: PROBLEMS[400],
            403: PROBLEMS[403],
            409: OpenApiResponse(ProblemSerializer, description=f"Stale plan, or a limit ({PROBLEM_OR_LIMIT})"),
        },
    )
    def post(self, request):
        files = _files(request)
        expected = request.data.get("plan_hash")
        if not isinstance(expected, str) or not expected.strip():
            raise FieldErrors({"plan_hash": ["Send the plan_hash of the plan you reviewed."]})
        result = engine.execute(
            self.organization,
            request.user,
            files,
            prune=_flag(request, "prune"),
            apply=True,
            expected_hash=expected.strip(),
            max_creates=engine.API_MAX_CREATES,
            allow_mass_delete=_flag(request, "allow_mass_delete"),
        )
        if not result["applied"]:
            raise engine.InvalidTree(result)
        return Response(PlanSerializer(result).data)
