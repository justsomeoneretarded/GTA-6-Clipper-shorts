import unittest
from unittest.mock import patch

from clipper.handoff import follow_shorts

ENGINE = "owner/shorts"
EDITOR = "owner/condensed"
PRODUCE = f"/repos/{EDITOR}/actions/workflows/produce.yml"
CATALOG = {"sha": "a" * 40, "commit": {"message": "Publish GTA VI catalog for Shorts run 123 attempt 2"}}


class HandoffTests(unittest.TestCase):
    @patch("clipper.handoff.api")
    def test_dispatch_uses_the_catalog_commit_of_the_finished_attempt(self, api):
        previous = {"sha": "b" * 40, "commit": {"message": "Publish GTA VI catalog for Shorts run 123 attempt 1"}}
        api.side_effect = [
            [CATALOG, previous],
            {"workflow_runs": [{"status": "completed", "display_title": "Shorts batch 123.1"}]},
            {"status": "completed", "conclusion": "success", "path": ".github/workflows/produce.yml"},
            None,
        ]
        follow_shorts(ENGINE, EDITOR, "main", "local-token", "catalog-token")
        self.assertEqual(api.call_count, 4)
        self.assertEqual(api.call_args_list[2].args,
                         (f"/repos/{ENGINE}/actions/runs/123/attempts/2", "catalog-token"))
        api.assert_called_with(f"{PRODUCE}/dispatches", "local-token", payload={
            "ref": "main",
            "inputs": {"shorts_run": "123.2", "catalog_ref": CATALOG["sha"]},
        })

    @patch("clipper.handoff.api")
    def test_editor_waits_while_shorts_is_still_running(self, api):
        api.side_effect = [[CATALOG], {"workflow_runs": []}, {"status": "in_progress"}]
        follow_shorts(ENGINE, EDITOR, "main", "local-token")
        self.assertEqual(api.call_count, 3)

    @patch("clipper.handoff.api")
    def test_started_batches_are_not_dispatched_again(self, api):
        api.side_effect = [[CATALOG], {"workflow_runs": [{"status": "completed", "display_title": "Shorts batch 123.2"}]}]
        follow_shorts(ENGINE, EDITOR, "main", "local-token")
        self.assertEqual(api.call_count, 2)

    @patch("clipper.handoff.api")
    def test_unmarked_catalog_refresh_does_not_start_production(self, api):
        api.return_value = [{"sha": "c" * 40, "commit": {"message": "Refresh GTA VI catalog.json"}}]
        follow_shorts(ENGINE, EDITOR, "main", "local-token")
        api.assert_called_once_with(f"/repos/{ENGINE}/commits?sha=clipper-state&per_page=60", "")
