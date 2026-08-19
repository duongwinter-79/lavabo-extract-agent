"""Replaying stored pastes after the capture code changes.

The extraction cache already covers its half: it is keyed on the prompt version, the
schema fingerprint and the model, so improving a prompt re-extracts everything. Nothing
covered the other half. An order's .txt is the output of whatever splitting and trimming
ran the day it was captured, and re-pasting does not correct it -- a corrected body that is
SHORTER loses to the stored one, by the same rule that rescues an order from a scroll cut
short.

The tests that matter here are the ones about not destroying anything.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import zalo_capture as zc                                        # noqa: E402
from lavabo import closers, rawpaste, resegment                  # noqa: E402
from lavabo.config import Config                                 # noqa: E402

PASTE = """13/7 đơn 5 (Chị Hương)
1 tủ BC52
Tổng 5.800
Đã cọc 500k
ok chị em nhận rồi
mai giao nhé"""


class Replaying(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "zalo"
        self.inbox.mkdir(parents=True)
        self.cfg = Config()
        self.cfg.zalo.inbox_dir = self.inbox

    def tearDown(self):
        self.tmp.cleanup()

    def _capture_with_old_code(self, text=PASTE):
        """trim=False stands in for a capture bug that kept trailing chatter."""
        zc.handle_orders(text, self.cfg, 7, 2026, all_months=False, trim=False,
                         closer="Trà My")

    def test_a_correction_that_shortens_an_order_is_found(self):
        """The case re-pasting cannot fix, which is why this command exists."""
        self._capture_with_old_code()
        result = resegment.run(self.cfg)
        self.assertEqual([c.kind for c in result.changes], ["changed"])

    def test_a_dry_run_writes_nothing(self):
        self._capture_with_old_code()
        before = (self.inbox / "13-7 đơn 5 (Chị Hương).txt").read_text(encoding="utf-8")
        resegment.run(self.cfg)
        self.assertEqual(
            (self.inbox / "13-7 đơn 5 (Chị Hương).txt").read_text(encoding="utf-8"), before)

    def test_applying_corrects_the_order(self):
        self._capture_with_old_code()
        resegment.run(self.cfg, apply=True)
        body = (self.inbox / "13-7 đơn 5 (Chị Hương).txt").read_text(encoding="utf-8")
        self.assertNotIn("ok chị em nhận rồi", body)
        self.assertIn("Đã cọc 500k", body)

    def test_an_order_todays_code_finds_is_added(self):
        zc.handle_orders("13/7 đơn 5 - Chị Hương\n1 tủ BC52\nTổng 5.800",
                         self.cfg, 7, 2026, all_months=False, trim=True, closer="Trà My")
        rawpaste.store(self.inbox,
                       "13/7 đơn 5 - Chị Hương\n1 tủ BC52\nTổng 5.800\n"
                       "13/7 đơn 6 (Minh Nguyễn)\n1 lavabo\nTổng 2tr",
                       month=7, year=2026, closer="Trà My")
        result = resegment.run(self.cfg, apply=True)
        self.assertIn("added", [c.kind for c in result.changes])
        self.assertTrue((self.inbox / "13-7 đơn 6 (Minh Nguyễn).txt").exists())

    def test_an_order_with_no_stored_paste_is_kept(self):
        """Orders captured before pastes were stored have no source to replay. A
        maintenance command must not be how they go missing."""
        self._capture_with_old_code()
        orphan = self.inbox / "01-7 đơn 1 - Cũ.txt"
        orphan.write_text("1/7 đơn 1 - Cũ\n1 sen\nTổng 1tr", encoding="utf-8")
        result = resegment.run(self.cfg, apply=True)
        self.assertTrue(orphan.exists())
        self.assertIn("unreproducible", [c.kind for c in result.changes])

    def test_a_hand_typed_closer_is_never_overwritten(self):
        """Người chốt đơn is typed by a person and splits the shop's revenue. The replay
        knows whatever the paste was captured under, which may be the older answer."""
        self._capture_with_old_code()
        closers.record(self.inbox, "13-7 đơn 5 (Chị Hương).txt", "Ngọc Anh")
        resegment.run(self.cfg, apply=True)
        self.assertEqual(closers.load(self.inbox)["13-7 đơn 5 (Chị Hương).txt"], "Ngọc Anh")

    def test_replaying_twice_changes_nothing_the_second_time(self):
        self._capture_with_old_code()
        resegment.run(self.cfg, apply=True)
        again = resegment.run(self.cfg)
        self.assertEqual(again.of("changed"), [])
        self.assertEqual(again.of("added"), [])

    def test_the_replay_does_not_restock_the_paste_store(self):
        self._capture_with_old_code()
        before = len(rawpaste.load_index(self.inbox))
        resegment.run(self.cfg, apply=True)
        self.assertEqual(len(rawpaste.load_index(self.inbox)), before)

    def test_nothing_stored_is_not_an_error(self):
        result = resegment.run(self.cfg)
        self.assertEqual(result.pastes, 0)
        self.assertEqual(result.changes, [])

    def test_a_period_filter_only_replays_that_month(self):
        self._capture_with_old_code()
        self.assertEqual(resegment.run(self.cfg, month=7, year=2026).pastes, 1)
        self.assertEqual(resegment.run(self.cfg, month=8, year=2026).pastes, 0)

    def test_the_replay_uses_the_configured_segmenter(self):
        """A fresh Config() here would default ai_segmentation to off, so a shop that
        segments with the model would have its orders replayed by the regex splitter and
        then "corrected" to that answer -- the command undoing what it was told to keep."""
        from lavabo import segment

        self.cfg.extract.ai_segmentation = "on"
        payload = {"orders": [{"header_line": 1, "end_line": 5, "day": 13, "month": 7,
                               "order_number": 5, "customer": "Chị Hương"}]}
        calls = []

        class Fake:
            def complete_json(self, system, user, schema, *, max_tokens=0):
                calls.append(1)
                return segment.Completion(payload, 100, 20, "STOP")

        real = segment.completer_for
        segment.completer_for = lambda cfg: Fake()
        try:
            # The model keeps a Note line the regex trim would cut.
            zc.handle_orders(
                "13/7 đơn 5 (Chị Hương)\n1 tủ BC52\nTổng 5.800\nĐã cọc 500k\nNote: giao sáng",
                self.cfg, 7, 2026, all_months=False, trim=True, closer="Trà My")
            during = len(calls)
            resegment.run(self.cfg, apply=True)
            body = (self.inbox / "13-7 đơn 5 (Chị Hương).txt").read_text(encoding="utf-8")
        finally:
            segment.completer_for = real

        self.assertIn("Note: giao sáng", body, "the replay must not fall back to regex")
        self.assertEqual(len(calls), during,
                         "the replay must reuse the cached answer, not buy it again")

    def test_backup_copies_the_inbox_aside(self):
        self._capture_with_old_code()
        target = resegment.backup(self.inbox)
        try:
            self.assertTrue((target / "13-7 đơn 5 (Chị Hương).txt").exists())
        finally:
            shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
