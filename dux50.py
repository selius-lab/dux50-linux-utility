#!/usr/bin/env python3
"""
dux50 - ELECOM M-DUX30 / M-DUX50 gaming mouse configuration tool for Linux.

Pure Python 3 standard library - no kernel module, no dependencies.

The mouse exposes a vendor HID interface (usage page 0xFF18).  The vendor
interface is driven with HID Feature Reports; on Linux the kernel hidraw
path does not deliver those reports for this device, so this tool talks to
the device through usbfs (USBDEVFS_CONTROL) and temporarily detaches usbhid
from the vendor interface while running (the driver is re-attached on exit).

Protocol: 64-byte HID feature reports, report id 1, checked with
report[63] = XOR(report[1..62]).  Commands are the payload after the
report-ID byte (bytes after the command are ignored):

    read       {01 00 00 ADDR_LO ADDR_HI}       -> 61 bytes in resp[2..62]
    write      {01 03 ADDR_LO ADDR_HI LEN d..}  -> write <= 0x20 bytes
    apply      {01 04 00 00 00 00 00 00}        -> commit / reload
    dpi stage  {01 07 00 STAGE}                 -> switch active DPI stage
    dpi live   {01 07 01 X Y}                   -> apply DPI to current stage
    session    {00 12 12}                       -> sent at start-up

The mouse's persistent configuration is a 4096-byte image at address 0
(0xFF past the used area = erased flash).  It holds the profile/button
tables, DPI values and macro bytecode.
"""

import argparse
import ctypes
import fcntl
import glob
import os
import re
import sys
import time

VID = "056e"
PIDS = {"00e7": "M-DUX30/50 series", "00e8": "M-DUX30/50 series",
        "00e9": "M-DUX50", "00ea": "M-DUX30/50 series"}
CONFIG_SIZE = 0x1000

USBDEVFS_CONTROL = 0xC0185500
USBDEVFS_DISCONNECT_CLAIM = 0x8108551B


class Ctrl(ctypes.Structure):
    _fields_ = [("bRequestType", ctypes.c_ubyte), ("bRequest", ctypes.c_ubyte),
                ("wValue", ctypes.c_ushort), ("wIndex", ctypes.c_ushort),
                ("wLength", ctypes.c_ushort), ("wTimeout", ctypes.c_uint),
                ("data", ctypes.c_void_p)]


