"""`ci-flow init`: turn a fresh machine into a CI box.

Each step checks whether the machine already has what it sets up and only
plans what is missing, so init can run again at any time. Without --yes it
only prints the plan. With --yes it runs it; commands that need root go
through sudo, and nothing else ever does.

Steps, in order:

  packages     Docker, Tailscale and gh, from the distro's packages
  services     the docker and tailscaled services, and the docker group
  tailscale    joined to a tailnet (interactive: printed, never run)
  gh-auth      gh logged in (interactive: printed, never run)
  linger       user units keep running with nobody logged in
  install      ci-flow's code, the cimon/ci-flow links and the ci-flowd unit
  lid          laptops on external power keep running with the lid closed
               (a logind drop-in, unless the machine already decides this)
  prune        a weekly user timer pruning dangling images and old build cache
  alerts       dashboard badge always on; push notifications if configured
  power        come back after a power cut (BIOS: printed guidance)
  repos        security warnings for each managed repo
  apply        render and start the runners in the config
"""
import getpass
import os
import shutil
import subprocess

from . import config, engine, install, notify

LOGIND_DROPIN = "/etc/systemd/logind.conf.d/ci-flow.conf"
LOGIND_CONF = ["/etc/systemd/logind.conf", "/usr/lib/systemd/logind.conf.d", "/etc/systemd/logind.conf.d"]
LOGIND_TEXT = (
    "# Written by ci-flow init: on external power a CI machine keeps running\n"
    "# with its lid closed. On battery the lid does what it did before.\n"
    "[Login]\n"
    "HandleLidSwitchExternalPower=ignore\n"
    "HandleLidSwitchDocked=ignore\n"
)
LID_KEYS = ("HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleLidSwitchDocked")
PRUNE_DAYS = 7

# Per distro family: commands that come first (refresh the package lists, add
# vendor repos) and the packages for each tool. Tailscale publishes its own
# repos except on Arch; its install script sets them up.
APT_UPDATE = ["sudo", "apt-get", "update"]
DNF_PLUGINS = ["sudo", "dnf", "install", "-y", "dnf-plugins-core"]
PACKAGES = {
    "ubuntu": {"docker": ["docker.io", "docker-compose-v2"], "gh": ["gh"]},
    "debian": {"docker": ["docker.io", "docker-compose"], "gh": ["gh"]},
    "arch": {"docker": ["docker", "docker-compose"], "gh": ["github-cli"], "tailscale": ["tailscale"]},
    "fedora": {"docker": ["moby-engine", "docker-compose"], "gh": ["gh"]},
    # RHEL and its rebuilds have no Docker or gh packages: use the vendors' repos.
    "rhel": {"docker": ["docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"], "gh": ["gh"]},
}
VENDOR_REPOS = {
    "rhel": {
        "docker": ["sudo", "dnf", "config-manager", "--add-repo",
                   "https://download.docker.com/linux/rhel/docker-ce.repo"],
        "gh": ["sudo", "dnf", "config-manager", "--add-repo",
               "https://cli.github.com/packages/rpm/gh-cli.repo"],
    },
}
INSTALL_CMD = {
    "ubuntu": ["sudo", "apt-get", "install", "-y"],
    "debian": ["sudo", "apt-get", "install", "-y"],
    # A sync without the upgrade would be a partial upgrade, which Arch doesn't support.
    "arch": ["sudo", "pacman", "-Syu", "--needed", "--noconfirm"],
    "fedora": ["sudo", "dnf", "install", "-y"],
    "rhel": ["sudo", "dnf", "install", "-y"],
}
TAILSCALE_SCRIPT = ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"]


def distro(os_release="/etc/os-release"):
    """The distro family from /etc/os-release: 'ubuntu', 'debian', 'arch',
    'fedora' or 'rhel', or None."""
    fields = {}
    try:
        with open(os_release) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                fields[k] = v.strip('"').lower()
    except OSError:
        return None
    ids = [fields.get("ID", "")] + fields.get("ID_LIKE", "").split()
    for i in ids:
        if i in ("ubuntu", "linuxmint", "pop"):
            return "ubuntu"
        if i in ("debian", "raspbian"):
            return "debian"
        if i in ("arch", "manjaro", "endeavouros", "omarchy"):
            return "arch"
        if i in ("rhel", "centos", "rocky", "almalinux"):
            return "rhel"
        if i == "fedora":
            return "fedora"
    return None


