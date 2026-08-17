# Moving Lavabo to the cloud — architecture plan

Written against the repo at `78aeab0`: 6,704 lines, of which `cli.py` is 760, `index.html`
552, `lavabo_web.py` 410. This is a plan, not a change — nothing here is implemented yet.

---

## 1. The short version

**Cloud Run is the right target, and the app cannot run on it as written.** Not because of
its size — it is small — but because every piece of state it owns is a file on the machine
it runs on, and Cloud Run has no such machine. The container's filesystem is in-memory,
per-instance, and gone when the instance goes.

Three things follow, in descending order of how much they cost you:

1. **`lavabo append` — writing into the shop's own workbook — cannot survive the move.**
   The workbook is an `.xlsx` on a Mac. Cloud Run cannot reach it. This is the feature the
   month-end close actually runs on. §3 is about what replaces it, and the answer is
   probably Google Sheets.
2. **The export job's design breaks on Cloud Run specifically.** It responds immediately
   and works in a background thread (`lavabo_web.py:328`), tracking progress in a module
   global (`JOB`, line 37). Cloud Run throttles CPU outside a request and load-balances
   polls across instances, so the work stalls and the page polls an instance that has
   never heard of the job. This is a rewrite, not a config flag.
3. **The app has no login.** Local plus Tailscale made that a defensible choice. A public
   URL does not: the settings screen stores an API key and the app writes to your books.

**Free is achievable** for a shop doing ~90 orders a month, but not on the stack you'd
reach for first — Cloud SQL has no free tier. See §5 for what does fit, and §6 for the
option that requires almost no rearchitecture at all, which you should read before
committing to Cloud Run.

---

## 2. What the scan found

Every module that touches disk, and what happens to it in a stateless container.

| What | Where | On Cloud Run |
|---|---|---|
| Staging DB (SQLite) | `config.py:200` → `store.py:101` | **Lost on every scale-to-zero.** Two instances = two divergent databases |
| Captured orders (`*.txt`) | `config.py:175`, written by `zalo_capture.save()` | **Lost.** The inbox is the source of truth before extraction |
| Closer sidecar (`_closers.json`) | `closers.py` | **Lost.** Người chốt đơn goes with it |
| Output workbooks | `pipeline.py:73`, served by `_download` (`lavabo_web.py:336`) | Survives one request on one instance; a poll landing elsewhere 404s |
| The shop's workbook | `app.workbook` → `load/append.py:159` | **Unreachable.** See §3 |
| API key in `.env` | `settings.py:write_env_var` | **Lost on redeploy**, and writing secrets into a container layer is wrong anyway |
| `config.yaml` writes | `settings.py:write_settings` | **Lost.** The settings screen has nowhere to persist to |
| Clipboard capture | `zalo_capture.py`, `lavabo_app.py` | **Impossible** — no clipboard in a container. The terminal app stays a local tool |
| Job state | `lavabo_web.py:37` | **Wrong per instance.** See §1.2 |

Nothing here is a surprise for a tool that was designed to run on one desk. It is the bill
for changing that assumption, and it is worth seeing in full before deciding.

**What ports cleanly and needs no thought:** `models.py`, `money.py`, `extract/`,
`load/senkahomes.py` (given a stream instead of a path), `connectors/zalo_export.py` (given
text instead of files), and the whole frontend. That is most of the interesting logic. The
work is in the seams, not the core.

---

## 3. The decision that dominates everything: the workbook

`append` writes into `QUẢN LÝ ĐƠN SENKAHOMES.xlsx` — backs it up, skips orders already
present, refuses to overwrite the summary block, and leaves the `Xe thu hộ` formula alone
so the sheet keeps computing it. That behaviour is the product. In the cloud the file is
not there.

Three ways out, and they are genuinely different products:

### A. Upload / download each time
Operator uploads the `.xlsx`, the app appends, they download it back and replace theirs.
Stateless, no new services, works next week.
**Cost:** a manual step at the exact moment you were trying to remove manual steps, and two
people uploading the same workbook silently lose one set of edits.

### B. The workbook lives in GCS
Cloud storage becomes the canonical copy; the Mac stops holding the master.
**Cost:** the shop must stop opening it locally, or accept conflicts. Excel has no merge.
You are running a shared document without a document server.

### C. Move the workbook to Google Sheets — **recommended**
Append rows via the Sheets API instead of openpyxl.
- The `=SUMIF($L:$L,"Trà My",$G:$G)` revenue split and `=G{n}-I{n}` keep working — Sheets
  has both.
- Concurrent editing is what Sheets is for; the conflict problem in A and B disappears.
- No upload, no download, no backup step — Sheets keeps its own version history, which is
  strictly better than the timestamped `.backup-*.xlsx` copies we write today.
- Free, and the shop can open it on a phone.

