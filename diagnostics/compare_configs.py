#!/usr/bin/env python3
"""네 가지 구동 설정을 각각 다른 색으로 시도해 어떤 것이 실제로 동작하는지 가린다.

각 설정마다: 같은 설정으로 먼저 끄기 시도 -> 2초 -> 같은 설정으로 색 넣기 -> 8초.
설정이 동작하면 (꺼짐 -> 그 색)이 보이고, 동작하지 않으면 앞 색이 그대로 남는다.
따라서 "사용자가 본 색들의 집합" = "동작한 설정들의 집합" 이 된다.

  A  bit-bang, sfsel=SFIO   -> 빨강
  B  bit-bang, sfsel=GPIO   -> 파랑
  C  SPI 100kHz             -> 노랑
  D  SPI 8MHz               -> 보라
"""

import mmap
import os
import struct
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
DWELL = 8.0
GAP = 2.0


def set_pad(off, gpio_mode):
    """TRISTATE 해제 + mux=SPI1 + sfsel 설정. 설정 후 값을 돌려준다."""
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        addr = PADCTL_BASE + off
        base = addr & ~(PAGE - 1)
        idx = addr - base
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
        new = (val & ~TRISTATE & ~MUX_MASK)
        new = (new | GPIO_SF_SEL) if gpio_mode else (new & ~GPIO_SF_SEL)
        m[idx:idx + 4] = struct.pack("<I", new)
        new = struct.unpack("<I", m[idx:idx + 4])[0]
        m.close()
        return new
    finally:
        os.close(fd)


def configure(gpio_mode):
    a = set_pad(OFF_MOSI, gpio_mode)
    b = set_pad(OFF_SCK, gpio_mode)
    set_pad(OFF_CS0, gpio_mode)
    mode = "GPIO" if gpio_mode else "SFIO"
    print(f"    pads: mosi=0x{a:08x} sck=0x{b:08x} sfsel={mode}", flush=True)


def apa(pixels):
    data = [0, 0, 0, 0]
    for r, g, b, br in pixels:
        data += [0xE0 | (br & 0x1F), b, g, r]
    return data + [0xFF] * 4


def run_bitbang(gpio_mode, color):
    chip = gpiod.Chip("gpiochip0")
    lines = chip.get_lines([LINE_CLK, LINE_DATA])
    lines.request(consumer="cmp", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0, 0])
    configure(gpio_mode)          # gpiod 요청 이후에 padctl 적용 (순서 중요)

    def frame(px):
        sv = lines.set_values
        for byte in apa(px):
            for i in range(7, -1, -1):
                bit = (byte >> i) & 1
                sv([0, bit])
                sv([1, bit])
        sv([0, 0])

    frame([(0, 0, 0, 0)] * NUM_LED)
    time.sleep(GAP)
    for _ in range(3):
        frame([color] * NUM_LED)
        time.sleep(0.05)
    time.sleep(DWELL)
    lines.release()
    chip.close()


def run_spi(hz, color):
    configure(False)              # SPI는 sfsel=SFIO 여야 한다
    sp = spidev.SpiDev()
    sp.open(0, 0)
    sp.mode = 0
    sp.max_speed_hz = hz
    sp.xfer2(apa([(0, 0, 0, 0)] * NUM_LED))
    time.sleep(GAP)
    for _ in range(3):
        sp.xfer2(apa([color] * NUM_LED))
        time.sleep(0.05)
    time.sleep(DWELL)
    sp.close()


RED = (255, 0, 0, 20)
BLUE = (0, 0, 255, 20)
YELLOW = (255, 150, 0, 20)
MAGENTA = (255, 0, 255, 20)

print("3초 후 시작\n", flush=True)
time.sleep(3)

print("[A] bit-bang, sfsel=SFIO  -> 빨강", flush=True)
run_bitbang(False, RED)

print("[B] bit-bang, sfsel=GPIO  -> 파랑", flush=True)
run_bitbang(True, BLUE)

print("[C] SPI 100kHz            -> 노랑", flush=True)
run_spi(100000, YELLOW)

print("[D] SPI 8MHz              -> 보라", flush=True)
run_spi(8000000, MAGENTA)

print("\n끝 — 마지막 색은 그대로 유지됩니다", flush=True)
