"""Checking a screen recording before anyone trusts what was read out of it.

Capturing a month on a phone means recording the screen while scrolling Zalo, because a
phone cannot select-all a conversation. That works, but it fails in a way this project has
learned to fear: scroll too fast and a screenful of orders is simply never on any frame,
and nothing downstream can tell the difference between an order that was never posted and
one that was scrolled past. It is the Ctrl+A truncation problem again, in a new costume.

So the recording is checked before it is read. This module answers three questions:

    Did the screen actually record, or is it blank?
    Did the scroll ever move more than a screen between usable frames?
    Which frames are worth spending vision tokens on?

The first two are reported as timestamps the operator can go and re-record. That is the
whole point: "0:34-0:37 was too fast" is a thing somebody can fix, while a quietly missing
order is found weeks later by reconciling a spreadsheet.

Nothing here reads text. It decides whether reading would be worth doing.
"""

from __future__ import annotations

import logging
import math
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# How much of a screen may pass between two kept frames. Well under half, so consecutive
# frames overlap generously -- an order split by a frame edge then appears whole in the
# next one, and merge_into prefers the fuller capture without being told.
STEP_FRACTION = 0.45

# Frames per second pulled out of the video before choosing between them. Higher than the
# rate we keep, so there is a choice: a frame caught mid-swipe is smeared, and one caught
# during the pause after it is not.
SAMPLE_FPS = 10

# Below this share of the TYPICAL frame, a frame is motion-blurred enough that small text
# -- which is where the money is -- cannot be relied on. Measured against the median and
# not the sharpest frame: a ratio against the maximum moves with one unusually crisp
# frame, and rejected 20% of a recording at full size against 49% of the same recording
# shrunk for analysis. The median barely moves between the two.
SHARPNESS_FLOOR = 0.6

# Row-signature correlation below which two frames cannot be shown to overlap. Treated as
# a gap rather than as an overlap, deliberately: unproven overlap is exactly the state
# where an order goes missing quietly.
MIN_CORRELATION = 0.30

# The least two frames may share before a match between them means anything. Without it
# the search returned its own ceiling -- a shift leaving 32 rows of a 408-row band in
# common, 8% -- and scored those 32 rows at 0.5, which passed. A recording that had
# scrolled past most of a month was reported as having no gaps at all. A shift that would
# leave less than this is not a small overlap; it is a page that moved further than the
# screen, which is exactly the thing being looked for.
MIN_OVERLAP_FRACTION = 0.25


class VideoToolsMissing(RuntimeError):
    """Raised with a fix, not a traceback -- this lands in front of a shop operator."""


@dataclass
class Gap:
    """A stretch where the scroll outran the frames. Timestamps, because the operator's
    next action is to re-record that part."""
    start_seconds: float
    end_seconds: float
    reason: str

    def __str__(self) -> str:
        return (f"{_mmss(self.start_seconds)}–{_mmss(self.end_seconds)}: {self.reason}")


@dataclass
class Report:
    duration_seconds: float = 0.0
    sampled: int = 0
    kept: list[int] = field(default_factory=list)      # indices into the sampled frames
    kept_seconds: list[float] = field(default_factory=list)
    screens_covered: float = 0.0
    blank: bool = False
    gaps: list[Gap] = field(default_factory=list)
    blurred: int = 0
    frame_size: tuple[int, int] = (0, 0)

    @property
    def ok(self) -> bool:
        return not self.blank and not self.gaps

    def merged_gaps(self, join_within: float = 0.6) -> list[Gap]:
        """Adjacent gaps collapsed into one range.

        A fast swipe produces a gap between every pair of sampled frames, and reporting
        twenty-five of them a tenth of a second apart is not something anyone can act on.
        One line saying "0:03-0:05 was too fast" is.
        """
        out: list[Gap] = []
        for gap in sorted(self.gaps, key=lambda g: g.start_seconds):
            if out and gap.start_seconds - out[-1].end_seconds <= join_within:
                out[-1] = Gap(out[-1].start_seconds, gap.end_seconds, out[-1].reason)
            else:
                out.append(Gap(gap.start_seconds, gap.end_seconds, gap.reason))
        return out

    def messages(self) -> list[str]:
        """Vietnamese, because this is read on the phone that made the recording."""
        if self.blank:
            return ["Video trống — màn hình không được ghi lại. "
                    "Kiểm tra lại quyền ghi màn hình rồi quay lại."]
        out = [f"Video {_mmss(self.duration_seconds)}, đã xem {self.sampled} khung hình, "
               f"giữ lại {len(self.kept)} khung, cuộn qua khoảng {self.screens_covered:.0f} "
               f"màn hình."]
        gaps = self.merged_gaps()
        if gaps:
            lost = sum(g.end_seconds - g.start_seconds for g in gaps)
            out.append(f"Cuộn quá nhanh ở {len(gaps)} đoạn (tổng {lost:.0f} giây) — "
                       "phần này có thể sót đơn. Quay lại các đoạn:")
            out.extend(f"  {gap}" for gap in gaps)
            out.append("Mẹo: vuốt nửa màn hình rồi dừng 1 giây, đừng vuốt liên tục.")
        else:
            out.append("Không có đoạn nào cuộn quá nhanh.")
        if self.blurred:
            out.append(f"{self.blurred} khung hình bị nhoè và đã bị loại.")
        return out


