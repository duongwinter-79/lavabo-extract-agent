# Task 2 — Automation plan: chat → Excel ETL agent

Local-first. Plain Python + markdown runbooks. No server, no queue, no container.

---

## 1. Design principle

Zalo and Meta arrive by completely different mechanisms (human drops a file vs. we poll an
API), but everything after ingestion is identical. So the pipeline forces both into one
canonical shape as early as possible, and every later stage only ever sees that shape.

```
                 ┌────────────────────────┐
  Zalo  ──drop──▶│  connectors/zalo_*.py  │─┐
                 └────────────────────────┘ │
                                            ├─▶ canonical  ─▶ staging  ─▶ extract ─▶ load
                 ┌────────────────────────┐ │  Message /     SQLite      (LLM)      Excel
  Meta  ──poll──▶│ connectors/meta_graph  │─┘  Conversation
                 └────────────────────────┘
```

Five stages, each independently runnable and re-runnable:

| Stage | Command | Idempotent on |
|---|---|---|
| **1 Ingest** | `lavabo ingest --source zalo\|meta` | `(source, message_id)` |
| **2 Normalize** | folded into ingest | — |
| **3 Extract** | `lavabo extract` | `(conversation_id, schema_version)` |
| **4 Load** | `lavabo load --out report.xlsx` | full rewrite from staging |
| **5 Verify** | `lavabo verify` | — |

The point of the SQLite layer between 1 and 3: **LLM calls are the expensive, slow, and
non-deterministic part.** Staging means you re-ingest without re-paying for extraction, tweak
the Excel layout without re-calling the LLM, and re-run extraction on only the conversations
whose schema is stale.

---

## 2. Canonical model

```python
Message:      source, conversation_id, message_id, sent_at (tz-aware UTC),
              sender_id, sender_name, direction (inbound|outbound|system),
              text, attachments[], raw (original JSON, kept for audit)

Conversation: source, conversation_id, customer_name, customer_handle,
              started_at, last_message_at, message_count, messages[]
```

`raw` is retained deliberately. When your column requirements land and one of them needs a
field we didn't map, we recover it from `raw` instead of re-crawling Meta.

---

## 3. Stage detail

### Stage 1 — Ingest

**Meta (`connectors/meta_graph.py`)** — walks `/{page}/conversations` then
`/{conv}/messages`, cursor-paginated, with backoff on 429/`code 4` throttling. Stores a
watermark per page so subsequent runs only fetch conversations with
`updated_time > last_run`. First run is a backfill; later runs are incremental.

**Zalo (`connectors/zalo_export.py`)** — watches `data/inbox/zalo/`. For each new file it
sniffs the format and dispatches to a parser: plain-text transcript, JSON, or HTML. The
text parser is regex-driven with the pattern in config, because **the exact line format of a
Zalo copy-paste depends on client version and we have not seen a real sample yet.** Tuning
that one regex against your first real file is a 10-minute job; see
`docs/03-zalo-runbook.md`.

Every ingested file is hashed and recorded, so re-dropping the same export is a no-op.

### Stage 2 — Normalize

Timestamps to tz-aware UTC (Zalo exports are naive local time — `Asia/Ho_Chi_Minh` from
config). Sender identity resolved to a stable `customer_id`. Direction inferred: for Meta,
by comparing `from.id` to the Page ID; for Zalo, by comparing the sender name to your
configured own-name list. Consecutive messages from the same sender within N seconds are
optionally merged to reduce token count.

### Stage 3 — Extract (the LLM step)

This is the stage your pending requirements plug into, and it is deliberately the only place
they appear.

**`config/schema.yaml` is the single source of truth for your columns.** One entry per
column: name, type, description, whether required, optional enum of allowed values. From
that file the code generates *both* the JSON schema handed to the model *and* the Excel
header row — so adding a column is a one-file edit, never a code change.

```yaml
schema_version: 1
columns:
  - name: customer_name
    type: string
    description: Full name of the customer as stated in the conversation.
    required: true
  - name: order_status
    type: string
    enum: [new, confirmed, shipped, cancelled, unknown]
    description: Current status of the order discussed.
```

Mechanics:
- **Structured output, not prose parsing.** Anthropic: a tool definition whose
  `input_schema` is the generated schema, with `tool_choice` forcing that tool. Gemini:
  `response_schema` + `response_mime_type: application/json`. Either way the model
  physically cannot return unparseable output — no regex-scraping of markdown fences.
- **One conversation per call.** Simpler, parallelizable, and one bad conversation can't
  poison a batch.
