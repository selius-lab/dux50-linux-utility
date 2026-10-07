#!/usr/bin/env python3
"""Pure-logic unit tests for dux50 (no hardware required)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import dux50  # noqa: E402

CHUNK = 61          # bytes returned per read transaction


class FakeTransport(dux50.Dux50):
    """Dux50 with the USB layer replaced by an in-memory memory model."""

    def __init__(self, mem=None):
        self.info = {"pid": "00e9", "busnum": 1, "devnum": 1,
                     "iface": 1, "sysname": "1-1:1.1"}
        self.verbose = False
        self.detached = False
        self.fd = None
        self.mem = bytearray(mem if mem is not None else b"\xff" * 64)
        self.sent = []

    def close(self):
        pass

    def _send(self, payload, delay=0):
        self.sent.append(bytes(payload))

    def _get(self):
        """Answer the last read command from the memory model."""
        cmd = self.sent[-1]
        if not (cmd[0] == 0x01 and cmd[1] == 0x00):
            return bytes(64)                # non-read command: plain echo
        addr = cmd[3] | (cmd[4] << 8)
        payload = bytes(self.mem[addr:addr + CHUNK]).ljust(CHUNK, b"\xff")
        return (b"\x01\x01" + payload).ljust(64, b"\x00")


class FakeDevice(FakeTransport):
    """FakeTransport whose write() also updates the memory model."""

    def write(self, addr, data):
        super().write(addr, data)
        self.mem[addr:addr + len(data)] = data


class TestReadFraming(unittest.TestCase):
    def test_read_command_bytes(self):
        t = FakeTransport(bytes(range(64)))
        t.read(0, CHUNK)
        self.assertEqual(list(t.sent[-1]), [0x01, 0x00, 0x00, 0x00, 0x00])

    def test_read_chunks_and_reassembly(self):
        mem = bytes(range(256)) * 4
        t = FakeTransport(mem)
        out = t.read(0, 100)
        self.assertEqual(out, mem[:100])
        self.assertEqual(len(t.sent), 2)            # 61 + 39
        self.assertEqual(list(t.sent[1][3:5]), [CHUNK & 0xFF, CHUNK >> 8])

    def test_read_16bit_address(self):
        mem = b"\x00" * 0x100 + b"\xaa" + b"\xff" * 300
        t = FakeTransport(mem)
        self.assertEqual(t.read(0x100, 1), b"\xaa")
        self.assertEqual(list(t.sent[-1][3:5]), [0x00, 0x01])


class TestWriteFraming(unittest.TestCase):
    def test_write_single_block(self):
        t = FakeTransport(bytearray(64))
        data = bytes(range(10))
        t.write(0x20, data)
        self.assertEqual(len(t.sent), 2)            # write + apply
        self.assertEqual(list(t.sent[0][:5]), [0x01, 0x03, 0x20, 0x00, 10])
        self.assertEqual(list(t.sent[0][5:15]), list(data))
        self.assertEqual(list(t.sent[1][:2]), [0x01, 0x04])   # apply

    def test_write_chunks_at_32_bytes(self):
        t = FakeTransport(bytearray(128))
        data = bytes((i * 3) & 0xFF for i in range(80))
        t.write(0, data)
        self.assertEqual(len(t.sent), 4)            # 32 + 32 + 16 + apply
        self.assertEqual(t.sent[0][4], 32)
        self.assertEqual(t.sent[1][4], 32)
        self.assertEqual(t.sent[2][4], 16)

    def test_write_no_apply(self):
        t = FakeTransport(bytearray(64))
        t.write(0x20, b"\x01\x02", apply=False)
        self.assertEqual(len(t.sent), 1)

    def test_write_read_roundtrip(self):
        t = FakeDevice(bytearray(b"\x00" * dux50.CONFIG_SIZE))
        image = bytes((i * 7) & 0xFF for i in range(dux50.CONFIG_SIZE))
        t.write_config(image)
        self.assertEqual(t.read(0, 256), image[:256])


class TestDpiCommands(unittest.TestCase):
    def test_set_dpi_live(self):
        t = FakeTransport(bytearray(64))
        t.set_dpi_live(0x18, 0x18)
        self.assertEqual(list(t.sent[-1]), [0x01, 0x07, 0x01, 0x18, 0x18])

    def test_select_dpi_stage(self):
        t = FakeTransport(bytearray(64))
        t.select_dpi_stage(2)
        self.assertEqual(list(t.sent[-1]), [0x01, 0x07, 0x00, 0x02])

    def test_dpi_byte(self):
        self.assertEqual(dux50.dpi_byte(1200), 0x18)   # 24 x 50
        self.assertEqual(dux50.dpi_byte(12250), 0xF5)  # 245 x 50
        self.assertEqual(dux50.dpi_byte(100000), 0xFF)  # clamped


class TestReportRate(unittest.TestCase):
    def test_decode_known_values(self):
        self.assertEqual(dux50.decode_report_rate(bytes([0] * 16)), (0, 1000))
        cfg = bytearray(16)
        cfg[dux50.RATE_BASE] = 0x10
        self.assertEqual(dux50.decode_report_rate(bytes(cfg)), (1, 500))
        cfg[dux50.RATE_BASE] = 0x30
        self.assertEqual(dux50.decode_report_rate(bytes(cfg)), (3, 250))
        cfg[dux50.RATE_BASE] = 0x70
        self.assertEqual(dux50.decode_report_rate(bytes(cfg)), (7, 125))

    def test_decode_unknown(self):
        cfg = bytearray(16)
        cfg[dux50.RATE_BASE] = 0x20
        self.assertEqual(dux50.decode_report_rate(bytes(cfg)), (2, None))

    def test_rate_value(self):
        self.assertEqual(dux50.rate_value(1000), 0)
        self.assertEqual(dux50.rate_value(500), 1)
        self.assertEqual(dux50.rate_value(250), 3)
        self.assertEqual(dux50.rate_value(125), 7)
        with self.assertRaises(ValueError):
            dux50.rate_value(333)

    def test_decode_ignores_other_bits(self):
        cfg = bytearray(16)
        cfg[dux50.RATE_BASE] = 0x8F        # low bits set, rate field = 0
        self.assertEqual(dux50.decode_report_rate(bytes(cfg)), (0, 1000))


class TestHelpers(unittest.TestCase):
    def test_used_size(self):
        self.assertEqual(dux50.used_size(b"\x01\x02\xff\xff"), 2)
        self.assertEqual(dux50.used_size(b"\xff\xff"), 0)

    def test_extract_utf16_strings(self):
        data = b"\x00\xff" + "TestMacro".encode("utf-16-le") + b"\x00\x00" * 3
        strings = [s for _, s in dux50.extract_utf16_strings(data)]
        self.assertIn("TestMacro", strings)

    def test_extract_utf16_odd_alignment(self):
        data = b"\x99" + "hello".encode("utf-16-le") + b"\xff\xff"
        strings = [s for _, s in dux50.extract_utf16_strings(data)]
        self.assertIn("hello", strings)

    def test_hexdump(self):
        out = dux50.hexdump(b"\x00\x01\x02\x03")
        self.assertIn("000000", out)
        self.assertIn("00 01 02 03", out)


class TestButtonMapping(unittest.TestCase):
    def test_names_match_count(self):
        self.assertEqual(len(dux50.BTN_NAMES), dux50.BTN_COUNT)

    def test_known_slots(self):
        # index -> physical button (M-DUX50, verified on hardware)
        expect = {0: "Left click", 1: "Right click", 2: "Wheel click",
                  3: "G1", 4: "G3", 5: "G4", 6: "G5", 7: "Tilt R",
                  8: "Tilt L", 9: "G2", 15: "G11"}
        for idx, name in expect.items():
            self.assertEqual(dux50.BTN_NAMES[idx], name)

    def test_decode_buttons_count(self):
        rows = dux50.decode_buttons(bytes(dux50.CONFIG_SIZE), 0)
        self.assertEqual(len(rows), dux50.BTN_COUNT)

    def test_slot_offsets(self):
        # arrays sit at base+0x40/0x58/0x70/0x88, plus the button index
        self.assertEqual(dux50.btn_off(0, 3, "a"), 0x43)
        self.assertEqual(dux50.btn_off(0, 3, "b"), 0x5B)
        self.assertEqual(dux50.btn_off(0, 3, "c"), 0x73)
        self.assertEqual(dux50.btn_off(0, 3, "d"), 0x8B)
        self.assertEqual(dux50.btn_off(1, 0, "a"), 0xC0)


if __name__ == "__main__":
    unittest.main()
