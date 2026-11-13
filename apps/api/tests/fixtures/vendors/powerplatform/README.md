# Power Platform fixtures

Read on Microsoft Learn on 2026-11-11. Replayed through `discovery/powerplatform.py` and
`discovery/graph_source.Client` by `tests/test_vendor_fixtures.py`.

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `bap_environments_list.json` | https://learn.microsoft.com/en-us/power-platform/admin/list-environments | OFFICIAL (shortened) | The page is titled "Power Platform API - 2021-02-01 - List environments (preview)" and documents the legacy BAP URL `.../scopes/admin/environments?api-version=2020-10-01`. Its sample shows no `nextLink` and the page says the answer is always 200 OK. GUIDs, the org name and the scale group sanitized. |
| `dataverse_bots_page1.json` | https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/query/page-results ; https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/query/select-columns ; https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/entities/bot | SYNTH | No Learn page shows a `bots` answer. The envelope, `@odata.etag`, `@odata.nextLink` and the `@OData.Community.Display.V1.FormattedValue` annotations follow the generic contacts and accounts examples; the columns are the Agent (bot) table's (EntitySetName `bots`). Choice values and labels are guesses. |
| `dataverse_page_headers.json` | https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/query/page-results | OFFICIAL (headers) | `Preference-Applied: odata.maxpagesize=N`. The page says: don't change or append to `@odata.nextLink`, and send the same `odata.maxpagesize` preference on every page (maximum 5,000). |
