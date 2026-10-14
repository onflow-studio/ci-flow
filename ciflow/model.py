"""What the dashboards show, derived from a collector snapshot.

The rules live here once so the terminal and the web dashboard agree: which
runner slot is busy, which runs are running or waiting, the health badge and
the recent results. derive() returns plain JSON-serialisable data; timestamps
are epoch seconds and the views turn them into elapsed times.
"""
import re
from datetime import datetime

RECENT = 8  # completed runs shown per repo
UP = ("active", "activating", "reloading")
NOT_STARTED = ("queued", "waiting", "pending")


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def short(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def repo_name(repo):
    return repo.split("/")[-1]


def ticket(branch):
    """'feat/abc-123-add-login' -> 'ABC-123', 'fix/45-typo-in-footer' -> '#45'."""
    tail = branch.split("/")[-1]
    m = re.match(r"([a-z]+-\d+)", tail)
    if m:
        return m.group(1).upper()
    m = re.match(r"(\d+)-", tail)
    return f"#{m.group(1)}" if m else short(tail, 9)


def job_label(name):
    return short(re.sub(r"\s*(Tests?|Scan)$", "", name), 20)


def slot_of(host, runner_name):
    """The slot number of a runner registered from `host`, as a label:
    'ci-box-web-app-2-1791986499' (project in the name) or the older
    'ci-box-2-1791986499' -> '2'."""
    m = re.fullmatch(rf"{re.escape(host)}-(?:[a-z0-9_-]+-)?(\d+)-\d+", runner_name or "")
    return m and m.group(1)


def job_order(j):
    """Pipeline order: finished and running jobs by start time, queued ones last."""
    if j["status"] in ("completed", "in_progress") and j.get("started_at"):
        return 0, j["started_at"]
    return 1, j.get("name", "")


def derive(snap):
    hostname = snap["hostname"]
    repos = snap["repos"]
    units = snap["units"]
    slots_up = set(snap["slots_up"])
    runs = snap["runs"]
    jobs = snap["jobs"]
    gh_err = snap["gh_err"]
    hs = snap["host"]

    def jobs_of(r):
        return jobs.get(str(r["databaseId"]), [])

    def job_view(j):
        steps = j.get("steps") or []
        return {
            "name": j["name"],
            "label": job_label(j["name"]),
            "status": j["status"],
            "conclusion": j.get("conclusion"),
            "started_at": parse_iso(j.get("started_at")),
            "slot": slot_of(hostname, j.get("runner_name")),
            "steps_done": sum(s["status"] == "completed" for s in steps),
            "steps_total": len(steps),
            "url": j.get("html_url"),
        }

    def run_view(repo, r, status):
        js = sorted(jobs_of(r), key=job_order)
        done = sum(j["status"] == "completed" for j in js)
        return {
            "repo": repo,
            "name": repo_name(repo),
            "id": r["databaseId"],
            "workflow": r["workflowName"],
            "branch": r["headBranch"],
            "ticket": ticket(r["headBranch"]),
            "title": r["displayTitle"],
            "status": status,
            "conclusion": r.get("conclusion"),
            "created_at": parse_iso(r["createdAt"]),
            "updated_at": parse_iso(r["updatedAt"]),
            "url": r.get("url"),
            "done": done,
            "total": len(js),
            "pct": done / len(js) * 100 if js else 0,
            "jobs": [job_view(j) for j in js],
        }

    # What each runner slot is running right now, from GitHub's job assignments.
    busy = {}  # (repo, slot) -> job
    for repo, rs in runs.items():
        for r in rs:
            for j in jobs_of(r):
                s = slot_of(hostname, j.get("runner_name"))
                if j["status"] == "in_progress" and s:
                    busy[(repo, int(s))] = j

    fleets = [{**f, "slots": {int(n): r for n, r in f["slots"].items()}} for f in snap["fleets"]]
    slots = []
    for repo in repos:
        row = []
        for f in fleets:
            for n, r in f["slots"].items():
                if r != repo:
                    continue
                u = units.get(f"{f['project']}/{n}")
                slot = {"n": n, "project": f["project"], "unit": u}
                if (repo, n) in busy:
                    j = busy[(repo, n)]
                    slot.update(state="busy", job=j["name"], started_at=parse_iso(j.get("started_at")),
                                url=j.get("html_url"))
                elif u not in UP:
                    slot["state"] = "down"
                elif f"{f['project']}/{n}" in slots_up:
                    slot["state"] = "idle"
                else:
                    slot["state"] = "starting"
                row.append(slot)
        slots.append({"repo": repo, "name": repo_name(repo), "slots": row})

    # Machine numbers and the health badge: the worst thing worth looking at, first.
    mem_pct = hs["mem"]["used"] / hs["mem"]["total"] * 100
    disk_pct = hs["disk"]["used"] / hs["disk"]["total"] * 100
    load_per_core = hs["load"][0] / hs["ncpu"]
    t = hs["temp"]
    issues = []
    down = [f"{repo_name(f['slots'][n])}#{n}" for f in fleets for n in f["slots"]
            if units.get(f"{f['project']}/{n}") not in UP]
    if down:
        issues.append({"level": "crit", "text": f"runner down: {', '.join(down)}"})
    if not snap["docker_ok"]:
        issues.append({"level": "crit", "text": "docker unreachable"})
    for pct, warn, crit, what in ((disk_pct, 80, 90, "disk"), (mem_pct, 85, 95, "memory")):
        if pct >= warn:
            issues.append({"level": "crit" if pct >= crit else "warn", "text": f"{what} {pct:.0f}%"})
    if t and t >= 85:
        issues.append({"level": "crit" if t >= 95 else "warn", "text": f"hot {t:.0f}°C"})
    if hs["ac"] is False:
        issues.append({"level": "crit", "text": "on battery"})
    if gh_err:
        issues.append({"level": "warn", "text": f"gh: {', '.join(repo_name(r) for r in gh_err)}"})
    level = "ok" if not issues else "crit" if any(i["level"] == "crit" for i in issues) else "warn"

    # Pipelines: GitHub keeps a run "queued" while some of its jobs already run.
    live = [(repo, r) for repo in repos for r in runs.get(repo, []) if r["status"] != "completed"]
    live.sort(key=lambda x: x[1]["createdAt"])
    running, queue = {}, []
    for repo, r in live:
        if r["status"] == "in_progress" or any(j["status"] == "in_progress" for j in jobs_of(r)):
            running.setdefault(repo, []).append(run_view(repo, r, "in_progress"))
        else:
            queue.append(run_view(repo, r, r["status"]))
    running = [{"repo": repo, "name": repo_name(repo), "runs": running[repo]} for repo in repos if repo in running]

    recent = []
    for repo in repos:
        rs = [run_view(repo, r, "completed") for r in runs.get(repo, []) if r["status"] == "completed"]
        fail = next((r for r in rs if r["conclusion"] == "failure"), None)
        recent.append({
            "repo": repo,
            "name": repo_name(repo),
            "error": gh_err.get(repo),
            "loading": repo not in runs,
            "runs": rs[:RECENT],
            "last": rs[0] if rs else None,
            "last_fail": fail if fail and rs and fail is not rs[0] else None,
        })

    return {
        "health": {"level": level, "issues": issues},
        "machine": {"mem_pct": mem_pct, "disk_pct": disk_pct, "load_per_core": load_per_core},
        "counts": {
            "busy": len(busy),
            "slots": sum(len(f["slots"]) for f in fleets),
            "jobs_not_started": sum(j["status"] in NOT_STARTED for _, r in live for j in jobs_of(r)),
            "runs_queued": len(queue),
        },
        "slots": slots,
        "running": running,
        "queue": queue,
        "recent": recent,
        "recent_limit": RECENT,
    }
