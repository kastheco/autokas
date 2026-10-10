"""Disposable real-Git fixtures for runner smoke paths that launch omp."""

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock


class GitOmpSmoke:
    """Keep Git real and local, capture omp, and reject other commands."""

    def __init__(self, branch: str) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory
        self._temporary = self.temporary_directory()
        self.root = Path(self._temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.origin = self.checkout
        self.env = {"PATH": os.defpath, "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1"}
        self.real_run = subprocess.run
        self.real_popen = subprocess.Popen
        self.launches: list[dict[str, Any]] = []
        self.on_omp: Callable[..., Any] | None = None
        self.git(["init", "-b", branch])

    def close(self) -> None:
        self._temporary.cleanup()

    def git(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Run fixture setup and observations against the disposable checkout."""
        return self.real_run(["git", *args], cwd=cwd or self.checkout, env=self.env,
                             capture_output=True, text=True, check=True)

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        # Authentication is irrelevant for a disposable local remote.
        if args == ["gh", "auth", "setup-git"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if not args or args[0] != "git":
            raise AssertionError(f"unexpected command: {args}")
        if args[:3] == ["git", "clone", "--no-checkout"]:
            args = [*args[:3], str(self.origin), args[-1]]
        return self.real_run(args, **{**kwargs, "env": self.env})

    def popen(self, args: list[str], **kwargs: Any) -> Any:
        # subprocess.run also calls Popen, so Git must pass through here too.
        if args and args[0] == "git":
            return self.real_popen(args, **{**kwargs, "env": self.env})
        if not args or args[0] != "omp":
            raise AssertionError(f"unexpected command: {args}")
        self.launches.append({"argv": list(args), "cwd": Path(kwargs["cwd"]),
                              "env": dict(kwargs["env"])})
        if self.on_omp:
            return self.on_omp(args, **kwargs)
        return Mock(pid=-1, **{"wait.return_value": 0})
