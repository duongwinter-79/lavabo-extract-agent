# Đọc trước — thư mục này là gì

Đây là bộ biểu mẫu để trợ lý tự động trả lời tin nhắn Facebook của shop
trả lời đúng. AI chỉ biết những gì trong thư mục này, không biết gì thêm.

## Điền theo thứ tự này

**Làm trước (chưa có thì chưa chạy được gì):**
1. `store.md` — thông tin cửa hàng. 10 phút.
2. `policies.md` — cọc, bảo hành, đổi trả, lắp đặt.
3. `shipping.xlsx` — phí ship theo tỉnh.
4. `faq.xlsx` — 30-50 câu khách hay hỏi nhất, kèm câu trả lời nhân viên
   VẪN ĐANG dùng. **Đây là file quan trọng nhất.**
5. `voice.md` — cách shop xưng hô, và 3 hội thoại mẫu. Nhờ đúng người
   đang trả lời tin nhắn hằng ngày viết, đừng nhờ người khác viết hộ.
6. `handoff.xlsx` — những gì AI KHÔNG được tự trả lời. File này bên em
   đã điền sẵn phần quan trọng, anh/chị chỉ cần đọc lại và thêm tình
   huống riêng của shop.

**Làm sau (nhưng chưa có thì AI không được phép báo giá):**
7. `catalog.xlsx` — bảng giá. Bắt đầu bằng 50 mẫu bán chạy nhất là đủ.
8. Ảnh sản phẩm — bỏ vào thư mục `images/`.

**Có thì tốt, không có cũng được:**
`synonyms.xlsx`, `promotions.xlsx`, `images.xlsx`, `dont_say.md`

## Bốn quy tắc, quan trọng hơn mọi thứ khác

1. **Một thông tin chỉ ghi ở MỘT chỗ.** Giá chỉ nằm trong `catalog.xlsx`.
   Đừng ghi lại giá trong `faq.xlsx` — để hai nơi thì một nơi sẽ cũ, và
   AI đọc trúng cái cũ.
2. **Giá chỉ ghi chữ số:** `2850000`. Không ghi `2tr850`, không ghi
   `2.850.000đ`, không ghi `2-3tr`. Ô giá sẽ báo lỗi ngay khi gõ sai.
3. **"Hết hàng" phải là thật.** Nếu shop không đếm tồn kho, để tất cả là
   `đặt trước`. Nói "còn hàng" về thứ phải đặt mới có là cách nhanh nhất
   để mất khách.
4. **Để trống còn hơn ghi sai.** Ô trống thì AI nói "để em kiểm tra lại"
   rồi chuyển cho nhân viên. Ô ghi sai thì AI nói sai với khách.

## Cách điền file .md (store, policies, voice)

Mở bằng Notepad, TextEdit hoặc Word đều được. Trong file có nhiều chỗ ghi
`[chưa điền]` — thay chỗ đó bằng câu trả lời của shop. Ví dụ:

    - **Hotline:** [chưa điền]   ← ví dụ: 0912 345 678

điền xong thành:

    - **Hotline:** 0912 345 678

Mục nào chưa biết thì cứ để nguyên `[chưa điền]`, bên em sẽ hỏi lại. Mục ghi
*(không bắt buộc)* thì bỏ trống cũng được.

## Cách điền file Excel

- Mỗi file có sheet **Hướng dẫn** giải thích từng cột.
- Di chuột vào tên cột để xem ghi chú.
- Cột tiêu đề **màu đỏ** là bắt buộc, **màu xanh** là không bắt buộc.
- **Dòng chữ xám là ví dụ mẫu — xoá đi trước khi gửi lại.**
- Một số cột có danh sách chọn sẵn, bấm mũi tên để chọn.

## Gửi lại

Điền được đến đâu gửi đến đó, không cần đợi xong hết. Bên em có công cụ
kiểm tra file và sẽ báo lại chính xác dòng nào cần sửa.
