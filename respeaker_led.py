"""ReSpeaker 2-Mics Pi HAT v2.0 의 APA102 LED 3개를 Jetson Orin Nano에서 구동.

왜 SPI가 아니라 GPIO 비트뱅잉인가
---------------------------------
HAT은 LED를 40핀 헤더의 pin 19(데이터)/pin 23(클럭)에 물린다. 라즈베리파이에서는
그게 SPI0이라 spidev로 쏘면 되지만, Jetson에서는 두 가지가 발목을 잡는다:

1. 부트로더(MB1 BCT)가 이 패드들을 TRISTATE=1 로 남겨둔다. 출력 드라이버 자체가
   꺼져 있어서 SPI든 GPIO든 신호가 핀까지 나가지 않는다.
2. padctl의 GPIO_SF_SEL 비트를 0(SFIO)으로 둔 상태에서만 실제로 LED가 반응한다.
   그런데 커널 pinctrl에 gpio-ranges가 등록되어 있어서, gpiod로 라인을 요청하는
   순간 커널이 이 비트를 1로 되돌린다.

=> 반드시 "gpiod 라인 요청  ->  그 다음에 padctl 수정" 순서여야 한다.
   순서가 뒤바뀌면 조용히 아무 일도 일어나지 않는다.

같은 이유로 spidev 경로는 이 보드에서 동작하지 않는 것으로 확인됐다
(100kHz~8MHz 전부 실패). 비트뱅잉은 APA102가 클럭 동기식이라 속도 제약이 없어
문제가 되지 않는다.

/dev/mem 을 써야 하므로 root 권한이 필요하다.

핀 맵
-----
  헤더 pin 19 = MOSI = PZ.05 = gpiochip0 line 135 -> APA102 DI
  헤더 pin 23 = SCLK = PZ.03 = gpiochip0 line 133 -> APA102 CI
  헤더 pin 11 = 버튼 = PR.04 = gpiochip0 line 112 (active-low)
"""

import mmap
import os
import struct

import gpiod

__all__ = ["APA102", "Pixels", "NUM_LED", "BUTTON_LINE"]

# --- padctl (Tegra234 PADCTL, drivers/pinctrl/tegra/pinctrl-tegra234.c) -------
PADCTL_BASE = 0x02430000
PAGE = 4096
OFF_MOSI = 0xD040          # spi1_mosi_pz5  (헤더 pin 19)
OFF_SCK = 0xD028           # spi1_sck_pz3   (헤더 pin 23)
TRISTATE = 1 << 4          # 1 = 출력 드라이버 꺼짐
GPIO_SF_SEL = 1 << 10      # LED가 반응하려면 0 이어야 한다
MUX_MASK = 0x3

# --- gpiochip0 라인 번호 ------------------------------------------------------
LINE_CLK = 133             # PZ.03 / pin 23
LINE_DATA = 135            # PZ.05 / pin 19
BUTTON_LINE = 112          # PR.04 / pin 11, active-low

NUM_LED = 3
LED_PREFIX = 0xE0          # 0b111xxxxx, 하위 5비트 = global brightness
MAX_BRIGHTNESS = 31


def _fix_pad(off):
    """TRISTATE 해제 + GPIO_SF_SEL 해제 + mux=SPI1. 반드시 라인 요청 이후에 호출."""
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        addr = PADCTL_BASE + off
        base = addr & ~(PAGE - 1)
        idx = addr - base
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
        m[idx:idx + 4] = struct.pack("<I", val & ~TRISTATE & ~GPIO_SF_SEL & ~MUX_MASK)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
        m.close()
        return val
    finally:
        os.close(fd)


