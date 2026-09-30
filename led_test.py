#!/usr/bin/env python3
"""ReSpeaker 2-Mics Pi HAT v2.0 LED 검수 스크립트 — Jetson Orin Nano 용.

이 파일 하나만 대상 장비에 복사하면 된다. 외부 의존은 python3-gpiod 뿐이다.

    scp led_test.py <장비>:~/
    ssh <장비> 'chmod +x led_test.py && sudo ./led_test.py'

한 번 실행하면 환경 점검 → 색 순서대로 표시 → PASS/FAIL 을 찍고 끝난다.
종료 코드는 PASS 0 / FAIL 1 이라 다른 검수 스크립트에 물려 쓸 수 있다.

자동으로 잡히는 것과 못 잡는 것
-------------------------------
이 보드의 대표적 실패 모드는 "에러 없이 조용히 아무 일도 안 일어남" 이다.
그래서 padctl 을 고친 뒤 레지스터를 되읽어서 TRISTATE / GPIO_SF_SEL 이
실제로 내려갔는지 확인한다. 이게 어긋나면 LED 를 보지 않고도 FAIL 로 잡힌다.

다만 LED 소자 자체가 죽었거나 납땜이 떨어진 경우는 소프트웨어로 알 수 없다.
색이 눈에 보였는지는 사람이 봐야 한다 (`--confirm` 을 주면 마지막에 물어본다).
"""

import argparse
import mmap
import os
import struct
import sys
import time

# --- padctl (Tegra234 PADCTL, drivers/pinctrl/tegra/pinctrl-tegra234.c) -------
PADCTL_BASE = 0x02430000
PAGE = 4096
OFF_MOSI = 0xD040          # spi1_mosi_pz5  (헤더 pin 19, APA102 DI)
OFF_SCK = 0xD028           # spi1_sck_pz3   (헤더 pin 23, APA102 CI)
TRISTATE = 1 << 4          # 1 = 출력 드라이버 꺼짐
GPIO_SF_SEL = 1 << 10      # LED 가 반응하려면 0 이어야 한다
MUX_MASK = 0x3

# --- gpiochip0 라인 번호 ------------------------------------------------------
LINE_CLK = 133             # PZ.03 / pin 23
LINE_DATA = 135            # PZ.05 / pin 19

NUM_LED = 3
LED_PREFIX = 0xE0          # 0b111xxxxx, 하위 5비트 = global brightness
MAX_BRIGHTNESS = 31

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)
CYAN = (0, 255, 255)
OFF = (0, 0, 0)


# --- 출력 -------------------------------------------------------------------

def _tty():
    return sys.stdout.isatty()


C_OK = "\033[32m" if _tty() else ""
C_BAD = "\033[31m" if _tty() else ""
C_DIM = "\033[2m" if _tty() else ""
C_END = "\033[0m" if _tty() else ""

TOTAL_STEPS = 6


def step(n, label):
    print(f"[{n}/{TOTAL_STEPS}] {label} ".ljust(34, "."), end=" ", flush=True)


def ok(msg="OK"):
    print(f"{C_OK}{msg}{C_END}", flush=True)


def bad(msg):
    print(f"{C_BAD}{msg}{C_END}", flush=True)


def note(msg):
    print(f"      {C_DIM}{msg}{C_END}", flush=True)


class Fail(Exception):
    """검수 실패. message 가 그대로 FAIL 사유로 출력된다."""

    def __init__(self, reason, hint=None):
        super().__init__(reason)
        self.reason = reason
        self.hint = hint


# --- gpiod (v1 / v2 양쪽 지원) -------------------------------------------------

class _LinesV1:
    """libgpiod 1.x python 바인딩 (JetPack 6.x 기본, python3-gpiod 1.6.x)."""

    api = "gpiod 1.x"

    def __init__(self, gpiod, chip_name, consumer):
        self.chip = gpiod.Chip(chip_name)
        self.lines = self.chip.get_lines([LINE_CLK, LINE_DATA])
        self.lines.request(consumer=consumer,
                           type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0, 0])
        self.set = self.lines.set_values

    def release(self):
        self.lines.release()
        self.chip.close()


class _LinesV2:
    """libgpiod 2.x python 바인딩. JetPack 이미지에 따라 이쪽인 경우가 있다."""

    api = "gpiod 2.x"

    def __init__(self, gpiod, chip_name, consumer):
        from gpiod.line import Direction, Value
        self._on, self._off = Value.ACTIVE, Value.INACTIVE
        settings = gpiod.LineSettings(direction=Direction.OUTPUT,
                                      output_value=Value.INACTIVE)
        self.req = gpiod.request_lines(
            f"/dev/{chip_name}", consumer=consumer,
            config={LINE_CLK: settings, LINE_DATA: settings})

    def set(self, vals):
        clk, dat = vals
        self.req.set_values({LINE_CLK: self._on if clk else self._off,
                             LINE_DATA: self._on if dat else self._off})

    def release(self):
        self.req.release()