def _mmss(seconds: float) -> str:
    """m:ss.t — the tenth matters, because a gap can be shorter than a second and a
    range printed as "0:00-0:00" tells the operator nothing about where to look."""
    return f"{int(seconds) // 60}:{seconds % 60:04.1f}"


def ffmpeg_path() -> str:
    """A usable ffmpeg, from PATH or from the pip wheel that bundles one.

    The wheel matters: this runs on a shop laptop where nobody is going to install ffmpeg
    by hand, and a missing codec is not a reason to lose a month of orders.
    """
    if found := shutil.which("ffmpeg"):
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise VideoToolsMissing(
            "Chưa cài ffmpeg để đọc video. Chạy:  pip install imageio-ffmpeg"
        ) from exc


def _numpy():
    try:
        import numpy
        return numpy
    except ImportError as exc:
        raise VideoToolsMissing(
            "Chưa cài thư viện đọc ảnh. Chạy:  pip install numpy pillow") from exc


# Width the frames are shrunk to before they are measured. Everything here works on
# per-row statistics, so horizontal detail is wasted effort -- and at full width a
# 13-second clip took 16 seconds to check, which for a real two-minute recording would
# have been slower than watching it.
ANALYSIS_WIDTH = 192


def probe(path: Path) -> tuple[float, int, int]:
    """(duration seconds, width, height) from ffmpeg's own report.

    The true frame size is read here and kept, because the analysis works on shrunken
    copies and the vision cost must be quoted for the real ones.
    """
    proc = subprocess.run([ffmpeg_path(), "-i", str(path)],
                          capture_output=True, text=True, errors="replace")
    seconds, width, height = 0.0, 0, 0
    for line in proc.stderr.splitlines():
        if "Duration:" in line and not seconds:
            stamp = line.split("Duration:")[1].split(",")[0].strip()
            try:
                hours, minutes, secs = stamp.split(":")
                seconds = int(hours) * 3600 + int(minutes) * 60 + float(secs)
            except ValueError:
                pass
        if "Video:" in line and not width:
            for token in line.split(","):
                token = token.strip().split(" ")[0]
                if "x" in token:
                    a, _, b = token.partition("x")
                    if a.isdigit() and b.isdigit():
                        width, height = int(a), int(b)
                        break
    return seconds, width, height


def duration_seconds(path: Path) -> float:
    return probe(path)[0]


