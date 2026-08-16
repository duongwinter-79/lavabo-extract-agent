# Output mapping — QUẢN LÝ ĐƠN SENKAHOMES

How each column of the target workbook gets filled from a captured Zalo order note.

Derived from the real file: 4 monthly sheets (`052026`…`082026`), 90 orders, 291 item rows.
`082026` is empty — that is the sheet this pipeline fills.

---

## 1. The layout is one row per line item

Not one row per order. An order occupies a **group of rows**: the first carries all the
order-level fields, and each additional product gets a row where only `Tên sản phẩm` and
`Số lượng` are filled.

```
STT  NGÀY CHỐT   Tên KH    Địa chỉ   Tên sản phẩm              Số lượng  Tổng Tiền   Xe thu hộ  Cọc     Trạng thái
1    06/05/2026  Ry Trương Nhà hàng… Tủ HD-1M gương trùm kín…  1         12,300,000  11,500,000 800,000 Đã giao
                                     Tủ BC02-80 gương led tròn 1
                                     Vòi + xifong              2
2    11/05/2026  Bích Nhạn  …
```

Average 3.23 items per order. **This requires a change to the Excel writer**, which currently
emits one row per record — see §5.

---

## 2. Column-by-column mapping

Source line references are to a captured note:

```
15/8 đơn 1 - Meloxicam                 <- header
1 tủ BC52, gương bo, mặt tinh thể…     <- item lines, quantity first
2 sen cơ như hình
Xóm 3 thôn vạn đồn, xã hồng dũng…      <- address
0367002126                             <- phone
Tổng 29tr                              <- total
Đã cọc 500k                            <- deposit
Note: đã báo làm tủ rồi…               <- note
```

| # | Column | Source | Method | Confidence |
|---|---|---|---|---|
| 1 | `STT` | running counter | **computed** at write time | certain |
| 2 | `NGÀY CHỐT` | header date `15/8` + year | **parsed** from header | certain |
| 3 | `Tên KH` | header customer `Meloxicam` | **parsed** from header | certain |
| 4 | `Địa chỉ` | address line + phone line | LLM | high |
| 5 | `Tên sản phẩm` | item line, quantity stripped | LLM (array) | high |
| 6 | `Số lượng` | leading number of the item line | LLM (array) | high |
| 7 | `Tổng Tiền hóa đơn` | `Tổng 29tr` | LLM verbatim → **parsed** to number | high |
| 8 | `Xe thu hộ` | — | **computed**: `Tổng − Cọc` | verified 89/90 |
| 9 | `Cọc` | `Đã cọc 500k` | LLM verbatim → **parsed** to number | high |
| 10 | `Trạng thái` | not in the note | **default `New`** | needs your confirmation |
| 11 | `Ngày hẹn giao` | not in the note | **leave blank** (0/90 filled in your file) | certain |
| 12 | `Người chốt đơn` | the message sender | **not capturable** — see §4 | blocked |

**Only three columns actually need the model**: address, line items, and the two money
figures. Everything else is parsed, computed, or defaulted — which is deliberate, since a
regex is exactly right where an LLM would only be probably right.

### Xe thu hộ is computed, not extracted

Tested across all 90 orders: `Xe thu hộ = Tổng Tiền hóa đơn − Cọc` holds in **89**. The one
exception has `Xe thu hộ = 0` with a 16,000,000 total and 1,000,000 deposit, which looks like
a row that was never filled in rather than a different rule. Computing it removes a whole
class of arithmetic error.

### Money needs a deterministic parser, not the model

Vietnamese shorthand is compact and easy to misread:

| Written | Means |
|---|---|
| `29tr` | 29,000,000 |
| `2tr5` | 2,500,000 |
| `1tr2` | 1,200,000 |
| `500k` | 500,000 |
| `29tr500` | 29,500,000 |
| **`5.800`** | **5,800,000** — bare numbers on the total line are in millions |
| **`11.500`** | **11,500,000** |
| `6.000.000` | 6,000,000 — full đồng notation, left as written |

A bare number and a full amount look the same (digits and separators), so the two are
told apart by which reading lands in a plausible range for an order: below 100,000 đồng
the number is read as millions, and a millions reading above 500,000,000 is rejected as
absurd. `5.800` becomes 5,800,000; `6.000.000` stays 6,000,000.

The model returns the **verbatim string** it found (`"5.800"`), and a Python function
converts it. A money column is the worst place to accept "probably right".

This split has a practical payoff: when a conversion rule turns out to be wrong, the stored
text is still correct, so only `lavabo load` needs re-running. No re-extraction, no API
calls. Had the model been asked for a number, every such fix would cost a full re-run.

### Địa chỉ absorbs the phone

In your file 73 of 90 addresses already contain the phone number, often as `Sdt:0987485647`
or on a second line. The model extracts address and phone separately, and the writer joins
them the same way, so the column matches your existing convention.

