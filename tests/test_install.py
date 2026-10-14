import contextlib
import fcntl
import io
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ciflow import cli, config, engine, install, loop  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def make_tree(d, body="print(1)\n"):
    os.makedirs(os.path.join(d, "ciflow", "__pycache__"))
    os.makedirs(os.path.join(d, "systemd"))
    for name in ("ci-flow", "ci-flowd", "cimon"):
        with open(os.path.join(d, name), "w") as f:
            f.write("#!/usr/bin/env python3\n")
    with open(os.path.join(d, "ciflow", "mod.py"), "w") as f:
        f.write(body)
    with open(os.path.join(d, "ciflow", "__pycache__", "mod.cpython-314.pyc"), "w") as f:
        f.write("junk")
    with open(os.path.join(d, "systemd", "ci-flowd.service"), "w") as f:
        f.write("ExecStart=@CIFLOWD@\nEnvironment=PATH=@PATH@\n")
    with open(os.path.join(d, "README.md"), "w") as f:
        f.write("not installed")


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "src")
        self.home = os.path.join(self.tmp.name, "home")
        make_tree(self.src)
        p = mock.patch.object(install, "describe", lambda src: None)
        p.start()
        self.addCleanup(p.stop)

    def test_copies_code_only_and_points_current_at_it(self):
        ver, changed = install.install_code(self.src, self.home)
        self.assertTrue(changed)
        cur = install.current(self.home)
        self.assertEqual(os.readlink(cur), ver)
        self.assertEqual(sorted(install.files(cur)),
                         ["ci-flow", "ci-flowd", "ciflow/mod.py", "cimon", "systemd/ci-flowd.service"])
        self.assertFalse(os.path.exists(os.path.join(cur, "README.md")))

    def test_same_code_is_the_same_version(self):
        v1, _ = install.install_code(self.src, self.home)
        v2, changed = install.install_code(self.src, self.home)
        self.assertEqual(v1, v2)
        self.assertFalse(changed)

    def test_version_includes_git_describe(self):
        with mock.patch.object(install, "describe", lambda src: "abc1234-dirty"):
            self.assertRegex(install.version(self.src), r"^abc1234-dirty-[0-9a-f]{10}$")

    def test_new_code_swaps_current_and_keeps_two_versions(self):
        versions = []
        for i in range(4):
            with open(os.path.join(self.src, "ciflow", "mod.py"), "w") as f:
                f.write(f"print({i})\n")
            ver, changed = install.install_code(self.src, self.home)
            self.assertTrue(changed)
            versions.append(ver)
            os.utime(os.path.join(install.lib_dir(self.home), ver), (time.time() + i, time.time() + i))
        install.prune(self.home)
        left = sorted(n for n in os.listdir(install.lib_dir(self.home)) if n != "current")
        self.assertEqual(left, sorted(versions[-2:]))
        self.assertEqual(install.installed_version(self.home), versions[-1])

    def test_prune_never_removes_current(self):
        v1, _ = install.install_code(self.src, self.home)
        with open(os.path.join(self.src, "ciflow", "mod.py"), "w") as f:
            f.write("print(2)\n")
        v2, _ = install.install_code(self.src, self.home)
        # Roll back to v1 while it is the oldest.
        os.utime(os.path.join(install.lib_dir(self.home), v1), (0, 0))
        os.symlink(v1, os.path.join(install.lib_dir(self.home), ".c"))
        os.replace(os.path.join(install.lib_dir(self.home), ".c"), install.current(self.home))
        install.prune(self.home, keep=1)
        self.assertTrue(os.path.isdir(os.path.join(install.lib_dir(self.home), v1)))
        self.assertFalse(os.path.isdir(os.path.join(install.lib_dir(self.home), v2)))

    def test_prune_never_removes_a_version_a_process_holds(self):
        v1, _ = install.install_code(self.src, self.home)
        old = os.path.join(install.lib_dir(self.home), v1)
        fd = os.open(old, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_SH)  # what hold() does in a running loop
        for i in range(3):
            with open(os.path.join(self.src, "ciflow", "mod.py"), "w") as f:
                f.write(f"print({i + 10})\n")
            install.install_code(self.src, self.home)
        self.assertTrue(os.path.isdir(old))
        fcntl.flock(fd, fcntl.LOCK_UN)
        install.prune(self.home)
        self.assertFalse(os.path.isdir(old))

    def test_hold_only_an_installed_copy(self):
        self.assertFalse(install.hold(self.src, self.home))
        install.install_code(self.src, self.home)
        root = os.path.realpath(install.current(self.home))
        self.addCleanup(lambda: [os.close(install._held.pop()) for _ in list(install._held)])
        self.assertTrue(install.hold(root, self.home))
        self.assertTrue(install.in_use(root))

    def test_is_installed(self):
        install.install_code(self.src, self.home)
        self.assertTrue(install.is_installed(install.current(self.home), self.home))
        self.assertFalse(install.is_installed(self.src, self.home))

    def test_link_bin_points_through_current(self):
        install.install_code(self.src, self.home)
        bin_dir = os.path.join(self.tmp.name, "bin")
        install.link_bin(self.home, bin_dir)
        for name in ("cimon", "ci-flow"):
            self.assertEqual(os.readlink(os.path.join(bin_dir, name)),
                             os.path.join(install.current(self.home), name))

    def test_ciflowd_unit_runs_the_installed_copy(self):
        with mock.patch.object(install, "source_root", lambda: self.src):
            text = install.ciflowd_unit(self.home)
        self.assertIn(f"ExecStart={os.path.join(install.current(self.home), 'ci-flowd')}", text)
        self.assertNotIn("@", text)


