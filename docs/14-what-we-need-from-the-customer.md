# Current status, and what we need from the customer

Written after an audit of the repo at `6376d8c`. Every status line below is backed by a
command that was actually run, not by memory — §1 shows the output.

The short version: **the Zalo→Excel pipeline is built and tested. Nothing Facebook-related
has ever been connected.** The Meta connector can read conversations in principle and has
never been pointed at a real Page; nothing anywhere in the repo can *send* a message. So
every Facebook item in this document is blocked on access we do not have, and the list in
§3 is what unblocks it.

---

## 1. What the audit found

```
$ lavabo check
schema:     NOT READY — config/schema.yaml not found
timezone:   Asia/Ho_Chi_Minh
FAIL llm:   ANTHROPIC_API_KEY is not set
OK   zalo:  0 file(s) found — WARNING: zalo.own_names is empty
FAIL messenger: page_id/instagram_id not configured

$ staging db: conversations 0, messages 0, extractions 0
```

| Piece | Status | How we know |
|---|---|---|
| Zalo paste → segment → extract → Excel | **Built, 160 tests passing** | `python -m unittest discover -s tests` |
| LLM extraction (Gemini or Claude) | Built; **no API key configured** | `FAIL llm: ANTHROPIC_API_KEY is not set` |
| Intake pack tooling (`lavabo kb init` / `kb check`) | **Built, 26 tests** | added this week |
| Meta **reading** (`ingest --source meta`) | Code written, **never run against a real Page** | `FAIL messenger: page_id not configured`; the connector only ever calls `session.get` |
| Meta **replying** | **Does not exist** | no Send API, no `message_attachments`, no `pass_thread_control` anywhere in `src/` or `scripts/` |
| Knowledge storage (`kb_docs`, `kb_chunks`, `kb_images`, `agent_turns`) | **Proposed only** — [docs/11](11-facebook-reply-agent.md) §4.2 | zero occurrences in `store.py` |
| Business AI (Path A) | Not started; it is customer-side clicking, not code | [docs/12](12-business-ai-step-1.md) |
| Zalo OA webhook | Built but **wire format unverified** — the source says so | `connectors/zalo_oa.py` header |

Note this is the *repository*. If the shop already runs the Zalo pipeline on their own
machine, their `config.yaml` and `.env` live there and are not visible from here — §3.3
asks for that state rather than assuming it.

---

## 2. Two corrections to the earlier plan

Both came out of "we have no Messenger history and no integration", and both change work
that was planned:

1. **The eval set cannot be built from the shop's own history.** [docs/11](11-facebook-reply-agent.md)
   §8 said to pull the 100 most common real questions out of staging. Staging is empty and
   `ingest --source meta` has never authenticated. Until access exists, the test set is the
   scripts in [docs/12](12-business-ai-step-1.md) §4 and [docs/13](13-business-ai-step-2.md)
   §9 plus the three sample conversations the shop writes in `voice.md` — hand-written, not
   mined. Corrected in the docs.
2. **`lavabo kb mine` is off the table for now**, and `kb feed` waits on a filled catalogue.
   Neither is worth building before §3.1 and §3.2 land.

There is also a fork we cannot resolve from here, and it matters:

> **Does the Page already have customer messages in its inbox?**
>
> - **Yes** → Business AI's "Lịch sử trò chuyện" knowledge source has something to learn
>   from, and the shop's real tone comes along for free.
> - **No / very few** → that knowledge source is empty, and **`faq.xlsx` carries the entire
>   load.** The agent will only ever be as good as those 30 rows, and we should ask for 50.
>
> It is question 2 in §3.1, and the answer changes how hard we push on the FAQ file.

---

## 3. What we need from the customer