class DisconnectClaim(ctypes.Structure):
    _fields_ = [("interface", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("driver", ctypes.c_char * 256)]


def find_device():
    """Locate the vendor interface of the mouse on the host.

    Returns dict with busnum, devnum, sysname (e.g. '4-1:1.1') or None.
    """
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            with open(dev + "/idVendor") as f:
                vid = f.read().strip()
            with open(dev + "/idProduct") as f:
                pid = f.read().strip()
        except OSError:
            continue
        if vid != VID or pid not in PIDS:
            continue
        info = {"pid": pid}
        try:
            info["busnum"] = int(open(dev + "/busnum").read())
            info["devnum"] = int(open(dev + "/devnum").read())
        except OSError:
            continue
        # pick the interface whose HID report descriptor uses page 0xFF18
        info["iface"] = None
        info["sysname"] = None
        name = os.path.basename(dev)
        for ifdir in sorted(glob.glob(f"{dev}:*")):
            try:
                bnum = int(open(ifdir + "/bInterfaceNumber").read())
            except OSError:
                continue
            hidmod = os.path.join(ifdir, "driver")
            # find the hidraw node for this interface to read the descriptor
            for hid in glob.glob(f"{ifdir}/0003:*"):
                desc = os.path.join(hid, "report_descriptor")
                try:
                    with open(desc, "rb") as f:
                        d = f.read()
                except OSError:
                    continue
                if b"\x06\x18\xff" in d:
                    info["iface"] = bnum
                    info["sysname"] = f"{name}:1.{bnum}"
        if info["iface"] is None:
            info["iface"] = 1
            info["sysname"] = f"{name}:1.1"
        return info
    return None


def rebind_driver(sysname):
    """Re-attach usbhid to the vendor interface after use."""
    try:
        with open("/sys/bus/usb/drivers/usbhid/bind", "w") as f:
            f.write(sysname)
    except OSError:
        pass


class Dux50:
    def __init__(self, info, verbose=False):
        self.info = info
        self.verbose = verbose
        self.detached = False
        self.path = f"/dev/bus/usb/{info['busnum']:03d}/{info['devnum']:03d}"
        self.iface = info["iface"]
        self.fd = os.open(self.path, os.O_RDWR)
        dc = DisconnectClaim()
        dc.interface = self.iface
        dc.flags = 0
        try:
            fcntl.ioctl(self.fd, USBDEVFS_DISCONNECT_CLAIM, dc)
            self.detached = True
        except OSError:
            pass

    def close(self):
        if self.fd is None:
            return
        os.close(self.fd)
        self.fd = None
        if self.detached:
            time.sleep(0.2)
            rebind_driver(self.info["sysname"])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # --- low level ---
    def _xfer(self, reqtype, req, val, data=b"", length=None):
        if length is None:
            length = len(data)
        buf = ctypes.create_string_buffer(max(length, 1))
        if data:
            ctypes.memmove(buf, bytes(data), len(data))
        c = Ctrl(reqtype, req, val, self.iface, length, 2000,
                 ctypes.addressof(buf))
        r = fcntl.ioctl(self.fd, USBDEVFS_CONTROL, c)
        return bytes(buf[:min(r, length)])

    def _send(self, payload, delay=0.004):
        # 64-byte report: [0] must be the report id (1); the command follows.
        # The tool's tx path also sets [63] = XOR([1..62]).
        tx = bytearray(64)
        tx[0] = 1
        tx[1:1 + len(payload)] = bytes(payload)
        ck = 0
        for i in range(1, 63):
            ck ^= tx[i]
        tx[63] = ck
        self._xfer(0x21, 0x09, (3 << 8) | 1, tx)
        if self.verbose:
            print("  tx:", bytes(payload).hex(" "))
        if delay:
            time.sleep(delay)

    def _get(self):
        d = self._xfer(0xA1, 0x01, (3 << 8) | 1, length=64)
        if self.verbose:
            print("  rx:", d[:16].hex(" ") + (" ..." if len(d) > 16 else ""))
        return d

    # --- protocol (64-byte feature reports, vendor usage page 0xFF18) ---
    def read(self, addr, length):
        # {01 00 00 A0 A1}: read 61 bytes from the 16-bit address A.
        out = bytearray()
        while len(out) < length:
            self._send([0x01, 0x00, 0x00, addr & 0xFF, (addr >> 8) & 0xFF], delay=0.003)
            out += self._get()[2:63]
            addr += 61
        return bytes(out[:length])

    def apply(self):
        # {01 04 ...}: commit / reload.  The device needs this after a group of
        # writes -- WITHOUT it the device keeps using the previous settings
        # even though the config bytes changed (verified on hardware: a button
        # remap only takes effect after this command).
        self._send([0x01, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00], delay=0.25)
        return self._get()

    def set_dpi_live(self, xbyte, ybyte):
        # {01 07 01 X Y}: push X/Y to the *current* DPI stage.
        # Verified on hardware: the pointer speed changes immediately.
        self._send([0x01, 0x07, 0x01, xbyte & 0xFF, ybyte & 0xFF], delay=0.05)
        return self._get()

    def select_dpi_stage(self, stage):
        # {01 07 00 STAGE}: make STAGE the active DPI stage.
        # The mouse reloads the stage's stored value.
        self._send([0x01, 0x07, 0x00, stage & 0xFF], delay=0.05)
        return self._get()

    def write(self, addr, data, apply=True):
        # {01 03 A0 A1 LEN d0..}: block write, <= 32 bytes.
        idx = 0
        while idx < len(data):
            chunk = data[idx:idx + 0x20]
            self._send([0x01, 0x03, addr & 0xFF, (addr >> 8) & 0xFF, len(chunk)]
                       + list(chunk), delay=0.06)
            addr += len(chunk)
            idx += 0x20
        if apply:
            self.apply()

    def read_config(self):
        return self.read(0, CONFIG_SIZE)

    def write_config(self, image):
        if len(image) != CONFIG_SIZE:
            raise ValueError(f"config image must be {CONFIG_SIZE} bytes")
        self.write(0, image)


# ---------------------------------------------------------------- dump/decode

def hexdump(data, base=0):
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + i:06x}  {hx:<47}  |{asc}|")
    return "\n".join(lines)


