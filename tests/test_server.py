import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ciflow import collect, config  # noqa: E402
from ciflow.collect import Collector  # noqa: E402
from ciflow.server import make_server  # noqa: E402
from ciflow.tui import fetch_snapshot  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "snapshot.json")


class FakeSource:
    def __init__(self):
        with open(FIXTURE) as f:
            self.snap = json.load(f)
        self.refreshed = threading.Event()

    def snapshot(self):
        return self.snap

    def refresh(self):
        self.refreshed.set()


class Server(unittest.TestCase):
    def setUp(self):
        self.source = FakeSource()
        self.server = make_server(self.source, port=0, interval=0.05)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def test_snapshot(self):
        status, ctype, body = self.get("/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(json.loads(body), self.source.snap)

    def test_events_stream_snapshots(self):
        with urllib.request.urlopen(self.url + "/api/events", timeout=5) as r:
            self.assertEqual(r.headers.get("Content-Type"), "text/event-stream")
            events = []
            while len(events) < 2:
                line = r.readline().decode()
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        self.assertEqual(events[0]["hostname"], "ci-box")

    def test_refresh_wakes_collectors(self):
        req = urllib.request.Request(self.url + "/api/refresh", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 202)
        self.assertTrue(self.source.refreshed.wait(1))

    def test_index_and_not_found(self):
        status, ctype, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html; charset=utf-8")
        self.assertIn(b"ci-flow", body)
        self.assertIn(b'EventSource(api("events"))', body)
        self.assertIn(b'api("refresh")', body)
        self.assertNotIn(b'"/api/', body)  # relative, so the page works under a path prefix
        self.assertNotRegex(body, rb"<script[^>]+src=|<link[^>]+href=\"http")  # no external assets
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/nope")
        self.assertEqual(e.exception.code, 404)
        e.exception.close()

    def test_whoami_reads_tailscale_identity_headers(self):
        req = urllib.request.Request(self.url + "/api/whoami", headers={
            "Tailscale-User-Login": "ana@example.com",
            "Tailscale-User-Name": "Ana Núñez".encode().decode("latin-1")})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(json.load(r), {"login": "ana@example.com", "name": "Ana Núñez"})
        status, _, body = self.get("/api/whoami")
        self.assertEqual(json.loads(body), {"login": None, "name": None})

    def test_cimon_reads_collector(self):
        self.assertEqual(fetch_snapshot(self.url), self.source.snap)

    def test_cimon_falls_back_when_collector_is_down(self):
        self.assertIsNone(fetch_snapshot("http://127.0.0.1:9", timeout=0.5))


class Snapshot(unittest.TestCase):
    def test_collector_snapshot_is_json_and_keeps_only_live_jobs(self):
        fleets = [{"project": "acme-ci", "unit": "acme-ci-runner@{}.service", "slots": {1: "acme/web"}}]
        c = Collector(fleets)
        run = {"workflowName": "CI", "displayTitle": "t", "headBranch": "main", "conclusion": "",
               "createdAt": "2026-10-14T13:00:00Z", "updatedAt": "2026-10-14T13:00:00Z"}
        c.st.runs = {"acme/web": [
            {**run, "databaseId": 1, "status": "in_progress"},
            {**run, "databaseId": 2, "status": "completed"},
        ]}
        job = {"status": "queued", "conclusion": None, "started_at": None, "runner_name": "", "steps": []}
        c.st.jobs = {1: [{**job, "name": "a"}], 2: [{**job, "name": "b"}]}
        c.st.units = {"acme-ci/1": "active"}
        c.st.slots_up = {"acme-ci/1"}
        snap = json.loads(json.dumps(c.snapshot()))
        self.assertEqual([j["name"] for j in snap["jobs"]["1"]], ["a"])
        self.assertNotIn("2", snap["jobs"])
        self.assertEqual(snap["view"]["running"][0]["runs"][0]["id"], 1)
        self.assertEqual(snap["view"]["queue"], [])
        self.assertEqual(snap["fleets"][0]["slots"], {"1": "acme/web"})
        self.assertEqual(snap["slots_up"], ["acme-ci/1"])
        for key in ("cpu", "cpu_hist", "net", "mem", "disk", "load", "ncpu", "temp", "ac", "bat", "uptime"):
            self.assertIn(key, snap["host"])

    def test_snapshot_still_served_when_the_view_cannot_be_derived(self):
        c = Collector([{"project": "acme-ci", "unit": "acme-ci-runner@{}.service", "slots": {1: "acme/web"}}])
        c.st.runs = {"acme/web": [{"databaseId": 1, "status": "in_progress"}]}
        snap = c.snapshot()
        self.assertIsNone(snap["view"])
        self.assertIn("KeyError", snap["view_error"])

    def test_collect_repo_drops_jobs_of_finished_runs(self):
        st = collect.State()
        st.jobs = {1: [{"name": "a"}], 2: [{"name": "b"}]}
        runs = [{"databaseId": 1, "status": "completed"}, {"databaseId": 2, "status": "in_progress"}]
        replies = {"run": json.dumps(runs), "api": json.dumps({"jobs": [{"name": "c"}]})}
        with mock.patch.object(collect, "sh", lambda cmd, timeout=10: replies[cmd[1]]):
            collect.collect_repo(st, "acme/web")
        self.assertNotIn(1, st.jobs)
        self.assertEqual(st.jobs[2][0]["name"], "c")

    def test_collect_repo_reports_invalid_gh_json(self):
        st = collect.State()
        st.runs = {"acme/web": [{"databaseId": 1, "status": "completed"}]}
        with mock.patch.object(collect, "sh", lambda cmd, timeout=10: "<html>rate limited</html>"):
            collect.collect_repo(st, "acme/web")
        self.assertIn("invalid JSON", st.gh_err["acme/web"])
        self.assertEqual(st.runs["acme/web"][0]["databaseId"], 1)

    def test_collect_repo_keeps_jobs_when_jobs_reply_is_invalid(self):
        st = collect.State()
        st.jobs = {2: [{"name": "old"}]}
        runs = [{"databaseId": 2, "status": "in_progress"}]
        replies = {"run": json.dumps(runs), "api": "not json"}
        with mock.patch.object(collect, "sh", lambda cmd, timeout=10: replies[cmd[1]]):
            collect.collect_repo(st, "acme/web")
        self.assertEqual(st.jobs[2], [{"name": "old"}])
        self.assertNotIn("acme/web", st.gh_err)

    def test_collector_rereads_a_changed_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")

            def write(conf, ns):
                with open(path, "w") as f:
                    json.dump(conf, f)
                os.utime(path, ns=(ns, ns))

            one = {"repos": [{"repo": "acme/web", "slots": 1}]}
            write(one, 1_000_000_000)
            logs = []
            c = Collector(config.fleets(config.load(path)), config_path=path, log=logs.append)
            c.st.runs = {"acme/web": []}
            self.assertFalse(c.reload_config())  # unchanged file

            write({"repos": [{"repo": "acme/api", "slots": 2}]}, 2_000_000_000)
            self.assertTrue(c.reload_config())
            self.assertEqual(c.repos, ["acme/api"])
            self.assertEqual(c.fleets[0]["slots"], {1: "acme/api", 2: "acme/api"})
            self.assertNotIn("acme/web", c.st.runs)
            self.assertEqual(c.snapshot()["repos"], ["acme/api"])

            with open(path, "w") as f:
                f.write("{not json")
            os.utime(path, ns=(3_000_000_000, 3_000_000_000))
            self.assertFalse(c.reload_config())  # a broken file keeps the last good config
            self.assertEqual(c.repos, ["acme/api"])
            self.assertIn("keeping the last good one", logs[-1])

    def test_stop_ends_collector_loops(self):
        c = Collector([])
        calls = []
        c.every(0.01, lambda: calls.append(1))
        time.sleep(0.05)
        c.stop()
        time.sleep(0.05)
        n = len(calls)
        time.sleep(0.05)
        self.assertGreater(n, 0)
        self.assertEqual(len(calls), n)


class Install(unittest.TestCase):
    """install.sh against a throwaway home with stub tools."""

    def run_install(self, with_shims):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home)
        bin_dir = os.path.join(home, "tools", "gh-2.0", "bin")
        os.makedirs(bin_dir)
        for tool in ("gh", "docker", "systemctl"):
            path = os.path.join(bin_dir, tool)
            with open(path, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(path, 0o755)
        if with_shims:
            shims = os.path.join(home, ".local", "share", "mise", "shims")
            os.makedirs(shims)
            os.symlink(os.path.join(bin_dir, "gh"), os.path.join(shims, "gh"))
        env = {"HOME": home, "PATH": f"{bin_dir}:/usr/bin:/bin"}
        script = os.path.join(os.path.dirname(__file__), "..", "install.sh")
        subprocess.run(["bash", script, "--no-start"], env=env, check=True, capture_output=True)
        with open(os.path.join(home, ".config", "systemd", "user", "ci-flowd.service")) as f:
            unit = f.read()
        return home, bin_dir, next(l for l in unit.splitlines() if l.startswith("Environment=PATH="))

    def test_unit_path_uses_shims_when_present(self):
        home, bin_dir, line = self.run_install(with_shims=True)
        self.assertIn(os.path.join(home, ".local", "share", "mise", "shims") + ":", line)
        self.assertNotIn("gh-2.0", line.split(":")[0])

    def test_unit_path_falls_back_to_where_gh_is(self):
        _, bin_dir, line = self.run_install(with_shims=False)
        self.assertTrue(line.startswith(f"Environment=PATH={bin_dir}:"))


if __name__ == "__main__":
    unittest.main()
