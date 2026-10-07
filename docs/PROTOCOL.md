# ELECOM M-DUX50 / M-DUX30 — HID protocol (observed on hardware)

Device: `056e:00e9` (M-DUX50). Two interfaces:

| iface | collection | reports |
|---|---|---|
| 0 | mouse | consumer-page feature report (64 B, no report id) — not used |
| 1 | vendor `0xFF18` | **feature report, report id 1, 63 data bytes** ← config channel |
| 1 | consumer / keyboard | input reports id 3 / id 4 (remapped keys) |

## Transport

`SET_REPORT` / `GET_REPORT`, `bmRequestType = 0x21 / 0xA1`,
`bRequest = 0x09 / 0x01`, `wValue = (3 << 8) | 1` (feature, report id 1),
`wIndex = 1`, **64 data bytes**.

Note: the 64-byte report *includes* the report-id byte at `[0]`; the device
parses the command starting at `[1]`. (`HidD_SetFeature` behaves the same way.)

The host tool uses **usbfs** (`USBDEVFS_CONTROL`), because the kernel's hidraw
path does not deliver this device's feature reports.

Every report ends with `report[63] = XOR(report[1..62])` (verified on hardware).

## Commands

Both commands were verified on real hardware.

### Read memory

```
report[1] = 0x01, [2] = 0x00, [3] = 0x00, [4] = addr & 0xff, [5] = addr >> 8
```

* `addr` is a **16-bit byte address**.
* The device answers with 61 payload bytes in `resp[2..62]`
  (`resp[1]` echoes the opcode).
* Reading is a plain flash read: 4096 bytes ≈ 0.5 s.

### Write memory

```
report[1] = 0x01, [2] = 0x03,
report[3] = addr & 0xff, [4] = addr >> 8,
report[5] = len, report[6..] = data
```

* `len` up to 0x20; verified with both 1-byte and 4-byte writes.
* Single bytes are written one per setting byte.
* After a write the value is immediately visible to a read (verified).

### Other opcodes observed

| opcode | meaning |
|---|---|
| `{01, 00, ...}` | read memory |
| `{01, 03, ...}` | write memory |
| `{01, 04, ...}` | commit / reload (emitted after a group of writes) |
| `{01, 07, 00, stage}` | switch the active DPI stage |
| `{01, 07, 01, X, Y}` | apply DPI byte pair to the active stage |
| `{00, 12, 12}` | emitted at start-up / before a write group |
| `{00, 01, 06, 02, 02, ...}` / `{00, 01, 00, 01, W, 0}` | start-up reads |

The command dispatch is driven by `report[1]`: `& 3 == 1` performs a memory
read (this is why every attempted opcode behaved like a read).

## Known caveat

The device only accepted writes after a configuration session
(`{01,04,…}` commands) had been run against it.  With a device that has never
been configured, writes may be ignored until that state is reached — verify
after a re-plug with `dux50.py write`.

## Configuration layout (4 KB image)

Addresses 0x0000-0x0fff hold the configuration; the same image is mirrored at
0x2000, 0x4000 … 0xe000 (8 identical copies). 0xffff:0xfffe hold a `12 00`
signature.

First bytes:

```
0x0000  12 00 01 10 10 18 18 30 30 46 46 f5 12 00 10 00
0x0020-0x025f  5 x 0x80-byte profile records
0x02a0  60 04 7a 00 da 04 54 00 2e 05 5f 00   (delta-coded value table)
0x0400+ macros (UTF-16 names + bytecode; opcodes 0x83/0x84/0x88/0x89 …)
```

**Button assignments.** 5 profiles of 0x80 bytes at `0x20 + profile*0x80`. A
profile owns four parallel byte arrays (base = profile*0x80); button *i*'s
fields are the *i*-th byte of each array:

| array | offset | meaning |
|---|---|---|
| A | base+0x40+i | function type (0=Default, 4=Mouse, 5=Keyboard, 8/9/10=Macro, 12/13=DPI+/-, …) |
| B | base+0x58+i | parameter (mouse action / flags) |
| C | base+0x70+i | parameter — HID keycode when type = 5 |
| D | base+0x88+i | parameter — macro index for the macro types |

The 16 slots map to the physical buttons as follows (verified on hardware):

| i | button | i | button |
|---|---|---|---|
| 0 | Left | 8 | Tilt left |
| 1 | Right | 9 | G2 (profile switch) |
| 2 | Wheel click | 10 | G6 |
| 3 | G1 | 11 | G7 |
| 4 | G3 | 12 | G8 |
| 5 | G4 | 13 | G9 |
| 6 | G5 | 14 | G10 |
| 7 | Tilt right | 15 | G11 |

