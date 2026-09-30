#!/usr/bin/env python3
"""60초 동안 최대 밝기 흰색으로 1Hz 깜빡임 — 타이밍 맞출 필요 없이 확인용.

pin 19/23 패드를 GPIO + 출력구동으로 강제한 뒤 APA102를 비트뱅잉한다.
매 프레임마다 blank 프레임을 먼저 보내 체인 상태를 초기화한다.
"""

import mmap
import os
import struct
import sys
import time

import gpiod

PADCTL_BASE = 0x02430000
PAGE = 4096
OFF_MOSI, OFF_SCK = 0xD040, 0xD028
TRISTATE = 1 << 4
GPIO_SF_SEL = 1 << 10

LINE_DATA, LINE_CLK = 135, 133   # PZ.05 = pin 19, PZ.03 = pin 23
NUM_LED = 3
DURATION = 60.0


def pad_to_gpio_output(off):
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        addr = PADCTL_BASE + off
        base = addr & ~(PAGE - 1)
        idx = addr - base
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
        m[idx:idx + 4] = struct.pack("<I", (val & ~TRISTATE) | GPIO_SF_SEL)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
        m.close()
        return val
    finally:
        os.close(fd)


class BitBang:
    def __init__(self):
        self.chip = gpiod.Chip("gpiochip0")
        self.lines = self.chip.get_lines([LINE_CLK, LINE_DATA])
        self.lines.request(consumer="apa102-blink",
                           type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0, 0])
        for name, off in [("pin23 SCLK", OFF_SCK), ("pin19 MOSI", OFF_MOSI)]:
            v = pad_to_gpio_output(off)
            print(f"  {name}: 0x{v:08x} "
                  f"[{'TRISTATE' if v & TRISTATE else 'driving'}] "
                  f"[{'GPIO' if v & GPIO_SF_SEL else 'SFIO'}]", flush=True)

    def _byte(self, b):
        sv = self.lines.set_values
        for i in range(7, -1, -1):
            bit = (b >> i) & 1
            sv([0, bit])
            sv([1, bit])

    def frame(self, pixels):
        """pixels: [(r,g,b,brightness), ...]"""
        data = [0, 0, 0, 0]
        for r, g, b, br in pixels:
            data += [0xE0 | (br & 0x1F), b, g, r]
        data += [0xFF] * 4
        for byte in data:
            self._byte(byte)
        self.lines.set_values([0, 0])

    def off(self):
        self.frame([(0, 0, 0, 0)] * NUM_LED)

    def close(self):
        self.off()
        self.lines.release()
        self.chip.close()


def main():
    bb = BitBang()
    print(f"\n  {DURATION:.0f}초 동안 흰색 1Hz 깜빡임 시작 — 편하게 보드를 봐주세요",
          flush=True)
    t0 = time.time()
    n = 0
    try:
        while time.time() - t0 < DURATION:
            bb.frame([(255, 255, 255, 31)] * NUM_LED)
            time.sleep(0.5)
            bb.off()
            time.sleep(0.5)
            n += 1
            if n % 10 == 0:
                print(f"  ...{n}회 깜빡임 ({time.time()-t0:.0f}초 경과)", flush=True)
    finally:
        bb.close()
        print(f"  종료 (총 {n}회)", flush=True)


if __name__ == "__main__":
    main()
