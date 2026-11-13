# HaloITSM fixtures

Read on 2026-11-11. Halo publishes its API reference on each tenant (`{base}/apidoc`), which was not contacted, so
these are built from a third-party mirror and RFC 6749. Replayed through `discovery/halo.py`, `core/halo.py` and
`tickets/clients.halo_token` by `tests/test_vendor_fixtures.py`. There is no Halo ticket (`GET /api/Tickets/{id}`)
fixture: no public page shows one.

| File | Doc URL | Label | What was synthesized |
|---|---|---|---|
| `assets_list_page.json` | https://kb.dtctoday.com/books/halopsa-api-reference/page/asset-schema (THIRD-PARTY mirror) | SYNTH | The mirror's Device_View: `page_no`, `page_size`, `record_count`, `assets` (Device_List), `columns_id`, `columns_tilehtml`, `columns`, `xtypeunamecancreatenew`. The mirror defines `id`, `inventory_number`, `key_field`, `client_name`, `site_name`, `assettype_name` and `inactive` on the full Device object, not on Device_List (left undefined there), so the row fields are a guess. |
| `token_response.json` | RFC 6749 section 5.1 | SYNTH | The generic OAuth token answer; no Halo page read confirms `scope` or extra fields. |