def _open_lines(chip_name, consumer):
    try:
        import gpiod
    except ImportError:
        raise Fail("python3-gpiod 가 없다",
                   "sudo apt install python3-libgpiod  (또는 python3-gpiod)")

    cls = _LinesV1 if hasattr(gpiod, "LINE_REQ_DIR_OUT") else _LinesV2
    try:
        return cls(gpiod, chip_name, consumer)
    except OSError as e:
        # EBUSY = 다른 프로세스가 이미 이 라인을 잡고 있다
        raise Fail(f"gpio 라인 {LINE_CLK}/{LINE_DATA} 요청 실패: {e}",
                   "다른 LED 프로세스가 떠 있는지 확인: "
                   "sudo gpioinfo gpiochip0 | grep -E 'line +(133|135)'")


# --- padctl -------------------------------------------------------------------

def _fix_pad(off):
    """TRISTATE 해제 + GPIO_SF_SEL 해제 + mux=SPI1. 반드시 라인 요청 이후에 호출.

    되읽은 레지스터 값을 돌려준다.
    """
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    except OSError as e:
        raise Fail(f"/dev/mem 열기 실패: {e}",
                   "커널이 CONFIG_STRICT_DEVMEM 으로 빌드됐는지 확인")
    try:
        addr = PADCTL_BASE + off
        base = addr & ~(PAGE - 1)
        idx = addr - base
        m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED,
                      mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        try:
            val = struct.unpack("<I", m[idx:idx + 4])[0]
            m[idx:idx + 4] = struct.pack(
                "<I", val & ~TRISTATE & ~GPIO_SF_SEL & ~MUX_MASK)
            return struct.unpack("<I", m[idx:idx + 4])[0]
        finally:
            m.close()
    finally:
        os.close(fd)


def _check_pad(name, val):
    """되읽은 padctl 값이 LED 가 동작하는 상태인지 검사. 문제 목록을 돌려준다."""
    problems = []
    if val & TRISTATE:
        problems.append(f"{name}: TRISTATE 가 아직 1 (출력 드라이버 꺼짐)")
    if val & GPIO_SF_SEL:
        problems.append(f"{name}: GPIO_SF_SEL 이 아직 1 (커널 pinctrl 이 되돌림)")
    if val & MUX_MASK:
        problems.append(f"{name}: mux 가 {val & MUX_MASK} (0 이어야 함)")
    return problems


# --- APA102 -------------------------------------------------------------------

class Leds:
    def __init__(self, brightness=8, chip="gpiochip0"):
        self.brightness = max(0, min(MAX_BRIGHTNESS, brightness))
        self._buf = [LED_PREFIX, 0, 0, 0] * NUM_LED
        self._lines = _open_lines(chip, "led-test")
        self.api = self._lines.api
        # 순서 중요: 라인 요청이 GPIO_SF_SEL 을 다시 1로 만들기 때문에 그 뒤에 고친다
        self.pad_sck = _fix_pad(OFF_SCK)
        self.pad_mosi = _fix_pad(OFF_MOSI)
        self.solid(OFF, 0)

    def set_pixel(self, index, r, g, b, brightness=None):
        br = self.brightness if brightness is None else brightness
        base = index * 4
        # APA102 는 프레임당 [0xE0|밝기, B, G, R] 순서로 받는다
        self._buf[base] = LED_PREFIX | (max(0, min(MAX_BRIGHTNESS, br)) & 0x1F)
        self._buf[base + 1] = b & 0xFF
        self._buf[base + 2] = g & 0xFF
        self._buf[base + 3] = r & 0xFF

    def show(self):
        data = [0x00] * 4 + self._buf + [0xFF] * 4   # start frame, 픽셀, end frame
        sv = self._lines.set
        for byte in data:
            for i in range(7, -1, -1):
                bit = (byte >> i) & 1
                sv([0, bit])      # 클럭 low 에서 데이터 세팅
                sv([1, bit])      # 상승 에지에 APA102 가 샘플링
        sv([0, 0])

    def solid(self, color, brightness=None):
        for i in range(NUM_LED):
            self.set_pixel(i, *color, brightness=brightness)
        self.show()

    def one(self, index, color, brightness=None):
        for i in range(NUM_LED):
            self.set_pixel(i, *(color if i == index else OFF),
                           brightness=brightness)
        self.show()

    def close(self):
        try:
            self.solid(OFF, 0)
        finally:
            self._lines.release()


# --- 검수 본체 -----------------------------------------------------------------