def extract_frames(path: Path, out_dir: Path, *, fps: int = SAMPLE_FPS,
                   width: int | None = ANALYSIS_WIDTH) -> list[Path]:
    """Frames as PNGs. `width` shrinks them; pass None for full size."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scale = f",scale={width}:-1" if width else ""
    subprocess.run(
        [ffmpeg_path(), "-loglevel", "error", "-i", str(path),
         "-vf", f"fps={fps}{scale}", str(out_dir / "f%05d.png")],
        check=True, capture_output=True)
    return sorted(out_dir.glob("*.png"))


def _load(paths: list[Path]):
    np = _numpy()
    try:
        from PIL import Image
    except ImportError as exc:
        raise VideoToolsMissing("Chưa cài Pillow. Chạy:  pip install pillow") from exc
    return [np.asarray(Image.open(p).convert("L"), dtype=np.float32) for p in paths]


def content_band(frames) -> tuple[int, int]:
    """Rows that actually change: the chat, without the phone's fixed furniture.

    The status bar, the chat header and the message box never move, so leaving them in
    makes every pair of frames look partly identical and hides real scrolling. Found from
    the recording rather than hardcoded, since it differs by phone, by orientation and by
    whether a notch is in the way.
    """
    np = _numpy()
    stack = np.stack(frames)
    per_row = stack.std(axis=0).mean(axis=1)          # how much each row varies over time
    if per_row.max() <= 0:
        return 0, frames[0].shape[0]
    moving = per_row > per_row.max() * 0.08
    rows = np.flatnonzero(moving)
    if rows.size == 0:
        return 0, frames[0].shape[0]
    return int(rows[0]), int(rows[-1]) + 1


def sharpness(frame) -> float:
    """Variance of a Laplacian. Low means smeared by motion, and small text goes first."""
    inner = (frame[1:-1, 1:-1] * 4 - frame[:-2, 1:-1] - frame[2:, 1:-1]
             - frame[1:-1, :-2] - frame[1:-1, 2:])
    return float(inner.var())


# How many horizontal buckets each row is summarised into. One number per row -- how much
# detail it holds -- describes a row's SHAPE but not its CONTENT, and two unrelated screens
# of chat have much the same shape: bands of text separated by gaps. Tested against two
# unrelated screens it reported 0.996 confidence, which would have made the gap detector
# blind in exactly the case it exists for. Splitting the row into buckets makes rows
# distinguishable by what is in them and drops that to noise.
SIGNATURE_BUCKETS = 8


def _buckets(frame):
    """Horizontal detail per row, in SIGNATURE_BUCKETS columns across the width.

    Built from gradients rather than raw pixels because a video playing inside the chat
    changes pixels without the page moving at all, and matching raw pixels then reports
    scrolling that never happened.
    """
    np = _numpy()
    gradient = np.abs(np.diff(frame, axis=1))
    width = gradient.shape[1]
    edges = [round(i * width / SIGNATURE_BUCKETS) for i in range(SIGNATURE_BUCKETS + 1)]
    return np.stack([gradient[:, edges[i]:edges[i + 1]].mean(axis=1)
                     for i in range(SIGNATURE_BUCKETS) if edges[i + 1] > edges[i]], axis=1)


def _match(a_rows, b_rows) -> float:
    """How alike two equal-height strips are, from each row's horizontal PROFILE.

    A single number per row -- how much detail it holds -- describes a row's shape and not
    its content, and two unrelated screens of chat have much the same shape: bands of text
    with gaps between them. Measured that way, two unrelated screens scored 0.97, which
    would have made the gap detector blind in exactly the case it exists for.

    Centring each row removes "how much" and leaves "what arrangement", so a text row only
    matches a text row laid out the same way. Weighting by row energy keeps blank rows --
    whose centred profile is pure noise -- from voting. Unrelated screens now score ~0.03.
    """
    np = _numpy()
    a = a_rows - a_rows.mean(axis=1, keepdims=True)
    b = b_rows - b_rows.mean(axis=1, keepdims=True)
    energy_a = np.linalg.norm(a, axis=1)
    energy_b = np.linalg.norm(b, axis=1)
    cosine = (a * b).sum(axis=1) / (energy_a * energy_b + 1e-6)
    weight = np.minimum(energy_a, energy_b)
    return float((cosine * weight).sum() / (weight.sum() + 1e-6))


def scroll_between(a, b, *, coarse: int = 8) -> tuple[int, float]:
    """(rows scrolled from a to b, confidence 0-1). Confidence is the point.

    A low correlation does not mean "no scrolling", it means "cannot tell" -- and the
    caller must treat that as a gap, because unproven overlap is where orders vanish.

    Searched coarsely then refined, because the exact search cost 16 seconds on a 13
    second video and a real recording is ten times longer. Scrolling moves whole text
    rows, so a stride of 8 pixels cannot step over the answer, only near it.
    """
    rows_a, rows_b = _buckets(a), _buckets(b)
    height = len(rows_a)
    # Never consider a shift that would leave too little in common: a score over a
    # sliver is noise, and returning it as a match is how a lost screenful reads as
    # a fine one.
    limit = max(1, int(height * (1 - MIN_OVERLAP_FRACTION)))

    def score_at(shift: int) -> float:
        return _match(rows_a[shift:], rows_b[:height - shift])

    best_shift, best_score = 0, -1.0
    for shift in range(0, limit, coarse):
        if (score := score_at(shift)) > best_score:
            best_shift, best_score = shift, score
    for shift in range(max(0, best_shift - coarse), min(limit, best_shift + coarse)):
        if (score := score_at(shift)) > best_score:
            best_shift, best_score = shift, score

    # Pinned at the ceiling means the true shift is at least this far -- the page
    # moved further than the search may look, so the two cannot be shown to overlap.
    if best_shift >= limit - coarse:
        return best_shift, 0.0
    return best_shift, best_score


def analyse(path: Path, *, sample_fps: int = SAMPLE_FPS,
            step_fraction: float = STEP_FRACTION) -> Report:
    """Check a recording and choose the frames worth reading. Reads no text."""
    seconds, width, height = probe(path)
    report = Report(duration_seconds=seconds, frame_size=(width, height))

    with tempfile.TemporaryDirectory(prefix="lavabo-video-") as tmp:
        paths = extract_frames(path, Path(tmp), fps=sample_fps)
        if not paths:
            report.blank = True
            return report
        frames = _load(paths)

    report.sampled = len(frames)
    if not width:                      # ffmpeg did not report a size; fall back to ours
        report.frame_size = (frames[0].shape[1], frames[0].shape[0])

    np = _numpy()
    # A recording an app blocked comes back a flat colour: no detail anywhere, in any
    # frame. Cheaper to catch here than after spending vision tokens on 96 black squares.
    if max(sharpness(f) for f in frames) < 1.0:
        report.blank = True
        return report

    top, bottom = content_band(frames)
    band = [f[top:bottom] for f in frames]
    height = band[0].shape[0]
    if height < 20:                        # nothing scrolled: the whole screen was static
        report.blank = True
        return report

    sharps = [sharpness(f) for f in band]
    floor = statistics.median(sharps) * SHARPNESS_FLOOR
    report.blurred = sum(1 for s in sharps if s < floor)

    step = height * step_fraction
    seconds_per_frame = 1.0 / sample_fps
    kept = [0]
    travelled = 0.0
    since_kept = 0.0

    for i in range(1, len(band)):
        shift, confidence = scroll_between(band[i - 1], band[i])
        if confidence < MIN_CORRELATION:
            # Two adjacent samples with nothing in common: at this sampling rate the page
            # moved further than a screen, so whatever was between them was never filmed.
            report.gaps.append(Gap((i - 1) * seconds_per_frame, i * seconds_per_frame,
                                   "cuộn quá nhanh, mất nội dung giữa hai khung hình"))
            since_kept = 0.0
            kept.append(i)
            continue
        travelled += shift
        since_kept += shift
        if since_kept >= step:
            # Take the sharpest of the next few frames rather than this exact one: the
            # pause at the end of a swipe is where the text is crisp.
            window = [k for k in range(i, min(i + 3, len(band))) if sharps[k] >= floor]
            kept.append(max(window, key=lambda k: sharps[k]) if window else i)
            since_kept = 0.0

    report.kept = sorted(set(kept))
    report.kept_seconds = [round(k * seconds_per_frame, 1) for k in report.kept]
    report.screens_covered = travelled / height if height else 0.0
    return report


def estimated_tokens(report: Report, *, tokens_per_tile: int = 258,
                     tile: int = 768) -> int:
    """What reading the kept frames would cost, in Gemini image tokens.

    Reported before anything is sent, because "this recording will cost 50,000 tokens" is
    a decision somebody may want to make while still standing next to the phone.
    """
    width, height = report.frame_size
    if not width or not height:
        return 0
    tiles = 1 if (width <= 384 and height <= 384) else (
        math.ceil(width / tile) * math.ceil(height / tile))
    return len(report.kept) * tiles * tokens_per_tile


def frame_at(path: Path, seconds: float, out: Path) -> Path | None:
    """One full-size frame, seeked to directly. None if ffmpeg produced nothing."""
    subprocess.run(
        [ffmpeg_path(), "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", str(path),
         "-frames:v", "1", "-y", str(out)],
        check=False, capture_output=True)
    return out if out.exists() and out.stat().st_size else None


def kept_frames(path: Path, report: Report) -> list[bytes]:
    """The chosen frames, full size, as PNG bytes in time order.

    Seeked one at a time rather than dumping every frame at full size first: a two-minute
    recording at the sampling rate is over a thousand frames, and writing them all to reach
    the hundred that were chosen would put a third of a gigabyte through a shop laptop's
    disk for nothing.

    Full size on purpose -- the shrunken copies exist to be measured, and reading small
    text off them is exactly what must not happen.
    """
    out: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="lavabo-frames-") as tmp:
        for index, seconds in enumerate(report.kept_seconds):
            target = Path(tmp) / f"k{index:05d}.png"
            if frame_at(path, seconds, target):
                out.append(target.read_bytes())
            else:
                log.warning("could not read the frame at %.1fs", seconds)
    return out
