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

    def test_matches_by_environment_when_names_differ(self):
        merged = merge_published({"prod": {"image": OLD, "modal_environment": "main"}},
                                 {"kas": {"image": NEW, "modal_environment": "main"}})
        self.assertEqual(NEW, merged["prod"]["image"])

    def test_unpublished_and_extra_teams_are_left_alone(self):
        secret = {"kas": {"image": OLD, "modal_environment": "main", "repo_images": {"o/r": APP}}}
        merged = merge_published(secret, {"untapped": {"image": NEW, "modal_environment": None}})
        self.assertEqual(secret, merged)


if __name__ == "__main__":
    unittest.main()
