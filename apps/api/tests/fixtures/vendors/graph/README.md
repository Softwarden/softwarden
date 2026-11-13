# Microsoft Graph fixtures

Read on Microsoft Learn on 2026-11-11. Replayed through `discovery/entra.py`, `discovery/entra_licenses.py`,
`discovery/graph_source.Client` and `core/graph.py` by `tests/test_vendor_fixtures.py`.

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `applications_list_page1.json`, `applications_list_page2.json` | https://learn.microsoft.com/en-us/graph/api/application-list?view=graph-rest-1.0 ; https://learn.microsoft.com/en-us/graph/api/resources/application?view=graph-rest-1.0 | COMPOSED | Learn's list samples are "shortened" (appId, identifierUris, displayName, publisherDomain, signInAudience; Example 6 shows `@odata.context`, `@odata.nextLink` with `$skiptoken` and `@microsoft.graph.tips`). `id`, `web.homePageUrl`, `tags` and `passwordCredentials` (keyId, displayName, endDateTime, hint, secretText) come from the resource page. The expanded `owners` shape is SYNTH. Page 2 has no next link. |
| `servicePrincipals_list.json` | https://learn.microsoft.com/en-us/graph/api/serviceprincipal-list?view=graph-rest-1.0 ; https://learn.microsoft.com/en-us/graph/api/resources/serviceprincipal?view=graph-rest-1.0 | COMPOSED | Learn's sample is one item (accountEnabled, displayName, servicePrincipalType, signInAudience; no id or appId). Three items built from the resource page; `publisherName` appears on Learn only as a `$select` value in the list page's Example 4, not in the resource's property table. The second item's `appOwnerOrganizationId` is a synthetic third-party tenant. |
| `subscribedSkus_list.json` | https://learn.microsoft.com/en-us/graph/api/subscribedsku-list?view=graph-rest-1.0 | OFFICIAL | Learn's two-item sample; GUIDs and account names sanitized. Only `$select` is supported and no paging is shown. |
| `error_429_throttling.json`, `error_429_throttling.headers.json` | https://learn.microsoft.com/en-us/graph/throttling | OFFICIAL | Learn's sample has exactly these headers; `Retry-After` is delta-seconds. The page says all APIs send Retry-After "except where indicated". request-id sanitized. |
| `error_shape_documented.json` | https://learn.microsoft.com/en-us/graph/errors | OFFICIAL | The errors page example (spells `innerError`). |
| `error_resource_type_schema.json` | https://learn.microsoft.com/en-us/graph/errors | OFFICIAL (schema) | The JSON representation of the error resource, which spells `innererror` (lower case) and has a `details` array. Values are the schema's type names. |
