"""What the shop hands over, described once.

The same failure the column schema in `config.py` was built to prevent applies here: if
the blank template and the validator each carry their own idea of what `catalog.xlsx`
looks like, they drift, and the shop finds out by having a file rejected for a column the
template never gave them. So both are generated from the specs below -- the dropdown in
the workbook and the rule that rejects a bad cell are the same `Field`.

Column names are ASCII snake_case on purpose. They survive being opened in Excel, saved
as CSV, mapped to Meta's product feed and typed into a script, none of which is reliably
true of "Giá niêm yết".

See docs/13-business-ai-step-2.md §3 for what each column is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    label: str                       # Vietnamese, for the instructions sheet
    kind: str = "text"               # text | integer | date | enum
    required: bool = False
    enum: tuple[str, ...] = ()
    strict_enum: bool = True         # False: the dropdown suggests, it does not constrain
    example: str = ""
    note: str = ""
    # This field becomes required once the named one is filled -- km_den_ngay is
    # meaningless alone and dangerous when missing.
    required_with: str | None = None


@dataclass(frozen=True, slots=True)
class SheetSpec:
    filename: str
    title: str
    intro: str
    fields: tuple[Field, ...]
    examples: tuple[tuple, ...] = ()
    unique: str | None = None        # column that must not repeat
    required_file: bool = True
    # Rows shipped already filled in, because they are the answer rather than an example
    # of one. The shop adds to them; removing one is a decision, not a tidy-up.
    seed_rows: tuple[tuple, ...] = ()
    locked: tuple[str, ...] = ()     # seeded values that must still be present

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    def get(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


LOAI = ("tủ lavabo", "gương", "sen tắm", "chậu rửa", "phụ kiện")
TINH_TRANG = ("còn hàng", "hết hàng", "đặt trước")
DON_VI = ("cái", "bộ", "m")
GIA_GOM = ("chưa gồm ship", "đã gồm ship", "chưa gồm ship và lắp đặt", "đã gồm tất cả")

# Rows older than this have not been looked at in a business quarter's worth of price
# movement; quoting them publicly is the failure docs/13 §8 exists to prevent.
STALE_FAIL_DAYS = 60
STALE_WARN_DAYS = 30

# Left in the template on purpose, so `kb check` can notice they were never replaced.
EXAMPLE_MARKER = "VI-DU"


CATALOG = SheetSpec(
    filename="catalog.xlsx",
    title="Danh mục sản phẩm",
    intro=(
        "Mỗi dòng là MỘT thứ khách mua được ở MỘT mức giá. "
        "Cùng một mẫu mà 3 kích thước 3 giá thì ghi 3 dòng. "
        "Cùng một giá mà 3 màu thì ghi 1 dòng, cột mau ghi 'trắng; xám; vân gỗ'."
    ),
    unique="ma_sp",
    fields=(
        Field("ma_sp", "Mã sản phẩm", required=True, example=f"{EXAMPLE_MARKER}-BC52-80-TRANG",
              note="Không dấu, không khoảng trắng. Đặt rồi thì KHÔNG BAO GIỜ đổi, "
                   "và không dùng lại mã cũ cho sản phẩm khác."),
        Field("ten_sp", "Tên sản phẩm", required=True, example="Tủ lavabo BC52 80cm",
              note="Tên khách gọi, không phải tên nội bộ."),
        Field("loai", "Loại", kind="enum", enum=LOAI, required=True, example="tủ lavabo"),
        Field("kich_thuoc", "Kích thước", example="80cm"),
        Field("chat_lieu", "Chất liệu", example="nhựa PVC cao cấp"),
        Field("mau", "Màu", example="trắng; xám",
              note="Nhiều màu cùng giá thì ngăn cách bằng dấu chấm phẩy."),
        Field("don_vi", "Đơn vị", kind="enum", enum=DON_VI, required=True, example="bộ",
              note="AI sẽ đọc kèm mỗi lần báo giá: '2.850.000đ/bộ'."),
        Field("gia_niem_yet", "Giá niêm yết", kind="integer", required=True, example="2850000",
              note="CHỈ CHỮ SỐ. Không viết 2tr850, không viết 2.850.000đ, không viết '2-3tr'. "
                   "Một dòng một giá — nếu giá thay đổi theo đơn thì đừng đưa vào file này."),
        Field("gia_km", "Giá khuyến mãi", kind="integer", example="2550000",
              note="Để trống nếu không có. Phải nhỏ hơn giá niêm yết."),
        Field("km_den_ngay", "Khuyến mãi đến ngày", kind="date", required_with="gia_km",
              example="",
              note="BẮT BUỘC nếu có giá khuyến mãi. Không có ngày kết thúc thì AI sẽ "
                   "báo giá khuyến mãi đó mãi mãi."),
        Field("tinh_trang", "Tình trạng", kind="enum", enum=TINH_TRANG, required=True,
              example="đặt trước",
              note="Nếu shop không đếm tồn kho thật thì để TẤT CẢ là 'đặt trước'. "
                   "Nói 'còn hàng' về thứ phải đặt mới có là cách nhanh nhất để mất khách."),
        Field("thoi_gian_giao", "Thời gian giao", example="3-5 ngày",
              note="Ghi khoảng, không bao giờ ghi một ngày cụ thể."),
        Field("bao_hanh", "Bảo hành", example="12 tháng"),
        Field("gia_gom", "Giá đã gồm gì", kind="enum", enum=GIA_GOM, strict_enum=False,
              required=True, example="chưa gồm ship",
              note="AI phải nói câu này mỗi lần báo giá."),
        Field("anh", "Ảnh", example=f"{EXAMPLE_MARKER}-BC52-80-TRANG__front.jpg",
              note="Tên file ảnh, nhiều ảnh ngăn cách bằng dấu chấm phẩy."),
        Field("ghi_chu_tu_van", "Ghi chú tư vấn",
              example="Khách hay hỏi có kèm chậu không — có kèm chậu và vòi.",
              note="Câu nhân viên vẫn nói về sản phẩm này. Cột bị xem nhẹ nhất "
                   "nhưng là thứ làm AI nói giống người bán hàng chứ không giống tờ rơi."),
        Field("cap_nhat_ngay", "Ngày cập nhật", kind="date", required=True,
              note="Ngày có NGƯỜI kiểm tra lại dòng này, không phải ngày lưu file."),
    ),
)

SHIPPING = SheetSpec(
    filename="shipping.xlsx",
    title="Phí vận chuyển",
    intro="Mỗi tỉnh/thành một dòng. Đây là bảng AI đọc khi khách hỏi 'ship về ... bao nhiêu'.",
    unique="tinh",
    fields=(
        Field("tinh", "Tỉnh/Thành", required=True, example=f"{EXAMPLE_MARKER} Thái Bình"),
        Field("phi_ship", "Phí ship", kind="integer", required=True, example="150000",
              note="CHỈ CHỮ SỐ. 0 nếu miễn phí."),
        Field("thoi_gian", "Thời gian giao", required=True, example="2-3 ngày"),
        Field("mien_phi_tu", "Miễn phí từ", kind="integer", example="5000000",
              note="Đơn từ mức này trở lên thì miễn ship. Để trống nếu không áp dụng."),
        Field("ghi_chu", "Ghi chú", example="Hàng cồng kềnh tính thêm theo báo giá nhà xe"),
    ),
)

FAQ = SheetSpec(
    filename="faq.xlsx",
    title="Câu hỏi thường gặp",
    intro=(
        "Câu hỏi THẬT của khách và câu trả lời nhân viên VẪN ĐANG dùng. "
        "Tuyệt đối không ghi con số giá ở đây — giá chỉ nằm trong catalog.xlsx."
    ),
    fields=(
        Field("cau_hoi", "Câu hỏi", required=True,
              example=f"({EXAMPLE_MARKER}) Tủ có kèm chậu và vòi không ạ?"),
        Field("tra_loi", "Trả lời", required=True,
              example="Dạ tủ đã kèm chậu và vòi ạ, mình chỉ cần lắp thêm ống thoát thôi ạ."),
        Field("chu_de", "Chủ đề", example="sản phẩm",
              note="sản phẩm / vận chuyển / bảo hành / thanh toán / lắp đặt"),
    ),
)

SYNONYMS = SheetSpec(
    filename="synonyms.xlsx",
    title="Từ khách hay dùng",
    intro=(
        "Khách gõ một đằng, danh mục ghi một nẻo. Thiếu bảng này thì AI trả lời "
        "'bên em không có mẫu đó' về đúng thứ đang bày trong cửa hàng."
    ),
    required_file=False,
    fields=(
        Field("khach_goi", "Khách gọi là", required=True, example=f"({EXAMPLE_MARKER}) sen cây"),
        Field("trong_danh_muc", "Trong danh mục là", required=True, example="sen tắm đứng"),
    ),
)

PROMOTIONS = SheetSpec(
    filename="promotions.xlsx",
    title="Khuyến mãi",
    intro="Khuyến mãi không có ngày kết thúc là khuyến mãi được báo mãi mãi.",
    required_file=False,
    fields=(
        Field("ten_km", "Tên khuyến mãi", required=True, example=f"({EXAMPLE_MARKER}) Giảm 10% tủ 80"),
        Field("ap_dung_cho", "Áp dụng cho", required=True, example="tủ lavabo",
              note="Mã sản phẩm hoặc tên loại."),
        Field("noi_dung", "Nội dung", required=True, example="Giảm 10%, tối đa 300.000đ"),
        Field("tu_ngay", "Từ ngày", kind="date", required=True),
        Field("den_ngay", "Đến ngày", kind="date", required=True),
    ),
)

IMAGES = SheetSpec(
    filename="images.xlsx",
    title="Ảnh sản phẩm",
    intro=(
        "Chỉ cần file này nếu ảnh KHÔNG được đặt tên theo mã sản phẩm. "
        "Ảnh không có trong bảng này thì AI không nhìn thấy."
    ),
    required_file=False,
    fields=(
        Field("ten_file", "Tên file", required=True, example="IMG_4821.jpg"),
        Field("ma_sp", "Mã sản phẩm", required=True, example=f"{EXAMPLE_MARKER}-BC52-80-TRANG"),
        Field("vai_tro", "Vai trò", kind="enum",
              enum=("front", "angle", "detail", "lapdat", "size"), example="front"),
        Field("chu_thich", "Chú thích", example="Tủ 80 màu trắng, mặt đá"),
    ),
)

AI_LAM_GI = ("chuyển ngay cho nhân viên", "trả lời chung rồi chuyển", "không trả lời")

# The topics that protect the shop. Shipped filled in and checked for, because the cost of
# a bot improvising on a complaint or a discount is borne by the shop, not by us.
LOCKED_TOPICS = (
    "Hỏi giá mẫu không có trong bảng giá",
    "Mặc cả, xin giảm giá, xin giá sỉ",
    "Tính tổng tiền nhiều món, giá combo",
    "Đơn đã đặt: kiểm tra, sửa, hủy, giục giao",
    "Khiếu nại, hàng lỗi, hàng vỡ",
    "Yêu cầu bảo hành cụ thể",
    "Khách gửi ảnh hàng bị lỗi",
    "Mua sỉ, làm đại lý",
    "Xuất hóa đơn VAT, hợp đồng, công nợ",
    "Khách xin số tài khoản để chuyển tiền",
    "Khách nói muốn gặp người thật",
    "Khách tỏ ra khó chịu, bực bội",
)

HANDOFF = SheetSpec(
    filename="handoff.xlsx",
    title="AI KHÔNG được trả lời — chuyển cho người thật",
    intro=(
        "Những tình huống AI phải chuyển cho nhân viên thay vì tự trả lời. "
        "Các dòng có sẵn là bắt buộc — shop đọc lại và thêm tình huống của mình "
        "ở phía dưới. Muốn bỏ một dòng có sẵn thì báo bên em, đừng xoá thẳng."
    ),
    unique="tinh_huong",
    fields=(
        Field("tinh_huong", "Tình huống", required=True,
              example="Khách hỏi mẫu shop không bán"),
        Field("vi_du_cau_hoi", "Ví dụ câu khách hỏi",
              example="Bên mình có bồn tắm massage không?"),
        Field("ai_lam_gi", "AI làm gì", kind="enum", enum=AI_LAM_GI, required=True,
              example="chuyển ngay cho nhân viên"),
        Field("chuyen_cho_ai", "Chuyển cho ai", example="chị Hương",
              note="Để trống thì dùng người mặc định trong voice.md."),
        Field("bat_buoc", "Bắt buộc", kind="enum", enum=("có", "không"),
              note="'có' là dòng bên em khuyến nghị không nên bỏ."),
        Field("ghi_chu", "Ghi chú"),
    ),
    seed_rows=tuple(
        (topic, "", "chuyển ngay cho nhân viên", "", "có", "")
        for topic in LOCKED_TOPICS
    ),
    locked=LOCKED_TOPICS,
)

SHEETS: tuple[SheetSpec, ...] = (CATALOG, SHIPPING, FAQ, HANDOFF, SYNONYMS, PROMOTIONS,
                                 IMAGES)

# ---------------------------------------------------------------- the text files

# The markdown files were skeletons of bare headings, which is a blank page with extra
# steps: nothing said what a good answer looked like, and a file returned untouched was
# indistinguishable from one deliberately left empty. So every blank is now a marker the
# shop overwrites, and `kb check` can tell the difference.
PLACEHOLDER = "[chưa điền]"


@dataclass(frozen=True, slots=True)
class DocField:
    label: str
    required: bool = True
    example: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class DocSection:
    heading: str
    intro: str = ""
    fields: tuple[DocField, ...] = ()
    # A section the shop writes prose into rather than filling fields.
    prose: bool = False
    prose_required: bool = True


@dataclass(frozen=True, slots=True)
class DocSpec:
    filename: str
    title: str
    why: str                       # what the agent does with this file
    sections: tuple[DocSection, ...]

    def fields(self) -> list[DocField]:
        return [f for s in self.sections for f in s.fields]


STORE = DocSpec(
    filename="store.md",
    title="Thông tin cửa hàng",
    why='AI đọc file này để trả lời "shop ở đâu", "mấy giờ đóng cửa", "có ship tỉnh không".',
    sections=(
        DocSection("Cửa hàng", fields=(
            DocField("Tên shop", example="Senka Homes"),
            DocField("Shop bán gì", example="thiết bị vệ sinh: tủ lavabo, gương, sen tắm, chậu rửa"),
        )),
        DocSection("Địa chỉ và giờ mở cửa", fields=(
            DocField("Địa chỉ 1", example="Số 12, đường ABC, phường X, TP Thái Bình"),
            DocField("Địa chỉ 2", required=False, note="Bỏ qua nếu chỉ có một cơ sở."),
            DocField("Giờ mở cửa", example="8h00 - 20h00, tất cả các ngày"),
            DocField("Ngày nghỉ", required=False, example="nghỉ mùng 1-5 Tết"),
        )),
        DocSection("Liên hệ", fields=(
            DocField("Hotline", example="0912 345 678"),
            DocField("Zalo", required=False),
            DocField("Website", required=False,
                     note="Để trống nếu chưa có. Không bắt buộc."),
        )),
        DocSection("Giao hàng", fields=(
            DocField("Khu vực giao hàng", example="toàn quốc"),
            DocField("Có hỗ trợ lắp đặt không", example="có, tại Thái Bình và các tỉnh lân cận"),
        )),
    ),
)

POLICIES = DocSpec(
    filename="policies.md",
    title="Chính sách",
    why=("Đây là những câu AI được phép trả lời chắc chắn. Mục nào để trống thì AI sẽ "
         "chuyển cho nhân viên thay vì đoán — để trống vẫn an toàn, ghi sai thì không."),
    sections=(
        DocSection("Đặt cọc và thanh toán", fields=(
            DocField("Mức cọc", example="30% giá trị đơn"),
            DocField("Hình thức thanh toán", example="chuyển khoản hoặc tiền mặt khi nhận hàng"),
            DocField("Thanh toán nốt khi nào", example="khi nhận hàng"),
        )),
        DocSection("Bảo hành", fields=(
            DocField("Thời gian bảo hành", example="12 tháng"),
            DocField("Bảo hành những gì", example="lỗi kỹ thuật của nhà sản xuất, bản lề, ray trượt"),
            DocField("KHÔNG bảo hành những gì", example="vỡ do va đập, ngấm nước do lắp sai",
                     note="Quan trọng ngang phần được bảo hành. Thiếu mục này AI dễ hứa quá tay."),
            DocField("Cách yêu cầu bảo hành", example="gọi hotline, gửi ảnh qua Zalo"),
        )),
        DocSection("Đổi trả", fields=(
            DocField("Đổi trả trong bao lâu", example="7 ngày kể từ khi nhận hàng"),
            DocField("Điều kiện đổi trả", example="còn nguyên hộp, chưa lắp đặt"),
            DocField("Ai chịu phí ship đổi trả", example="shop chịu nếu lỗi từ shop"),
        )),
        DocSection("Lắp đặt và thời gian xử lý", fields=(
            DocField("Phí lắp đặt", example="miễn phí trong nội thành"),
            DocField("Sau khi chốt đơn bao lâu thì giao", example="3-5 ngày",
                     note="Ghi khoảng, đừng ghi một ngày cụ thể."),
        )),
    ),
)

VOICE = DocSpec(
    filename="voice.md",
    title="Giọng của shop",
    why=("Quyết định AI nghe như người của shop hay như máy. Phần hội thoại mẫu ở cuối "
         "có giá trị hơn tất cả phần còn lại cộng lại — nhờ ĐÚNG người đang trả lời tin "
         "nhắn hằng ngày viết, đừng nhờ người khác viết hộ."),
    sections=(
        DocSection("Xưng hô", fields=(
            DocField("Shop tự xưng là", example="em"),
            DocField("Gọi khách là", example="anh/chị"),
        )),
        DocSection("Cách trả lời", fields=(
            DocField("Độ dài mỗi câu trả lời", example="2-3 câu, ngắn gọn"),
            DocField("Có dùng emoji không", example="có, tối đa 1 cái"),
            DocField("Câu kết thúc thường dùng", required=False,
                     example="Anh/chị cần em tư vấn thêm gì không ạ?"),
        )),
        DocSection("Nhân viên và giờ làm việc", fields=(
            DocField("Giờ nhân viên trả lời tin nhắn", example="8h00 - 20h00"),
            DocField("Ai nhận tin nhắn AI chuyển sang", example="chị Hương",
                     note="Phải là một người cụ thể. Không có tên ở đây thì tin nhắn "
                          "chuyển đi sẽ rơi vào khoảng không."),
            DocField("Câu AI nói khi chuyển cho nhân viên",
                     example="Dạ em nhờ bạn phụ trách trả lời giúp mình ngay ạ."),
            DocField("Câu AI nói ngoài giờ làm việc",
                     example="Dạ ngoài giờ làm việc, bạn phụ trách sẽ liên hệ lại đầu giờ sáng ạ."),
        )),
        DocSection(
            "Hội thoại mẫu 1 — khách hỏi giá",
            intro="Chép lại một đoạn chat thật gần đây, cả câu khách hỏi và câu shop trả lời.",
            prose=True),
        DocSection(
            "Hội thoại mẫu 2 — khách hỏi ship hoặc bảo hành",
            prose=True),
        DocSection(
            "Hội thoại mẫu 3 — một tình huống khó",
            intro="Khách mặc cả, khách phàn nàn, hoặc khách hỏi mẫu shop không có.",
            prose=True),
    ),
)

DOC_SPECS: tuple[DocSpec, ...] = (STORE, POLICIES, VOICE)

# Files with nothing to fill in: a covering letter, and a list the shop only adds to.
STATIC_DOCS: dict[str, str] = {
    "00-DOC-TRUOC.md": """# Đọc trước — thư mục này là gì

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
""",
    "00-GUI-ANH-TU-DIEN-THOAI.md": """# Gửi ảnh và video sản phẩm từ điện thoại

