import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ciflow import cli, notify  # noqa: E402
from ciflow import init as machine_init  # noqa: E402


class FakeMachine:
    """A machine where `have` decides what is already set up."""

    def __init__(self, have=(), laptop=True, files=None, groups="wheel", lid=None):
        self.have = set(have)
        self.laptop = laptop
        self.files = files or {}
        self.groups = groups
        self.lid = lid or {}  # logind's effective lid settings

    def which(self, tool):
        return f"/usr/bin/{tool}" if tool in self.have else None

    def ok(self, cmd):
        joined = " ".join(cmd)
        return any(h in joined for h in self.have if " " in h or h.endswith(".timer"))

    def out(self, cmd):
        if cmd[:2] == ["id", "-nG"]:
            return self.groups
        if cmd[0] == "loginctl":
            return "yes\n" if "linger" in self.have else "no\n"
        return ""

    def read(self, path):
        return self.files.get(path)

    def exists(self, path):
        return path in self.files

    def listdir(self, path):
        return sorted(os.path.basename(f) for f in self.files if os.path.dirname(f) == path)

    def logind(self, prop):
        return self.lid.get(prop)

    def user(self):
        return "ci"

    def is_laptop(self):
        return self.laptop


class FakeGitHub:
    def __init__(self, private=True):
        self.private = private

    def repo(self, repo):
        return {"private": self.private, "default_branch": "main"}

    def profile(self, repo, branch):
        return None

    def fork_settings(self, repo, public):
        return {}


EVERYTHING = {"docker", "tailscale", "gh", "is-enabled --quiet docker", "is-enabled --quiet tailscaled",
              "tailscale status", "gh auth status", "linger", "ci-flow-prune.timer"}


def by_name(steps):
    return {s.name: s for s in steps}