class Outdated(unittest.TestCase):
    def test_only_an_installed_copy_that_is_not_current_is_outdated(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            make_tree(src)
            with mock.patch.object(install, "describe", lambda s: None):
                v1, _ = install.install_code(src, d)
                with open(os.path.join(src, "ciflow", "mod.py"), "w") as f:
                    f.write("print(2)\n")
                old_root = os.path.join(install.lib_dir(d), v1)
                self.assertFalse(loop.outdated(old_root, d))
                install.install_code(src, d)
            self.assertTrue(loop.outdated(old_root, d))
            self.assertFalse(loop.outdated(install.current(d), d))
            self.assertFalse(loop.outdated(src, d))


class Deregister(unittest.TestCase):
    def test_retries_while_github_still_has_the_runner(self):
        calls = []

        def api(path, method="GET", body=None):
            calls.append(method)
            if method == "DELETE":
                return None if calls.count("DELETE") < 3 else {}
            return {"id": 7, "busy": True}

        with mock.patch.object(engine, "gh_api", api):
            self.assertTrue(loop.deregister("o/r", 7, wait=60, sleep=lambda s: None))
        self.assertEqual(calls.count("DELETE"), 3)

    def test_a_runner_already_gone_counts_as_removed(self):
        with mock.patch.object(engine, "gh_api", lambda path, method="GET", body=None: None):
            self.assertTrue(loop.deregister("o/r", 7, wait=60, sleep=lambda s: None))

    def test_gives_up_after_the_wait(self):
        api = lambda path, method="GET", body=None: None if method == "DELETE" else {"id": 7}  # noqa: E731
        with mock.patch.object(engine, "gh_api", api):
            self.assertFalse(loop.deregister("o/r", 7, wait=0, sleep=lambda s: None))


class Registration(unittest.TestCase):
    def test_stop_signals_are_held_while_registering(self):
        with tempfile.TemporaryDirectory() as d:
            pdir = os.path.join(d, "projects", "ciflow-web")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "project.json"), "w") as f:
                json.dump({"repo": "acme/web", "slots": {"1": {"labels": ["self-hosted"]}}}, f)
            slot = loop.Slot("ciflow-web-1", home=d)
            seen = {}

            def api(path, method="GET", body=None):
                seen["blocked"] = signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, [])
                return None

            def stop_loop(*_):
                raise KeyboardInterrupt

            with mock.patch.object(engine, "gh_api", api), \
                    mock.patch.object(loop, "outdated", lambda: False), \
                    mock.patch.object(loop, "build_image", lambda *a, **k: True), \
                    mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)), \
                    mock.patch("signal.signal"), \
                    mock.patch.object(loop.time, "sleep", stop_loop), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    slot.run()
            self.assertTrue(seen["blocked"])
            self.assertNotIn(signal.SIGTERM, signal.pthread_sigmask(signal.SIG_BLOCK, []))

    def test_outdated_loop_exits_between_jobs(self):
        slot = loop.Slot("ciflow-web-1", home="/nonexistent")
        with mock.patch.object(loop, "outdated", lambda: True), mock.patch("signal.signal"), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                slot.run()
        self.assertEqual(cm.exception.code, 0)


