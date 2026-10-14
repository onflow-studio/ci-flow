"""The runner loop: keeps one slot of a managed repo online.

Run by the `ci-flow-runner@<project>-<slot>` user unit. Each iteration starts
the slot's dind daemon, clears what the last job left in it, mints a
single-use JIT runner config, and runs exactly one job in a fresh runner
container. On stop it stops the container and deregisters the runner.

When `ci-flow install` or `apply` has switched lib/current to a new version,
the loop exits between jobs and systemd starts it again on the new code, so an
upgrade never interrupts a running job.
"""
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time

from . import engine, install

HOST = socket.gethostname().split(".")[0]
IMAGE_MAX_AGE = 86400  # rebuild the runner image at most daily
MAX_BACKOFF = 300
DEREGISTER_FOR = 60  # GitHub reports a stopped runner busy for up to about a minute
STOP_SIGNALS = {signal.SIGTERM, signal.SIGINT}


def log(slot, msg):
    print(f"[{slot}] {msg}", flush=True)


def parse_instance(instance):
    """'ciflow-web-app-2' -> ('ciflow-web-app', 2)."""
    project, _, n = instance.rpartition("-")
    if not project or not n.isdigit():
        raise ValueError(f"instance must be <project>-<slot>, not {instance!r}")
    return project, int(n)


def image_age(image):
    out = subprocess.run(["docker", "image", "inspect", "-f", "{{.Created}}", image],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    r = subprocess.run(["date", "-d", out.stdout.strip(), "+%s"], capture_output=True, text=True)
    return time.time() - int(r.stdout) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def deregister(repo, runner_id, wait=DEREGISTER_FOR, sleep=time.sleep):
    """Remove a runner from GitHub. While GitHub still thinks it is running a
    job the DELETE fails, so retry for a while."""
    deadline = time.monotonic() + wait
    while True:
        path = f"repos/{repo}/actions/runners/{runner_id}"
        if engine.gh_api(path, method="DELETE") is not None or engine.gh_api(path) is None:
            return True  # removed now, or already gone
        if time.monotonic() >= deadline:
            return False
        sleep(5)


def outdated(root=None, home=None):
    """True when this process runs an installed version that is no longer current."""
    root = root or install.source_root()
    if not install.is_installed(root, home):
        return False
    return os.path.realpath(install.current(home)) != os.path.realpath(root)


def lock_path(project):
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"ci-flow-{project}-build.lock")


def build_image(project, directory, log_fn=print, force=False):
    """Build the project's runner image when it is missing, older than a day,
    or forced; one build per project at a time."""
    image = engine.image_of(project)
    lock = lock_path(project)
    # The image's Created time doesn't move when a rebuild is fully cached (the
    # base image hasn't changed), so the last build is stamped here instead.
    stamp = os.path.join(directory, ".image-built")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            age = time.time() - os.path.getmtime(stamp) if image_age(image) is not None else None
        except OSError:
            age = None
        if not force and age is not None and age < IMAGE_MAX_AGE:
            return True
        log_fn(f"building {image}")
        if subprocess.run(["docker", "build", "--pull", "-q", "-t", image, directory]).returncode:
            return False
        with open(stamp, "w"):
            pass
        return True



NAME_MAX = 64  # GitHub's limit for a runner name


def runner_name(host, project, n, ts):
    """'ci-box', 'ciflow-web-app', 2 -> 'ci-box-web-app-2-<ts>'. The
    project keeps slots of different repos apart; the dashboards read the slot
    number from the end."""
    tail = f"-{n}-{ts}"
    slug = project[len("ciflow-"):] if project.startswith("ciflow-") else project
    slug = slug[:max(0, NAME_MAX - len(host) - 1 - len(tail))].rstrip("-")
    return f"{host}-{slug}{tail}" if slug else f"{host}{tail}"