class Distro(unittest.TestCase):
    def check(self, text, want):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(text)
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(machine_init.distro(f.name), want)

    def test_families(self):
        self.check('ID=ubuntu\nID_LIKE=debian\n', "ubuntu")
        self.check('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "ubuntu")
        self.check('ID=debian\n', "debian")
        self.check('ID=raspbian\nID_LIKE=debian\n', "debian")
        self.check('ID=omarchy\nID_LIKE=arch\n', "arch")
        self.check('ID="fedora"\n', "fedora")
        self.check('ID=rocky\nID_LIKE="rhel centos fedora"\n', "rhel")
        self.check('ID=almalinux\nID_LIKE="rhel centos fedora"\n', "rhel")
        self.check('ID=nixos\n', None)

    def test_missing_file(self):
        self.assertIsNone(machine_init.distro("/nonexistent/os-release"))


class Packages(unittest.TestCase):
    def test_plan_per_distro(self):
        m = FakeMachine()
        ubuntu = machine_init.step_packages(m, "ubuntu")
        self.assertEqual(ubuntu.commands, [
            ["sudo", "apt-get", "update"],
            ["sudo", "apt-get", "install", "-y", "docker.io", "docker-compose-v2", "gh"],
            machine_init.TAILSCALE_SCRIPT])
        debian = machine_init.step_packages(m, "debian")
        self.assertEqual(debian.commands[:2], [
            ["sudo", "apt-get", "update"],
            ["sudo", "apt-get", "install", "-y", "docker.io", "docker-compose", "gh"]])
        arch = machine_init.step_packages(m, "arch")
        self.assertEqual(arch.commands, [["sudo", "pacman", "-Syu", "--needed", "--noconfirm",
                                          "docker", "docker-compose", "tailscale", "github-cli"]])
        fedora = machine_init.step_packages(m, "fedora")
        self.assertEqual(fedora.commands[0], ["sudo", "dnf", "install", "-y", "moby-engine", "docker-compose", "gh"])
        rhel = machine_init.step_packages(m, "rhel")
        self.assertEqual(rhel.commands[0], machine_init.DNF_PLUGINS)
        self.assertIn("https://download.docker.com/linux/rhel/docker-ce.repo", rhel.commands[1])
        self.assertIn("https://cli.github.com/packages/rpm/gh-cli.repo", rhel.commands[2])
        self.assertEqual(rhel.commands[3], ["sudo", "dnf", "install", "-y", "docker-ce", "docker-ce-cli",
                                            "containerd.io", "docker-compose-plugin", "gh"])
        self.assertNotIn("moby-engine", sum(rhel.commands, []))
        for s in (ubuntu, debian, arch, fedora, rhel):
            self.assertTrue(s.root)
            self.assertFalse(s.done)
            self.assertTrue(all(c[0] == "sudo" or c == machine_init.TAILSCALE_SCRIPT for c in s.commands))

    def test_only_missing_tools(self):
        s = machine_init.step_packages(FakeMachine({"docker", "tailscale"}), "arch")
        self.assertEqual(s.commands, [["sudo", "pacman", "-Syu", "--needed", "--noconfirm", "github-cli"]])
        rhel = machine_init.step_packages(FakeMachine({"docker", "tailscale"}), "rhel")
        self.assertEqual(len(rhel.commands), 3)  # plugins, the gh repo, install gh: no Docker repo

    def test_unknown_distro_is_a_note(self):
        s = machine_init.step_packages(FakeMachine(), None)
        self.assertEqual(s.commands, [])
        self.assertIn("unknown distro", s.notes[0])

    def test_nothing_missing(self):
        self.assertTrue(machine_init.step_packages(FakeMachine({"docker", "tailscale", "gh"}), "apt").done)


class Plan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.units = os.path.join(self.tmp.name, "units")

    def plan(self, m, conf=None, **kw):
        return by_name(machine_init.plan(m, conf or {}, FakeGitHub(), "/c.json", self.units, pm="arch", **kw))

    def test_a_set_up_machine_plans_nothing(self):
        files = {machine_init.LOGIND_DROPIN: machine_init.LOGIND_TEXT}
        for n, t in machine_init.prune_units(docker="/usr/bin/docker").items():
            files[os.path.join(self.units, n)] = t
        m = FakeMachine(EVERYTHING, files=files, groups="wheel docker")
        with mock.patch.object(machine_init, "step_install",
                               lambda m, home=None: machine_init.Step("install", "x", True)):
            steps = machine_init.plan(m, {}, FakeGitHub(), "/c.json", self.units, pm="arch")
        out = []
        self.assertEqual(machine_init.show(steps, out.append), 0)
        self.assertTrue(all(s.done or s.done is None for s in steps), [s.name for s in steps if not s.done])

    def test_a_fresh_machine_plans_every_step(self):
        with mock.patch.object(machine_init, "step_install",
                               lambda m, home=None: machine_init.Step("install", "x", False, [["ci-flow", "install"]])):
            steps = self.plan(FakeMachine(), {"repos": [{"repo": "acme/web"}]})
        for name in ("packages", "services", "linger", "install", "lid", "prune", "apply"):
            self.assertFalse(steps[name].done, name)
            self.assertTrue(steps[name].commands, name)
        self.assertIn(["sudo", "usermod", "-aG", "docker", "ci"], steps["services"].commands)
        self.assertEqual(steps["linger"].commands, [["sudo", "loginctl", "enable-linger", "ci"]])
        # Interactive logins are never run, only explained.
        for name in ("tailscale", "gh-auth"):
            self.assertFalse(steps[name].done)
            self.assertEqual(steps[name].commands, [])
            self.assertTrue(steps[name].notes)

    def test_only_root_steps_use_sudo(self):
        steps = self.plan(FakeMachine())
        for s in steps.values():
            uses_sudo = any(c and c[0] == "sudo" for c in s.commands)
            self.assertEqual(uses_sudo, s.root and bool(s.commands), s.name)
        self.assertFalse(steps["prune"].root)

    def test_install_is_done_only_when_ci_flowd_runs_the_installed_copy(self):
        home = "/h"
        unit = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                            "systemd", "user", "ci-flowd.service")
        current = os.path.join(machine_init.install.current(home), "ci-flowd")
        with mock.patch.object(machine_init.install, "installed_version", lambda home=None: "v1"), \
                mock.patch.object(machine_init.install, "is_installed", lambda src, home=None: True):
            checkout = FakeMachine(files={unit: "[Service]\nExecStart=/home/me/Work/ci-flow/ci-flowd\n"})
            self.assertFalse(machine_init.step_install(checkout, home).done)
            installed = FakeMachine(files={unit: f"[Service]\nExecStart={current}\n"})
            self.assertTrue(machine_init.step_install(installed, home).done)
            self.assertFalse(machine_init.step_install(FakeMachine(), home).done)

    def test_lid(self):
        self.assertTrue(machine_init.step_lid(FakeMachine(laptop=False)).done)
        done = machine_init.step_lid(FakeMachine(files={machine_init.LOGIND_DROPIN: machine_init.LOGIND_TEXT}))
        self.assertTrue(done.done)
        todo = machine_init.step_lid(FakeMachine())
        self.assertFalse(todo.done)
        self.assertTrue(todo.root)
        self.assertIn(machine_init.LOGIND_DROPIN, todo.commands[0])
        self.assertEqual(todo.commands[1], ["sudo", "systemctl", "kill", "-s", "HUP", "systemd-logind"])
        self.assertIn("HandleLidSwitchExternalPower=ignore", machine_init.LOGIND_TEXT)
        # On battery the lid keeps doing what the owner chose.
        self.assertNotIn("HandleLidSwitch=", machine_init.LOGIND_TEXT)

    def test_lid_already_ignored_on_external_power(self):
        # Like a machine with its own 30-lid-ignore.conf that keeps suspend on battery.
        m = FakeMachine(lid={"HandleLidSwitch": "suspend", "HandleLidSwitchExternalPower": "ignore",
                             "HandleLidSwitchDocked": "ignore"})
        s = machine_init.step_lid(m)
        self.assertTrue(s.done)
        self.assertEqual(s.commands, [])
        self.assertIn("suspend", s.notes[0])

    def test_lid_owner_setting_is_reported_not_overridden(self):
        own = "/etc/systemd/logind.conf.d/30-lid.conf"
        m = FakeMachine(files={own: "[Login]\nHandleLidSwitchExternalPower=suspend\n"},
                        lid={"HandleLidSwitch": "suspend", "HandleLidSwitchExternalPower": "suspend"})
        s = machine_init.step_lid(m)
        self.assertIsNone(s.done)
        self.assertEqual(s.commands, [])
        self.assertIn(own, s.notes[0])
        self.assertFalse(s.root and s.commands)

    def test_lid_settings_follow_logind_order(self):
        etc, usr = "/etc/systemd/logind.conf.d", "/usr/lib/systemd/logind.conf.d"
        m = FakeMachine(files={
            "/etc/systemd/logind.conf": "[Login]\nHandleLidSwitchExternalPower=lock\n",
            f"{usr}/10-vendor.conf": "HandleLidSwitchExternalPower=suspend\n",
            f"{etc}/10-vendor.conf": "HandleLidSwitchExternalPower=poweroff\n",
            f"{etc}/20-mine.conf": "HandleLidSwitch=hibernate\n",
            machine_init.LOGIND_DROPIN: "HandleLidSwitchExternalPower=ignore\n",
        })
        found = machine_init.lid_settings(m)
        self.assertEqual(found["HandleLidSwitchExternalPower"], ("poweroff", f"{etc}/10-vendor.conf"))
        self.assertEqual(found["HandleLidSwitch"][0], "hibernate")

    def test_repos_warn_about_public_repos(self):
        s = machine_init.step_repos({"repos": [{"repo": "acme/web"}]}, FakeGitHub(private=False))
        self.assertFalse(s.done)
        self.assertTrue(any("PUBLIC" in n for n in s.notes))
        self.assertTrue(machine_init.step_repos({"repos": [{"repo": "acme/web"}]}, FakeGitHub()).done)

    def test_alerts(self):
        self.assertTrue(machine_init.step_alerts({"notify": {"ntfy": "https://ntfy.sh/x"}}).done)
        self.assertIn("ntfy", machine_init.step_alerts({}).notes[0])
        self.assertFalse(machine_init.step_alerts({"notify": {"ntfy": "nope"}}).done)

    def test_power_is_guidance(self):
        s = machine_init.step_power()
        self.assertIsNone(s.done)
        self.assertEqual(s.commands, [])


