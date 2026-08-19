<title>Lavabo — Zalo orders to Excel</title>

# Lavabo

Turns order messages from a Zalo group chat into the monthly Excel order sheet.

You copy the chat. It finds the orders, reads them, and writes the workbook.

---

## For whoever is running it

### 1. Install (once)

Download the repo, then:

| | |
|---|---|
| **macOS** | double-click **`start.command`** |
| **Windows** | double-click **`start.bat`** |
| Terminal | `bash start.sh` |

First launch installs everything and asks two questions:

- **Gemini API key** — free, no card. Get one at <https://aistudio.google.com/apikey>
- **Người chốt đơn** — the name to put in that column

Both are saved. You are never asked again.

> Needs **Python 3.11+**. macOS ships 3.9, so if it complains:
> `brew install python@3.12`, then double-click again.

### 2. Copy orders from Zalo

Leave the app open. In Zalo, open the group chat, scroll up through the month, then
**select all and copy** (`Cmd+A`/`Cmd+C`, or `Ctrl+A`/`Ctrl+C` on Windows).

The counter on screen goes up. Copy in chunks as you scroll — overlapping is fine, each
order is saved once.

Only messages starting with an order header are kept. Both of these are recognised:

```
15/8 đơn 1 - Meloxicam
1 tủ BC52, gương bo, mặt tinh thể - 80- 401
2 sen cơ như hình
Xóm 3 thôn vạn đồn, xã hồng dũng, thái thuỵ, TB
0367002126
Tổng 29tr
Đã cọc 500k
```

```
2/7 đơn 2 (Trần Thị Liên)
...
```

Everything else in the chat is ignored, and only the selected month is captured.

> **`Ctrl+A` only copies what Zalo has loaded.** It is not a size limit you can raise —
> Zalo lazy-loads history, so a select-all reaches back exactly as far as you scrolled.
> One sweep over a whole month usually misses the start of it.
>
> Work in chunks: scroll up, copy, paste, repeat, letting the chunks overlap. The app
> reports which days of the month each paste reached, and warns when order numbers skip
> (a day with `đơn 2` and `đơn 4` but no `đơn 3` means one is still sitting further up).
> Nothing is lost by pasting the same stretch twice.

### 3. Pick who closed them, press `1` or `2`

```
  Người chốt đơn: Trà My

  [1]  Xuất file Excel mới
  [2]  Thêm vào file quản lý   (QUẢN LÝ ĐƠN SENKAHOMES.xlsx)
  [3]  Đổi người chốt đơn
  [4]  Đổi tháng
```

`[1]` writes a fresh file you can check before touching anything. `[2]` adds the month's
orders straight into your own workbook — it backs the file up first, skips orders already
in the sheet, and refuses to write if the rows it needs aren't empty. Set which workbook
under `app.workbook` in `config/config.yaml`, or press `[2]` and paste the path once.

`[4]` matters at the start of a month: on 2 September the app is looking for September
orders, so switch to August to finish closing it.

That name is recorded **per order**, not per session, so if two people's orders are in one
paste you can switch with `[2]` and paste the rest. Re-pasting an order with a different
name corrects it, and costs nothing — the name is stored beside the order, so changing it
never re-runs the AI.

It matters more than it looks: your sheet totals revenue with
`=SUMIF($L:$L,"Trà My",$G:$G)`, so a wrong name here moves money between staff.

