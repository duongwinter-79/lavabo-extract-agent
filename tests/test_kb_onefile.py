"""The pack as one workbook, and the rules following it there.

The point of this format is a shop owner with a phone and no Excel: one Google Sheets
link, filled in a browser. The risk is having two packs whose rules quietly diverge, so
these tests are mostly about the single workbook reaching the SAME verdict as the folder.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openpyxl import load_workbook                                # noqa: E402

from lavabo.kb import spec                                        # noqa: E402
from lavabo.kb.check import check_one_file                        # noqa: E402
from lavabo.kb.onefile import ANSWER_COLUMN, DOC_TABS, TABS, write_one_file  # noqa: E402

from test_kb_check import GOOD                                    # noqa: E402


class TheWorkbook(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "pack.xlsx"
        write_one_file(self.path)

    def test_every_form_is_a_tab(self):
        names = load_workbook(self.path).sheetnames
        self.assertEqual(names[0], "Đọc trước")
        for title in list(DOC_TABS) + list(TABS):
            self.assertIn(title, names)

    def test_the_data_tabs_carry_the_same_columns_as_the_folder_pack(self):
        book = load_workbook(self.path)
        for title, sheet in TABS.items():
            with self.subTest(title):
                header = [c.value for c in book[title][1]]
                self.assertEqual(header, sheet.names)

    def test_the_handoff_tab_carries_its_seeded_rows(self):
        """The one-file build had its own copy of the row-seeding logic, so this tab
        shipped with a single example row where twelve rules should be."""
        ws = load_workbook(self.path)["Không được tự trả lời"]
        topics = [r[0] for r in ws.iter_rows(min_row=2, values_only=True) if r[0]]
        self.assertEqual(len(topics), len(spec.LOCKED_TOPICS))
        self.assertIn("Khiếu nại, hàng lỗi, hàng vỡ", topics)

    def test_seeded_tabs_carry_no_example_row(self):
        ws = load_workbook(self.path)["Không được tự trả lời"]
        cells = [str(c) for row in ws.iter_rows(values_only=True) for c in row if c]
        self.assertFalse(any(spec.EXAMPLE_MARKER in c for c in cells))

    def test_it_refuses_to_overwrite_without_force(self):
        with self.assertRaises(FileExistsError):
            write_one_file(self.path)
        write_one_file(self.path, force=True)

    def test_an_untouched_workbook_names_the_missing_answers(self):
        fatal = [p for p in check_one_file(self.path) if p.fatal]
        self.assertEqual(len(fatal), len(DOC_TABS))
        self.assertTrue(any("Tên shop" in str(p) for p in fatal))

    def test_problems_are_located_by_tab_not_by_filename(self):
        problems = [str(p) for p in check_one_file(self.path)]
        self.assertTrue(any("tab 'Cửa hàng'" in p for p in problems), problems)
        self.assertFalse(any("store.md" in p for p in problems), problems)


class FillingItIn(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "pack.xlsx"
        write_one_file(self.path)
        self.book = load_workbook(self.path)

    def save(self):
        self.book.save(self.path)

    def answer_everything(self):
        for title, doc in DOC_TABS.items():
            ws = self.book[title]
            for row in ws.iter_rows(min_row=2):
                label = row[0].value
                if label and not str(label).startswith(("—", doc.why[:20])):
                    ws.cell(row=row[0].row, column=ANSWER_COLUMN, value="xong")

    def write_catalog(self, row: dict):
        ws = self.book["Danh mục sản phẩm"]
        ws.delete_rows(2, ws.max_row)
        ws.append([row.get(c) for c in spec.CATALOG.names])

    def test_a_fully_answered_workbook_has_no_fatal_problems(self):
        self.answer_everything()
        self.write_catalog(GOOD)
        self.save()
        fatal = [str(p) for p in check_one_file(self.path) if p.fatal]
        self.assertEqual(fatal, [])

    def test_a_bad_price_is_caught_in_the_workbook_too(self):
        """Google Sheets drops the whole-number guard the Excel form has, so this is the
        only thing standing between "2tr850" and a feed."""
        self.answer_everything()
        self.write_catalog({**GOOD, "gia_niem_yet": "2tr850"})
        self.save()
        fatal = [str(p) for p in check_one_file(self.path) if p.fatal]
        self.assertEqual(len(fatal), 1)
        self.assertIn("2850000", fatal[0])
        self.assertIn("Danh mục sản phẩm", fatal[0])

    def test_an_expired_promo_is_caught_in_the_workbook_too(self):
        self.answer_everything()
        self.write_catalog({**GOOD, "gia_km": 2550000,
                            "km_den_ngay": date(2020, 1, 1)})
        self.save()
        self.assertTrue(any("hết hạn" in str(p)
                            for p in check_one_file(self.path) if p.fatal))

    def test_a_placeholder_typed_back_in_still_counts_as_empty(self):
        """Somebody will paste the marker into the answer column. It is not an answer."""
        self.answer_everything()
        ws = self.book["Cửa hàng"]
        for row in ws.iter_rows(min_row=2):
            if row[0].value == "Tên shop":
                ws.cell(row=row[0].row, column=ANSWER_COLUMN, value=spec.PLACEHOLDER)
        self.write_catalog(GOOD)
        self.save()
        fatal = [str(p) for p in check_one_file(self.path) if p.fatal]
        self.assertTrue(any("Tên shop" in f for f in fatal), fatal)


class ItIsMissing(unittest.TestCase):
    def test_a_path_that_does_not_exist_is_reported_not_raised(self):
        problems = check_one_file(Path(tempfile.mkdtemp()) / "nope.xlsx")
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].fatal)


if __name__ == "__main__":
    unittest.main()
