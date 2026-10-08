# c2 — Phase 2 risk feed (disabled)

This package is deliberately empty in the first version (ТЗ 9.1 stage 5,
FR-C2-01…07, LR-16, LR-20). It can only be imported when the global feature
flag `feature_c2_enabled` is on; the default is off and the flag change is
audited (FR-ADM-05).

Extension points that already exist in stages 0–3 and that phase 2 will use:

- `domain_source.source = czds` and the CZDS-only lineage filter in
  `entitlements` (AS-22, LR-12) — the only place where CZDS domains may surface.
- `obs_checkout_host` (all categories, FR-DT-11) and `obs_tech.version`
  (AS-23) — collected now, internal-only until the legal opinion.
- `entitlement.field_profile = c2_risk`, which cannot be assigned while the
  flag is off (FR-KYC-07).
- `provider.role` values `fraud_tool` and `3ds_sdk`.

Nothing in this directory may be enabled without a written legal opinion and
the owner's decision (ТЗ 2.6, LR-16, LR-23).
