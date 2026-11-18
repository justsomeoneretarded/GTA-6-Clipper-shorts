import unittest
from unittest.mock import Mock, patch

from clipper.core import Moment, Range, Source, WEIGHTS, duplicate, source_url
from clipper.discovery import discover, grounding_source, listed_twitch_source, tavily_discovery
from clipper.editorial import bounded, build_catalog, score_cut, scout_windows
from clipper.gemini import QuotaError, quota_error
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

    @patch("clipper.discovery.requests.get")
    def test_gemini_grounding_links_resolve_to_platform_sources(self, get):
        response = get.return_value.__enter__.return_value
        response.is_redirect = True
        response.headers = {"Location": "https://youtu.be/TestGTA0001"}
        link = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/example"
        self.assertEqual(grounding_source(link).key, "youtube:TestGTA0001")
        get.assert_called_once_with(link, allow_redirects=False, stream=True, timeout=(5, 8))

    def test_twitch_fallback_uses_game_category(self):
        clip = {"title": "GTA VI now", "url": "https://www.twitch.tv/channel/clip/SomeSlug",
                "createdAt": "2026-11-18T11:30:00Z", "durationSeconds": 30,
                "game": {"name": "Grand Theft Auto VI"}}
        earliest = "2026-11-18T11:00:00Z"
        self.assertEqual(listed_twitch_source(clip, "channel", clip=True, earliest=earliest).duration, 30)
        clip["game"]["name"] = "Grand Theft Auto V"
        self.assertIsNone(listed_twitch_source(clip, "channel", clip=True, earliest=earliest))

    def test_zero_search_quota_explains_the_billing_check(self):
        response = Mock()
        response.json.return_value = {"error": {"details": [{"violations": [{"quotaValue": "0"}]}]}}
        message, zero_limit = quota_error(response, search=True)
        self.assertTrue(zero_limit)
        self.assertIn("Google Search returned HTTP 429", message)

    @patch("clipper.discovery.requests.post")
    def test_tavily_uses_basic_search_and_canonical_urls(self, post):
        post.return_value.ok = True
        post.return_value.json.return_value = {
            "results": [{"url": "https://youtu.be/TestGTA0001"}, {"url": "https://example.com/movie"}],
        }
        self.assertEqual({item.key for item in tavily_discovery("example")}, {"youtube:TestGTA0001"})
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(call.kwargs["json"]["search_depth"] == "basic" for call in post.call_args_list))

    @patch.dict("os.environ", {}, clear=True)
    @patch("clipper.discovery.youtube_fallback")
    @patch("clipper.discovery.twitch_fallback")
    def test_twitch_results_do_not_skip_youtube_discovery(self, twitch, youtube):
        def list_twitch(sources, report, days):
            for count in range(9):
                source = source_url(f"https://clips.twitch.tv/FixtureClip{count}")
                source.duration = 30
                sources[source.key] = source

        twitch.side_effect = list_twitch
        video = source_url("https://youtu.be/TestGTA0001")
        video.duration = 60
        youtube.return_value = [video]
        report = []
        sources = discover(Mock(), Mock(), report)
        self.assertEqual(len(sources), 10)
        self.assertIn(video.key, sources)
        self.assertEqual(youtube.call_count, 2)
        self.assertIn("YouTube discovery collected 1 video link.", report)

    @patch("clipper.editorial.audition", side_effect=QuotaError("quota"))
    @patch("clipper.editorial.scout")
    def test_quota_pause_preserves_the_scouted_moment(self, scout, audition):
        source = source_url("https://www.youtube.com/watch?v=TestGTA0001")
        source.duration = 310
        scout.return_value = {"subject": "gameplay", "sequences": [], "moments": [
            {"start": 15, "end": 45, "theme": "chase", "hook": "Roadblock", "setup": "Escape route",
             "payoff": "Gets away", "evidence": "Police car passes", "potential": 90},
        ]}
        result = build_catalog(Mock(), Mock(), {source.key: source}, {}, [])
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(result["moments"], [])
        self.assertIn(source.key, result["sources"])

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
