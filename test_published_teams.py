import unittest

from published_teams import merge_published

OLD = "ghcr.io/kastheco/autokas-teams/kas@sha256:" + "1" * 64
NEW = "ghcr.io/kastheco/autokas-teams/kas@sha256:" + "2" * 64
APP = "ghcr.io/kastheco/autokas-teams/kasthelife@sha256:" + "3" * 64


class MergePublishedTest(unittest.TestCase):
    def test_published_image_and_repo_images_replace_the_secret_digest(self):
        secret = {"kas": {"image": OLD, "modal_environment": "main", "pull_secret": "autokas-ghcr-pull",
                          "worker_secret": "omp-runner-worker"}}
        published = {"kas": {"image": NEW, "modal_environment": "main", "pull_secret": "x", "worker_secret": "y",
                             "repo_images": {"kastheco/kasthelife": APP}}}
        merged = merge_published(secret, published)["kas"]
        self.assertEqual(NEW, merged["image"])
        self.assertEqual({"kastheco/kasthelife": APP}, merged["repo_images"])
        self.assertEqual("autokas-ghcr-pull", merged["pull_secret"])
        self.assertEqual("omp-runner-worker", merged["worker_secret"])

    def test_published_repo_images_replace_or_clear_stale_overrides(self):
        secret = {"kas": {"image": OLD, "repo_images": {"o/stale": OLD, "o/current": OLD}}}
        for overrides in ({"o/current": APP}, {}):
            with self.subTest(overrides=overrides):
                merged = merge_published(secret, {"kas": {"repo_images": overrides}})
                self.assertEqual(overrides, merged["kas"]["repo_images"])
                self.assertEqual(OLD, secret["kas"]["repo_images"]["o/stale"])

    def test_absent_published_repo_images_preserve_secret_overrides(self):
        secret = {"kas": {"image": OLD, "repo_images": {"o/r": APP}}}
        merged = merge_published(secret, {"kas": {"image": NEW}})
        self.assertEqual({"o/r": APP}, merged["kas"]["repo_images"])
        self.assertEqual(NEW, merged["kas"]["image"])

    def test_matches_by_environment_when_names_differ(self):
        merged = merge_published({"prod": {"image": OLD, "modal_environment": "main"}},
                                 {"kas": {"image": NEW, "modal_environment": "main"}})
        self.assertEqual(NEW, merged["prod"]["image"])

    def test_ambiguous_environment_preserves_the_secret_in_either_order(self):
        secret = {"prod": {"image": OLD, "modal_environment": "main",
                           "repo_images": {"o/r": APP}, "pull_secret": "private-pull"}}
        entries = [("kas", {"image": NEW, "modal_environment": "main",
                            "repo_images": {"o/r": NEW}}),
                   ("other", {"image": APP, "modal_environment": "main"})]
        for order in (entries, entries[::-1]):
            with self.subTest(order=[name for name, _ in order]):
                self.assertEqual(secret, merge_published(secret, dict(order)))

    def test_name_match_wins_when_environment_is_ambiguous(self):
        secret = {"kas": {"image": OLD, "modal_environment": "main"}}
        published = {"other": {"image": APP, "modal_environment": "main"},
                     "kas": {"image": NEW, "modal_environment": "main"}}
        self.assertEqual(NEW, merge_published(secret, published)["kas"]["image"])

    def test_unpublished_and_extra_teams_are_left_alone(self):
        secret = {"kas": {"image": OLD, "modal_environment": "main", "repo_images": {"o/r": APP}}}
        merged = merge_published(secret, {"untapped": {"image": NEW, "modal_environment": None}})
        self.assertEqual(secret, merged)


if __name__ == "__main__":
    unittest.main()