Ảnh sản phẩm của shop đang nằm trong điện thoại. Không cần đổi tên từng file —
trên điện thoại việc đó gần như không làm được. Chỉ cần **xếp vào thư mục theo
mã sản phẩm**, bên em lo phần còn lại.

## Cách làm (khoảng 15 phút cho 20 sản phẩm)

1. Mở app **Google Drive** trên điện thoại, tạo một thư mục tên `ANH-SAN-PHAM`.
2. Trong đó, tạo mỗi sản phẩm một thư mục, **đặt tên đúng bằng mã sản phẩm**
   trong `catalog.xlsx`:

       ANH-SAN-PHAM/
         BC52-80-TRANG/
         GUONG-BO-60/
         SEN-CAY-INOX/

3. Mở thư mục của sản phẩm nào thì bấm **+ → Tải lên → Ảnh và video**, chọn ảnh
   của đúng sản phẩm đó. Tên file để nguyên (`IMG_4821.jpg` cũng được).
4. Chia sẻ thư mục `ANH-SAN-PHAM` cho bên em.

Chưa có mã sản phẩm thì đặt tên thư mục bằng tên sản phẩm cũng được, miễn là
gọi giống nhau giữa các file.

## Mỗi sản phẩm nên có

- **Ít nhất 1 ảnh** chụp thẳng, đủ sáng.
- Mẫu bán chạy: thêm ảnh chụp nghiêng, ảnh cận chi tiết, và **ảnh đã lắp trong
  phòng tắm thật** — ảnh lắp thật là ảnh khách hỏi nhiều nhất.
