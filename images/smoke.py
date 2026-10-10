"""Check the baked image before creating any temporary RPC state."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any


def tree_hash(root: Path) -> str:
    """Hash relative names and bytes, including an unambiguous empty tree."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"skill symlink is not permitted: {path}")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def inventory() -> dict[str, list[str]]:
    """Reject credential stores, mutable databases, sockets and cloned repos."""
    findings: dict[str, list[str]] = {
        "secrets": [], "auth": [], "sqlite": [], "sockets": [], "clones": []
    }
    secrets = {
        ".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials",
        "id_rsa", "id_ed25519", "id_ecdsa", "credentials.json",
        "credentials", "token.json", "tokens.json", ".bash_history",
        ".zsh_history", ".python_history", ".sqlite_history",
    }
    auth = {"auth.json", "auth.db", "auth.sqlite", "auth.sqlite3", "hosts.yml"}
    excluded = {"/proc", "/sys", "/dev"}

    def inaccessible(error: OSError) -> None:
        raise RuntimeError(f"cannot inventory baked filesystem: {error}") from error

    for directory, dirs, files in os.walk("/", onerror=inaccessible, followlinks=False):
        dirs[:] = [name for name in dirs if str(Path(directory) / name) not in excluded]
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if name in secrets or name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"}:
                findings["secrets"].append(str(path))
            if name in auth or name in {".ssh", ".aws", ".azure", ".kube"}:
                findings["auth"].append(str(path))
            if path.suffix in {".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm"}:
                findings["sqlite"].append(str(path))
            elif path.suffix == ".db" and stat.S_ISREG(mode):
                with path.open("rb") as handle:
                    if handle.read(16) == b"SQLite format 3\0":
                        findings["sqlite"].append(str(path))
            if stat.S_ISSOCK(mode):
                findings["sockets"].append(str(path))
            if name == ".git":
                findings["clones"].append(str(path))
    print(json.dumps({"inventory": findings}), flush=True)
    if any(findings.values()):
        raise RuntimeError("baked filesystem contains prohibited state")
    return findings


def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """Run a bounded tool check and return its real output."""
    result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    return result.stdout.strip()


async def rpc_check(repo: Path, home: Path, settings: Path) -> dict[str, Any]:
    """Read an idle state response without invoking a model, then close stdin."""
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "CI": "true",
        "PI_CODING_AGENT_DIR": str(home / "agent"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }
    args = ["omp", "--mode", "rpc", "--no-session", "--no-title", "--no-prewalk",
            "--no-extensions", "--no-skills", "--no-rules", "--no-lsp", "--no-tools",
            "--config", str(settings), "--model", "openai/gpt-4.1"]
    process = await asyncio.create_subprocess_exec(
        *args, cwd=repo, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    stderr_task = asyncio.create_task(process.stderr.read())
    state: dict[str, Any] | None = None
    try:
        process.stdin.write(b'{"id":"image-smoke","type":"get_state"}\n')
        await process.stdin.drain()
        async with asyncio.timeout(60):
            while line := await process.stdout.readline():
                message = json.loads(line)
                if message.get("type") == "response" and message.get("id") == "image-smoke":
                    if message.get("command") != "get_state" or message.get("success") is not True:
                        raise RuntimeError(f"get_state failed: {message}")
                    state = message.get("data")
                    if not isinstance(state, dict) or state.get("isStreaming") is not False or state.get("messageCount") != 0:
                        raise RuntimeError(f"unexpected idle state: {state}")
                    break
            if state is None:
                await process.wait()
                stderr = (await stderr_task).decode(errors="replace")
                raise RuntimeError(f"omp ended without a get_state response: {stderr}")
            process.stdin.close()
            await process.stdout.read()
            exit_code = await process.wait()
            stderr = (await stderr_task).decode(errors="replace")
            if exit_code != 0:
                raise RuntimeError(f"omp clean shutdown failed ({exit_code}): {stderr}")
            return {"command": "get_state", "success": True, "isStreaming": False,
                    "messageCount": state["messageCount"], "exit_code": exit_code,
                    "stderr": stderr}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await stderr_task


def main() -> None:
    """Inventory an image and exercise the installed toolchain as nonroot."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc-only", action="store_true", help="check installed omp in a throwaway repo only")
    parser.add_argument("--settings", type=Path, default=Path("/opt/autokas/settings.json"))
    args = parser.parse_args()
    if not args.rpc_only and os.getuid() == 0:
        raise RuntimeError("image smoke must run as nonroot")
    report: dict[str, Any] = {}
    if not args.rpc_only:
        if os.uname().machine != "x86_64":
            raise RuntimeError("public image must be linux-x64")
        report["inventory"] = inventory()
        for directory in (Path("/workspace/repo"), Path("/state")):
            if directory.stat().st_uid != os.getuid() or any(directory.iterdir()):
                raise RuntimeError(f"expected an empty nonroot-owned directory: {directory}")
        report["settings_sha256"] = hashlib.sha256(args.settings.read_bytes()).hexdigest()
        report["skills_sha256"] = tree_hash(Path("/opt/autokas/skills"))
        report["versions"] = {
            name: run(name, "--version") for name in ("git", "gh", "bun", "node", "corepack", "omp")
        }
        for tool in ("curl", "unzip", "cc", "make", "python3"):
            if shutil.which(tool) is None:
                raise RuntimeError(f"missing tool: {tool}")
        help_output = run("omp", "--help")
        if "--mode" not in help_output:
            raise RuntimeError("installed omp does not advertise RPC mode")
        report["omp_help"] = help_output
        print(json.dumps(report), flush=True)
    workspace = None if args.rpc_only else "/workspace/repo"
    with tempfile.TemporaryDirectory(prefix="image-smoke-", dir=workspace) as scratch:
        root = Path(scratch)
        repo, home = root / "repo", root / "home"
        repo.mkdir()
        home.mkdir()
        run("git", "init", "--quiet", str(repo))
        report["rpc"] = asyncio.run(rpc_check(repo, home, args.settings.resolve()))
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
