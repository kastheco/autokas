"""Environment isolation and immutable team image selection."""

import builtins
from types import SimpleNamespace
import copy
import importlib.util
import io
import json
import os
from pathlib import PosixPath as Path
import shutil
import sys
import subprocess
import tarfile
import time
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch
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


    def test_repository_images_require_immutable_team_digests(self) -> None:
        for image in ("ghcr.io/kastheco/autokas-teams/klippy:latest",
                      "ghcr.io/kastheco/autokas-teams/klippy@sha256:" + "a" * 63,
                      "ghcr.io/other/klippy@sha256:" + "a" * 64, None):
            with self.subTest(image=image):
                self.config["teams"]["first"]["repo_images"] = {"kastheco/klippy": image}
                with self.assertRaisesRegex(ValueError, "Repository image.*digest"):
                    team_config(self.config, "first-env")

    def test_repository_image_keys_are_unambiguous(self) -> None:
        for overrides in ([], {"klippy": self.config["teams"]["first"]["image"]},
                          {"Kastheco/klippy": self.config["teams"]["first"]["image"]}):
            with self.subTest(overrides=overrides):
                self.config["teams"]["first"]["repo_images"] = overrides
                with self.assertRaisesRegex(ValueError, "repo_images"):
                    team_config(self.config, "first-env")

class RunnerRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.checkout = self.root / "checkout"
        self.runtime = self.root / "runtime"
        self.skills = self.root / "team-skills"
        self.settings = self.root / "settings.json"
        self.workspace = self.root / "workspace"
        self.state = self.root / "state"
        self.workspace.mkdir()
        self.state.mkdir()
        self.settings.write_text(json.dumps({"teamOnly": True, "retry": {"enabled": True}}))
        self.checkout.mkdir()
        self.runtime.mkdir()
        source = Path(__file__).resolve().parent
        for name in ("runner.py", "linear_intake.py", "consult.py", "kas-voice-profile.md"):
            shutil.copyfile(source / name, self.checkout / name)
        shutil.copyfile(source / "config.example.json", self.checkout / "config.json")

    @contextmanager
    def imported_runner(self, local: bool, environment: str | None = "main"):
        def runtime_path(*parts):
            path = Path(*parts)
            return {Path("/opt/autokas/runner"): self.runtime,
                    Path("/opt/autokas/skills"): self.skills,
                    Path("/opt/autokas/settings.json"): self.settings,
                    Path("/workspace/repo"): self.workspace}.get(path, path)

        name = "revision_local" if local else "revision_remote"
        root = self.checkout if local else self.runtime
        spec = importlib.util.spec_from_file_location(name, root / "runner.py")
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__

        def runtime_import(import_name, globals=None, locals=None, fromlist=(), level=0):
            if import_name == "pathlib" and globals and globals.get("__name__") == name:
                return SimpleNamespace(Path=runtime_path)
            return original_import(import_name, globals, locals, fromlist, level)

        with patch.dict(sys.modules, {name: module}), \
                patch.dict(os.environ, {"MODAL_ENVIRONMENT": environment or ""}), \
                patch("modal.is_local", return_value=local), \
                patch("builtins.__import__", side_effect=runtime_import):
            if environment is None:
                os.environ.pop("MODAL_ENVIRONMENT")
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

    def test_container_startup_imports_mounted_modules_without_checkout(self) -> None:
        import runner

        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

        class MountedImage:
            def env(self, values):
                environment.update(values)
                return self

            def add_local_file(image, source, destination):
                shutil.copyfile(source, self.runtime / Path(destination).name)
                return image

        runner.with_runner_files(MountedImage())
        entry = self.root / "modal-entry"
        entry.mkdir()
        shutil.copyfile(self.runtime / "runner.py", entry / "runner.py")
        # Modal loads the entry module separately from the explicitly mounted files.
        script = f"""
import json
import builtins
from types import SimpleNamespace
from pathlib import PosixPath as Path
from unittest.mock import patch
import modal
runtime = Path({str(self.runtime)!r})
def runtime_path(*parts):
    path = Path(*parts)
    return runtime if path == Path('/opt/autokas/runner') else path
original_import = builtins.__import__
def runtime_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == 'pathlib' and globals and globals.get('__name__') == 'runner':
        return SimpleNamespace(Path=runtime_path)
    return original_import(name, globals, locals, fromlist, level)
with patch('modal.is_local', return_value=False), patch('builtins.__import__', side_effect=runtime_import):
    import runner
    print(json.dumps({{'revision': runner.REVISION, 'intake': runner.linear_intake.__file__}}))
"""
        # Redirect only the mount root, keeping Python's real import search behavior.
        environment["PYTHONPATH"] = environment.get("PYTHONPATH", "").replace(
            "/opt/autokas/runner", str(self.runtime))
        result = subprocess.run([sys.executable, "-c", script], cwd=entry,
                                env=environment, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        startup = json.loads(result.stdout)
        self.assertEqual(startup["revision"], runner.REVISION)
        self.assertEqual(Path(startup["intake"]), self.runtime / "linear_intake.py")


    def configure_overrides(self, image=None) -> str:
        digest = image or "ghcr.io/kastheco/autokas-teams/klippy@sha256:" + "c" * 64
        path = self.checkout / "config.json"
        config = json.loads(path.read_text())
        config["allowed_owners"] = ["kastheco"]
        config["teams"]["example"]["repo_images"] = {
            "kastheco/klippy": digest, "kastheco/another-android-app": digest,
        }
        path.write_text(json.dumps(config))
        return digest

    def test_override_routes_coding_jobs_and_keeps_other_repos_on_team_image(self) -> None:
        import modal

        digest = self.configure_overrides()
        with patch("modal.Image.from_registry", wraps=modal.Image.from_registry) as registry, \
                self.imported_runner(local=True) as module:
            self.assertEqual([call.args[0] for call in registry.call_args_list],
                             [module.TEAM["image"], digest])
            override = module.REPO_WORKERS["kastheco/klippy"]
            self.assertIs(override, module.REPO_WORKERS["kastheco/another-android-app"])
            override_image = override._get_class_service_function().spec.image
            self.assertIsNot(override_image, module.IMAGE)
            self.assertIs(module.REPO_PLANNERS["kastheco/klippy"].spec.image, override_image)
            for repo, selected in (("Kastheco/Klippy", override), ("kastheco/autokas", module.PRWorker)):
                with self.subTest(repo=repo), patch("modal.Cls.__call__", autospec=True, return_value=Mock()) as pool, \
                        patch.object(module, "github", return_value={"permission": "write"}), \
                        patch.object(module, "acknowledge_review"), patch.object(module, "log"), \
                        patch("modal.current_function_call_id", return_value=None):
                    job = {"repo": repo, "pr": 1, "key": repo + ":command", "author": "kas",
                           "mode": "command"}
                    module.worker.local(job)
                    pool.assert_called_once_with(selected, pr_key=repo.lower() + "#1")
                    pool.return_value.run.spawn.assert_called_once_with(job)

    def test_override_starts_from_mounted_runtime_without_checkout(self) -> None:
        self.configure_overrides()
        with self.imported_runner(local=True) as local:
            self.stage_runtime(local)
            with self.imported_runner(local=False) as remote:
                self.assertEqual(remote.REVISION, local.REVISION)
                self.assertIn("kastheco/klippy", remote.REPO_WORKERS)
                self.assertIn("kastheco/klippy", remote.REPO_PLANNERS)
                self.assertEqual(remote.image_settings()["teamOnly"], True)
                with patch.dict(remote.CONFIG, allowed_owners=[]), \
                        patch.object(remote, "github_token") as token:
                    remote.REPO_WORKERS["kastheco/klippy"](pr_key="kastheco/klippy#1").run.local(
                        {"repo": "kastheco/klippy"})
                    token.assert_not_called()

    def test_malformed_override_fails_during_runner_import(self) -> None:
        self.configure_overrides("ghcr.io/kastheco/autokas-teams/klippy@sha256:bad")
        with self.assertRaisesRegex(ValueError, "Repository image.*digest"):
            with self.imported_runner(local=True):
                pass

    def test_missing_environment_cannot_select_main(self) -> None:
        for environment in (None, ""):
            with self.subTest(environment=environment), \
                    self.assertRaisesRegex(ValueError, "MODAL_ENVIRONMENT"):
                with self.imported_runner(local=True, environment=environment):
                    pass

    def test_deployed_settings_keep_team_defaults_and_apply_config(self) -> None:
        with self.imported_runner(local=True) as local:
            self.stage_runtime(local)
            with self.imported_runner(local=False) as worker:
                self.assertEqual(worker.image_settings(), {
                    "teamOnly": True, "retry": {"enabled": False, "modelFallback": False},
                })

    def test_planner_removes_checkout_and_never_reads_previous_jobs_files(self) -> None:
        with self.imported_runner(local=True) as local:
            self.stage_runtime(local)
        temporary_directory = tempfile.TemporaryDirectory
        checkouts = []

        def job_directory(**kwargs):
            return temporary_directory(prefix=kwargs["prefix"], dir=self.state)

        def plan(args, worktree, env, *args_rest, **kwargs):
            checkouts.append(worktree)
            self.assertEqual({path.name for path in worktree.iterdir()}, {"current.txt"})
            self.assertEqual((worktree / "current.txt").read_text(), "current job")
            (worktree / "stale.txt").write_text("previous job")
            if fail:
                raise RuntimeError("planner failed")
            return 0, '["inspect the current job"]'

        checkout = io.BytesIO()
        with tarfile.open(fileobj=checkout, mode="w") as archive:
            content = b"current job"
            entry = tarfile.TarInfo("current.txt")
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        with self.imported_runner(local=False) as worker, \
                patch("runner.tempfile.TemporaryDirectory", side_effect=job_directory), \
                patch.dict(os.environ, BUN_INSTALL="/unused"), \
                patch.object(worker, "linear_rpc", side_effect=plan):
            for fail in (False, True, False):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, "planner failed"):
                        worker.linear_planner.local(
                            {"prompt": "plan"}, checkout.getvalue(), "synthetic-proxy-key", time.time() + 60,
                        )
                else:
                    self.assertEqual(worker.linear_planner.local(
                        {"prompt": "plan"}, checkout.getvalue(), "synthetic-proxy-key", time.time() + 60,
                    ), (0, '["inspect the current job"]'))
                self.assertFalse(checkouts[-1].exists(), "checkout survived job cleanup")

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
