# End-to-end flow — every step, and whose step it is

One place to see the whole path from today to an agent answering price questions, with
each step attributed. Steps are numbered `S1`–`S28` and the table in §6 lists every one
with its owner, effort, and what it blocks.

The three actors, colour-coded in every diagram below:

| | Actor | Does |
|---|---|---|
| 🟡 | **Shop** (khách hàng) | Grants access, writes the knowledge pack, makes the business decisions |
| 🔵 | **Us** | Runs the tooling, configures Business Suite, tests, measures |
| ⚪ | **Meta** | Verification, rollout, catalogue ingestion — mostly waiting |

Nothing here is a schedule. It is a dependency order: **§6's "blocks" column is the real
content**, and the diagrams are the same information drawn.

---

## 1. Overview — four phases and the gate that ends each

```mermaid
flowchart TD
    START([Today]) --> P0

    subgraph P0["PHASE 0 — access + the pack"]
        direction LR
        P0a["Shop grants access<br/>S1-S6"] --- P0b["We send the forms<br/>S7-S8"]
    end

    P0 --> G0{"GATE 0<br/>Screenshot of<br/>Hộp thư → Tự động hóa"}
    G0 -->|"Business AI is there"| P1
    G0 -->|"Not rolled out"| PB["Path B<br/>custom agent<br/>docs/11"]

    subgraph P1["PHASE 1 — answers without prices"]
        direction LR
        P1a["Pack comes back,<br/>kb check passes<br/>S9-S11"] --- P1b["Configure + test<br/>S12-S16"]
    end

    P1 --> G1{"GATE 1<br/>20-question script:<br/>every 🔁 row hands off,<br/>zero prices"}
    G1 -->|Pass| LIVE1([Step 1 live<br/>S17-S18])
    G1 -->|Fail| P1

    LIVE1 --> G2{"GATE 2<br/>1 week clean +<br/>catalog.xlsx passes +<br/>someone owns the refresh"}
    G2 -->|"Any one missing"| WAIT["Stay on step 1.<br/>Prices keep handing off."]
    G2 -->|"All three"| P2

    subgraph P2["PHASE 2 — prices"]
        direction LR
        P2a["Shop: catalogue,<br/>photos, decisions<br/>S19-S23"] --- P2b["Us: feed, rules, test<br/>S24-S27"]
    end

    P2 --> G3{"GATE 3<br/>25-question script:<br/>zero wrong numbers"}
    G3 -->|Pass| LIVE2([Step 2 live<br/>S28])
    G3 -->|"One wrong number"| RB["Roll back to step 1<br/>in 5 minutes"]
    RB --> P2

    LIVE2 --> LOOP(["Weekly loop<br/>§5"])

    style P0 fill:#fffbe6,stroke:#856404,color:#111
    style P1 fill:#e7f1ff,stroke:#004085,color:#111
    style P2 fill:#e7f1ff,stroke:#004085,color:#111
    style G0 fill:#fff3cd,stroke:#856404,color:#111
    style G1 fill:#fff3cd,stroke:#856404,color:#111
    style G2 fill:#fff3cd,stroke:#856404,color:#111
    style G3 fill:#f8d7da,stroke:#721c24,color:#111
    style LIVE1 fill:#d4edda,stroke:#155724,color:#111
    style LIVE2 fill:#d4edda,stroke:#155724,color:#111
    style RB fill:#f8d7da,stroke:#721c24,color:#111
    style PB fill:#cce5ff,stroke:#004085,color:#111
    style WAIT fill:#eee,stroke:#666,color:#111
```

**Gate 3 is the only one that can send you backwards after going live**, and it is
deliberately harsh: one wrong number rolls the catalogue back out.

---

## 2. Phase 0 — access and the pack

Both columns start today and neither waits on the other.

