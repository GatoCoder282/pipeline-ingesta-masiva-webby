---
name: catalog-document-extraction
description: Extract and review product catalogs from local PDF or image files, preserving page-level evidence and producing JSON/CSV candidates without inventing missing values.
---

# Catalog Document Extraction

Use this skill when a user provides a PDF, JPG, PNG, WEBP or TIFF catalog and
needs products or variants structured for the ingestion pipeline.

## Workflow

1. Keep real source files under `data/incoming/<tenant>/`; do not commit them.
2. Run `ingestion extract-document` with the tenant document profile when one
   exists, otherwise use the default profile.
3. Inspect the extraction artifact before trusting candidates. Every accepted
   field must have a page, bounding box or block reference when available.
4. Treat low-confidence OCR, missing names/prices, conflicting prices and
   unlinked variants as review items. Do not fill them from context or guesses.
5. Keep products and variants separate. Variants must retain
   `sku_producto`, `sku_variante` and structured `atributos`.
6. Use the generated JSON as the evidence-preserving result and the CSV only
   as a review/export representation. Do not publish to Webby from this skill.

## Expected artifacts

The run should preserve the raw file, extraction JSON, processed product and
variant JSONL/Parquet files, quarantine and report. If the document shape is
not handled by the deterministic card extractor, report the unparsed cards and
the exact evidence needed to add a source profile.

## Safety boundary

This skill may inspect and structure local documents. It must not send document
contents to external OCR or scraping services unless the user explicitly asks
for that route and the required credentials/consent are available.
