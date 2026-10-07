#!/usr/bin/env python3
"""Measure the ELECOM mouse's input report rate from /dev/input.

Each HID input report from the mouse interface produces one
EV_SYN/SYN_REPORT event, so counting them over a window gives the current
report rate -- while the mouse is being moved, the count per second is the
polling rate (e.g. ~1000 Hz or ~125 Hz).

Usage:
    python3 tools/measure-rate.py [SECONDS]

Requires read access to the mouse's /dev/input/event node (usually root or
the 'input' group).  No dependencies.
"""
import os
import re
import select
import struct
import sys
import time

EVENT = struct.Struct("llHHi")          # 64-bit Linux input_event, 24 bytes
EV_SYN, SYN_REPORT = 0x00, 0x00
EV_REL = 0x02


def find_event():
    """Locate the event node of the ELECOM mouse (movement interface)."""
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return None
    for block in blocks:
        if "056e" not in block.upper() and "ELECOM" not in block:
            continue
        if "EV=17" not in block:        # the relative-motion mouse collection
            continue
        m = re.search(r"Handlers=(.*)", block)
        if not m:
            continue
        for handler in m.group(1).split():
            if handler.startswith("event"):
                return "/dev/input/" + handler
    return None


def measure(seconds, dev):
    try:
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        print(f"error: cannot open {dev}: {e}", file=sys.stderr)
        return None
    try:
        # drain whatever is buffered
        while True:
            try:
                if not os.read(fd, EVENT.size * 64):
                    break
            except BlockingIOError:
                break
        syn = rel = 0
        t0 = time.monotonic()
        deadline = t0 + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([fd], [], [], left)
            if not r:
                break
            try:
                data = os.read(fd, EVENT.size * 256)
            except BlockingIOError:
                continue
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, typ, code, _ = EVENT.unpack_from(data, i)
                if typ == EV_SYN and code == SYN_REPORT:
                    syn += 1
                elif typ == EV_REL:
                    rel += 1
        dt = time.monotonic() - t0
    finally:
        os.close(fd)
    return syn / dt if dt else 0.0, syn, rel, dt


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    dev = find_event()
    if not dev:
        print("error: ELECOM mouse event device not found", file=sys.stderr)
        return 1
    print(f"measuring {dev} for {seconds:.0f} s -- move the mouse now!")
    result = measure(seconds, dev)
    if result is None:
        return 1
    hz, syn, rel, dt = result
    print(f"reports: {syn} SYN, {rel} REL over {dt:.1f} s")
    print(f"report rate: {hz:.0f} Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