```mermaid
flowchart TD
    A["A — Meta access<br/>§3.1"] --> A1["See whether Business AI<br/>exists for this account"]
    A --> A2["Turn off conflicting<br/>automations"]
    A --> A3["Read the Page inbox<br/>→ real FAQ, real tone"]
    B["B — Knowledge pack<br/>§3.2"] --> B1["Step 1 can answer<br/>ship / bảo hành / địa chỉ"]
    B --> B2["catalog.xlsx → step 2<br/>prices"]
    C["C — Decisions<br/>§3.3"] --> C1["Handoff lands on a person"]
    C --> C2["Prices stay true"]
    A1 --> GO(["Step 1 live"])
    B1 --> GO
    C1 --> GO
    GO --> S2(["Step 2: prices"])
    B2 --> S2
    C2 --> S2
    D["D — Only if Path B<br/>§3.4"] -.-> PB(["Custom agent"])

    style A fill:#f8d7da,stroke:#721c24,color:#111
    style B fill:#fff3cd,stroke:#856404,color:#111
    style C fill:#fff3cd,stroke:#856404,color:#111
    style D fill:#cce5ff,stroke:#004085,color:#111
    style GO fill:#d4edda,stroke:#155724,color:#111
    style S2 fill:#d4edda,stroke:#155724,color:#111
```

### 3.1 Meta access — **P0, nothing Facebook moves without this**

