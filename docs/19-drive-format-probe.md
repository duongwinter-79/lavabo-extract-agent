# Which file formats the Drive source actually works with

The connector's accepted formats are unknown, and the answer decides whether the shop
retypes a price list or not. This is the analysis, the assumption to build on, and a
half-hour probe that settles it.

> Nothing here is verified against Meta. `developers.facebook.com` and Business Suite are
> both blocked from this environment, so §2 is reasoning about likelihoods, not a
> quotation from documentation. §3 is how to replace it with fact.

---

## 1. Two questions, not one

The one everybody asks is **acceptance**: will it ingest `.xlsx`? The one that decides
whether this works is **comprehension**: having ingested a price table, does the agent
answer *the right row*?

They come apart badly. A spreadsheet the connector happily indexes can still be read as a
run-on wall of text, and the agent then answers "tủ 80" with the price from the line above.
That is the failure [docs/13](13-business-ai-step-2.md) §6.4 warns about for the
price-list-as-PDF fallback, and it does not announce itself — you get a confident, wrong
number rather than an error.

So the probe tests both, and the second one is the one that can veto a format.

---

## 2. The assumption to build on

Ranked by how likely each is to be accepted, with what it costs us:

| # | Outcome | Likelihood | What it costs |
|---|---|---|---|
| A | **Google Docs / Sheets native** | High — it is a Drive connector | Nothing. See below |
| B | **+ PDF** | High | Fine to read, **bad to maintain** — the shop cannot edit a PDF, so the weekly update breaks unless we regenerate it every time |
| C | **+ Office files (.xlsx, .docx) stored in Drive** | Medium | Best case: the intake pack goes in untouched |
| D | **+ .csv / .txt** | Medium | Usable for the price table; poor for prose |
| E | **Nothing usable, or the connector needs setup we have not seen** | Low | Fall back to **Thêm thông tin** / **Thêm bảng giá** uploads, and Drive becomes just our staging area |

### Build for A — Google Sheets — regardless of what the probe finds

Not because it is most likely, but because it is the best answer even if C turns out true:

- **The shop edits it more easily than Excel**, on a phone or a PC, with no file to send back.
- **It round-trips**: File → Download → `.xlsx` gives `lavabo kb check` and `lavabo kb feed`
  exactly the file they already parse. Our tooling needs **no change at all**.
- **Converting is free**: upload `catalog.xlsx` to Drive, open with Google Sheets, done. If
  C is true we skip even that.
- One live copy, which is the entire argument of [docs/18](18-google-drive-source.md).

So: **Sheets for the tables, Docs for the prose**, and treat C as a bonus that saves one
conversion step rather than as the plan.

### The fork worth watching

**Thêm bảng giá** is a dedicated price-list upload sitting right next to the Drive
connector. If it takes a structured file and reads it *as a table*, the better architecture
may be:

- **prices → Thêm bảng giá** (structured, purpose-built for it)
- **prose → Drive folder** (store info, policies, shipping, FAQ)

That is not a compromise — a purpose-built price importer beating a general document
crawler at reading a price table is exactly what you would expect. §3's probe C tests it.

---

## 3. The probe — about 30 minutes

**The agent stays off.** Chat thử answers from the knowledge without anything reaching a
customer, which is what makes this safe to do on a live account.

> **Use real content, not fake products.** A test file with invented SKUs and prices left
> behind in the knowledge is a fake price waiting to be quoted the day someone flips the
> toggle. Store information is the right probe material: harmless if it lingers, and true.

### Probe A — acceptance, one format at a time

Make the same short store-info content in each format, and add **one file at a time**:

1. Google Doc
2. Google Sheet
3. PDF
4. `.docx` / `.xlsx`

After each, ask in Chat thử: **"Shop ở đâu, mấy giờ mở cửa?"**

Record for each format: **indexed or not**, and **how long** until the answer changed —
that second number is the sync latency, which is
[docs/18](18-google-drive-source.md) §7's open question and matters for the weekly update.

One at a time is the point. Put four formats in at once and a correct answer tells you
nothing about which file it read.

### Probe B — comprehension, the one that can veto

Only in the formats that passed A. Take **five real products with prices you have
verified today** — not the fifteen-week-old ones — as a small table: mã, tên, kích thước,
giá, tình trạng.

Ask:

