"""What reaches the folder Meta reads, and what must not.

Publishing is the moment the shop's working files become sentences the Page can say. The
cases here are the ways a form leaks into published content: an unanswered blank, a grey
example row, an "← ví dụ" hint that is a second phone number, and the two files that are
instructions rather than facts and would have the agent reciting house rules at customers.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openpyxl import Workbook, load_workbook                      # noqa: E402

from lavabo.kb import spec                                        # noqa: E402
from lavabo.kb.check import Problem, check_intake                 # noqa: E402
from lavabo.kb.publish import (KNOWLEDGE_DIR, PHOTO_COLUMN,  # noqa: E402
                               blocking, instruction_gaps, publish)
from lavabo.kb.templates import write_intake                      # noqa: E402

from test_kb_check import GOOD                                    # noqa: E402


class Publishing(unittest.TestCase):
    def setUp(self):
        self.intake = Path(tempfile.mkdtemp()) / "intake"
        self.out = Path(tempfile.mkdtemp()) / "drive"
        write_intake(self.intake)
        self.write_catalog([GOOD])

    def write_catalog(self, rows):
        wb = Workbook()
        ws = wb.active
        ws.title = "Dữ liệu"
        ws.append(spec.CATALOG.names)
        for row in rows:
            ws.append([row.get(c) for c in spec.CATALOG.names])
        wb.save(self.intake / "catalog.xlsx")

    def answer(self, filename, label, value):
        path = self.intake / filename
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"- **{label}:**"):
                line = line.replace(spec.PLACEHOLDER, value)
            lines.append(line)
        path.write_text("\n".join(lines), encoding="utf-8")

    def run_publish(self, **kwargs):
        return publish(self.intake, self.out, today=date(2026, 9, 14), **kwargs)

    def knowledge(self) -> set[str]:
        return {p.name for p in (self.out / KNOWLEDGE_DIR).iterdir()}

    def price_rows(self):
        ws = load_workbook(self.out / KNOWLEDGE_DIR / "05-bang-gia.xlsx")["Dữ liệu"]
        return list(ws.iter_rows(values_only=True))


class WhatGetsWritten(Publishing):
    def test_the_connected_folder_holds_only_customer_facing_files(self):
        self.run_publish()
        self.assertEqual(
            self.knowledge(),
            {"01-thong-tin-cua-hang.md", "02-chinh-sach.md", "03-phi-van-chuyen.xlsx",
             "04-cau-hoi-thuong-gap.xlsx", "05-bang-gia.xlsx"})

    def test_instructions_are_never_published(self):
        """voice.md and dont_say.md are how the agent should behave. An agent that reads
        them as knowledge tells a customer not to compare with other shops."""
        report = self.run_publish()
        for name in ("voice.md", "dont_say.md", "handoff.xlsx", "synonyms.xlsx",
                     "images.xlsx"):
            self.assertFalse(any(name in f for f in self.knowledge()), name)
            self.assertTrue(any(name in line for line in report.excluded), name)

    def test_the_form_folder_and_photos_are_separate_and_unconnected(self):
        self.run_publish()
        self.assertTrue((self.out / "02-ANH-SAN-PHAM").is_dir())
        self.assertTrue((self.out / "bieu-mau").is_dir())

    def test_the_folder_rules_note_stays_out_of_the_connected_folder(self):
        """A note explaining our filing system, indexed as knowledge, becomes the agent
        explaining our filing system to a customer."""
        self.run_publish()
        self.assertTrue((self.out / "00-DOC-TRUOC.txt").exists())
        self.assertNotIn("00-DOC-TRUOC.txt", self.knowledge())

    def test_every_file_carries_an_update_date(self):
        self.run_publish()
        text = (self.out / KNOWLEDGE_DIR / "01-thong-tin-cua-hang.md").read_text("utf-8")
        self.assertIn("Cập nhật ngày: 2026-09-14", text)
        self.assertIn("Cập nhật ngày: 2026-09-14", str(self.price_rows()[0]))


class WhatGetsStripped(Publishing):
    def test_unanswered_blanks_do_not_reach_the_agent(self):
        self.answer("store.md", "Tên shop", "SENKA HOME")
        self.run_publish()
        text = (self.out / KNOWLEDGE_DIR / "01-thong-tin-cua-hang.md").read_text("utf-8")
        self.assertIn("SENKA HOME", text)
        self.assertNotIn(spec.PLACEHOLDER, text)
        self.assertNotIn("Website", text)

    def test_the_example_hint_is_removed_from_an_answered_line(self):
        """"- Hotline: 0912 345 678   ← ví dụ: 0912 345 678" leaves two phone numbers in
        the knowledge, one of them invented by us."""
        self.answer("store.md", "Hotline", "0987 111 222")
        self.run_publish()
        text = (self.out / KNOWLEDGE_DIR / "01-thong-tin-cua-hang.md").read_text("utf-8")
        self.assertIn("0987 111 222", text)
        self.assertNotIn("ví dụ", text)
        self.assertNotIn("0912 345 678", text)

    def test_example_rows_never_reach_the_price_list(self):
        self.write_catalog([GOOD, {**GOOD, "ma_sp": f"{spec.EXAMPLE_MARKER}-X"}])
        report = self.run_publish()
        codes = [r[0] for r in self.price_rows()[2:]]
        self.assertEqual(codes, ["BC52-80-TRANG"])
        # The counter spans every table in the pack, not just this one.
        self.assertGreaterEqual(report.skipped_examples, 1)

    def test_internal_columns_are_not_published(self):
        self.run_publish()
        header = [c for c in self.price_rows()[1] if c]
        self.assertNotIn("anh", header)
        self.assertNotIn("cap_nhat_ngay", header)
        self.assertIn("gia_niem_yet", header)
        self.assertIn("ghi_chu_tu_van", header)


class ThePhotoLink(Publishing):
    """`--image-base` is the only thing that puts a photo address in the knowledge.

    A filename is dropped on purpose -- the agent would state "IMG_4821.jpg" as though it
    meant something. A URL is that datum made actionable, so it appears only when somebody
    asserts the photos are actually reachable.
    """

    def test_no_photo_column_without_a_base(self):
        self.run_publish()
        self.assertNotIn(PHOTO_COLUMN, self.price_rows()[1])

    def test_the_base_joins_onto_the_filename_the_catalogue_carries(self):
        self.run_publish(image_base="https://host/anh")
        header, row = self.price_rows()[1], self.price_rows()[2]
        self.assertEqual(header[-1], PHOTO_COLUMN)
        self.assertEqual(row[-1], f"https://host/anh/{GOOD['anh']}")

    def test_a_trailing_slash_does_not_double_up(self):
        """The shop pastes whatever Drive gave them; both forms have to land the same."""
        self.run_publish(image_base="https://host/anh/")
        self.assertEqual(self.price_rows()[2][-1], f"https://host/anh/{GOOD['anh']}")

    def test_several_photos_on_one_row_each_get_an_address(self):
        self.write_catalog([{**GOOD, "anh": "a.jpg; b.jpg"}])
        self.run_publish(image_base="https://host/anh")
        self.assertEqual(self.price_rows()[2][-1],
                         "https://host/anh/a.jpg; https://host/anh/b.jpg")

    def test_a_row_with_no_photo_gets_an_empty_cell_not_a_broken_link(self):
        """A bare base URL would send the customer to a directory, or to nothing."""
        self.write_catalog([{**GOOD, "anh": None}])
        self.run_publish(image_base="https://host/anh")
        cell = self.price_rows()[2][-1]
        self.assertFalse(cell, cell)          # an empty cell reads back as None

    def test_the_rest_of_the_price_list_is_untouched(self):
        self.run_publish()
        plain = self.price_rows()
        self.run_publish(image_base="https://host/anh")
        linked = self.price_rows()
        self.assertEqual([r[:-1] for r in linked[1:]], [tuple(r) for r in plain[1:]])


class TheProseFormat(Publishing):
    """`.md` is not a format Meta's Drive picker offers, so the prose has to leave as
    something it does: its filter lists Tài liệu, Hình ảnh and Bảng tính."""

    def docx_text(self, name: str) -> str:
        """Every run's text, whitespace collapsed.

        Word splits a line across runs -- bolding the label alone makes two of them -- so
        joining them reintroduces spaces that are not in the rendered document. Collapsing
        is what makes a comparison about content rather than about run boundaries.
        """
        import re
        import zipfile
        xml = zipfile.ZipFile(self.out / KNOWLEDGE_DIR / name).read(
            "word/document.xml").decode("utf-8")
        return re.sub(r"\s+", " ", " ".join(
            re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)))

    def test_markdown_is_still_the_default(self):
        """A folder that is not going to be connected should not need a Word document."""
        self.run_publish()
        self.assertIn("01-thong-tin-cua-hang.md", self.knowledge())
        self.assertNotIn("01-thong-tin-cua-hang.docx", self.knowledge())

    def test_docx_replaces_the_markdown_rather_than_joining_it(self):
        """Both formats in one folder is two files where one is unreadable to the picker,
        and a reader with no way to tell which is current."""
        self.answer("store.md", "Tên shop", "SENKA HOME")
        self.run_publish(prose_format="docx")
        self.assertIn("01-thong-tin-cua-hang.docx", self.knowledge())
        self.assertNotIn("01-thong-tin-cua-hang.md", self.knowledge())

    def test_the_answers_survive_the_trip_into_word(self):
        self.answer("store.md", "Tên shop", "SENKA HOME")
        self.answer("store.md", "Hotline", "0769080568")
        self.run_publish(prose_format="docx")
        text = self.docx_text("01-thong-tin-cua-hang.docx")
        self.assertIn("SENKA HOME", text)
        self.assertIn("0769080568", text)
        self.assertIn("Cập nhật ngày: 2026-09-14", text)

    def test_the_example_hint_is_stripped_in_word_too(self):
        """The filter runs before the format is chosen, so neither output can carry it."""
        self.answer("store.md", "Hotline", "0987 111 222")
        self.run_publish(prose_format="docx")
        text = self.docx_text("01-thong-tin-cua-hang.docx")
        self.assertIn("0987 111 222", text)
        self.assertNotIn("0912 345 678", text)
        self.assertNotIn(spec.PLACEHOLDER, text)

    def test_a_folded_answer_becomes_separate_lines(self):
        """`write_pack` folds a multi-line answer onto one form line with " / ". Splitting
        it back gives the shop's own line breaks -- and "anh/chị" must survive that."""
        self.answer("policies.md", "Thời gian bảo hành", "Tủ 20 năm / Gương 5 năm")
        self.answer("policies.md", "Cách yêu cầu bảo hành", "gọi cho anh/chị phụ trách")
        self.run_publish(prose_format="docx")
        import zipfile
        xml = zipfile.ZipFile(self.out / KNOWLEDGE_DIR / "02-chinh-sach.docx").read(
            "word/document.xml").decode("utf-8")
        self.assertIn("ListBullet2", xml)                      # the second line indented
        self.assertIn("anh/chị phụ trách", self.docx_text("02-chinh-sach.docx"))

    def test_both_formats_carry_the_same_words(self):
        self.answer("store.md", "Tên shop", "SENKA HOME")
        self.run_publish()
        markdown = (self.out / KNOWLEDGE_DIR / "01-thong-tin-cua-hang.md").read_text("utf-8")
        self.run_publish(prose_format="docx")
        word = self.docx_text("01-thong-tin-cua-hang.docx")
        import re
        for line in markdown.splitlines():
            stripped = re.sub(r"\s+", " ", line.lstrip("# -").strip())
            if stripped:
                self.assertIn(stripped, word, stripped)


class WhatBlocksIt(unittest.TestCase):
    def test_a_broken_price_blocks_publishing(self):
        problems = [Problem("catalog.xlsx", "giá sai", row=2)]
        self.assertEqual(len(blocking(problems)), 1)

    def test_an_unfilled_voice_file_does_not_block_publishing(self):
        """It is never published, so it cannot reach a customer through this action. It
        blocks switching the agent ON, which is a different action."""
        problems = [Problem("voice.md", "còn 11 mục chưa điền")]
        self.assertEqual(blocking(problems), [])
        self.assertEqual(len(instruction_gaps(problems)), 1)

    def test_warnings_never_block(self):
        problems = [Problem("catalog.xlsx", "chưa có ảnh", row=2, fatal=False)]
        self.assertEqual(blocking(problems), [])


class EndToEnd(unittest.TestCase):
    def test_a_blank_pack_blocks_on_its_data_files_not_its_instructions(self):
        intake = Path(tempfile.mkdtemp()) / "intake"
        write_intake(intake)
        problems = check_intake(intake)
        stoppers = {p.file for p in blocking(problems)}
        self.assertIn("store.md", stoppers)
        self.assertNotIn("voice.md", stoppers)


if __name__ == "__main__":
    unittest.main()
