#!/usr/bin/env python3
"""APA102를 SPI 대신 GPIO 비트뱅잉으로 구동한다.

핀먹스가 SPI로 안 잡혀 있어도, gpiochip으로 라인을 요청하면 Tegra 패드가
GPIO 모드로 전환되므로 이 방법은 핀먹스 설정과 무관하게 동작한다.
APA102는 클럭 동기식이라 느린 비트뱅잉도 문제없다 (WS2812와 달리 타이밍 제약 없음).

  헤더 pin 19 = MOSI = PZ.05 = gpiochip0 line 135  -> APA102 DI
  헤더 pin 23 = SCLK = PZ.03 = gpiochip0 line 133  -> APA102 CI
"""

import sys
import time

import gpiod

CHIP = "gpiochip0"
LINE_DATA = 135  # PZ.05 / header pin 19
LINE_CLK = 133   # PZ.03 / header pin 23

NUM_LED = 3


class BitBangAPA102:
    def __init__(self, num_led=NUM_LED, order="bgr"):
        self.num_led = num_led
        self.order = order
        self.chip = gpiod.Chip(CHIP)
        self.lines = self.chip.get_lines([LINE_CLK, LINE_DATA])
        self.lines.request(consumer="apa102", type=gpiod.LINE_REQ_DIR_OUT,
                           default_vals=[0, 0])
        self.leds = [0xE0, 0, 0, 0] * num_led

    def _write_byte(self, byte):
        for i in range(8):
            bit = (byte >> (7 - i)) & 1
            self.lines.set_values([0, bit])   # clk low, data 세팅
            self.lines.set_values([1, bit])   # clk 상승 에지에 샘플링

    def _write_bytes(self, data):
        for b in data:
            self._write_byte(b)

    def _order_bytes(self, r, g, b):
        m = {"r": r, "g": g, "b": b}
        return [m[c] for c in self.order]

    def set_pixel(self, index, r, g, b, brightness=31):
        base = index * 4
        self.leds[base] = 0xE0 | (brightness & 0x1F)
        self.leds[base + 1:base + 4] = self._order_bytes(r, g, b)

    def fill(self, r, g, b, brightness=31):
        for i in range(self.num_led):
            self.set_pixel(i, r, g, b, brightness)

    def show(self):
        self._write_bytes([0x00] * 4)                 # start frame
        self._write_bytes(self.leds)
        self._write_bytes([0xFF] * 4)                 # end frame
        self.lines.set_values([0, 0])

    def clear(self):
        self.fill(0, 0, 0, 0)
        self.show()

    def close(self):
        self.clear()
        self.lines.release()
        self.chip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def main():
    order = sys.argv[1] if len(sys.argv) > 1 else "bgr"
    print(f"bit-bang APA102: clk=line{LINE_CLK}(pin23) data=line{LINE_DATA}(pin19) order={order}")
    with BitBangAPA102(order=order) as leds:
        for name, rgb in [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)),
                          ("BLUE", (0, 0, 255)), ("WHITE", (255, 255, 255))]:
            print(f"  전체 {name} (3초)")
            leds.fill(*rgb, brightness=10)
            leds.show()
            time.sleep(3)
        print("  끄기")


if __name__ == "__main__":
    main()
