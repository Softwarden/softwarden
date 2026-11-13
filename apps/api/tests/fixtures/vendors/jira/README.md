# Jira Cloud fixtures

Read on 2026-11-11 from the Jira Cloud REST v3 reference and its swagger
(https://dac-static.atlassian.com/cloud/jira/platform/swagger-v3.v3.json). Replayed through `tickets/clients.py` by
`tests/test_vendor_fixtures.py`.

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `issue_get_official_example.json` | https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/ (getIssue, 200 example) | OFFICIAL | Atlassian's example as published. It has **no `fields.status` and no `fields.summary`**, the two fields Softwarden asks for. |
| `issue_get_fields_status_summary.json` | the same swagger, schemas StatusDetails and StatusCategory | COMPOSED | What `?fields=status,summary` returns, built from the schemas. The category keys `new`, `indeterminate` and `done` are not enumerated in the swagger (StatusCategory.key is a free string). |
| `create_issue_201.json` | the same swagger, createIssue 201 example | OFFICIAL | As published. |
| `error_400.json` | the same swagger, createIssue 400 example | OFFICIAL | As published: `{errorMessages, errors}`, not a Graph-style error object. |
| `error_schema_ErrorCollection.json` | the same swagger, schema ErrorCollection | OFFICIAL (schema) | Type names as values. |
