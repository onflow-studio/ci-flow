import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ciflow import cli, collect, config, engine, gitlab, init, loop  # noqa: E402

GL = {"repo": "acme/platform/api", "forge": "gitlab", "slots": 2, "labels": ["ci-box"], "memory": "6g", "cpus": 3}


class FakeGitLab:
    def __init__(self, visibility="private"):
        self.visibility = visibility
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self

    def repo(self, repo):
        return {"id": 42, "private": self.visibility != "public", "visibility": self.visibility,
                "default_branch": "main"}


class Config(unittest.TestCase):
    def test_github_stays_the_default(self):
        [m] = config.managed({"repos": [{"repo": "acme/web"}]})
        self.assertEqual(m["forge"], {"kind": "github"})
        self.assertIsNone(m["image"])

    def test_gitlab_entry(self):
        [m] = config.managed({"repos": [GL]})
        self.assertEqual(m["forge"], {"kind": "gitlab", "url": "https://gitlab.com"})
        self.assertEqual(m["project"], "ciflow-api")
        self.assertEqual(m["image"], gitlab.DEFAULT_JOB_IMAGE)
        self.assertEqual(m["slots"][2]["memory"], "6g")

    def test_self_managed_url_and_image(self):
        [m] = config.managed({"repos": [dict(GL, gitlab_url="https://git.example.com/", image="node:22")]})
        self.assertEqual(m["forge"]["url"], "https://git.example.com")
        self.assertEqual(m["image"], "node:22")

    def test_rejects_what_does_not_apply(self):
        bad = [
            dict(GL, forge="bitbucket"),
            dict(GL, profile={"apt": ["jq"]}),
            dict(GL, node_heap=2048),
            dict(GL, slot={"1": {"dind_memory": "2g"}}),
            dict(GL, gitlab_url="ftp://x"),
            dict(GL, image="ubuntu; rm -rf /"),
            {"repo": "acme/web", "image": "node:22"},
            {"repo": "acme/web", "gitlab_url": "https://gitlab.com"},
            {"repo": "acme/a/b"},  # nested paths are GitLab's
        ]
        for e in bad:
            with self.subTest(e=e), self.assertRaises(config.ConfigError):
                config.managed({"repos": [e]})

    def test_fleets_carry_the_forge(self):
        conf = {"repos": [GL], "fleets": [{"project": "old", "unit": "old@{}.service", "forge": "gitlab",
                                           "gitlab_url": "https://git.example.com", "slots": {"1": "a/b"}}]}
        old, new = config.fleets(conf)
        self.assertEqual(old["forge"], {"kind": "gitlab", "url": "https://git.example.com"})
        self.assertEqual(new["forge"]["kind"], "gitlab")
        self.assertEqual(collect.forges_of([old, new])["acme/platform/api"]["url"], "https://gitlab.com")


class Render(unittest.TestCase):
    def setUp(self):
        [self.entry] = config.managed({"repos": [GL]})
        self.files = engine.render(self.entry, engine.parse_profile(None)[0], "none")

    def test_runner_image_is_gitlab_runner_with_its_entrypoint(self):
        self.assertIn(f"FROM {gitlab.BASE_IMAGE}", self.files["Dockerfile"])
        self.assertIn("gitlab-runner run-single", self.files["entrypoint.sh"])
        self.assertIn("--max-builds 1", self.files["entrypoint.sh"])

    def test_compose_bounds_the_daemon_and_keeps_slots_apart(self):
        c = self.files["docker-compose.yml"]
        self.assertIn("  dind-1:\n", c)
        self.assertIn("  runner-2:\n", c)
        self.assertIn('mem_limit: "6g"\n    cpus: 3\n', c)
        self.assertIn("network_mode: service:dind-2", c)
        self.assertIn('CIFLOW_GITLAB_URL: "https://gitlab.com"', c)
        self.assertIn(f'CIFLOW_JOB_IMAGE: "{gitlab.DEFAULT_JOB_IMAGE}"', c)
        self.assertNotIn("_work", c)

    def test_project_json(self):
        meta = json.loads(self.files["project.json"])
        self.assertEqual(meta["forge"]["kind"], "gitlab")
        self.assertIsNone(meta["profile"])
        self.assertEqual(meta["slots"]["1"]["labels"], config.BASE_LABELS + ["ci-box"])


