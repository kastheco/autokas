"""One authenticated Jarvis consultation, called by omp through its bash tool."""

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from email.message import Message
from typing import IO


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the consultation bearer to another endpoint."""

    def redirect_request(
        self, req: urllib.request.Request, fp: IO[bytes], code: int,
        msg: str, headers: Message, newurl: str,
    ) -> None:
        return None


def completed_reply(lines: Iterable[bytes]) -> str:
    """Return advice only after the UI message stream finishes successfully."""
    text: list[str] = []
    finished = False
    for line in lines:
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            if not finished or not "".join(text).strip():
                raise ValueError("Jarvis did not provide a completed answer")
            return "".join(text).strip()
        event = json.loads(data)
        kind = event.get("type")
        if kind in {"error", "abort"}:
            raise ValueError("Jarvis interrupted or failed the consultation")
        if kind == "text-delta":
            text.append(event["delta"])
        if kind == "finish":
            if event.get("finishReason") != "stop":
                raise ValueError("Jarvis did not complete normally")
            finished = True
    raise ValueError("Jarvis stream ended before completion")


def main() -> int:
    """Read the question from stdin without exposing credentials in arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-id", required=True, type=uuid.UUID)
    args = parser.parse_args()
    repo = os.environ.get("OMP_JOB_REPO", "").split("/")
    if len(repo) != 2 or repo[0].lower() != "example-org" or not repo[1]:
        print("blocked: Jarvis is only available for example-org repositories; "
              "report business-logic changes to the repository owner", file=sys.stderr)
        return 1
    raw = sys.stdin.read(12_001)
    message = raw.strip()
    if not message or len(raw) > 12_000:
        parser.error("question must contain 1 to 12000 characters")
    token = os.environ.get("JARVIS_RUNNER_TOKEN")
    url = os.environ.get("JARVIS_CONSULT_URL", "")
    if not token or not url.startswith("https://"):
        print("blocked: Jarvis consultation is not configured", file=sys.stderr)
        return 1
    payload = json.dumps({"requestId": str(args.request_id), "message": message}).encode()
    request = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "text/event-stream",
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=180) as response:
            if response.headers.get_content_type() != "text/event-stream":
                raise ValueError("Jarvis returned a non-stream response")
            answer = completed_reply(response)
    except urllib.error.HTTPError as error:
        print(f"blocked: Jarvis returned HTTP {error.code}; no automatic retry", file=sys.stderr)
        return 1
    except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError):
        print("blocked: Jarvis response failed or was incomplete; no automatic retry", file=sys.stderr)
        return 1
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="jarvis-advice-", suffix=".txt", delete=False,
        ) as advice:
            advice.write(answer.replace(token, "[redacted]"))
    except OSError:
        print("blocked: Jarvis completed but advice could not be saved; no automatic retry", file=sys.stderr)
        return 1
    print(json.dumps({"requestId": str(args.request_id), "advice_file": advice.name}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
