import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trebuchet import flasher, printguard  # noqa: E402
from trebuchet.config import Settings  # noqa: E402
from trebuchet.registry import Registry  # noqa: E402
from trebuchet.runner import Runner  # noqa: E402


def body(state, filename=""):
    return ('{"result": {"status": {"print_stats": {"state": "%s", "filename": "%s"}}}}'
            % (state, filename))


class ParseTests(unittest.TestCase):
    def test_states(self):
        for state, busy in (("printing", True), ("paused", True), ("standby", False),
                            ("complete", False), ("cancelled", False), ("error", False)):
            st = printguard.parse_print_stats(body(state, "cube.gcode"))
            self.assertEqual((st.reachable, st.busy), (True, busy), state)
        self.assertIn("cube.gcode", printguard.parse_print_stats(body("printing", "cube.gcode")).label)

    def test_garbage_is_unreachable_not_idle(self):
        for bad in ("", "<html>", "{}", '{"result": {}}'):
            st = printguard.parse_print_stats(bad)
            self.assertFalse(st.reachable)
            self.assertFalse(st.busy)

    def test_unreachable_server(self):
        def opener(url, timeout=0):
            raise OSError("refused")
        self.assertFalse(printguard.print_state("http://x", opener=opener).reachable)


class FakeIO:
    def __init__(self, confirm):
        self._confirm, self.lines = confirm, []

    def say(self, t=""): self.lines.append(t)
    def warn(self, t): self.lines.append("WARN " + t)
    def confirm(self, t, default=False): return self._confirm
    def pause(self, t=""): pass
    def choose(self, title, options): return 0


class StopServiceGuard(unittest.TestCase):
    def flasher(self, tmp, state, confirm=False):
        s = Settings(data_dir=Path(tmp), dry_run=True)
        runner = Runner(dry_run=True, echo=lambda *_: None)
        f = flasher.Flasher(s, runner, Registry(Path(tmp) / "r.json"), FakeIO(confirm))
        printguard_orig = printguard.print_state
        printguard.print_state = lambda url, **kw: state
        self.addCleanup(setattr, printguard, "print_state", printguard_orig)
        return f

    def test_blocks_while_printing(self):
        with tempfile.TemporaryDirectory() as t:
            r = self.flasher(t, printguard.PrintState("printing", "a.gcode", True), confirm=True).stop_service()
        self.assertFalse(r.ok)
        self.assertIn("print is in progress", r.message)

    def test_blocks_while_paused(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self.flasher(t, printguard.PrintState("paused", "", True)).stop_service().ok)

    def test_idle_proceeds(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertTrue(self.flasher(t, printguard.PrintState("standby", "", True)).stop_service().ok)

    def test_unknown_requires_explicit_yes(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self.flasher(t, printguard.PrintState(), confirm=False).stop_service().ok)
            self.assertTrue(self.flasher(t, printguard.PrintState(), confirm=True).stop_service().ok)


if __name__ == "__main__":
    unittest.main()
