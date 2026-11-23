import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import Mock, patch

from clipper.core import (
    GTA_VI_PUBLICATION_CUTOFF, Moment, Range, Sequence, Source, WEIGHTS, dated, duplicate, source_cutoff, source_url,
)
from clipper.discovery import (
    SEARCH_TERMS, discover, grounding_source, listed_twitch_source, tavily_discovery, twitch_search_channels,
)
from clipper.editorial import bounded, build_catalog, merge_catalogs, score_cut, scout_windows, scouting_batches, width_rotation
from clipper.gemini import QuotaError, quota_error
from clipper.media import MediaError
from clipper.plans import condensed_plan, is_new, single_moment


class EditorialTests(unittest.TestCase):
    def test_canonical_urls(self):
        link = source_url("https://youtu.be/TestGTA0001?si=foo")
        self.assertEqual(link.url, "https://www.youtube.com/watch?v=TestGTA0001")
        self.assertEqual(source_url("https://www.youtube.com/shorts/TestGTA0001"), link)
        self.assertIsNone(source_url("https://www.youtube.com.example.org/watch?v=TestGTA0001"))
        self.assertIsNone(source_url("https://["))

    def test_window_ranges_are_relative(self):
        window = Range("youtube:video", 200, 280)
        result = bounded({"start": 10, "end": 25}, window.source, window)
        self.assertEqual((result.start, result.end), (210, 225))
        with self.assertRaises(ValueError):
            bounded({"start": 10, "end": 100}, window.source, window)

    def test_short_scouts_prioritize_viewer_clips_without_losing_either_platform(self):
        youtube = source_url("https://www.youtube.com/watch?v=TestGTA0001")
        replay = source_url("https://www.twitch.tv/videos/1234567890")
        clips = [source_url(f"https://clips.twitch.tv/Fixture{number}") for number in range(6)]
        sources = {item.key: item for item in (youtube, replay, *clips)}
        windows = [Range(item.key, 0, 30) for item in sources.values()]
        batches = scouting_batches(windows, sources, viewer_first=True)
        self.assertEqual([batch[0].source for batch in batches], [youtube.key, clips[0].key, clips[3].key, replay.key])
        self.assertEqual([len(batch) for batch in batches], [1, 3, 3, 1])
        replay.duration = 2000
        self.assertTrue(all(window.seconds == 150 for window in scout_windows(replay, width=150)))

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch("clipper.editorial.audition")
    def test_rejected_short_does_not_hide_an_overlapping_recut(self, audition, clock):
        source = source_url("https://www.youtube.com/watch?v=TestGTA0001")
        source.duration = 120
        source.published = dated(1)
        rejected = Moment(Range(source.key, 20, 40), "comedy", "Hook", "Setup", "Payoff", "Evidence", 92)
        recut = Moment(Range(source.key, 18, 47), "comedy", "Hook", "Setup", "Payoff", "Evidence", 90)
        audition.return_value = recut
        previous = {"sources": {source.key: asdict(source)}, "moments": [rejected.to_dict()],
                    "pending": [recut.to_dict()]}
        catalog = build_catalog(Mock(), Mock(), {}, previous, [], rejected={single_moment(rejected).identity: {}})
        audition.assert_called_once()
        self.assertEqual([row["id"] for row in catalog["moments"]], [recut.identity])

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch("clipper.editorial.scout", return_value={"subject": "other"})
    @patch("clipper.editorial.audition", return_value=None)
    def test_a_viewer_clip_can_get_individual_review_without_bypassing_the_quality_gate(self, audition, scout, clock):
        item = source_url("https://clips.twitch.tv/FixtureReserve")
        item.duration = 30
        item.published = dated(1)
        item.parent = "twitch-vod:1234567890"
        item.parent_offset = 15
        catalog = build_catalog(Mock(), Mock(), {item.key: item}, {}, [])
        audition.assert_called_once()
        self.assertEqual(audition.call_args.args[3].span, Range(item.key, 0, 30))
        self.assertEqual(catalog["moments"], [])

    @patch("clipper.discovery.requests.get")
    def test_gemini_grounding_links_resolve_to_platform_sources(self, get):
        response = get.return_value.__enter__.return_value
        response.is_redirect = True
        response.headers = {"Location": "https://youtu.be/TestGTA0001"}
        link = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/example"
        self.assertEqual(grounding_source(link).key, "youtube:TestGTA0001")
        get.assert_called_once_with(link, allow_redirects=False, stream=True, timeout=(5, 8))

    def test_twitch_clip_listing_uses_game_category(self):
        clip = {"title": "GTA VI now", "url": "https://www.twitch.tv/channel/clip/SomeSlug",
                "createdAt": "2026-11-18T11:30:00Z", "durationSeconds": 30,
                "game": {"name": "Grand Theft Auto VI"}, "videoOffsetSeconds": 101,
                "video": {"id": "1234567890", "publishedAt": "2026-11-18T10:00:00Z"}}
        earliest = "2026-11-18T11:00:00Z"
        found = listed_twitch_source(clip, "channel", clip=True, earliest=earliest)
        self.assertEqual(found.duration, 30)
        self.assertEqual((found.parent, found.parent_offset), ("twitch-vod:1234567890", 101.0))
        self.assertEqual(found.footage_date, "2026-11-18T10:01:41Z")
        clip["game"]["name"] = "Grand Theft Auto V"
        self.assertIsNone(listed_twitch_source(clip, "channel", clip=True, earliest=earliest))

    @patch("clipper.discovery.twitch_query")
    def test_twitch_search_uses_the_shared_terms_and_live_game_category(self, query):
        query.return_value = {"term0": {"channels": {"edges": [
            {"item": {"login": "FixtureOnline", "stream": {"game": {"name": "Grand Theft Auto V"}}}},
            {"item": {"login": "FixtureCreator", "stream": {"game": {"name": "Grand Theft Auto VI"}}}},
        ]}}}
        self.assertEqual(twitch_search_channels(), ["fixturecreator"])
        for term in SEARCH_TERMS:
            self.assertIn(f'userQuery: "{term}"', query.call_args.args[0])

    def test_recording_twitch_replay_uses_a_window_behind_the_live_edge(self):
        row = {"id": "1234567890", "title": "GTA VI first playthrough", "status": "RECORDING",
               "publishedAt": "2026-11-19T11:00:00Z", "lengthSeconds": 600,
               "game": {"name": "Grand Theft Auto VI"}}
        replay = listed_twitch_source(row, "fixturecreator", clip=False, earliest=GTA_VI_PUBLICATION_CUTOFF)
        self.assertTrue(replay.recording)
        self.assertEqual(replay.duration, 540)
        self.assertEqual(scout_windows(replay)[0], Range(replay.key, 120, 540))

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
    @patch("clipper.discovery.youtube_search")
    @patch("clipper.discovery.twitch_public_discovery")
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
        self.assertEqual([call.args[1] for call in youtube.call_args_list], list(SEARCH_TERMS))
        self.assertIn("YouTube discovery collected 4 video links.", report)
        self.assertIn("Skipped sources before the publication cutoff (2) or with an unknown publication date (1).", report)

    @patch("clipper.core.now", return_value=datetime(2026, 11, 18, 20, 59, 59, tzinfo=UTC))
    @patch("clipper.discovery.twitch_public_discovery")
    def test_discovery_waits_until_november_19_in_nairobi(self, twitch, clock):
        self.assertEqual(discover(Mock(), Mock(), []), {})
        twitch.assert_not_called()

    @patch("clipper.core.now", return_value=datetime(2026, 11, 18, 19, tzinfo=UTC))
    @patch.dict("os.environ", {}, clear=True)
    def test_manual_november_18_allowance_moves_the_cutoff_for_the_batch(self, clock):
        self.assertEqual(source_cutoff(7), GTA_VI_PUBLICATION_CUTOFF)
        with patch.dict("os.environ", {"INCLUDE_NOV18": "true"}):
            self.assertEqual(source_cutoff(7), "2026-11-17T21:00:00Z")
            self.assertEqual(source_cutoff(21), "2026-11-17T21:00:00Z")
        self.assertEqual(source_cutoff(7), GTA_VI_PUBLICATION_CUTOFF)

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch.dict("os.environ", {}, clear=True)
    @patch("clipper.editorial.scout_clips")
    @patch("clipper.editorial.scout")
    @patch("clipper.editorial.audition")
    def test_free_catalog_can_supply_ten_shorts_after_rejections(self, audition, scout, scout_clips, clock):
        sources = {}
        for index in range(14):
            url = (f"https://youtu.be/Fixture{index:04d}" if index < 7
                   else f"https://clips.twitch.tv/Fixture{index}")
            source = source_url(url)
            source.duration = 60
            source.published = dated(1)
            sources[source.key] = source
        opening = width_rotation(sources, {})[:10]
        self.assertEqual(sum(sources[span.source].site == "youtube" for span in opening), 5)
        scout.return_value = {"subject": "gameplay", "gta_vi_evidence": "Lucia drives in Vice City.",
                              "sequences": [], "moments": [
            {"start": 15, "end": 40, "theme": "chase", "hook": "Roadblock", "setup": "Police pursuit",
             "payoff": "Gets away", "evidence": "The car clears the roadblock", "potential": 90},
        ]}
        scout_clips.side_effect = lambda model, downloads, sources, windows, report: [
            (window, scout.return_value) for window in windows
        ]
        audition.side_effect = lambda gemini, downloads, source, lead, floor: (
            lead if audition.call_count > 4 else None
        )
        gemini = Mock()
        gemini.json.return_value = {"order": []}
        result = build_catalog(gemini, Mock(), sources, {}, [])
        self.assertEqual(len(result["moments"]), 10)
        self.assertEqual(sum(sources[row["span"]["source"]].site == "youtube" for row in result["moments"]), 5)
        self.assertEqual(result["pending"], [])

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch("clipper.editorial.scout")
    @patch("clipper.editorial.audition")
    def test_condensed_search_adds_long_sequences_to_the_shared_catalog(self, audition, scout, clock):
        clip = source_url("https://clips.twitch.tv/FixtureClip")
        replay = source_url("https://www.twitch.tv/videos/1234567890")
        youtube = source_url("https://youtu.be/TestGTA0001")
        clip.duration, replay.duration, youtube.duration = 30, 400, 400
        clip.published = replay.published = youtube.published = dated(1)
        shared_sequence = {"span": asdict(Range(youtube.key, 0, 320)), "theme": "chase",
                           "story": "Opening chase", "score": 95}
        shared = {"sources": {youtube.key: asdict(youtube)}, "moments": [], "sequences": [shared_sequence]}
        local = {"sources": {}, "moments": [], "sequences": []}
        scout.return_value = {"subject": "gameplay", "gta_vi_evidence": "Lucia drives in Vice City.",
                              "moments": [], "sequences": [
            {"start": 20, "end": 350, "theme": "chase", "story": "Roadblocks and escape", "potential": 90},
        ]}
        result = build_catalog(Mock(), Mock(), {clip.key: clip, replay.key: replay}, merge_catalogs(local, shared),
                               [], mode="condensed")
        self.assertEqual({row["span"]["source"] for row in result["sequences"]}, {youtube.key, replay.key})
        self.assertEqual(scout.call_count, 1)
        self.assertTrue(scout.call_args.kwargs["sequence_only"])
        audition.assert_not_called()

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch.dict("os.environ", {"SCOUT_WINDOWS": "1"}, clear=True)
    @patch("clipper.editorial.scout")
    def test_failed_twitch_fetch_is_replaced_with_another_scouting_window(self, scout, clock):
        sources = {}
        for identifier in ("1234567890", "1234567891"):
            source = source_url(f"https://www.twitch.tv/videos/{identifier}")
            source.duration = 80
            source.published = dated(1)
            sources[source.key] = source
        scout.side_effect = [MediaError("Unreadable section"),
                             {"subject": "other", "gta_vi_evidence": "", "moments": [], "sequences": []}]
        result = build_catalog(Mock(), Mock(), sources, {}, [])
        self.assertEqual(scout.call_count, 2)
        self.assertIn(Range("twitch-vod:1234567891", 0, 80).identity, result["scanned"])

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch.dict("os.environ", {"SCOUT_WINDOWS": "1"}, clear=True)
    @patch("clipper.editorial.window_proxy")
    @patch("clipper.editorial.audition", side_effect=lambda gemini, downloads, source, lead, floor: lead)
    def test_three_twitch_clips_share_one_review_and_keep_their_own_times(self, audition, proxy, clock):
        sources = {}
        for slug in ("FirstFixture", "SecondFixture", "ThirdFixture"):
            source = source_url(f"https://clips.twitch.tv/{slug}")
            source.duration, source.published = 30, dated(1)
            sources[source.key] = source
        gemini = Mock()
        rejected = {"subject": "other", "gta_vi_evidence": "", "moments": [], "sequences": []}
        gemini.json.return_value = {"clips": [
            {**rejected, "clip_id": "3", "subject": "gameplay", "gta_vi_evidence": "Lucia enters the car.", "moments": [
                {"start": 4, "end": 26, "theme": "chase", "hook": "Roadblock", "setup": "Pursuit",
                 "payoff": "Gets away", "evidence": "The car clears the roadblock", "potential": 90},
            ]},
            {**rejected, "clip_id": "1"},
            {**rejected, "clip_id": "2"},
        ]}
        report = []
        result = build_catalog(gemini, Mock(), sources, {}, report)
        self.assertEqual(len(result["scanned"]), 3)
        self.assertEqual([Range(**row["span"]) for row in result["moments"]], [Range("twitch-clip:ThirdFixture", 4, 26)])
        gemini.json.assert_called_once()
        self.assertEqual(len(gemini.json.call_args.args[2]), 3)
        self.assertIn("Excerpts reviewed — YouTube 0, Twitch 3.", report)

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch.dict("os.environ", {"SCOUT_WINDOWS": "2"}, clear=True)
    @patch("clipper.editorial.scout_clips", side_effect=ValueError("Grouped request unsupported"))
    @patch("clipper.editorial.scout", return_value={"subject": "other", "gta_vi_evidence": "", "sequences": [], "moments": []})
    def test_grouped_review_error_falls_back_within_the_same_budget(self, scout, scout_clips, clock):
        sources = {}
        for slug in ("FirstFixture", "SecondFixture", "ThirdFixture"):
            source = source_url(f"https://clips.twitch.tv/{slug}")
            source.duration, source.published = 30, dated(1)
            sources[source.key] = source
        result = build_catalog(Mock(), Mock(), sources, {}, [])
        self.assertEqual(len(result["scanned"]), 1)
        scout_clips.assert_called_once()
        scout.assert_called_once()

    @patch("clipper.plans.window_proxy")
    def test_condensed_plan_retries_timestamps_outside_its_excerpt(self, proxy):
        source = source_url("https://www.twitch.tv/videos/1234567890")
        sequence = Sequence(Range(source.key, 1000, 1260), "mission", "A mission", 90)
        gemini = Mock()
        gemini.json.side_effect = [
            {"cuts": [{"start": 1010, "end": 1260, "beat": "Wrong offset"}]},
            {"cuts": [{"start": 10, "end": 260.6, "beat": "Mission"}], "idea": "Mission", "story": "Ready"},
        ]
        edit = condensed_plan(gemini, Mock(), sequence, source)
        self.assertEqual(gemini.json.call_count, 2)
        self.assertEqual(edit.cuts[0].span, Range(source.key, 1010, 1260))

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch("clipper.editorial.audition", side_effect=QuotaError("quota"))
    @patch("clipper.editorial.scout")
    def test_quota_pause_preserves_the_scouted_moment(self, scout, audition, clock):
        source = source_url("https://www.youtube.com/watch?v=TestGTA0001")
        source.duration = 120
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

    def test_strong_scores_cannot_mask_a_weak_hook_or_payoff(self):
        row = {"scores": dict.fromkeys(WEIGHTS, 9), "confidence": .93, "time_to_hook": 2}
        self.assertEqual(score_cut(row, 70), 90)
        for metric, rating in (("hook", 6), ("payoff", 7)):
            with self.subTest(metric=metric):
                weak = {**row, "scores": {**row["scores"], metric: rating}}
                with self.assertRaises(ValueError):
                    score_cut(weak, 70)

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
