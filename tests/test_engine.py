import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ciflow import cli, collect, config, engine, install, loop  # noqa: E402

WEBAPP = {
    "repo": "acme/web-app",
    "slots": 4,
    "labels": ["acme", "general"],
    "memory": "3584m", "cpus": 4, "node_heap": 3072,
    "dind_memory": "2560m", "dind_cpus": 2,
    "slot": {"1": {"labels": ["acme", "build"], "memory": "8g", "cpus": 8, "node_heap": 6144}},
}
PROFILE = """
apt = ["postgresql-client", "fonts-liberation"]
tools = ["docker-compose"]
caches = ["pnpm", "playwright"]
node_heap = 2048
"""


class FakeGitHub:
    def __init__(self, private=True, profile=None, fork=None):
        self.private = private
        self.text = profile
        self.fork = fork
        self.profile_refs = []

    def repo(self, repo):
        return {"private": self.private, "default_branch": "main"}

    def profile(self, repo, branch):
        self.profile_refs.append(branch)
        return self.text

    def fork_settings(self, repo, public):
        return self.fork


class Config(unittest.TestCase):
    def test_slots_resolve_defaults_repo_settings_and_overrides(self):
        [m] = config.managed({"repos": [WEBAPP]})
        self.assertEqual(m["project"], "ciflow-web-app")
        self.assertEqual(sorted(m["slots"]), [1, 2, 3, 4])
        self.assertEqual(m["slots"][1], {"labels": ["acme", "build"], "memory": "8g", "cpus": 8,
                                         "node_heap": 6144, "dind_memory": "2560m", "dind_cpus": 2})
        self.assertEqual(m["slots"][3]["labels"], ["acme", "general"])
        self.assertEqual(m["slots"][3]["memory"], "3584m")

    def test_minimal_repo_gets_defaults(self):
        [m] = config.managed({"repos": [{"repo": "acme/web"}]})
        self.assertEqual(m["slots"], {1: dict(config.DEFAULTS)})

    def test_fleets_merge_watch_only_and_managed(self):
        conf = {"fleets": [{"project": "old-ci", "unit": "old@{}.service", "slots": {"1": "acme/api"}}],
                "repos": [{"repo": "acme/web", "slots": 2}]}
        fl = config.fleets(conf)
        self.assertEqual(fl[0], {"project": "old-ci", "unit": "old@{}.service", "slots": {1: "acme/api"}})
        self.assertEqual(fl[1], {"project": "ciflow-web", "unit": "ci-flow-runner@ciflow-web-{}.service",
                                 "slots": {1: "acme/web", 2: "acme/web"}})

    def test_rejects_bad_entries(self):
        bad = [
            {"repo": "not-a-repo"},
            {"repo": "acme/web", "slots": 0},
            {"repo": "acme/web", "slots": True},
            {"repo": "acme/web", "memroy": "4g"},
            {"repo": "acme/web", "labels": ["ok", "no spaces"]},
            {"repo": "acme/web", "memory": "4 gigs"},
            {"repo": "acme/web", "cpus": -1},
            {"repo": "acme/web", "slot": {"3": {"cpus": 2}}},
            {"repo": "acme/web", "slot": {"1": {"repo": "x/y"}}},
            {"repo": "acme/web", "project": "Bad Name"},
            {"repo": "acme/web", "project": "other-ci"},
            {"repo": "acme/web", "project": "ciflow-"},
        ]
        for e in bad:
            with self.subTest(e=e), self.assertRaises(config.ConfigError):
                config.managed({"repos": [e]})

    def test_project_names_must_be_distinct(self):
        with self.assertRaises(config.ConfigError):
            config.managed({"repos": [{"repo": "a/web"}, {"repo": "b/web"}]})
        config.managed({"repos": [{"repo": "a/web"}, {"repo": "b/web", "project": "ciflow-b-web"}]})

    def test_load_accepts_watch_only_config_and_load_fleets_uses_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w") as f:
                json.dump({"fleets": [{"project": "p", "unit": "u@{}.service", "slots": {"1": "a/b"}}]}, f)
            self.assertEqual(collect.load_fleets(path), [{"project": "p", "unit": "u@{}.service", "slots": {1: "a/b"}}])
            with open(path, "w") as f:
                json.dump({"other": 1}, f)
            with self.assertRaises(config.ConfigError):
                config.load(path)