def extract_utf16_strings(data, minlen=3):
    """Find UTF-16LE printable runs (macro/profile names)."""
    out = []
    n = len(data)
    for align in (0, 1):
        i = align
        while i + 1 < n:
            # a UTF-16LE printable char is (0x20..0x7e, 0x00)
            if 0x20 <= data[i] < 0x7f and data[i + 1] == 0:
                j = i
                s = []
                while j + 1 < n and 0x20 <= data[j] < 0x7f and data[j + 1] == 0:
                    s.append(chr(data[j]))
                    j += 2
                if len(s) >= minlen:
                    out.append((i, "".join(s)))
                i = j
                continue
            i += 2
    out.sort()
    # drop duplicates from the two alignments
    seen, uniq = set(), []
    for off, s in out:
        if (off, s) not in seen:
            seen.add((off, s))
            uniq.append((off, s))
    return uniq


def used_size(cfg):
    """Bytes up to the last non-0xFF byte (0xFF = erased flash)."""
    last = -1
    for i, b in enumerate(cfg):
        if b != 0xFF:
            last = i
    return last + 1


def decode_dump(cfg):
    """Human readable structural view of the 4KB config image."""
    used = used_size(cfg)
    print(f"config size      : {len(cfg)} bytes (used: {used} bytes, "
          f"rest 0xFF = erased)")
    print(f"header [0x000-10] : {cfg[:16].hex(' ')}")
    # records: [00 00 0T ...] markers
    print("records ([00 00 TT ..] markers):")
    for m in re.finditer(rb"\x00\x00([\x01-\x0f])(?=[\x00-\xff])", cfg):
        off = m.start()
        typ = m.group(1)[0]
        snippet = cfg[off:off + 20].hex(" ")
        print(f"  0x{off:04x} type={typ:#04x}  {snippet}")
    strings = extract_utf16_strings(cfg)
    if strings:
        print("UTF-16 strings (macro/profile names):")
        for off, s in strings:
            print(f"  0x{off:04x}  {s!r}")


# ------------------------------------------------------------ button mapping
"""Button records (observed on hardware).

Each profile owns four parallel byte arrays of 24 entries (base = profile*0x80):

    A (func type)  at base+0x40+i      C (param)  at base+0x70+i
    B (param)      at base+0x58+i      D (param)  at base+0x88+i

Button i's fields are the i-th byte of each array.  `A` is the function type
(see TYPE_NAMES), `C` is the HID keycode when A == Keyboard, `D` carries the
macro index for the macro types.  A button edit writes all four bytes at once.
"""

BTN_ARRAYS = {"a": 0x40, "b": 0x58, "c": 0x70, "d": 0x88}
BTN_COUNT = 16
# config slot -> physical button (M-DUX50, verified on hardware):
#   0 Left  1 Right  2 Wheel  3 G1  4 G3  5 G4  6 G5
#   7 TiltR  8 TiltL  9 G2  10 G6  11 G7  12 G8  13 G9  14 G10  15 G11
BTN_NAMES = ["Left click", "Right click", "Wheel click", "G1", "G3", "G4",
             "G5", "Tilt R", "Tilt L", "G2", "G6", "G7", "G8", "G9",
             "G10", "G11"]
TYPE_NAMES = {
    0: "Default", 1: "No function", 2: "HID Key1", 3: "HID Key2", 4: "Mouse",
    5: "Keyboard", 6: "Multimedia", 8: "Macro", 9: "Macro(R)", 10: "Macro(F)",
    12: "DPI+", 13: "DPI-", 14: "DPI loop", 15: "DPI firekey",
    16: "DPI hotkey", 17: "Profile switch", 18: "Adjust lift",
    19: "LED on/off", 20: "Angle snapping", 21: "Adjust DPI",
    22: "Adjust Button", 23: "Profile/DPI switch", 30: "Lock X-axis",
    31: "Lock Y-axis",
}


def btn_off(profile, index, arr):
    return profile * 0x80 + BTN_ARRAYS[arr] + index


# ------------------------------------------------------------ DPI stages
# Stage values live at config[3+2i] (X) and config[4+2i] (Y); the value is
# stored unconverted and shown as x50.  So: dpi = stored_byte * 50.
DPI_BASE = 0x03
DPI_STAGES = 5
DPI_UNIT = 50


def dpi_off(stage, axis):
    return DPI_BASE + stage * 2 + axis