class Prune(unittest.TestCase):
    def test_never_touches_volumes_containers_or_tagged_images(self):
        for days in (1, 7, 30):
            for cmd in machine_init.prune_commands(days):
                self.assertEqual(cmd[0], "docker")
                self.assertIn(cmd[1], ("image", "builder"))
                self.assertEqual(cmd[2], "prune")
                for bad in ("-a", "--all", "volume", "volumes", "--volumes", "system", "container"):
                    self.assertNotIn(bad, cmd)
                self.assertIn(f"until={days * 24}h", cmd)

    def test_units(self):
        units = machine_init.prune_units(7, "/usr/bin/docker")
        service = units["ci-flow-prune.service"]
        execs = [l for l in service.splitlines() if l.startswith("ExecStart=")]
        self.assertEqual(execs, ["ExecStart=/usr/bin/docker image prune --force --filter until=168h",
                                 "ExecStart=/usr/bin/docker builder prune --force --filter until=168h"])
        self.assertNotIn("volume", service.replace("never volumes", ""))
        self.assertIn("OnCalendar=weekly", units["ci-flow-prune.timer"])

    def test_step_writes_units_in_its_action(self):
        with tempfile.TemporaryDirectory() as d:
            s = machine_init.step_prune(FakeMachine({"docker"}), d, 3)
            self.assertFalse(s.done)
            s.action()
            self.assertEqual(sorted(os.listdir(d)), ["ci-flow-prune.service", "ci-flow-prune.timer"])
            with open(os.path.join(d, "ci-flow-prune.service")) as f:
                self.assertIn("until=72h", f.read())