def run(args):
    warnings = []

    # [1/6] 환경 점검 — root / gpiod / chip / /dev/mem / padctl 되읽기
    step(1, "환경 점검")
    if os.geteuid() != 0:
        raise Fail("root 권한이 아니다 (/dev/mem 으로 padctl 을 고쳐야 한다)",
                   f"sudo {os.path.basename(sys.argv[0])} 로 실행")
    if not os.path.exists(f"/dev/{args.chip}"):
        raise Fail(f"/dev/{args.chip} 가 없다",
                   "ls /dev/gpiochip* 로 확인")

    leds = Leds(brightness=args.brightness, chip=args.chip)
    try:
        problems = (_check_pad("pin23/SCK (spi1_sck_pz3)", leds.pad_sck)
                    + _check_pad("pin19/DATA (spi1_mosi_pz5)", leds.pad_mosi))
        if problems:
            raise Fail("padctl 이 LED 동작 상태로 안 잡혔다",
                       " / ".join(problems))
        ok()
        note(f"{leds.api}, padctl sck=0x{leds.pad_sck:08X} "
             f"mosi=0x{leds.pad_mosi:08X}, 밝기 {leds.brightness}/31")

        # [2..4] 단색 — 3개가 모두 같은 색으로 켜져야 한다
        for n, (label, color) in enumerate(
                (("RED", RED), ("GREEN", GREEN), ("BLUE", BLUE)), start=2):
            step(n, f"{label:<5} ({args.hold:g}s)")
            leds.solid(color)
            time.sleep(args.hold)
            ok()

        # [5] spin — 한 칸씩 도는 표시. 개별 LED 가 따로 제어되는지 본다
        step(5, "spin  (LED 개별 확인)")
        for _ in range(args.revolutions):
            for i in range(NUM_LED):
                leds.one(i, CYAN)
                time.sleep(args.delay)
        ok()

        # [6] blink — 전체 on/off 전환
        step(6, "blink (전체 점멸)")
        for _ in range(3):
            leds.solid(WHITE)
            time.sleep(args.delay * 2)
            leds.solid(OFF, 0)
            time.sleep(args.delay * 2)
        ok()

        # 마지막 프레임 속도 — 크게 느려졌으면 뭔가 이상한 것
        t0 = time.perf_counter()
        for _ in range(20):
            leds.solid(OFF, 0)
        ms = (time.perf_counter() - t0) / 20 * 1000
        if ms > 20:
            warnings.append(f"프레임 전송이 느리다 ({ms:.1f} ms/frame, 보통 ~2 ms)")

        if args.confirm:
            print()
            try:
                answer = input("  빨강/초록/파랑/회전/점멸이 LED 3개에서 "
                               "모두 보였나? [y/N] ").strip().lower()
            except EOFError:
                raise Fail("육안 확인 입력을 받지 못했다 (stdin 이 닫혀 있다)",
                           "--confirm 은 대화형 터미널에서만 쓸 수 있다")
            if answer not in ("y", "yes"):
                raise Fail("육안 확인에서 실패로 응답함",
                           "색이 아예 안 나오면 5V 전원과 HAT 장착 상태부터 확인")
    finally:
        leds.close()

    return warnings


def main():
    ap = argparse.ArgumentParser(
        description="ReSpeaker 2-Mics Pi HAT v2.0 LED 검수 (root 필요)")
    ap.add_argument("-b", "--brightness", type=int, default=12,
                    help="0~31 (기본 12)")
    ap.add_argument("-t", "--hold", type=float, default=3.0,
                    help="단색 하나를 유지할 초 (기본 3)")
    ap.add_argument("-d", "--delay", type=float, default=0.2,
                    help="spin/blink 한 칸 간격 초 (기본 0.2)")
    ap.add_argument("-r", "--revolutions", type=int, default=3,
                    help="spin 바퀴 수 (기본 3)")
    ap.add_argument("--fast", action="store_true",
                    help="짧게: hold 0.6s, delay 0.08s, spin 2바퀴")
    ap.add_argument("--confirm", action="store_true",
                    help="마지막에 육안 확인 y/n 을 물어본다")
    ap.add_argument("--chip", default="gpiochip0", help="기본 gpiochip0")
    args = ap.parse_args()

    if args.fast:
        args.hold, args.delay, args.revolutions = 0.6, 0.08, 2

    print("ReSpeaker 2-Mics Pi HAT v2.0 — LED 검수")
    print(f"{C_DIM}pin19=DATA(line {LINE_DATA})  pin23=CLK(line {LINE_CLK})  "
          f"LED {NUM_LED}개{C_END}")
    if args.confirm and not sys.stdin.isatty():
        # 파이프로 답을 넣어주는 건 정상 용법이라 막지는 않고, 미리 알려만 준다
        note("--confirm 인데 stdin 이 터미널이 아니다. "
             "답(y/n)을 파이프로 넣지 않으면 마지막에 FAIL 로 끝난다")
    print()

    try:
        warnings = run(args)
    except Fail as e:
        print()
        bad(f"결과: FAIL — {e.reason}")
        if e.hint:
            note(f"→ {e.hint}")
        return 1
    except KeyboardInterrupt:
        print()
        bad("결과: 중단됨")
        return 130
    except Exception as e:                      # 예상 못 한 것도 FAIL 로 접수
        print()
        bad(f"결과: FAIL — 예상치 못한 오류: {type(e).__name__}: {e}")
        return 1

    print()
    for w in warnings:
        print(f"{C_DIM}경고: {w}{C_END}")
    ok("결과: PASS")
    if not args.confirm:
        note("※ padctl 까지는 확인됐다. 색이 실제로 보였는지는 눈으로 확인할 것 "
             "(--confirm 을 주면 물어본다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
