"""Image evidence checks independent of the Modal runner."""

import tempfile
from pathlib import Path
import unittest

from images.smoke import tree_hash


class SkillEvidenceTests(unittest.TestCase):
    def test_hash_tracks_skill_names_and_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "SKILL.md"
            skill.write_text("first skill")
            original = tree_hash(root)
            skill.write_text("changed skill")
            self.assertNotEqual(original, tree_hash(root))
            skill.write_text("first skill")
            skill.rename(root / "OTHER.md")
            self.assertNotEqual(original, tree_hash(root))

    def test_symlink_cannot_hide_external_skill_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "SKILL.md").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(RuntimeError, "skill symlink"):
                tree_hash(root)
