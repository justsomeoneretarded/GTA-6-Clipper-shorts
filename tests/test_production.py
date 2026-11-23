import tempfile
import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from clipper.core import VERSION, Cut, Edit, Moment, Range, Sequence, source_url
from clipper.editorial import COLD_OPEN
from clipper.plans import single_moment
from clipper.production import MODES, EditorialRejection, approved_package, render_batch


class ProductionEligibilityTests(unittest.TestCase):
    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch("clipper.production.render_edit")
    def test_empty_batch_saves_rejection_and_next_batch_uses_alternate_cut(self, render, clock):
        source = source_url("https://www.youtube.com/watch?v=TestGTA0001")
        source.duration = 500
        source.published = "2026-11-19T01:00:00Z"
        first = Moment(Range(source.key, 10, 30), "comedy", "Opening", "Setup", "Payoff", "Evidence", 92)
        second = Moment(Range(source.key, 15, 32), "comedy", "Opening", "Setup", "Payoff", "Evidence", 90)
        first.alternate_cuts = [second]
        catalog = {"version": VERSION, "sources": {source.key: asdict(source)}, "moments": [first.to_dict()]}
        render.side_effect = EditorialRejection("Opening too slow.")
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            with patch.dict("os.environ", {"MAX_RENDER_ATTEMPTS": "1"}):
                ledger = render_batch("shorts", Mock(), Mock(), catalog, {}, work, work / "first", [])
            self.assertEqual(ledger["edits"], [])
            self.assertEqual(ledger["rejected"][0]["id"], single_moment(first).identity)
            render.reset_mock(side_effect=True)
            render.return_value = {"directory": "short-01"}
            ledger = render_batch("shorts", Mock(), Mock(), catalog, ledger, work, work / "second", [])
        render.assert_called_once()
        self.assertEqual(ledger["edits"][0]["id"], single_moment(second).identity)

    @patch("clipper.core.now", return_value=datetime(2026, 11, 21, 12, tzinfo=UTC))
    @patch("clipper.production.render_edit")
    def test_all_editors_skip_pre_cutoff_sources_from_an_older_catalog(self, render, clock):
        source = source_url("https://www.youtube.com/watch?v=TestGTA0001")
        source.duration = 500
        source.published = "2026-11-18T20:59:59Z"
        moments = [Moment(Range(source.key, start, start + 20), "chase", "Opening",
                          "Setup", "Payoff", "Evidence", 90) for start in (0, 40, 80, 120)]
        sequence = Sequence(Range(source.key, 0, 350), "chase", "A chain of events", 90)
        catalog = {"version": VERSION, "sources": {source.key: asdict(source)},
                   "moments": [moment.to_dict() for moment in moments], "sequences": [sequence.to_dict()]}
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            for mode in MODES:
                with self.subTest(mode=mode):
                    gemini, downloads = Mock(), Mock()
                    result = render_batch(mode, gemini, downloads, catalog, {}, work, work / mode, [])
                    self.assertEqual(result, {})
                    self.assertEqual(gemini.mock_calls, [])
                    self.assertEqual(downloads.mock_calls, [])
        render.assert_not_called()

    @patch("clipper.production.proxy", return_value=Path("fixture-review.mp4"))
    def test_final_review_rejects_an_edit_without_gta_vi_evidence(self, proxy):
        gemini = Mock()
        edit = Edit([Cut(Range("youtube:fixture", 0, 240))], "mission", "Title", "Context")
        response = {"gta_vi": True, "gta_vi_evidence": "Lucia drives through the pictured location.",
                    "passes": True, "reason": "Strong edit", "hook_seconds": 2, "thumbnail_seconds": 5,
                    "engagement_score": 9, "payoff_evidence": "Lucia escapes through an alley.", "payoff_seconds": 220,
                    "titles": ["GTA VI scene"], "description": "A scene from the game."}
        for valid, evidence in ((False, "This shows GTA V"), (True, "")):
            with self.subTest(valid=valid):
                gemini.json.return_value = {**response, "gta_vi": valid, "gta_vi_evidence": evidence}
                with self.assertRaisesRegex(ValueError, "verifiable GTA VI"):
                    approved_package(gemini, edit, Path("fixture.mp4"), 240, [], "condensed", [])
        gemini.json.return_value = response
        result = approved_package(gemini, edit, Path("fixture.mp4"), 240, [], "condensed", [])
        self.assertEqual(result["gta_vi_evidence"], response["gta_vi_evidence"])

    @patch("clipper.production.proxy", return_value=Path("fixture-review.mp4"))
    def test_all_editors_reject_an_ordinary_payoff_even_with_a_hook(self, proxy):
        gemini = Mock()
        response = {"gta_vi": True, "gta_vi_evidence": "Lucia is visible in the scene.",
                    "passes": True, "reason": "Clear footage", "hook_seconds": 1, "thumbnail_seconds": 5,
                    "engagement_score": 5, "payoff_evidence": "Lucia takes a drink for a normal stat gain.",
                    "payoff_seconds": 20, "titles": ["Testing the fridge"], "description": "Lucia takes a drink."}
        opening = {"readable": True, "reason_to_stay": "The viewer can hear the speaker.", "curiosity": 8}
        gemini.json.side_effect = lambda prompt, schema, media: opening if schema is COLD_OPEN else response
        for mode in MODES:
            with self.subTest(mode=mode):
                duration = 30 if mode in {"shorts", "ranked"} else 240
                edit = Edit([Cut(Range("youtube:fixture", 0, duration))], "discovery", "Title", "Context")
                with self.assertRaisesRegex(ValueError, "standout payoff"):
                    approved_package(gemini, edit, Path("fixture.mp4"), duration, [], mode, [])
