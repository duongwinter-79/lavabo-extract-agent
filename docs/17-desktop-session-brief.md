# Briefs for the Claude session on our own machine

The session that can configure Meta Business Suite is the one running **on our own
machine** — the one with Chrome logged into the shop's Business account and a local clone
of this repo. A cloud session cannot: it has no browser profile, no cookies, and
`business.facebook.com` is blocked by its egress proxy (verified — `curl` returns 403 at
the proxy).

To be clear about whose machine this is: **ours, not the shop's.** We hold delegated access
to SENKA HOME's Page; the shop is on the other end of a phone.

So this file is the handover. Paste §2 into that session. §1 is for the human sitting
next to it.

---

## 1. Before pasting — the human's part

1. **Confirm the access landed.** Open
   <https://business.facebook.com/settings/pages> → the Page → your name should appear
   with **Toàn quyền kiểm soát**. Appearing under
   [/settings/people](https://business.facebook.com/settings/people) alone is not enough
   and is the usual reason settings are unreachable.
2. **Be at the keyboard.** This is someone else's live business account, and real
   customers are messaging it.
   The brief tells the session to stop and ask before anything becomes customer-facing;
   that safeguard only works if somebody is there to answer.
3. **Have the shop reachable** (phone or Zalo) for the handful of facts that cannot be
   read off the Page.

---

## 2. The prompt

Paste everything between the lines.

---

You are working on the shop's own computer, in a Chrome that is already logged into Meta
Business Suite. Goal: get the Facebook Page answering basic customer questions
automatically, today, using only the built-in automations.

**Scope — do not go outside it.** You may read Page information and create or edit
**Inbox → Automations** (greeting, instant reply, FAQs, ice breakers, away message) and,
if it exists, Business AI. You must **not** touch ads, billing, people and permissions,
Page roles, published posts, or anything under Commerce Manager. Never delete anything.

**Before any change that customers will see, stop and ask the human to confirm.** Saving
an automation makes it live immediately — there is no separate publish step.

If you can drive the browser, do it. If you cannot, give the human one instruction at a
time and ask them to confirm or screenshot each screen before you continue.

### Step 1 — report what is actually there

Open <https://business.facebook.com/latest/inbox/automations> and tell me, before
changing anything:

- Which automations exist, with their exact Vietnamese labels.
- Whether **Business AI / Trợ lý AI** appears at all. This decides everything downstream,
  so quote the label you see rather than paraphrasing.
- Whether any automation is already switched on, and what its current text says. Do not
  overwrite existing text without showing me it first — the shop may have written it.

Also check <https://business.facebook.com/settings/business_apps> (or Cài đặt → Ứng dụng)
for a connected third-party chatbot — Abit, vPage/Nhanh, Botcake, AhaChat, Manychat. If
one is connected, stop and tell me: two automations in one inbox reply over each other,
and that has to be resolved before anything else.

### Step 2 — collect the facts

Read from <https://business.facebook.com/settings/info> and the Page itself: shop name,
address, opening hours, phone, and anything about delivery already written there.

Then ask the human for whatever is still missing:

1. Phí ship tính thế nào (a general answer is fine — no per-province table needed)
2. Bảo hành bao lâu, bảo hành những gì
3. Đặt cọc bao nhiêu phần trăm
4. Giờ nhân viên trả lời tin nhắn
5. Tên người nhận tin nhắn khi cần chuyển cho người thật

Fact 5 is not optional. An automated reply promising a human, with nobody behind it, is
worse than no automation at all.

### Step 3 — write the automations

Vietnamese, replacing every `[...]` with a real value. Show me the filled text before
saving each one.

**Lời chào**
```
Dạ [TÊN SHOP] xin chào anh/chị 👋
Shop chuyên tủ lavabo, gương, sen tắm và thiết bị vệ sinh.
Anh/chị cần hỏi gì cứ nhắn ạ, hoặc bấm vào câu hỏi bên dưới.
```

**Trả lời tức thì** — ON while these basic automations are the only thing running. If
Business AI is switched on later, this must be turned OFF or every customer gets two
replies a second apart.
```
Dạ [TÊN SHOP] đã nhận được tin nhắn của anh/chị ạ 🙏
Anh/chị bấm vào các câu hỏi gợi ý để xem thông tin ngay,
hoặc để lại câu hỏi — bạn phụ trách sẽ trả lời trong [GIỜ LÀM VIỆC] ạ.
```

**Câu hỏi thường gặp** — add as many as the panel allows:

1. *Shop ở đâu? Mấy giờ mở cửa?*
```
Dạ shop ở [ĐỊA CHỈ] ạ.
Mở cửa [GIỜ MỞ CỬA]. Anh/chị qua trực tiếp xem hàng được ạ.
Hotline: [HOTLINE]
```
2. *Có ship về tỉnh không? Phí bao nhiêu?*
```
Dạ shop ship [KHU VỰC GIAO] ạ.
Phí ship tuỳ tỉnh và kích thước hàng, [CÁCH TÍNH PHÍ SHIP].
Anh/chị cho em xin địa chỉ, bạn phụ trách báo phí chính xác ngay ạ.
```
3. *Bảo hành thế nào?*
```
Dạ bảo hành [THỜI GIAN BẢO HÀNH] ạ, áp dụng cho [BẢO HÀNH NHỮNG GÌ].
Cần bảo hành anh/chị gọi [HOTLINE], bên em xử lý ạ.
```
4. *Đặt hàng thế nào? Cọc bao nhiêu?*
```
Dạ anh/chị chọn mẫu rồi nhắn cho em mã hoặc ảnh sản phẩm ạ.
Đặt cọc [MỨC CỌC], phần còn lại thanh toán khi nhận hàng.
Anh/chị để lại số điện thoại, bạn phụ trách gọi xác nhận ngay ạ.
```
5. *Giá bao nhiêu?* — **the answer must not contain a number.**
```
Dạ mỗi mẫu một giá khác nhau ạ. Anh/chị cho em xin ảnh hoặc tên mẫu
đang quan tâm, bạn phụ trách báo giá chính xác trong [GIỜ LÀM VIỆC] ạ.
Hoặc gọi [HOTLINE] để được báo giá ngay.
```
6. *Em muốn gặp nhân viên tư vấn*
```
Dạ em chuyển cho bạn phụ trách ngay ạ.
Trong [GIỜ LÀM VIỆC] bên em trả lời trong ít phút.
Gấp thì anh/chị gọi [HOTLINE] giúp em ạ.
```

**Câu hỏi gợi ý** — exactly four, that is the cap:
```
Shop ở đâu ạ?
Phí ship về tỉnh bao nhiêu?
Bảo hành thế nào?
Em muốn gặp nhân viên tư vấn
```

**Tin nhắn vắng mặt** — set business hours to `[GIỜ LÀM VIỆC]` first:
```
Dạ ngoài giờ làm việc, em đã ghi nhận tin nhắn của anh/chị ạ.
Bạn phụ trách sẽ liên hệ lại ngay đầu giờ [GIỜ LÀM VIỆC].
Gấp thì anh/chị gọi [HOTLINE] giúp em ạ.
```

### Step 4 — Business AI, only if step 1 found it

Ask me first. If we proceed: set the language to **Tiếng Việt**, turn on Lịch sử trò
chuyện and Nội dung trang as knowledge, leave the **product catalogue off**, and paste the
guardrail instructions and handoff topics I will give you. Then turn **Trả lời tức thì
off** and trim the static FAQ, so two layers are not answering the same question
differently.

### Step 5 — test from outside

Have the human message the Page from a personal Facebook account (not an admin of the
Page — an admin sees different behaviour). Check:

1. Opening the chat shows the greeting and four suggested questions.
2. Tapping each suggested question returns the right answer.
3. Typing **"tủ 80 bao nhiêu tiền?"** returns **no number** — this is the one that blocks
   launch if it fails.
4. Typing "em muốn gặp người thật" routes to a human and mentions the hotline.
5. Two messages in a row do not produce duplicate replies.

Report the results as a list. If test 3 produces a price, say so plainly and we turn the
automation off rather than leaving it live.

### Step 6 — hand back

Tell me: what is now switched on, the exact text saved in each, anything you could not do
and why, and where the off switch is
(<https://business.facebook.com/latest/inbox/automations>). Name the person from fact 5 —
they need to know messages will start arriving tagged for them.

---

## 3. What comes back to this repo

Whatever that session reports in step 1 — the real panel labels, and whether Business AI
exists — is the answer to the open question in
[docs/14](14-what-we-need-from-the-customer.md) §3.1. Every label in
[docs/12](12-business-ai-step-1.md) and [docs/16](16-quick-setup.md) is currently marked
**[confirm]** because it came from secondary sources. Paste the report back and the guesses
get replaced with what is actually on screen.

---

## Related

| Doc | |
|---|---|
| [docs/16-quick-setup.md](16-quick-setup.md) | the same procedure, for a human doing it by hand |
| [docs/12-business-ai-step-1.md](12-business-ai-step-1.md) | the Business AI guardrail blocks for step 4 |
| [docs/14-what-we-need-from-the-customer.md](14-what-we-need-from-the-customer.md) | the access §1 assumes |

---

## 4. Second brief — reading the product images

Provenance is settled: **the shop sent these images to customers from its Page inbox**, so
the prices are the shop's own. That does not make them current — a price quoted weeks ago,
or quoted specially to one customer, reads exactly like a list price once it is a number in
a column. Everything this produces is a draft a human confirms.

Runs on **our** machine, where the repo already is. Paste everything between the lines,
fixing the two paths.

---

Máy này là máy của tôi (bên triển khai), đã có sẵn repo lavabo-extract-agent.
Khách hàng là shop SENKA HOME — thiết bị vệ sinh: tủ lavabo, gương, sen tắm,
chậu rửa. Tôi có quyền truy cập Meta Business của shop, nhưng shop không ngồi
đây; muốn hỏi gì về sản phẩm thì phải nhắn cho họ.

NHIỆM VỤ: đọc thông tin sản phẩm từ các ảnh trong
  <ĐƯỜNG DẪN THƯ MỤC ẢNH>
thành một bảng nháp để người kiểm tra lại.

Nguồn ảnh: chính shop đã gửi những ảnh này cho khách qua Messenger của Page,
tôi lấy ra từ Hộp thư. Nên giá trong ảnh là giá shop từng báo — nhưng có thể đã
cũ, hoặc là giá riêng cho một khách. Mọi thứ bạn tạo ra là BẢN NHÁP, không phải
bảng giá.

== BƯỚC 0: chuẩn bị repo (đã có sẵn trên máy) ==

  cd <ĐƯỜNG DẪN REPO lavabo-extract-agent>
  git fetch origin
  git checkout claude/fervent-bell-cpq3ru
  git pull

Nếu chưa có .venv thì chạy scripts\setup.ps1 (Windows) hoặc bash scripts/setup.sh.
Rồi kích hoạt: .venv\Scripts\activate  (hoặc source .venv/bin/activate)

Cần GEMINI_API_KEY trong .env (miễn phí: https://aistudio.google.com/apikey).
Kiểm tra:  lavabo check
Dòng "FAIL messenger" là bình thường, không liên quan việc này.

== BƯỚC 1: NHÌN trước khi chạy gì ==

Mở 3-4 ảnh bất kỳ trong thư mục đó và mô tả cho tôi:
 - Là ảnh sản phẩm có chữ in lên ảnh (kích thước, màu, giá)?
 - Hay là ảnh chụp màn hình đoạn chat giữa shop và khách?
 - Chép lại nguyên văn phần chữ bạn đọc được trên MỘT ảnh.

Trả lời xong hãy dừng lại, đừng chạy tiếp. Câu trả lời này quyết định --mode.

== BƯỚC 2: đọc thử 5 ảnh ==

Ảnh sản phẩm có chữ:
  lavabo kb from-images --from "<ĐƯỜNG DẪN THƯ MỤC ẢNH>" --limit 5 --out draft-5.xlsx

Ảnh chụp màn hình đoạn chat:
  lavabo kb from-images --from "<ĐƯỜNG DẪN THƯ MỤC ẢNH>" --mode chat --limit 5 --out draft-5.xlsx

Mở draft-5.xlsx và báo lại từng dòng đọc được gì. 5 ảnh gần như không tốn tiền,
và cho biết prompt có đọc đúng không trước khi chạy cả trăm ảnh.

== BƯỚC 3: chạy hết, rồi báo cáo ==

Bỏ --limit, --out catalog-draft.xlsx. Báo lại:
 - bao nhiêu ảnh, bao nhiêu đọc được sản phẩm, bao nhiêu đọc được giá
 - riêng --mode chat: bao nhiêu giá là do SHOP báo (cột nguoi_bao_gia)
 - ảnh nào đọc lỗi
 - gửi kèm file catalog-draft.xlsx

== KHÔNG ĐƯỢC LÀM ==

- KHÔNG ghi vào catalog.xlsx. Bản nháp là file riêng, cố ý như vậy.
- KHÔNG tự điền cột ma_sp. Ảnh không chứa mã sản phẩm; model được hỏi sẽ bịa ra.
- KHÔNG tải gì lên Meta, KHÔNG bật Meta Business Agent. Nó đang TẮT và phải giữ
  nguyên cho tới khi có người đối chiếu giá với shop.
- KHÔNG commit hay push gì trong repo. Cần sửa code thì báo tôi.

---

**Step 1 is the one not to skip.** Product graphics and conversation screenshots need
different flags, and conversation screenshots are the better source: the shop's own
sentence beats OCR of a graphic, and `--mode chat` records who said each number.

### What comes back here

The draft, and the counts. Then a human confirms prices against what the shop charges
today, fills `ma_sp`, and only then does anything become `catalog.xlsx` —
[docs/13](13-business-ai-step-2.md) §4.
