"""The update flow end to end with scripted answers (no hardware, no menuconfig)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_core import (CatalogAndPinTests, FakeIO, fake_config, make_env, make_profile)
from trebuchet import discover, kconfig, matching
from trebuchet.advisor import advise
from trebuchet.menu import App
from trebuchet.profiles import Machine, MachineBoard, list_machines, load_profile
from trebuchet.registry import Registry


class ScriptedIO(FakeIO):
    """Answers choose() by matching a substring of the title (first rule that matches)."""

    def __init__(self, rules, asks=(), confirms=None):
        super().__init__(confirm=False)
        self.rules = list(rules)
        self.asks = list(asks)
        self.confirms = confirms or {}
        self.seen = []

    def ask(self, text, default=""):
        self.seen.append(("ask", text))
        return (self.asks.pop(0) if self.asks else "") or default

    def confirm(self, text, default=False):
        self.seen.append(("confirm", text))
        for k, v in self.confirms.items():
            if k in text:
                return v
        return default

    def choose(self, title, options, default=None, cancel="Cancel"):
        self.seen.append(("choose", title, list(options), default))
        for key, idx in self.rules:
            if key in title:
                return idx(options) if callable(idx) else idx
        return default


def patched_discovery(tmp, by_id):
    orig = discover.discover
    discover.discover = lambda settings, runner, **kw: orig(settings, runner, by_id_dir=by_id,
                                                            sys_net=tmp / "net", **kw)
    return orig


class UpdateFlowTests(unittest.TestCase):
    def setup_env(self, tmp: Path):
        s, _ = make_env(tmp)
        s.klipper_dir = CatalogAndPinTests().klipper_cfgs(tmp)
        (s.klipper_dir / "Makefile").write_text((s.katapult_dir / "Makefile").read_text())
        s.printer_cfg = str(tmp / "no-such-printer.cfg")      # the flow must work without any config
        (s.klipper_dir / ".config").write_text(fake_config("stm32", "stm32f446", "usb", None, klipper=True))
        by_id = tmp / "by-id"
        by_id.mkdir()
        (by_id / "usb-Klipper_stm32f446xx_AAA111-if00").write_text("")
        return s, by_id

    def test_new_machine_without_any_config_types_only_the_machine_name(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            s, by_id = self.setup_env(tmp)
            io = ScriptedIO([("Which board is this", 0),
                             ("Klipper firmware settings", 1),          # use the current .config
                             ("already have Katapult", 0),               # yes
                             ("What now", 2)],                           # save and exit
                            asks=["bench"])
            app = App(s, io)
            orig = patched_discovery(tmp, by_id)
            try:
                from trebuchet.update_flow import UpdateFlow
                m = UpdateFlow(app).new_machine()
            finally:
                discover.discover = orig
            self.assertIsNotNone(m)
            asked = [q for k, q, *_ in io.seen if k == "ask"]
            self.assertEqual(len(asked), 1, asked)                      # only the machine name
            self.assertEqual(m.slug, "bench")
            prof = app.profiles()[m.boards[0].profile_id]
            self.assertEqual((prof.family, prof.mcu, prof.interface), ("stm32", "stm32f446", "usb"))
            self.assertTrue(prof.klipper_config.exists())
            self.assertEqual(app.reg.katapult("bench", m.boards[0].label), "yes")
            self.assertEqual(len(list_machines(s.machines_dir)), 1)

    def test_board_is_recognised_next_time(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            s, by_id = self.setup_env(tmp)
            p = make_profile(s, "spider", interface="usb", mcu="stm32f446")
            app = App(s, ScriptedIO([]))
            dev = discover.Device("mcu", "usb", serial="AAA111", chip="stm32f446xx")
            app.reg.remember_board(Registry.board_key(dev.uuid, dev.serial), "spider", "mcu")
            entries = []
            sug = matching.suggest(dev, list(app.profiles().values()), entries, app.reg, None)
            self.assertEqual(sug[0].kind, "remembered")
            self.assertEqual(sug[0].profile.id, p.id)

    def test_suggestions_filter_by_chip_and_interface(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            s, _ = make_env(tmp)
            ps = [make_profile(s, "octo", interface="usb", mcu="stm32f446"),
                  make_profile(s, "ebb", interface="can", mcu="stm32g0b1", bitrate=1000000)]
            app = App(s, ScriptedIO([]))
            dev = discover.Device("th", "can", uuid="aabbccddeeff", chip="stm32g0b1xx")
            sug = matching.suggest(dev, ps, [], app.reg, None)
            self.assertEqual([x.profile.id for x in sug], ["ebb"])

    def test_print_in_progress_blocks_service_stop(self):
        from trebuchet import printguard
        st = printguard.parse_print_stats(
            '{"result": {"status": {"print_stats": {"state": "printing", "filename": "benchy.gcode"}}}}')
        self.assertTrue(st.busy)
        self.assertFalse(printguard.parse_print_stats("garbage").reachable)

    def test_wizard_rejects_config_for_wrong_chip_then_accepts(self):
        from trebuchet.profile_wizard import ProfileWizard
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            s, _ = make_env(tmp)
            bad = tmp / "bad.config"
            bad.write_text(fake_config("stm32", "stm32g0b1", "usb", None, klipper=True))
            good = tmp / "good.config"
            good.write_text(fake_config("stm32", "stm32f446", "usb", None, klipper=True))
            io = ScriptedIO([("Klipper firmware settings", 2),
                             ("How is the first Katapult", lambda o: 0)],
                            asks=["Octopus", str(bad), str(good)])
            dev = discover.Device("mcu", "usb", serial="A", chip="stm32f446xx")
            prof = ProfileWizard(s, io, App(s, io).runner).create(device=dev)
            self.assertIsNotNone(prof)
            self.assertIn("does not fit", io.text)
            self.assertEqual(prof.mcu, "stm32f446")

    def test_cancelled_wizard_leaves_no_half_profile(self):
        from trebuchet.profile_wizard import ProfileWizard
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            io = ScriptedIO([("Klipper firmware settings", None)], asks=["Foo"])
            self.assertIsNone(ProfileWizard(s, io, App(s, io).runner).create())
            self.assertEqual(list(s.profiles_dir.iterdir()), [])


class AdvisorKconfigTests(unittest.TestCase):
    def test_config_contradicting_profile_is_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            p = make_profile(s, "ebb", interface="can", mcu="stm32g0b1", bitrate=1000000)
            p.klipper_config.write_text(fake_config("stm32", "stm32g0b1", "usb", None, klipper=True))
            m = Machine("x", "can0", [MachineBoard("th", "ebb", uuid="aabbccddeeff")])
            self.assertTrue(advise(m, {"ebb": p}, {"th": "yes"}).has_errors)

    def test_katapult_offset_mismatch_is_an_error(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            p = make_profile(s, "ebb", interface="can", mcu="stm32g0b1", bitrate=1000000)
            p.katapult_config.write_text('CONFIG_MCU="stm32g0b1xx"\nCONFIG_LAUNCH_APP_ADDRESS=0x8001000\n')
            m = Machine("x", "can0", [MachineBoard("th", "ebb", uuid="aabbccddeeff")])
            plan = advise(m, {"ebb": p}, {"th": "no"})
            self.assertTrue(plan.has_errors)
            self.assertTrue(any("offset" in a.text.lower() or "address" in a.text.lower()
                                for a in plan.advice))

    def test_consistent_configs_have_no_errors(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            p = make_profile(s, "ebb", interface="can", mcu="stm32g0b1", bitrate=1000000)
            m = Machine("x", "can0", [MachineBoard("th", "ebb", uuid="aabbccddeeff")])
            self.assertFalse(advise(m, {"ebb": p}, {"th": "no"}).has_errors)


class RegistryIdentityTests(unittest.TestCase):
    def test_board_keys_roundtrip_and_forget(self):
        with tempfile.TemporaryDirectory() as t:
            r = Registry(Path(t) / "r.toml")
            k = Registry.board_key("AABBCCDDEEFF", "")
            self.assertEqual(k, "uuid:aabbccddeeff")
            self.assertEqual(Registry.board_key("", "S1"), "serial:S1")
            r.remember_board(k, "ebb", "toolhead")
            self.assertEqual(Registry(Path(t) / "r.toml").recall_board(k)["profile"], "ebb")
            r.forget_board(k)
            self.assertEqual(r.recall_board(k), {})


class StatusTests(unittest.TestCase):
    def test_home_status_lines_work_with_nothing_installed(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            s.klipper_dir = Path(t) / "none"
            lines = App(s, FakeIO()).status_lines()
            self.assertTrue(any("Klipper" in x for x in lines))


if __name__ == "__main__":
    unittest.main()