A button edit writes the same offset `p*0x80 + i` in all four arrays (observed:
`{01 03 43 00 01 09}` = A[3], `{01 03 5b 00 01 4f}` = B[3],
`{01 03 73 00 01 05}` = C[3], `{01 03 8b 00 01 00}` = D[3]).

A real edit (Right click → Mouse = A[1]=0x04, B[1]=0x02; Side1 → Keyboard 't' =
A[3]=0x05, C[3]=0x17) decodes to exactly that. `dux50.py buttons` prints this
table, `dux50.py button` edits it.

### Scalar settings — addresses touched

| address | access | notes |
|---|---|---|
| 0x02 | r/w | "current profile" |
| 0x03..0x0c | r/w | **5 DPI stages, 2 bytes each (X, Y)** — see below |
| 0x0d,0x0e,0x0f | r/w | scalar settings — see "Other settings" below |
| 0x40/0x58/0x70/0x88 + i | w | button arrays (see above) |
| 0x1fff,0x3fff,0x7fff | w | slot end markers (written to 0) |
| 0xfffe,0xffff | r/w | `12 00` signature |

### DPI

**Verified on hardware.** The five DPI stages live at `config[0x03 .. 0x0c]`,
two bytes (X,Y) each, effective value **DPI = stored byte × 50**:

| stage | offset | this unit |
|---|---|---|
| 0 | 0x03,0x04 | 0x10,0x10 -> 800 dpi |
| 1 | 0x05,0x06 | 0x18,0x18 -> 1200 dpi |
| 2 | 0x07,0x08 | 0x30,0x30 -> 2400 dpi |
| 3 | 0x09,0x0a | 0x46,0x46 -> 3500 dpi |
| 4 | 0x0b,0x0c | 0xf5,0x12 |

(On this unit stage 4 reads as X=12250 / Y=900 dpi; the firmware stores exactly
these two bytes, so the tool reports them as-is.)

Two runtime commands, both confirmed on hardware:

```
switch stage : {01 07 00 STAGE}
apply values : {01 07 01 X Y}
```

* `{01 07 00 STAGE}` makes `STAGE` (0..4) the active DPI stage. The device
  **reloads that stage's stored X/Y pair**, so the pointer speed changes
  immediately (verified: selecting the unit's stage 4 with its asymmetric
  `X=0xF5, Y=0x12` gives a fast-horizontal / slow-vertical pointer).
* `{01 07 01 X Y}` overrides the **currently active** stage's X/Y at runtime
  (verified: sending `Y=0x10, X=0x04` slows the pointer down immediately).

The config write persists a stage; the two commands above are the runtime
side. To edit DPI, write the two config bytes and then send `{01 07 01 X Y}`
when the edited stage is the active one. `{01 04 …}` is still sent once at the
end of the session.

A separate command `{00 00 04 X Y 00 00 12}` belongs to a different code path;
it did **not** change the speed on this device.

`dux50.py dpi` lists the stages, `set-dpi` writes one (and applies it live),
`use-dpi` switches the active stage.

`0x02a0` holds a 4-entry 16-bit table (`60 04 7a 00 da 04 54 00 2e 05 5f 00`).

### Report rate

**Verified on hardware** by measuring the input report rate with
`tools/measure-rate.py` while moving the mouse.  `config[0x0e]` bits 4-6 hold
the rate; the stored value is `2**k - 1` where *k* is the option index:

| stored value | rate | measured |
|---|---|---|
| 0 | 1000 Hz | 1000 Hz |
| 1 | 500 Hz | (not measured) |
| 3 | 250 Hz | 251 Hz |
| 7 | 125 Hz | 106 Hz |

The field is updated as `config[0x0e] = (old & ~0x70) | (value << 4)`.

`dux50.py report-rate` prints the current value, `set-report-rate HZ` changes
it (verified: 1000 -> 251 Hz immediately after writing 3, and back to exactly
1000 Hz).  The device reloads the new rate without a re-plug.

### Other settings (not yet mapped)

The remaining scalar bytes `0x0d` and `0x0f` take part in bit-field setters:

* `config[0x0f]` bit 0
* `config[0x0f]` bits 1-3
* `config[0x0f]` bits 4-6 may hold the rate on some units; on this unit the
  rate lives in `0x0e`.

These are the "Other" page options (angle snapping, OSD, mouse acceleration,
lift-off, ...).  Use `backup` / `restore` to keep them unchanged.
