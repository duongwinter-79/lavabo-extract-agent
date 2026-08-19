"""Checks on the screen-recording validator.

The point of that validator is to catch a recording that skipped part of a month, so the
tests that matter are the ones about NOT claiming a recording is fine. Written with
unittest so they run on a clean install; the image parts skip themselves when the optional
video dependencies are absent, since the rest of the app works without them.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lavabo import video                                          # noqa: E402

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


class Reporting(unittest.TestCase):
    def test_adjacent_gaps_become_one_range(self):
        """A fast swipe gaps every pair of sampled frames. Twenty-five lines a tenth of a
        second apart is not something anyone can act on; one range is."""
        report = video.Report(gaps=[
            video.Gap(0.5, 0.6, "x"), video.Gap(0.6, 0.7, "x"), video.Gap(0.7, 0.8, "x"),
            video.Gap(5.0, 5.1, "x"),
        ])
        merged = report.merged_gaps()
        self.assertEqual([(g.start_seconds, g.end_seconds) for g in merged],
                         [(0.5, 0.8), (5.0, 5.1)])

    def test_distant_gaps_stay_separate(self):
        report = video.Report(gaps=[video.Gap(1.0, 1.1, "x"), video.Gap(9.0, 9.1, "x")])
        self.assertEqual(len(report.merged_gaps()), 2)

    def test_a_recording_with_a_gap_is_not_ok(self):
        self.assertTrue(video.Report().ok)
        self.assertFalse(video.Report(gaps=[video.Gap(1, 2, "x")]).ok)
        self.assertFalse(video.Report(blank=True).ok)

    def test_a_blank_recording_says_what_to_do(self):
        message = " ".join(video.Report(blank=True).messages())
        self.assertIn("quyền ghi màn hình", message)

    def test_timestamps_keep_a_tenth_of_a_second(self):
        """Whole seconds print every short gap as 0:00-0:00, which locates nothing."""
        self.assertEqual(video._mmss(3.8), "0:03.8")
        self.assertEqual(video._mmss(75.2), "1:15.2")

    def test_the_token_estimate_uses_the_real_frame_size(self):
        """Frames are shrunk before they are measured; the cost must be quoted for the
        full-size ones that would actually be sent."""
        report = video.Report(kept=[0, 1, 2], frame_size=(576, 1252))
        self.assertEqual(video.estimated_tokens(report), 3 * 2 * 258)
        self.assertEqual(video.estimated_tokens(video.Report()), 0)


@unittest.skipUnless(HAVE_NUMPY, "numpy/pillow not installed")
class Measuring(unittest.TestCase):
    def _page(self, height=600, width=192, seed=0):
        rng = np.random.default_rng(seed)
        page = np.zeros((height, width), dtype=np.float32)
        for row in range(20, height - 20, 24):          # text-like bands
            page[row:row + 8] = rng.uniform(60, 255, size=(8, width))
        return page

    def test_a_known_scroll_is_measured(self):
        page = self._page(height=1400)
        a, b = page[0:600], page[200:800]               # b is a scrolled 200px
        shift, confidence = video.scroll_between(a, b)
        self.assertAlmostEqual(shift, 200, delta=8)
        self.assertGreater(confidence, video.MIN_CORRELATION)

    def test_two_unrelated_screens_do_not_claim_overlap(self):
        """The failure that matters. Unproven overlap must read as a gap, because that is
        exactly where an order disappears without trace."""
        a, b = self._page(seed=1), self._page(seed=2)
        _, confidence = video.scroll_between(a, b)
        self.assertLess(confidence, video.MIN_CORRELATION)

    def test_scrolling_past_a_whole_screen_is_not_reported_as_a_match(self):
        """The bug this exists for. The search used to return its own ceiling -- a shift
        leaving 8% of the band in common -- and score that sliver at 0.5, which passed.
        A recording that had scrolled past most of a month reported no gaps at all."""
        tall = self._page(height=4000, seed=7)
        a, b = tall[0:600], tall[1800:2400]          # three screens apart: nothing shared
        _, confidence = video.scroll_between(a, b)
        self.assertLess(confidence, video.MIN_CORRELATION,
                        "scrolling past a screen must read as a gap, not a match")

    def test_a_shift_leaving_too_little_in_common_is_refused(self):
        """Overlap has to be real to count. A sliver scores like noise and must not pass."""
        page = self._page(height=2000, seed=8)
        overlapping = video.scroll_between(page[0:600], page[120:720])[1]
        self.assertGreater(overlapping, video.MIN_CORRELATION)
        barely = video.scroll_between(page[0:600], page[560:1160])[1]
        self.assertLess(barely, video.MIN_CORRELATION)

    def test_fixed_chrome_is_excluded_from_the_content_band(self):
        """A status bar and a message box never move. Left in, they make every pair of
        frames look partly identical and hide real scrolling."""
        tall = self._page(height=1600)
        chrome_top = np.full((80, 192), 200.0, dtype=np.float32)
        chrome_bottom = np.full((60, 192), 150.0, dtype=np.float32)
        frames = [np.vstack([chrome_top, tall[i * 100:i * 100 + 600], chrome_bottom])
                  for i in range(6)]
        top, bottom = video.content_band(frames)
        self.assertGreaterEqual(top, 60)
        self.assertLessEqual(bottom, 80 + 600 + 20)

    def test_a_blurred_frame_scores_lower_than_a_sharp_one(self):
        sharp = self._page()
        blurred = sharp.copy()
        for _ in range(4):                              # crude vertical smear
            blurred = (blurred + np.roll(blurred, 1, axis=0)) / 2
        self.assertGreater(video.sharpness(sharp), video.sharpness(blurred) * 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