class Profile(unittest.TestCase):
    def test_missing_profile_is_defaults(self):
        self.assertEqual(engine.parse_profile(None), ({"apt": [], "tools": [], "caches": [], "node_heap": None}, []))

    def test_parses_known_keys(self):
        prof, warns = engine.parse_profile(PROFILE)
        self.assertEqual(warns, [])
        self.assertEqual(prof, {"apt": ["postgresql-client", "fonts-liberation"], "tools": ["docker-compose"],
                                "caches": ["pnpm", "playwright"], "node_heap": 2048})

    def test_drops_bad_entries_with_warnings(self):
        prof, warns = engine.parse_profile(
            'apt = ["ok-pkg", "evil; rm -rf /"]\ntools = ["kubectl"]\ncaches = ["/etc"]\nnode_heap = "big"\nextra = 1\n')
        self.assertEqual(prof, {"apt": ["ok-pkg"], "tools": [], "caches": [], "node_heap": None})
        self.assertEqual(len(warns), 5)

    def test_invalid_toml_falls_back_to_defaults(self):
        prof, warns = engine.parse_profile("apt = [")
        self.assertEqual(prof["apt"], [])
        self.assertIn("not valid TOML", warns[0])


class Render(unittest.TestCase):
    def setUp(self):
        [self.entry] = config.managed({"repos": [WEBAPP]})
        self.prof, _ = engine.parse_profile(PROFILE)
        self.files = engine.render(self.entry, self.prof)

    def test_files(self):
        self.assertEqual(sorted(self.files), ["Dockerfile", "docker-compose.yml", "entrypoint.sh", "project.json"])

    def test_dockerfile(self):
        df = self.files["Dockerfile"]
        self.assertIn(f"FROM {engine.BASE_IMAGE}", df)
        self.assertIn("install -y --no-install-recommends xz-utils zstd postgresql-client fonts-liberation", df)
        self.assertIn("cli-plugins/docker-compose", df)
        self.assertIn("/home/runner/.local/share/pnpm/store /home/runner/.cache/ms-playwright", df)
        self.assertIn("chown -R runner:runner /home/runner/_work /home/runner/externals-shared "
                      "/home/runner/.cache /home/runner/.local", df)

    def test_dockerfile_header_names_the_profile_source(self):
        self.assertIn("# Profile: defaults.", self.files["Dockerfile"])
        df = engine.render(self.entry, self.prof, "inline in the config")["Dockerfile"]
        self.assertIn("# Profile: inline in the config.", df)
        self.assertNotIn(".github/ci-flow.toml", df)
        df = engine.render(self.entry, self.prof, ".github/ci-flow.toml on main")["Dockerfile"]
        self.assertIn("# Profile: .github/ci-flow.toml on main.", df)

    def test_compose_slots_limits_and_isolation(self):
        c = self.files["docker-compose.yml"]
        self.assertIn("name: ciflow-web-app\n", c)
        for n in range(1, 5):
            self.assertIn(f"  dind-{n}:\n", c)
            self.assertIn(f"  runner-{n}:\n", c)
            self.assertIn(f"    network_mode: service:dind-{n}\n", c)
            self.assertIn(f"      - docker-{n}:/var/lib/docker\n", c)
        runner1 = c.split("  runner-1:\n")[1].split("  dind-2:\n")[0]
        self.assertIn('NODE_OPTIONS: "--max-old-space-size=6144"', runner1)
        self.assertIn('mem_limit: "8g"', runner1)
        self.assertIn("cpus: 8", runner1)
        runner2 = c.split("  runner-2:\n")[1].split("  dind-3:\n")[0]
        self.assertIn('NODE_OPTIONS: "--max-old-space-size=3072"', runner2)
        self.assertIn('mem_limit: "3584m"', runner2)
        self.assertIn("      - cache-pnpm:/home/runner/.local/share/pnpm/store\n", runner2)
        self.assertIn('CIFLOW_CACHES: "/home/runner/.local/share/pnpm/store /home/runner/.cache/ms-playwright"',
                      runner2)
        self.assertIn('    mem_limit: "2560m"\n', c.split("  dind-1:\n")[1])
        self.assertIn("  cache-playwright:\n", c.split("volumes:\n")[-1])

    def test_profile_heap_applies_when_config_leaves_it_out(self):
        [e] = config.managed({"repos": [{"repo": "acme/web"}]})
        c = engine.render(e, self.prof)["docker-compose.yml"]
        self.assertIn('NODE_OPTIONS: "--max-old-space-size=2048"', c)
        c = engine.render(e, engine.parse_profile(None)[0])["docker-compose.yml"]
        self.assertNotIn("NODE_OPTIONS", c)

    def test_project_json_labels(self):
        meta = json.loads(self.files["project.json"])
        self.assertEqual(meta["slots"]["1"]["labels"], ["self-hosted", "linux", "x64", "acme", "build"])
        self.assertEqual(meta["slots"]["4"]["labels"], ["self-hosted", "linux", "x64", "acme", "general"])
        self.assertEqual(meta["image"], "ciflow-web-app-runner:latest")


