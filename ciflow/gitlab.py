"""GitLab: the API calls ci-flow makes for a repo whose forge is "gitlab".

GitHub repos go through gh; GitLab ones through the REST API (v4) with a
personal, group or project access token in GITLAB_TOKEN, read from the
environment or from ~/.config/ci-flow/env (the file the units load). The token
needs the create_runner and read_api scopes.

A runner slot registers a fresh project runner per job (POST /user/runners),
runs one job with `gitlab-runner run-single --max-builds 1`, and deletes the
runner with its own token afterwards. Pipelines and jobs are mapped onto the
GitHub run and job shapes the collector already keeps, so the dashboards need
no GitLab knowledge.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://gitlab.com"
BASE_IMAGE = "gitlab/gitlab-runner:latest"
DEFAULT_JOB_IMAGE = "ubuntu:24.04"
ENV_FILE = os.path.expanduser("~/.config/ci-flow/env")
URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~-]+)*")
# Docker image references: registry/name:tag or @sha256 digests, nothing else.
IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/:@-]*")


def token(env_file=ENV_FILE):
    """GITLAB_TOKEN from the environment, else from the ci-flow env file."""
    if os.environ.get("GITLAB_TOKEN"):
        return os.environ["GITLAB_TOKEN"]
    try:
        with open(env_file) as f:
            for line in f:
                key, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and key.strip() == "GITLAB_TOKEN":
                    return value.strip().strip("'\"") or None
    except OSError:
        pass
    return None


def api(base, path, method="GET", data=None, tok=None, timeout=20):
    """Call the GitLab REST API. Returns parsed JSON ({} for an empty reply),
    or None on failure. `data` is sent form-encoded."""
    tok = tok if tok is not None else token()
    headers = {"Accept": "application/json"}
    if tok:
        headers["PRIVATE-TOKEN"] = tok
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(f"{base.rstrip('/')}/api/v4/{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
    except (OSError, urllib.error.URLError, ValueError):
        return None
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        return None


def pid(repo):
    """'group/sub/project' -> 'group%2Fsub%2Fproject', the API's project id."""
    return urllib.parse.quote(repo, safe="")


class GitLab:
    """What apply, add and init need from GitLab. Tests swap in a fake."""

    def __init__(self, url=DEFAULT_URL):
        self.url = url

    def repo(self, repo):
        """The project, normalised to the keys ci-flow reads from GitHub's."""
        p = api(self.url, f"projects/{pid(repo)}")
        if not isinstance(p, dict) or "id" not in p:
            return None
        return {"id": p["id"], "private": p.get("visibility") != "public",
                "visibility": p.get("visibility"), "default_branch": p.get("default_branch")}


def security_warnings(repo, info):
    if info is None:
        return [f"{repo}: could not read the project from GitLab; is it spelled right and is "
                "GITLAB_TOKEN set (in ~/.config/ci-flow/env)?"]
    if info.get("visibility") == "public":
        return [f"{repo} is PUBLIC. Merge requests from forks run their pipelines in the fork, "
                "away from these runners, but a maintainer who runs one in the parent project "
                "runs its code on this machine. Read the fork's changes first."]
    return []


# ── Runners ──────────────────────────────────────────────────────────────────
def register(base, repo, project_id, name, tags):
    """Create a single-use project runner. Returns {"id", "token"} or None."""
    r = api(base, "user/runners", method="POST",
            data={"runner_type": "project_type", "project_id": project_id, "description": name,
                  "tag_list": ",".join(tags), "run_untagged": "false"})
    if not isinstance(r, dict) or not r.get("token"):
        return None
    return {"id": r.get("id"), "token": r["token"]}


def deregister(base, runner_token):
    """Delete a runner with its own token; no admin rights needed. A runner
    that is already gone counts as removed."""
    return api(base, "runners", method="DELETE", data={"token": runner_token}, tok="") is not None \
        or api(base, "runners/verify", method="POST", data={"token": runner_token}, tok="") is None


# ── Pipelines, as the collector's runs and jobs ──────────────────────────────
STATUS = {
    "running": ("in_progress", None),
    "success": ("completed", "success"),
    "failed": ("completed", "failure"),
    "canceled": ("completed", "cancelled"),
    "canceling": ("in_progress", None),
    "skipped": ("completed", "skipped"),
    # Waiting for someone to press play: nothing will run on its own.
    "manual": ("completed", "skipped"),
}  # created, waiting_for_resource, preparing, pending, scheduled: queued


def status_of(s):
    return STATUS.get(s, ("queued", None))


def branch_of(ref):
    """'refs/merge-requests/12/head' -> 'mr-12'; branch names stay as they are."""
    m = re.fullmatch(r"refs/merge-requests/(\d+)/\w+", ref or "")
    return f"mr-{m.group(1)}" if m else ref or ""


def run_of(p, title=None):
    status, conclusion = status_of(p.get("status"))
    return {
        "databaseId": p["id"],
        "workflowName": p.get("name") or "pipeline",
        "displayTitle": title or p.get("ref") or "",
        "headBranch": branch_of(p.get("ref")),
        "status": status,
        "conclusion": conclusion or "",
        "createdAt": p.get("created_at"),
        "updatedAt": p.get("updated_at") or p.get("created_at"),
        "event": p.get("source"),
        "url": p.get("web_url"),
    }


def job_of(j):
    status, conclusion = status_of(j.get("status"))
    return {
        "name": j.get("name"),
        "status": status,
        "conclusion": conclusion,
        "started_at": j.get("started_at"),
        "runner_name": (j.get("runner") or {}).get("description") or "",
        "html_url": j.get("web_url"),
        "steps": [],
    }


def runs(base, repo, titles, limit=15):
    """The project's latest pipelines as runs, or None when GitLab can't be
    read. `titles` caches commit titles by sha across calls."""
    ps = api(base, f"projects/{pid(repo)}/pipelines?per_page={limit}")
    if not isinstance(ps, list):
        return None
    out = []
    for p in ps:
        sha = p.get("sha")
        if sha and sha not in titles:
            c = api(base, f"projects/{pid(repo)}/repository/commits/{sha}")
            if isinstance(c, dict) and c.get("title"):
                titles[sha] = c["title"]
        out.append(run_of(p, titles.get(sha)))
    return out


def jobs(base, repo, pipeline_id):
    js = api(base, f"projects/{pid(repo)}/pipelines/{pipeline_id}/jobs?per_page=100")
    return [job_of(j) for j in js] if isinstance(js, list) else None
