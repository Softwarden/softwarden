# Microsoft Entra ID token fixture

Read on Microsoft Learn on 2026-11-11. Replayed through `core/oidc_discovery.validate`, `identity_key` and `email_of`
by `tests/test_vendor_fixtures.py`, signed there with a test key (the timestamps are moved to the test's clock).

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `id_token_claims_v2.json` | https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference | OFFICIAL names, synthetic values | The default v2.0 claims: aud, iss (`https://login.microsoftonline.com/{tid}/v2.0`), iat, nbf, exp, aio, name, nonce, oid, preferred_username, rh, sid, sub, tid, uti, ver `"2.0"`. `email` is **not** in the default set (Learn: present by default for guests with an email, for members only through the email optional claim or scope; "not guaranteed to be correct and is mutable"); `preferred_username` is mutable; no `email_verified` claim is documented. Others Learn lists and the fixture omits: roles, hasgroups, groups:src1, idp, c_hash, at_hash. No `azp` in the ID token. |
