"""A failed cutover must still reconcile every installed repository."""

import copy
import os
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import deploy


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.persisted = None
        self.claims = Mock()
        self.claims.get.side_effect = lambda *_: copy.deepcopy(self.persisted)
        self.claims.put.side_effect = lambda _key, state: setattr(self, "persisted", copy.deepcopy(state))
        self.claims.keys.return_value = []

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {"DEPLOY_CLEANUP": "0"}))
        stack.enter_context(patch.object(deploy.runner, "CLAIMS", self.claims))
        stack.enter_context(patch.object(deploy, "installed_repos", return_value=["owner/one", "owner/two"]))
        self.drain = stack.enter_context(patch.object(deploy, "drain", side_effect=self.drained))
        self.process = stack.enter_context(patch.object(deploy.subprocess, "run"))
        self.verify = stack.enter_context(patch.object(deploy, "verify"))
        self.reconcile = stack.enter_context(patch.object(deploy, "reconcile_repo"))
        return stack

    def drained(self):
        self.assertIsNotNone(self.persisted, "the reconcile window must be saved before the cutover")

    def test_cutover_purges_idle_oauth_entries_without_reading_values(self):
        with self.patches():
            self.claims.keys.return_value = ["linear:oauth:client:org", "linear:state:session", "job-key"]
            deploy.main()
        self.claims.pop.assert_called_once_with("linear:oauth:client:org", None)
        self.assertTrue(self.persisted["complete"])

    def test_failures_at_each_cutover_stage_still_reconcile(self):
        for stage in ("drain", "deploy", "verify"):
            with self.subTest(stage=stage), self.patches():
                self.persisted = None
                target = {"drain": self.drain, "deploy": self.process, "verify": self.verify}[stage]
                target.side_effect = RuntimeError(stage + " failed")
                with self.assertRaisesRegex(RuntimeError, stage + " failed"):
                    deploy.main()
                self.assertTrue(self.persisted["complete"])
                self.assertEqual(self.persisted["reconciled"], ["owner/one", "owner/two"])
                started = self.persisted["started"]
                self.assertEqual([c.args for c in self.reconcile.call_args_list],
                                 [("owner/one", started), ("owner/two", started)])
                if stage == "drain":
                    self.process.assert_not_called()

    def test_interrupted_reconciliation_keeps_progress_for_cleanup(self):
        with self.patches():
            self.reconcile.side_effect = [None, RuntimeError("GitHub unavailable")]
            with self.assertRaisesRegex(RuntimeError, "GitHub unavailable"):
                deploy.main()
            started = self.persisted["started"]
            self.assertEqual(self.persisted["reconciled"], ["owner/one"])
            self.assertFalse(self.persisted.get("complete", False))
            self.reconcile.reset_mock(side_effect=True)
            with patch.dict(os.environ, {"DEPLOY_CLEANUP": "1"}):
                deploy.main()
            self.reconcile.assert_called_once_with("owner/two", started)
            self.assertTrue(self.persisted["complete"])
            self.process.assert_called_once()

    def test_next_deploy_reconciles_from_an_unfinished_window(self):
        with self.patches():
            self.persisted = {"started": "2026-09-27T00:00:00Z", "reconciled": ["owner/one"]}
            deploy.main()
            self.assertEqual([c.args for c in self.reconcile.call_args_list],
                             [("owner/one", "2026-09-27T00:00:00Z"), ("owner/two", "2026-09-27T00:00:00Z")])
            self.assertTrue(self.persisted["complete"])


if __name__ == "__main__":
    unittest.main()
