# Azure Resource Manager fixtures

Read on Microsoft Learn on 2026-11-11. Replayed through `discovery/azure.py` and `discovery/graph_source.Client` by
`tests/test_vendor_fixtures.py`.

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `subscriptions_list.json` | https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/list?view=rest-resources-2022-12-01 | OFFICIAL | Learn's `nextLink` is the placeholder `http://nextlink`; replaced with a synthetic https URL on `management.azure.com`. GUIDs sanitized. |
| `logic_workflows_list.json` | https://learn.microsoft.com/en-us/rest/api/logic/workflows/list-by-subscription?view=rest-logic-2019-05-01 | OFFICIAL | No top-level `nextLink` in Learn's sample (the ARM list schema allows one). IP addresses replaced with 192.0.2.x. |
| `web_sites_list.json` | https://learn.microsoft.com/en-us/rest/api/appservice/web-apps/list?view=rest-appservice-2024-04-01 | OFFICIAL item 1 (shortened); items 2 and 3 SYNTH | Item 1 is Learn's 2024-04-01 sample, `siteConfig` shortened. Items 2 (`kind` `functionapp,linux`) and 3 (`kind` `functionapp,workflowapp`) are invented: Learn has no function-app or Standard Logic App list item. `nextLink` is in the schema table, not in the sample. |
| `automation_accounts_list.json` | https://learn.microsoft.com/en-us/rest/api/automation/automation-account/list?view=rest-automation-2024-10-23 | OFFICIAL | api-version 2024-10-23 exists for the account list. Subscription GUID sanitized. |
| `automation_runbooks_list.json` | https://learn.microsoft.com/en-us/rest/api/automation/runbook/list-by-automation-account?view=rest-automation-2024-10-23 | OFFICIAL | Learn's sample has no `properties.description`. Subscription GUID sanitized. |
| `error_response_schema.json` | https://learn.microsoft.com/en-us/rest/api/resources/subscriptions/list?view=rest-resources-2022-12-01 (Definitions: ErrorResponse) | OFFICIAL (schema) | The ErrorResponse/CloudError definition with its type names as values. No literal AuthorizationFailed, SubscriptionNotFound or MissingSubscriptionRegistration body was found on any page read. |
| `throttling_429.headers.json` | https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/request-limits-and-throttling | headers only | Retry-After is in seconds (the value is Learn's placeholder `<seconds>`). Documented header names: `x-ms-ratelimit-remaining-subscription-(deletes\|reads\|writes)`, `-tenant-(reads\|writes)`, `-subscription-resource-requests`, `-subscription-resource-entities-read`. No 429 body is documented. |
