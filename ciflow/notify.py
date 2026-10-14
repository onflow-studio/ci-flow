"""Push notifications for the health badge.

The dashboards already show the badge: runner down, docker unreachable, disk
or memory filling up, too hot, on battery. With `"notify"` in the machine
config, ci-flowd also sends a push notification when a problem appears and
when it clears:

    "notify": {"ntfy": "https://ntfy.sh/<a topic only you know>", "level": "crit"}

`level` is "crit" (default: only red problems) or "warn" (yellow ones too).
A problem must show in two checks in a row before it is sent, and be gone
for two checks in a row before it is resolved, so neither a blip nor a value
hovering at a threshold pages you over and over. A problem already sent is
sent again when it changes: another runner goes down, or warn turns crit.
"""
import re
import threading
import urllib.parse
import urllib.request

LEVELS = ("warn", "crit")
CHECK_EVERY = 30


class NotifyError(Exception):
    pass


def settings(conf):
    """The notify settings from the machine config, or None when there are none."""
    n = conf.get("notify")
    if n is None:
        return None
    if not isinstance(n, dict) or not isinstance(n.get("ntfy"), str) \
            or not re.match(r"https?://[^\s/]+/\S+$", n["ntfy"]):
        raise NotifyError('notify must be {"ntfy": "https://<server>/<topic>", "level": "crit"}')
    level = n.get("level", "crit")
    if level not in LEVELS:
        raise NotifyError(f"notify level must be one of {', '.join(LEVELS)}")
    return {"ntfy": n["ntfy"], "level": level}


def redact(url):
    """The ntfy URL without its topic, which is the secret, for logs and output."""
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.hostname or ''}/…"


def key(issue):
    """'disk 91%' and 'disk 92%' are the same problem."""
    return re.sub(r"[\d.]+", "#", issue["text"].split(":")[0]).strip()


def shape(issue):
    """What makes a sent problem worth sending again: its severity and its text
    without the numbers that move on their own (91% -> 92%, 95°C -> 96°C)."""
    return issue.get("level"), re.sub(r"\d+(\.\d+)?", "#", issue["text"])


def post_ntfy(url, title, body, priority):
    req = urllib.request.Request(url, data=body.encode(), method="POST",
                                 headers={"Title": title, "Priority": priority, "Tags": "computer"})
    with urllib.request.urlopen(req, timeout=10):
        pass


class Notifier:
    def __init__(self, url, level="crit", host="ci", send=post_ntfy):
        self.url = url
        self.levels = LEVELS[LEVELS.index(level):]
        self.host = host
        self.send = send
        self.seen = {}     # key -> consecutive checks seen
        self.active = {}   # key -> (issue text, shape) for problems already sent
        self.gone = {}     # key -> consecutive checks an active problem was absent
        self.changed = {}  # key -> (new shape, consecutive checks) for a sent problem that changed

    def update(self, issues):
        """Feed the badge's issues from one check. Returns what was sent."""
        now = {key(i): i for i in issues if i.get("level") in self.levels}
        sent = []
        for k in list(self.seen):
            if k not in now:
                del self.seen[k]
        for k, i in now.items():
            self.seen[k] = self.seen.get(k, 0) + 1
            if self.seen[k] < 2:
                continue
            if k in self.active:
                if self.active[k][1] == shape(i):
                    self.changed.pop(k, None)
                    continue
                prev = self.changed.get(k)
                n = prev[1] + 1 if prev and prev[0] == shape(i) else 1
                self.changed[k] = (shape(i), n)
                if n < 2:  # a change has to hold for two checks too
                    continue
                del self.changed[k]
            self.active[k] = (i["text"], shape(i))
            sent.append(self._send(f"{self.host}: {i['text']}", i["text"],
                                   "high" if i["level"] == "crit" else "default"))
        for k in list(self.active):
            if k in now:
                self.gone.pop(k, None)
                continue
            self.gone[k] = self.gone.get(k, 0) + 1
            if self.gone[k] >= 2:
                del self.gone[k]
                self.changed.pop(k, None)
                text = self.active.pop(k)[0]
                sent.append(self._send(f"{self.host}: resolved", f"resolved: {text}", "low"))
        return sent

    def _send(self, title, body, priority):
        try:
            self.send(self.url, title, body, priority)
        except OSError:
            pass  # try again only when the problem comes back
        return title


def watch(source, notifier, stopped, every=CHECK_EVERY):
    """Check the snapshot's health every `every` seconds until `stopped` is set."""
    def loop():
        while not stopped.wait(every):
            try:
                view = source.snapshot().get("view") or {}
                notifier.update((view.get("health") or {}).get("issues") or [])
            except Exception:  # keep the collector alive
                pass
    threading.Thread(target=loop, daemon=True).start()
