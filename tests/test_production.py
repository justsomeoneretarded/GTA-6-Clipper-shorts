import tempfile
import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from clipper.core import VERSION, Cut, Edit, Moment, Range, Sequence, source_url
from clipper.production import MODES, approved_package, render_batch


class ProductionEligibilityTests(unittest.TestCase):
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
                    "titles": ["GTA VI scene"], "description": "A scene from the game."}
        for valid, evidence in ((False, "This shows GTA V"), (True, "")):
            with self.subTest(valid=valid):
                gemini.json.return_value = {**response, "gta_vi": valid, "gta_vi_evidence": evidence}
                with self.assertRaisesRegex(ValueError, "verifiable GTA VI"):
                    approved_package(gemini, edit, Path("fixture.mp4"), 240, [], "condensed", [])
        gemini.json.return_value = response
        result = approved_package(gemini, edit, Path("fixture.mp4"), 240, [], "condensed", [])
        self.assertEqual(result["gta_vi_evidence"], response["gta_vi_evidence"])