| Ask | Passes if |
|---|---|
| "Mã [a real code] giá bao nhiêu?" | The exact price of **that row** |
| "Mẫu 80cm màu trắng giá bao nhiêu?" | Finds the row by attributes, not by position |
| "Có mẫu nào dưới [X] không?" | Does not invent one; either answers from the table or declines |
| "[A code that is NOT in the table] giá bao nhiêu?" | **Does not guess a neighbouring row.** The most important row here |

A format that passes A and fails the last two rows of B is a format that will quote a
wrong price to a customer. Do not use it, however convenient it is.

### Probe C — the bảng giá route

Upload the same five rows through **Thêm bảng giá** and run Probe B's questions again.
Compare. If it wins, prices move there and the Drive folder keeps the prose.

### Probe D — can a customer see the cabinet

Only worth running while Commerce Manager is out of reach. A catalogue shows a product
card; this asks whether the Drive route can manage anything at all.

The photos folder is not connected and indexing it would achieve nothing — there is no
text in a `.jpg`, and no mechanism to attach a file to a reply. But **a URL is text**, and
text is what the connector carries. So the question is whether a link sitting in the price
table comes back out.

1. Share **one** photo from `02-ANH-SAN-PHAM` as "anyone with the link", and take the
   direct-image URL — the form that returns the image bytes, not the Drive viewer page.
2. Republish with the base: `lavabo kb publish --file <workbook> --image-base <URL thư mục>`.
   That adds a `link_anh` column to `05-bang-gia` and nothing else; without the flag the
   file is identical, column for column.
3. Ask in Chat thử: **"Mẫu [mã] trông thế nào?"** and **"Gửi em xem ảnh mẫu [mã] với"**.

| Passes if | Meaning |
|---|---|
| The reply contains the URL | The link survives indexing. Enough to be useful |
| Messenger renders it as a preview | Best case — a photo reaches the customer without Commerce |
| The URL comes back mangled or truncated | The column is worse than nothing; drop `--image-base` |
| No link at all | The connector drops URLs, or ranks that column away. Wait for Commerce |

One row first, for the same reason Probe A goes one format at a time. And the row must be
a **real product with its real photo** — a placeholder URL left in the knowledge is a
broken link waiting to be sent to a customer.

---

## 4. What each result changes

| Probe A says | Then |
|---|---|
| Sheets/Docs work | Proceed as planned. Shop edits in Drive; we download `.xlsx` for `kb check` |
| Office files work too | Skip the conversion — the intake pack goes in as-is |
| PDF only | Sheets stays the master; **we** export a PDF on every update. Note the extra step in the weekly ritual and expect it to be forgotten |
| Nothing indexes | Drive becomes our staging area only; knowledge goes in via Thêm thông tin / Thêm bảng giá by hand |

| Probe B says | Then |
|---|---|
| Right row, declines unknown codes | Prices can live in the Drive table |
| Right row, but guesses on unknown codes | Prices stay out until the Hướng dẫn guardrail (docs/12 §3.5) is in place, then re-test |
| Wrong rows | **Prices do not go in this format at all.** Try Probe C, and if that fails too, the agent launches on policy answers only — which is [docs/16](16-quick-setup.md)'s design anyway |

| Probe D says | Then |
|---|---|
| The link comes back, preview or not | Publish with `--image-base` and host the photos properly. A link is not a product card, but it beats describing a cabinet in words |
| Link mangled, or never surfaces | Publish without the flag. Photos wait for a Facebook Catalog — [docs/13](13-business-ai-step-2.md) §6 |

---

## 5. What comes back here

Whatever the probe finds replaces the guesses in
[docs/18](18-google-drive-source.md) §2 and §7, and the format table in §2 above. Send:

- which formats indexed, and the sync delay for each
- the four Probe B answers verbatim
- whether Thêm bảng giá beat the Drive table

It is thirty minutes that decides how the shop spends a day.

---

## Related

| Doc | |
|---|---|
| [docs/18-google-drive-source.md](18-google-drive-source.md) | the folder this feeds, and the cutover it is step 1 of |
| [docs/13-business-ai-step-2.md](13-business-ai-step-2.md) | §6.4 on why a price table read as text is dangerous |
| [docs/16-quick-setup.md](16-quick-setup.md) | the policy-only launch that works whatever the probe says |
