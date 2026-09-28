"""A failed cutover must restore every saved hook before reconciliation."""

import copy
import os
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import deploy


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.state = {"hooks": [{"repo": "owner/one", "id": 1}, {"repo": "owner/two", "id": 2}],
                      "started": "2026-09-27T00:00:00Z", "restored": False, "reconciled": []}
        self.persisted = None
        self.active = {1: True, 2: True}
        self.fail_pause = False
        self.fail_restore = False
        self.restore_attempts = []
        self.claims = Mock()
        self.claims.get.side_effect = lambda *_: copy.deepcopy(self.persisted)
        self.claims.put.side_effect = lambda _key, state: setattr(self, "persisted", copy.deepcopy(state))

    def github(self, path, method="GET", data=None):
        hook = int(path.rsplit("/", 1)[1])
        if method == "PATCH":
            self.assertIsNotNone(self.persisted, "the restoration list must precede the first pause")
            active = data["active"]
            if active:
                self.restore_attempts.append(hook)
            if active and self.fail_restore and hook == 1:
                raise RuntimeError("restore failed")
            self.active[hook] = active
            if not active and self.fail_pause and hook == 2:
                raise RuntimeError("pause response lost")
        return {"active": self.active[hook], "config": {"url": deploy.URL}}

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {"DEPLOY_CLEANUP": "0"}))
        stack.enter_context(patch.object(deploy.runner, "CLAIMS", self.claims))
        stack.enter_context(patch.object(deploy, "github", side_effect=self.github))
        stack.enter_context(patch.object(deploy, "discover", side_effect=lambda: copy.deepcopy(self.state)))
        self.drain = stack.enter_context(patch.object(deploy, "drain"))
        self.process = stack.enter_context(patch.object(deploy.subprocess, "run"))
        self.verify = stack.enter_context(patch.object(deploy, "verify"))
        self.reconcile = stack.enter_context(patch.object(deploy, "reconcile_repo"))
        return stack

    def test_failures_at_each_cutover_stage_restore_intake(self):
        for stage in ("pause", "drain", "deploy", "verify"):
            with self.subTest(stage=stage), self.patches():
                self.persisted = None
                self.fail_pause = stage == "pause"
                target = {"drain": self.drain, "deploy": self.process, "verify": self.verify}.get(stage)
                if target is not None:
                    target.side_effect = RuntimeError(stage + " failed")
                with self.assertRaises(RuntimeError):
                    deploy.main()
                self.assertEqual(self.active, {1: True, 2: True})
                self.assertTrue(self.persisted["complete"])
                self.assertEqual(self.persisted["reconciled"], ["owner/one", "owner/two"])
                if stage in {"pause", "drain"}:
                    self.process.assert_not_called()

    def test_restore_failure_still_attempts_other_hooks_and_cleanup_resumes(self):
        with self.patches():
            self.active = {1: False, 2: False}
            self.persisted = copy.deepcopy(self.state)
            self.fail_restore = True
            with self.assertRaisesRegex(RuntimeError, "1 hooks could not be restored"):
                deploy.restore_and_reconcile(copy.deepcopy(self.state))
            self.assertEqual(self.restore_attempts, [1, 2])
            self.assertEqual(self.active, {1: False, 2: True})
            self.assertFalse(self.persisted.get("complete", False))
            self.reconcile.assert_not_called()
            self.fail_restore = False
            with patch.dict(os.environ, {"DEPLOY_CLEANUP": "1"}):
                deploy.main()
            self.assertEqual(self.active, {1: True, 2: True})
            self.assertTrue(self.persisted["complete"])
            self.process.assert_not_called()

    def test_interrupted_reconciliation_keeps_progress_for_cleanup(self):
        with self.patches():
            self.reconcile.side_effect = [None, RuntimeError("GitHub unavailable")]
            with self.assertRaisesRegex(RuntimeError, "GitHub unavailable"):
                deploy.main()
            self.assertEqual(self.active, {1: True, 2: True})
            self.assertEqual(self.persisted["reconciled"], ["owner/one"])
            self.assertFalse(self.persisted.get("complete", False))
            self.reconcile.reset_mock(side_effect=True)
            with patch.dict(os.environ, {"DEPLOY_CLEANUP": "1"}):
                deploy.main()
            self.reconcile.assert_called_once_with("owner/two", self.state["started"])
            self.assertTrue(self.persisted["complete"])


if __name__ == "__main__":
    unittest.main()
