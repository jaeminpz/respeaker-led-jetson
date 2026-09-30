#!/usr/bin/env python3
"""LED가 실제로 켜졌던 그 시퀀스를 처음부터 그대로 재생한다.

그때의 순서:
  1) padctl: pin19/23/24 -> mux=SPI1, TRISTATE 해제, sfsel=SFIO
  2) spidev0.0 열어서 8MHz APA102 프레임 전송 (그 자체로는 안 켜졌던 단계)
  3) gpiod 라인 요청 후 TRISTATE만 해제 (sfsel은 SFIO 유지) — 무효했던 단계
  4) gpiod 라인 요청 후 TRISTATE 해제 + sfsel=GPIO — 켜졌던 단계

숨은 상태 의존성이 있을 수 있으니 중간 단계까지 전부 재현한다.
마지막에는 흰색을 켠 채로 유지한다 (Ctrl-C 또는 타임아웃까지).
"""

import mmap
import os
import struct
import sys
import time

import gpiod
import spidev

PADCTL_BASE = 0x02430000
PAGE = 4096
OFF_MOSI, OFF_SCK, OFF_CS0 = 0xD040, 0xD028, 0xD008
TRISTATE = 1 << 4
GPIO_SF_SEL = 1 << 10
MUX_MASK = 0x3

LINE_DATA, LINE_CLK = 135, 133
NUM_LED = 3
HOLD_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0


def pad(off, clear_tri=False, set_gpio=None, set_mux_spi1=False):
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        addr = PADCTL_BASE + off
        base = addr & ~(PAGE - 1)
        idx = addr - base
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
        new = val
        if clear_tri:
            new &= ~TRISTATE
        if set_mux_spi1:
            new &= ~MUX_MASK
        if set_gpio is True:
            new |= GPIO_SF_SEL
        elif set_gpio is False:
            new &= ~GPIO_SF_SEL
        if new != val:
            m[idx:idx + 4] = struct.pack("<I", new)
            new = struct.unpack("<I", m[idx:idx + 4])[0]
        m.close()
        return new
    finally:
        os.close(fd)


def desc(v):
    return (f"0x{v:08x} [{'TRISTATE' if v & TRISTATE else 'driving':<8}]"
            f"[{'GPIO' if v & GPIO_SF_SEL else 'SFIO'}]")


def apa_bytes(pixels):
    data = [0, 0, 0, 0]
    for r, g, b, br in pixels:
        data += [0xE0 | (br & 0x1F), b, g, r]
    return data + [0xFF] * 4


# ---------------- step 1 ----------------
print("[1] padctl -> mux=SPI1, TRISTATE 해제, sfsel=SFIO", flush=True)
for name, off in [("pin19 MOSI", OFF_MOSI), ("pin23 SCLK", OFF_SCK),
                  ("pin24 CE0 ", OFF_CS0)]:
    print(f"    {name}: {desc(pad(off, clear_tri=True, set_gpio=False, set_mux_spi1=True))}",
          flush=True)

# ---------------- step 2 ----------------
print("[2] spidev0.0 8MHz 전송 (그때도 안 켜졌던 단계)", flush=True)
sp = spidev.SpiDev()
sp.open(0, 0)
sp.mode = 0
sp.max_speed_hz = 8000000
sp.xfer2(apa_bytes([(255, 0, 0, 10)] * NUM_LED))
time.sleep(0.5)
sp.xfer2(apa_bytes([(0, 0, 0, 0)] * NUM_LED))
sp.close()

# ---------------- step 3 ----------------
print("[3] gpiod 요청 + TRISTATE만 해제 (sfsel=SFIO 유지) — 무효했던 단계", flush=True)
chip = gpiod.Chip("gpiochip0")
lines = chip.get_lines([LINE_CLK, LINE_DATA])
lines.request(consumer="replay-step3", type=gpiod.LINE_REQ_DIR_OUT,
              default_vals=[0, 0])
for name, off in [("pin23 SCLK", OFF_SCK), ("pin19 MOSI", OFF_MOSI)]:
    print(f"    {name}: {desc(pad(off, clear_tri=True))}", flush=True)


def bb_byte(b):
    sv = lines.set_values
    for i in range(7, -1, -1):
        bit = (b >> i) & 1
        sv([0, bit])
        sv([1, bit])


def bb_frame(pixels):
    for byte in apa_bytes(pixels):
        bb_byte(byte)
    lines.set_values([0, 0])


bb_frame([(0, 255, 0, 10)] * NUM_LED)
time.sleep(0.5)
lines.release()

# ---------------- step 4 ----------------
print("[4] gpiod 요청 + TRISTATE 해제 + sfsel=GPIO — 켜졌던 단계", flush=True)
lines = chip.get_lines([LINE_CLK, LINE_DATA])
lines.request(consumer="replay-step4", type=gpiod.LINE_REQ_DIR_OUT,
              default_vals=[0, 0])
for name, off in [("pin23 SCLK", OFF_SCK), ("pin19 MOSI", OFF_MOSI)]:
    print(f"    {name}: {desc(pad(off, clear_tri=True, set_gpio=True))}", flush=True)

print(f"\n>>> 흰색 최대밝기로 켜고 {HOLD_SECONDS:.0f}초 동안 유지합니다."
      f" 천천히 보드를 확인해주세요 <<<", flush=True)
try:
    t0 = time.time()
    while time.time() - t0 < HOLD_SECONDS:
        # 유지 중에도 주기적으로 프레임을 다시 보내 체인이 놓치지 않게 한다
        bb_frame([(255, 255, 255, 31)] * NUM_LED)
        time.sleep(1.0)
finally:
    bb_frame([(0, 0, 0, 0)] * NUM_LED)
    lines.release()
    chip.close()
    print("종료", flush=True)