**Cost, honestly:** a one-time migration of the existing workbook, an OAuth or service
account setup, and a rewrite of `load/append.py` against a different API — its careful
logic (collision detection, already-present matching, formula preservation) has to be
re-implemented and re-tested against Sheets semantics. Roughly the biggest single item in
this plan. It is still the right call, because A and B both leave you managing a binary
file that two people want to write at once.

> If C is not acceptable, **reconsider Cloud Run entirely** and read §6 first. Most of the
> cost in this document exists to serve a stateless runtime; if the workbook must stay a
> local file, a stateless runtime is fighting you.

---

## 4. Target structure

The problem to solve structurally is that `Path` is hardcoded through the middle of the
app. The fix is ordinary: put interfaces at the edges, keep one codebase, let local and
cloud pick different implementations. Local stays a first-class target — the terminal app
and CLI keep working on a Mac with no cloud account.

```
src/lavabo/
  core/                    # no I/O, no config -- pure logic
    models.py  money.py  schema.py  prompt.py

  ports/                   # interfaces the core is written against
    inbox.py               # put_order / list_orders / read_order
    store.py               # conversations, extractions
    secrets.py             # get / set
    ledger.py              # the destination: append_orders, already_present
    jobs.py                # submit / status

  adapters/
    inbox/        local_fs.py       gcs.py
    store/        sqlite.py         postgres.py
    secrets/      dotenv.py         secret_manager.py
    ledger/       xlsx_local.py     gsheets.py
    jobs/         thread.py         cloud_tasks.py

  services/                # orchestration, was pipeline.py
    capture.py  extract.py  publish.py

  web/                     # FastAPI app
    api.py  auth.py  deps.py
  cli/
    main.py

web/                       # frontend, unchanged in kind
  index.html  app.js  styles.css

deploy/
  Dockerfile  cloudbuild.yaml  service.yaml  README.md
```

**Composition root.** One function reads the environment and builds the adapter set:
`LAVABO_ENV=local` wires `local_fs + sqlite + dotenv + xlsx_local + thread`;
`LAVABO_ENV=cloud` wires `gcs + postgres + secret_manager + gsheets + cloud_tasks`. Nothing
below the composition root knows which it got.

**Web framework: replace `http.server` with FastAPI.** Not fashion — the stdlib handler has
no graceful shutdown (Cloud Run sends SIGTERM and expects in-flight requests to finish), no
health endpoint, no request concurrency model worth deploying, and hand-rolled routing that
already grew a bug this month. FastAPI plus uvicorn gets all of that, and the frontend does
not change.

**Frontend: split `index.html`.** At 552 lines it is at the limit of one file. Into
`index.html` + `app.js` + `styles.css`, served as static files — and once they are static,
they can move to a CDN or Firebase Hosting later without touching the API.

---

## 5. Deployment on Google Cloud

### Shape

```
Cloud Build  ──build──▶  Artifact Registry  ──deploy──▶  Cloud Run (web)
   ▲                                                        │
   │ push to master                                         ├─▶ Cloud SQL / Neon  (orders, extractions)
GitHub                                                      ├─▶ GCS bucket        (captured .txt, outputs)
                                                            ├─▶ Secret Manager    (API keys)
                                                            └─▶ Sheets API        (the ledger)

Cloud Run Job (extract)  ◀── Cloud Tasks ◀── enqueued by the web service
```

**The extraction runs as a Cloud Run Job, not a thread.** This is the fix for §1.2. The web
service enqueues, returns immediately, and the job writes progress to the database, which
every instance can read. As a bonus the job can run with more CPU and a long timeout while
the web service stays small and scales to zero.

### Free-tier arithmetic