def decode_dpi(cfg):
    out = []
    for s in range(DPI_STAGES):
        x = cfg[dpi_off(s, 0)]
        y = cfg[dpi_off(s, 1)]
        out.append((s, x, y, x * DPI_UNIT, y * DPI_UNIT))
    return out


def dpi_byte(dpi):
    return max(0, min(255, round(dpi / DPI_UNIT)))


# ------------------------------------------------------------ report rate
#   config[0x0e] = (old & ~0x70) | (value << 4)
# The stored field is (2**k - 1) for the k-th option, so the four standard
# rates map to 0, 1, 3, 7.  Verified on hardware by measuring the input
# report rate: 0 -> 1000 Hz, 1 -> 500 Hz, 3 -> 250 Hz, 7 -> 125 Hz.
RATE_BASE = 0x0e
RATE_SHIFT = 4
RATE_MASK = 0x70
RATE_VALUES = [(0, 1000), (1, 500), (3, 250), (7, 125)]


def decode_report_rate(cfg):
    """Return (field_value, hz_or_None) for the config's report-rate field."""
    val = (cfg[RATE_BASE] & RATE_MASK) >> RATE_SHIFT
    return val, dict(RATE_VALUES).get(val)


def rate_value(hz):
    for val, value in RATE_VALUES:
        if value == hz:
            return val
    raise ValueError(f"unsupported report rate: {hz} Hz "
                     f"(choose from {[v for _, v in RATE_VALUES]})")


def hid_key_name(code):
    """Best-effort HID usage id -> character."""
    if 0x04 <= code <= 0x1D:
        return chr(ord('a') + code - 0x04)
    if 0x1E <= code <= 0x26:
        return chr(ord('1') + code - 0x1E)
    if code == 0x27:
        return "0"
    if code == 0x28:
        return "Enter"
    if code == 0x2C:
        return "Space"
    if code == 0x29:
        return "Esc"
    if code == 0x2B:
        return "Tab"
    return None


def decode_buttons(cfg, profile=0):
    rows = []
    for i in range(BTN_COUNT):
        t = cfg[btn_off(profile, i, "a")]
        b = cfg[btn_off(profile, i, "b")]
        c = cfg[btn_off(profile, i, "c")]
        d = cfg[btn_off(profile, i, "d")]
        name = BTN_NAMES[i] if i < len(BTN_NAMES) else f"#{i}"
        tname = TYPE_NAMES.get(t, f"type {t}")
        extra = ""
        if t in (2, 3, 5) and c:
            key = hid_key_name(c)
            extra = f"  key=0x{c:02x}" + (f" ('{key}')" if key else "")
        elif t in (8, 9, 10) and d:
            extra = f"  macro={d}"
        elif t or b or c or d:
            extra = f"  b=0x{b:02x} c=0x{c:02x} d=0x{d:02x}"
        rows.append((i, name, t, tname, extra))
    return rows


