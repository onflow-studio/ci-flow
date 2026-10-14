"""cimon: live terminal dashboard for a self-hosted CI machine.

Shows the machine's health, what each self-hosted runner is doing, a live
pipeline per running workflow (one chip per job) and recent results per repo.
Reads snapshots from the ci-flowd collector when it runs, otherwise collects
in-process.

Keys: q quit · r refresh now
"""
import json
import os
import re
import select
import shutil
import signal
import sys
import termios
import time
import tty
import urllib.request

from .collect import SNAPSHOT_VERSION, Collector, ConfigError, load_fleets
from .model import RECENT, derive, job_order, short, slot_of, ticket  # noqa: F401 (re-exported)

COLLECTOR_URL = os.environ.get("CIFLOW_URL", "http://127.0.0.1:7340").rstrip("/")
TICK = 1.0
RETRY = 10.0  # seconds between attempts to reach the collector while in local mode

# ── ANSI ─────────────────────────────────────────────────────────────────────
ESC = "\x1b["
RST = ESC + "0m"
B = ESC + "1m"
DIM = ESC + "2m"
RED, GRN, YEL, BLU, MAG, CYN, GRY = (ESC + c for c in ("31m", "32m", "33m", "34m", "35m", "36m", "90m"))
BG_GRN, BG_YEL, BG_RED = (ESC + c for c in ("42;30m", "43;30m", "41;97m"))
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def vlen(s):
    return len(ANSI_RE.sub("", s))


def fit(s, w):
    """Truncate/pad a string with ANSI codes to exactly w visible columns."""
    out, n, i = [], 0, 0
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        if n >= w:
            break
        out.append(s[i])
        n += 1
        i += 1
    return "".join(out) + " " * (w - n) + RST


def level(pct, warn=70, crit=90):
    return RED if pct >= crit else YEL if pct >= warn else GRN


def bar(pct, width=20, warn=70, crit=90, color=None):
    pct = max(0.0, min(100.0, pct))
    full = pct * width / 100
    n = int(full)
    part = " ▏▎▍▌▋▊▉"[int((full - n) * 8)] if n < width else ""
    body = (("█" * n) + part).rstrip()
    return (color or level(pct, warn, crit)) + body + GRY + "·" * (width - len(body)) + RST


SPARK = "▁▂▃▄▅▆▇█"


def spark(vals, hi=100.0):
    return "".join(SPARK[min(7, int(v / hi * 7.99))] for v in vals)


def human(n, unit="B"):
    for u in ("", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}{u}{unit}" if u in ("", "K") else f"{n:.1f}{u}{unit}"
        n /= 1024


def ago(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}"
    return f"{s // 86400}d{s % 86400 // 3600}h"


# ── Rendering ────────────────────────────────────────────────────────────────
def section(title, w, extra=""):
    t = f"{B}{CYN}▌{title}{RST}"
    if extra:
        t += f"  {extra}"
    return fit(t, w)


def run_icon(r):
    if r["status"] == "completed":
        return {"success": GRN + "✔", "failure": RED + "✖", "cancelled": GRY + "⊘",
                "skipped": GRY + "»", "startup_failure": RED + "✖"}.get(r["conclusion"], YEL + "?") + RST
    if r["status"] == "in_progress":
        return YEL + "▶" + RST
    return MAG + "◌" + RST


def job_chip(j):
    name = j["label"]
    if j["status"] == "completed":
        c = j["conclusion"]
        if c == "success":
            return f"{GRN}✔ {name}{RST}"
        if c == "failure":
            return f"{BG_RED} ✖ {name} {RST}"
        if c == "skipped":
            return f"{GRY}{DIM}» {name}{RST}"
        return f"{GRY}⊘ {name}{RST}"
    if j["status"] == "in_progress":
        prog = bar(j["steps_done"] / j["steps_total"] * 100, 5, color=YEL) if j["steps_total"] else ""
        where = f"{GRY}@{j['slot']}{RST}" if j["slot"] else ""
        return f"{B}{YEL}▶ {name}{RST} {prog}{where}"
    return f"{GRY}◌ {name}{RST}"