| # | What we need | Why | Effort |
|---|---|---|---|
| 1 | **Business portfolio: Employee access, AND the Page asset assigned with Toàn quyền kiểm soát** — see below | Everything in [docs/12](12-business-ai-step-1.md) is a settings screen | 10 min |
| 2 | **Answer: does the Page get customer messages today?** Roughly how many a day, and is there history in the inbox? | Decides whether Business AI can learn from past chats — see the fork in §2 | 1 min |
| 3 | **Is the Page inside a Business Portfolio** (Trình quản lý doanh nghiệp)? Which one? | Required for verification and for any API path | 5 min |
| 4 | **Business Verification status** — done / in progress / not started. If not started, **start it today** (GPKD, mã số thuế, địa chỉ, điện thoại) | The only item with an unbounded external clock. Needed for Path B, useful regardless | 30 min + days of waiting |
| 5 | **Is any third-party chatbot connected** to the Page (Abit, vPage/Nhanh, Botcake, AhaChat, Manychat)? | Two automations in one inbox = duplicate replies. It has to be off before we start | 5 min |
| 6 | ~~Screenshot of the automations panel~~ **DONE 13/09/2026** | **Meta Business Agent is available** and currently off. Path A confirmed. [/latest/business_ai/knowledge](https://business.facebook.com/latest/business_ai/knowledge) | — |

#### Getting item 1 right

Two separate grants, and being added as a business user alone gives **nothing**:

| Layer | Ask for | Why not more |
|---|---|---|
| Business portfolio | **Employee access** (Nhân viên) | Portfolio Admin also manages people, assets and billing. Not needed until Path B |
| The Page, as an assigned asset | **Toàn quyền kiểm soát** (full control) | Partial access covers content, comments, messages, ads and insights but **cannot open Page settings**, and Tự động hóa / Business AI live there. There is no narrower permission that reaches them |

Their click path, with links:

1. [business.facebook.com/settings/people](https://business.facebook.com/settings/people)
   → Thêm người → email → **Nhân viên**.
2. [business.facebook.com/settings/pages](https://business.facebook.com/settings/pages)
   → chọn Trang → Assign people → tick **Toàn quyền kiểm soát**.

**Step 2 is the one that gets skipped**, and the symptom is confusing: the invited person
appears under Người dùng, sees the Page, and finds the settings unreachable. Verify from
`/settings/pages`, not from `/settings/people`.

Full control includes removing other admins and deleting the Page, so ask for it with the
mitigations attached: the owner keeps their own full control (never be the sole
controller), it is revocable in two clicks, and we step down to Messages-only access once
setup is done. If they would rather not grant it at all, a booked screen-share where the
owner clicks and we guide works — slower, and the answer to "can you just fix that one
thing" becomes another meeting.

**The invite goes to an email; the access attaches to a Facebook account.** Business
portfolios run on Facebook user identities — there is no standalone Meta business login.
Clicking the invite link binds the access to **whichever Facebook account that browser is
logged into at that moment**, and the email need not match it. So decide which account
should hold this, sign into it first (a clean profile or incognito avoids accepting as
whatever Chrome remembered), and only then open the link. Accepting as the wrong identity
is fixed only by the customer removing and re-inviting.

**Turn on two-factor authentication on that account before the invite is sent.** Portfolios
commonly require it and the invite fails without it; this is the usual "the link doesn't
work".

**Do not create a second Facebook account for this.** It is the natural instinct and it is
a trap: Meta allows one personal account per person, duplicates get disabled, and a
disabled account takes the client's business access with it.

Not yet: the **catalogue** asset (Commerce Manager) matters only at step 2, and portfolio
Admin belongs to §3.4.

### 3.2 The knowledge pack — **P0, this is the shop's homework**

**The pack is committed**, so whoever sends it to the shop needs no Python. Two shapes:

> **This shop: send the folder.** They have a PC with Excel, which is the version where
> the dropdowns work and the price column refuses `2tr850` as it is typed — caught in the
> cell rather than in a report a week later. Send the files loose; Zalo and Messenger
> handle `.zip` attachments badly.
>
> Still worth confirming *who* types. "The shop has a PC" and "the person who knows the
> prices uses that PC" are not the same sentence — if the owner dictates and someone else
> types, switch to the one-file version so both can see it at once.


| | For | |
|---|---|---|
| [`templates/intake/`](../templates/intake) | A PC with Excel, someone comfortable with files | Dropdowns, header comments, and a price cell that rejects `2tr850` as it is typed |
| [`templates/lavabo-intake-onefile.xlsx`](../templates/lavabo-intake-onefile.xlsx) | **A phone, or no Excel** | Upload to Drive → open with Google Sheets → share one link. Every form is a tab, including the text ones. Sheets keeps the dropdowns; it drops the whole-number guard, so `kb check --file` is what catches a bad price instead |

Both are generated output, kept honest by `tests/test_intake_pack.py`, which compares them
cell by cell against a fresh generation and fails with the regenerate command. After any
spec change:

```bash
lavabo kb init --dir templates/intake --force
lavabo kb init --one-file templates/lavabo-intake-onefile.xlsx --force
```

Checking what comes back:

```bash
lavabo kb check --dir intake            # the folder
lavabo kb check --file pack.xlsx        # the Google Sheets copy, downloaded as .xlsx
```

Media arrives separately, straight off a phone — a folder per mã SP, any filenames:

```bash
lavabo kb media --from ~/Downloads/ANH-SAN-PHAM --dir intake   # a folder
lavabo kb media --from ~/Downloads/pack.xlsx --dir intake          # photos pasted into the workbook
```

It renames, rotates, downscales, pulls stills from demo videos, writes `images.xlsx`, and
reports what it could not place: HEIC files (with the one iPhone setting that fixes them
for good), loose photos belonging to no product, and codes absent from `catalog.xlsx`. It
never guesses which product a loose photo belongs to — an unattached photo costs a missing
picture, a misattached one puts the wrong product in front of a customer. It is a set of forms now, not a blank
page: dropdowns on every list column, a comment on every header, an instructions sheet, and
a price cell that rejects `2tr850` as it is typed.

The three text files are forms too. Every answer is a `[chưa điền]` marker the shop
overwrites, with an example beside it, so `kb check` can report **which answers are still
missing by name** — `store.md: còn 7 mục chưa điền: Tên shop, Địa chỉ 1, Hotline…`. Without
that, a file returned untouched and a file deliberately left empty look identical, and
"we sent you everything" and "half of it is blank" are both true and unarguable.

| File | Priority | Notes |
|---|---|---|
| `store.md` | **P0** | 10 minutes. Địa chỉ, giờ, hotline |
| `policies.md` | **P0** | Cọc, bảo hành, đổi trả, lắp đặt |
| `shipping.xlsx` | **P0** | One row per province they actually ship to |
| `faq.xlsx` | **P0** | 30 rows minimum — **50 if the answer to §3.1 #2 is "no history"** |
| `voice.md` | **P0** | Including the three sample conversations, written by whoever answers messages today |
| `dont_say.md` | P1 | |
| `catalog.xlsx` | **P1 — gates step 2 entirely** | The big one. Top 50 sellers is enough to start |
| Ảnh sản phẩm | P1 | **From their phone — do not ask them to rename anything.** One Drive folder per mã SP, photos dropped in, folder shared. `00-GUI-ANH-TU-DIEN-THOAI.md` in the pack walks them through it |
| Video demo | P2 | Same folders. `lavabo kb media` pulls the sharpest frames out — for many SKUs an installation video is the only picture of the product in a real bathroom |
| `synonyms.xlsx`, `promotions.xlsx`, `images.xlsx` | P2 | Optional |

They send it back; we run `lavabo kb check --dir intake` and it either passes or says
exactly which row is wrong.

### 3.3 Decisions only the owner can make — **P1, before go-live**

| # | Decision | Consequence of not deciding |
|---|---|---|
| 7 | **Who is the escalation person**, and what hours? | Handoffs land nowhere, which is worse than no agent |
| 8 | **Is stock tracked, honestly?** ([docs/13](13-business-ai-step-2.md) §5 option a or b) | The agent says "còn hàng" about things that have to be ordered |
| 9 | **Who owns the weekly 20-minute price refresh?** | Without a name, **do not do step 2 at all** — a catalogue nobody refreshes quotes last quarter to everyone |
| 10 | **Privacy policy URL** — exists, or needs writing? | Blocks App Review later; harmless to sort now |
| 11 | **Is the Zalo→Excel pipeline already running on a shop machine?** If so: which machine, is `config.yaml` filled in, whose Gemini key? | We may be about to duplicate something that already works |

### 3.4 Only if we go to Path B — **P2, do not collect yet**

Listed so nobody is surprised later: a Meta Business app + Messenger product, a long-lived
Page token (`META_PAGE_TOKEN`), the app secret for webhook signatures, App Review for
`pages_messaging` Advanced Access, and a machine that is awake 24/7 (the shop's PC behind a
tunnel, or ~5 USD/month for a small server). **None of this is needed for Path A**, and
asking for it now only slows the part that can start this week.

---

## 4. What we do *not* need from them

- Any technical work. Everything in §3.2 is Excel, photos and a text file.
- A website.
- Their Zalo group history — that pipeline is separate and already works.
- Product data in any particular system. A messy price list is fine; `kb check` will say
  what to fix.

---

## 5. The order

1. **Today:** §3.1 items 1–6, and start Business Verification (#4).
2. **Today:** `lavabo kb init`, send the folder with §3.2's priority list attached.
3. **When the screenshot arrives:** the Path A / Path B fork resolves, and
   [docs/12](12-business-ai-step-1.md) step 1 becomes a two-hour job.
4. **When `store/policies/shipping/faq/voice` come back and pass `kb check`:** configure
   step 1, run the 20-question script, go live narrow.
5. **When `catalog.xlsx` passes and #9 has a name:** [docs/13](13-business-ai-step-2.md), step 2.

Steps 1 and 2 are independent and both can start today. Everything else waits on them.

---

## Phụ lục — bản gửi cho shop

Copy-paste, in Vietnamese, for the shop directly:

```
Chào anh/chị, để bật trợ lý trả lời tin nhắn Facebook cho shop, bên em cần
mấy thứ sau ạ:

A. QUYỀN TRUY CẬP (làm được ngay hôm nay, ~30 phút)
1. Thêm em vào Trình quản lý doanh nghiệp:
   Cài đặt → Người dùng → Thêm người → email [EMAIL] → chọn quyền
   "Nhân viên", rồi ở bước gán tài sản chọn Trang [TÊN TRANG] và bật
   "Toàn quyền kiểm soát".
   Bước cuối bắt buộc — Tự động hóa và Business AI nằm trong Cài đặt
   trang, quyền thấp hơn không mở được. Anh/chị vẫn giữ nguyên quyền của
   mình và gỡ quyền của em bất cứ lúc nào cũng được.
   (Hoặc hẹn 30 phút gọi video để anh/chị bấm, bên em hướng dẫn.)
2. Cho em hỏi: trang Facebook hiện có khách nhắn tin không ạ? Khoảng bao
   nhiêu tin mỗi ngày, và trong hộp thư đã có tin nhắn cũ chưa?
3. Trang đã nằm trong Trình quản lý doanh nghiệp (Business Manager) chưa ạ?
4. Doanh nghiệp đã xác minh (Business Verification) chưa? Nếu chưa thì nên
   bắt đầu hôm nay — khâu này Meta duyệt lâu, cần giấy phép kinh doanh,
   mã số thuế, địa chỉ, số điện thoại.
5. Trang đang kết nối phần mềm chatbot nào không (Abit, Nhanh/vPage,
   Botcake, AhaChat...)? Nếu có thì cần tắt trước, không thì hai con bot
   sẽ trả lời chồng lên nhau.
6. Anh/chị chụp giúp em màn hình: Meta Business Suite → Hộp thư → Tự động hóa.

B. THÔNG TIN ĐỂ AI TRẢ LỜI ĐÚNG (quan trọng nhất)
Em gửi kèm thư mục biểu mẫu, anh/chị điền vào:
  - store.md       thông tin cửa hàng            (bắt buộc, 10 phút)
  - policies.md    cọc, bảo hành, đổi trả        (bắt buộc)
  - shipping.xlsx  phí ship theo tỉnh            (bắt buộc)
  - faq.xlsx       30-50 câu khách hay hỏi       (bắt buộc, quan trọng nhất)
  - voice.md       cách shop xưng hô + 3 hội thoại mẫu (bắt buộc)
  - catalog.xlsx   bảng giá sản phẩm             (làm sau cũng được, nhưng
                   chưa có file này thì AI chưa được phép báo giá)
  - ảnh sản phẩm   đặt tên theo mã sản phẩm

C. BA QUYẾT ĐỊNH CỦA ANH/CHỊ
7. Ai là người nhận các tin nhắn AI chuyển sang, và trong khung giờ nào?
8. Shop có đếm tồn kho thật không? Nếu không, tất cả sản phẩm sẽ để
   "đặt trước" — đúng hơn và an toàn hơn.
9. Ai là người cập nhật lại bảng giá mỗi tuần (khoảng 20 phút)? Nếu chưa
   có ai nhận việc này thì bên em khuyên CHƯA nên cho AI báo giá.

Riêng mục B, anh/chị cứ điền được đến đâu gửi đến đó — em kiểm tra file
bằng công cụ và sẽ báo lại chính xác dòng nào cần sửa ạ.
```

---

## Related

| Doc | |
|---|---|
| [docs/15-end-to-end-flow.md](15-end-to-end-flow.md) | **every step drawn**, including the shop's, with the gates between them |
| [docs/18-google-drive-source.md](18-google-drive-source.md) | where the pack now lands: one Drive folder, connected to the agent |
| [docs/11-facebook-reply-agent.md](11-facebook-reply-agent.md) | the full plan and the intake spec |
| [docs/12-business-ai-step-1.md](12-business-ai-step-1.md) | step 1 — what the access in §3.1 unlocks |
| [docs/13-business-ai-step-2.md](13-business-ai-step-2.md) | step 2 — what `catalog.xlsx` unlocks |
| [docs/04-meta-setup.md](04-meta-setup.md) | the Path B token and permission work in §3.4 |
