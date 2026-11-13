"""API acceptance: the application and review endpoints are described in OpenAPI."""

import json


def test_schema_documents_applications_and_reviews(a, api):
    assert api().get("schema?format=json").status_code in (401, 403)  # signed-in users only
    response = api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json")
    assert response.status_code == 200
    doc = json.loads(response.content)
    paths = doc["paths"]
    create = paths["/api/v1/applications"]["post"]
    body = doc["components"]["schemas"]["ApplicationInputRequest"]
    assert set(body["required"]) == {"name", "lifecycle"}
    assert {"201", "400", "403", "409"} <= set(create["responses"])
    review = paths["/api/v1/applications/{id}/reviews"]["post"]
    assert review["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("ReviewInputRequest")
    assert "cursor" in {p["name"] for p in paths["/api/v1/applications"]["get"]["parameters"]}
    # Every endpoint is at least listed.
    assert {"/api/v1/people", "/api/v1/needs-attention", "/api/v1/audit-events"} <= set(paths)


def test_schema_documents_relationships(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths = doc["paths"]
    create = paths["/api/v1/relationships"]["post"]
    assert create["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("RelationshipInputRequest")
    assert {"201", "400", "403", "409"} <= set(create["responses"])
    assert {"resource", "type"} <= {p["name"] for p in paths["/api/v1/relationships"]["get"]["parameters"]}
    detail = paths["/api/v1/relationships/{id}"]
    assert {"get", "patch", "delete"} <= set(detail)
    assert {"204", "404"} <= set(detail["delete"]["responses"])
    schemas = doc["components"]["schemas"]
    assert set(schemas["RelationshipInputRequest"]["required"]) == {"type", "source", "target"}
    assert {"source", "target", "purpose", "created_by"} <= set(schemas["Relationship"]["properties"])


def test_schema_documents_integrations(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths = doc["paths"]
    create = paths["/api/v1/integrations"]["post"]
    assert create["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("IntegrationInputRequest")
    assert {"201", "400", "403", "409"} <= set(create["responses"])
    filters = {p["name"] for p in paths["/api/v1/integrations"]["get"]["parameters"]}
    assert {"source", "target", "protocol", "implemented", "owner", "q", "cursor"} <= filters
    detail = paths["/api/v1/integrations/{id}"]
    assert {"get", "patch", "delete"} <= set(detail)
    assert {"204", "404", "409"} <= set(detail["delete"]["responses"])
    assert "post" in paths["/api/v1/integrations/{id}/archive"] and "post" in paths["/api/v1/integrations/{id}/restore"]
    schemas = doc["components"]["schemas"]
    assert set(schemas["IntegrationInputRequest"]["required"]) == {"name", "lifecycle", "source", "target"}
    assert {"source", "target", "vendor_native", "implemented_by", "protocol"} <= set(
        schemas["Integration"]["properties"]
    )
    assert "criticality" not in schemas["Integration"]["properties"]


def test_schema_documents_confirmations(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    for url in ("/api/v1/relationships/{id}/confirmations", "/api/v1/integrations/{id}/confirmations"):
        create = paths[url]["post"]
        assert create["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "ConfirmationInputRequest"
        )
        assert {"201", "400", "403", "404"} <= set(create["responses"])
        assert "200" in paths[url]["get"]["responses"]
    assert set(schemas["ConfirmationInputRequest"]["required"]) == {"confirmed_by", "evidence"}
    assert {"confirmed_signature", "confirmed_by", "recorded_by", "confirmed_on", "evidence"} <= set(
        schemas["Confirmation"]["properties"]
    )


def test_schema_documents_link_state(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    schemas = doc["components"]["schemas"]
    for name in ("Relationship", "Integration"):
        assert {"link_state", "link_why", "last_confirmed_on"} <= set(schemas[name]["properties"])
    states = (
        schemas["LinkStateEnum"] if "LinkStateEnum" in schemas else schemas["Relationship"]["properties"]["link_state"]
    )
    assert "needs_verification" in json.dumps(states)


def test_schema_documents_dependencies(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    get = doc["paths"]["/api/v1/dependencies"]["get"]
    params = {p["name"]: p for p in get["parameters"]}
    assert {"target", "direction", "depth", "include_retired", "show_instances", "data_flows"} <= set(params)
    assert params["target"]["required"] is True
    assert {"200", "400", "403", "404"} <= set(get["responses"])
    body = doc["components"]["schemas"]["Dependencies"]
    assert {"nodes", "data_flows", "total", "shown", "capped", "summary"} <= set(body["properties"])
    node = doc["components"]["schemas"]["DependencyNode"]
    assert {"path", "cycle", "depth", "link", "technical_owner"} <= set(node["properties"])


def test_schema_documents_assessments(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    listing = paths["/api/v1/assessments"]
    assert {"status", "stale", "change_type", "target", "owner", "q"} <= {
        p["name"] for p in listing["get"]["parameters"]
    }
    assert listing["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "AssessmentInputRequest"
    )
    assert {"201", "400", "403"} <= set(listing["post"]["responses"])
    assert set(schemas["AssessmentInputRequest"]["required"]) == {"target", "change_type", "title"}
    assert {"snapshot", "history", "status", "stale", "retirement_ack_reason"} <= set(
        schemas["Assessment"]["properties"]
    )
    assert {"nodes", "links", "stale_reasons", "counts"} <= set(schemas["Snapshot"]["properties"])
    assert {"role", "path", "in_cycle", "lifecycle_then", "lifecycle_now", "owners_then", "changed"} <= set(
        schemas["SnapshotNode"]["properties"]
    )
    for url, method in (
        ("/api/v1/assessments/{id}", "get"),
        ("/api/v1/assessments/{id}", "patch"),
        ("/api/v1/assessments/{id}/status", "post"),
        ("/api/v1/assessments/{id}/refresh", "post"),
        ("/api/v1/assessments/{id}/snapshots", "get"),
        ("/api/v1/assessments/{id}/snapshots/{snapshot_pk}", "get"),
    ):
        assert {"403", "404"} <= set(paths[url][method]["responses"]), (url, method)


def test_schema_documents_the_retirement_acknowledgment(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    schemas = doc["components"]["schemas"]
    for name in ("ApplicationInputRequest", "AssessmentStatusInputRequest", "AssessmentInputRequest"):
        assert {"retirement_ack", "retirement_ack_reason"} <= set(schemas[name]["properties"]), name
    assert {"dependents", "not_required"} <= set(schemas["Problem"]["properties"])
    assert "retirement" in doc["paths"]["/api/v1/applications/{id}"]["patch"]["responses"]["409"]["description"]


def test_schema_documents_needs_attention(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    get = doc["paths"]["/api/v1/needs-attention"]["get"]
    assert "scope" in {p["name"] for p in get["parameters"]}
    assert {"200", "400", "403"} <= set(get["responses"])
    props = doc["components"]["schemas"]["Attention"]["properties"]
    assert {"counts", "ownership_gaps", "reviews", "links_to_verify", "stale_assessments"} <= set(props)
    assert {"why", "steward", "last_confirmed_on", "link_kind"} <= set(
        doc["components"]["schemas"]["AttentionLinkRow"]["properties"]
    )


# ---- Every dependency endpoint has request and response bodies --------------------------------------------------------

DEPENDENCY_PREFIXES = (
    "/api/v1/relationships",
    "/api/v1/integrations",
    "/api/v1/dependencies",
    "/api/v1/assessments",
    "/api/v1/needs-attention",
)
DEPENDENCY_AUDIT = (
    "/api/v1/applications/{id}/audit",
    "/api/v1/components/{id}/audit",
    "/api/v1/integrations/{id}/audit",
    "/api/v1/audit-events",
)
# Actions that take no request body: the verb is the whole request.
NO_BODY = {
    ("post", "/api/v1/integrations/{id}/archive"),
    ("post", "/api/v1/integrations/{id}/restore"),
    ("post", "/api/v1/assessments/{id}/refresh"),
    ("post", "/api/v1/connectors/{id}/access-check"),  # the check needs nothing but the connector
    ("post", "/api/v1/assessments/{id}/ticket-links/{link_pk}/refresh"),  # reads the ticket's status
}
DEPENDENCY_OPERATIONS = {
    ("get", "/api/v1/relationships"),
    ("post", "/api/v1/relationships"),
    ("get", "/api/v1/relationships/{id}"),
    ("patch", "/api/v1/relationships/{id}"),
    ("delete", "/api/v1/relationships/{id}"),
    ("get", "/api/v1/relationships/{id}/confirmations"),
    ("post", "/api/v1/relationships/{id}/confirmations"),
    ("get", "/api/v1/relationships/{id}/confirmations/{confirmation_pk}"),  # DRF layer B: retrieve
    ("get", "/api/v1/integrations"),
    ("post", "/api/v1/integrations"),
    ("get", "/api/v1/integrations/{id}"),
    ("patch", "/api/v1/integrations/{id}"),
    ("delete", "/api/v1/integrations/{id}"),
    ("post", "/api/v1/integrations/{id}/archive"),
    ("post", "/api/v1/integrations/{id}/restore"),
    ("get", "/api/v1/integrations/{id}/confirmations"),
    ("post", "/api/v1/integrations/{id}/confirmations"),
    ("get", "/api/v1/integrations/{id}/confirmations/{confirmation_pk}"),  # DRF layer B: retrieve
    ("get", "/api/v1/integrations/{id}/audit"),
    ("get", "/api/v1/integrations/{id}/audit/{event_pk}"),
    ("get", "/api/v1/integrations/{id}/attachments"),  # attachments
    ("post", "/api/v1/integrations/{id}/attachments"),
    ("get", "/api/v1/dependencies"),
    ("get", "/api/v1/assessments"),
    ("post", "/api/v1/assessments"),
    ("get", "/api/v1/assessments/{id}"),
    ("patch", "/api/v1/assessments/{id}"),
    ("post", "/api/v1/assessments/{id}/status"),
    ("post", "/api/v1/assessments/{id}/refresh"),
    ("get", "/api/v1/assessments/{id}/snapshots"),
    ("get", "/api/v1/assessments/{id}/snapshots/{snapshot_pk}"),
    ("get", "/api/v1/assessments/{id}/ticket-links"),  # ticket links
    ("post", "/api/v1/assessments/{id}/ticket-links"),
    ("post", "/api/v1/assessments/{id}/ticket-links/create-ticket"),
    ("get", "/api/v1/assessments/{id}/ticket-links/{link_pk}"),
    ("patch", "/api/v1/assessments/{id}/ticket-links/{link_pk}"),
    ("delete", "/api/v1/assessments/{id}/ticket-links/{link_pk}"),
    ("post", "/api/v1/assessments/{id}/ticket-links/{link_pk}/refresh"),
    ("get", "/api/v1/needs-attention"),
    ("get", "/api/v1/applications/{id}/audit"),
    ("get", "/api/v1/components/{id}/audit"),
    ("get", "/api/v1/audit-events"),
}


def json_schema(content):
    schema = (content or {}).get("application/json", {}).get("schema")
    return schema if schema else None


def test_every_dependency_endpoint_has_request_and_response_bodies(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    found = {}
    for path, operations in doc["paths"].items():
        if (path.startswith(DEPENDENCY_PREFIXES) and not path.startswith(RENEWAL_PREFIXES)) or path in DEPENDENCY_AUDIT:
            for method, op in operations.items():
                if method in ("get", "post", "patch", "put", "delete"):
                    found[method, path] = op
    assert set(found) == DEPENDENCY_OPERATIONS  # a new or removed endpoint here changes this list on purpose
    assert body_problems(found) == []


def body_problems(found):
    problems = []
    for (method, path), op in sorted(found.items()):
        label = f"{method.upper()} {path}"
        success = [code for code in op["responses"] if code.startswith("2")]
        if not success:
            problems.append(f"{label}: no success response")
        for code in success:
            if code != "204" and not json_schema(op["responses"][code].get("content")):
                problems.append(f"{label}: {code} has no response body schema")
        if (
            method in ("post", "patch")
            and (method, path) not in NO_BODY
            and not json_schema(op.get("requestBody", {}).get("content"))
        ):
            problems.append(f"{label}: no request body schema")
        for code in ("400", "403", "404"):
            body = op["responses"].get(code, {}).get("content", {})
            if "application/json" not in body and "application/problem+json" not in body:
                problems.append(f"{label}: {code} is not described as a problem response")
    return problems


# ---- Every renewal endpoint has request and response bodies -----------------------------------------------------------

RENEWAL_PREFIXES = ("/api/v1/renewals", "/api/v1/needs-attention/snoozes", "/api/v1/admin/digests", "/api/v1/auth/me/")
NO_BODY |= {
    ("post", "/api/v1/renewals/{id}/archive"),
    ("post", "/api/v1/renewals/{id}/restore"),
    ("post", "/api/v1/admin/digests/{id}/retry"),
    ("post", "/api/v1/renewals/{id}/ticket-links/{link_pk}/refresh"),  # reads the ticket's status
}
RENEWAL_OPERATIONS = {
    ("get", "/api/v1/renewals"),
    ("post", "/api/v1/renewals"),
    ("get", "/api/v1/renewals/preview"),
    ("get", "/api/v1/renewals/{id}"),
    ("patch", "/api/v1/renewals/{id}"),
    ("delete", "/api/v1/renewals/{id}"),
    ("post", "/api/v1/renewals/{id}/archive"),
    ("post", "/api/v1/renewals/{id}/restore"),
    ("get", "/api/v1/renewals/{id}/audit"),
    ("get", "/api/v1/renewals/{id}/audit/{event_pk}"),
    ("post", "/api/v1/renewals/{id}/cycles"),
    ("patch", "/api/v1/renewals/{id}/cycles/{cycle_pk}"),
    ("get", "/api/v1/renewals/{id}/ticket-links"),  # ticket links
    ("post", "/api/v1/renewals/{id}/ticket-links"),
    ("post", "/api/v1/renewals/{id}/ticket-links/create-ticket"),
    ("get", "/api/v1/renewals/{id}/ticket-links/{link_pk}"),
    ("patch", "/api/v1/renewals/{id}/ticket-links/{link_pk}"),
    ("delete", "/api/v1/renewals/{id}/ticket-links/{link_pk}"),
    ("post", "/api/v1/renewals/{id}/ticket-links/{link_pk}/refresh"),
    ("post", "/api/v1/renewals/{id}/cycles/{cycle_pk}/decision"),
    ("post", "/api/v1/renewals/{id}/cycles/{cycle_pk}/completion"),
    ("get", "/api/v1/auth/me/preferences"),
    ("patch", "/api/v1/auth/me/preferences"),
    ("post", "/api/v1/auth/me/organization"),  # switch the current organization
    ("get", "/api/v1/needs-attention/snoozes"),  # DRF layer B: list and retrieve
    ("post", "/api/v1/needs-attention/snoozes"),
    ("get", "/api/v1/needs-attention/snoozes/{id}"),
    ("delete", "/api/v1/needs-attention/snoozes/{id}"),
    ("get", "/api/v1/admin/digests"),
    ("get", "/api/v1/admin/digests/{id}"),  # DRF layer B: retrieve
    ("post", "/api/v1/admin/digests/{id}/retry"),
}


def test_every_renewal_endpoint_has_request_and_response_bodies(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    found = {
        (method, path): op
        for path, operations in doc["paths"].items()
        if path.startswith(RENEWAL_PREFIXES)
        for method, op in operations.items()
        if method in ("get", "post", "patch", "put", "delete")
    }
    assert set(found) == RENEWAL_OPERATIONS  # a new or removed endpoint here changes this list on purpose
    assert body_problems(found) == []


# ---- Every discovery endpoint has request and response bodies---------------------------------------------------------

DISCOVERY_PREFIXES = ("/api/v1/connectors", "/api/v1/discovery/", "/api/v1/assistant/")
DISCOVERY_SOURCES = {"/api/v1/applications/{id}/discovery-sources", "/api/v1/components/{id}/discovery-sources"}
NO_BODY |= {
    ("post", "/api/v1/connectors/{id}/run"),
    ("post", "/api/v1/discovery/observations/{id}/reopen"),
    ("post", "/api/v1/discovery/observations/{id}/create"),
}
DISCOVERY_OPERATIONS = {
    ("get", "/api/v1/connectors"),
    ("post", "/api/v1/connectors"),
    ("get", "/api/v1/connectors/{id}"),
    ("patch", "/api/v1/connectors/{id}"),
    ("delete", "/api/v1/connectors/{id}"),
    ("post", "/api/v1/connectors/{id}/access-check"),
    ("get", "/api/v1/discovery/credentials"),
    ("post", "/api/v1/connectors/{id}/run"),
    ("get", "/api/v1/connectors/{id}/runs"),
    ("get", "/api/v1/discovery/runs"),
    ("get", "/api/v1/connectors/{id}/runs/{run_pk}"),  # DRF layer C: retrieve
    ("get", "/api/v1/discovery/runs/{id}"),  # A run by its own ID
    ("get", "/api/v1/discovery/sources"),  # what each source needs before a connector exists
    ("get", "/api/v1/discovery/shadow-it"),  # shadow IT overview
    ("get", "/api/v1/discovery/shadow-it/export.csv"),
    ("get", "/api/v1/discovery/candidates"),
    ("get", "/api/v1/discovery/observations/{id}"),  # DRF layer C: retrieve
    ("post", "/api/v1/discovery/observations/{id}/link"),
    ("post", "/api/v1/discovery/observations/{id}/create"),
    ("post", "/api/v1/discovery/observations/{id}/dismiss"),
    ("post", "/api/v1/discovery/observations/{id}/reopen"),
    # Reconciliation: suggestions, source differs, no longer seen
    ("get", "/api/v1/discovery/observations/{id}/suggestions"),
    # Assistant (capability assistant)
    ("post", "/api/v1/discovery/observations/{id}/assistant/accept"),
    ("post", "/api/v1/discovery/observations/{id}/assistant/reject"),
    ("get", "/api/v1/assistant/settings"),
    ("patch", "/api/v1/assistant/settings"),
    ("get", "/api/v1/assistant/usage"),
    ("get", "/api/v1/assistant/batches"),
    ("post", "/api/v1/assistant/batches"),
    ("get", "/api/v1/assistant/batches/{id}"),
    ("get", "/api/v1/discovery/observations/{id}/drift"),
    ("post", "/api/v1/discovery/observations/{id}/drift/apply"),
    ("post", "/api/v1/discovery/observations/{id}/drift/ignore"),
    ("post", "/api/v1/discovery/observations/{id}/missing/acknowledge"),
    ("post", "/api/v1/discovery/observations/{id}/missing/archive"),
    ("get", "/api/v1/applications/{id}/discovery-sources"),
    ("get", "/api/v1/components/{id}/discovery-sources"),
}


def test_every_discovery_endpoint_has_request_and_response_bodies(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    found = {
        (method, path): op
        for path, operations in doc["paths"].items()
        if path.startswith(DISCOVERY_PREFIXES) or path in DISCOVERY_SOURCES
        for method, op in operations.items()
        if method in ("get", "post", "patch", "put", "delete")
    }
    assert set(found) == DISCOVERY_OPERATIONS  # a new or removed endpoint here changes this list on purpose
    # A CSV download answers text/csv, not JSON (as the part C CSV routes).
    assert body_problems({k: v for k, v in found.items() if not k[1].endswith(".csv")}) == []


def test_schema_documents_first_run_setup(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    setup = doc["paths"]["/api/v1/setup"]
    assert {"get", "post"} <= set(setup)
    assert setup["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("SetupInputRequest")
    assert {"200", "403", "409", "429"} <= set(setup["post"]["responses"])
    assert set(doc["components"]["schemas"]["SetupInputRequest"]["required"]) >= {"token", "email", "password"}


# ---- viewsets: generated from serializers, FilterSets and the pagination class ------------------


def test_resource_viewsets_are_described_from_their_serializers(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    shared = {"q", "lifecycle", "criticality", "owner", "support_team", "it_steward", "gap", "review_state"}
    shared |= {"archived", "imported", "tag", "cursor", "page_size", "ordering"}
    # Department, data classification and the compliance profile's filters.
    shared |= {"department", "data_classification", "is_gxp", "validation_status", "risk_tier", "periodic_review_due"}
    shared |= {"completeness__lt", "completeness__gte", "quality_rule"}  # data quality
    application = {"vendor", "category", "deployment_model", "authentication_method", "sso_enabled"}
    application |= {"scim_provisioning"}
    for plural, noun, own in (
        ("applications", "Application", application),
        ("components", "Component", {"component_type", "hosting", "environment"}),
    ):
        listing = paths[f"/api/v1/{plural}"]
        assert {p["name"] for p in listing["get"]["parameters"]} == shared | own
        page = schemas[f"Paginated{noun}List"]["properties"]
        assert set(page) == {"results", "next", "previous"} and page["results"]["items"]["$ref"].endswith(f"/{noun}")
        assert listing["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
            f"{noun}InputRequest"
        )
        assert listing["post"]["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith(noun)
        detail = paths[f"/api/v1/{plural}/{{id}}"]
        assert detail["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
            f"{noun}Detail"
        )
        for method in ("patch", "delete"):
            assert "If-Match" in {p["name"] for p in detail[method]["parameters"]}, (plural, method)
            assert {"403", "404", "409", "412"} <= set(detail[method]["responses"]), (plural, method)
        for action in ("archive", "restore"):
            assert {"200", "403", "404"} <= set(paths[f"/api/v1/{plural}/{{id}}/{action}"]["post"]["responses"])
        assert {"archived_at", "archived_by"} <= set(schemas[f"{noun}Detail"]["properties"])
    assert "instances" in schemas["ApplicationDetail"]["properties"]
    assert "instances" not in schemas["ComponentDetail"]["properties"]
    assert set(schemas["ComponentInputRequest"]["required"]) == {"name", "lifecycle"}
    assert "revision" in schemas["Problem"]["properties"]


# ---- DRF layer C: accounts, tokens, imports, audit and sign-in ----------------------------------------------------------

C_OPERATIONS = {
    ("get", "/api/v1/users"),
    ("post", "/api/v1/users"),
    ("get", "/api/v1/users/{id}"),
    ("patch", "/api/v1/users/{id}"),
    ("delete", "/api/v1/users/{id}"),
    ("post", "/api/v1/users/{id}/reset-password"),
    ("delete", "/api/v1/users/{id}/identity"),
    ("delete", "/api/v1/users/{id}/identities/{identity_id}"),
    ("get", "/api/v1/organization"),
    ("patch", "/api/v1/organization"),
    # The organization's data export and its deletion
    ("get", "/api/v1/organization/exports"),
    ("post", "/api/v1/organization/exports"),
    ("get", "/api/v1/organization/exports/{id}"),
    ("get", "/api/v1/organization/deletion"),
    ("post", "/api/v1/organization/deletion"),
    ("get", "/api/v1/organization/limits"),  # limits and usage
    ("delete", "/api/v1/organization/deletion"),
    ("post", "/api/v1/auth/password"),
    ("get", "/api/v1/api-tokens"),
    ("post", "/api/v1/api-tokens"),
    ("get", "/api/v1/api-tokens/{id}"),
    ("post", "/api/v1/api-tokens/{id}/revoke"),
    ("get", "/api/v1/import-sources"),
    ("post", "/api/v1/import-sources"),
    ("get", "/api/v1/import-sources/{id}"),
    ("patch", "/api/v1/import-sources/{id}"),
    ("delete", "/api/v1/import-sources/{id}"),
    ("get", "/api/v1/imports"),
    ("post", "/api/v1/imports"),
    ("get", "/api/v1/imports/{id}"),
    ("patch", "/api/v1/imports/{id}"),
    ("get", "/api/v1/imports/{id}/rows"),
    ("post", "/api/v1/imports/{id}/commit"),
    ("post", "/api/v1/imports/{id}/cancel"),
    ("post", "/api/v1/imports/{id}/rerun"),
    ("get", "/api/v1/audit-events"),
    ("get", "/api/v1/audit-events/{id}"),
    ("get", "/api/v1/system"),
    ("get", "/api/v1/auth/me"),
    ("post", "/api/v1/auth/login"),
    ("get", "/api/v1/auth/options"),
}
C_PREFIXES = ("/api/v1/users", "/api/v1/organization", "/api/v1/api-tokens", "/api/v1/import", "/api/v1/audit-events")
C_EXACT = {"/api/v1/auth/password", "/api/v1/system", "/api/v1/auth/me", "/api/v1/auth/login", "/api/v1/auth/options"}
# CSV downloads answer text/csv; the verbs below take no body.
CSV = {
    "/api/v1/imports/template.csv",
    "/api/v1/imports/{id}/errors.csv",
    "/api/v1/audit-events/export.csv",
    "/api/v1/audit-events/export.jsonl",
    "/api/v1/organization/exports/{id}/download",  # a zip, not JSON (tests/test_org_lifecycle.py)
}
NO_BODY |= {
    ("post", "/api/v1/users/{id}/reset-password"),
    ("post", "/api/v1/api-tokens/{id}/revoke"),
    ("post", "/api/v1/imports/{id}/commit"),
    ("post", "/api/v1/imports/{id}/cancel"),
    ("post", "/api/v1/imports/{id}/rerun"),
    ("post", "/api/v1/organization/exports"),  # asking needs nothing but the Admin
}


def test_every_part_c_endpoint_has_request_and_response_bodies(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    found = {
        (method, path): op
        for path, operations in doc["paths"].items()
        if (path.startswith(C_PREFIXES) or path in C_EXACT) and path not in CSV
        for method, op in operations.items()
        if method in ("get", "post", "patch", "put", "delete")
    }
    assert set(found) == C_OPERATIONS  # a new or removed endpoint changes this list on purpose
    # The upload is multipart, not JSON.
    upload = found.pop(("post", "/api/v1/imports"))
    assert "multipart/form-data" in upload["requestBody"]["content"]
    # Sign-in and the options answer before signing in, so they don't document 403/404 as a problem of their own.
    public = {k: found.pop(k) for k in [("post", "/api/v1/auth/login"), ("get", "/api/v1/auth/options")]}
    # The signed-in account, the System status and your password name no record, so they never answer 404.
    no_record = [
        ("get", "/api/v1/auth/me"),
        ("get", "/api/v1/system"),
        ("post", "/api/v1/auth/password"),
        ("get", "/api/v1/organization/deletion"),  # the current organization's state; no ID, no filters
        ("get", "/api/v1/organization/limits"),  # the current organization's limits; no ID, no filters
    ]
    for key in no_record:
        op = found.pop(key)
        assert "application/problem+json" in op["responses"]["403"]["content"], key
        assert "200" in op["responses"] or "204" in op["responses"], key
    assert body_problems(found) == []
    assert "setup_pending" in doc["components"]["schemas"]["AuthOptions"]["properties"]
    for op in public.values():
        assert json_schema(op["responses"]["200"]["content"])
    for method in ("patch", "delete"):
        assert "If-Match" in {p["name"] for p in doc["paths"]["/api/v1/users/{id}"][method]["parameters"]}
    assert "If-Match" in {p["name"] for p in doc["paths"]["/api/v1/organization"]["patch"]["parameters"]}
    audit = {p["name"] for p in doc["paths"]["/api/v1/audit-events"]["get"]["parameters"]}
    assert {"actor", "action", "from", "to", "cursor"} <= audit
    rows = {p["name"] for p in doc["paths"]["/api/v1/imports/{id}/rows"]["get"]["parameters"]}
    assert {"outcome", "q", "offset", "limit"} <= rows


def test_schema_documents_audit_depth(a, api):
    """The downloads take the log's filters and answer files; a record's History documents `children` and
    each row's `child`."""
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    log = {p["name"] for p in doc["paths"]["/api/v1/audit-events"]["get"]["parameters"]} - {"cursor"}
    for path, media in (("export.csv", "text/csv"), ("export.jsonl", "application/x-ndjson")):
        op = doc["paths"][f"/api/v1/audit-events/{path}"]["get"]
        assert {p["name"] for p in op["parameters"]} == log
        assert media in op["responses"]["200"]["content"]  # a file, declared as such
        assert {"400", "403"} <= set(op["responses"])
    assert "audit.export" in doc["paths"]["/api/v1/audit-events/export.jsonl"]["get"]["description"]
    history = doc["paths"]["/api/v1/applications/{id}/audit"]["get"]
    assert {"children", "cursor"} <= {p["name"] for p in history["parameters"]}
    row = doc["components"]["schemas"]["RecordAuditEvent"]["properties"]
    assert "child" in row
    child = doc["components"]["schemas"]["AuditChild"]["properties"]
    assert set(child) == {"kind", "kind_label", "id", "name", "sentence"}


def test_no_secret_is_a_documented_response_field(a, api):
    """Secrets are write-only. A connector's secret is in its input only; a token's secret is in the create
    response only; no schema has a hash, ciphertext or password field."""
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    schemas = doc["components"]["schemas"]
    for name, schema in schemas.items():
        props = set(schema.get("properties", {}))
        assert not props & {"token_hash", "secret_ciphertext", "licence_key_ciphertext", "password_hash"}, name
        if "secret" in props:
            # A staff account's TOTP secret, in its enrollment response only (shown once).
            assert name in WRITE_ONLY_SECRET | {"ApiTokenCreated", "ScimTokenCreated", "SecondFactorEnrollment"}, name
    for name in WRITE_ONLY_SECRET:
        assert schemas[name]["properties"]["secret"].get("writeOnly") is True, name


# Request bodies that take a secret to store it (connections; the connector's legacy credential fields).
WRITE_ONLY_SECRET = {
    "ConnectorInputRequest",
    "PatchedConnectorInputRequest",
    "MicrosoftConnectionInputRequest",
    "PatchedMicrosoftConnectionInputRequest",
    "HaloConnectionInputRequest",
    "PatchedHaloConnectionInputRequest",
    "TicketConnectionInputRequest",  # a Jira API token or a ServiceNow password
    "PatchedTicketConnectionInputRequest",
    # The Okta private key and the Google service account key file
    "OktaConnectionInputRequest",
    "PatchedOktaConnectionInputRequest",
    "GoogleWorkspaceConnectionInputRequest",
    "PatchedGoogleWorkspaceConnectionInputRequest",
}


def test_the_schema_generates_without_warnings_or_errors(tmp_path):
    """drf-spectacular guesses nothing: every view has serializers (0 errors) and no names collide (0 warnings)."""
    from django.core.management import call_command

    call_command("spectacular", "--validate", "--fail-on-warn", "--file", str(tmp_path / "schema.yaml"))


def test_schema_documents_limits_link_labels_row_codes_and_too_many_events(a, api):
    """Relationships' opt-in limit (array or {results, count, has_more}); snapshot link labels; bulk row codes; the
    audit export's too-many-events problem."""
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    schemas = doc["components"]["schemas"]
    op = doc["paths"]["/api/v1/relationships"]["get"]
    limit = next(p for p in op["parameters"] if p["name"] == "limit")
    assert "1 to 500" in limit["description"] and "{results, count, has_more}" in limit["description"]
    shape = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert shape["$ref"].endswith("/RelationshipList")
    choices = schemas["RelationshipList"]["oneOf"]
    assert {"type": "array", "items": {"$ref": "#/components/schemas/Relationship"}} in choices
    assert {"$ref": "#/components/schemas/RelationshipLimited"} in choices
    assert set(schemas["RelationshipLimited"]["required"]) == {"results", "count", "has_more"}
    link = schemas["SnapshotLink"]["properties"]
    assert link["type_name"]["nullable"] and link["inverse_name"]["nullable"]
    for name in ("BulkRow", "BulkRefusedRow"):
        assert schemas[name]["properties"]["codes"]["items"] == {"$ref": "#/components/schemas/BulkRowCodeEnum"}
    assert "retire_dependents" in schemas["BulkRowCodeEnum"]["enum"]
    refused = doc["paths"]["/api/v1/bulk-edit/apply"]["post"]["responses"]["400"]
    assert refused["content"]["application/problem+json"]["schema"]["$ref"].endswith("/BulkRefused")
    assert schemas["BulkRefused"]["properties"]["rows"]["items"]["$ref"].endswith("/BulkRefusedRow")
    problem = schemas["Problem"]["properties"]
    assert {"count", "limit"} <= set(problem) and "too-many-events" in problem["count"]["description"]
    assert "/problems/too-many-events" in doc["paths"]["/api/v1/audit-events/export.csv"]["get"]["description"]


def test_schema_documents_connection_tests_and_discovery_sources(a, api):
    """A connection test's 200 includes `result`; access checks and discovered credentials are described."""
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    for kind in ("microsoft", "halo"):
        ref = paths[f"/api/v1/connections/{kind}/{{id}}/test"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]
        name = ref["schema"]["$ref"].rsplit("/", 1)[1]
        assert "result" in schemas[name]["properties"]
        assert set(schemas["ProbeResult"]["properties"]) == {"ok", "problem", "problem_label", "code", "detail", "next"}
    assert "post" in paths["/api/v1/connectors/{id}/access-check"]
    assert {"access_check", "permissions"} <= set(schemas["Connector"]["properties"])
    assert {"state", "connector", "expires_within", "q"} <= {
        p["name"] for p in paths["/api/v1/discovery/credentials"]["get"]["parameters"]
    }
    assert {"credentials", "agent", "registration"} <= set(schemas["Observation"]["properties"])


# ---- Fit for client generation ----------------------------------------------------------------------------------------

FILES = ("text/csv", "application/x-ndjson", "text/calendar")


def operations(doc):
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            if method in ("get", "post", "patch", "put", "delete"):
                yield method, path, op


def untyped(schema) -> bool:
    """A body a generator can only type as `unknown`: `{}`, or an object with neither properties nor a value type."""
    if not schema:
        return True
    if "$ref" in schema or "oneOf" in schema or "allOf" in schema or "anyOf" in schema:
        return False
    if schema.get("type") == "array":
        return untyped(schema.get("items") or {})
    if schema.get("type") == "object":
        return not schema.get("properties") and not schema.get("additionalProperties")
    return "type" not in schema and "format" not in schema


def test_every_operation_is_typed_for_a_generated_client(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    ids, problems = [], []
    for method, path, op in operations(doc):
        label = f"{method.upper()} {path}"
        ids.append(op.get("operationId"))
        for code, response in op["responses"].items():
            content = response.get("content") or {}
            if code == "default" or code[0] in "45":
                schema = (content.get("application/problem+json") or {}).get("schema", {})
                if not schema.get("$ref", "").endswith(("/Problem", "/BulkRefused", "/ProblemOrLimitReached")):
                    problems.append(f"{label} {code}: not a problem+json Problem")
                if "application/json" in content:
                    problems.append(f"{label} {code}: an error declared as application/json")
            elif code.startswith("2") and code != "204":
                json_body = content.get("application/json")
                if json_body is None:
                    if not any(media.startswith(FILES) for media in content) and not content.get(
                        "application/octet-stream"
                    ):
                        if not (content or path.endswith((".csv", ".xlsx", ".jsonl", ".ics", "/download"))):
                            problems.append(f"{label} {code}: no body")
                elif untyped(json_body.get("schema")):
                    problems.append(f"{label} {code}: untyped body")
        for media, body in (op.get("requestBody") or {}).get("content", {}).items():
            if untyped(body.get("schema")):
                problems.append(f"{label}: untyped {media} request body")
    assert len(ids) == len(set(ids)) and None not in ids, "every operationId is unique"
    assert problems == [], "\n".join(problems)
    problem = doc["components"]["schemas"]["Problem"]
    assert set(problem["required"]) == {"type", "title", "status", "detail"}
    assert problem["additionalProperties"] is True
    assert "/problems/rate-limited (429)" in problem["properties"]["type"]["description"]


def test_every_enum_is_a_named_component(a, api):
    """Inline enums would each become an anonymous union in a generated client; named ones are shared types."""
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    inline = []

    def walk(schema, where):
        if not isinstance(schema, dict):
            return
        for name, prop in (schema.get("properties") or {}).items():
            if isinstance(prop, dict) and "enum" in prop:
                inline.append(f"{where}.{name}")
            walk(prop, f"{where}.{name}")
        for key in ("items", "additionalProperties"):
            walk(schema.get(key), f"{where}.{key}")
        for key in ("oneOf", "allOf", "anyOf"):
            for i, part in enumerate(schema.get(key) or []):
                walk(part, f"{where}.{key}[{i}]")

    for name, schema in doc["components"]["schemas"].items():
        walk(schema, name)
    assert inline == []
    assert {"LifecycleEnum", "RoleEnum", "LinkStateEnum", "CredentialRowKindEnum"} <= set(doc["components"]["schemas"])


def test_the_first_schema_build_runs_alone_and_warm_builds_overlap(monkeypatch):
    """Drf-spectacular loads its extension classes lazily without a lock, so two cold requests building the
    first schema at once raced (a 500). A process's first build runs alone; once one has finished, builds no longer
    wait for each other (warm requests don't queue)."""
    import threading
    import time

    from drf_spectacular.views import SpectacularAPIView

    from api import schema

    inside, most = [0], [0]
    guard = threading.Lock()

    def build(self, request, *args, **kwargs):
        with guard:
            inside[0] += 1
            most[0] = max(most[0], inside[0])
        time.sleep(0.05)
        with guard:
            inside[0] -= 1

    def together():
        most[0] = 0
        threads = [threading.Thread(target=schema.SchemaView().get, args=(None,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return most[0]

    monkeypatch.setattr(SpectacularAPIView, "get", build)
    monkeypatch.setattr(schema, "_built", threading.Event())  # a cold process
    assert together() == 1 and schema._built.is_set()
    assert together() > 1
