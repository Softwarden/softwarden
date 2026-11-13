# Okta fixtures

Read on 2026-11-11 from developer.okta.com and the public OpenAPI document
(`okta/okta-management-openapi-spec`, `dist/current/management-minimal.yaml`). Replayed through `discovery/okta.py`,
`discovery/rest_source.Api`, `core/outbound.merged_headers` and `core/oidc_discovery.validate` by
`tests/test_vendor_fixtures.py`.

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `apps_list_page1.json`, `apps_list_page2.json` | https://developer.okta.com/docs/api/openapi/okta-management/management/tag/Application/ (operationId listApplications, example GetApplicationsByNameResponseEx) | OFFICIAL | The spec's example (a bare JSON array); app and org IDs, ORNs, the signing kid and `siteURL` (now `hr.example.com`) replaced with synthetic values. Page 2 is the same example's first item with a new ID and label, to make a second page. |
| `apps_list_page1.headers.json`, `apps_list_page2.headers.json` | https://developer.okta.com/docs/api/ (Pagination) ; https://developer.okta.com/docs/reference/rl-best-practices/ | COMPOSED | The pagination text shows two separate `link` header lines (`rel="self"`, then `rel="next"`); the last page has only `rel="self"`. Rate-limit header names from the best-practices page (`X-Rate-Limit-Reset` is UTC epoch seconds); values synthetic. |
| `app_users_list.json` | the same spec, listApplicationUsers, example AppUserListEx | OFFICIAL | Okta's sample user, app, type and schema IDs replaced with synthetic `00u000...`, `0oa000...`, `oty000...` and `osc000...` values, and every link host made `example.okta.com` (the sample had one on `rain.okta1.com`). The emails were already `example.com`. |
| `error_429.json`, `error_429.headers.json` | the same spec, example ErrorTooManyRequests (E0000047) ; https://developer.okta.com/docs/reference/rl-best-practices/ | OFFICIAL body, COMPOSED headers | Okta documents no `Retry-After`; for a concurrent-limit 429 Limit and Remaining are 0 and Reset is only a suggestion. Header values synthetic. |
| `error_403.json` | the same spec, example ErrorAccessDenied (E0000006) | OFFICIAL | errorId sanitized. |
| `id_token_claims.json` | https://developer.okta.com/docs/reference/api/oidc/ (ID token payload) | OFFICIAL names, synthetic values | Okta's sample address, phone number, profile URL and zone replaced with synthetic ones (`1 Example Street`, `+1 (555) 010-0000`, `UTC`); the generic sample name John Doe is kept. The documented example has **no `nonce` and no `azp`**; Okta says the nonce is included when the authorize request sent one (the replay adds the nonce Softwarden sends, `core/oidc.py`). `ver` is the integer 1; `alg` is always RS256. Org server `iss` is `https://{domain}`. |
