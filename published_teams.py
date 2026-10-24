"""Overlay the image digests autokas-teams publishes onto the private team config."""

import base64
import json
import urllib.request

TEAMS_URL = "https://api.github.com/repos/kastheco/autokas-teams/contents/teams.json"


def fetch_published(token: str) -> dict:
    """Read autokas-teams' teams.json, which its image workflow rewrites after every publish."""
    request = urllib.request.Request(TEAMS_URL, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    return json.loads(base64.b64decode(body["content"]))["teams"]


def merge_published(teams: dict, published: dict) -> dict:
    """Take image and repo_images from the published entry; keep every other field from the secret.

    A secret team matches by name, or failing that by a unique modal environment match.
    Published teams the secret doesn't define are ignored, so this never adds a team to a deploy.
    """
    merged = {}
    for name, entry in teams.items():
        source = published.get(name)
        if source is None and entry.get("modal_environment"):
            matches = (team for team in published.values()
                       if team.get("modal_environment") == entry["modal_environment"])
            source = next(matches, None)
            if next(matches, None) is not None:
                source = None
        if source is None:
            merged[name] = entry
            continue
        updated = dict(entry)
        if source.get("image"):
            updated["image"] = source["image"]
        if "repo_images" in source:
            updated["repo_images"] = dict(source["repo_images"])
        merged[name] = updated
    return merged
