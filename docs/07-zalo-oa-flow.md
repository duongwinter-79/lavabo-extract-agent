# Zalo OA flow — orders from an OA-owned group

> ## Not available on the shop's account
>
> SENKAHOMES has a **Zalo Business** account — a normal personal account, paid to unlock
> a business profile, catalog and higher limits. That is a different product from a
> **Zalo Official Account**, and it has **no API, no webhooks and no developer app**.
> Everything below needs an OA.
>
> This document, `scripts/lavabo_webhook.py` and `src/lavabo/connectors/zalo_oa.py` are
> kept because they work and cost nothing to keep — if the shop registers an OA later,
> the connector slots in behind the same interface. Until then the copy-paste route in
> `docs/03-zalo-runbook.md` is not a stopgap, it is **the** route.

Replaces copy-and-paste with messages Zalo pushes to us. Orders arrive as they are
posted, nobody copies anything, and **`Người chốt đơn` becomes a fact** — the payload
carries the sender.

> **Wire format not verified.** `developers.zalo.me` was unreachable from the machine
> this was written on, so endpoint paths, webhook field names and the signature scheme
> are a best reading of Zalo's conventions, not confirmed against the docs. Everything
> Zalo-specific is confined to two places, listed in §6, so correcting it is a small
>, contained change. Do that before pointing this at production traffic.

---

## 1. Why an OA-owned group, and not your current one

An OA cannot read an ordinary group chat. It only sees its own 1-1 conversations with
followers, and groups it owns through **GMF** (Group Management Function). So the flow
depends on one organisational change:

> The team stops posting orders in the existing staff group, and posts them in a group
> **created and owned by the OA** instead.

Nothing else about how they write orders changes. The same header, the same item lines,
the same `Tổng`/`Đã cọc`.

| | Copy-paste (today) | OA group |
|---|---|---|
| Effort per order | someone copies | none |
| Người chốt đơn | one name per session, guessed | real sender, per order |
| Timestamps | none | real |
| Missed orders | whatever wasn't copied | none, Zalo pushes each one |
| Needs | nothing | verified OA + paid package + a public URL |

---

## 2. What it costs to set up

1. **A verified Zalo Official Account.** Business documents, Zalo's review.
2. **A GMF-capable package.** Group management is on the paid Advanced/Premium tiers,
   not the free OA. Check current pricing — this is the main commitment.
3. **An app** at developers.zalo.me, linked to the OA, with group permissions granted.
4. **A public HTTPS URL** for the webhook. Zalo will not deliver to `localhost`.

Item 4 is the one people underestimate. Options, cheapest first:

```bash
cloudflared tunnel --url http://localhost:8770     # free, no account for a quick tunnel
ngrok http 8770                                    # free tier, URL changes on restart
```

A tunnel URL that changes on restart means re-registering the webhook each time. For
anything ongoing, a small always-on VPS with a stable domain is the honest answer —
because **a webhook only fires while something is listening**, and Zalo offers no
history endpoint for a group. An order posted while the receiver is down is not
recoverable through the API.

---

## 3. Running it

```bash
# .env
ZALO_APP_ID=...
ZALO_OA_SECRET=...

python scripts/lavabo_webhook.py --port 8770
cloudflared tunnel --url http://localhost:8770
# register https://<tunnel>/webhook in the OA app's webhook settings
```

Then, whenever you want the workbook:

```bash
lavabo ingest --source oa
lavabo extract
lavabo load --layout senkahomes --out data/out/donhang.xlsx --year 2026
```

`--closer` is no longer needed: the sender on each order wins over it. It stays as a
fallback for orders that arrived by the copy-paste route.

---

## 4. How it fits the existing pipeline

```
Zalo group ──push──▶ lavabo_webhook.py ──▶ oa_events (raw, verbatim)
                                              │
                                              ▼
                                     ZaloOAConnector
                                              │
                          canonical Conversation ─▶ same staging, extraction, Excel
```

Two deliberate choices:

**Store the raw delivery first, parse later.** The receiver verifies, writes the payload
as it arrived, and answers 200. Because Zalo will not replay a group message, a parsing
bug must never be able to lose one — so parsing happens afterwards, from stored rows,
and can be re-run after a fix.

**The connector does no networking.** It reads `oa_events` and yields Conversations, so
it is testable without credentials and a wire-format correction touches one module.

Deliveries are keyed on the message id, so Zalo's retries are no-ops. Messages that are
not orders — `@All mai phải đi đơn nào…` — are stored but produce no order, on the same
header rule the copy-paste path uses.

---

## 5. What was tested, and what was not

Verified here, against simulated deliveries:

- signature computation changes with body and with secret
- receiver stores events, dedupes a retried delivery, and reports 200 either way
- order messages become Conversations; group chatter does not
- header parsing gives date, order number and customer
- real timestamps and real senders survive into the workbook
- `Người chốt đơn` per order beat a deliberately wrong `--closer`

Not verified, because Zalo was unreachable:

- the exact webhook JSON field names
- the exact signature header and algorithm
- whether a group history endpoint exists after all

---

## 6. Where to correct the wire format

| What | Where |
|---|---|
| Webhook field names, event names | `EVENT_FIELDS` and `MESSAGE_EVENTS` in `src/lavabo/connectors/zalo_oa.py` |
| Signature scheme, header name | `expected_signature()` and `SIGNATURE_HEADER` in `scripts/lavabo_webhook.py` |

Nothing else should need touching. To check a real delivery against expectations, run
the receiver with `--insecure` on a tunnel, post one order in the group, and read the
stored payload:

```bash
sqlite3 data/staging.db "SELECT payload FROM oa_events ORDER BY received_at DESC LIMIT 1;"
```

That single row tells you every field name Zalo actually sends.

---

## 7. Security

- The receiver **fails closed**: without `ZALO_OA_SECRET` it refuses to start, unless
  `--insecure` is passed explicitly for local testing.
- A bad signature is logged and answered `200 {"ok": false}` — rejecting with an error
  status would make Zalo retry a forged request indefinitely.
- `ZALO_OA_SECRET` lives in `.env`, never in `config.yaml`.
- Anything the receiver stores is customer data, in the gitignored `data/`.