class APA102:
    """ReSpeaker 2-Mics Pi HAT v2.0 의 APA102 3개.

    사용 예:
        with APA102() as leds:
            leds.set_pixel(0, 255, 0, 0)
            leds.show()
    """

    def __init__(self, num_led=NUM_LED, brightness=8, chip="gpiochip0"):
        if os.geteuid() != 0:
            raise PermissionError(
                "root 권한이 필요합니다 (/dev/mem 으로 padctl 을 수정해야 함). "
                "sudo 로 실행하세요.")
        self.num_led = num_led
        self.brightness = max(0, min(MAX_BRIGHTNESS, brightness))
        self._buf = [LED_PREFIX, 0, 0, 0] * num_led

        self.chip = gpiod.Chip(chip)
        self.lines = self.chip.get_lines([LINE_CLK, LINE_DATA])
        self.lines.request(consumer="respeaker-apa102",
                           type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0, 0])
        # 순서 중요: 라인 요청이 GPIO_SF_SEL 을 다시 1로 만들기 때문에 그 뒤에 고친다
        self.pad_state = (_fix_pad(OFF_SCK), _fix_pad(OFF_MOSI))
        self.clear()

    # --- 픽셀 조작 -----------------------------------------------------------
    def set_pixel(self, index, r, g, b, brightness=None):
        """index 번 LED 색 설정. brightness 생략 시 생성자 값 사용 (0~31)."""
        if not 0 <= index < self.num_led:
            raise IndexError(f"LED index {index} 는 0~{self.num_led - 1} 범위 밖")
        br = self.brightness if brightness is None else brightness
        base = index * 4
        # APA102 는 프레임당 [0xE0|밝기, B, G, R] 순서로 받는다
        self._buf[base] = LED_PREFIX | (max(0, min(MAX_BRIGHTNESS, br)) & 0x1F)
        self._buf[base + 1] = b & 0xFF
        self._buf[base + 2] = g & 0xFF
        self._buf[base + 3] = r & 0xFF

    def fill(self, r, g, b, brightness=None):
        for i in range(self.num_led):
            self.set_pixel(i, r, g, b, brightness)

    def clear(self):
        for i in range(self.num_led):
            self.set_pixel(i, 0, 0, 0, 0)
        self.show()

    # --- 전송 ---------------------------------------------------------------
    def show(self):
        """현재 버퍼를 LED 체인으로 밀어낸다."""
        data = [0x00] * 4 + self._buf + [0xFF] * 4   # start frame, 픽셀, end frame
        sv = self.lines.set_values
        for byte in data:
            for i in range(7, -1, -1):
                bit = (byte >> i) & 1
                sv([0, bit])      # 클럭 low 에서 데이터 세팅
                sv([1, bit])      # 상승 에지에 APA102 가 샘플링
        sv([0, 0])

    # --- 정리 ---------------------------------------------------------------
    def close(self):
        try:
            self.clear()
        finally:
            self.lines.release()
            self.chip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Pixels(APA102):
    """자주 쓰는 표시 패턴 몇 가지를 얹은 버전."""

    OFF = (0, 0, 0)
    RED = (255, 0, 0)
    GREEN = (0, 255, 0)
    BLUE = (0, 0, 255)
    YELLOW = (255, 150, 0)
    CYAN = (0, 255, 255)
    MAGENTA = (255, 0, 255)
    WHITE = (255, 255, 255)

    def solid(self, color, brightness=None):
        self.fill(*color, brightness=brightness)
        self.show()

    def one(self, index, color, brightness=None):
        """index 번만 켜고 나머지는 끈다."""
        for i in range(self.num_led):
            self.set_pixel(i, *(color if i == index else self.OFF),
                           brightness=brightness)
        self.show()

    def spin(self, color=BLUE, revolutions=3, delay=0.12, brightness=None):
        """한 칸씩 도는 표시 — 듣는 중/처리 중 표시에 쓰기 좋다."""
        import time
        for _ in range(revolutions):
            for i in range(self.num_led):
                self.one(i, color, brightness)
                time.sleep(delay)

    def blink(self, color=WHITE, times=3, on=0.25, off=0.25, brightness=None):
        import time
        for _ in range(times):
            self.solid(color, brightness)
            time.sleep(on)
            self.solid(self.OFF, 0)
            time.sleep(off)


def _main():
    import argparse
    import time

    colors = {n.lower(): v for n, v in vars(Pixels).items()
              if n.isupper() and isinstance(v, tuple)}

    ap = argparse.ArgumentParser(
        description="ReSpeaker 2-Mics Pi HAT v2.0 LED 제어 (root 필요)")
    ap.add_argument("command", choices=["solid", "off", "blink", "spin", "demo"],
                    help="실행할 동작")
    ap.add_argument("color", nargs="?", default="white",
                    help=f"색 이름 ({', '.join(sorted(colors))}) 또는 R,G,B")
    ap.add_argument("-b", "--brightness", type=int, default=8,
                    help="0~31 (기본 8)")
    ap.add_argument("-t", "--hold", type=float, default=0.0,
                    help="solid 후 유지할 초 (0 이면 켠 채로 바로 종료하지 않고 대기)")
    args = ap.parse_args()

    if "," in args.color:
        color = tuple(int(x) for x in args.color.split(",")[:3])
    else:
        color = colors.get(args.color.lower())
        if color is None:
            ap.error(f"모르는 색: {args.color}")

    px = Pixels(brightness=args.brightness)
    try:
        if args.command == "off":
            px.solid(Pixels.OFF, 0)
        elif args.command == "solid":
            px.solid(color)
            time.sleep(args.hold) if args.hold else input("Enter 를 누르면 끕니다...")
        elif args.command == "blink":
            px.blink(color)
        elif args.command == "spin":
            px.spin(color)
        elif args.command == "demo":
            for name in ("red", "green", "blue", "white"):
                print(f"  {name}")
                px.solid(colors[name])
                time.sleep(1.0)
            print("  spin")
            px.spin()
            print("  blink")
            px.blink()
    finally:
        px.close()


if __name__ == "__main__":
    _main()