def main():
    ap = argparse.ArgumentParser(prog="dux50",
                                 description="ELECOM M-DUX50/30 config tool")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect", help="locate the mouse")
    sub.add_parser("info", help="show device information")

    p = sub.add_parser("read", help="read config memory (hexdump)")
    p.add_argument("offset", type=lambda x: int(x, 0), nargs="?", default=0)
    p.add_argument("length", type=lambda x: int(x, 0), nargs="?",
                   default=CONFIG_SIZE)

    p = sub.add_parser("dump", help="annotated structural dump")
    p.add_argument("-o", "--out", help="also save raw image to this file")

    p = sub.add_parser("backup", help="save the full config to a file")
    p.add_argument("file", nargs="?", help="output file (default: timestamped)")

    p = sub.add_parser("restore", help="write a config image file to the mouse")
    p.add_argument("file")
    p.add_argument("--yes", action="store_true", help="skip confirmation")

    p = sub.add_parser("verify", help="compare the mouse against an image file")
    p.add_argument("file")

    p = sub.add_parser("write", help="write raw bytes at an address (dangerous)")
    p.add_argument("offset", type=lambda x: int(x, 0))
    p.add_argument("hexdata")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("buttons", help="list the button assignments")
    p.add_argument("--profile", type=int, default=0)

    p = sub.add_parser("button", help="change one button assignment")
    p.add_argument("index", type=int,
                   help="0=left,1=right,2=wheel,3=G1,4=G3,5=G4,6=G5,"
                        "7=tiltR,8=tiltL,9=G2,10=G6,11=G7,12=G8,13=G9,"
                        "14=G10,15=G11")
    p.add_argument("--profile", type=int, default=0)
    p.add_argument("--type", type=lambda x: int(x, 0),
                   help="function type (5=Keyboard, 8/9/10=Macro, 12=DPI+, ...)")
    p.add_argument("--b", type=lambda x: int(x, 0), help="B byte")
    p.add_argument("--c", type=lambda x: int(x, 0),
                   help="C byte (HID keycode when --type 5)")
    p.add_argument("--d", type=lambda x: int(x, 0),
                   help="D byte (macro index for macro types)")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--key", help="convenience: single character for --type 5")

    p = sub.add_parser("dpi", help="list the 5 DPI stages")
    p = sub.add_parser("use-dpi", help="switch the active DPI stage")
    p.add_argument("stage", type=int, help="0..4")
    p = sub.add_parser("report-rate", help="show the configured polling rate")
    p = sub.add_parser("set-report-rate", help="set the polling rate (Hz)")
    p.add_argument("hz", type=int, help="1000, 500, 250 or 125")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("set-dpi", help="set one DPI stage (dpi = byte x %d)" % DPI_UNIT)
    p.add_argument("stage", type=int, help="0..4")
    p.add_argument("x", type=int, help="X dpi")
    p.add_argument("y", type=int, nargs="?", help="Y dpi (default: same as X)")
    p.add_argument("--no-live", action="store_true",
                   help="only store the value, do not change the speed now")
    p.add_argument("--yes", action="store_true")

    args = ap.parse_args()

    info = find_device()
    if not info:
        print("error: ELECOM M-DUX50/30 not found (VID 056e PID 00e7-00ea)",
              file=sys.stderr)
        return 1
    if args.cmd == "detect":
        print(f"bus {info['busnum']} dev {info['devnum']} "
              f"interface {info['iface']} ({info['sysname']}) "
              f"pid 0x{info['pid']} {PIDS[info['pid']]}")
        return 0

    with Dux50(info, verbose=args.verbose) as dev:
        try:
            if args.cmd == "info":
                print(f"device     : {dev.path} interface {dev.iface} "
                      f"(pid 0x{info['pid']} {PIDS[info['pid']]})")
                cfg = dev.read_config()
                print(f"config     : {CONFIG_SIZE} bytes, {used_size(cfg)} bytes used")
                strings = extract_utf16_strings(cfg)
                names = [s for _, s in strings if len(s) >= 3]
                if names:
                    print("names      : " + ", ".join(names[:12]) +
                          (" ..." if len(names) > 12 else ""))
            elif args.cmd == "read":
                print(hexdump(dev.read(args.offset, args.length), args.offset))
            elif args.cmd == "dump":
                cfg = dev.read_config()
                if args.out:
                    with open(args.out, "wb") as f:
                        f.write(cfg)
                    print(f"saved raw image to {args.out}\n")
                decode_dump(cfg)
                print()
                print(hexdump(cfg[:0x300]))
            elif args.cmd == "backup":
                cfg = dev.read_config()
                path = args.file or time.strftime("dux50-backup-%Y%m%d-%H%M%S.bin")
                with open(path, "wb") as f:
                    f.write(cfg)
                print(f"saved {len(cfg)} bytes to {path}")
            elif args.cmd == "verify":
                with open(args.file, "rb") as f:
                    want = f.read()
                got = dev.read_config()
                if got == want:
                    print("OK: mouse config matches the file")
                else:
                    diffs = [i for i in range(min(len(got), len(want)))
                             if got[i] != want[i]]
                    print(f"DIFFERS: {len(diffs)} differing bytes "
                          f"(first at 0x{diffs[0]:04x})" if diffs else "DIFFERS")
                    return 3
            elif args.cmd == "restore":
                with open(args.file, "rb") as f:
                    image = f.read()
                if len(image) != CONFIG_SIZE:
                    print(f"error: image must be {CONFIG_SIZE} bytes "
                          f"(got {len(image)})", file=sys.stderr)
                    return 2
                if not args.yes:
                    print(f"about to overwrite the mouse config with {args.file}")
                    print("pass --yes to proceed (a backup is written first)")
                    return 2
                before = dev.read_config()
                if image == before:
                    print("the config on the mouse already matches the file; "
                          "nothing to write")
                    return 0
                backup = time.strftime("dux50-pre-restore-%Y%m%d-%H%M%S.bin")
                with open(backup, "wb") as f:
                    f.write(before)
                print(f"backed up current config to {backup}")
                dev.write_config(image)
                time.sleep(0.4)
                got = dev.read_config()
                if got == image:
                    print("restore OK (verified)")
                    return 0
                if got == before:
                    print("ERROR: the mouse ignored the write - the config is "
                          "unchanged.\n"
                          "The device only accepts writes after a configuration\n"
                          "session has been run against it; see "
                          "docs/PROTOCOL.md.", file=sys.stderr)
                    return 4
                diffs = [i for i in range(CONFIG_SIZE) if got[i] != image[i]]
                print(f"WARNING: verify mismatch ({len(diffs)} bytes, "
                      f"first 0x{diffs[0]:04x})", file=sys.stderr)
                return 3
            elif args.cmd == "write":
                if not args.yes:
                    print("refusing without --yes", file=sys.stderr)
                    return 2
                data = bytes.fromhex(args.hexdata)
                before = dev.read(args.offset, len(data))
                dev.write(args.offset, data)
                time.sleep(0.2)
                got = dev.read(args.offset, len(data))
                if got == data:
                    print(f"wrote {len(data)} bytes at 0x{args.offset:x} (verified)")
                elif got == before:
                    print("ERROR: the mouse ignored the write (see "
                          "docs/PROTOCOL.md)", file=sys.stderr)
                    return 4
                else:
                    print(f"unexpected readback: {got.hex(' ')}", file=sys.stderr)
                    return 3
            elif args.cmd == "buttons":
                cfg = dev.read_config()
                print(f"buttons (profile {args.profile}):")
                for i, name, t, tname, extra in decode_buttons(cfg, args.profile):
                    print(f"  {i:2d} {name:<12} {tname:<15} "
                          f"(type=0x{t:02x}){extra}")
            elif args.cmd == "button":
                if not 0 <= args.index < BTN_COUNT:
                    print(f"error: index must be 0..{BTN_COUNT - 1}",
                          file=sys.stderr)
                    return 2
                t, c = args.type, args.c
                if args.key is not None:
                    ch = args.key.lower()
                    if len(ch) != 1:
                        print("error: --key takes a single character",
                              file=sys.stderr)
                        return 2
                    if t is None:
                        t = 5
                    if 'a' <= ch <= 'z':
                        c = 0x04 + ord(ch) - ord('a')
                    elif '1' <= ch <= '9':
                        c = 0x1E + ord(ch) - ord('1')
                    elif ch == '0':
                        c = 0x27
                    else:
                        print(f"error: unsupported key {args.key!r}",
                              file=sys.stderr)
                        return 2
                targets = [(a, v) for a, v
                           in (("a", t), ("b", args.b), ("c", c), ("d", args.d))
                           if v is not None]
                if not targets:
                    print("nothing to change (use --type/--b/--c/--d/--key)",
                          file=sys.stderr)
                    return 2
                for _, v in targets:
                    if not 0 <= v <= 0xFF:
                        print("error: values must be 0..255", file=sys.stderr)
                        return 2
                if not args.yes:
                    desc = ", ".join(f"{a}={v:#04x}" for a, v in targets)
                    print(f"about to set button {args.index} "
                          f"(profile {args.profile}): {desc}")
                    print("pass --yes to proceed")
                    return 2
                for arr, val in targets:
                    dev.write(btn_off(args.profile, args.index, arr),
                              bytes([val]))
                    time.sleep(0.1)
                time.sleep(0.3)
                cfg = dev.read_config()
                if all(cfg[btn_off(args.profile, args.index, a)] == v
                       for a, v in targets):
                    print("OK: " + ", ".join(f"{a}=0x{v:02x}"
                                             for a, v in targets))
                else:
                    print("ERROR: write not reflected (device may need a "
                          "configuration session first)", file=sys.stderr)
                    return 4
                for i, name, tt, tname, extra in decode_buttons(cfg, args.profile):
                    if i == args.index:
                        print(f"  now: {name} = {tname} "
                              f"(type=0x{tt:02x}){extra}")
            elif args.cmd == "dpi":
                cfg = dev.read_config()
                print(f"DPI stages (dpi = byte x {DPI_UNIT}):")
                for s, x, y, dx, dy in decode_dpi(cfg):
                    mark = "" if x == y else "  (X != Y)"
                    print(f"  stage {s}: X=0x{x:02x} ({dx} dpi)  "
                          f"Y=0x{y:02x} ({dy} dpi){mark}")
            elif args.cmd == "use-dpi":
                if not 0 <= args.stage < DPI_STAGES:
                    print(f"error: stage must be 0..{DPI_STAGES - 1}",
                          file=sys.stderr)
                    return 2
                cfg = dev.read_config()
                xb = cfg[dpi_off(args.stage, 0)]
                yb = cfg[dpi_off(args.stage, 1)]
                dev.select_dpi_stage(args.stage)
                time.sleep(0.2)
                print(f"OK: active DPI stage {args.stage} "
                      f"(stored X={xb * DPI_UNIT} dpi / "
                      f"Y={yb * DPI_UNIT} dpi)")
            elif args.cmd == "report-rate":
                cfg = dev.read_config()
                val, hz = decode_report_rate(cfg)
                shown = f"{hz} Hz" if hz else f"unknown (value {val})"
                print(f"report rate: {shown}  (config[0x{RATE_BASE:02x}] = "
                      f"0x{cfg[RATE_BASE]:02x})")
            elif args.cmd == "set-report-rate":
                if args.hz not in [v for _, v in RATE_VALUES]:
                    print(f"error: rate must be one of "
                          f"{[v for _, v in RATE_VALUES]}", file=sys.stderr)
                    return 2
                val = rate_value(args.hz)
                before = dev.read_config()
                if not args.yes:
                    old = decode_report_rate(before)[1]
                    print(f"about to change the report rate "
                          f"{old if old else '?'} Hz -> {args.hz} Hz")
                    print("pass --yes to proceed (a backup is written first)")
                    return 2
                backup = time.strftime("dux50-pre-rate-%Y%m%d-%H%M%S.bin")
                with open(backup, "wb") as f:
                    f.write(before)
                new = (before[RATE_BASE] & ~RATE_MASK) | (val << RATE_SHIFT)
                for attempt in range(3):
                    dev.write(RATE_BASE, bytes([new]))
                    time.sleep(0.4)
                    cfg = dev.read_config()
                    if cfg[RATE_BASE] == new:
                        break
                got_val, got_hz = decode_report_rate(cfg)
                if got_val == val:
                    print(f"OK: report rate = {got_hz} Hz (backup: {backup})")
                    print("note: the new rate takes effect immediately; "
                          "re-plug the mouse if it does not")
                else:
                    print("ERROR: write not reflected", file=sys.stderr)
                    return 4
            elif args.cmd == "set-dpi":
                if not 0 <= args.stage < DPI_STAGES:
                    print(f"error: stage must be 0..{DPI_STAGES - 1}",
                          file=sys.stderr)
                    return 2
                y = args.y if args.y is not None else args.x
                xb, yb = dpi_byte(args.x), dpi_byte(y)
                if not args.yes:
                    print(f"about to set DPI stage {args.stage} to "
                          f"X={args.x} Y={y} (bytes 0x{xb:02x} 0x{yb:02x})")
                    print("pass --yes to proceed")
                    return 2
                # config write (0x03+2*stage) then the live command
                # {01 07 01 X Y} which applies to the current stage
                before = dev.read_config()
                backup = time.strftime("dux50-pre-dpi-%Y%m%d-%H%M%S.bin")
                with open(backup, "wb") as f:
                    f.write(before)
                dev.write(dpi_off(args.stage, 0), bytes([xb, yb]), apply=False)
                if not args.no_live:
                    dev.set_dpi_live(xb, yb)
                time.sleep(0.15)
                dev.apply()
                time.sleep(0.2)
                cfg = dev.read_config()
                got_x = cfg[dpi_off(args.stage, 0)]
                got_y = cfg[dpi_off(args.stage, 1)]
                if (got_x, got_y) == (xb, yb):
                    live = "" if args.no_live else " (live applied)"
                    print(f"OK: stage {args.stage} = X {got_x * DPI_UNIT} dpi / "
                          f"Y {got_y * DPI_UNIT} dpi{live} "
                          f"(backup: {backup})")
                else:
                    print("ERROR: write not reflected", file=sys.stderr)
                    return 4
        finally:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
