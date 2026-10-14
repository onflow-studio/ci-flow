"""Collectors for the CI machine: host metrics, runner slots and GitHub runs.

Everything a view needs ends up in one JSON-serialisable snapshot, built by
Collector.snapshot(). Pure stdlib; data comes from /proc, /sys, docker,
systemctl --user and gh.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque

from . import config
from .config import ConfigError  # noqa: F401 (re-exported)
from .model import derive

HOST = socket.gethostname().split(".")[0]
SNAPSHOT_VERSION = 1


def load_fleets(path=None):
    """Runner fleets: compose project, systemd unit, and the repo each slot serves."""
    return config.fleets(config.load(path or config.CONFIG))


def repos_of(fleets):
    return list(dict.fromkeys(r for f in fleets for r in f["slots"].values()))


def slot_key(project, n):
    return f"{project}/{n}"


def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def read(path):
    with open(path) as f:
        return f.read()


# ── Host metrics (cheap, every second) ───────────────────────────────────────
class Host:
    def __init__(self):
        self.prev_cpu = None
        self.prev_net = None
        self.prev_t = None
        self.cpu_hist = deque([0.0] * 40, maxlen=40)
        self.cpu = 0.0
        self.net = (0.0, 0.0)

    @staticmethod
    def _cpu_times():
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
        return sum(v), v[3] + v[4]

    @staticmethod
    def _net_bytes():
        rx = tx = 0
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, data = line.split(":", 1)
                name = name.strip()
                if name == "lo" or name.startswith(("veth", "br-", "docker")):
                    continue
                d = data.split()
                rx += int(d[0])
                tx += int(d[8])
        return rx, tx

    def sample(self):
        now = time.monotonic()
        cpu = self._cpu_times()
        net = self._net_bytes()
        if self.prev_cpu:
            dt_all = cpu[0] - self.prev_cpu[0]
            dt_idle = cpu[1] - self.prev_cpu[1]
            self.cpu = 100.0 * (1 - dt_idle / dt_all) if dt_all else 0.0
            self.cpu_hist.append(self.cpu)
            dt = now - self.prev_t
            self.net = ((net[0] - self.prev_net[0]) / dt, (net[1] - self.prev_net[1]) / dt)
        self.prev_cpu, self.prev_net, self.prev_t = cpu, net, now

    @staticmethod
    def mem():
        m = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                m[k] = int(v.split()[0]) * 1024
        return (m["MemTotal"] - m["MemAvailable"], m["MemTotal"],
                m["SwapTotal"] - m["SwapFree"], m["SwapTotal"])

    @staticmethod
    def temp():
        best = None
        for hw in os.listdir("/sys/class/hwmon"):
            base = f"/sys/class/hwmon/{hw}"
            try:
                name = read(f"{base}/name").strip()
            except OSError:
                continue
            if name in ("coretemp", "k10temp", "zenpower"):
                try:
                    best = int(read(f"{base}/temp1_input")) / 1000
                except OSError:
                    pass
        return best

    @staticmethod
    def power():
        ps = "/sys/class/power_supply"
        ac = bat = None
        try:
            entries = os.listdir(ps)
        except OSError:
            return ac, bat
        for d in entries:
            try:
                t = read(f"{ps}/{d}/type").strip()
                if t == "Mains":
                    ac = read(f"{ps}/{d}/online").strip() == "1"
                elif t == "Battery":
                    bat = int(read(f"{ps}/{d}/capacity"))
            except OSError:
                pass
        return ac, bat

    @staticmethod
    def uptime():
        return float(read("/proc/uptime").split()[0])

    def snapshot(self):
        mu, mt, su, stot = self.mem()
        du = shutil.disk_usage("/")
        ac, bat = self.power()
        return {
            "cpu": self.cpu,
            "cpu_hist": list(self.cpu_hist),
            "net": list(self.net),
            "mem": {"used": mu, "total": mt, "swap_used": su, "swap_total": stot},
            "disk": {"used": du.used, "total": du.total, "free": du.free},
            "load": list(os.getloadavg()),
            "ncpu": os.cpu_count() or 1,
            "temp": self.temp(),
            "ac": ac,
            "bat": bat,
            "uptime": self.uptime(),
        }


# ── Slow collectors (background threads) ─────────────────────────────────────
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.docker_ok = True
        self.slots_up = set()  # "project/n" with a running runner container
        self.units = {}        # "project/n" -> ActiveState
        self.runs = {}         # repo -> list of runs
        self.jobs = {}         # run id -> list of jobs
        self.gh_err = {}       # repo -> error text

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


def collect_local(st, fleets):
    units = {}
    for f in fleets:
        for n in f["slots"]:
            out = sh(["systemctl", "--user", "show", f["unit"].format(n), "-p", "LoadState,ActiveState"])
            p = dict(l.split("=", 1) for l in (out or "").splitlines() if "=" in l)
            units[slot_key(f["project"], n)] = p.get("ActiveState") if p.get("LoadState") == "loaded" else None
    out = sh(["docker", "ps", "--format",
              '{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}'])
    up = set()
    for line in (out or "").splitlines():
        proj, _, svc = line.partition("\t")
        m = re.fullmatch(r"runner-(\d+)", svc)
        if m:
            up.add(slot_key(proj, int(m.group(1))))
    st.set(units=units, slots_up=up, docker_ok=out is not None)


def trim_job(j):
    """Keep only what the views use; the jobs API returns far more."""
    return {
        "name": j.get("name"),
        "status": j.get("status"),
        "conclusion": j.get("conclusion"),
        "started_at": j.get("started_at"),
        "runner_name": j.get("runner_name"),
        "html_url": j.get("html_url"),
        "steps": [{"status": s.get("status")} for s in j.get("steps") or []],
    }


def collect_repo(st, repo):
    out = sh(["gh", "run", "list", "-R", repo, "-L", "15", "--json",
              "databaseId,workflowName,displayTitle,headBranch,status,conclusion,createdAt,updatedAt,event,url"],
             timeout=20)
    if out is None:
        with st.lock:
            st.gh_err[repo] = "gh run list failed"
        return
    try:
        runs = json.loads(out)
        if not isinstance(runs, list):
            raise ValueError("not a list")
    except ValueError:
        with st.lock:
            st.gh_err[repo] = "gh run list returned invalid JSON"
        return
    for r in runs:
        if r["status"] == "completed":
            continue
        j = sh(["gh", "api", f"repos/{repo}/actions/runs/{r['databaseId']}/jobs?per_page=100"], timeout=20)
        try:
            jobs = [trim_job(x) for x in json.loads(j).get("jobs", [])] if j else None
        except (ValueError, AttributeError):
            jobs = None  # keep the last good jobs for this run
        if jobs is not None:
            with st.lock:
                st.jobs[r["databaseId"]] = jobs
    with st.lock:
        st.runs[repo] = runs
        st.gh_err.pop(repo, None)
        for r in runs:
            if r["status"] == "completed":
                st.jobs.pop(r["databaseId"], None)


def mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


class Collector:
    """Runs the collectors and builds snapshots for the views.

    With `config_path` it rereads the config when the file changes, so
    `ci-flow apply`, `add` and `remove` show up without a restart.
    """

    RELOAD_EVERY = 5

    def __init__(self, fleets, config_path=None, log=None):
        self.fleets = fleets
        self.repos = repos_of(fleets)
        self.host = Host()
        self.st = State()
        self.wakers = []
        self.stopped = threading.Event()
        self.config_path = config_path
        self.config_mtime = mtime(config_path) if config_path else None
        self.config_error = None
        self.watching = set()
        self.log = log or (lambda msg: print(f"ci-flowd: {msg}", flush=True))

    def reload_config(self):
        """Pick up a changed config. Returns True when the fleets changed. A
        config that doesn't load keeps the last good one."""
        if not self.config_path:
            return False
        m = mtime(self.config_path)
        if m == self.config_mtime:
            return False
        self.config_mtime = m
        try:
            fleets = config.fleets(config.load(self.config_path))
        except ConfigError as e:
            if str(e) != self.config_error:
                self.log(f"config not reloaded, keeping the last good one: {e}")
            self.config_error = str(e)
            return False
        self.config_error = None
        if fleets == self.fleets:
            return False
        repos = repos_of(fleets)
        with self.st.lock:
            self.fleets, self.repos = fleets, repos
            for gone in set(self.st.runs) - set(repos):
                self.st.runs.pop(gone, None)
                self.st.gh_err.pop(gone, None)
        if self.started:
            for repo in repos:
                self.watch_repo(repo)
        self.log(f"config reloaded: {len(repos)} repo{'s' if len(repos) != 1 else ''}, "
                 f"{sum(len(f['slots']) for f in fleets)} slots")
        self.refresh()
        return True

    def watch_repo(self, repo):
        """One collector loop per repo, for as long as the repo is in the config."""
        if repo in self.watching:
            return
        self.watching.add(repo)

        def once():
            if repo in self.repos:
                collect_repo(self.st, repo)
        self.every(15, once)

    started = False

    def every(self, interval, fn, *args):
        ev = threading.Event()
        self.wakers.append(ev)

        def loop():
            while not self.stopped.is_set():
                try:
                    fn(*args)
                except Exception:  # keep the collector alive
                    pass
                ev.wait(interval)
                ev.clear()
        threading.Thread(target=loop, daemon=True).start()

    def start(self, sample_interval=1.0):
        def sampler():
            while not self.stopped.is_set():
                try:
                    self.host.sample()
                except Exception:
                    pass
                self.stopped.wait(sample_interval)
        threading.Thread(target=sampler, daemon=True).start()
        self.started = True
        self.every(3, lambda: collect_local(self.st, self.fleets))
        for repo in self.repos:
            self.watch_repo(repo)
        if self.config_path:
            self.every(self.RELOAD_EVERY, self.reload_config)

    def collect_once(self):
        self.host.sample()
        collect_local(self.st, self.fleets)
        for repo in self.repos:
            collect_repo(self.st, repo)
        self.host.sample()

    def refresh(self):
        for ev in self.wakers:
            ev.set()

    def stop(self):
        """End the collector threads after their current pass."""
        self.stopped.set()
        self.refresh()

    def snapshot(self):
        host = self.host.snapshot()
        with self.st.lock:
            runs = {k: [dict(r) for r in v] for k, v in self.st.runs.items() if k in self.repos}
            live = {r["databaseId"] for rs in runs.values() for r in rs if r["status"] != "completed"}
            jobs = {str(k): [dict(j) for j in v] for k, v in self.st.jobs.items() if k in live}
            fleets, repos = self.fleets, self.repos
            snap = {
                "version": SNAPSHOT_VERSION,
                "ts": time.time(),
                "hostname": HOST,
                "fleets": [{"project": f["project"], "unit": f["unit"],
                            "slots": {str(n): r for n, r in f["slots"].items()}} for f in fleets],
                "repos": list(repos),
                "host": host,
                "docker_ok": self.st.docker_ok,
                "units": dict(self.st.units),
                "slots_up": sorted(self.st.slots_up),
                "runs": runs,
                "jobs": jobs,
                "gh_err": dict(self.st.gh_err),
            }
        try:
            snap["view"] = derive(snap)
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:  # serve the raw data anyway
            snap["view"] = None
            snap["view_error"] = f"{type(e).__name__}: {e}"
        return snap