class Slot:
    def __init__(self, instance, home=None):
        self.instance = instance
        self.project, self.n = parse_instance(instance)
        self.dir = engine.project_dir(self.project, home)
        self.child = None
        self.runner_id = None
        self.repo = None

    def log(self, msg):
        log(self.instance, msg)

    def meta(self):
        with open(os.path.join(self.dir, "project.json")) as f:
            return json.load(f)

    def compose(self, *args):
        return ["docker", "compose", "-p", self.project, "-f", os.path.join(self.dir, "docker-compose.yml"), *args]

    def clean_dind(self):
        # The runner removes its job containers itself; this catches what a
        # crashed or cancelled job left behind in this slot's daemon.
        subprocess.run(self.compose("exec", "-T", f"dind-{self.n}", "sh", "-c",
                                    "docker ps -aq | xargs -r docker rm -f; docker network prune -f; "
                                    "docker volume prune -af; docker image prune -af --filter until=168h"),
                       capture_output=True)

    def stop(self, *_):
        self.log("stopping")
        ids = subprocess.run(["docker", "ps", "-q",
                              "--filter", f"label=com.docker.compose.project={self.project}",
                              "--filter", f"label=com.docker.compose.service=runner-{self.n}"],
                             capture_output=True, text=True).stdout.split()
        if ids:
            subprocess.run(["docker", "stop", "-t", "20", *ids], capture_output=True)
        if self.child:
            try:
                self.child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.child.kill()
        # An ephemeral runner removes itself after its job; one stopped before
        # or during its job would linger offline.
        if self.runner_id and self.repo and not deregister(self.repo, self.runner_id):
            self.log(f"could not deregister runner {self.runner_id}; GitHub drops it when it stays offline")
        raise SystemExit(0)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        backoff = 5
        while True:
            if outdated():
                self.log(f"ci-flow {install.installed_version()} is installed; restarting on it")
                raise SystemExit(0)
            try:
                meta = self.meta()
            except (OSError, ValueError) as e:
                self.log(f"no project files ({e}); run `ci-flow apply`")
                time.sleep(60)
                continue
            slot = meta["slots"].get(str(self.n))
            if slot is None:
                self.log("this slot is no longer in the config; idle until the unit is disabled")
                time.sleep(60)
                continue
            self.repo = meta["repo"]
            if not build_image(self.project, self.dir, self.log) and image_age(engine.image_of(self.project)) is None:
                self.log(f"no runner image; retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            if subprocess.run(self.compose("up", "-d", "--wait", f"dind-{self.n}"), capture_output=True).returncode:
                self.log(f"dind-{self.n} did not start; retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            self.clean_dind()

            name = runner_name(HOST, self.project, self.n, int(time.time()))
            # A stop signal between registering and recording the runner's id
            # would leave a registration nobody deregisters, so hold signals
            # until the id is recorded.
            signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
            try:
                resp = engine.gh_api(f"repos/{self.repo}/actions/runners/generate-jitconfig", method="POST",
                                     body={"name": name, "runner_group_id": 1, "labels": slot["labels"],
                                           "work_folder": "_work"})
                if resp and "encoded_jit_config" in resp:
                    self.runner_id = resp.get("runner", {}).get("id")
            finally:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
            if not resp or "encoded_jit_config" not in resp:
                self.log(f"could not create a JIT runner config; retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            backoff = 5
            self.log(f"waiting for a job as {name} ({', '.join(slot['labels'])})")
            env = dict(os.environ, RUNNER_JITCONFIG=resp["encoded_jit_config"])
            self.child = subprocess.Popen(self.compose("run", "--rm", "--no-deps", "-T", "-e", "RUNNER_JITCONFIG",
                                                       f"runner-{self.n}"), env=env)
            status = self.child.wait()
            self.child = None
            self.log(f"runner exited ({status})")
            if status and self.runner_id:
                # A runner that failed before or during its job may still be
                # registered; one that ran its job has removed itself.
                deregister(self.repo, self.runner_id, wait=0)
            self.runner_id = None
            if status:
                time.sleep(5)


def main(instance):
    try:
        slot = Slot(instance)
    except ValueError as e:
        sys.exit(f"ci-flow runner-loop: {e}")
    install.hold()
    slot.run()
