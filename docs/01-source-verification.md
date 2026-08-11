# Task 1 — Source verification: what can we actually get out of Zalo and Meta?

Status: desk research, August 2026. Every claim below is tagged with a confidence level.
Anything marked **UNVERIFIED** must be confirmed on your own machine/account before we
build against it — `scripts/probe_zalo_export.py` exists for exactly that.

---

## 1. Zalo Desktop "Export data" — the short answer

**Your assumption does not hold. Zalo PC's export is a backup, not an export.**
It is not a data interchange format and we should not design the pipeline around it.

### What the feature actually is

Path: `Zalo PC → Settings (gear) → Data / Storage (Quản lý dữ liệu) → Backup / Export data
(Sao lưu · Xuất dữ liệu)`.

| Property | Finding | Confidence |
|---|---|---|
| Output | A single archive, typically `backup_zalo_<date>.zip`, default to Desktop | High |
| Selectable content | Messages, images, videos, files (checkbox list) | High |
| Protection | Prompts you to **set a backup password** on first use | High |
| Readability | Encrypted / not openable with 7-Zip or WinRAR; users report "corrupt or unknown format" | Medium-High |
| Intended use | Restore back into Zalo on another device (`Import data / Khôi phục`) | High |
| Message payload | Opaque blob, not documented; media folders may be partially visible | Medium |