```mermaid
flowchart TD
    subgraph SHOP["🟡 Shop"]
        S1["S1 · Cấp quyền toàn quyền<br/>trang Facebook"]
        S2["S2 · Trả lời: trang có<br/>khách nhắn tin chưa?"]
        S3["S3 · Xác nhận trang nằm trong<br/>Business Portfolio"]
        S4["S4 · BẮT ĐẦU xác minh<br/>doanh nghiệp"]
        S5["S5 · Gỡ chatbot bên thứ ba<br/>nếu đang có"]
        S6["S6 · Chụp màn hình<br/>Hộp thư → Tự động hóa"]
        S9["S9 · Điền store / policies /<br/>shipping / faq / voice"]
    end

    subgraph US["🔵 Us"]
        S7["S7 · lavabo kb init"]
        S8["S8 · Gửi intake-lavabo.zip<br/>+ lời nhắn docs/14"]
        S10["S10 · lavabo kb check"]
    end

    subgraph META["⚪ Meta"]
        M1["Xác minh doanh nghiệp<br/>vài ngày đến vài tuần"]
    end

    S7 --> S8 --> S9 --> S10
    S10 -->|"LỖI"| S11["S11 · Gửi lại đúng<br/>dòng cần sửa"]
    S11 --> S9
    S10 -->|"ĐẠT"| READY([Pack sẵn sàng])
    S4 --> M1
    S6 --> G0{"Gate 0"}
    S1 --> G0
    S5 --> G0

    style SHOP fill:#fffbe6,stroke:#856404,color:#111
    style US fill:#e7f1ff,stroke:#004085,color:#111
    style META fill:#f2f2f2,stroke:#666,color:#111
    style READY fill:#d4edda,stroke:#155724,color:#111
    style G0 fill:#fff3cd,stroke:#856404,color:#111
```

**S4 is the one to start today even though nothing needs it yet** — it is the only step
with an unbounded external clock, and Path B is unreachable without it.

**S5 is the one people skip.** A third-party chatbot still connected means two automations
in one inbox, replying over each other.

---

## 3. Phase 1 — the agent answers, without prices

```mermaid
flowchart TD
    subgraph US1["🔵 Us — Business Suite"]
        S13["S13 · Ngôn ngữ = Tiếng Việt,<br/>tắt Trả lời tức thì"]
        S14["S14 · Dán: thông tin, kiến thức,<br/>hướng dẫn tùy chỉnh,<br/>chủ đề chuyển nhân viên"]
        S15["S15 · Lời chào (có nói rõ là<br/>trợ lý tự động) + câu gợi ý"]
        S16["S16 · Chat thử — 20 câu"]
    end

    subgraph SHOP1["🟡 Shop"]
        S12["S12 · Cử người nhận<br/>tin nhắn chuyển sang"]
        GAP["S18 · Ghi nhật ký lỗi<br/>tuần đầu"]
    end

    START([Pack sẵn sàng<br/>+ Gate 0 đã qua]) --> S13 --> S14 --> S15 --> S16
    S12 --> S16
    S16 --> G1{"Mọi câu 🔁<br/>đều chuyển người?<br/>Không câu nào có giá?"}
    G1 -->|"Không"| FIX["Sửa hướng dẫn tùy chỉnh<br/>rồi chạy lại CẢ 20 câu"]
    FIX --> S16
    G1 -->|"Có"| S17["S17 · Bật thật, hẹp trước:<br/>ngoài giờ → cả ngày có người xem<br/>→ bình thường"]
    S17 --> GAP
    GAP --> OUT(["Danh sách câu AI<br/>không trả lời được<br/>= phạm vi của bước 2 và Path B"])

    style US1 fill:#e7f1ff,stroke:#004085,color:#111
    style SHOP1 fill:#fffbe6,stroke:#856404,color:#111
    style G1 fill:#fff3cd,stroke:#856404,color:#111
    style FIX fill:#f8d7da,stroke:#721c24,color:#111
    style OUT fill:#d4edda,stroke:#155724,color:#111
```

Phase 1's real output is not a running bot — it is **the gap log**. That list is what
decides whether phase 2 is worth doing and what Path B gets built for.

---

## 4. Phase 2 — prices