class Step:
    """One thing init sets up. `done` says whether the machine already has it;
    `commands` are what would set it up (lists of argv), `notes` what the
    owner must do by hand, `root` whether the commands go through sudo.
    `done` is None for a step that is only guidance."""

    def __init__(self, name, what, done, commands=(), notes=(), root=False, action=None):
        self.name = name
        self.what = what
        self.done = done
        self.commands = [list(c) for c in commands]
        self.notes = list(notes)
        self.root = root
        self.action = action  # a Python callable instead of, or after, commands


class Machine:
    """What init reads from the machine. Tests swap in a fake."""

    def which(self, tool):
        return shutil.which(tool)

    def ok(self, cmd):
        try:
            return subprocess.run(cmd, capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def out(self, cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout if r.returncode == 0 else ""

    def read(self, path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return None

    def exists(self, path):
        return os.path.exists(path)

    def listdir(self, path):
        try:
            return sorted(os.listdir(path))
        except OSError:
            return []

    def logind(self, prop):
        """logind's effective value of a property, e.g. 'suspend', or None."""
        out = self.out(["busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
                        "org.freedesktop.login1.Manager", prop]).split()
        return out[1].strip('"') if len(out) == 2 and out[0] == "s" else None

    def user(self):
        return getpass.getuser()

    def is_laptop(self):
        ps = "/sys/class/power_supply"
        try:
            return any(self.read(os.path.join(ps, d, "type")) == "Battery\n" for d in os.listdir(ps))
        except OSError:
            return False


# ── Steps ────────────────────────────────────────────────────────────────────
def step_packages(m, pm):
    missing = [t for t in ("docker", "tailscale", "gh") if not m.which(t)]
    if not missing:
        return Step("packages", "Docker, Tailscale and gh are installed", True)
    if pm is None:
        return Step("packages", f"install {', '.join(missing)}", False,
                    notes=[f"unknown distro: install {', '.join(missing)} with your package manager"])
    names = [p for t in missing for p in PACKAGES[pm].get(t, [])]
    commands = []
    if pm in ("ubuntu", "debian"):
        commands.append(APT_UPDATE)
    vendor = [VENDOR_REPOS.get(pm, {}).get(t) for t in missing]
    if any(vendor):
        commands += [DNF_PLUGINS] + [c for c in vendor if c]
    if names:
        commands.append(INSTALL_CMD[pm] + names)
    if "tailscale" in missing and "tailscale" not in PACKAGES[pm]:
        commands.append(TAILSCALE_SCRIPT)
    return Step("packages", f"install {', '.join(missing)}", False, commands, root=True)


def step_services(m):
    user = m.user()
    commands = []
    if not m.ok(["systemctl", "is-enabled", "--quiet", "docker"]):
        commands.append(["sudo", "systemctl", "enable", "--now", "docker"])
    if not m.ok(["systemctl", "is-enabled", "--quiet", "tailscaled"]):
        commands.append(["sudo", "systemctl", "enable", "--now", "tailscaled"])
    in_group = "docker" in m.out(["id", "-nG", user]).split()
    if not in_group:
        commands.append(["sudo", "usermod", "-aG", "docker", user])
    notes = [] if in_group else ["log out and in again (or reboot) so the docker group applies"]
    return Step("services", "docker and tailscaled run at boot; you are in the docker group",
                not commands, commands, notes, root=True)


def step_tailscale(m):
    up = m.ok(["tailscale", "status"])
    return Step("tailscale", "joined to your tailnet", up,
                notes=[] if up else ["run `sudo tailscale up` and log in (interactive)"])


def step_gh_auth(m):
    ok = m.ok(["gh", "auth", "status"])
    return Step("gh-auth", "gh is logged in", ok,
                notes=[] if ok else ["run `gh auth login` (interactive); a token that can manage "
                                     "the repos' runners needs the repo scope (or Administration: write)"])


def step_linger(m):
    user = m.user()
    on = m.out(["loginctl", "show-user", user, "-p", "Linger", "--value"]).strip() == "yes"
    return Step("linger", "your user units run without a login session", on,
                [["sudo", "loginctl", "enable-linger", user]], root=True)


def step_install(m, home=None):
    src = install.source_root()
    current = install.installed_version(home)
    if install.is_installed(src, home):
        want = current
    else:
        want = install.version(src)
    unit = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                        "systemd", "user", "ci-flowd.service")
    done = current == want and install.unit_runs_current(m.read(unit), home)
    return Step("install", f"ci-flow {want} installed, ci-flowd runs it", done,
                [[os.path.join(src, "ci-flow"), "install"]])


def lid_settings(m):
    """Lid settings written in logind's config files other than ci-flow's own
    drop-in, as {key: (value, file)}; later files win, as for logind."""
    main, dropins = LOGIND_CONF[0], {}
    for d in LOGIND_CONF[1:]:  # /etc replaces a /usr/lib drop-in of the same name
        for n in m.listdir(d):
            if n.endswith(".conf"):
                dropins[n] = os.path.join(d, n)
    found = {}
    for f in [main] + [dropins[n] for n in sorted(dropins)]:
        if f == LOGIND_DROPIN:
            continue
        for line in (m.read(f) or "").splitlines():
            k, _, v = line.strip().partition("=")
            if k in LID_KEYS:
                found[k] = (v.strip(), f)
    return found


def step_lid(m):
    if not m.is_laptop():
        return Step("lid", "not a laptop: nothing to do for the lid", True)
    if m.read(LOGIND_DROPIN) == LOGIND_TEXT:
        return Step("lid", f"on external power the lid doesn't suspend ({LOGIND_DROPIN})", True)
    effective = {k: m.logind(k) for k in LID_KEYS}
    battery = effective["HandleLidSwitch"]
    on_battery = [f"on battery, closing the lid does `{battery}`"] if battery and battery != "ignore" else []
    if effective["HandleLidSwitchExternalPower"] == "ignore":
        return Step("lid", "on external power the lid doesn't suspend (already set on this machine)", True,
                    notes=on_battery)
    owned = lid_settings(m).get("HandleLidSwitchExternalPower")
    if owned:
        # The owner decided this in their own config: report it, don't override it.
        return Step("lid", "closing the lid on external power", None, notes=[
            f"{owned[1]} sets HandleLidSwitchExternalPower={owned[0]}: on external power, closing the lid "
            f"stops the CI machine. Set it to ignore there if it should keep running."] + on_battery)
    staged = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "ci-flow-logind.conf")

    def stage():
        with open(staged, "w") as f:
            f.write(LOGIND_TEXT)

    return Step("lid", f"on external power the lid doesn't suspend ({LOGIND_DROPIN})", False, [
        ["sudo", "install", "-D", "-m", "0644", staged, LOGIND_DROPIN],
        # HUP makes logind reread its config; restarting it would end sessions.
        ["sudo", "systemctl", "kill", "-s", "HUP", "systemd-logind"],
    ], notes=["the drop-in sets HandleLidSwitchExternalPower and HandleLidSwitchDocked to ignore"] + on_battery,
        root=True, action=stage)


def prune_commands(days=PRUNE_DAYS):
    """What the prune timer runs. Dangling images and build cache only: never
    volumes, never containers, never a tagged image, so no fleet's caches or
    runner images can go."""
    until = f"until={days * 24}h"
    return [
        ["docker", "image", "prune", "--force", "--filter", until],
        ["docker", "builder", "prune", "--force", "--filter", until],
    ]


def prune_units(days=PRUNE_DAYS, docker="/usr/bin/docker"):
    cmds = [[docker if c[0] == "docker" else c[0], *c[1:]] for c in prune_commands(days)]
    service = "\n".join([
        "# Written by ci-flow init. Dangling images and old build cache only;",
        "# never volumes, containers or tagged images.",
        "[Unit]",
        "Description=ci-flow: prune dangling Docker images and old build cache",
        "",
        "[Service]",
        "Type=oneshot",
        *[f"ExecStart={' '.join(c)}" for c in cmds],
        "",
    ])
    timer = "\n".join([
        "# Written by ci-flow init.",
        "[Unit]",
        "Description=ci-flow: weekly Docker prune",
        "",
        "[Timer]",
        "OnCalendar=weekly",
        "RandomizedDelaySec=1h",
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return {"ci-flow-prune.service": service, "ci-flow-prune.timer": timer}


def step_prune(m, unit_dir, days=PRUNE_DAYS, write=None):
    units = prune_units(days, m.which("docker") or "/usr/bin/docker")
    done = all(m.read(os.path.join(unit_dir, n)) == t for n, t in units.items()) \
        and m.ok(["systemctl", "--user", "is-enabled", "--quiet", "ci-flow-prune.timer"])

    def action():
        os.makedirs(unit_dir, exist_ok=True)
        for n, t in units.items():
            with open(os.path.join(unit_dir, n), "w") as f:
                f.write(t)

    return Step("prune", f"weekly prune of dangling images and build cache older than {days} days",
                done, [["systemctl", "--user", "daemon-reload"],
                       ["systemctl", "--user", "enable", "--now", "ci-flow-prune.timer"]],
                notes=[f"writes {', '.join(os.path.join(unit_dir, n) for n in units)}"],
                action=write or action)


def step_alerts(conf):
    try:
        n = notify.settings(conf)
    except notify.NotifyError as e:
        return Step("alerts", "push notifications", False, notes=[f"fix the config: {e}"])
    if n:
        return Step("alerts", f"{n['level']} problems are pushed to {notify.redact(n['ntfy'])}", True)
    return Step("alerts", "disk, heat, battery and runner problems show on the dashboards' badge", True,
                notes=['for push notifications add "notify": {"ntfy": "https://ntfy.sh/<secret topic>"} '
                       "to the config and restart ci-flowd"])


def step_power():
    return Step("power", "comes back on after a power cut", None, notes=[
        "set the BIOS/UEFI option to power on when AC is restored ('Restore on AC power loss', "
        "'AC Back', 'Power On After Power Failure'); ci-flow can't set it for you"])


def step_repos(conf, gh):
    warns = []
    for e in config.managed(conf):
        info = gh.repo(e["repo"])
        fork = gh.fork_settings(e["repo"], not info.get("private")) if info else None
        warns += engine.security_warnings(e["repo"], info, fork)
    n = len(config.managed(conf))
    return Step("repos", f"{n} managed repo{'s' if n != 1 else ''} checked", not warns, notes=warns)


def step_apply(conf, config_path):
    n = len(config.managed(conf))
    if not n:
        return Step("apply", "no managed repos yet", True,
                    notes=["add one with `ci-flow add owner/repo --runners N`, then run `ci-flow apply`"])
    return Step("apply", f"runners for {n} managed repo{'s' if n != 1 else ''} rendered and started", False,
                [[os.path.join(install.source_root(), "ci-flow"), "--config", config_path, "apply"]])


def plan(m, conf, gh, config_path, unit_dir, home=None, prune_days=PRUNE_DAYS, pm="detect"):
    pm = distro() if pm == "detect" else pm
    return [
        step_packages(m, pm),
        step_services(m),
        step_tailscale(m),
        step_gh_auth(m),
        step_linger(m),
        step_install(m, home),
        step_lid(m),
        step_prune(m, unit_dir, prune_days),
        step_alerts(conf),
        step_power(),
        step_repos(conf, gh),
        step_apply(conf, config_path),
    ]


def show(steps, out=print):
    todo = 0
    for s in steps:
        mark = "note" if s.done is None else "ok  " if s.done else ("todo" if s.commands or s.action else "note")
        out(f"[{mark}] {s.name:<10} {s.what}{'  (sudo)' if s.root and not s.done else ''}")
        if not s.done:
            todo += bool(s.commands or s.action)
            for c in s.commands:
                out(f"         $ {' '.join(c)}")
        for n in s.notes:
            out(f"         - {n}")
    return todo


def run(steps, runner=None, out=print):
    """Run the steps that aren't done. Stops at the first failing command."""
    runner = runner or (lambda cmd: subprocess.run(cmd).returncode == 0)
    for s in steps:
        if s.done or not (s.commands or s.action):
            continue
        out(f"==> {s.name}: {s.what}")
        if s.action:
            s.action()
        for c in s.commands:
            if not runner(c):
                out(f"ci-flow init: `{' '.join(c)}` failed; fix it and run init again")
                return False
    return True
