import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from clipper.core import GTA_VI_PUBLICATION_CUTOFF, Moment, Range, Source, WEIGHTS, dated, duplicate, source_url
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

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch.dict("os.environ", {}, clear=True)
    @patch("clipper.discovery.youtube_fallback")
    @patch("clipper.discovery.twitch_fallback")
    def test_twitch_results_do_not_skip_date_filtered_youtube_discovery(self, twitch, youtube, clock):
        def list_twitch(sources, report, days):
            for count in range(9):
                source = source_url(f"https://clips.twitch.tv/FixtureClip{count}")
                source.duration = 30
                source.published = dated(1)
                sources[source.key] = source

        twitch.side_effect = list_twitch
        video = source_url("https://youtu.be/TestGTA0001")
        video.duration = 60
        video.published = GTA_VI_PUBLICATION_CUTOFF
        old = source_url("https://youtu.be/TestGTA0002")
        old.duration = 60
        old.published = dated(100)
        undated = source_url("https://youtu.be/TestGTA0003")
        undated.duration = 60
        before = source_url("https://youtu.be/TestGTA0004")
        before.duration = 60
        before.published = "2026-11-18T20:59:59Z"
        youtube.return_value = [video, old, undated, before]
        report = []
        sources = discover(Mock(), Mock(), report)
        self.assertEqual(len(sources), 10)
        self.assertIn(video.key, sources)
        self.assertEqual(youtube.call_count, 2)
        self.assertIn("YouTube discovery collected 4 video links.", report)
        self.assertIn("Skipped sources before the publication cutoff (2) or with an unknown publication date (1).", report)

    @patch("clipper.core.now", return_value=datetime(2026, 11, 18, 20, 59, 59, tzinfo=UTC))
    @patch("clipper.discovery.twitch_fallback")
    def test_discovery_waits_until_november_19_in_nairobi(self, twitch, clock):
        self.assertEqual(discover(Mock(), Mock(), []), {})
        twitch.assert_not_called()

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch("clipper.editorial.audition", side_effect=QuotaError("quota"))
    @patch("clipper.editorial.scout")
    def test_quota_pause_preserves_the_scouted_moment(self, scout, audition, clock):
        source = source_url("https://www.youtube.com/watch?v=TestGTA0001")
        source.duration = 310
        source.published = dated(1)
        scout.return_value = {"subject": "gameplay", "gta_vi_evidence": "The player controls Lucia in GTA VI.",
                              "sequences": [], "moments": [
            {"start": 15, "end": 45, "theme": "chase", "hook": "Roadblock", "setup": "Escape route",
             "payoff": "Gets away", "evidence": "Police car passes", "potential": 90},
        ]}
        result = build_catalog(Mock(), Mock(), {source.key: source}, {}, [])
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(result["moments"], [])
        self.assertIn(source.key, result["sources"])

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch("clipper.editorial.scout")
    @patch("clipper.editorial.audition")
    def test_scouted_moments_require_a_gta_vi_subject_and_evidence(self, audition, scout, clock):
        source = source_url("https://www.youtube.com/watch?v=TestGTA0001")
        source.duration = 310
        source.published = dated(1)
        for subject, evidence in (("other", "GTA V"), ("gameplay", "")):
            with self.subTest(subject=subject):
                scout.return_value = {"subject": subject, "gta_vi_evidence": evidence, "sequences": [], "moments": [
                    {"start": 13, "end": 32, "theme": "chase", "hook": "Roadblock", "setup": "Pursuit",
                     "payoff": "Escapes", "evidence": "Police car passes", "potential": 95},
                ]}
                result = build_catalog(Mock(), Mock(), {source.key: source}, {}, [])
                self.assertEqual(result["moments"], [])
                self.assertEqual(result["pending"], [])
        audition.assert_not_called()

    @patch("clipper.core.now", return_value=datetime(2026, 12, 7, 12, tzinfo=UTC))
    def test_catalog_drops_expired_and_undated_sources(self, clock):
        old = source_url("https://youtu.be/TestGTA0001")
        old.published = dated(40)
        undated = source_url("https://youtu.be/TestGTA0002")
        saved = source_url("https://youtu.be/TestGTA0003")
        saved.published = dated(15)
        items = (old, undated, saved)
        previous = {
            "sources": {item.key: asdict(item) for item in items},
            "moments": [Moment(Range(item.key, 0, 20), "chase", "Opening", "Setup", "Payoff", "Evidence", 85).to_dict()
                        for item in items],
        }
        result = build_catalog(Mock(), Mock(), {}, previous, [])
        self.assertEqual(set(result["sources"]), {saved.key})
        self.assertEqual([row["span"]["source"] for row in result["moments"]], [saved.key])

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