Sources: [QuanTriMang backup guide](https://quantrimang.com/cong-nghe/2-buoc-sao-luu-tin-nhan-zalo-118519),
[GEARVN](https://gearvn.com/blogs/thu-thuat-giai-dap/cach-sao-luu-tin-nhan-zalo-tren-laptop),
[VOZ thread — users failing to open the backup zip](https://voz.vn/t/lam-sao-de-mo-file-zip-backup-cua-zalo-vay-cac-bac.255843/),
[Mytour — backup password](https://mytour.vn/en/blog/bai-viet/how-to-set-up-zalo-backup-password-create-backup-pass-and-restore-zalo-messages.html).

### What it is *not*

- There is **no per-conversation "export to TXT/HTML/CSV/PDF"** in Zalo PC. Blog posts
  claiming HTML/PDF/CSV export are describing third-party tools or manual copy-paste, not a
  Zalo feature. A question asking for exactly this on Zalo's own developer forum has no
  official "yes, use feature X" answer.
  ([developer forum thread](https://developers.zalo.me/community/detail/192ebbcc87896ed73798))
- The backup is **not** an archive of JSON files you can iterate. Treat any blog post saying
  "unzip it and read messages/*.json" as wrong until the probe script proves otherwise.

> **Escape hatch — run the probe before we accept this.** Search results conflict, and Zalo
> ships changes without notes. Do the export once and run:
> ```
> python scripts/probe_zalo_export.py "C:/Users/<you>/Desktop/backup_zalo_....zip"
> ```
> It reports whether the archive opens, its entry tree, and whether any entry is readable
> text/JSON/SQLite — without printing message content. If it comes back readable, we get a
> much better Zalo path and I will rewrite the plan around it.

### Realistic Zalo options, ranked

| # | Option | Gets history? | Effort | Risk | Verdict |
|---|---|---|---|---|---|
| 1 | **Manual copy-paste per conversation → `.txt` in a watched folder** | Yes, what you select | Low per convo, human time | None | **Recommended starting point** |
| 2 | **Zalo OA OpenAPI** (`/v2.0/oa/conversation`, `/v2.0/oa/listrecentchat`) | Yes, OA↔follower only | Medium | None | Best option **if** you get an OA |
| 3 | Unofficial web-session libraries (e.g. [`zlapi`](https://github.com/Its-VrxxDev/zlapi)) | Real-time yes; history unclear | Medium | **Account ban** — explicitly violates Zalo ToS | Not recommended |
| 4 | Reading Zalo PC's local app data directly | Unknown | High | Fragile, breaks on update | Only if probe finds something |
| 5 | UI automation (scroll + OCR/accessibility scrape) | Yes | High | Brittle | Last resort |

Option 1 is unglamorous but it is the only one that is zero-risk, works for a **personal
(non-OA) account** today, and produces something an LLM can read directly. The pipeline is
designed so that when/if you obtain an OA, option 2 slots in behind the same interface with
no change to the rest of the agent.

**Decision needed from you:** is getting a Zalo OA (Official Account) on the table? It flips
Zalo from "human does the export" to "fully automated", and it's the single biggest lever on
this project.

---

## 2. Meta Business chat — the short answer

**Your assumption holds.** The Graph API path is real and is the right one. Caveats are about
permissions and review, not about capability.

### Messenger (Facebook Page) — primary path

Two endpoints do the whole job:

```
GET /v21.0/{PAGE_ID}/conversations
    ?platform=messenger
    &fields=id,updated_time,message_count,participants,snippet
    &limit=50

GET /v21.0/{CONVERSATION_ID}/messages
    ?fields=id,created_time,from,to,message,attachments,shares,sticker
    &limit=100
```

Both are cursor-paginated. Messages come back newest-first, so reverse them.

| Property | Finding | Confidence |
|---|---|---|
| Token type | **Page access token**, from a user with `MESSAGING` or `MODERATE` task on the Page | High |
| Permissions | `pages_messaging`, `pages_read_engagement`, `pages_manage_metadata`, plus `business_management` for Business-Suite-managed assets | High |
| Access level | **Advanced Access required** to read conversations with people who have no role on your Page — i.e. required for all real customers | High |
| Gate | App Review + Business Verification for Advanced Access | High |
| History depth | Full conversation history via pagination; no documented cutoff | Medium |
| Known exclusion | Conversations in the **Requests folder inactive >30 days are not returned** | High |

Sources: [Conversations API](https://developers.facebook.com/docs/messenger-platform/conversations/),
[Page Conversations reference](https://developers.facebook.com/docs/graph-api/reference/page/conversations/),
[Conversation reference](https://developers.facebook.com/docs/graph-api/reference/conversation/).

### Instagram DM — same shape, tighter limits

`GET /{IG_ID}/conversations?platform=instagram`, permission `instagram_manage_messages` (+
`instagram_basic`). Message pagination maxes at `limit=100`, defaults to 25. Instagram's
messaging rate limits are materially lower than Messenger's, so pace the crawl.
([Instagram messaging platform docs](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api/))

### WhatsApp Business — important gap

If "Meta business chat" includes WhatsApp for you: **there is no history-retrieval endpoint.**
The Cloud API is webhook-delivery only. You can only capture messages from the moment you
start listening, into your own store. Retroactive WhatsApp history is not obtainable via API.
Flag this now if WhatsApp is in scope — it changes the architecture (we'd need a persistent
webhook receiver, not a batch puller).

### Fallback if App Review stalls

Meta's **"Download Your Information"** produces messages in **JSON or HTML** with a date-range
selector, delivered by email in ~10–30 minutes. This is a legitimate manual fallback for
Messenger while Advanced Access is pending, and it drops into the same "watched folder"
ingest as the Zalo manual path.
Confidence that this covers *Page* inbox (not just personal DMs): **Medium — UNVERIFIED**,
worth one test request.
([Coupler.io guide](https://blog.coupler.io/how-to-export-facebook-data/),
[downloading Page Messenger history](https://slickbots.com/how-to-download-messenger-chat-history-of-your-facebook-business-page/))

Browser-extension chat exporters for Business Suite exist but put your session cookie in a
third party's hands — not recommended for customer data.

---

## 3. Consequence for the design

The two sources are **asymmetric in automation level**, and pretending otherwise would produce
a pipeline that silently does nothing for Zalo:

- **Meta = pull.** Scheduled, incremental, fully automated once Advanced Access lands.
- **Zalo = drop.** A human puts a file in a folder; the agent watches, parses, and processes.

So the agent is built around a **single canonical message model** with pluggable connectors,
where "someone dropped a file" and "we polled an API" are the same event downstream. That is
the only structure that survives Zalo turning into an OA later, or WhatsApp joining, without a
rewrite. See `docs/02-agent-plan.md`.

---

## 4. Open questions for you

1. Zalo OA — obtainable, or are we permanently on manual export?
2. Is WhatsApp in scope, or Messenger + Instagram only?
3. Do you already have a Meta app with Advanced Access, or do we start App Review from zero?
4. Roughly how many conversations per run, and how far back for the first backfill?
5. Do attachments/images matter, or is text sufficient for the columns you'll define?
