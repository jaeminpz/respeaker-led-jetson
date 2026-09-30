"""APA102 (DotStar) 3-LED driver for ReSpeaker 2-Mics Pi HAT on Jetson.

LED 데이터는 40핀 헤더의 SPI로 나간다:
  pin 19 = MOSI (Jetson PZ.05), pin 23 = SCLK (Jetson PZ.03)
Jetson에서 이 컨트롤러는 spi@3210000 -> /dev/spidev0.0 이다.
CS는 APA102가 쓰지 않지만, spidev로 열려면 chip select가 하나 필요해서 0.0을 쓴다.
"""

import spidev

START_FRAME = [0x00] * 4
LED_PREFIX = 0xE0  # 0b111xxxxx, 하위 5비트가 global brightness (0~31)


class APA102:
    def __init__(self, num_led=3, bus=0, device=0, speed_hz=8000000, order="bgr"):
        self.num_led = num_led
        self.order = order
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0b00
        # 픽셀당 4바이트: [0xE0|brightness, c0, c1, c2]
        self.leds = [LED_PREFIX, 0, 0, 0] * num_led

    def _order_bytes(self, r, g, b):
        m = {"r": r, "g": g, "b": b}
        return [m[c] for c in self.order]

    def set_pixel(self, index, r, g, b, brightness=31):
        if not 0 <= index < self.num_led:
            raise IndexError(index)
        base = index * 4
        self.leds[base] = LED_PREFIX | (brightness & 0x1F)
        self.leds[base + 1:base + 4] = self._order_bytes(r, g, b)

    def fill(self, r, g, b, brightness=31):
        for i in range(self.num_led):
            self.set_pixel(i, r, g, b, brightness)

    def clear(self):
        self.fill(0, 0, 0, 0)
        self.show()

    def show(self):
        # end frame: LED 하나당 1비트 이상의 클럭이 더 필요하다
        end_frame = [0xFF] * ((self.num_led + 15) // 16 + 1)
        self.spi.xfer2(START_FRAME + list(self.leds) + end_frame)

    def close(self):
        self.clear()
        self.spi.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