class Pipelines(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(gitlab.status_of("running"), ("in_progress", None))
        self.assertEqual(gitlab.status_of("failed"), ("completed", "failure"))
        self.assertEqual(gitlab.status_of("canceled"), ("completed", "cancelled"))
        self.assertEqual(gitlab.status_of("pending"), ("queued", None))
        self.assertEqual(gitlab.status_of("manual"), ("completed", "skipped"))

    def test_merge_request_refs(self):
        self.assertEqual(gitlab.branch_of("refs/merge-requests/12/head"), "mr-12")
        self.assertEqual(gitlab.branch_of("feat/abc-1-x"), "feat/abc-1-x")

    def test_runs_and_jobs_take_the_github_shape(self):
        replies = {
            "projects/acme%2Fapi/pipelines?per_page=15": [
                {"id": 7, "status": "running", "ref": "main", "sha": "s1", "source": "push",
                 "created_at": "2026-11-16T10:00:00Z", "updated_at": "2026-11-16T10:01:00Z",
                 "web_url": "https://gitlab.com/acme/api/-/pipelines/7"}],
            "projects/acme%2Fapi/repository/commits/s1": {"title": "fix: login"},
            "projects/acme%2Fapi/pipelines/7/jobs?per_page=100": [
                {"name": "test", "status": "running", "started_at": "2026-11-16T10:00:30Z",
                 "runner": {"description": "ci-box-api-2-1"}, "web_url": "https://gitlab.com/j/1"}],
        }
        calls = []

        def api(base, path, **kw):
            calls.append(path)
            return replies.get(path)

        st = collect.State()
        with mock.patch.object(gitlab, "api", api):
            collect.collect_gitlab(st, "acme/api", "https://gitlab.com")
            collect.collect_gitlab(st, "acme/api", "https://gitlab.com")
        [r] = st.runs["acme/api"]
        self.assertEqual((r["databaseId"], r["status"], r["displayTitle"], r["headBranch"]),
                         (7, "in_progress", "fix: login", "main"))
        [j] = st.jobs[7]
        self.assertEqual((j["status"], j["runner_name"], j["steps"]), ("in_progress", "ci-box-api-2-1", []))
        self.assertEqual(calls.count("projects/acme%2Fapi/repository/commits/s1"), 1)  # cached

    def test_api_failure_is_reported_and_keeps_the_last_runs(self):
        st = collect.State()
        st.runs = {"acme/api": [{"databaseId": 1}]}
        with mock.patch.object(gitlab, "api", lambda base, path, **kw: None):
            collect.collect_gitlab(st, "acme/api", "https://gitlab.com")
        self.assertIn("GitLab", st.gh_err["acme/api"])
        self.assertEqual(st.runs["acme/api"], [{"databaseId": 1}])


class Token(unittest.TestCase):
    def test_env_wins_then_the_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "env")
            with open(path, "w") as f:
                f.write("GH_TOKEN=x\nexport GITLAB_TOKEN='glpat-file'\n")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(gitlab.token(path), "glpat-file")
            with mock.patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-env"}):
                self.assertEqual(gitlab.token(path), "glpat-env")
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(gitlab.token(os.path.join(d, "missing")))


