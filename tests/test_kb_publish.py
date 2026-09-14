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
from lavabo.kb.publish import (KNOWLEDGE_DIR, blocking, instruction_gaps,  # noqa: E402
                               publish)
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

    def run_publish(self):
        return publish(self.intake, self.out, today=date(2026, 9, 14))

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
