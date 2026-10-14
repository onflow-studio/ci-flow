import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ciflow.tui import ANSI_RE, Source, fit, job_order, render, slot_of, ticket, vlen, wrap_chips  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "snapshot.json")


def plain(s):
    return ANSI_RE.sub("", s)


class Helpers(unittest.TestCase):
    def test_fit_pads_and_truncates_visible_columns(self):
        self.assertEqual(vlen(fit("\x1b[31mabc\x1b[0m", 6)), 6)
        self.assertEqual(plain(fit("\x1b[31mabcdef\x1b[0m", 3)), "abc")
        self.assertEqual(vlen(fit("", 4)), 4)

    def test_ticket(self):
        self.assertEqual(ticket("feat/abc-123-add-login"), "ABC-123")
        self.assertEqual(ticket("fix/45-typo-in-footer"), "#45")
        self.assertEqual(ticket("main"), "main")
        self.assertEqual(ticket("ci/self-hosted-runner"), "self-hos…")

    def test_slot_of(self):
        self.assertEqual(slot_of("ci-box", "ci-box-2-1791986499"), "2")  # before the project was in the name
        self.assertEqual(slot_of("ci-box", "ci-box-web-app-2-1791986499"), "2")
        self.assertEqual(slot_of("ci-box", "ci-box-api-1-1791986499"), "1")
        self.assertFalse(slot_of("ci-box", "GitHub Actions 1000011702"))
        self.assertFalse(slot_of("ci-box", "other-box-2-1791986499"))
        self.assertFalse(slot_of("ci-box", None))

    def test_job_order_puts_started_jobs_first_by_start_time(self):
        jobs = [
            {"name": "B", "status": "queued", "started_at": None},
            {"name": "late", "status": "in_progress", "started_at": "2026-10-14T13:02:00Z"},
            {"name": "early", "status": "completed", "started_at": "2026-10-14T13:01:00Z"},
            {"name": "A", "status": "queued", "started_at": None},
        ]
        self.assertEqual([j["name"] for j in sorted(jobs, key=job_order)], ["early", "late", "A", "B"])

    def test_wrap_chips_wraps_at_width_with_indent(self):
        lines = wrap_chips(["aaaa", "bbbb", "cccc"], 16, "  ")
        self.assertEqual([plain(x) for x in lines], ["  aaaa · bbbb", "  cccc"])
        self.assertTrue(all(vlen(x) <= 16 for x in lines))


class Render(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE) as f:
            self.snap = json.load(f)

    def text(self, w=150, h=60, source="collector"):
        return [plain(x) for x in render(self.snap, w, h, source)]

    def test_every_line_is_exactly_the_terminal_width(self):
        for w in (80, 100, 150, 200):
            lines = render(self.snap, w, 60, "collector")
            self.assertEqual(len(lines), 60)
            for line in lines:
                self.assertEqual(vlen(line), w, repr(plain(line)))

    def test_sections_and_content(self):
        lines = self.text()
        body = "\n".join(lines)
        for title in ("▌MACHINE", "▌RUNNERS", "▌RUNNING", "▌QUEUE", "▌RECENT"):
            self.assertIn(title, body)
        self.assertIn("ci-box", lines[0])
        self.assertIn("RUNNER DOWN: API#3", lines[0])
        self.assertIn("1/3 busy", body)
        self.assertIn("■ web  1 running", body)
        self.assertIn("ABC-12", body)
        self.assertIn("▶ Unit", body)
        self.assertIn("@2", body)
        self.assertIn("1 waiting", body)
        self.assertIn("#45", body)
        self.assertRegex(body, r"repo +last 8 +last run +last fail")
        self.assertRegex(body, r"web +✖ +✖ Deploy +main +\S+ ago")

    def test_runner_states(self):
        line = next(x for x in self.text() if x.lstrip().startswith("web "))
        self.assertIn("1  idle", line)
        self.assertIn("Unit Tests", line)
        api = next(x for x in self.text() if x.lstrip().startswith("api "))
        self.assertIn("failed", api)

    def test_footer_shows_source(self):
        self.assertIn("source: collector", self.text()[-1])
        self.assertIn("source: local", self.text(source="local")[-1])

    def test_render_does_not_mutate_snapshot(self):
        before = json.dumps(self.snap, sort_keys=True)
        render(self.snap, 150, 60)
        self.assertEqual(json.dumps(self.snap, sort_keys=True), before)

    def test_machine_cells_are_not_cut_at_140_columns(self):
        self.snap["host"]["mem"] = {"used": 10.0 * 1024**3, "total": 15.2 * 1024**3,
                                    "swap_used": 0, "swap_total": 0}
        self.snap["host"]["disk"]["free"] = 405.3 * 1024**3
        for w in (110, 120, 140, 150):
            lines = self.text(w=w)
            i = next(n for n, x in enumerate(lines) if x.startswith("▌MACHINE"))
            row = "\n".join(lines[i + 1:i + 7])
            self.assertIn("10.0GB/15.2GB", row, w)
            self.assertIn("405.3GB free", row, w)
            self.assertNotRegex(row, r"\S(MEM|DISK)", w)


class FakeLocal:
    def __init__(self, snap):
        self.snap, self.started, self.stopped, self.refreshed = snap, False, False, False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def snapshot(self):
        return self.snap

    def refresh(self):
        self.refreshed = True


class Sources(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE) as f:
            self.snap = json.load(f)
        self.local = FakeLocal({**self.snap, "hostname": "local-box"})
        self.reply = None
        self.now = 0.0

    def source(self):
        return Source(url="http://x", fetch=lambda url: self.reply, make_local=lambda: self.local,
                      retry=10, clock=lambda: self.now)

    def test_uses_collector_when_it_answers(self):
        self.reply = self.snap
        snap, label = self.source().get()
        self.assertEqual((snap["hostname"], label), ("ci-box", "collector"))
        self.assertFalse(self.local.started)

    def test_falls_back_then_reconnects_after_retry_interval(self):
        src = self.source()
        snap, label = src.get()
        self.assertEqual(snap["hostname"], "local-box")
        self.assertIn("collector unreachable", label)
        self.assertTrue(self.local.started)
        self.reply = self.snap
        self.now = 5
        self.assertEqual(src.get()[1], label)  # not retried yet
        self.now = 10
        snap, label = src.get()
        self.assertEqual((snap["hostname"], label), ("ci-box", "collector"))
        self.assertTrue(self.local.stopped)

    def test_version_mismatch_falls_back_with_a_note(self):
        self.reply = {**self.snap, "version": 99}
        snap, label = self.source().get()
        self.assertEqual(snap["hostname"], "local-box")
        self.assertIn("v99", label)
        lines = [plain(x) for x in render(snap, 150, 60, label)]
        self.assertIn("v99", lines[-1])

    def test_refresh_goes_to_the_active_source(self):
        src = self.source()
        src.get()
        src.refresh()
        self.assertTrue(self.local.refreshed)


if __name__ == "__main__":
    unittest.main()