class ConfigAndGitHub(unittest.TestCase):
    def test_same_repo_twice_is_rejected(self):
        conf = {"repos": [{"repo": "acme/web"}, {"repo": "Acme/Web", "project": "ciflow-web2"}]}
        with self.assertRaisesRegex(config.ConfigError, "listed twice"):
            config.managed(conf)

    def test_profile_branch_is_url_encoded(self):
        paths = []
        with mock.patch.object(engine, "gh_api", lambda path, method="GET", body=None: paths.append(path)):
            engine.GitHub().profile("acme/web", "feat/a b&c")
        self.assertTrue(paths[0].endswith("?ref=feat%2Fa%20b%26c"), paths[0])

    def test_unit_stop_timeout_covers_the_stop_path(self):
        text = engine.unit("/x/ci-flow")
        timeout = int(text.split("TimeoutStopSec=")[1].split()[0])
        self.assertGreaterEqual(timeout, 20 + 30 + loop.DEREGISTER_FOR + 10)


class FakeGitHub:
    def __init__(self, profile=None):
        self.text = profile

    def repo(self, repo):
        return {"private": True, "default_branch": "main"}

    def profile(self, repo, branch):
        return self.text

    def fork_settings(self, repo, public):
        return {}


class CliCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = os.path.join(self.tmp.name, "config.json")
        self.home = os.path.join(self.tmp.name, "home")
        self.units = os.path.join(self.tmp.name, "units")
        self.run_dir = os.path.join(self.tmp.name, "run")
        os.makedirs(self.run_dir)
        for p in (mock.patch.object(engine, "HOME", self.home), mock.patch.object(cli, "UNIT_DIR", self.units),
                  mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.run_dir}),
                  mock.patch.object(cli, "units_of", lambda p: set()),
                  mock.patch.object(cli.System, "run", lambda self, cmd, check=False: True)):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv, gh=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--config", self.conf, *argv], gh=gh or FakeGitHub())
        return out.getvalue()

    def render(self, *repos):
        with open(self.conf, "w") as f:
            json.dump({"repos": [{"repo": r} for r in repos]}, f)
        os.makedirs(self.units, exist_ok=True)
        with open(os.path.join(self.units, engine.UNIT_FILE), "w") as f:
            f.write("unit")
        for r in repos:
            project = config.project_of(r)
            os.makedirs(engine.project_dir(project, self.home))
            with open(loop.lock_path(project), "w"):
                pass

    def test_removing_the_last_repo_removes_the_template_and_leftovers(self):
        self.render("acme/web", "acme/api")
        self.run_cli("remove", "acme/web")
        self.assertTrue(os.path.exists(os.path.join(self.units, engine.UNIT_FILE)))
        self.assertFalse(os.path.exists(loop.lock_path("ciflow-web")))
        self.run_cli("remove", "acme/api")
        self.assertFalse(os.path.exists(os.path.join(self.units, engine.UNIT_FILE)))
        self.assertFalse(os.path.exists(os.path.join(self.home, "projects")))
        self.assertFalse(os.path.exists(loop.lock_path("ciflow-api")))

    def test_dry_run_remove_keeps_everything(self):
        self.render("acme/web")
        out = self.run_cli("remove", "acme/web", "--dry-run")
        self.assertIn("would remove", out)
        self.assertTrue(os.path.exists(os.path.join(self.units, engine.UNIT_FILE)))
        self.assertTrue(os.path.exists(loop.lock_path("ciflow-web")))

    def test_status_reports_profile_drift(self):
        self.render("acme/web")
        prof, _ = engine.parse_profile('apt = ["jq"]')
        with open(os.path.join(engine.project_dir("ciflow-web", self.home), "project.json"), "w") as f:
            json.dump({"profile": prof}, f)
        [e] = config.managed({"repos": [{"repo": "acme/web"}]})
        self.assertEqual(cli.profile_drift(e, FakeGitHub('apt = ["jq"]'), self.home), ".github/ci-flow.toml on main")
        self.assertIn("changed since the last apply", cli.profile_drift(e, FakeGitHub('apt = ["git"]'), self.home))
        self.assertIn("changed", cli.profile_drift(e, FakeGitHub(None), self.home))
        # An inline profile is what counts once the config has one.
        [e] = config.managed({"repos": [{"repo": "acme/web", "profile": {"apt": ["jq"]}}]})
        self.assertEqual(cli.profile_drift(e, FakeGitHub('apt = ["git"]'), self.home), "inline in the config")
        [e] = config.managed({"repos": [{"repo": "acme/web", "profile": {"apt": ["git"]}}]})
        self.assertIn("inline in the config, changed", cli.profile_drift(e, FakeGitHub(None), self.home))