class Run(unittest.TestCase):
    def test_runs_pending_steps_and_stops_at_a_failure(self):
        ran, acted = [], []
        steps = [
            machine_init.Step("a", "done", True, [["never"]]),
            machine_init.Step("b", "todo", False, [["one"], ["two"]], action=lambda: acted.append("b")),
            machine_init.Step("c", "fails", False, [["bad"]]),
            machine_init.Step("d", "after", False, [["unreached"]]),
        ]
        ok = machine_init.run(steps, runner=lambda c: ran.append(c[0]) or c[0] != "bad", out=lambda s: None)
        self.assertFalse(ok)
        self.assertEqual(ran, ["one", "two", "bad"])
        self.assertEqual(acted, ["b"])


class InitCli(unittest.TestCase):
    def test_without_yes_only_prints_the_plan(self):
        steps = [machine_init.Step("x", "something", False, [["sudo", "touch", "/etc/x"]], root=True)]
        out = io.StringIO()
        with mock.patch.object(machine_init, "plan", lambda *a, **k: steps), \
                mock.patch.object(machine_init, "run") as run, contextlib.redirect_stdout(out):
            cli.main(["--config", "/nonexistent.json", "init"], gh=FakeGitHub())
            cli.main(["--config", "/nonexistent.json", "init", "--yes", "--dry-run"], gh=FakeGitHub())
        run.assert_not_called()
        self.assertIn("$ sudo touch /etc/x", out.getvalue())
        self.assertIn("ci-flow init --yes", out.getvalue())

    def test_yes_runs_it(self):
        steps = [machine_init.Step("x", "something", False, [["true"]])]
        with mock.patch.object(machine_init, "plan", lambda *a, **k: steps), \
                mock.patch.object(machine_init, "run", return_value=True) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--config", "/nonexistent.json", "init", "--yes"], gh=FakeGitHub())
        run.assert_called_once()


