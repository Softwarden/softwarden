# Vendor payload fixtures

Responses in the shapes the vendors document, replayed through the real parsers and clients by
`tests/test_vendor_fixtures.py`. A fake transport answers at the HTTP boundary (status, headers, body bytes); nothing
here goes through a simulator, so a parser that assumes a shape the vendor doesn't send fails a test.

- **No vendor or tenant was contacted.** Each file was built on 2026-11-11 from public documentation pages and public
  spec files (Microsoft Learn, the Okta management OpenAPI document, the Atlassian swagger, Google's Admin SDK
  reference, ServiceNow's Table API page). Every ID, name, email and address is synthetic (`00000000-...` GUIDs,
  `example.com`, `192.0.2.x`).
- **Provenance labels** in each folder's README: **OFFICIAL** is the vendor's own sample, sanitized; **COMPOSED** is
  built from the vendor's schema or field tables (every field documented, the combination ours); **SYNTH** is a shape
  the vendor doesn't publish (flagged: a passing test proves only that our parser reads our guess).
- **`*.headers.json`** files are response-header sidecars: `_status` is the status line, every other key a header. A
  list value is the same header sent on several lines (Okta's two `link` lines); the replay joins them as the real
  transport does (`core/outbound.merged_headers`).
- **Recorded live answers** replace a SYNTH or COMPOSED file when someone with an approved tenant captures one: strip
  every identifier and secret first, keep the file name, and change the label in the folder's README.

## Where the code assumes something the docs don't say (VF-n)

Found while building the fixtures (2026-11-11). The replay tests name them; none blocks a documented OFFICIAL shape.

| VF | Where | What | Test |
|---|---|---|---|
| 1 | `core/oidc_discovery.validate` | Requires `nonce` for every provider. Okta's documented ID token has none; Okta returns it when the authorize request sent one, and Softwarden always does (`core/oidc.begin`). | Okta token test adds the nonce and records the refusal without it. |
| 2 | `core/oidc_discovery.link_email`, `core/oidc.match_user` | **Fixed:** a first Entra sign-in links an existing member only by the `email` claim of a non-guest of the pinned tenant (`idp` absent or equal to `iss`, `acct` absent or 0); `preferred_username` never links. Anything else goes to pending access. Returning identities match (`tid`, `oid`) only. Learn: `email` isn't in the default claims and is mutable, `preferred_username` is mutable, neither is for authorization; no `email_verified`. | Entra token test checks `link_email`; tests/test_invitations.py covers the sign-ins. |
| 3 | `discovery/entra.map_object` | Reads `servicePrincipal.publisherName`, which Learn shows only as a `$select` value; falls back to `publisherDomain`. | Fallback test. |
| 4 | `discovery/powerplatform._bap_environments` | Follows `nextLink` on the BAP list, which Learn doesn't show; a link to a regional host (`unitedstates.api.bap.microsoft.com`, in the sample's `runtimeEndpoints`) would stop the run. | Not replayed (no documented next page). |
| 5 | `discovery/rest_source.Api.get` | Fixed: Google's Directory API documents 403 `userRateLimitExceeded`/`quotaExceeded` as quota errors; they were reported as the missing Reports admin privilege and not retried. Now `Service.throttled` (`discovery/google_workspace._throttled`) retries them with backoff; any other 403 is still a missing grant. | Replay tests for both reasons and a plain 403. |
| 6 | `discovery/azure.py`, `graph_source.refusal` | Branches on AuthorizationFailed, SubscriptionNotFound, MissingSubscriptionRegistration; Learn shows only the generic ErrorResponse. | Generic shape only. |
| 7 | `discovery/azure.site_type` | `functionapp` and `workflowapp` kind tokens aren't in Learn's sample (kind `app` only). | SYNTH items. |
| 8 | `tickets/clients.servicenow_status` | Asks for `sysparm_display_value=all` (no vendor example); the documented default form gives stored values, mapped for incident only. | Problem-table test records UNKNOWN. |
| 9 | `tickets/clients.jira_status` | Category keys `new`, `indeterminate`, `done` aren't enumerated in the swagger; Atlassian's getIssue example has no status or summary. | Official example records UNKNOWN. |
| 10 | `discovery/halo.map_object` | Reads Device fields on Device_List rows (the third-party mirror leaves Device_List undefined). | SYNTH page. |
| 11 | `discovery/entra_coverage.py` | Says conditional-access 429s carry no Retry-After; Learn's throttling page doesn't name that exception. | Not replayed. |

Confirmed against the docs, not discrepancies: `core/graph.error_of` reads both `innerError` and `innererror`,
`parse_retry_after` reads Retry-After seconds (the only documented form), and `core/outbound.merged_headers` keeps both
of Okta's `link` lines. Each is replayed.

The live tests (`pytest --live`, README "Live tests") are the other half of this check and run before each release.