class InlineProfile(unittest.TestCase):
    def test_checked_like_the_repo_file(self):
        [e] = config.managed({"repos": [{"repo": "acme/web", "profile": {
            "apt": ["postgresql-client"], "tools": ["docker-compose"], "caches": ["npm"], "node_heap": 3072}}]})
        self.assertEqual(e["profile"], {"apt": ["postgresql-client"], "tools": ["docker-compose"],
                                        "caches": ["npm"], "node_heap": 3072})
        same, _ = engine.parse_profile('apt = ["postgresql-client"]\ntools = ["docker-compose"]\n'
                                       'caches = ["npm"]\nnode_heap = 3072\n')
        self.assertEqual(e["profile"], same)

    def test_bad_inline_profile_is_a_config_error(self):
        for bad in ({"apt": ["ok", "x; rm -rf /"]}, {"tools": ["curl"]}, {"caches": ["maven"]},
                    {"node_heap": "big"}, {"apt": "jq"}, {"extra": 1}, ["apt"]):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.managed({"repos": [{"repo": "acme/web", "profile": bad}]})

    def test_repos_without_one_have_none(self):
        [e] = config.managed({"repos": [{"repo": "acme/web"}]})
        self.assertIsNone(e["profile"])


class ProfilePrecedence(unittest.TestCase):
    def plan(self, entry, repo_file, preview=None):
        [e] = config.managed({"repos": [entry]})
        gh = FakeGitHub(repo_file)
        with mock.patch.object(gh, "profile", wraps=gh.profile) as fetch:
            p = cli.plan_repo(e, gh, preview)
        return p, fetch

    def test_inline_wins_over_the_repo_file(self):
        p, fetch = self.plan({"repo": "acme/web", "profile": {"apt": ["jq"]}}, 'apt = ["git"]')
        self.assertEqual(p["profile_source"], "inline in the config")
        self.assertIn("jq", p["files"]["Dockerfile"])
        self.assertNotIn(" git ", p["files"]["Dockerfile"])
        fetch.assert_not_called()

    def test_repo_file_when_no_inline_profile(self):
        p, _ = self.plan({"repo": "acme/web"}, 'apt = ["git"]')
        self.assertEqual(p["profile_source"], ".github/ci-flow.toml on main")
        self.assertIn("git", p["files"]["Dockerfile"])

    def test_defaults_when_neither(self):
        p, _ = self.plan({"repo": "acme/web"}, None)
        self.assertEqual(p["profile_source"], "defaults (no profile in the repo)")

    def test_preview_file_wins_over_both(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('apt = ["curl"]\n')
        self.addCleanup(os.unlink, f.name)
        p, _ = self.plan({"repo": "acme/web", "profile": {"apt": ["jq"]}}, 'apt = ["git"]', preview=f.name)
        self.assertEqual(p["profile_source"], f.name)
        self.assertIn("curl", p["files"]["Dockerfile"])


if __name__ == "__main__":
    unittest.main()