class Security(unittest.TestCase):
    def test_private_repo_with_safe_fork_settings(self):
        self.assertEqual(engine.security_warnings("a/b", {"private": True},
                                                  {"run_workflows_from_fork_pull_requests": False}), [])

    def test_private_repo_running_fork_prs_without_approval(self):
        w = engine.security_warnings("a/b", {"private": True}, {"run_workflows_from_fork_pull_requests": True,
                                                               "require_approval_for_fork_pr_workflows": False})
        self.assertEqual(len(w), 1)

    def test_public_repo_always_warns(self):
        w = engine.security_warnings("a/b", {"private": False}, {"approval_policy": "all_external_contributors"})
        self.assertEqual(len(w), 1)
        self.assertIn("PUBLIC", w[0])
        w = engine.security_warnings("a/b", {"private": False}, {"approval_policy": "first_time_contributors"})
        self.assertEqual(len(w), 2)
        w = engine.security_warnings("a/b", {"private": False}, None)
        self.assertEqual(len(w), 2)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = os.path.join(self.tmp.name, "config.json")
        self.home = os.path.join(self.tmp.name, "home")
        self.units = os.path.join(self.tmp.name, "units")
        patches = [mock.patch.object(engine, "HOME", self.home), mock.patch.object(cli, "UNIT_DIR", self.units)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv, gh=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main(["--config", self.conf, *argv], gh=gh or FakeGitHub(profile=PROFILE))
        return out.getvalue(), err.getvalue()

    def test_add_writes_config_and_keeps_fleets(self):
        with open(self.conf, "w") as f:
            json.dump({"fleets": [{"project": "p", "unit": "u@{}.service", "slots": {"1": "x/y"}}]}, f)
        out, _ = self.run_cli("add", "acme/web", "--runners", "2", "--labels", "big,fast")
        with open(self.conf) as f:
            conf = json.load(f)
        self.assertEqual(conf["repos"], [{"repo": "acme/web", "slots": 2, "labels": ["big", "fast"]}])
        self.assertEqual(len(conf["fleets"]), 1)
        self.assertIn("ci-flow apply", out)
        with self.assertRaises(SystemExit):
            self.run_cli("add", "acme/web")

    def test_add_warns_about_public_repos(self):
        _, err = self.run_cli("add", "acme/web", gh=FakeGitHub(private=False, fork=None))
        self.assertIn("PUBLIC", err)

    def test_apply_dry_run_changes_nothing(self):
        self.run_cli("add", "acme/web", "--runners", "2")
        with mock.patch("subprocess.run") as run, mock.patch.object(loop, "build_image") as build, \
                mock.patch.object(install, "version", lambda src: "v1"):
            out, _ = self.run_cli("apply", "--dry-run")
        run.assert_not_called()
        build.assert_not_called()
        self.assertFalse(os.path.exists(self.home))
        self.assertFalse(os.path.exists(self.units))
        self.assertIn("ciflow-web (2 slots)", out)
        self.assertIn("+name: ciflow-web", out)
        self.assertIn("would run: systemctl --user enable --now ci-flow-runner@ciflow-web-2.service", out)
        self.assertIn("would build ciflow-web-runner:latest", out)
        self.assertIn("would install ci-flow v1", out)

    def test_apply_reads_profile_from_default_branch(self):
        self.run_cli("add", "acme/web")
        gh = FakeGitHub(profile=PROFILE)
        self.run_cli("apply", "--dry-run", gh=gh)
        self.assertEqual(gh.profile_refs, ["main"])

    def test_apply_writes_files_and_starts_units(self):
        self.run_cli("add", "acme/web", "--runners", "2")
        calls = []
        with mock.patch.object(cli.System, "run", lambda self, cmd, check=False: calls.append(cmd) or True), \
                mock.patch.object(cli.System, "build", lambda self, p, d: calls.append(["build", p]) or True), \
                mock.patch.object(cli, "units_of", lambda p: {1, 2, 3}), \
                mock.patch.object(loop, "image_age", lambda image: None):
            self.run_cli("apply")
        d = os.path.join(self.home, "projects", "ciflow-web")
        self.assertEqual(sorted(os.listdir(d)), ["Dockerfile", "docker-compose.yml", "entrypoint.sh", "project.json"])
        self.assertTrue(os.access(os.path.join(d, "entrypoint.sh"), os.X_OK))
        with open(os.path.join(self.units, engine.UNIT_FILE)) as f:
            self.assertIn("runner-loop %i", f.read())
        self.assertIn(["build", "ciflow-web"], calls)
        self.assertIn(["systemctl", "--user", "enable", "--now", "ci-flow-runner@ciflow-web-1.service"], calls)
        self.assertIn(["systemctl", "--user", "disable", "--now", "ci-flow-runner@ciflow-web-3.service"], calls)

        # A second apply with nothing changed rewrites nothing and builds nothing.
        calls.clear()
        with mock.patch.object(cli.System, "run", lambda self, cmd, check=False: calls.append(cmd) or True), \
                mock.patch.object(cli.System, "build", lambda self, p, d: calls.append(["build", p]) or True), \
                mock.patch.object(cli, "units_of", lambda p: {1, 2}), \
                mock.patch.object(loop, "image_age", lambda image: 60.0):
            out, _ = self.run_cli("apply")
        self.assertNotIn(["build", "ciflow-web"], calls)
        self.assertNotIn("update", out)

    def test_apply_restarts_ciflowd_only_when_it_runs_the_install(self):
        self.run_cli("add", "acme/web")
        unit = os.path.join(self.units, "ci-flowd.service")
        os.makedirs(self.units, exist_ok=True)

        def apply_with(exec_start):
            with open(unit, "w") as f:
                f.write(f"[Service]\nExecStart={exec_start}\n")
            calls = []
            with mock.patch.object(cli.System, "run", lambda self, cmd, check=False: calls.append(cmd) or True), \
                    mock.patch.object(cli.System, "build", lambda self, p, d: True), \
                    mock.patch.object(cli, "install_code", lambda system, home=None, out=None: True), \
                    mock.patch.object(cli, "units_of", lambda p: {1}), \
                    mock.patch.object(loop, "image_age", lambda image: 60.0):
                self.run_cli("apply")
            return ["systemctl", "--user", "try-restart", "ci-flowd.service"] in calls

        self.assertFalse(apply_with("/home/me/Work/ci-flow/ci-flowd"))
        self.assertTrue(apply_with(os.path.join(install.current(self.home), "ci-flowd")))

    def test_profile_override_must_name_a_managed_repo(self):
        self.run_cli("add", "acme/web")
        with self.assertRaises(SystemExit):
            self.run_cli("apply", "--dry-run", "--profile", "other/repo=/dev/null")


class Loop(unittest.TestCase):
    def test_parse_instance(self):
        self.assertEqual(loop.parse_instance("ciflow-web-app-2"), ("ciflow-web-app", 2))
        for bad in ("ciflow", "ciflow-x", "-3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                loop.parse_instance(bad)

    def test_build_image_goes_by_the_last_build_not_the_image_date(self):
        # A fully cached rebuild keeps the image's old Created time; the stamp
        # must stop the loop from rebuilding before every job.
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d}), \
                mock.patch.object(loop, "image_age", lambda image: 10 * loop.IMAGE_MAX_AGE), \
                mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(loop.build_image("ciflow-web", d, lambda m: None))
            self.assertTrue(loop.build_image("ciflow-web", d, lambda m: None))
            self.assertEqual(run.call_count, 1)
            os.utime(os.path.join(d, ".image-built"), (0, 0))
            self.assertTrue(loop.build_image("ciflow-web", d, lambda m: None))
            self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class RunnerName(unittest.TestCase):
    def test_project_keeps_slots_of_different_repos_apart(self):
        a = loop.runner_name("ci-box", "ciflow-web-app", 2, 1791995614)
        b = loop.runner_name("ci-box", "ciflow-api", 2, 1791995614)
        self.assertEqual(a, "ci-box-web-app-2-1791995614")
        self.assertNotEqual(a, b)

    def test_fits_githubs_limit_and_keeps_the_slot_readable(self):
        from ciflow.model import slot_of
        name = loop.runner_name("ci-box", "ciflow-" + "x" * 80, 3, 1791995614)
        self.assertLessEqual(len(name), loop.NAME_MAX)
        self.assertEqual(slot_of("ci-box", name), "3")
        self.assertEqual(slot_of("ci-box", loop.runner_name("ci-box", "ciflow-web", 12, 1)), "12")
