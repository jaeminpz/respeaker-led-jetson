#!/usr/bin/env python3
"""tristate를 해제한 상태에서 GPIO 비트뱅잉.

앞선 비트뱅 시도는 패드가 TRISTATE라 출력이 물리적으로 꺼져 있어서 무효였다.
여기서는 gpiod로 라인을 잡은 "다음에" padctl의 TRISTATE 비트를 지운다
(GPIO 드라이버가 라인 요청 시 레지스터를 다시 쓰기 때문에 순서가 중요하다).

이게 실패하면 문제는 Tegra 바깥 — HAT의 5V 버퍼 또는 5V 전원 — 에 있다.
"""

import mmap
import os
import struct
import sys
import time

import gpiod

PADCTL_BASE = 0x02430000
PAGE = 4096
OFF_MOSI = 0xD040   # pin 19
OFF_SCK = 0xD028    # pin 23
TRISTATE = 1 << 4

LINE_DATA = 135  # PZ.05 / pin 19
LINE_CLK = 133   # PZ.03 / pin 23


def padctl(off, clear_tristate=False):
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        addr = PADCTL_BASE + off
        base = addr & ~(PAGE - 1)
        idx = addr - base
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                      offset=base)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
        if clear_tristate:
            m[idx:idx + 4] = struct.pack("<I", (val & ~TRISTATE) | (1 << 10))
            val = struct.unpack("<I", m[idx:idx + 4])[0]
        m.close()
        return val
    finally:
        os.close(fd)


class BB:
    def __init__(self, num_led=3):
        self.num_led = num_led
        self.chip = gpiod.Chip("gpiochip0")
        self.lines = self.chip.get_lines([LINE_CLK, LINE_DATA])
        self.lines.request(consumer="apa102", type=gpiod.LINE_REQ_DIR_OUT,
                           default_vals=[0, 0])
        # GPIO 요청 이후에 tristate 해제 — 순서 중요
        for name, off in [("pin23 SCLK", OFF_SCK), ("pin19 MOSI", OFF_MOSI)]:
            v = padctl(off, clear_tristate=True)
            print(f"  {name}: 0x{v:08x} "
                  f"[{'TRISTATE' if v & TRISTATE else 'driving'}]")
        self.leds = [0xE0, 0, 0, 0] * num_led

    def _byte(self, b):
        for i in range(8):
            bit = (b >> (7 - i)) & 1
            self.lines.set_values([0, bit])
            self.lines.set_values([1, bit])

    def show(self):
        for b in [0] * 4 + self.leds + [0xFF] * 4:
            self._byte(b)
        self.lines.set_values([0, 0])

    def fill(self, r, g, b, brightness=31):
        for i in range(self.num_led):
            self.leds[i * 4:i * 4 + 4] = [0xE0 | (brightness & 0x1F), b, g, r]

    def close(self):
        self.fill(0, 0, 0, 0)
        self.show()
        self.lines.release()
        self.chip.close()


def main():
    print("GPIO 라인 요청 후 tristate 해제:")
    bb = BB()
    try:
        print("\n전송 중 패드 상태 확인:")
        for name, off in [("pin23 SCLK", OFF_SCK), ("pin19 MOSI", OFF_MOSI)]:
            v = padctl(off)
            print(f"  {name}: 0x{v:08x} "
                  f"[{'TRISTATE' if v & TRISTATE else 'driving'}] "
                  f"[{'GPIO' if v & (1 << 10) else 'SFIO'}]")
        print()
        for name, rgb in [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)),
                          ("BLUE", (0, 0, 255)), ("WHITE", (255, 255, 255))]:
            print(f"  전체 {name} (3초)")
            bb.fill(*rgb, brightness=15)
            bb.show()
            time.sleep(3)
    finally:
        bb.close()
        print("  끄기")


if __name__ == "__main__":
    main()