It reads the orders, extracts them with AI, and writes
`data/out/donhang-YYYYMM.xlsx` — the same 12 columns as the existing sheet, one row per
line item, ready to paste in, followed by three review columns that exist only in this
file (see [What it fills in](#what-it-fills-in)). `[2]` writes the 12 columns and nothing
else, so your own workbook keeps its shape.

That is the whole job.

### Browser version — works on phone, laptop and PC

Same pipeline, reached through a browser instead of the terminal.

| | |
|---|---|
| macOS | double-click **`web.command`** |
| Windows | double-click **`web.bat`** |
| Terminal | `bash web.sh --lan`, or on Windows `web.bat --lan` |

First run on Windows installs everything itself via `scripts\setup.ps1`; it needs
Python 3.11+ with *Add python.exe to PATH* ticked. Nothing else differs — the page, the
capture and the Excel writing are identical on both.

Without `--lan` it listens on this machine only. With `--lan` it prints a second URL to
open on a phone connected to the same wifi. Double-clicking cannot pass `--lan`, so run
it from a terminal when you want the phone to reach it.

For a phone that is **not** on the shop wifi — on 4G, at a customer's house — see
[docs/08-tailscale.md](docs/08-tailscale.md): Tailscale keeps it private to your own devices,
or a tunnel (ngrok / Cloudflare) gives a public URL that needs nothing installed, with a
login in front.

The gear icon opens **Cài đặt**: AI provider, API key (with a *Kiểm tra key* button that
asks the provider before you commit to it), model, the workbook path and the default
closer. Saving writes `config/config.yaml` and `.env` and reloads them in place — no
restart. The key is written but never sent back to the browser; the screen shows only
whether one is stored and a masked hint.

```
  máy này:      http://127.0.0.1:8765
  điện thoại:   http://192.168.1.24:8765
```

On the phone: copy the chat in Zalo, pick **Người chốt đơn**, paste into the box, tap
**Lưu đơn**, then **Xuất file Excel mới** and download. The picked name is remembered on
that device, so the phone and the laptop can be two different people capturing their own
orders. **Thêm vào file quản lý** does the same as `[2]` above, writing into the workbook
on the machine running the server — so there is nothing to download, and no second copy
to edit by mistake. The month and year selectors at the top cover this year and last, and
apply to everyone using the page — with a line under them stating plainly which month is
being captured when it is not the current one.

> The page has no password. Anyone on the same network can open it while it runs, so use
> it on your own wifi rather than a public one, and close it when you are done.

---

## What it fills in

| Column | Where it comes from |
|---|---|
| STT | counted |
| NGÀY CHỐT | the `15/8` in the header |
| Tên KH | the name in the header |
| Địa chỉ | read from the message (phone appended) |
| Tên sản phẩm / Số lượng | read from the message, one row each |
| Tổng Tiền hóa đơn | read, then converted (`5.800` → 5,800,000) |
| Xe thu hộ | **calculated**: Tổng − Cọc |
| Cọc | read, then converted |
| Trạng thái | `New` |
| Ngày hẹn giao | left blank |
| Người chốt đơn | **who you picked before pasting** — stored per order |

Only the address, the items and the two money figures involve AI. Everything else is read
from the header, calculated, or fixed — so the parts most easily got wrong are the parts
never guessed at.

Amber cells mean the AI found nothing there, rather than zero.

### Three more columns, in the exported file only

`Xuất file Excel mới` adds these after column L. **Thêm vào file quản lý** does not write
them, so your own workbook is untouched:

| Column | What it holds |
|---|---|
| Bổ sung | a later message about this order, kept word for word |
| Số tiền bổ sung | the money that message states — read out so you can see it, but **never added to Tổng** |
| Cần xem lại | why a human should look: `có bổ sung`, `trùng số đơn`, `2 phiên bản`, `bổ sung — chưa chắc`, `chưa qua AI` |

Rows with anything in **Cần xem lại** are tinted, the whole order, not just its first row.

Nothing here is decided for you, on purpose. When a later message changes an order, the
app cannot tell a correction from an add-on, so it never edits the money it already saved:

- **A later message with no header** (`đã thu thêm 2tr`) is attached to the order it
  belongs to, in `Bổ sung`, and the order is flagged.
- **A second full version of the same order** gets a row of its own — same date and
  customer, no STT and no money — so both versions are visible and neither is counted
  twice. Marked `2 phiên bản`.
- **The same day and số đơn arriving as two separate orders** is marked `trùng số đơn`.
  This one *does* count twice in the total until you delete a row, which is exactly why it
  is flagged rather than merged.

So `=SUM` and `=SUMIF` over the exported columns stay right while you review, and you
decide what the order really was.

---

## If something looks off

| | |
|---|---|
| Counter not moving when you copy | the messages have no `ngày/tháng đơn N` header, or they are from another month |
| A total looks wrong | tell us the exact text — it is a conversion rule, fixed without re-running the AI |
| Rows nearly empty | extraction failed; run `lavabo inspect` (below) to see why |
| "rate limited, retrying" | normal on the free tier, it waits and continues |
| Orders missing from the export | the paste never reached them — see the day range it reports, scroll further up in Zalo and paste again |
| "Có thể sót đơn — số thứ tự bị nhảy cách" | a `đơn N` is missing between two captured ones; scroll up and paste that stretch |
| An order counted twice in the total | flagged `trùng số đơn` and tinted — delete the row you do not want |
| A tinted row with no money | a second version of an order, kept for you to compare; it is deliberately not in the total |
| `ZoneInfoNotFoundError: Asia/Ho_Chi_Minh` | Windows has no timezone database: `.venv\Scripts\python.exe -m pip install tzdata`, then restart |

---

## For developers

The app is a front end over a CLI that does the same work in steps:

```bash
source .venv/bin/activate

lavabo config      # effective settings, and drift from config.example.yaml
lavabo check       # API key valid? paths? schema?
lavabo models      # which models this key can use
lavabo inspect     # what is stored per order, including failures

lavabo ingest --source zalo
lavabo extract
lavabo load --layout senkahomes --out data/out/report.xlsx --month 8 --year 2026
lavabo append --into "QUẢN LÝ ĐƠN SENKAHOMES.xlsx" --month 8 --year 2026 --dry-run
lavabo verify
```

Capture can also be run on its own, with more control:

```bash
python scripts/zalo_capture.py             # current month
python scripts/zalo_capture.py --month 7   # a different month
python scripts/zalo_capture.py --retrim    # re-trim files captured earlier
python scripts/zalo_capture.py --debug     # explain what was accepted or rejected
```

### Who splits the paste into orders

`extract.ai_segmentation` in `config/config.yaml`:

| | |
|---|---|
| `off` (default) | the regexes in `scripts/zalo_capture.py`. Capture is local, instant, free, and needs no API key |
| `shadow` | both run, **the regex result is still used**, differences land in `data/inbox/raw/zalo/shadow.log` |
| `on` | the model decides; regexes stay as cross-check and fallback |

Every paste is written to `data/inbox/raw/zalo/` before anything parses it, so a failed
call costs a retry over stored text rather than another scroll through Zalo. In `on`,
orders the fallback captured are marked `chưa qua AI` until a later paste reaches the
model.

`shadow` and `on` cost one API call per paste. Read a week of `shadow.log` before
switching to `on`.

### Documentation

| | |
|---|---|
| [docs/00-quickstart.md](docs/00-quickstart.md) | manual first run, step by step |
| [docs/01-source-verification.md](docs/01-source-verification.md) | why Zalo is copy-paste and Meta is an API |
| [docs/02-agent-plan.md](docs/02-agent-plan.md) | architecture |
| [docs/03-zalo-runbook.md](docs/03-zalo-runbook.md) | capture, trimming, order-header formats |
| [docs/04-meta-setup.md](docs/04-meta-setup.md) | Meta/Messenger connector (not used yet) |
| [docs/05-schema-guide.md](docs/05-schema-guide.md) | changing the extracted columns |
| [docs/06-output-mapping.md](docs/06-output-mapping.md) | column-by-column mapping and open questions |
| [docs/07-zalo-oa-flow.md](docs/07-zalo-oa-flow.md) | the Zalo OA group flow — built, but **needs an Official Account**, which a Zalo Business account is not |
| [docs/08-tailscale.md](docs/08-tailscale.md) | reaching the browser version off the shop wifi — Tailscale, or a public URL via ngrok / Cloudflare Tunnel |
| [docs/09-cloud-architecture.md](docs/09-cloud-architecture.md) | plan for moving to Cloud Run — what breaks, what it costs, and the cheaper alternative |
| [docs/10-setup-guide.md](docs/10-setup-guide.md) | **start here on a new machine** — install, Gemini key, autostart, Tailscale, first run |

### Shape of it

```
start.command / start.bat / start.sh   launchers
scripts/  lavabo_app.py                the one-screen app
          zalo_capture.py              clipboard capture, order splitting
          setup.sh / setup.ps1         install
          lavabo_web.py                browser version (phone/laptop/PC)
          web/index.html               its page — HTML/CSS/JS, edit and refresh
          lavabo_webhook.py            Zalo OA webhook receiver
          probe_zalo_*.py              checks on Zalo's own export
src/lavabo/
  connectors/  zalo_export.py          parses captured notes
               zalo_oa.py              OA group webhook events -> orders
               meta_graph.py           Messenger/Instagram (built, unused)
  extract/     gemini, anthropic       same interface, swap in config
  load/        senkahomes.py           the 12-column layout
               excel.py                generic layout
  closers.py   who chốt each order, stored beside them
  pipeline.py  the steps behind the buttons, shared by both front ends
  settings.py  reads/writes config.yaml and .env for the settings screen
  money.py     Vietnamese amounts -> VND
  store.py     SQLite staging + extraction cache
data/          inbox/ staging.db out/  gitignored — never commit chat content
```

### Notes

- Conversation text goes to the configured LLM provider and nowhere else.
- API keys live in `.env` only; `config/config.yaml` holds no secrets.
- `data/` is gitignored. Customer messages must not be committed.
- Extractions are cached on content + schema + prompt + model, so re-running is cheap and
  a schema change correctly forces a re-run.
