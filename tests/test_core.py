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
from trebuchet.runner import Runner  # noqa: E402

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
