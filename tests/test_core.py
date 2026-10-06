import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trebuchet import detect, flasher  # noqa: E402
from trebuchet.advisor import ERROR, advise  # noqa: E402
from trebuchet.builder import BuildError, build_firmware, load_build  # noqa: E402
from trebuchet.config import Settings  # noqa: E402
from trebuchet import profiles  # noqa: E402
from trebuchet.executor import execute_plan  # noqa: E402
from trebuchet.flasher import Flasher  # noqa: E402
from trebuchet.profiles import (BoardProfile, Machine, MachineBoard, list_machines,  # noqa: E402
                          list_profiles, load_machine, save_machine, save_profile)
from trebuchet.registry import Registry  # noqa: E402
from trebuchet.runner import Result, Runner  # noqa: E402

MAKEFILE = """\
KCONFIG_CONFIG ?= .config
all:
\tmkdir -p out && echo fw > out/klipper.bin && echo fw > out/katapult.bin
olddefconfig:
\ttouch $(KCONFIG_CONFIG)
clean:
\trm -rf out
"""


class FakeIO:
    def __init__(self, confirm=True):
        self._confirm = confirm
        self.lines: list[str] = []

    def say(self, t=""): self.lines.append(t)
    def warn(self, t): self.lines.append("WARN " + t)
    def error(self, t): self.lines.append("ERR " + t)
    def ok(self, t): self.lines.append("OK " + t)
    def confirm(self, t, default=False): return self._confirm
    def pause(self, t=""): pass
    def choose(self, title, options): return 0
    @property
    def text(self): return "\n".join(self.lines)


