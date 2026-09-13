"""What the Meta feed has to get right.

The feed is the moment a spreadsheet stops being a spreadsheet: after this file is
uploaded, the Page quotes these numbers to customers by itself. So the cases here are
about the conversions that silently change a price or a promise -- a currency suffix
Meta parses, an availability word that means "we have it", a sale window that says when
the discount stops.
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lavabo.kb.feed import build_feed                              # noqa: E402

from test_kb_check import GOOD                                     # noqa: E402

TODAY = date.today()


def feed(rows: list[dict], **kw) -> list[dict]:
    out = Path(tempfile.mkdtemp()) / "feed.csv"
    kw.setdefault("link", "https://facebook.com/senkahomes")
    build_feed([{**r, "_row": 2} for r in rows], out, **kw)
    with out.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class Mapping(unittest.TestCase):
    def test_price_carries_the_currency_meta_expects(self):
        self.assertEqual(feed([GOOD])[0]["price"], "2850000 VND")

    def test_the_id_is_the_shops_own_code(self):
        row = feed([GOOD])[0]
        self.assertEqual(row["id"], "BC52-80-TRANG")
        # brand may be blank for a shop with no brand; mpn is what keeps the row legal.
        self.assertEqual(row["mpn"], "BC52-80-TRANG")

    def test_title_carries_size_and_colour(self):
        title = feed([{**GOOD, "ten_sp": "Tủ lavabo BC52", "mau": "trắng; xám"}])[0]["title"]
        self.assertEqual(title, "Tủ lavabo BC52 - 80cm - trắng/xám")

    def test_title_does_not_repeat_what_the_name_already_says(self):
        """A customer sees this string in the catalogue. "Tủ lavabo BC52 80cm - 80cm"
        is what happens when the shop puts the size in both columns, which they will."""
        title = feed([{**GOOD, "ten_sp": "Tủ lavabo BC52 80cm", "mau": ""}])[0]["title"]
        self.assertEqual(title, "Tủ lavabo BC52 80cm")

    def test_what_the_price_includes_travels_with_it(self):
        """docs/13 §7 makes the agent say this every time it quotes. The description is
        the only field that reaches the model alongside the number."""
        self.assertIn("chưa gồm ship", feed([GOOD])[0]["description"])

    def test_condition_is_always_new(self):
        self.assertEqual(feed([GOOD])[0]["condition"], "new")


class Availability(unittest.TestCase):
    def test_the_three_vietnamese_states_map_to_meta_states(self):
        for ours, theirs in (("còn hàng", "in stock"),
                             ("hết hàng", "out of stock"),
                             ("đặt trước", "preorder")):
            with self.subTest(ours):
                self.assertEqual(feed([{**GOOD, "tinh_trang": ours}])[0]["availability"],
                                 theirs)

    def test_an_unrecognised_state_becomes_preorder_not_in_stock(self):
        """The safe direction. Guessing "in stock" promises something the shop may not
        have; guessing "preorder" only costs a slightly slower answer."""
        self.assertEqual(feed([{**GOOD, "tinh_trang": "?"}])[0]["availability"], "preorder")


class Sales(unittest.TestCase):
    def test_a_live_promo_becomes_sale_price_with_an_end_date(self):
        until = TODAY + timedelta(days=20)
        row = feed([{**GOOD, "gia_km": 2550000, "km_den_ngay": until}])[0]
        self.assertEqual(row["sale_price"], "2550000 VND")
        self.assertIn(until.strftime("%Y-%m-%d"), row["sale_price_effective_date"])
        self.assertIn("/", row["sale_price_effective_date"])
        self.assertEqual(row["price"], "2850000 VND")

    def test_no_promo_leaves_both_sale_fields_empty(self):
        row = feed([GOOD])[0]
        self.assertEqual(row["sale_price"], "")
        self.assertEqual(row["sale_price_effective_date"], "")

    def test_the_window_carries_the_shops_utc_offset(self):
        row = feed([{**GOOD, "gia_km": 2550000, "km_den_ngay": TODAY + timedelta(days=5)}],
                   timezone_name="Asia/Ho_Chi_Minh")[0]
        self.assertIn("+07:00", row["sale_price_effective_date"])


class LinksAndImages(unittest.TestCase):
    def test_one_link_can_serve_every_row(self):
        rows = feed([GOOD, {**GOOD, "ma_sp": "GUONG-60"}])
        self.assertEqual({r["link"] for r in rows}, {"https://facebook.com/senkahomes"})

    def test_a_template_is_filled_per_product(self):
        row = feed([GOOD], link="", link_template="https://shop.vn/sp/{ma_sp}")[0]
        self.assertEqual(row["link"], "https://shop.vn/sp/BC52-80-TRANG")

    def test_images_need_a_public_base_or_they_are_left_empty(self):
        """Not an error: phone photos in a folder genuinely have no URL, and docs/13 §6.3
        says to add those items by hand instead. The count is reported to the caller."""
        self.assertEqual(feed([GOOD])[0]["image_link"], "")

    def test_a_base_url_turns_filenames_into_links(self):
        row = feed([{**GOOD, "anh": "a.jpg; b.jpg; c.jpg"}],
                   image_base="https://cdn.shop.vn/anh/")[0]
        self.assertEqual(row["image_link"], "https://cdn.shop.vn/anh/a.jpg")
        self.assertEqual(row["additional_image_link"],
                         "https://cdn.shop.vn/anh/b.jpg,https://cdn.shop.vn/anh/c.jpg")


class WhatTheCallerIsTold(unittest.TestCase):
    def build(self, rows, **kw):
        out = Path(tempfile.mkdtemp()) / "feed.csv"
        kw.setdefault("link", "https://facebook.com/x")
        return build_feed([{**r, "_row": 2} for r in rows], out, **kw)

    def test_rows_without_images_are_counted(self):
        result = self.build([GOOD, {**GOOD, "ma_sp": "X", "anh": ""}])
        self.assertEqual(result.rows, 2)
        self.assertEqual(result.without_image, 2)

    def test_hosted_images_clear_the_count(self):
        result = self.build([GOOD], image_base="https://cdn.shop.vn")
        self.assertEqual(result.without_image, 0)

    def test_sale_rows_are_counted(self):
        result = self.build([GOOD,
                             {**GOOD, "ma_sp": "X", "gia_km": 2000000,
                              "km_den_ngay": TODAY + timedelta(days=3)}])
        self.assertEqual(result.on_sale, 1)

    def test_the_file_is_replaced_atomically(self):
        out = Path(tempfile.mkdtemp()) / "feed.csv"
        build_feed([{**GOOD, "_row": 2}], out, link="https://facebook.com/x")
        self.assertTrue(out.exists())
        self.assertEqual(list(out.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
