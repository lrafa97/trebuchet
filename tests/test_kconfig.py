import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trebuchet import kconfig  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "kconfig"


def load(name, kind="klipper"):
    return kconfig.parse_config(FIX / name, kind)


class ParseTests(unittest.TestCase):
    """The fixtures are real .config files produced by Klipper's and Katapult's own `make olddefconfig`."""

    def test_usb(self):
        c = load("klipper-usb.config")
        self.assertEqual((c.mcu, c.interface, c.family, c.mcu_core), ("stm32g0b1xx", "usb", "stm32", "stm32g0b1"))
        self.assertIsNone(c.can_bitrate)            # the default 1000000 is meaningless without CAN
        self.assertEqual(c.app_address, 0x8002000)

    def test_can_node_with_bitrate(self):
        c = load("klipper-can.config")
        self.assertEqual((c.interface, c.can_bitrate), ("can", 500000))

    def test_usb_can_bridge(self):
        c = load("klipper-bridge.config")
        self.assertEqual((c.interface, c.can_bitrate), ("usb-can-bridge", 1000000))

    def test_uart(self):
        self.assertEqual(load("klipper-ser.config").interface, "uart")

    def test_rp2040(self):
        c = load("klipper-rp.config")
        self.assertEqual((c.mcu, c.family, c.interface), ("rp2040", "rp2040", "usb"))
        self.assertEqual(load("klipper-rpcan.config").interface, "can")
        self.assertEqual(load("klipper-rpcan.config").can_bitrate, 500000)

    def test_out_of_scope_family(self):
        self.assertEqual(load("klipper-lpc.config").family, "other")

    def test_missing_or_foreign_file(self):
        self.assertIsNone(kconfig.parse_config(FIX / "nope.config"))
        p = FIX / "README-not-a-config.txt"
        p.write_text("hello\n")
        try:
            self.assertIsNone(kconfig.parse_config(p))
        finally:
            p.unlink()


class ConsistencyTests(unittest.TestCase):
    def test_profile_agrees(self):
        c = load("klipper-can.config")
        self.assertEqual(kconfig.check_against_profile(
            c, family="stm32", mcu="stm32g0b1", interface="can", can_bitrate=500000), [])

    def test_profile_contradictions_are_reported(self):
        c = load("klipper-can.config")
        probs = kconfig.check_against_profile(
            c, family="stm32", mcu="stm32f446", interface="usb", can_bitrate=1000000)
        self.assertEqual(len(probs), 2)           # bitrate is not compared when the profile is not CAN
        self.assertTrue(any("stm32f446" in p for p in probs))
        self.assertTrue(any("'can'" in p for p in probs))
        probs = kconfig.check_against_profile(
            c, family="stm32", mcu="stm32g0b1", interface="can", can_bitrate=1000000)
        self.assertTrue(any("500000" in p for p in probs))

    def test_katapult_klipper_offsets(self):
        kat = load("katapult-g0b1-8k.config", "katapult")
        self.assertEqual(kat.app_address, 0x8002000)
        self.assertIsNone(kconfig.check_offsets(load("klipper-usb.config"), kat))       # 8KiB == 8KiB
        msg = kconfig.check_offsets(load("klipper-usb.config"),
                                    load("katapult-g0b1-4k.config", "katapult"))
        self.assertIn("0x8002000", msg)
        self.assertIn("0x8001000", msg)
        self.assertIsNone(kconfig.check_offsets(None, kat))

    def test_rp2040_offsets_line_up(self):
        self.assertIsNone(kconfig.check_offsets(load("klipper-rp-16k.config"),
                                                load("katapult-rp.config", "katapult")))
        self.assertIsNotNone(kconfig.check_offsets(load("klipper-rp.config"),      # no bootloader: 0x10000100
                                                   load("katapult-rp.config", "katapult")))


if __name__ == "__main__":
    unittest.main()