---

## 3. Two shapes of quantity

Item lines put the quantity first: `2 sen cơ như hình` → qty `2`, name `sen cơ như hình`.
Your existing sheet stores them split exactly like that, with no quantity inside the name.
Lines with no leading number default to qty `1`.

---

## 4. The one real gap: Người chốt đơn

This column records **which staff member closed the order** — `Trà My` (82) or `Hường` (5)
in your file. It is not written in the order note; it is *who sent the message*.

**A Zalo Web copy strips sender names**, so it is not in what we capture. And it is not
coming from an API either: the shop has a **Zalo Business** account (an upgraded personal
account), which has no API — see `docs/07`. So this column has to be got right in the app,
permanently. There is no automated source waiting behind it.

What that leaves, best first:

1. **Ask in the app, per capture, defaulting to last time.** The operator picks who closed
   these orders before saving them. One tap for the common case where one person captures
   their own orders — which matches the 82:5 split — and it is stored per order rather
   than per run, so a mixed paste can still be corrected.
2. **Check whether Zalo Desktop's copy includes sender names.** If it does, the connector
   already handles labelled transcripts and (1) becomes a fallback rather than the source.
   One copy answers it: `pbpaste | head -20`.
3. **Set one name for the whole run.** `--closer "Trà My"` today. Fine for a single-person
   session, wrong the moment two people's orders are in the same paste — and wrong here
   means revenue moved between staff.
4. **Leave blank and fill by hand.** 90 orders a month makes this the least attractive.

Today the code does (3). (1) is the one worth building, and it is now the *only* fix —
before, it was a stopgap until an OA arrived.

---

## 5. What needs building

| Piece | Status |
|---|---|
| Capture, split, month filter | done |
| Header parsing → date, order no, customer | done |
| `config/schema.senkahomes.yaml` | done |
| Array-of-objects column type (line items) | done — `type: object_array` |
| VND text → number parser | done — `money.py`, 21 cases |
| Excel writer: one row per line item | done — `lavabo load --layout senkahomes` |
| Write into your existing workbook's monthly sheet vs a new file | both — `load` writes a separate file, `append` inserts into yours after a backup |

### The sheet is a template, not an empty grid

Reading the real file closely changed how export has to work. Sheet `082026` looked
empty but is not:

| What | Where |
|---|---|
| `=G{n}-I{n}` in **Xe thu hộ** | rows 2–67, pre-filled |
| `Tổng doanh thu cửa hàng` = `=sum(G1:G120)` | row 72 |
| `Doanh thu Trà My` = `=SUMIF($L:$L,"Trà My",$G:$G)` | row 73 |
| `Doanh thu Hường` = `=SUMIF($L:$L,"Hường",$G:$G)` | row 74 |
| `Doanh thu chị Hảo` = `=F72-F73-F74` | row 75 |

Three consequences:

1. **Xe thu hộ is the sheet's own formula.** `lavabo append` writes nothing into
   column H where a formula already sits, so the sheet keeps computing it and keeps
   tracking any later edit to Tổng or Cọc.
2. **Orders must start at row 2**, above the summary block. Appending after the last
   used row put them at row 76 — beneath their own totals.
3. **`Người chốt đơn` drives revenue reporting.** Those SUMIFs split the month's takings
   by name. A wrong value there does not merely look wrong; it moves money between
   people's totals — so it has to be got right in the app, since no Zalo route will
   supply it. See §4.

### Producing the workbook

```bash
cp config/schema.senkahomes.yaml config/schema.yaml   # once
lavabo extract                                        # re-extract under these columns
lavabo load --layout senkahomes --out data/out/senka.xlsx \
            --year 2026 --closer "Trà My"
```

The sheet is named after the month it contains (`082026`), matching your workbook, and the
headers are byte-identical to it. Paste the rows into your real file, or keep the generated
one.

`--status` sets Trạng thái (default `New`) and `--closer` fills Người chốt đơn, which the
notes do not record.

---

## 6. Open questions

1. **Người chốt đơn** — does Zalo Desktop's copy include sender names? (§4)
2. **Trạng thái** — is `New` the right default for a freshly captured order?
3. **STT** — a running 1..N per sheet, or the `đơn N` from the header? They differ: your
   sheet counts 1,2,3… per month, while headers show `đơn 4` and `đơn 1` both on 15/8, so the
   header number looks like a per-day counter. I have assumed a running counter.
4. **Output target** — append into sheet `082026` of your existing workbook, or write a
   separate file you paste from? Appending is doable but edits a file you rely on; I would
   default to writing a separate workbook with the identical layout unless you say otherwise.
5. **Ngày hẹn giao** — never filled in any of the 90 orders. Confirm it stays blank.
