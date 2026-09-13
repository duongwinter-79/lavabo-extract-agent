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

SHEETS: tuple[SheetSpec, ...] = (CATALOG, SHIPPING, FAQ, SYNONYMS, PROMOTIONS, IMAGES)

# The text files. Templates, not schemas -- nothing validates prose.
DOCS: dict[str, str] = {
    "store.md": """# Thông tin cửa hàng

- Tên shop:
- Địa chỉ 1:
- Địa chỉ 2:
- Giờ mở cửa:
- Hotline:
- Zalo:
- Khu vực giao hàng:
- Website (nếu có):
""",
    "policies.md": """# Chính sách

## Đặt cọc
Mức cọc:
Hình thức thanh toán:

## Bảo hành
Thời gian:
Bảo hành những gì:
KHÔNG bảo hành những gì:
Cách yêu cầu bảo hành:

## Đổi trả
Trong bao lâu:
Điều kiện:
Ai chịu phí ship đổi trả:

## Lắp đặt
Có hỗ trợ lắp không:
Phí:

## Thời gian xử lý đơn
""",
    "voice.md": """# Giọng của shop

- Xưng hô (em/shop/bên mình):
- Gọi khách là (anh chị/bạn):
- Độ dài mỗi câu trả lời:
- Dùng emoji không:
- Giờ nhân viên trả lời tin nhắn:
- Câu chuyển cho nhân viên (viết đúng câu khách sẽ nhìn thấy):

## Ba hội thoại mẫu
Người đang trả lời tin nhắn hằng ngày viết, không phải người viết tài liệu này.

### 1. Khách hỏi giá

### 2. Khách hỏi ship

### 3. Một tình huống khó (khách phàn nàn, mặc cả, hoặc hỏi mẫu không có)
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
