import unittest

from clipper.core import Moment, Range, Source, WEIGHTS, duplicate, source_url
from clipper.editorial import bounded, score_cut, scout_windows
from clipper.plans import is_new


class EditorialTests(unittest.TestCase):
    def test_canonical_urls(self):
        link = source_url("https://youtu.be/TestGTA0001?si=foo")
        self.assertEqual(link.url, "https://www.youtube.com/watch?v=TestGTA0001")
        self.assertIsNone(source_url("https://www.youtube.com.example.org/watch?v=TestGTA0001"))
        self.assertIsNone(source_url("https://["))

    def test_window_ranges_are_relative(self):
        window = Range("youtube:video", 200, 280)
        result = bounded({"start": 10, "end": 25}, window.source, window)
        self.assertEqual((result.start, result.end), (210, 225))
        with self.assertRaises(ValueError):
            bounded({"start": 10, "end": 100}, window.source, window)

    def test_strong_scores_cannot_mask_a_weak_hook(self):
        row = {"scores": dict.fromkeys(WEIGHTS, 9), "confidence": .93, "time_to_hook": 2}
        self.assertEqual(score_cut(row, 70), 90)
        row["scores"]["hook"] = 5
        with self.assertRaises(ValueError):
            score_cut(row, 70)

    def test_twitch_clip_and_vod_are_the_same_moment(self):
        source = Source("twitch-clip:Slug", "", "twitch-clip", parent="twitch-vod:123", parent_offset=100)
        vod = Source("twitch-vod:123", "", "twitch-vod")
        sources = {source.key: source, vod.key: vod}
        a = Moment(Range(source.key, 0, 20), "chase", "", "", "", "", 90)
        b = Moment(Range(vod.key, 102, 119), "chase", "", "", "", "", 91)
        self.assertTrue(duplicate(a, b, sources))
        self.assertFalse(is_new(a.span, [b.span], sources))

    def test_long_streams_include_non_peak_windows(self):
        source = Source("youtube:video", "", "youtube", duration=7200, anchors=[1850])
        windows = scout_windows(source)
        self.assertTrue(any(window.start <= 1850 <= window.end for window in windows))
        self.assertTrue(any(window.start >= 5000 for window in windows))
        self.assertTrue(all(0 <= window.start < window.end <= 7200 for window in windows))