def wrap_chips(chips, w, indent):
    lines, cur = [], indent
    sep = f" {GRY}·{RST} "
    for ch in chips:
        piece = ch if cur == indent else sep + ch
        if vlen(cur) + vlen(piece) > w and cur != indent:
            lines.append(cur)
            cur = indent + ch
        else:
            cur += piece
    lines.append(cur)
    return lines


LEVEL = {"crit": RED, "warn": YEL}


def render(snap, w, h, source="local"):
    L = []
    now = time.time()
    v = derive(snap)
    hostname = snap["hostname"]
    hs = snap["host"]
    ac, bat, t = hs["ac"], hs["bat"], hs["temp"]
    cpu, ncpu, load = hs["cpu"], hs["ncpu"], hs["load"]
    mu, mt = hs["mem"]["used"], hs["mem"]["total"]
    su, stot = hs["mem"]["swap_used"], hs["mem"]["swap_total"]
    mp, dp = v["machine"]["mem_pct"], v["machine"]["disk_pct"]
    lp = v["machine"]["load_per_core"] * 100

    def since(ts):
        return ago(now - (ts or now))

    issues = v["health"]["issues"]
    if not issues:
        badge = f"{BG_GRN} ● ALL GOOD {RST}"
    else:
        badge = f"{BG_RED if v['health']['level'] == 'crit' else BG_YEL} ▲ {issues[0]['text'].upper()} {RST}"
        if len(issues) > 1:
            badge += " " + "  ".join(f"{LEVEL[i['level']]}{i['text']}{RST}" for i in issues[1:])

    pw = "" if ac is None else f"{GRY}AC{RST}" if ac else f"{RED}{B}BATTERY{RST}"
    if bat is not None:
        pw += f" {level(100 - bat, 50, 80)}{bat}%{RST}"
    L.append(fit(f"{B}{MAG} ◆ cimon{RST} {B}{hostname}{RST}   {badge}", w - 30)
             + fit(f"{pw}  {GRY}up {ago(hs['uptime'])}{RST}  {B}{time.strftime('%H:%M:%S')}{RST}", 30))

    # Machine
    L.append("")
    L.append(section("MACHINE", w))
    meters = [  # label, percent, warn, crit, text after the percentage
        ("CPU ", cpu, 70, 90, f"{GRY}{spark(hs['cpu_hist'][-12:])}{RST}"),
        ("MEM ", mp, 85, 95, f"{GRY}{human(mu)}/{human(mt)}{RST}"),
        ("DISK", dp, 80, 90, f"{GRY}{human(hs['disk']['free'])} free{RST}"),
    ]
    others = [
        f"{B}LOAD{RST} {level(lp, 100, 200)}{load[0] / ncpu:.1f}×{RST} {GRY}per core{RST}",
        f"{B}TEMP{RST} " + (f"{level(t, 85, 95)}{t:.0f}°C{RST}" if t else f"{GRY}-{RST}"),
        f"{B}NET {RST} {GRN}↓{RST}{human(hs['net'][0])}/s {BLU}↑{RST}{human(hs['net'][1])}/s"
        + (f"  {GRY}swap {human(su)}{RST}" if stot else ""),
    ]

    def meter(label, pct, warn, crit, tail, bw):
        return f"{B}{label}{RST} {bar(pct, bw, warn, crit)} {level(pct, warn, crit)}{pct:3.0f}%{RST} {tail}"

    # Three columns when every cell fits with a bar of at least 8 and a 2-column gap.
    # The text beside each bar is sized for its widest value ("999.9GB/999.9GB")
    # so bars keep their width as the numbers change.
    cw = (w - 2) // 3
    text = max(26, *(vlen(meter(*m, 0)) for m in meters))
    bw = min(24, cw - 2 - text)
    if w >= 110 and bw >= 8 and all(vlen(c) <= cw - 2 for c in others):
        L.append(fit("  " + "".join(fit(meter(*m, bw), cw) for m in meters), w))
        L.append(fit("  " + "".join(fit(c, cw) for c in others), w))
    else:
        bw = max(6, min(30, w // 3, w - 2 - text))
        L.extend(fit("  " + c, w) for c in [meter(*m, bw) for m in meters] + others)

    # Runners: one row per repo, one chip per slot.
    c = v["counts"]
    L.append("")
    queued, runs_queued = c["jobs_not_started"], c["runs_queued"]
    qs = f"  {YEL}{queued} job{'s' if queued != 1 else ''} not started{RST}" if queued else ""
    if runs_queued:
        qs += f" {GRY}·{RST} {MAG}{runs_queued} run{'s' if runs_queued != 1 else ''} queued{RST}"
    L.append(section("RUNNERS", w, f"{B}{c['busy']}{RST}/{c['slots']} busy{qs}"))
    sw = max(18, min(34, (w - 20) // 4))
    for row in v["slots"]:
        chips = []
        for sl in row["slots"]:
            n = sl["n"]
            if sl["state"] == "busy":
                chip = f"{BG_GRN} {n} {RST} {B}{short(sl['job'], sw - 12)}{RST} {GRY}{since(sl['started_at'])}{RST}"
            elif sl["state"] == "down":
                chip = f"{BG_RED} {n} {RST} {RED}{sl['unit'] or 'not installed'}{RST}"
            elif sl["state"] == "idle":
                chip = f"{GRY}{B} {n} {RST}{GRY} idle{RST}"
            else:
                chip = f"{YEL}{B} {n} {RST}{YEL} starting{RST}"
            chips.append(fit(chip, sw))
        L.append(fit(f"  {B}{short(row['name'], 16):<16}{RST} " + "".join(chips), w))

    def run_line(r, when, pc, lead="", indent="  ", with_repo=True):
        right = f" {bar(r['pct'], 24, color=pc)} {pc}{r['pct']:3.0f}%{RST}  " + fit(when, 14)
        where = f"{B}{short(r['name'], 14):<14}{RST} " if with_repo else ""
        left = (f"{indent}{lead}{run_icon(r)} {where}{CYN}{short(r['workflow'], 12):<12}{RST}"
                f" {BLU}{r['ticket']:<9}{RST} {r['title']}")
        return fit(left, w - vlen(right)) + right

    def chip_rows(js, indent):
        chips = [job_chip(j) for j in js]
        return [fit(x, w) for x in wrap_chips(chips, w, indent)] if chips else []

    n_running = sum(len(g["runs"]) for g in v["running"])
    L.append("")
    L.append(section("RUNNING", w, f"{GRY}{n_running} pipeline{'s' if n_running != 1 else ''}{RST}"))
    if not v["running"]:
        L.append(fit(f"  {GRY}nothing running{RST}", w))
    for gi, g in enumerate(v["running"]):
        if gi:
            L.append("")
        L.append(fit(f"  {B}{MAG}■ {g['name']}{RST}  {GRY}{len(g['runs'])} running{RST}", w))
        for i, r in enumerate(g["runs"]):
            if i:
                L.append("")
            L.append(run_line(r, f"{YEL}{since(r['created_at'])}{RST}", YEL, indent="    ", with_repo=False))
            L.extend(chip_rows(r["jobs"], "        "))

    L.append("")
    L.append(section("QUEUE", w, f"{GRY}{len(v['queue'])} waiting · oldest first{RST}"))
    if not v["queue"]:
        L.append(fit(f"  {GRY}empty{RST}", w))
    for i, r in enumerate(v["queue"], 1):
        L.append(run_line(r, f"{MAG}waiting {since(r['created_at'])}{RST}", MAG, lead=f"{GRY}{i:>2}.{RST} "))

    # Recent results: a strip per repo, newest first.
    L.append("")
    L.append(section("RECENT", w, f"{GRY}last {RECENT} per repo · newest first{RST}"))
    wf_w, ref_w, ago_w = 14, 10, 10

    def cell(r, color):
        return (f"{color}{fit(short(r['workflow'], wf_w), wf_w)} {BLU}{fit(short(r['ticket'], ref_w), ref_w)} "
                f"{GRY}{since(r['updated_at']) + ' ago':>{ago_w}}{RST}")

    last_w = 2 + wf_w + 1 + ref_w + 1 + ago_w
    head = (f"  {'repo':<16} {'last ' + str(RECENT):<{RECENT * 2}} {'last run':<{last_w}}   last fail")
    L.append(fit(f"{GRY}{head}{RST}", w))
    for row in v["recent"]:
        last, fail = row["last"], row["last_fail"]
        if row["error"]:
            tail = f"{RED}{row['error']}{RST}"
        elif row["loading"]:
            tail = f"{GRY}loading…{RST}"
        elif not last:
            tail = f"{GRY}no runs{RST}"
        else:
            tail = f"{run_icon(last)} " + cell(last, "")
            if fail:
                tail += f"   {RED}✖{RST} " + cell(fail, RED)
        strip = " ".join(run_icon(r) for r in row["runs"])
        L.append(fit(f"  {B}{short(row['name'], 16):<16}{RST} {fit(strip, RECENT * 2)} {tail}", w))

    foot = fit(f"{GRY} q quit · r refresh   │ source: {source}   │ logs: journalctl --user -u '*-ci-runner@*' -f{RST}", w)
    L = [fit(x, w) for x in L[:h - 1]]
    L += [" " * w] * (h - 1 - len(L))
    L.append(foot)
    return L


# ── Sources ──────────────────────────────────────────────────────────────────
def fetch_snapshot(url=COLLECTOR_URL, timeout=1.0):
    """The collector's snapshot, or None when it isn't reachable."""
    try:
        with urllib.request.urlopen(f"{url}/api/snapshot", timeout=timeout) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def request_refresh(url=COLLECTOR_URL):
    try:
        urllib.request.urlopen(urllib.request.Request(f"{url}/api/refresh", data=b"", method="POST"),
                               timeout=1.0).close()
    except OSError:
        pass


def local_collector():
    try:
        return Collector(load_fleets())
    except ConfigError as e:
        sys.exit(f"cimon: {e}")


class Source:
    """Where snapshots come from: the collector when it answers with a snapshot
    this cimon understands, otherwise an in-process collector. While local, the
    collector is retried every `retry` seconds and used again once it answers."""

    def __init__(self, url=COLLECTOR_URL, fetch=fetch_snapshot, make_local=local_collector,
                 retry=RETRY, clock=time.monotonic):
        self.url, self.fetch, self.make_local, self.retry, self.clock = url, fetch, make_local, retry, clock
        self.local = None
        self.note = ""
        self.last_try = None

    def try_collector(self):
        self.last_try = self.clock()
        snap = self.fetch(self.url)
        if snap is None:
            self.note = "collector unreachable"
            return None
        version = snap.get("version") if isinstance(snap, dict) else None
        if version != SNAPSHOT_VERSION:
            self.note = f"collector snapshot v{version}, cimon needs v{SNAPSHOT_VERSION}"
            return None
        return snap

    def get(self):
        """(snapshot, label for the footer)."""
        if self.local is None or self.clock() - self.last_try >= self.retry:
            snap = self.try_collector()
            if snap is not None:
                if self.local is not None:
                    self.local.stop()
                    self.local = None
                return snap, "collector"
            if self.local is None:
                self.local = self.make_local()
                self.local.start()
        return self.local.snapshot(), f"local ({self.note})"

    def refresh(self):
        if self.local is not None:
            self.local.refresh()
        else:
            request_refresh(self.url)


def main():
    if "--once" in sys.argv:
        src = Source()
        snap, source = src.try_collector(), "collector"
        if snap is None:
            c = local_collector()
            c.collect_once()
            snap, source = c.snapshot(), f"local ({src.note})"
        w, h = shutil.get_terminal_size((160, 60))
        *lines, foot = render(snap, w, 400, source)
        while lines and not ANSI_RE.sub("", lines[-1]).strip():
            lines.pop()
        print("\n".join(lines + ["", foot]))
        return

    src = Source()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    size = None
    try:
        tty.setcbreak(fd)
        while True:
            snap, source = src.get()
            w, h = shutil.get_terminal_size()
            if (w, h) != size:
                out.write("\x1b[2J")
                size = (w, h)
            out.write("\x1b[H" + "\n".join(render(snap, w, h, source)))
            out.flush()
            r, _, _ = select.select([fd], [], [], TICK)
            if r:
                k = os.read(fd, 8).decode(errors="ignore").lower()
                if "q" in k:
                    break
                if "r" in k:
                    src.refresh()
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()