Verified for 2026 ([Google Cloud free features](https://docs.cloud.google.com/free/docs/free-cloud-features)):

| Service | Always-free allowance | This shop's usage |
|---|---|---|
| Cloud Run | 2M requests, 180k vCPU-s, 360k GiB-s per month | ~90 orders/month, a few operators. **Nowhere near the limit** |
| Cloud Build | 120 build-minutes/day | A ~2-minute image build per push. **Fine** |
| Artifact Registry | 0.5 GB | A slim Python image is ~150–250 MB. **Fine with a cleanup policy** |
| Secret Manager | small free allowance of active versions | Two keys. **Fine** |
| GCS | free allowance in US regions | Transcripts are text; outputs are small. **Fine** |
| **Cloud SQL** | **none** | **~$9+/month minimum.** This is what breaks "free" |

Two constraints worth designing around now rather than discovering later:

- **Free tier is per billing account and region-locked** to `us-central1`, `us-east1`,
  `us-west1`. A Vietnam-based shop therefore eats ~200 ms of latency per request for the
  free tier. Paste-and-wait tolerates that; it is not nothing.
- **Cold starts.** Scale-to-zero means the first request after idle pays container startup.
  For an app someone opens twice a day, that is a few seconds of blank screen unless you
  set `min-instances=1` — which leaves the free tier.

### Keeping it free: skip Cloud SQL

- **Postgres on [Neon](https://neon.tech) or [Supabase](https://supabase.com)** — real
  Postgres, free tier, scales to zero. Recommended: your data is relational and small, and
  you keep standard SQL.
- **Firestore** — has an always-free allowance, but it is a document store and `store.py`
  is relational. A worse fit for the sake of staying inside one vendor.
- **SQLite on a GCS FUSE mount** — do not. Concurrent writes over FUSE corrupt SQLite.

### Auth

Non-negotiable once there is a public URL. In increasing order of effort:
**Cloud Run IAM + IAP** (Google account gate, no app code, but every operator needs a Google
identity), **Firebase Auth** (email link — good on a shop phone), or a shared password with
a signed session cookie (least work, weakest, but strictly better than nothing).

Whichever: the settings screen must sit behind it, and the API key must move to Secret
Manager. Note the key can then no longer be edited from the page as freely — writing to
Secret Manager needs IAM permission the runtime probably should not hold. Expect the
settings screen to become read-mostly in cloud, with key rotation done by an admin.

---

## 6. The alternative you should price first

Everything above exists to make a filesystem-shaped app fit a stateless runtime. There is a
cloud option that skips the entire exercise: **a small always-on VM with a disk.**

[Oracle Cloud Always Free](https://www.oracle.com/cloud/free/) gives an Arm VM (up to 4
cores / 24 GB across the allowance) with block storage, free with no time limit. Also worth
pricing: [Fly.io](https://fly.io) with a persistent volume, or any ~$5/month VPS.

**On a VM, the app deploys essentially as-is.** SQLite works. The inbox works. `.env` works.
`app.workbook` works — the workbook lives on the VM's disk, and the append logic you already
have, backups and collision checks and all, runs unchanged. Add Tailscale to the VM and it
is never on the public internet, so the missing login stays a non-issue.

| | Cloud Run | Always-free VM |
|---|---|---|
| Code change | Large — §4 in full | Near zero: a systemd unit and a firewall rule |
| Workbook append | Rewrite (§3) | Works today |
| Auth required | Yes | No, if kept on Tailscale |
| Cost | Free without Cloud SQL | Free |
| Scales to zero | Yes | No — always on, which is why cold starts vanish |
| Ops burden | Patching is Google's | **Yours**: OS updates, disk, backups |
| Ceiling | High | One machine |

**Cloud Run is the better answer if** you expect more shops, more operators, or want CI/CD
and managed everything — and you accept §3 and §4 as the price. **The VM is the better
answer if** the goal is "reachable from anywhere without my laptop being on", which is what
started this conversation. The honest read is that the VM solves today's problem for a
fraction of the work, and Cloud Run solves the problem you may have in a year.

---

## 7. If you go with Cloud Run: phases

Each phase ends somewhere shippable. Do not start the next until the last one runs.

**Phase 0 — containerize, prove the pipe.** Dockerfile, Cloud Build trigger on `master`,
deploy to Cloud Run. State still ephemeral, so the app is a toy — but the build and deploy
path is real and never has to be debugged again while you are also debugging storage.

**Phase 1 — extract the ports.** Introduce `ports/` and the local adapters only. Everything
still runs on a laptop exactly as now; the CLI and terminal app must stay green. This is a
pure refactor and the only phase with no new services. Do not skip it — it is what stops
phases 2–4 becoming three parallel rewrites.

**Phase 2 — cloud storage adapters.** GCS for inbox and outputs, Postgres for the store, and
a migration for existing data. Now the app survives a restart.

**Phase 3 — auth and secrets.** Gate the service, move keys to Secret Manager. **Nothing is
exposed publicly before this phase lands.**

**Phase 4 — the job queue.** Move extraction to a Cloud Run Job with progress in the
database. Until this lands, exports over ~1 minute are unreliable in ways that look random.

**Phase 5 — the ledger.** Sheets adapter, workbook migrated, `append` re-implemented and
re-tested against the real sheet with its summary block and formulas.

**Phase 6 — frontend split and polish.** Static assets, cold-start tuning, cleanup policy on
Artifact Registry.

Phases 0–2 are where the risk is understated: "move SQLite to Postgres" is a day; migrating
the extraction cache without invalidating every cached extraction — which would re-bill the
whole history through the model — needs the schema fingerprinting in `store.py` read
carefully first.

---

## 8. Open questions

1. **Is the workbook allowed to become a Google Sheet?** This gates §3, and §3 gates the
   value of the whole migration. Everything else has a workaround; this does not.
2. **Who logs in?** Two named operators with Google accounts points at IAP; "whoever is on
   shift" points at Firebase Auth or a shared password.
3. **Does the terminal app stay?** It is the only thing using clipboard capture. Keeping it
   means maintaining a local path forever — which Phase 1 gives you for free, but it is a
   commitment.
4. **Latency vs free tier:** is a US region acceptable, or does Vietnam-region hosting (and
   the bill that comes with it) matter more?
5. **What is the actual goal** — "not tied to my laptop", or "a product other shops could
   use"? The first is §6. The second is §4–§7.