class Notify(unittest.TestCase):
    def test_settings(self):
        self.assertIsNone(notify.settings({}))
        self.assertEqual(notify.settings({"notify": {"ntfy": "https://ntfy.sh/abc"}}),
                         {"ntfy": "https://ntfy.sh/abc", "level": "crit"})
        for bad in ({"ntfy": "ntfy.sh/abc"}, {"ntfy": "https://ntfy.sh/abc", "level": "info"}, "x", {}):
            with self.subTest(bad=bad), self.assertRaises(notify.NotifyError):
                notify.settings({"notify": bad})

    def test_needs_two_checks_sends_once_and_resolves(self):
        sent = []
        n = notify.Notifier("https://ntfy.sh/t", host="box", send=lambda u, t, b, p: sent.append((t, p)))
        disk = {"level": "crit", "text": "disk 91%"}
        self.assertEqual(n.update([disk]), [])
        self.assertEqual(n.update([{"level": "crit", "text": "disk 92%"}]), ["box: disk 92%"])
        self.assertEqual(n.update([disk]), [])
        self.assertEqual(n.update([]), [])
        self.assertEqual(n.update([]), ["box: resolved"])
        self.assertEqual(sent, [("box: disk 92%", "high"), ("box: resolved", "low")])

    def test_a_problem_flapping_at_a_threshold_is_sent_once(self):
        n = notify.Notifier("u", send=lambda *a: None)
        hot = {"level": "crit", "text": "hot 95°C"}
        sent = []
        for issues in ([hot], [hot], [], [hot], [hot], [], [hot], [hot]):
            sent += n.update(issues)
        self.assertEqual(sent, ["ci: hot 95°C"])

    def test_a_sent_problem_is_sent_again_when_it_changes(self):
        n = notify.Notifier("u", "warn", host="box", send=lambda *a: None)
        one = {"level": "crit", "text": "runner down: web#1"}
        two = {"level": "crit", "text": "runner down: web#1, api#2"}
        sent = []
        for issues in ([one], [one], [two], [two], [two]):
            sent += n.update(issues)
        self.assertEqual(sent, ["box: runner down: web#1", "box: runner down: web#1, api#2"])
        # warn turning crit is a change too; a moving number is not.
        n = notify.Notifier("u", "warn", host="box", send=lambda *a: None)
        sent = []
        for issues in ([{"level": "warn", "text": "disk 85%"}], [{"level": "warn", "text": "disk 86%"}],
                       [{"level": "warn", "text": "disk 87%"}], [{"level": "crit", "text": "disk 91%"}],
                       [{"level": "crit", "text": "disk 92%"}]):
            sent += n.update(issues)
        self.assertEqual(sent, ["box: disk 86%", "box: disk 92%"])

    def test_a_change_must_hold_for_two_checks(self):
        n = notify.Notifier("u", host="box", send=lambda *a: None)
        one = {"level": "crit", "text": "runner down: web#1"}
        two = {"level": "crit", "text": "runner down: web#1, api#2"}
        sent = []
        for issues in ([one], [one], [two], [one], [two], [one]):
            sent += n.update(issues)
        self.assertEqual(sent, ["box: runner down: web#1"])

    def test_the_topic_is_kept_out_of_output(self):
        self.assertEqual(notify.redact("https://ntfy.sh/s3cret-topic"), "https://ntfy.sh/…")
        step = machine_init.step_alerts({"notify": {"ntfy": "https://ntfy.sh/s3cret-topic"}})
        self.assertNotIn("s3cret", step.what)

    def test_level_filter(self):
        n = notify.Notifier("u", "crit", send=lambda *a: None)
        warn = {"level": "warn", "text": "hot 88°C"}
        n.update([warn])
        self.assertEqual(n.update([warn]), [])
        n = notify.Notifier("u", "warn", send=lambda *a: None)
        n.update([warn])
        self.assertEqual(n.update([warn]), ["ci: hot 88°C"])

    def test_a_blip_is_not_sent(self):
        n = notify.Notifier("u", send=lambda *a: None)
        self.assertEqual(n.update([{"level": "crit", "text": "docker unreachable"}]), [])
        self.assertEqual(n.update([]), [])
        self.assertEqual(n.update([{"level": "crit", "text": "docker unreachable"}]), [])

    def test_send_failures_are_swallowed(self):
        def boom(*a):
            raise OSError("offline")
        n = notify.Notifier("u", send=boom)
        issue = {"level": "crit", "text": "on battery"}
        n.update([issue])
        self.assertEqual(n.update([issue]), ["ci: on battery"])

    def test_watch_feeds_the_snapshot_health(self):
        import threading
        got = threading.Event()
        seen = []

        class Source:
            def snapshot(self):
                return {"view": {"health": {"issues": [{"level": "crit", "text": "on battery"}]}}}

        class Recorder:
            def update(self, issues):
                seen.append(issues)
                got.set()

        stopped = threading.Event()
        notify.watch(Source(), Recorder(), stopped, every=0.01)
        self.assertTrue(got.wait(2))
        stopped.set()
        self.assertEqual(seen[0], [{"level": "crit", "text": "on battery"}])

    def test_key_ignores_numbers_and_details(self):
        self.assertEqual(notify.key({"text": "disk 91%"}), notify.key({"text": "disk 95.5%"}))
        self.assertEqual(notify.key({"text": "runner down: a#1"}), notify.key({"text": "runner down: a#1, b#2"}))


if __name__ == "__main__":
    unittest.main()
