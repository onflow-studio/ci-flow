"""Installing ci-flow's code to a fixed place.

The units must not run code from a git checkout: switching its branch would
change what live runners and the collector run on their next restart. So
`ci-flow install` (and `ci-flow apply`) copy the code into a versioned
directory, $CIFLOW_HOME/lib/<version>/, and swap the `lib/current` symlink to
it. The units, and the cimon/ci-flow links in ~/.local/bin, go through
`current`. The two most recent versions are kept, and so is any version a
running ci-flowd or runner loop holds (see hold()), so a process keeps its
files until it restarts.
"""
import fcntl
import hashlib
import os
import shutil
import subprocess

from . import engine

# What gets installed, relative to the source tree.
SCRIPTS = ("ci-flow", "ci-flowd", "cimon")
TREES = ("ciflow", "systemd")
KEEP = 2


def lib_dir(home=None):
    return os.path.join(home or engine.HOME, "lib")


def current(home=None):
    return os.path.join(lib_dir(home), "current")


def source_root():
    """The tree this code runs from: a checkout, or an installed version."""
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def is_installed(root, home=None):
    lib = os.path.realpath(lib_dir(home))
    return os.path.commonpath([os.path.realpath(root), lib]) == lib


def files(src):
    """Relative paths of the files to install, sorted."""
    out = [s for s in SCRIPTS if os.path.isfile(os.path.join(src, s))]
    for tree in TREES:
        for dirpath, dirnames, names in os.walk(os.path.join(src, tree)):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for n in names:
                if not n.endswith((".pyc", ".tmp")):
                    out.append(os.path.relpath(os.path.join(dirpath, n), src))
    return sorted(out)


def content_hash(src):
    h = hashlib.sha256()
    for rel in files(src):
        h.update(rel.encode() + b"\0")
        with open(os.path.join(src, rel), "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()[:10]


def describe(src):
    """`git describe` of a checkout, or None when src isn't one."""
    try:
        r = subprocess.run(["git", "-C", src, "describe", "--always", "--dirty"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def version(src):
    """'<git describe>-<content hash>': readable, and equal only for equal code."""
    d = describe(src)
    h = content_hash(src)
    return f"{d}-{h}" if d else h


def installed_version(home=None):
    try:
        return os.path.basename(os.readlink(current(home)))
    except OSError:
        return None


def install_code(src=None, home=None):
    """Copy src into lib/<version>/ and point lib/current at it.

    Returns (version, changed). Installing the version that is already current
    changes nothing."""
    src = src or source_root()
    lib = lib_dir(home)
    ver = version(src)
    target = os.path.join(lib, ver)
    if not os.path.isdir(target):
        tmp = target + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        for rel in files(src):
            dst = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(src, rel), dst)
        os.replace(tmp, target)
    changed = installed_version(home) != ver
    if changed:
        link = os.path.join(lib, ".current.tmp")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(ver, link)
        os.replace(link, current(home))
    prune(home)
    return ver, changed


_held = []


def hold(root=None, home=None):
    """Mark the installed version this process runs from as in use, for as
    long as the process lives, so prune() leaves it alone."""
    root = root or source_root()
    if not is_installed(root, home):
        return False
    fd = os.open(root, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)
    _held.append(fd)
    return True


def in_use(path):
    """True when a running process holds this version (see hold())."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def prune(home=None, keep=KEEP):
    """Drop all but the `keep` most recently installed versions; never the
    current one, nor one a running process holds."""
    lib = lib_dir(home)
    cur = installed_version(home)
    try:
        names = [n for n in os.listdir(lib) if not n.startswith(".") and n != "current"
                 and os.path.isdir(os.path.join(lib, n)) and not os.path.islink(os.path.join(lib, n))]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(lib, n)), reverse=True)
    kept = ([cur] if cur in names else []) + [n for n in names if n != cur]
    gone = [n for n in kept[keep:] if not n.endswith(".tmp") and not in_use(os.path.join(lib, n))]
    for n in gone:
        shutil.rmtree(os.path.join(lib, n), ignore_errors=True)
    return gone


def link_bin(home=None, bin_dir=None):
    """~/.local/bin/cimon and ci-flow -> lib/current/…"""
    bin_dir = bin_dir or os.path.expanduser("~/.local/bin")
    os.makedirs(bin_dir, exist_ok=True)
    for name in ("cimon", "ci-flow"):
        path = os.path.join(bin_dir, name)
        tmp = path + ".tmp"
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(os.path.join(current(home), name), tmp)
        os.replace(tmp, path)


def ciflowd_unit(home=None):
    """The ci-flowd unit, rendered from systemd/ci-flowd.service."""
    with open(os.path.join(source_root(), "systemd", "ci-flowd.service")) as f:
        text = f.read()
    return (text.replace("@CIFLOWD@", os.path.join(current(home), "ci-flowd"))
            .replace("@PATH@", engine.runner_path()))


def unit_runs_current(unit_text, home=None):
    """Whether a ci-flowd unit's ExecStart is the installed copy (lib/current)
    rather than a checkout."""
    want = os.path.join(current(home), "ci-flowd")
    for line in (unit_text or "").splitlines():
        if line.startswith("ExecStart="):
            return line[len("ExecStart="):].split()[0] == want
    return False