```mermaid
flowchart TD
    G2{"Gate 2 — S19: đủ cả ba?<br/>• 1 tuần không trả lời sai<br/>• catalog.xlsx ĐẠT<br/>• có người nhận việc<br/>cập nhật giá hằng tuần"}
    G2 -->|"Thiếu bất kỳ điều nào"| STOP(["Ở lại bước 1.<br/>Giá vẫn chuyển nhân viên."])
    G2 -->|"Đủ cả ba"| S20

    subgraph SHOP2["🟡 Shop"]
        S21["S21 · Điền catalog.xlsx<br/>50 mẫu bán chạy là đủ"]
        S22["S22 · Chụp ảnh sản phẩm,<br/>đặt tên theo mã"]
        S20["S20 · Quyết định: có đếm<br/>tồn kho thật không?"]
        S23["S23 · Cho biết link nào<br/>dùng cho cột link"]
    end

    subgraph US2["🔵 Us"]
        S24["S24 · lavabo kb check"]
        S25["S25 · lavabo kb feed<br/>→ meta-catalog-feed.csv"]
        S26["S26 · Sửa hướng dẫn: bỏ<br/>'không báo giá', thêm luật<br/>báo giá + không tự tính"]
        S27["S27 · Chat thử — 25 câu giá"]
    end

    subgraph META2["⚪ Meta"]
        M2["Commerce Manager<br/>nhận feed"]
        M3["Nối danh mục<br/>với trang"]
    end

    S21 --> S24
    S22 --> S24
    S20 --> S21
    S23 --> S25
    S24 -->|"LỖI"| BACK["Gửi lại dòng cần sửa"] --> S21
    S24 -->|"ĐẠT"| S25 --> M2 --> M3 --> S26 --> S27
    S27 --> G3{"25/25, và<br/>KHÔNG con số nào sai?"}
    G3 -->|"Có một con số sai"| RB["Gỡ danh mục khỏi trang,<br/>khôi phục luật bước 1.<br/>Dưới 5 phút."]
    RB --> S24
    G3 -->|"Đúng hết"| LIVE(["S28 · Bước 2 chạy thật"])

    style SHOP2 fill:#fffbe6,stroke:#856404,color:#111
    style US2 fill:#e7f1ff,stroke:#004085,color:#111
    style META2 fill:#f2f2f2,stroke:#666,color:#111
    style G2 fill:#fff3cd,stroke:#856404,color:#111
    style G3 fill:#f8d7da,stroke:#721c24,color:#111
    style RB fill:#f8d7da,stroke:#721c24,color:#111
    style LIVE fill:#d4edda,stroke:#155724,color:#111
    style STOP fill:#eee,stroke:#666,color:#111
```

**S20 changes what the agent is allowed to say.** No real stock count means every row is
`đặt trước` and the agent never says "còn hàng" — which for made-to-order bathroom
furniture is usually the truth, not a compromise.

---

## 5. After go-live — the weekly loop

`kb feed` is not a milestone you pass once. It is this:

```mermaid
flowchart LR
    W1["🟡 L1 · Shop sửa giá,<br/>thêm mẫu mới, xoá KM hết hạn,<br/>cập nhật cap_nhat_ngay"]
    W1 --> W2["🔵 L2 · lavabo kb check"]
    W2 -->|"LỖI"| W2b["Báo lại dòng cần sửa"] --> W1
    W2 -->|"ĐẠT"| W3["🔵 L3 · lavabo kb feed<br/>→ tải lên Commerce Manager"]
    W3 --> W4["🔵 L4 · 8 câu kiểm tra nhanh"]
    W4 --> W5(["Xong — 20 phút"])
    W5 -.->|"tuần sau"| W1

    style W1 fill:#fffbe6,stroke:#856404,color:#111
    style W2 fill:#e7f1ff,stroke:#004085,color:#111
    style W3 fill:#e7f1ff,stroke:#004085,color:#111
    style W4 fill:#e7f1ff,stroke:#004085,color:#111
    style W5 fill:#d4edda,stroke:#155724,color:#111
    style W2b fill:#f8d7da,stroke:#721c24,color:#111
```

A skipped week is not silent: `cap_nhat_ngay` ages, and at 60 days `kb check` starts
rejecting the file — which is the intended behaviour, not a bug. The Page quoting a price
nobody has verified this quarter is the thing that ritual exists to prevent.

---

## 6. Every step, with its owner

