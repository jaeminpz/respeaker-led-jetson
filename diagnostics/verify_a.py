#!/usr/bin/env python3
"""동작 확인된 설정(A)만으로 색을 순환시켜 재현성을 확정한다.

설정 A = GPIO 비트뱅 + padctl sfsel=SFIO + TRISTATE=0
순서 중요: gpiod 라인을 먼저 요청하고(이때 커널이 sfsel을 GPIO로 바꿈),
그 다음에 padctl을 SFIO로 되돌리고 TRISTATE를 해제한다.
"""

import mmap
import os
import struct
import time

import gpiod

PADCTL_BASE = 0x02430000
PAGE = 4096
OFF_MOSI, OFF_SCK = 0xD040, 0xD028
TRISTATE = 1 << 4
GPIO_SF_SEL = 1 << 10
MUX_MASK = 0x3
LINE_DATA, LINE_CLK = 135, 133
NUM_LED = 3


def set_pad(off):
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        addr = PADCTL_BASE + off
        base = addr & ~(PAGE - 1)
        idx = addr - base
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
        new = val & ~TRISTATE & ~GPIO_SF_SEL & ~MUX_MASK
        m[idx:idx + 4] = struct.pack("<I", new)
        new = struct.unpack("<I", m[idx:idx + 4])[0]
        m.close()
        return new
    finally:
        os.close(fd)


chip = gpiod.Chip("gpiochip0")
lines = chip.get_lines([LINE_CLK, LINE_DATA])
lines.request(consumer="verify-a", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0, 0])
print(f"  mosi=0x{set_pad(OFF_MOSI):08x}  sck=0x{set_pad(OFF_SCK):08x}", flush=True)


def frame(px):
    data = [0, 0, 0, 0]
    for r, g, b, br in px:
        data += [0xE0 | (br & 0x1F), b, g, r]
    data += [0xFF] * 4
    sv = lines.set_values
    for byte in data:
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            sv([0, bit])
            sv([1, bit])
    sv([0, 0])


print("  3초 후 시작", flush=True)
frame([(0, 0, 0, 0)] * NUM_LED)
time.sleep(3)

for name, c in [("RED", (255, 0, 0, 20)), ("GREEN", (0, 255, 0, 20)),
                ("BLUE", (0, 0, 255, 20)), ("WHITE", (255, 255, 255, 20))]:
    print(f"  {name}", flush=True)
    frame([c] * NUM_LED)
    time.sleep(4)

print("  흰색 유지 (120초) — 천천히 확인하세요", flush=True)
try:
    t0 = time.time()
    while time.time() - t0 < 120:
        frame([(255, 255, 255, 20)] * NUM_LED)
        time.sleep(2)
finally:
    frame([(0, 0, 0, 0)] * NUM_LED)
    lines.release()
    chip.close()
    print("  끝", flush=True)
