"""Environment isolation and immutable team image selection."""

import copy
import unittest
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
