---
name: catalog-iteration-enrichment
description: Enrich an existing catalog CSV from structured price lists, preserve evidence, normalize names/descriptions, and prepare the later product-variant/SKU pass.
---

# Catalog Iteration Enrichment

Use this skill when a previous catalog extraction produced products with weak
identity, missing brand/cost/price, or flattened descriptions and the operator
provides an authoritative structured price list.

## Route the work

1. Read the latest iteration report first. Start from the final processed CSV,
   its evidence JSONL, and the incoming source files; do not rediscover the
   catalog from screenshots if the final artifacts already contain locators.
2. Choose the parser by source type. Parse DOCX tables from OOXML when the
   source is a table; use the local PDF/image extractor and OCR only for visual
   documents. Keep real incoming files out of Git.
3. Create a tenant enrichment profile under `configs/enrichment/`. Put reviewed
   aliases and source-prefix brand rules there, not in a global fuzzy matcher.
4. Match only reviewed aliases or an exact normalized source name. A price-list
   row that has no unambiguous catalog target is a report item; do not create a
   product without product evidence and description.
5. If the CSV is sorted, keep its positional evidence JSONL sorted with it.
   Never reuse an old row-order JSONL against a reordered CSV; use the
   synchronization helper or carry CSV/evidence records as pairs during the
   transform.
6. For a visual PDF with printed price boxes, run document extraction first,
   then use a reviewed page/source-row profile. The generic card parser may
   classify covers, section headings, pH values or percentages as cards; those
   candidates are not authoritative until reviewed.
7. For descriptions from a designed PDF, reconstruct the text from reviewed
   page blocks and coordinates before invoking OCR. Keep the raw extraction
   evidence and write the normalized description only to the CSV projection.
   `ingestion improve-descriptions` is the local deterministic stage for this.
8. For the identity pass, assign stable brand-prefixed product SKUs and keep a
   persistent identity-to-SKU map. Resolve variants to parents from source
   name/page evidence before writing `sku_producto`; presentation stays in
   `atributos.presentacion`, not in the SKU.

## Authoritative fields

- `costo` comes from the price list's per-unit amount in Bs after the stated
  exchange rate, never from the order total or discount column.
- `precio` comes from the professional sale-price column.
- Clear stale OCR values in `precio` before enriching, because percentages such
  as `0.5%` are commonly misread as product prices.
- Assign `marca` from source evidence/profile. A leftover brand is safe only
  when the operator explicitly established that convention; otherwise leave it
  for review.
- Keep `sku`, `sku_producto` and `sku_variante` untouched until the identity
  pass. Product and variant records remain separate.
- A PDF without cost data may populate `precio` only. Never derive `costo` from
  the product price, margin, package size or any other field.

## Multi-presentation decision

When the product CSV still contains a parent row but the price list has several
presentations, use the lowest cost and lowest professional price as the
temporary parent value and record that decision in the enrichment report. Do
not write presentation-specific values into the product row as if they were a
single price. The subsequent pass must create/link variants and move the exact
presentation prices there.

## Normalization boundary

- Titles use sentence/title capitalization with Spanish stopwords lowercased,
  while retaining protected acronyms, symbols and numeric notation (`N°`,
  `PDRN+`, `DMAE`, `ZN`). Apply only safe orthographic corrections supported by
  the source; never guess an OCR fragment.
- Descriptions preserve source meaning. Decode entities, repair known mojibake,
  remove ligature artifacts, retain existing paragraph breaks, and insert
  paragraph breaks before explicit labels such as `Activos:`, `INCI:`,
  `Modo de uso:` and `Advertencias:`. Capitalize sentence starts without
  paraphrasing or silently completing uncertain words.
- A Markdown/layout converter such as Firecrawl's `pdf-inspector` is a good
  candidate for a future local benchmark because it exposes position-aware
  extraction, column ordering and selective OCR. It must remain an auxiliary
  candidate until the exact page/field benchmark beats the current extractor;
  do not replace evidence with Markdown alone.

## Visual OCR and description quality

- For JPEG/PNG or scanned PDF sources, select PaddleOCR through the optional
  `ocr-worker` container when the batch is large or layout is complex. Keep the
  base CLI lightweight and use CPU first; `gpu`/`gpu:0` is a compatible future
  runtime, not a reason to make GPU dependencies mandatory.
- Configure `ocr.engine: auto` when the tenant can run either engine. It tries
  PaddleOCR and falls back to Tesseract on unavailable models or inference
  failure; preserve the fallback message in extraction warnings.
- Reconstruct reading order from bboxes before any whitespace, capitalization
  or sentence normalization. For parallel columns, finish the left column
  before starting the right; never repair a column-interleaved string with
  punctuation rules alone.
- Remove only high-confidence decorative badges or repeated OCR artifacts from
  the normalized description. Keep their original block IDs in JSON/Markdown
  evidence so the operation remains auditable and reversible.
- Enable `quality.gate` and `strict_visual` for publish candidates. A
  description with critical OCR noise, mojibake, missing structure or
  insufficient length is `reconstruct`/`quarantine` and blocks approval; the
  candidate is retained for diagnostics rather than silently discarded.
- Validate language quality as well as field presence: sentence starts use
  initial capitals, headings/paragraphs are separated, split words are repaired
  only when the line-break pattern is certain, and the HTML review is sorted by
  brand. A visually legible OCR result is not automatically a publishable
  description.

## Identity and SKU rules

- The recommended stable pattern is `{brand_prefix}-{number:04d}` for products
  and `{product_sku}-V{number:02d}` for variants. Current reviewed prefixes are
  `LIB`, `VBL`, `RAR` and `ZOE`.
- Store the mapping in a versioned tenant artifact. Never regenerate a SKU from
  the current row order after a name correction or a new catalog sort.
- Use source file, page/bounding box and normalized name to identify products;
  use variant source name/page plus normalized attributes to identify variants.
- If a product has a variant row but was extracted as `simple`, promote it to
  `variable` before validation. A variable product without a linked variant is
  a blocking issue for this catalog contract.

## Output and checks

Preserve the canonical CSV header. Sort rows deterministically by the configured
brand order and then normalized name. Write a compact JSON report with document
rows, aliases used, catalog rows enriched, unmatched price rows, brand counts,
and the multi-presentation decision.

Validate at least:

- source row counts and DOCX table headers;
- nonnegative numeric cost/price values;
- every enriched row has a brand, and every price/cost has a source row;
- unmatched source names are visible in the report;
- CSV round-trips with quoted multiline descriptions;
- the sorted CSV and positional evidence JSONL have identical row counts and
  aligned source-derived brand/name keys;
- existing SKU blanks remain unchanged for the later pass;
- tests and Ruff pass before handoff.

The main project command for this workflow is `ingestion enrich-catalog` with
`configs/enrichment/<tenant>.yml`; use `ingestion merge-pdf-catalog` for a
reviewed page-price profile and `ingestion improve-descriptions` for reviewed
PDF copy. Never publish to Webby from this step.
