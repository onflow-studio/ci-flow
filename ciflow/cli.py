"""ci-flow: manage the self-hosted runners on this machine.

  ci-flow init [--yes]                                set up this machine (plan only without --yes)
  ci-flow install [--no-start]                        install this code, start the collector
  ci-flow add owner/repo --runners N [--labels a,b]   add a repo to the config
  ci-flow remove owner/repo [--purge]                 stop its runners, drop it
  ci-flow apply [--dry-run]                           install, render, build and start
  ci-flow status                                      what runs where

The config is ~/.config/ci-flow/config.json ($CIFLOW_CONFIG); the installed
code and rendered projects live in ~/.local/share/ci-flow ($CIFLOW_HOME).
"""
import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys

from . import config, engine, install, loop
from . import init as machine_init

UNIT_DIR = os.environ.get("CIFLOW_UNIT_DIR") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "systemd", "user")


def warn(msg):
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def say(msg):
    print(msg, flush=True)


class System:
    """Commands that change the machine. In dry-run mode they are printed instead."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run

    def run(self, cmd, check=False):
        if self.dry_run:
            print("  would run: " + " ".join(cmd))
            return True
        r = subprocess.run(cmd)
        if check and r.returncode:
            raise SystemExit(f"ci-flow: {' '.join(cmd)} failed ({r.returncode})")
        return r.returncode == 0

    def write(self, path, text):
        if self.dry_run:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        if path.endswith(".sh"):
            os.chmod(path, 0o755)

    def build(self, project, directory):
        if self.dry_run:
            print(f"  would build {engine.image_of(project)}")
            return True
        return loop.build_image(project, directory, say, force=True)


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def show_diff(path, old, new):
    lines = difflib.unified_diff((old or "").splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile=path if old is not None else "/dev/null", tofile=path)
    sys.stdout.writelines(lines)


def install_code(system, home=None, out=say):
    """Install the code this command runs from, unless it already is the
    installed copy. Returns True when lib/current moved to a new version."""
    src = install.source_root()
    if install.is_installed(src, home):
        out(f"ci-flow {install.installed_version(home)} (installed)")
        return False
    ver = install.version(src)
    if system.dry_run:
        same = install.installed_version(home) == ver
        out(f"{'ci-flow ' + ver + ' is already installed' if same else f'would install ci-flow {ver} from {src}'}")
        return False
    ver, changed = install.install_code(src, home)
    out(f"{'installed' if changed else 'already installed:'} ci-flow {ver} from {src}")
    return changed


def write_unit(system, path, text, out=say):
    old = read_text(path)
    if old == text:
        return False
    out(f"{'create' if old is None else 'update'} {path}")
    if system.dry_run:
        show_diff(path, old, text)
    system.write(path, text)
    return True


def cmd_install(args, system):
    for tool in ("gh", "docker", "systemctl"):
        if not shutil.which(tool):
            raise SystemExit(f"ci-flow: {tool} not found")
    if not os.path.exists(args.config):
        warn(f"no {args.config} yet; copy config.example.json there")
    changed = install_code(system)
    if not system.dry_run:
        install.link_bin()
    unit_path = os.path.join(UNIT_DIR, "ci-flowd.service")
    write_unit(system, unit_path, install.ciflowd_unit())
    system.run(["systemctl", "--user", "daemon-reload"])
    if args.no_start:
        return
    system.run(["systemctl", "--user", "enable", "ci-flowd.service"])
    system.run(["systemctl", "--user", "restart", "ci-flowd.service"])
    say(f"ci-flowd running: http://127.0.0.1:{os.environ.get('CIFLOW_PORT', '7340')}/")
    if changed:
        say("Runner slots switch to this version between jobs.")


def cmd_init(args, gh):
    conf = load_or_new(args.config)
    steps = machine_init.plan(machine_init.Machine(), conf, gh, args.config, UNIT_DIR, prune_days=args.prune_days)
    todo = machine_init.show(steps)
    if not todo:
        say("\nThis machine is set up.")
        return
    if not args.yes or args.dry_run:
        say(f"\n{todo} step{'s' if todo != 1 else ''} to do. Run `ci-flow init --yes` to do "
            f"{'them' if todo != 1 else 'it'}; commands marked (sudo) ask for your password.")
        return
    say("")
    if not machine_init.run(steps):
        raise SystemExit(1)
    say("\nDone. Run `ci-flow init` again to check; it only plans what is still missing.")


def units_of(project):
    """Slot numbers that have a ci-flow runner unit loaded for this project."""
    out = subprocess.run(["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend",
                          f"ci-flow-runner@{project}-*.service"], capture_output=True, text=True).stdout
    slots = set()
    for line in out.splitlines():
        name = line.split()[0] if line.split() else ""
        inst = name.removeprefix("ci-flow-runner@").removesuffix(".service")
        try:
            p, n = loop.parse_instance(inst)
        except ValueError:
            continue
        if p == project:
            slots.add(n)
    return slots


def read_profile(entry, gh, info, profile_file=None):
    """The profile to render with, where it came from, and warnings. In order:
    a --profile preview file, the config's inline profile, the repo's file on
    its default branch, the defaults."""
    if profile_file:
        text = read_text(profile_file)
        if text is None:
            raise SystemExit(f"ci-flow: cannot read {profile_file}")
        prof, warns = engine.parse_profile(text)
        return prof, profile_file, warns
    if entry.get("profile") is not None:
        return entry["profile"], "inline in the config", []
    branch = (info or {}).get("default_branch")
    if not branch:
        return engine.parse_profile(None)[0], "defaults (repo unreadable)", []
    text = gh.profile(entry["repo"], branch)
    prof, warns = engine.parse_profile(text)
    return prof, f"{engine.PROFILE_PATH} on {branch}" if text is not None else "defaults (no profile in the repo)", warns


def plan_repo(entry, gh, profile_file=None):
    """Read what GitHub says about the repo and render its project files."""
    repo = entry["repo"]
    info = gh.repo(repo)
    prof, source, warns = read_profile(entry, gh, info, profile_file)
    fork = gh.fork_settings(repo, not info.get("private")) if info else None
    return {
        "entry": entry,
        "profile_source": source,
        "warnings": warns + engine.security_warnings(repo, info, fork),
        "files": engine.render(entry, prof, source),
    }


def apply(conf, gh, system, home=None, profiles=None, out=say):
    home = home or engine.HOME
    entries = config.managed(conf)
    profiles = profiles or {}
    for repo in profiles:
        if repo not in {e["repo"] for e in entries}:
            raise SystemExit(f"ci-flow: --profile for {repo}, which is not a managed repo")
    plans = [plan_repo(e, gh, profiles.get(e["repo"])) for e in entries]
    if not plans:
        out("No managed repos in the config. Add one with `ci-flow add owner/repo`.")

    if plans:
        if install_code(system, home, out):
            # ci-flowd rereads the config by itself; it only needs a restart to
            # run new code, and only when it runs from this install.
            if install.unit_runs_current(read_text(os.path.join(UNIT_DIR, "ci-flowd.service")), home):
                out("Running slots switch to it between jobs; restarting ci-flowd.")
                system.run(["systemctl", "--user", "try-restart", "ci-flowd.service"])
            else:
                out("Running slots switch to it between jobs.")
        unit_path = os.path.join(UNIT_DIR, engine.UNIT_FILE)
        write_unit(system, unit_path, engine.unit(os.path.join(install.current(home), "ci-flow"), home=home), out)
        system.run(["systemctl", "--user", "daemon-reload"])

    for p in plans:
        e = p["entry"]
        d = engine.project_dir(e["project"], home)
        out(f"\n{e['repo']} → {e['project']} ({len(e['slots'])} slot{'s' if len(e['slots']) != 1 else ''}), "
            f"profile: {p['profile_source']}")
        for w in p["warnings"]:
            warn(w)
        image_inputs_changed = False
        for name, text in p["files"].items():
            path = os.path.join(d, name)
            old = read_text(path)
            if old == text:
                continue
            out(f"  {'create' if old is None else 'update'} {path}")
            if system.dry_run:
                show_diff(path, old, text)
            system.write(path, text)
            image_inputs_changed |= name in ("Dockerfile", "entrypoint.sh")
        image = engine.image_of(e["project"])
        if image_inputs_changed or not system.dry_run and loop.image_age(image) is None:
            if not system.build(e["project"], d):
                warn(f"building {image} failed; the runner loop will retry")
        loaded = set() if system.dry_run else units_of(e["project"])
        for n in e["slots"]:
            system.run(["systemctl", "--user", "enable", "--now", f"ci-flow-runner@{e['project']}-{n}.service"])
        for n in sorted(loaded - set(e["slots"])):
            out(f"  slot {n} is no longer configured; stopping it")
            system.run(["systemctl", "--user", "disable", "--now", f"ci-flow-runner@{e['project']}-{n}.service"])

    known = {e["project"] for e in config.managed(conf)}
    try:
        orphans = sorted(set(os.listdir(os.path.join(home, "projects"))) - known)
    except OSError:
        orphans = []
    for o in orphans:
        out(f"\n{o}: rendered here but not in the config; `ci-flow remove` it, or add its repo back.")


def cmd_add(args, gh):
    conf = load_or_new(args.config)
    repos = conf.setdefault("repos", [])
    if any(e.get("repo") == args.repo for e in repos):
        raise SystemExit(f"ci-flow: {args.repo} is already in {args.config}")
    entry = {"repo": args.repo, "slots": args.runners}
    if args.labels:
        entry["labels"] = [x for x in args.labels.split(",") if x]
    for key in ("memory", "cpus"):
        if getattr(args, key) is not None:
            entry[key] = getattr(args, key)
    repos.append(entry)
    try:
        config.managed(conf)
    except config.ConfigError as e:
        raise SystemExit(f"ci-flow: {e}")
    info = gh.repo(args.repo)
    for w in engine.security_warnings(args.repo, info, gh.fork_settings(args.repo, not info.get("private"))
                                      if info else None):
        warn(w)
    config.save(conf, args.config)
    labels = ", ".join(config.BASE_LABELS + entry.get("labels", []))
    print(f"Added {args.repo} with {args.runners} runner(s) ({labels}) to {args.config}.")
    print("Run `ci-flow apply` to start them; jobs opt in with `runs-on: [self-hosted, ...]`.")


def cmd_remove(args, system):
    conf = config.load(args.config)
    entries = {e["repo"]: e for e in config.managed(conf)}
    if args.repo not in entries:
        raise SystemExit(f"ci-flow: {args.repo} is not a managed repo")
    e = entries[args.repo]
    d = engine.project_dir(e["project"])
    for n in sorted(set(e["slots"]) | units_of(e["project"])):
        system.run(["systemctl", "--user", "disable", "--now", f"ci-flow-runner@{e['project']}-{n}.service"])
    compose_file = os.path.join(d, "docker-compose.yml")
    if os.path.exists(compose_file):
        system.run(["docker", "compose", "-p", e["project"], "-f", compose_file, "--profile", "runner",
                    "down", "--remove-orphans", *(["--volumes"] if args.purge else [])])
    if args.purge:
        system.run(["docker", "image", "rm", "-f", engine.image_of(e["project"])])
    if not system.dry_run:
        shutil.rmtree(d, ignore_errors=True)
        conf["repos"] = [x for x in conf["repos"] if x.get("repo") != args.repo]
        config.save(conf, args.config)
        lock = loop.lock_path(e["project"])
        if os.path.exists(lock):
            os.unlink(lock)
    if not [x for x in conf.get("repos") or [] if x.get("repo") != args.repo]:
        # The last managed repo: the shared unit template and the empty
        # projects directory go too.
        unit_path = os.path.join(UNIT_DIR, engine.UNIT_FILE)
        if os.path.exists(unit_path):
            print(f"{'would remove' if system.dry_run else 'remove'} {unit_path}")
            if not system.dry_run:
                os.unlink(unit_path)
            system.run(["systemctl", "--user", "daemon-reload"])
        if not system.dry_run:
            try:
                os.rmdir(os.path.join(engine.HOME, "projects"))
            except OSError:
                pass
    print(f"{'Would remove' if system.dry_run else 'Removed'} {args.repo}." + ("" if args.purge else " Its volumes (caches, image cache) are kept; "
                                                          "`docker volume ls` shows them, --purge removes them."))


def profile_drift(e, gh, home=None):
    """Where the profile comes from now, and whether it differs from the one
    the project was last rendered with. The daily image rebuild never rereads
    the profile; only `ci-flow apply` does."""
    prof, source, _ = read_profile(e, gh, gh.repo(e["repo"]))
    try:
        with open(os.path.join(engine.project_dir(e["project"], home), "project.json")) as f:
            rendered = json.load(f).get("profile")
    except (OSError, ValueError):
        return source
    if prof != rendered:
        return f"{source}, changed since the last apply; run `ci-flow apply`"
    return source


def cmd_status(args, gh):
    conf = config.load(args.config)
    print(f"ci-flow {install.installed_version() or 'not installed (run ci-flow install)'}")
    for e in config.managed(conf):
        d = engine.project_dir(e["project"])
        age = loop.image_age(engine.image_of(e["project"]))
        print(f"{e['repo']}  ({e['project']})")
        print(f"  files: {d if os.path.isdir(d) else 'not rendered; run ci-flow apply'}")
        print(f"  image: {engine.image_of(e['project'])} "
              f"{'missing' if age is None else f'built {int(age // 3600)}h ago'}")
        print(f"  profile: {profile_drift(e, gh)}")
        for n, s in e["slots"].items():
            unit = f"ci-flow-runner@{e['project']}-{n}.service"
            state = subprocess.run(["systemctl", "--user", "is-active", unit],
                                   capture_output=True, text=True).stdout.strip() or "unknown"
            print(f"  slot {n}: {state:<9} {', '.join(config.BASE_LABELS + s['labels'])}  "
                  f"{s['memory']} / {s['cpus']} cpu")
    for f in conf.get("fleets") or []:
        print(f"{f['project']}  (watch-only: {len(f['slots'])} slots, {f['unit']})")


def load_or_new(path):
    if not os.path.exists(path):
        return {}
    try:
        return config.load(path)
    except config.ConfigError as e:
        raise SystemExit(f"ci-flow: {e}")


def parse_profiles(items):
    out = {}
    for item in items or []:
        repo, sep, path = item.partition("=")
        if not sep or not config.REPO_RE.fullmatch(repo):
            raise SystemExit(f"ci-flow: --profile takes owner/repo=path, not {item!r}")
        out[repo] = path
    return out


def main(argv=None, gh=None):
    ap = argparse.ArgumentParser(prog="ci-flow", description="Manage the self-hosted runners on this machine.")
    ap.add_argument("--config", default=config.CONFIG, help="config file (default %(default)s)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{init,install,add,remove,apply,status}")
    n = sub.add_parser("init", help="set up this machine as a CI box (prints the plan; --yes runs it)")
    n.add_argument("--yes", action="store_true", help="run the plan (sudo for the steps that need root)")
    n.add_argument("--dry-run", action="store_true", help="only print the plan (the default)")
    n.add_argument("--prune-days", type=int, default=machine_init.PRUNE_DAYS,
                   help="prune dangling images and build cache older than this (default %(default)s)")
    i = sub.add_parser("install", help="install this code and start the ci-flowd collector")
    i.add_argument("--no-start", action="store_true", help="write the unit without starting it")
    i.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("add", help="add a repo to the config")
    a.add_argument("repo")
    a.add_argument("--runners", type=int, default=1, help="slots for this repo (default 1)")
    a.add_argument("--labels", help="extra runner labels, comma-separated")
    a.add_argument("--memory", help='runner memory limit, e.g. "4g"')
    a.add_argument("--cpus", type=float, help="runner CPU limit")
    r = sub.add_parser("remove", help="stop a repo's runners and drop it from the config")
    r.add_argument("repo")
    r.add_argument("--purge", action="store_true", help="also delete its volumes and image")
    r.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("apply", help="render projects, build images, start runner units")
    p.add_argument("--dry-run", action="store_true", help="print the changes instead of making them")
    p.add_argument("--profile", action="append", metavar="OWNER/REPO=PATH",
                   help="use a local profile file instead of the repo's (e.g. to preview one)")
    sub.add_parser("status", help="show managed repos, slots and units")
    lp = sub.add_parser("runner-loop")  # run by the ci-flow-runner@ units
    lp.add_argument("instance")
    args = ap.parse_args(argv)
    gh = gh or engine.GitHub()

    if args.cmd == "runner-loop":
        loop.main(args.instance)
    elif args.cmd == "init":
        cmd_init(args, gh)
    elif args.cmd == "install":
        cmd_install(args, System(args.dry_run))
    elif args.cmd == "add":
        cmd_add(args, gh)
    elif args.cmd == "remove":
        cmd_remove(args, System(args.dry_run))
    elif args.cmd == "status":
        cmd_status(args, gh)
    elif args.cmd == "apply":
        try:
            conf = config.load(args.config)
        except config.ConfigError as e:
            raise SystemExit(f"ci-flow: {e}")
        apply(conf, gh, System(args.dry_run), profiles=parse_profiles(args.profile))
