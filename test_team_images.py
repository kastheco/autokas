"""Image evidence checks independent of the Modal runner."""

from contextlib import redirect_stdout
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from images.smoke import inventory, tree_hash


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


class InventoryTests(unittest.TestCase):
    def scan(self, root: Path, *, prohibited: bool = False) -> dict[str, list[str]]:
        walk = os.walk
        output = io.StringIO()
        with patch("images.smoke.os.walk", side_effect=lambda _, **kwargs: walk(root, **kwargs)):
            with redirect_stdout(output):
                if prohibited:
                    with self.assertRaisesRegex(RuntimeError, "prohibited state"):
                        inventory()
                else:
                    inventory()
        return json.loads(output.getvalue())["inventory"]

    def test_gradle_namespaces_allow_dependencies_but_not_nested_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespaces = [
                root / "caches/modules-2/metadata-2.107/descriptors/androidx.sqlite",
                root / "caches/modules-2/files-2.1/androidx.sqlite",
            ]
            for namespace in namespaces:
                dependency = namespace / "sqlite/2.4.0/artifact"
                dependency.mkdir(parents=True)
                (dependency / "sqlite-2.4.0.pom").write_text("<project/>")
            self.assertEqual(self.scan(root), {
                "secrets": [], "auth": [], "sqlite": [], "sockets": [], "clones": []
            })
            for namespace in namespaces:
                with self.subTest(namespace=namespace.relative_to(root)):
                    database = namespace / "state.db"
                    with sqlite3.connect(database) as connection:
                        connection.execute("CREATE TABLE state (value TEXT)")
                    findings = self.scan(root, prohibited=True)
                    self.assertEqual(findings["sqlite"], [str(database)])
                    database.unlink()

    def test_sqlite_suffixes_and_symlinks_remain_prohibited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in (".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm"):
                for kind in ("file", "symlink"):
                    with self.subTest(suffix=suffix, kind=kind):
                        path = root / f"state{suffix}"
                        if kind == "file":
                            path.touch()
                        else:
                            path.symlink_to(root / "missing-directory", target_is_directory=True)
                        findings = self.scan(root, prohibited=True)
                        self.assertEqual(findings["sqlite"], [str(path)])
                        path.unlink()

    def test_namespace_directory_does_not_bypass_other_safety_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            namespace = root / "androidx.sqlite"
            namespace.mkdir()
            for name, category in (("credentials", "secrets"), ("auth.json", "auth"), (".git", "clones")):
                with self.subTest(name=name):
                    path = namespace / name
                    path.mkdir()
                    findings = self.scan(root, prohibited=True)
                    self.assertEqual(findings[category], [str(path)])
                    self.assertEqual(findings["sqlite"], [])
                    path.rmdir()
