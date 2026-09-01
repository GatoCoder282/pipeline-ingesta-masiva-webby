---
name: tenant-source-onboarding
description: Onboard a new store or tenant source profile by creating mappings, anonymized fixtures, expected outputs and regression tests for the ingestion pipeline.
---

# Tenant Source Onboarding

Use this skill when a new store supplies a different catalog layout, file format
or naming convention.

## Workflow

1. Identify the tenant and source profile. Put real samples in
   `data/incoming/<tenant>/` and keep them outside Git.
2. Run `preview` for tabular files or `extract-document` for PDF/image files.
3. Create or update the tenant/source configuration under `configs/` without
   changing core parsing or validation rules for a store-specific case.
4. Create a small anonymized fixture under
   `tests/fixtures/<tenant>/`. Preserve the layout pattern that caused the
   mapping or extraction decision.
5. Add an expected canonical JSON/CSV result and tests for products, variants,
   numeric formats, required fields and known ambiguity cases.
6. Record unsupported patterns and follow-up work in `docs/iterations/`.

## Invariants

- Mappings are source-header to canonical Webby field.
- Product variants remain separate records linked by `sku_producto`.
- Real customer or catalog files are never added to fixtures or Git.
- Store-specific behavior belongs in configuration or an explicit adapter.
- A new profile must not weaken validation or bypass review gates.

## Completion evidence

Return the changed profile, fixture, expected output, test command and any
remaining fields that require human confirmation. Do not publish the source
unless the user separately authorizes and reviews that action.
