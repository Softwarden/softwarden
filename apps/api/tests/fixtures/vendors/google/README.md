# Google Workspace fixtures

Read on 2026-11-11 from Google's Admin SDK reference. Replayed through `discovery/google_workspace.py` and
`discovery/rest_source.Api` by `tests/test_vendor_fixtures.py`.

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `activities_list_token_authorize.json` | https://developers.google.com/workspace/admin/reports/reference/rest/v1/activities/list ; https://developers.google.com/workspace/admin/reports/v1/appendix/activity/token | COMPOSED | Google gives the JSON template only (kind, etag, `items` of Activity, `nextPageToken`; Activity's id, actor, ipAddress, events with parameters carrying `name` and one of `value`, `multiValue`, ...) and the authorize parameters (`app_name`, `client_id`, `client_type`, `scope` documented as a string, `scope_data`). No literal sample. Whether `scope` arrives as `value` or `multiValue` is not documented: the fixture uses `multiValue`. The event `type` `auth` is unverified. |
| `error_quota_429.json` | https://developers.google.com/workspace/admin/directory/v1/limits ; https://developers.google.com/workspace/admin/reports/v1/limits | SYNTH | The limits pages document only status codes and reasons (Directory API: 403 `userRateLimitExceeded`, 403 `quotaExceeded`, 429 `rateLimitExceeded`; Reports API: quota errors are 503). The body (`error.errors[].reason`, `domain`) is Google's general error format, not shown on those pages. |
| `error_503_reports_quota.json` | https://developers.google.com/workspace/admin/reports/v1/limits | SYNTH | Only the status 503 and the "exponential backoff from 5 seconds" advice are documented; the body is the general format, low confidence. |
