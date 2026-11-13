"""Offboarding and access review. No models: it reads what the catalog, money, notify
and discovery apps already store, and writes only through their service functions.

- references.py: every place a Person or their user account can appear, found by introspecting the models, each
  classified as a footprint section or deliberately left out (tests/test_offboarding.py fails on an unclassified one);
- footprint.py: "Person X is leaving: what do they own, run, follow or appear in";
- reassign.py: hand selected responsibilities to another person, one audited change per row, in one transaction;
- access_review.py: every live record with its owners, flagging inactive owners, owners who left and records with
  no owner, and Entra owners that differ from the catalog's (informational)."""
