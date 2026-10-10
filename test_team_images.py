"""Environment isolation and immutable team image selection."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from runner import CONFIG, team_config


class TeamImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = copy.deepcopy(CONFIG)
        self.config["teams"] = {
            "first": {"image": "ghcr.io/kastheco/autokas-teams/first@sha256:" + "a" * 64,
                      "modal_environment": "first-env", "pull_secret": "autokas-ghcr-pull",
                      "worker_secret": "autokas-worker"},
            "second": {"image": "ghcr.io/kastheco/autokas-teams/second@sha256:" + "b" * 64,
                       "modal_environment": "second-env", "pull_secret": "autokas-ghcr-pull",
                       "worker_secret": "autokas-worker"},
        }

    def test_environment_never_selects_another_teams_image(self) -> None:
        self.assertEqual(team_config(self.config, "second-env")["image"],
                         "ghcr.io/kastheco/autokas-teams/second@sha256:" + "b" * 64)
        with self.assertRaisesRegex(ValueError, "exactly one team"):
            team_config(self.config, "unknown")
        self.config["teams"]["first"]["modal_environment"] = "second-env"
        with self.assertRaisesRegex(ValueError, "exactly one team"):
            team_config(self.config, "second-env")

    def test_mutable_tag_and_other_registry_are_rejected(self) -> None:
        for image in ("ghcr.io/kastheco/autokas-teams/first:latest",
                      "ghcr.io/another/team@sha256:" + "a" * 64):
            with self.subTest(image=image):
                self.config["teams"]["first"]["image"] = image
                with self.assertRaisesRegex(ValueError, "digest"):
                    team_config(self.config, "first-env")


class RunnerRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.checkout = self.root / "checkout"
        self.runtime = self.root / "runtime"
        self.skills = self.root / "team-skills"
        self.checkout.mkdir()
        self.runtime.mkdir()
        source = Path(__file__).resolve().parent
        for name in ("runner.py", "linear_intake.py", "consult.py", "kas-voice-profile.md"):
            shutil.copyfile(source / name, self.checkout / name)
        shutil.copyfile(source / "config.example.json", self.checkout / "config.json")

    @contextmanager
    def imported_runner(self, local: bool):
        def runtime_path(*parts):
            path = Path(*parts)
            return {Path("/opt/autokas/runner"): self.runtime,
                    Path("/opt/autokas/skills"): self.skills}.get(path, path)

        name = "revision_local" if local else "revision_remote"
        root = self.checkout if local else self.runtime
        spec = importlib.util.spec_from_file_location(name, root / "runner.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {name: module}), \
                patch.dict(os.environ, {"MODAL_ENVIRONMENT": "main"}), \
                patch("modal.is_local", return_value=local), \
                patch("pathlib.Path", side_effect=runtime_path):
            spec.loader.exec_module(module)
            try:
                yield module
            finally:
                if module._CONFIG_DIRECTORY is not None:
                    module._CONFIG_DIRECTORY.cleanup()

    def stage_runtime(self, local) -> None:
        for name in ("runner.py", "linear_intake.py", "consult.py", "kas-voice-profile.md"):
            shutil.copyfile(self.checkout / name, self.runtime / name)
        shutil.copyfile(local.CONFIG_SOURCE, self.runtime / "config.json")

    def test_revision_matches_receiver_and_team_images_with_different_skills(self) -> None:
        local_skills = self.checkout / "skills"
        local_skills.mkdir()
        (local_skills / "SKILL.md").write_text("public checkout skill")
        with self.imported_runner(local=True) as local:
            self.stage_runtime(local)
            with self.imported_runner(local=False) as receiver:
                self.assertEqual(local.REVISION, receiver.REVISION)
            self.skills.mkdir()
            (self.skills / "SKILL.md").write_text("private team skill")
            with self.imported_runner(local=False) as worker:
                self.assertEqual(local.REVISION, worker.REVISION)

    def test_revision_matches_staged_config_without_skills(self) -> None:
        with self.imported_runner(local=True) as local:
            self.stage_runtime(local)
            with self.imported_runner(local=False) as receiver:
                self.assertEqual(local.REVISION, receiver.REVISION)

    def test_revision_tracks_uncommitted_changes_to_every_mounted_source(self) -> None:
        with self.imported_runner(local=True) as local:
            original = local.REVISION
        for name in ("runner.py", "linear_intake.py", "config.json", "consult.py", "kas-voice-profile.md"):
            with self.subTest(name=name):
                path = self.checkout / name
                content = path.read_bytes()
                path.write_bytes(content + b"\n")
                try:
                    with self.imported_runner(local=True) as changed:
                        self.assertNotEqual(original, changed.REVISION)
                finally:
                    path.write_bytes(content)

    def test_team_digest_changes_the_deployed_revision(self) -> None:
        with self.imported_runner(local=True) as local:
            original = local.REVISION
        path = self.checkout / "config.json"
        config = json.loads(path.read_text())
        for team in config["teams"].values():
            if team["modal_environment"] == "main":
                team["image"] = "ghcr.io/kastheco/autokas-teams/test@sha256:" + "c" * 64
        path.write_text(json.dumps(config))
        with self.imported_runner(local=True) as changed:
            self.assertNotEqual(original, changed.REVISION)
