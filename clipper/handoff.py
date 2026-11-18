"""Start a companion workflow with the catalog published by a finished Shorts run."""

from __future__ import annotations

import json
import os
import re
from urllib.request import Request, urlopen

CATALOG_COMMIT = re.compile(
    r"Publish GTA VI catalog for Shorts run ([1-9][0-9]*) attempt ([1-9][0-9]*)( including November 18)?"
)


def api(path: str, token: str, *, payload: dict | None = None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    request = Request(f"https://api.github.com{path}", data=data, headers=headers)
    with urlopen(request, timeout=25) as response:
        body = response.read()
    return json.loads(body) if body else None


def follow_shorts(engine: str, repository: str, branch: str, token: str, catalog_token: str = "") -> None:
    source = f"/repos/{engine}"
    editor = f"/repos/{repository}/actions/workflows/produce.yml"
    commits = api(f"{source}/commits?sha=clipper-state&per_page=60", catalog_token)
    catalogs = []
    for commit in reversed(commits):
        published = CATALOG_COMMIT.fullmatch(commit["commit"]["message"].strip())
        if published:
            catalogs.append((commit["sha"], *published.groups()))
    if not catalogs:
        print("No Shorts run has published a catalog yet.")
        return

    runs = api(f"{editor}/runs?per_page=100", token)["workflow_runs"]
    if any(run["status"] != "completed" for run in runs):
        print("This editor already has a production run queued or running.")
        return
    started = {run["display_title"] for run in runs}
    for revision, run_id, attempt, include_nov18 in catalogs:
        batch = f"{run_id}.{attempt}"
        if f"Shorts batch {batch}" in started:
            continue
        run = api(f"{source}/actions/runs/{run_id}/attempts/{attempt}", catalog_token)
        if (run["status"] != "completed" or run["conclusion"] not in {"success", "failure"}
                or run["path"] != ".github/workflows/produce.yml"):
            continue
        inputs = {"shorts_run": batch, "catalog_ref": revision}
        if include_nov18:
            inputs["include_nov18"] = "true"
        api(f"{editor}/dispatches", token, payload={"ref": branch, "inputs": inputs})
        print(f"Started production from Shorts run {run_id}, attempt {attempt}, catalog {revision}.")
        return
    print("No finished Shorts run has a catalog waiting for this editor.")


if __name__ == "__main__":
    follow_shorts(os.environ["ENGINE"], os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF_NAME"],
                  os.environ["GH_TOKEN"], os.getenv("CATALOG_TOKEN", ""))
