import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ciflow.model import derive  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "snapshot.json")


class Derive(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE) as f:
            self.snap = json.load(f)
        self.v = derive(self.snap)

    def test_is_json_and_does_not_mutate_the_snapshot(self):
        before = json.dumps(self.snap, sort_keys=True)
        json.dumps(derive(self.snap))
        self.assertEqual(json.dumps(self.snap, sort_keys=True), before)

    def test_health(self):
        self.assertEqual(self.v["health"]["level"], "crit")
        self.assertEqual(self.v["health"]["issues"][0], {"level": "crit", "text": "runner down: api#3"})

    def test_slots(self):
        web, api = self.v["slots"]
        self.assertEqual([s["state"] for s in web["slots"]], ["idle", "busy"])
        self.assertEqual(web["slots"][1]["job"], "Unit Tests")
        self.assertEqual(api["slots"][0]["state"], "down")

    def test_counts(self):
        self.assertEqual(self.v["counts"], {"busy": 1, "slots": 3, "jobs_not_started": 2, "runs_queued": 1})

    def test_a_queued_run_with_a_running_job_counts_as_running(self):
        (group,) = self.v["running"]
        (run,) = group["runs"]
        self.assertEqual((run["id"], run["status"], run["ticket"]), (101, "in_progress", "ABC-12"))
        self.assertEqual([j["label"] for j in run["jobs"]], ["Lint", "Unit", "Build"])
        unit = run["jobs"][1]
        self.assertEqual((unit["slot"], unit["steps_done"], unit["steps_total"]), ("2", 1, 3))
        self.assertAlmostEqual(run["pct"], 100 / 3)

    def test_queue(self):
        self.assertEqual([(r["id"], r["ticket"]) for r in self.v["queue"]], [(102, "#45")])

    def test_recent(self):
        web, api = self.v["recent"]
        self.assertEqual((web["last"]["workflow"], web["last"]["conclusion"]), ("Deploy", "failure"))
        self.assertIsNone(web["last_fail"])  # the last run is the failure
        self.assertEqual(api["last"]["conclusion"], "success")
        self.assertEqual(self.v["recent_limit"], 8)


if __name__ == "__main__":
    unittest.main()
