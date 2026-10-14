"""The machine config: what this machine offers and to which repos.

~/.config/ci-flow/config.json (or $CIFLOW_CONFIG) has two lists:

- "repos": repos whose runners ci-flow manages (`ci-flow apply`).
- "fleets": runners set up some other way, which the dashboards only watch.

Both become "fleets" for the collector: a compose project, a systemd unit
template ({} is the slot number) and the repo each slot serves.
"""
import json
import os
import re

from . import profile as profiles

CONFIG = os.path.expanduser(os.environ.get("CIFLOW_CONFIG", "~/.config/ci-flow/config.json"))
UNIT = "ci-flow-runner@{project}-{{}}.service"
BASE_LABELS = ["self-hosted", "linux", "x64"]

# Limits and labels a repo sets for all its slots, and "slot" overrides per slot.
SLOT_KEYS = {"labels", "memory", "cpus", "node_heap", "dind_memory", "dind_cpus"}
REPO_KEYS = SLOT_KEYS | {"repo", "slots", "project", "slot", "profile"}
DEFAULTS = {"labels": [], "memory": "4g", "cpus": 4, "node_heap": None, "dind_memory": "2g", "dind_cpus": 2}

REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
LABEL_RE = re.compile(r"[A-Za-z0-9_.-]+")
MEMORY_RE = re.compile(r"\d+(\.\d+)?[bkmgBKMG]?")
# Always ciflow-*, so remove --purge can never reach a compose project ci-flow
# did not create.
PROJECT_RE = re.compile(r"ciflow-[a-z0-9][a-z0-9_-]*")


class ConfigError(Exception):
    pass


def load(path=CONFIG):
    try:
        with open(path) as f:
            conf = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {path} ({e}); see config.example.json") from e
    if not isinstance(conf, dict) or not ("repos" in conf or "fleets" in conf):
        raise ConfigError(f"{path} has neither \"repos\" nor \"fleets\"; see config.example.json")
    managed(conf)  # validate early
    return conf


def save(conf, path=CONFIG):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(conf, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def project_of(repo):
    """'acme/web-app' -> 'ciflow-web-app'."""
    return "ciflow-" + re.sub(r"[^a-z0-9]+", "-", repo.split("/")[-1].lower()).strip("-")


def _check(where, key, value):
    if key == "labels":
        if not isinstance(value, list) or not all(isinstance(x, str) and LABEL_RE.fullmatch(x) for x in value):
            raise ConfigError(f"{where}: labels must be a list of names (letters, digits, . _ -)")
    elif key in ("memory", "dind_memory"):
        if not isinstance(value, str) or not MEMORY_RE.fullmatch(value):
            raise ConfigError(f"{where}: {key} must look like \"4g\" or \"3584m\"")
    elif key in ("cpus", "dind_cpus"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"{where}: {key} must be a positive number")
    elif key == "node_heap":
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ConfigError(f"{where}: node_heap must be a positive number of MiB")


def managed(conf):
    """The managed repos, validated and with every slot's settings resolved.

    Returns [{"repo", "project", "profile", "slots": {n: {labels, memory, cpus,
    node_heap, dind_memory, dind_cpus}}}]. node_heap is None when the config
    leaves it to the repo's profile. "profile" is the inline profile, checked
    by the same rules as .github/ci-flow.toml, or None when the repo's file
    (or the defaults) apply.
    """
    out, projects, repos = [], {}, set()
    for i, e in enumerate(conf.get("repos") or []):
        where = f"repos[{i}]"
        if not isinstance(e, dict) or not isinstance(e.get("repo"), str) or not REPO_RE.fullmatch(e["repo"]):
            raise ConfigError(f"{where}: \"repo\" must be \"owner/name\"")
        where = e["repo"]
        if e["repo"].lower() in repos:
            raise ConfigError(f"{where}: listed twice; give it more slots instead")
        repos.add(e["repo"].lower())
        unknown = set(e) - REPO_KEYS
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
        n_slots = e.get("slots", 1)
        if isinstance(n_slots, bool) or not isinstance(n_slots, int) or not 1 <= n_slots <= 32:
            raise ConfigError(f"{where}: slots must be a number from 1 to 32")
        project = e.get("project") or project_of(e["repo"])
        if not PROJECT_RE.fullmatch(project):
            raise ConfigError(f"{where}: project must be \"ciflow-\" then lowercase letters, digits, - and _")
        if project in projects:
            raise ConfigError(f"{where}: project {project} is also used by {projects[project]}; "
                              "set a distinct \"project\"")
        projects[project] = e["repo"]
        base = dict(DEFAULTS)
        for k in SLOT_KEYS & set(e):
            _check(where, k, e[k])
            base[k] = e[k]
        overrides = e.get("slot") or {}
        if not isinstance(overrides, dict):
            raise ConfigError(f"{where}: slot must map slot numbers to settings")
        slots = {n: dict(base) for n in range(1, n_slots + 1)}
        for key, o in overrides.items():
            if not str(key).isdigit() or int(key) not in slots:
                raise ConfigError(f"{where}: slot {key!r} is not one of 1..{n_slots}")
            if not isinstance(o, dict) or set(o) - SLOT_KEYS:
                raise ConfigError(f"{where}: slot {key} may only set {sorted(SLOT_KEYS)}")
            for k, v in o.items():
                _check(f"{where} slot {key}", k, v)
                slots[int(key)][k] = v
        prof = None
        if "profile" in e:
            prof, problems = profiles.from_data(e["profile"], where=f"{where}: profile")
            if problems:
                raise ConfigError("; ".join(problems))
        out.append({"repo": e["repo"], "project": project, "profile": prof, "slots": slots})
    return out


def fleets(conf):
    """Every fleet the dashboards watch: the watch-only ones, then the managed ones."""
    out = []
    for fl in conf.get("fleets") or []:
        out.append({"project": fl["project"], "unit": fl["unit"],
                    "slots": {int(n): repo for n, repo in fl["slots"].items()}})
    for m in managed(conf):
        out.append({"project": m["project"], "unit": UNIT.format(project=m["project"]),
                    "slots": {n: m["repo"] for n in m["slots"]}})
    return out
