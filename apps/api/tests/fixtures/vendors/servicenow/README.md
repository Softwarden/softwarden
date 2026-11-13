# ServiceNow fixtures

Read on 2026-11-11 from ServiceNow's Table API reference. Replayed through `tickets/clients.py` by
`tests/test_vendor_fixtures.py`.

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `table_get_official_display_value_false.json` | https://www.servicenow.com/docs/r/api-reference/rest-apis/c_TableAPI.html | OFFICIAL (shortened) | The Table API example (problem table, `sysparm_limit=1`, default `sysparm_display_value=false`): plain strings, reference fields as `{link, value}`, `state` the stored value `"4"`. Instance host and sys_ids sanitized. A valid query with no match answers an empty `result` array. |
| `table_get_official.headers.json` | the same page | OFFICIAL (headers) | `Link` (rel next, prev, first, last) and `X-Total-Count` (value is the page's placeholder text). Softwarden reads one row and never reads them. |
| `table_get_display_value_all.json` | the same page (parameter text only) | SYNTH | The docs say `sysparm_display_value=all` returns both values but give no example; the `{display_value, value}` wrapper is from ServiceNow community posts. This is the mode Softwarden requests. |