- Có bản vẽ kích thước thì chụp luôn.

Nếu đặt tên file có chữ `nghieng`, `chitiet`, `lapdat`, `kichthuoc` thì bên em
tự nhận ra loại ảnh. Không đặt cũng không sao.

## Video demo thì sao?

**Cứ gửi.** Bỏ video vào đúng thư mục sản phẩm như ảnh. Bên em sẽ lấy những
khung hình rõ nhất trong video làm ảnh sản phẩm — với nhiều mẫu, video lắp đặt
là thứ duy nhất chụp được sản phẩm trong phòng tắm thật.

## Dán ảnh thẳng vào file Excel được không?

Được, nhưng **thư mục vẫn tốt hơn**: ảnh dán vào Excel làm file nặng, và
khi mở bằng Google Sheets thì ảnh hay bị rơi mất.

Nếu anh/chị vẫn muốn dán: dán ảnh vào **đúng dòng của sản phẩm đó** trong
tab "Danh mục sản phẩm" — bên em lấy ảnh ra theo dòng, nên dán lệch dòng
là gán nhầm sản phẩm. Một dòng dán nhiều ảnh cũng được.

Lưu ý: ảnh chèn bằng chức năng "chèn ảnh vào ô" của Google Sheets hoặc
bằng công thức =IMAGE(...) thì bên em KHÔNG lấy ra được. Dán ảnh bình
thường, hoặc dùng thư mục.

## Hai lỗi hay gặp

**Ảnh iPhone định dạng HEIC** bên em không đọc được. Sửa một lần cho tất cả:
*Cài đặt → Camera → Định dạng → chọn "Tương thích nhất"*. Ảnh đã chụp rồi thì
gửi qua Zalo hoặc Messenger, hai app này tự đổi sang JPG.

**Đừng nén ảnh.** Khi chia sẻ, chọn gửi ảnh gốc / chất lượng cao. Ảnh bị nén
nhỏ quá thì khách phóng to ra là vỡ.

## Không gửi

- Ảnh chụp màn hình có tên hoặc số điện thoại của khách khác.
- Ảnh có logo chìm của shop khác.
""",
    "dont_say.md": """# Những câu AI không được nói

- Không nói con số giá nào ngoài giá trong catalog.xlsx
- Không tự cộng trừ, không tính tổng nhiều món, không tự giảm giá
- Không nói "chắc chắn giao trong X ngày"
- Không so sánh với shop khác
- Không cam kết "dùng vĩnh viễn", "bảo hành trọn đời"
- Không hướng dẫn đấu điện, đấu nước
- (thêm của shop:)
""",
}