- **Every field carries provenance.** Alongside each extracted value we store the model's
  confidence and, where the schema asks for it, the quoting message ID. Cells the model was
  unsure about get flagged in Excel rather than silently guessed.
- **Never invent.** The prompt requires `null` over a plausible guess; `null` rate is a
  quality metric we report, not a failure.
- **Caching.** Keyed on `hash(conversation_text + schema_version + model + prompt_version)`.
  Re-running `extract` after changing nothing costs zero. Changing one column re-extracts
  everything, which is correct — the model sees all columns at once and they interact.
- **Long conversations.** Above a token threshold, chunk chronologically and reduce, rather
  than truncating. Which is right depends on your columns (a "final order value" column
  cares about the end; a "complaints raised" column cares about everything), so this stays
  configurable.

Provider choice is a config line. Claude and Gemini both sit behind the same
`Extractor.extract(conversation, schema) -> ExtractionResult` interface; swapping is a
one-word change and the cache is keyed by model, so results don't mix.

**Cost control:** `lavabo extract --limit 5 --dry-run` prints the exact prompt and estimated
token cost without calling anything. Use it before any full run.

### Stage 4 — Load

`openpyxl`, three sheets:

- **Data** — one row per conversation, columns exactly as `schema.yaml` orders them. Frozen
  header, autofilter, column widths, low-confidence cells shaded.
- **Sources** — one row per conversation: source, IDs, message count, date range, deep link
  back to the Meta thread or the Zalo filename. This is what makes a disputed cell
  checkable in 30 seconds.
- **Run** — timestamp, model, schema version, counts, error count, total token spend.

Written to a temp file then atomically moved, so an open Excel window never sees a
half-written file.

### Stage 5 — Verify

Fails loudly on: conversations ingested but never extracted, required columns null above a
threshold, duplicate conversation IDs, timestamps outside the requested window. Prints a
summary. Exit code non-zero on failure, so it can gate a scheduled run later.

---

## 4. Repository layout

```
lavabo-extract-agent/
├── README.md                        # quickstart
├── docs/
│   ├── 01-source-verification.md    # Task 1 findings
│   ├── 02-agent-plan.md             # this file
│   ├── 03-zalo-runbook.md           # the human SOP for Zalo exports
│   ├── 04-meta-setup.md             # Meta app / token / App Review walkthrough
│   └── 05-schema-guide.md           # how to define your columns
├── config/
│   ├── config.example.yaml
│   └── schema.example.yaml
├── scripts/
│   └── probe_zalo_export.py         # Task 1 empirical check
├── src/lavabo/
│   ├── models.py  config.py  store.py  normalize.py  cli.py
│   ├── connectors/  base.py  meta_graph.py  zalo_export.py
│   ├── extract/     base.py  prompt.py  anthropic_extractor.py  gemini_extractor.py
│   └── load/        excel.py
├── data/            inbox/zalo/   staging.db   out/
├── .env.example     requirements.txt
```

Secrets in `.env` only (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `META_PAGE_TOKEN`).
`data/` is gitignored — **customer conversation content must never be committed.**

---

## 5. Build order

| Phase | Deliverable | Blocked by |
|---|---|---|
| **0** | Findings + plan + probe script + skeleton | — *(delivered now)* |
| **1** | Meta connector + SQLite staging, verified against your real Page | Meta token |
| **2** | Zalo parser tuned to a real export sample | One sample file from you |
| **3** | Extraction schema + LLM step | **Your column requirements** |
| **4** | Excel writer + verify | Phase 3 |
| **5** | One-command run, optional scheduling | Phases 1–4 |

Phases 1 and 2 are independent and neither needs your column list — we can start immediately.
Phase 3 is the one genuinely waiting on you.

---

## 6. Deliberate non-goals

No web UI, no database server, no Docker, no cloud scheduler, no realtime streaming. If this
later needs to run unattended, the smallest correct step is a cron entry calling
`lavabo run --all`, not a service.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| Meta Advanced Access denied/slow | DYI manual export path reuses the same inbox ingest |
| Zalo stays fully manual | Runbook keeps it a 2-minute-per-conversation human task; OA upgrade is a drop-in |
| LLM extracts a plausible wrong value | Forced schema, null-over-guess, confidence flagging, Sources sheet for audit |
| Column requirements change late | Columns live in one YAML file, never in code |
| PII handling | Local-only storage, gitignored data dir, conversation text sent only to your chosen LLM provider |