def make_env(tmp: Path, dry_run=True):
    s = Settings(klipper_dir=tmp / "klipper", katapult_dir=tmp / "katapult",
                 klippy_env=tmp / "klippy-env", data_dir=tmp / "data", dry_run=dry_run,
                 check_toolchain=False)
    for repo in (s.klipper_dir, s.katapult_dir):
        repo.mkdir(parents=True)
        (repo / "Makefile").write_text(MAKEFILE)
    scripts = s.katapult_dir / "scripts"
    scripts.mkdir()
    marker = tmp / "flashtool_ran"
    (scripts / "flashtool.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    s.ensure_dirs()
    return s, marker


def make_profile(s: Settings, pid: str, *, interface="usb", family="stm32", mcu="stm32f446",
                 bitrate=None, katapult_cfg=True, method="dfu") -> BoardProfile:
    p = BoardProfile(id=pid, name=pid.upper(), mcu=mcu, family=family, interface=interface,
                     first_katapult_method=method, can_bitrate=bitrate)
    d = save_profile(s.profiles_dir, p)
    (d / "klipper.config").write_text("CONFIG_X=y\n")
    if katapult_cfg:
        (d / "katapult.config").write_text("CONFIG_Y=y\n")
    return p


class Parsers(unittest.TestCase):
    def test_by_id(self):
        k = detect.parse_by_id_name("usb-Klipper_stm32f446xx_2D0034001750-if00")
        self.assertEqual((k.app, k.mcu, k.serial), ("klipper", "stm32f446xx", "2D0034001750"))
        c = detect.parse_by_id_name("usb-katapult_rp2040_E66058-if00")
        self.assertEqual((c.app, c.mcu, c.serial), ("katapult", "rp2040", "E66058"))
        self.assertEqual(detect.parse_by_id_name("usb-FTDI_FT232R_A1B2-if00").app, "other")
        self.assertIsNone(detect.parse_by_id_name("ttyACM0"))

    def test_lsusb(self):
        text = ("Bus 001 Device 004: ID 0483:df11 STMicroelectronics STM Device in DFU Mode\n"
                "Bus 001 Device 005: ID 1d50:6177 OpenMoko Katapult\n"
                "Bus 001 Device 006: ID 1d50:614E OpenMoko Klipper\n"
                "Bus 001 Device 007: ID 2e8a:0003 Raspberry Pi RP2 Boot\n"
                "Bus 001 Device 001: ID 1d6b:0002 Linux Foundation root hub\n")
        kinds = [i.kind for i in detect.parse_lsusb(text)]
        self.assertEqual(kinds, ["stm32-dfu", "katapult", "klipper", "rp2040-bootsel", "other"])

    def test_can_queries(self):
        a = detect.parse_canbus_query("Found canbus_uuid=11aa22bb33cc, Application: Klipper\n"
                                      "Found canbus_uuid=aabbccddeeff, Application: CanBoot\n"
                                      "Total 2 uuids found\n")
        self.assertEqual([(n.uuid, n.kind) for n in a],
                         [("11aa22bb33cc", "klipper"), ("aabbccddeeff", "katapult")])
        b = detect.parse_flashtool_query("Detected UUID: a1b2c3d4e5f6, Application: Katapult")
        self.assertEqual((b[0].uuid, b[0].kind), ("a1b2c3d4e5f6", "katapult"))

    def test_ip_link(self):
        up = detect.parse_ip_link("3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 state UP \\ "
                                  "can state ERROR-ACTIVE bitrate 1000000 sample-point 0.875")
        self.assertEqual((up.exists, up.up, up.bitrate), (True, True, 1000000))
        down = detect.parse_ip_link("3: can0: <NOARP,ECHO> mtu 16 state DOWN")
        self.assertEqual((down.exists, down.up), (True, False))
        self.assertFalse(detect.parse_ip_link("", ok=False).exists)

    def test_flashtool_query_needs_confirmation(self):
        s = Settings()
        with self.assertRaises(ValueError):
            detect.query_can_katapult(s, Runner(echo=None), single_node_confirmed=False)

    def test_wait_for_and_pick(self):
        calls = iter([0, 0, "ok"])
        t = [0.0]
        out = detect.wait_for(lambda: next(calls), 10, 1, sleep=lambda x: t.__setitem__(0, t[0] + x),
                              clock=lambda: t[0])
        self.assertEqual(out, "ok")
        scan = detect.UsbScan(serial=[
            detect.UsbSerial("/a", "klipper", "stm32f446xx", "S1"),
            detect.UsbSerial("/b", "klipper", "rp2040", "S2")])
        self.assertIsNone(detect.pick_usb_serial(scan)[0])               # ambíguo
        self.assertEqual(detect.pick_usb_serial(scan, serial="s2")[0].path, "/b")
        self.assertEqual(detect.pick_usb_serial(scan, mcu_hint="rp2040")[0].path, "/b")


class Storage(unittest.TestCase):
    def test_roundtrip_and_registry(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            make_profile(s, "octopus", interface="can", bitrate=1000000)
            self.assertEqual(list_profiles(s.profiles_dir)[0].can_bitrate, 1000000)
            m = Machine("Cliente X", "can0", [
                MachineBoard("main", "octopus", uuid="AABBCCDDEEFF"),
                MachineBoard("tool", "octopus", serial="S1", device="/dev/ttyAMA0")])
            save_machine(s.machines_dir, m)
            back = load_machine(s.machines_dir / "cliente-x.toml")
            self.assertEqual(back.name, "Cliente X")
            self.assertEqual(back.boards[0].uuid, "aabbccddeeff")
            self.assertEqual(back.boards[1].device, "/dev/ttyAMA0")
            self.assertEqual(len(list_machines(s.machines_dir)), 1)

            r = Registry(s.registry_file)
            self.assertEqual(r.katapult("cliente-x", "main"), "unknown")
            r.set_katapult("cliente-x", "main", "yes")
            self.assertEqual(Registry(s.registry_file).katapult("cliente-x", "main"), "yes")
            with self.assertRaises(ValueError):
                r.set_katapult("cliente-x", "main", "talvez")


class Advisor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s, self.marker = make_env(Path(self.tmp.name))
        self.profs = {
            "bridge": make_profile(self.s, "bridge", interface="usb-can-bridge", bitrate=1000000),
            "node": make_profile(self.s, "node", interface="can", bitrate=1000000, mcu="stm32g0b1"),
            "mainusb": make_profile(self.s, "mainusb", interface="usb"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_bridge_last_and_builds_before_flash(self):
        m = Machine("m", boards=[MachineBoard("br", "bridge", uuid="111111111111"),
                                 MachineBoard("tool", "node", uuid="222222222222"),
                                 MachineBoard("main", "mainusb", serial="S1")])
        plan = advise(m, self.profs, {"br": "yes", "tool": "yes", "main": "yes"})
        self.assertFalse(plan.has_errors, plan.advice)
        kinds = [(s.kind, s.board) for s in plan.steps]
        flashes = [b for k, b in kinds if k == "flash_klipper"]
        self.assertEqual(flashes[-1], "br")
        last_build = max(i for i, (k, _) in enumerate(kinds) if k.startswith("build_"))
        first_flash = min(i for i, (k, _) in enumerate(kinds) if k == "flash_klipper")
        self.assertLess(last_build, first_flash)
        self.assertEqual(kinds[0][0], "stop_service")
        self.assertEqual(kinds[-1][0], "start_service")

    def test_first_katapult_steps_and_heater_warning(self):
        m = Machine("m", boards=[MachineBoard("main", "mainusb", serial="S1")])
        plan = advise(m, self.profs, {"main": "no"})
        kinds = [s.kind for s in plan.steps]
        self.assertEqual(kinds, ["stop_service", "build_katapult", "build_klipper",
                                 "first_katapult", "flash_klipper", "start_service"])
        self.assertTrue(any("aquecedor" in a.text for a in plan.advice))

    def test_new_can_node_queries_uuid_alone(self):
        m = Machine("m", boards=[MachineBoard("tool", "node")])
        plan = advise(m, self.profs, {"tool": "no"})
        self.assertFalse(plan.has_errors, plan.advice)
        kinds = [s.kind for s in plan.steps]
        self.assertLess(kinds.index("first_katapult"), kinds.index("query_uuid"))
        self.assertLess(kinds.index("query_uuid"), kinds.index("flash_klipper"))

    def test_errors_block_plan(self):
        # CAN sem UUID, Katapult desconhecido
        m = Machine("m", boards=[MachineBoard("tool", "node")])
        plan = advise(m, self.profs, {"tool": "unknown"})
        self.assertTrue(plan.has_errors)
        self.assertEqual(plan.steps, [])
        # bitrates diferentes
        other = make_profile(self.s, "node500", interface="can", bitrate=500000)
        m = Machine("m", boards=[MachineBoard("a", "node", uuid="1" * 12),
                                 MachineBoard("b", "node500", uuid="2" * 12)])
        plan = advise(m, {**self.profs, "node500": other}, {"a": "yes", "b": "yes"})
        self.assertTrue(any(a.level == ERROR and "bitrates" in a.text for a in plan.advice))
        # família fora do âmbito
        avr = make_profile(self.s, "avr", family="other", mcu="atmega2560")
        m = Machine("m", boards=[MachineBoard("a", "avr")])
        plan = advise(m, {"avr": avr}, {"a": "yes"})
        self.assertTrue(any("fora do âmbito" in a.text for a in plan.advice))
        # perfil inexistente / máquina vazia
        self.assertTrue(advise(Machine("m", boards=[MachineBoard("a", "nada")]), {}, {}).has_errors)
        self.assertTrue(advise(Machine("m"), {}, {}).has_errors)


class Commands(unittest.TestCase):
    def test_builders(self):
        tool, fw = Path("/k/flashtool.py"), Path("/o/klipper.bin")
        self.assertEqual(flasher.cmd_flashtool_usb(tool, "/dev/x", fw),
                         ["python3", "/k/flashtool.py", "-d", "/dev/x", "-f", "/o/klipper.bin"])
        self.assertEqual(flasher.cmd_flashtool_usb(tool, "/dev/x", request_only=True),
                         ["python3", "/k/flashtool.py", "-r", "-d", "/dev/x"])
        self.assertEqual(flasher.cmd_flashtool_usb(tool, "/dev/ttyAMA0", fw, baud=250000),
                         ["python3", "/k/flashtool.py", "-d", "/dev/ttyAMA0", "-b", "250000",
                          "-f", "/o/klipper.bin"])
        self.assertEqual(flasher.cmd_flashtool_can(tool, "can0", "aabbccddeeff", fw),
                         ["python3", "/k/flashtool.py", "-i", "can0", "-u", "aabbccddeeff",
                          "-f", "/o/klipper.bin"])
        self.assertEqual(flasher.cmd_flashtool_can(tool, "can0", "aabbccddeeff", request_only=True),
                         ["python3", "/k/flashtool.py", "-i", "can0", "-u", "aabbccddeeff", "-r"])
        sudo = [] if os.geteuid() == 0 else ["sudo"]
        self.assertEqual(flasher.cmd_dfu_util(Path("/o/katapult.bin")),
                         sudo + ["dfu-util", "-d", "0483:df11", "-a", "0", "-R", "-D", "/o/katapult.bin",
                                 "-s", "0x08000000:leave"])
        self.assertEqual(flasher.cmd_service("stop", "klipper"),
                         sudo + ["systemctl", "stop", "klipper"])


class RunnerTests(unittest.TestCase):
    def test_dry_run_only_blocks_mutating(self):
        with tempfile.TemporaryDirectory() as t:
            f1, f2 = Path(t) / "a", Path(t) / "b"
            r = Runner(dry_run=True, echo=None)
            res = r.run(["touch", str(f1)], mutating=True)
            self.assertTrue(res.dry_run and res.ok and not f1.exists())
            res = r.run(["touch", str(f2)])           # leitura/neutro corre sempre
            self.assertTrue(res.ok and f2.exists())
            self.assertEqual(r.run(["comando-que-nao-existe"]).returncode, 127)


class BuilderAndExecutor(unittest.TestCase):
    def test_build_copies_config_and_artifacts(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            p = make_profile(s, "b1")
            orig = (p.klipper_config).read_text()
            res = build_firmware(s, Runner(echo=None), kind="klipper", profile=p,
                                 machine_slug="m", label="main")
            self.assertTrue(res.bin.exists())
            self.assertEqual(p.klipper_config.read_text(), orig)       # perfil intacto
            self.assertEqual(load_build(s, "m", "main", "klipper").bin, res.bin)
            self.assertIsNone(load_build(s, "m", "main", "katapult"))

    def test_missing_toolchain_gives_clear_error(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            s.check_toolchain = True
            p = make_profile(s, "b1")
            with mock.patch("trebuchet.builder.shutil.which", return_value=None):
                with self.assertRaises(BuildError) as cm:
                    build_firmware(s, Runner(echo=None), kind="klipper", profile=p,
                                   machine_slug="m", label="main")
            self.assertIn("gcc-arm-none-eabi", str(cm.exception))

    def test_build_errors(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            p = make_profile(s, "b1", katapult_cfg=False)
            with self.assertRaises(BuildError):
                build_firmware(s, Runner(echo=None), kind="katapult", profile=p,
                               machine_slug="m", label="main")
            (s.klipper_dir / "Makefile").write_text("all:\n\tfalse\n")
            with self.assertRaises(BuildError):
                build_firmware(s, Runner(echo=None), kind="klipper", profile=p,
                               machine_slug="m", label="main")

    def test_full_plan_dry_run_touches_no_hardware(self):
        with tempfile.TemporaryDirectory() as t:
            s, marker = make_env(Path(t), dry_run=True)
            profs = {"bridge": make_profile(s, "bridge", interface="usb-can-bridge", bitrate=1000000),
                     "node": make_profile(s, "node", interface="can", bitrate=1000000)}
            m = Machine("Maq", "can0", [MachineBoard("br", "bridge", uuid="1" * 12),
                                        MachineBoard("tool", "node", uuid="2" * 12)])
            io = FakeIO()
            runner = Runner(dry_run=True, echo=io.say)
            reg = Registry(s.registry_file)
            reg.set_katapult(m.slug, "br", "yes")
            reg.set_katapult(m.slug, "tool", "yes")
            fl = Flasher(s, runner, reg, io, sleep=lambda x: None)
            plan = advise(m, profs, {"br": "yes", "tool": "yes"})
            ok = execute_plan(plan, m, profs, s, runner, reg, fl, io, lambda mm: None)
            self.assertTrue(ok, io.text)
            self.assertFalse(marker.exists(), "o flashtool correu em dry-run")
            self.assertIn("[dry-run] $ python3", io.text)
            self.assertIsNotNone(load_build(s, m.slug, "tool", "klipper"))

    def test_stops_at_first_failure_and_reports_remaining(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t), dry_run=True)
            p = make_profile(s, "node", interface="can", bitrate=1000000)
            m = Machine("Maq", "can0", [MachineBoard("a", "node", uuid="1" * 12),
                                        MachineBoard("b", "node", uuid="2" * 12)])
            io = FakeIO(confirm=False)          # utilizador recusa a gravação
            runner = Runner(dry_run=True, echo=io.say)
            reg = Registry(s.registry_file)
            plan = advise(m, {"node": p}, {"a": "yes", "b": "yes"})
            fl = Flasher(s, runner, reg, io, sleep=lambda x: None)
            ok = execute_plan(plan, m, {"node": p}, s, runner, reg, fl, io, lambda mm: None)
            self.assertFalse(ok)
            self.assertIn("Passos que ficaram por fazer", io.text)
            self.assertIn("Gravar Klipper em 'b'", io.text)


class CLI(unittest.TestCase):
    def run_cli(self, args, stdin=""):
        with tempfile.TemporaryDirectory() as t:
            env = {**os.environ, "NO_COLOR": "1"}
            return subprocess.run([sys.executable, "-m", "trebuchet", "--data-dir", t, *args], cwd=ROOT,
                                  input=stdin, capture_output=True, text=True, env=env, timeout=30)

    def test_quit(self):
        r = self.run_cli([], "q\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Trebuchet - Klipper Flash Helper", r.stdout)

    def test_eof_exits_cleanly(self):
        self.assertEqual(self.run_cli([], "").returncode, 0)

    def test_plan_unknown_machine(self):
        self.assertEqual(self.run_cli(["plan", "nada"]).returncode, 2)

    def test_doctor_runs(self):
        r = self.run_cli(["doctor"])
        self.assertIn("Diagnóstico", r.stdout)
        self.assertIn("Python >= 3.8", r.stdout)


class DiscoverTests(unittest.TestCase):
    CFG = (
        "[include extra/*.cfg]\n"
        "[mcu]\n"
        "serial: /dev/serial/by-id/usb-Klipper_stm32f446xx_AAA111-if00  # principal\n"
        "[mcu toolhead]\n"
        "canbus_uuid: 0123456789AB\n"
        "[mcu rpi]\n"
        "serial: /tmp/klipper_host_mcu\n"
        "[printer]\nkinematics: corexy\n"
        "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
    )
    EXTRA = "[mcu chamber]\ncanbus_uuid = ba98765432ff\ncanbus_interface: can0\n"

    class FakeRunner:
        def __init__(self, lsusb=""):
            self.lsusb, self.calls = lsusb, []

        def run(self, cmd, **kw):
            self.calls.append(list(cmd))
            return Result(list(cmd), 0, self.lsusb if cmd[0] == "lsusb" else "")

    def env(self, tmp: Path):
        cfgdir = tmp / "config"
        (cfgdir / "extra").mkdir(parents=True)
        (cfgdir / "printer.cfg").write_text(self.CFG)
        (cfgdir / "extra" / "chamber.cfg").write_text(self.EXTRA)
        by_id = tmp / "by-id"
        by_id.mkdir()
        (by_id / "usb-Klipper_stm32f446xx_AAA111-if00").write_text("")
        (by_id / "usb-katapult_rp2040_ZZZ999-if00").write_text("")
        s, _ = make_env(tmp)
        return s, cfgdir / "printer.cfg", by_id

    def test_parse_cfg_with_include_and_comments(self):
        from trebuchet import discover
        with tempfile.TemporaryDirectory() as t:
            _, cfg, _ = self.env(Path(t))
            mcus = {m.name: m for m in discover.parse_klipper_cfg(cfg)}
        self.assertEqual(set(mcus), {"mcu", "toolhead", "rpi", "chamber"})
        self.assertEqual(mcus["mcu"].serial, "/dev/serial/by-id/usb-Klipper_stm32f446xx_AAA111-if00")
        self.assertEqual(mcus["toolhead"].uuid, "0123456789ab")
        self.assertEqual(mcus["chamber"].uuid, "ba98765432ff")
        self.assertEqual(mcus["chamber"].canbus_interface, "can0")

    def test_include_loop_does_not_hang(self):
        from trebuchet import discover
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "a.cfg"
            f.write_text("[include a.cfg]\n[mcu]\ncanbus_uuid: 111111111111\n")
            self.assertEqual(len(discover.parse_klipper_cfg(f)), 1)

    def test_moonraker_parser(self):
        from trebuchet import discover
        body = ('{"result": {"status": {"mcu": {"mcu_version": "v0.12.0-1", "mcu_constants": '
                '{"MCU": "stm32f446xx", "CANBUS_BRIDGE": 1}}, "mcu toolhead": {"mcu_version": "v0.12.0-1", '
                '"mcu_constants": {"MCU": "stm32g0b1xx"}}, "toolhead": {}}}}')
        info = discover.parse_moonraker_mcus(body)
        self.assertEqual(info["mcu"].chip, "stm32f446xx")
        self.assertTrue(info["mcu"].bridge)
        self.assertEqual(info["toolhead"].chip, "stm32g0b1xx")
        self.assertIsNone(info["toolhead"].bridge)
        self.assertEqual(discover.parse_moonraker_mcus("lixo"), {})
        self.assertEqual(discover.parse_moonraker_mcus('{"result": {}}'), {})

    def test_chip_matches(self):
        from trebuchet.discover import chip_matches
        self.assertTrue(chip_matches("stm32f446", "stm32f446xx"))
        self.assertTrue(chip_matches("STM32F446 (Octopus)", "stm32f446xx"))
        self.assertFalse(chip_matches("stm32f446", "stm32g0b1xx"))
        self.assertTrue(chip_matches("rp2040", "rp2040"))
        self.assertIsNone(chip_matches("lpc1769", "lpc1769"))   # fora do âmbito: não bloqueia
        self.assertIsNone(chip_matches("", "stm32f446xx"))

    def run_discover(self, tmp, *, driver, moonraker_body=None):
        from trebuchet import discover
        s, cfg, by_id = self.env(tmp)
        sys_net = tmp / "net"
        if driver is not None:
            (sys_net / "can0").mkdir(parents=True)
            if driver:
                drv = tmp / "drivers" / driver
                drv.mkdir(parents=True)
                (sys_net / "can0" / "device").mkdir()
                (sys_net / "can0" / "device" / "driver").symlink_to(drv)

        class Resp:
            def __init__(self, b): self.b = b
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return self.b.encode()

        def opener(url, timeout=0):
            if moonraker_body is None:
                raise OSError("sem Moonraker")
            return Resp(moonraker_body)

        return discover.discover(s, self.FakeRunner(), printer_cfg=str(cfg), by_id_dir=by_id,
                                 sys_net=sys_net, opener=opener)

    def test_discover_bridge_unresolved_with_gs_usb(self):
        with tempfile.TemporaryDirectory() as t:
            d = self.run_discover(Path(t), driver="gs_usb")
        names = {x.name: x for x in d.devices}
        self.assertEqual(set(names), {"mcu", "toolhead", "chamber", "rp2040-Z999"})
        self.assertTrue(names["mcu"].present)                    # USB visto em by-id
        self.assertEqual(names["mcu"].chip, "stm32f446xx")
        self.assertEqual(names["rp2040-Z999"].app, "katapult")
        self.assertTrue(d.bridge_unresolved)
        self.assertFalse(d.moonraker)
        from trebuchet import discover
        self.assertIn("ponte?", discover.render(d))

    def test_discover_marks_bridge_from_moonraker_and_orders_last(self):
        from trebuchet import discover
        body = ('{"result": {"status": {"mcu toolhead": {"mcu_version": "v1", "mcu_constants": '
                '{"MCU": "stm32g0b1xx", "CANBUS_BRIDGE": 1}}, "mcu chamber": {"mcu_version": "v1", '
                '"mcu_constants": {"MCU": "stm32f072xb"}}}}}')
        with tempfile.TemporaryDirectory() as t:
            d = self.run_discover(Path(t), driver="gs_usb", moonraker_body=body)
        self.assertFalse(d.bridge_unresolved)
        order = [x.name for x in discover.flash_order(d.devices)]
        self.assertEqual(order[-1], "toolhead")                  # ponte em último
        self.assertLess(order.index("mcu"), order.index("chamber"))   # USB antes do CAN
        self.assertEqual({x.name: x.chip for x in d.devices}["chamber"], "stm32f072xb")

    def test_discover_non_bridge_adapter_means_no_bridge(self):
        with tempfile.TemporaryDirectory() as t:
            d = self.run_discover(Path(t), driver="mcp251x")
        self.assertFalse(d.bridge_unresolved)
        self.assertTrue(all(x.bridge is False for x in d.devices if x.transport == "can"))

    def test_discover_works_without_any_klipper_config(self):
        """Sem printer.cfg, sem Moonraker, sem Klipper instalado: só o que está ligado."""
        from trebuchet import discover
        lsusb = ("Bus 001 Device 004: ID 0483:df11 STMicroelectronics STM Device in DFU Mode\n"
                 "Bus 001 Device 007: ID 2e8a:0003 Raspberry Pi RP2 Boot\n")
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            s, _ = make_env(tmp)
            by_id = tmp / "by-id"
            by_id.mkdir()
            (by_id / "usb-Klipper_stm32f446xx_AAA111-if00").write_text("")
            s.klipper_dir = tmp / "nao-existe"            # nem sequer há Klipper
            def opener(url, timeout=0):
                raise OSError("sem Moonraker")
            d = discover.discover(s, self.FakeRunner(lsusb), by_id_dir=by_id,
                                  sys_net=tmp / "net", opener=opener,
                                  printer_cfg=str(tmp / "nao-existe.cfg"))
        names = {x.name: x for x in d.devices}
        self.assertEqual(set(names), {"stm32f446xx-A111", "dfu-1", "bootsel-1"})
        self.assertEqual(names["bootsel-1"].chip, "rp2040")
        self.assertEqual(names["dfu-1"].app, "dfu")
        self.assertTrue(any("Sem printer.cfg" in n for n in d.notes))

    def test_discover_flow_without_config_or_klipper_dir(self):
        from trebuchet import discover
        from trebuchet.menu import App

        class IO(FakeIO):
            def __init__(self, answers):
                super().__init__(confirm=False)
                self.answers = list(answers)
            def title(self, t): pass
            def ask(self, text, default=""):
                return (self.answers.pop(0) if self.answers else "") or default
            def choose(self, title, options):
                return 0

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            s, _ = make_env(tmp)
            s.klipper_dir = tmp / "nao-existe"
            s.printer_cfg = str(tmp / "nao-existe.cfg")
            by_id = tmp / "by-id"
            by_id.mkdir()
            (by_id / "usb-Klipper_stm32f446xx_AAA111-if00").write_text("")
            make_profile(s, "octo", interface="usb", mcu="stm32f446")
            io = IO(["maq", "", "s"])
            app = App(s, io)
            orig = discover.discover
            discover.discover = lambda settings, runner, **kw: orig(
                settings, runner, by_id_dir=by_id, sys_net=tmp / "net", **kw)
            try:
                m = app.discover_flow()
            finally:
                discover.discover = orig
        self.assertIsNotNone(m)
        self.assertEqual([b.profile_id for b in m.boards], ["octo"])

    def test_catalog_empty_without_klipper_dir(self):
        from trebuchet import catalog
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(catalog.load_catalog(Path(t) / "nao-existe", Path(t) / "c.toml"), [])

    def test_candidate_profiles_filters_by_interface_and_chip(self):
        from trebuchet import discover
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            ps = [make_profile(s, "octo", interface="usb", mcu="stm32f446"),
                  make_profile(s, "ebb", interface="can", mcu="stm32g0b1", bitrate=1000000),
                  make_profile(s, "ebb-old", interface="can", mcu="stm32f072", bitrate=1000000),
                  make_profile(s, "ponte", interface="usb-can-bridge", mcu="stm32g0b1", bitrate=1000000)]
        can = discover.Device("th", "can", uuid="0123456789ab", chip="stm32g0b1xx")
        self.assertEqual([p.id for p in discover.candidate_profiles(can, ps)], ["ebb"])
        can.bridge = True
        self.assertEqual([p.id for p in discover.candidate_profiles(can, ps)], ["ponte"])
        usb = discover.Device("m", "usb", serial="A", chip="stm32f446xx")
        self.assertEqual([p.id for p in discover.candidate_profiles(usb, ps)], ["octo"])

    def test_advisor_blocks_chip_mismatch(self):
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            p = make_profile(s, "octo", interface="usb", mcu="stm32f446")
            m = Machine(name="x", boards=[MachineBoard(label="main", profile_id=p.id,
                                                       chip="stm32g0b1xx")])
            plan = advise(m, {p.id: p}, {"main": "yes"})
        self.assertTrue(plan.has_errors)
        self.assertTrue(any("não corresponde" in a.text for a in plan.advice))

    def test_machine_chip_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            m = Machine(name="x", boards=[MachineBoard(label="a", profile_id="p", chip="stm32f446xx")])
            path = save_machine(Path(t), m)
            self.assertEqual(load_machine(path).boards[0].chip, "stm32f446xx")

    def test_discover_flow_creates_machine(self):
        from trebuchet import discover
        from trebuchet.menu import App

        class ScriptedIO(FakeIO):
            def __init__(self, answers):
                super().__init__(confirm=False)
                self.answers, self.titles = list(answers), []

            def title(self, t): self.titles.append(t)
            def ask(self, text, default=""):
                return (self.answers.pop(0) if self.answers else "") or default
            def choose(self, title, options):
                self.lines.append(f"CHOOSE {title} -> {options}")
                if options[0].startswith("Criar perfil novo"):   # sem perfis compatíveis
                    return len(options) - 1                       # ignorar
                return 0   # primeira opção: o perfil sugerido

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            s, cfg, by_id = self.env(tmp)
            s.printer_cfg = str(cfg)
            make_profile(s, "octo", interface="usb", mcu="stm32f446")
            make_profile(s, "ebb", interface="can", mcu="stm32g0b1", bitrate=1000000)
            io = ScriptedIO(["minha-maquina", "", "s", "", "n", "", "n"])
            app = App(s, io)
            orig = discover.discover
            discover.discover = lambda settings, runner, **kw: orig(
                settings, runner, by_id_dir=by_id, sys_net=tmp / "net", **kw)
            try:
                m = app.discover_flow()
            finally:
                discover.discover = orig
            labels = [b.label for b in m.boards]
            saved = load_machine(s.machines_dir / "minha-maquina.toml")
        self.assertIn("mcu", labels)
        self.assertIn("toolhead", labels)
        self.assertEqual(next(b for b in saved.boards if b.label == "mcu").chip, "stm32f446xx")
        self.assertEqual(next(b for b in saved.boards if b.label == "toolhead").uuid, "0123456789ab")
        self.assertEqual(app.reg.katapult("minha-maquina", "mcu"), "yes")


class CatalogAndPinTests(unittest.TestCase):
    def klipper_cfgs(self, tmp: Path) -> Path:
        kd = tmp / "klipper"
        (kd / "config").mkdir(parents=True)
        (kd / "config" / "generic-bigtreetech-octopus-v1.1.cfg").write_text(
            "# This file contains common pin mappings for the BigTreeTech Octopus\n"
            "# v1.1 board. Compile for the STM32F446 with a \"32KiB bootloader\"\n"
            "# (or STM32F429 if your board has it) and a \"12 MHz crystal\".\n"
            "\n# See docs/Config_Reference.md for a description of parameters.\n"
            "[stepper_x]\nstep_pin: PF13\ndir_pin: PF12\nenable_pin: !PF14\nendstop_pin: ^PG6\n"
            "[stepper_y]\nstep_pin: PG0\ndir_pin: PG1\nenable_pin: !PF15\n"
            "[heater_bed]\nheater_pin: PA1\nsensor_pin: PF3\n#commented_pin: PK9\n")
        (kd / "config" / "generic-fysetc-spider.cfg").write_text(
            "# This file contains common pin mappings for the Fysetc Spider board.\n"
            "# To use this config, the firmware should be compiled for the STM32F446.\n"
            "\n# See docs/Config_Reference.md for a description of parameters.\n"
            "[stepper_x]\nstep_pin: PE11\ndir_pin: PE10\nenable_pin: !PE9\nendstop_pin: PB14\n"
            "[heater_bed]\nheater_pin: PB4\nsensor_pin: PC4\n")
        (kd / "config" / "generic-bigtreetech-skr-pico-v1.0.cfg").write_text(
            "# pin mappings for the BIGTREETECH SKR Pico V1.0 board.\n"
            "# compile for the RP2040 with USB communication.\n"
            "\n# See docs/Config_Reference.md\n[stepper_x]\nstep_pin: gpio11\ndir_pin: gpio10\n")
        (kd / "config" / "sample-macros.cfg").write_text("# macros, no board here\n[gcode_macro X]\n")
        return kd

    def test_catalog_parses_klipper_headers(self):
        from trebuchet import catalog
        with tempfile.TemporaryDirectory() as t:
            kd = self.klipper_cfgs(Path(t))
            es = {e.id: e for e in catalog.load_klipper_catalog(kd)}
        self.assertEqual(set(es), {"generic-bigtreetech-octopus-v1.1", "generic-fysetc-spider",
                                   "generic-bigtreetech-skr-pico-v1.0"})
        octo = es["generic-bigtreetech-octopus-v1.1"]
        self.assertEqual(octo.name, "BigTreeTech Octopus v1.1")
        self.assertEqual(octo.chips, ("stm32f429", "stm32f446"))   # variantes: não escolhe sozinho
        self.assertEqual(octo.chip, "")
        self.assertEqual(es["generic-fysetc-spider"].chip, "stm32f446")
        self.assertEqual(es["generic-bigtreetech-skr-pico-v1.0"].family, "rp2040")
        self.assertNotIn("See docs", octo.notes)

    def test_user_catalog_overrides_and_adds(self):
        from trebuchet import catalog
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            kd = self.klipper_cfgs(tmp)
            (tmp / "catalog.toml").write_text(
                '[[board]]\nid = "fly-sb2040"\nvendor = "mellow"\nname = "Mellow Fly-SB2040"\n'
                'chip = "rp2040"\ninterface = "can"\nsource = "https://exemplo"\n')
            es = catalog.load_catalog(kd, tmp / "catalog.toml")
        self.assertEqual(es[0].id, "fly-sb2040")
        self.assertEqual((es[0].vendor, es[0].family, es[0].interface_hint), ("mellow", "rp2040", "can"))

    def test_user_pins_per_mcu_with_include_modifiers_and_comments(self):
        from trebuchet import pinmatch
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            (tmp / "extra").mkdir()
            (tmp / "extra" / "th.cfg").write_text("[fan]\npin: toolhead:PB13\n")
            cfg = tmp / "printer.cfg"
            cfg.write_text("[include extra/*.cfg]\n[stepper_x]\nstep_pin: PF13\ndir_pin: !PF12\n"
                           "endstop_pin: ^toolhead:PA3\n# step_pin: PK9\n"
                           "[extruder]\nstep_pin: toolhead:PD0 # comentario PE5\n")
            pins = pinmatch.user_pins(cfg)
        self.assertEqual(pins["mcu"], {"PF13", "PF12"})
        self.assertEqual(pins["toolhead"], {"PB13", "PA3", "PD0"})

    def test_pin_ranking_exact_board_first_and_ties_are_reported(self):
        from trebuchet import catalog, pinmatch
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            kd = self.klipper_cfgs(tmp)
            es = catalog.load_klipper_catalog(kd)
            user = {"PF13", "PF12", "PF14", "PG6", "PG0"}
            r = pinmatch.rank(user, es)
            self.assertEqual(r[0].entry.id, "generic-bigtreetech-octopus-v1.1")
            self.assertEqual(r[0].score, 1.0)
            self.assertEqual(r[0].shared, 5)
            self.assertEqual(len(r), 1)                       # as outras não partilham nenhum pin
            self.assertEqual(pinmatch.rank({"PF13", "PF12"}, es), [])        # poucos pins: sem palpite
            rp = pinmatch.rank({"gpio11", "gpio10", "gpio1", "gpio2"}, es)
            self.assertEqual(rp[0].entry.id, "generic-bigtreetech-skr-pico-v1.0")
            only446 = pinmatch.rank(user, es, chip_hint="stm32f446xx")
            self.assertTrue(all(m.entry.family == "stm32" for m in only446))
            self.assertEqual(pinmatch.rank(user, es, chip_hint="rp2040xx"), [])

    def test_catalog_has_create_from_scratch_escape_hatch(self):
        from trebuchet.menu import App

        class IO(FakeIO):
            def __init__(self):
                super().__init__(confirm=False)
                self.seen = []
            def title(self, t): pass
            def choose(self, title, options):
                self.seen.append((title, options))
                return len(options) - 1        # sempre a última: "criar do zero"

        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            s.klipper_dir = self.klipper_cfgs(Path(t))
            io = IO()
            self.assertIsNone(App(s, io).pick_catalog_entry())
        self.assertIn("criar do zero", io.seen[0][1][-1])
        self.assertIn("A criar o perfil do zero", io.text)

    def test_app_pin_suggestions_without_cfg_are_empty(self):
        from trebuchet.menu import App
        with tempfile.TemporaryDirectory() as t:
            s, _ = make_env(Path(t))
            s.printer_cfg = str(Path(t) / "nao-existe.cfg")
            self.assertEqual(App(s, FakeIO()).pin_suggestions("mcu"), [])


class ProfileFromDiscoveryTests(unittest.TestCase):
    def test_new_profile_prefilled_from_device(self):
        from trebuchet.menu import App

        class IO(FakeIO):
            def __init__(self, answers):
                super().__init__(confirm=False)
                self.answers = list(answers)
                self.asked = []

            def title(self, t): pass
            def ask(self, text, default=""):
                self.asked.append(text)
                return (self.answers.pop(0) if self.answers else "") or default
            def choose(self, title, options):
                self.asked.append("CHOOSE " + title)
                return 0

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            s, _ = make_env(tmp)
            cfg = tmp / "w.config"
            cfg.write_text("CONFIG_MACH_STM32=y\n")
            io = IO(["EBB42 1.2 BTT", "", "", "", "", str(cfg), str(cfg)])
            p = App(s, io).new_profile(chip="stm32g0b1xx", interface="can")
            self.assertEqual((p.family, p.mcu, p.interface), ("stm32", "stm32g0b1", "can"))
            self.assertEqual(p.id, "ebb42-1.2-btt")
            self.assertTrue(p.klipper_config.exists())
            # não perguntou família nem interface: já se sabiam
            self.assertFalse(any("Família" in q or "Interface com o Pi" in q for q in io.asked))


class UiTests(unittest.TestCase):
    def run_io(self, answers, fn):
        import builtins
        import contextlib
        import io as _io
        from trebuchet.ui import TerminalIO
        it = iter(answers)
        orig = builtins.input
        builtins.input = lambda prompt="": next(it)
        out = _io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                res = fn(TerminalIO(color=False))
        finally:
            builtins.input = orig
        return res, out.getvalue()

    def test_choose_is_numeric_with_zero_to_cancel(self):
        res, out = self.run_io(["EBB42 1.2 BTT", "3", ], lambda io: io.choose("T", ["a", "b", "c"]))
        self.assertEqual(res, 2)
        self.assertIn("1) a", out)
        self.assertIn("0) Cancelar", out)
        self.assertNotIn("B)", out)
        self.assertIn("Opção inválida", out)            # texto livre não é aceite como resposta

    def test_choose_cancel_and_range(self):
        res, _ = self.run_io(["9", "0"], lambda io: io.choose("T", ["a"]))
        self.assertIsNone(res)

    def test_menu_shows_exit_and_returns_index(self):
        res, out = self.run_io(["2"], lambda io: io.menu("M", ["x", "y"], back="Sair"))
        self.assertEqual(res, 1)
        self.assertIn("0) Sair", out)
        res, _ = self.run_io(["0"], lambda io: io.menu("M", ["x"], back="Sair"))
        self.assertIsNone(res)


class TomlMiniTests(unittest.TestCase):
    """O leitor próprio (usado em Python < 3.11) tem de ler igual ao tomllib."""

    SAMPLE = (
        '# comentário\n'
        'id = "octopus-pro" # fim de linha com # dentro "de aspas"\n'
        "name = 'Octopus \\ Pro'\n"
        'bitrate = 1_000_000\n'
        'ratio = 1.5\n'
        'on = true\n'
        'path = "C:\\\\dir\\t\\u00e9"\n'
        'tags = ["a", "b # c", 3]\n'
        'note = "tem # cardinal"\n'
        '\n[[board]]\nlabel = "mcu"\nuuid = "abc"\n'
        '\n[[board]]\nlabel = "toolhead"\n'
    )

    def test_matches_tomllib(self):
        try:
            import tomllib
        except ModuleNotFoundError:
            self.skipTest("sem tomllib neste Python")
        from trebuchet import tomlmini
        self.assertEqual(tomlmini.loads(self.SAMPLE), tomllib.loads(self.SAMPLE))

    def test_values(self):
        from trebuchet import tomlmini
        d = tomlmini.loads(self.SAMPLE)
        self.assertEqual(d["bitrate"], 1000000)
        self.assertIs(d["on"], True)
        self.assertEqual(d["tags"], ["a", "b # c", 3])
        self.assertEqual(d["note"], "tem # cardinal")
        self.assertEqual([b["label"] for b in d["board"]], ["mcu", "toolhead"])

    def test_roundtrip_with_our_writer(self):
        from trebuchet import tomlmini
        data = {"name": "Máquina \"X\"", "can_interface": "can0",
                "board": [{"label": "a", "profile": "p"}, {"label": "b", "uuid": "1"}]}
        back = tomlmini.loads(profiles.dump_toml(data, array_key="board"))
        self.assertEqual(back["name"], data["name"])
        self.assertEqual(back["board"], data["board"])

    def test_rejects_unsupported_and_invalid(self):
        from trebuchet import tomlmini
        for bad in ('a = {x = 1}', 'a = """x"""', 'a = ', 'a = "x', 'a.b = 1',
                    'a = 1\na = 2', 'sem igual', 'a = 2020-01-01', '[t]\n[t]'):
            with self.assertRaises(tomlmini.TOMLDecodeError, msg=bad):
                tomlmini.loads(bad)


if __name__ == "__main__":
    unittest.main()
