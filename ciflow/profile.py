"""Repo profiles: what a repo's jobs need in the runner image.

A profile comes from one of two places: inline in the machine config
(`"profile"` on a managed repo), or `.github/ci-flow.toml` on the repo's
default branch. Both are checked by the same rules here. Package, tool and
cache names are checked against fixed patterns and lists, so a profile can't
add arbitrary lines to the generated Dockerfile.
"""
import re
import tomllib

PROFILE_PATH = ".github/ci-flow.toml"

# Caches a profile may keep between jobs, shared by the repo's slots.
CACHES = {
    "npm": "/home/runner/.npm",
    "pnpm": "/home/runner/.local/share/pnpm/store",
    "yarn": "/home/runner/.cache/yarn",
    "playwright": "/home/runner/.cache/ms-playwright",
    "pip": "/home/runner/.cache/pip",
    "uv": "/home/runner/.cache/uv",
    "cypress": "/home/runner/.cache/Cypress",
}
# Extra tools a profile may ask for, as Dockerfile steps run as root.
TOOLS = {
    "docker-compose": (
        "# The image's docker CLI has no compose plugin.\n"
        "RUN mkdir -p /usr/local/lib/docker/cli-plugins \\\n"
        " && curl -fsSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \\\n"
        "      -o /usr/local/lib/docker/cli-plugins/docker-compose \\\n"
        " && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose\n"
    ),
}
APT_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*")
PROFILE_KEYS = {"apt", "tools", "caches", "node_heap"}


def from_data(data, where=PROFILE_PATH):
    """Check a profile's keys and values. Returns (profile, warnings); bad
    entries are dropped with a warning."""
    prof = {"apt": [], "tools": [], "caches": [], "node_heap": None}
    warns = []
    if not isinstance(data, dict):
        return prof, [f"{where}: must be a table of apt, tools, caches and node_heap"]
    for k in sorted(set(data) - PROFILE_KEYS):
        warns.append(f"{where}: ignoring unknown key {k!r}")

    def names(key):
        v = data.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            warns.append(f"{where}: {key} must be a list of strings; ignored")
            return []
        return v

    for p in names("apt"):
        (prof["apt"].append(p) if APT_RE.fullmatch(p) else warns.append(f"{where}: bad package name {p!r}"))
    for t in names("tools"):
        (prof["tools"].append(t) if t in TOOLS else warns.append(
            f"{where}: unknown tool {t!r} (known: {', '.join(sorted(TOOLS))})"))
    for c in names("caches"):
        (prof["caches"].append(c) if c in CACHES else warns.append(
            f"{where}: unknown cache {c!r} (known: {', '.join(sorted(CACHES))})"))
    heap = data.get("node_heap")
    if heap is not None:
        if isinstance(heap, int) and not isinstance(heap, bool) and heap > 0:
            prof["node_heap"] = heap
        else:
            warns.append(f"{where}: node_heap must be a positive number of MiB; ignored")
    for k in ("apt", "tools", "caches"):
        prof[k] = list(dict.fromkeys(prof[k]))
    return prof, warns


def parse_profile(text):
    """Parse a repo's .github/ci-flow.toml. None (no file) gives the defaults."""
    if text is None:
        return from_data({})
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return from_data({})[0], [f"{PROFILE_PATH} is not valid TOML ({e}); using defaults"]
    return from_data(data)