class Runners(unittest.TestCase):
    def test_runners_for(self):
        self.assertIsInstance(loop.runners_for({"repo": "a/b"}), loop.GitHubRunners)
        r = loop.runners_for({"repo": "a/b", "forge": {"kind": "gitlab", "url": "https://gitlab.com"}})
        self.assertIsInstance(r, loop.GitLabRunners)

    def test_register_looks_up_the_project_once(self):
        calls = []

        def api(base, path, method="GET", data=None, tok=None, timeout=20):
            calls.append((method, path, data))
            if path.startswith("projects/"):
                return {"id": 42, "visibility": "private"}
            return {"id": 9, "token": "glrt-x"}

        r = loop.GitLabRunners("acme/api", "https://gitlab.com")
        with mock.patch.object(gitlab, "api", api):
            first = r.register("ci-box-api-1-1", ["self-hosted", "ci-box"])
            r.register("ci-box-api-1-2", ["self-hosted", "ci-box"])
        self.assertEqual(first, ("glrt-x", {"CIFLOW_RUNNER_TOKEN": "glrt-x", "CIFLOW_RUNNER_NAME": "ci-box-api-1-1"}))
        self.assertEqual(sum(p.startswith("projects/") for _, p, _ in calls), 1)
        post = calls[1][2]
        self.assertEqual((post["project_id"], post["tag_list"], post["run_untagged"]),
                         (42, "self-hosted,ci-box", "false"))

    def test_deregister_with_the_runner_token_and_gone_counts_as_removed(self):
        seen = []

        def api(base, path, method="GET", data=None, tok=None, timeout=20):
            seen.append((method, path, tok))
            return None  # DELETE fails, verify says the token is unknown

        with mock.patch.object(gitlab, "api", api):
            self.assertTrue(gitlab.deregister("https://gitlab.com", "glrt-x"))
        self.assertEqual(seen[0], ("DELETE", "runners", ""))

    def test_slot_removes_its_gitlab_runner_after_a_good_job(self):
        with tempfile.TemporaryDirectory() as d:
            pdir = os.path.join(d, "projects", "ciflow-api")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "project.json"), "w") as f:
                json.dump({"repo": "acme/api", "forge": {"kind": "gitlab", "url": "https://gitlab.com"},
                           "slots": {"1": {"labels": ["self-hosted"]}}}, f)
            slot = loop.Slot("ciflow-api-1", home=d)
            removed, popen = [], []
            fake = mock.Mock()
            fake.register.return_value = ("glrt-x", {"CIFLOW_RUNNER_TOKEN": "glrt-x"})
            fake.remove_after_job = True
            fake.deregister.side_effect = lambda h, wait=0: removed.append(h) or True
            rounds = iter([False, KeyboardInterrupt])

            def outdated():
                v = next(rounds)
                if v is KeyboardInterrupt:
                    raise KeyboardInterrupt
                return v

            def make_popen(cmd, env=None):
                popen.append((cmd, env))
                return mock.Mock(wait=lambda: 0)

            with mock.patch.object(loop, "runners_for", lambda meta: fake), \
                    mock.patch.object(loop, "outdated", outdated), \
                    mock.patch.object(loop, "build_image", lambda *a, **k: True), \
                    mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)), \
                    mock.patch("subprocess.Popen", make_popen), \
                    mock.patch("signal.signal"), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    slot.run()
            self.assertEqual(removed, ["glrt-x"])
            cmd, env = popen[0]
            self.assertIn("CIFLOW_RUNNER_TOKEN", cmd)
            self.assertNotIn("glrt-x", cmd)  # the token goes through the environment
            self.assertEqual(env["CIFLOW_RUNNER_TOKEN"], "glrt-x")


class Cli(unittest.TestCase):
    def run_cli(self, argv, gl):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main(argv, gh=mock.Mock(), gl=gl)
        return out.getvalue(), err.getvalue()

    def test_add_gitlab(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            gl = FakeGitLab("public")
            with mock.patch.object(gitlab, "token", lambda: "t"):
                out, err = self.run_cli(["--config", path, "add", "acme/platform/api", "--forge", "gitlab",
                                         "--gitlab-url", "https://git.example.com", "--runners", "2",
                                         "--labels", "ci-box"], gl)
            with open(path) as f:
                [e] = json.load(f)["repos"]
            self.assertEqual(e, {"repo": "acme/platform/api", "slots": 2, "forge": "gitlab",
                                 "gitlab_url": "https://git.example.com", "labels": ["ci-box"]})
            self.assertEqual(gl.urls, ["https://git.example.com"])
            self.assertIn("PUBLIC", err)
            self.assertIn("tags:", out)

    def test_gitlab_options_need_the_gitlab_forge(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            self.run_cli(["--config", os.path.join(d, "c.json"), "add", "acme/web", "--image", "node:22"],
                         FakeGitLab())

    def test_apply_dry_run_renders_a_gitlab_project(self):
        out = []
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "UNIT_DIR", home), \
                mock.patch.object(cli, "install_code", lambda *a, **k: False), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.apply({"repos": [GL]}, mock.Mock(), cli.System(dry_run=True), home=home,
                      out=out.append, gl=FakeGitLab())
        text = "\n".join(out)
        self.assertIn("profile: none (GitLab jobs run in their own images)", text)
        self.assertIn("ciflow-api/docker-compose.yml", text)

    def test_profile_preview_is_refused_for_gitlab(self):
        [e] = config.managed({"repos": [GL]})
        with self.assertRaises(SystemExit):
            cli.read_profile(e, mock.Mock(), None, "/tmp/x.toml")


class Init(unittest.TestCase):
    def test_token_step_only_with_gitlab_repos(self):
        self.assertIsNone(init.step_gitlab_token({"repos": [{"repo": "acme/web"}]}))
        with mock.patch.object(gitlab, "token", lambda: None):
            step = init.step_gitlab_token({"repos": [GL]})
        self.assertFalse(step.done)
        self.assertIn("GITLAB_TOKEN", step.notes[0])

    def test_repos_step_asks_gitlab(self):
        step = init.step_repos({"repos": [GL]}, mock.Mock(), FakeGitLab("public"))
        self.assertIn("PUBLIC", step.notes[0])


if __name__ == "__main__":
    unittest.main()
