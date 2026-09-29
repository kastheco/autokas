#!/usr/bin/env python3
"""Create and configure the kastheco autokas GitHub App."""

from __future__ import annotations

import http.server
import json
import getpass
import secrets
import subprocess
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ORG = "kastheco"
CALLBACK = "http://127.0.0.1:8766/callback"
SECRET = "omp-runner-worker"
PERMISSIONS = {
    "contents": "write",
    "workflows": "write",
    "pull_requests": "write",
    "issues": "write",
}


def request(url: str, *, method: str = "GET", data: dict | None = None, headers: dict | None = None) -> dict:
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/vnd.github+json", **(headers or {})})
    if body:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main() -> None:
    if input("create the autokas GitHub App under kastheco now? [y/N] ").lower() != "y":
        return

    state = secrets.token_urlsafe(24)

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            nonlocal result
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback" or params.get("state", [None])[0] != state:
                self.send_error(400, "invalid callback")
                return
            result = request(
                "https://api.github.com/app-manifests/{}/conversions".format(params["code"][0]),
                method="POST",
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"autokas created. return to the terminal.")

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    callback = f"http://127.0.0.1:{server.server_address[1]}/callback"
    manifest = {
        "name": "autokas",
        "url": "https://github.com/kastheco",
        "hook_attributes": {"url": json.loads(Path("config.json").read_text())["docs_update"]["webhook_url"], "active": False},
        "redirect_url": callback,
        "description": "authenticated GitHub identity for omp-runner",
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }
    encoded = urllib.parse.quote(json.dumps(manifest, separators=(",", ":")), safe="")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"https://github.com/organizations/{ORG}/settings/apps/new?state={state}&manifest={encoded}"
    print("open this GitHub URL if the browser tab is blank:")
    print(url, flush=True)
    webbrowser.open(url)
    while not result:
        __import__("time").sleep(0.2)
    server.shutdown()
    server.server_close()

    if not result.get("id") or not result.get("pem"):
        raise SystemExit("GitHub App creation did not return an App ID and private key")

    app_id = str(result["id"])
    private_key = result["pem"]
    assertion = __import__("jwt").encode(
        {"iat": __import__("time").time() - 60, "exp": __import__("time").time() + 540, "iss": app_id},
        private_key,
        algorithm="RS256",
    )
    print("install autokas on the approved kastheco repositories in the GitHub tab, then return here.")
    input("press Enter after installation: ")
    installations = request(
        "https://api.github.com/app/installations",
        headers={"Authorization": f"Bearer {assertion}"},
    )
    matches = [item for item in installations if item.get("account", {}).get("login", "").lower() == ORG]
    if len(matches) != 1:
        raise SystemExit("autokas is not installed on kastheco")
    installation_id = str(matches[0]["id"])

    print(f"created autokas App {app_id}, installation {installation_id}")
    if input("write credentials to Modal and deploy the cutover now? [y/N] ").lower() != "y":
        print("App created. Add these values to Modal secret omp-runner-worker before deploying:")
        print(f"GITHUB_APP_ID={app_id}\nGITHUB_APP_PRIVATE_KEY=<private>")
        return

    env = {"GITHUB_APP_ID": app_id, "GITHUB_APP_PRIVATE_KEY": private_key, "CLI_PROXY_API_KEY": getpass.getpass("existing CLI proxy key: "), "JARVIS_RUNNER_TOKEN": getpass.getpass("existing Jarvis runner token: ")}
    env_file = Path(".autokas-modal-secret.env")
    try:
        env_file.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
        env_file.chmod(0o600)
        subprocess.run(["modal", "secret", "create", "--force", SECRET, "--from-dotenv", str(env_file)], check=True)
        subprocess.run(["modal", "deploy", "--strategy", "recreate", "runner.py"], check=True)
    finally:
        env_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