| # | Step | Who | Effort | Blocks |
|---|---|---|---|---|
| **S1** | Full-control admin on the Page | 🟡 Shop | 10 min | Everything Facebook |
| **S2** | Answer: does the Page get messages? Is there inbox history? | 🟡 Shop | 1 min | How hard we push on `faq.xlsx` |
| **S3** | Confirm the Page is in a Business Portfolio | 🟡 Shop | 5 min | S4 |
| **S4** | **Start Business Verification** | 🟡 Shop | 30 min + weeks of waiting | Path B; start today regardless |
| **S5** | Disconnect any third-party chatbot | 🟡 Shop | 5 min | S17 — two bots in one inbox |
| **S6** | Screenshot of Hộp thư → Tự động hóa | 🟡 Shop | 2 min | **Gate 0** — Path A vs Path B |
| **S7** | `lavabo kb init` | 🔵 Us | 1 min | S8 |
| **S8** | Send `intake-lavabo.zip` + the docs/14 message | 🔵 Us | 5 min | S9 |
| **S9** | Fill `store` / `policies` / `shipping` / `faq` / `voice` | 🟡 Shop | **half a day** | Step 1 entirely |
| **S10** | `lavabo kb check` | 🔵 Us | 1 min | S14 |
| **S11** | Send back the exact rows to fix | 🔵 Us | 5 min | loops to S9 |
| **S12** | **Name the escalation person** and their hours | 🟡 Shop | 5 min | S16 — handoffs need somewhere to land |
| **S13** | Language = Tiếng Việt, Trả lời tức thì off | 🔵 Us | 10 min | S14 |
| **S14** | Paste business info, knowledge, instructions, handoff topics | 🔵 Us | 45 min | S16 |
| **S15** | Greeting with the AI disclosure + ice breakers | 🔵 Us | 15 min | S16 |
| **S16** | Chat thử — the 20-question script | 🔵 Us | 30 min | **Gate 1** |
| **S17** | Go live, narrowly: after-hours → watched → normal | 🔵 Us | 1 week | S18 |
| **S18** | Keep the gap log for a week | 🟡 Shop + 🔵 Us | 5 min/day | **Gate 2**; scope of phase 2 and Path B |
| **S19** | **Name the weekly price owner** | 🟡 Shop | 5 min | **Gate 2 — no name, no phase 2** |
| **S20** | Decide: is stock genuinely tracked? | 🟡 Shop | 10 min | S21 — what the agent may claim |
| **S21** | Fill `catalog.xlsx` | 🟡 Shop | **1–2 days** | **Phase 2 entirely** |
| **S22** | Photograph products, name by SKU | 🟡 Shop | 1 day | Image answers |
| **S23** | Decide what goes in the `link` column | 🟡 Shop | 10 min | S25 |
| **S24** | `lavabo kb check` on the catalogue | 🔵 Us | 1 min | S25 |
| **S25** | `lavabo kb feed` → CSV → Commerce Manager → link to Page | 🔵 Us | 1 hour | S26 |
| **S26** | Rewrite the price rules (docs/13 §7) | 🔵 Us | 30 min | S27 |
| **S27** | Chat thử — the 25-question price script | 🔵 Us | 45 min | **Gate 3** |
| **S28** | Step 2 live | 🔵 Us | — | The weekly loop |

Shop time totals roughly **2–3 days spread over a fortnight**, almost all of it S9, S21 and
S22. Everything else on their side is minutes.

---

## 7. Where Path B enters

Path B is not a later phase of this flow — it is a different build, and the gap log from
S18 plus the handoffs from S28 are what justify it. It becomes the answer when:

- **Gate 0 fails** — Business AI isn't enabled for this account, so there is no Path A.
- **The gap log fills with arithmetic** — totals, combos, price + ship. Business AI quotes
  rows; it must not compute, and docs/13 §7 forbids it.
- **Orders need capturing** into `data/staging.db` and the monthly workbook.
- **A quote is disputed** and nobody can prove what the Page said. Business AI answers and
  forgets; `agent_turns.retrieved` is the reason Path B exists at all.

[docs/11](11-facebook-reply-agent.md) is the build; [docs/14](14-what-we-need-from-the-customer.md)
§3.4 is what it would then need from the shop.

---

## Related

| Doc | |
|---|---|
| [docs/14-what-we-need-from-the-customer.md](14-what-we-need-from-the-customer.md) | audited status and the request list — S1–S11 in prose |
| [docs/12-business-ai-step-1.md](12-business-ai-step-1.md) | S13–S18 click by click, with the 20-question script |
| [docs/13-business-ai-step-2.md](13-business-ai-step-2.md) | S19–S28, the column spec and the 25-question script |
| [docs/11-facebook-reply-agent.md](11-facebook-reply-agent.md) | §7 — the Path B build |
