#!/usr/bin/env python3
"""ReSpeaker 2-Mics Pi HAT v2.0 LED 검수 스크립트 — Jetson Orin Nano 용.

이 파일 하나만 대상 장비에 복사하면 된다. 외부 의존은 python3-gpiod 뿐이다.

    scp led_test.py <장비>:~/
    ssh <장비> 'chmod +x led_test.py && sudo ./led_test.py'

한 번 실행하면 환경 점검 → 실제 상태 표시를 순서대로 보여주고 PASS/FAIL 을 찍는다.
종료 코드는 PASS 0 / FAIL 1 이라 다른 검수 스크립트에 물려 쓸 수 있다.

색은 led_controller.py 가 실제로 쓰는 팔레트 그대로다. 임의의 빨강/초록/파랑이
아니라 현장에서 보게 될 화면으로 검수하기 위해서다. 다만 죽은 색 채널을 확실히
잡으려고 맨 앞에 R/G/B 단독 점검을 짧게 넣었다 (팔레트만으로는 한 채널이 죽어도
다른 색에 묻혀 안 보일 수 있다).

자동으로 잡히는 것과 못 잡는 것
-------------------------------
이 보드의 대표적 실패 모드는 "에러 없이 조용히 아무 일도 안 일어남" 이다.
그래서 padctl 을 고친 뒤 레지스터를 되읽어서 TRISTATE / GPIO_SF_SEL 이
실제로 내려갔는지 확인한다. 이게 어긋나면 LED 를 보지 않고도 FAIL 로 잡힌다.

다만 LED 소자 자체가 죽었거나 납땜이 떨어진 경우는 소프트웨어로 알 수 없다.
색이 눈에 보였는지는 사람이 봐야 한다 (`--confirm` 을 주면 마지막에 물어본다).

팔레트 동기화
-------------
이 파일은 한 파일로 배포되어야 하므로 led_controller.py 의 팔레트와 애니메이션을
복사해서 갖고 있다. 저장소 전체가 있는 환경에서는 실행할 때 두 구현의 출력을
비교해서 어긋났으면 경고한다 (한 파일만 있는 장비에서는 조용히 건너뛴다).
"""

import argparse
import math
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

# --- 팔레트: led_controller.py 와 같아야 한다 ---------------------------------
C_CYAN = (0.00, 0.70, 1.00)     # 듣는 중
C_PURPLE = (0.60, 0.00, 1.00)   # 생각 중
C_WARM = (1.00, 0.66, 0.31)     # 말하는 중
C_RED = (1.00, 0.00, 0.00)      # 오류 / 마이크 꺼짐
C_GREEN = (0.00, 1.00, 0.24)    # 확인
C_WHITE = (1.00, 1.00, 1.00)    # 기동

THINKING_SPEED = 3.2
THINKING_TAIL = 1.35
SPEAKING_HZ = 1.6
SPEAKING_SPREAD = 0.30
MUTE_LEVEL = 0.12

OFF3 = [(0.0, 0.0, 0.0)] * NUM_LED


# --- 출력 -------------------------------------------------------------------

def _tty():
    return sys.stdout.isatty()


C_OK = "\033[32m" if _tty() else ""
C_BAD = "\033[31m" if _tty() else ""
C_DIM = "\033[2m" if _tty() else ""
C_END = "\033[0m" if _tty() else ""

TOTAL_STEPS = 8


def step(n, label):
    print(f"[{n}/{TOTAL_STEPS}] {label} ".ljust(38, "."), end=" ", flush=True)


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
                   "다른 LED 프로세스(led_controller 등)가 떠 있는지 확인: "
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


# --- 색 계산 (led_controller.py 와 동일해야 한다) -----------------------------

def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _dim(color, k):
    k = _clamp01(k)
    return (color[0] * k, color[1] * k, color[2] * k)


def _bump(e, phase):
    return (0.5 * (1.0 + math.cos(2.0 * math.pi * (e - phase)))) ** 3


def a_listening(t, level):
    return [_dim(C_CYAN, 0.75)] * NUM_LED


def a_capturing(t, level):
    center = 0.30 + 0.70 * level
    outer = 0.05 + 0.95 * _clamp01((level - 0.12) / 0.88)
    return [_dim(C_CYAN, outer), _dim(C_CYAN, center), _dim(C_CYAN, outer)]


def a_thinking(t, level):
    head = (t * THINKING_SPEED) % NUM_LED
    out = []
    for i in range(NUM_LED):
        d = abs(i - head)
        d = min(d, NUM_LED - d)
        # max() 를 먼저. 음수의 실수 거듭제곱은 예외가 아니라 복소수를 낸다.
        out.append(_dim(C_PURPLE, max(0.0, 1.0 - d / THINKING_TAIL) ** 1.5))
    return out


def a_speaking(t, level):
    e = (t * SPEAKING_HZ) % 1.0
    amp = 0.35 + 0.65 * level
    center = (0.25 + 0.75 * _bump(e, 0.0)) * amp
    outer = (0.25 + 0.75 * _bump(e, SPEAKING_SPREAD)) * amp
    return [_dim(C_WARM, outer), _dim(C_WARM, center), _dim(C_WARM, outer)]


def a_mute(t, level):
    return [(0.0, 0.0, 0.0), _dim(C_RED, MUTE_LEVEL), (0.0, 0.0, 0.0)]


def a_boot(t, level):
    head = t * 4.0
    return [_dim(C_WHITE, 1.0 - abs(i - head) * 1.6) for i in range(NUM_LED)]


def a_ack(t, level):
    return [_dim(C_GREEN, math.sin(math.pi * _clamp01(t / 0.35)))] * NUM_LED


def a_error(t, level):
    on = t < 0.60 and int(t / 0.15) % 2 == 0
    return [C_RED] * NUM_LED if on else OFF3


#: 이름 -> (그리기 함수, led_controller 의 State 이름)
SHARED_ANIMS = {
    "listening": (a_listening, "LISTENING"),
    "capturing": (a_capturing, "CAPTURING"),
    "thinking": (a_thinking, "THINKING"),
    "speaking": (a_speaking, "SPEAKING"),
    "mute": (a_mute, "MUTE"),
    "boot": (a_boot, "BOOT"),
    "ack": (a_ack, "ACK"),
    "error": (a_error, "ERROR"),
}


def _palette_drift():
    """led_controller 가 옆에 있으면 두 구현이 같은 그림을 그리는지 비교한다.

    한 파일만 배포된 장비에서는 import 가 실패하므로 조용히 건너뛴다.
    """
    try:
        import led_controller as LC
    except Exception:
        return []

    drift = []
    for name, (fn, state_name) in SHARED_ANIMS.items():
        ref = LC.ANIMATIONS.get(getattr(LC.State, state_name, None))
        if ref is None:
            drift.append(f"{name}: led_controller 에 {state_name} 이 없다")
            continue
        for i in range(400):
            t = i * 0.005
            for lv in (0.0, 0.5, 1.0):
                mine, theirs = fn(t, lv), ref(t, lv)
                if any(abs(a - b) > 1e-9
                       for pa, pb in zip(mine, theirs)
                       for a, b in zip(pa, pb)):
                    drift.append(
                        f"{name}: led_controller 와 다른 그림 (t={t:.3f}, level={lv})")
                    break
            else:
                continue
            break
    return drift


# --- APA102 -------------------------------------------------------------------

class Leds:
    """전역 밝기는 31 로 고정하고 dimming 은 RGB 로 한다 (깜빡임 방지)."""

    def __init__(self, brightness=0.45, chip="gpiochip0"):
        self.master = _clamp01(brightness)
        self._buf = [LED_PREFIX | MAX_BRIGHTNESS, 0, 0, 0] * NUM_LED
        self._lines = _open_lines(chip, "led-test")
        self.api = self._lines.api
        # 순서 중요: 라인 요청이 GPIO_SF_SEL 을 다시 1로 만들기 때문에 그 뒤에 고친다
        self.pad_sck = _fix_pad(OFF_SCK)
        self.pad_mosi = _fix_pad(OFF_MOSI)
        self.draw(OFF3)

    def draw(self, colors):
        """0~1 실수 RGB 3개를 그린다."""
        k = self.master * 255.0
        for i, (r, g, b) in enumerate(colors):
            base = i * 4
            # APA102 는 프레임당 [0xE0|밝기, B, G, R] 순서로 받는다
            self._buf[base + 1] = int(_clamp01(b) * k + 0.5)
            self._buf[base + 2] = int(_clamp01(g) * k + 0.5)
            self._buf[base + 3] = int(_clamp01(r) * k + 0.5)
        self.show()

    def show(self):
        data = [0x00] * 4 + self._buf + [0xFF] * 4   # start frame, 픽셀, end frame
        sv = self._lines.set
        for byte in data:
            for i in range(7, -1, -1):
                bit = (byte >> i) & 1
                sv([0, bit])      # 클럭 low 에서 데이터 세팅
                sv([1, bit])      # 상승 에지에 APA102 가 샘플링
        sv([0, 0])

    def animate(self, fn, duration, level_fn=None, fps=50):
        """fn(t, level) 을 duration 초 동안 돌린다."""
        t0 = time.perf_counter()
        period = 1.0 / fps
        while True:
            t = time.perf_counter() - t0
            if t >= duration:
                break
            self.draw(fn(t, level_fn(t) if level_fn else 0.0))
            time.sleep(max(0.0, period - (time.perf_counter() - t0 - t)))

    def close(self):
        try:
            self.draw(OFF3)
        finally:
            self._lines.release()


def _fake_level(t, speaking=False):
    """말소리 비슷한 크기 곡선. CAPTURING/SPEAKING 을 살아 있게 보이려고 쓴다."""
    if speaking:
        v = 0.55 + 0.45 * math.sin(2 * math.pi * 1.1 * t)
        v *= 0.7 + 0.3 * math.sin(2 * math.pi * 0.37 * t + 1.0)
        return _clamp01(v)
    v = 0.5 + 0.5 * math.sin(2 * math.pi * 0.8 * t)
    v *= 0.5 + 0.5 * math.sin(2 * math.pi * 0.23 * t)
    return _clamp01(v ** 0.7)


# --- 검수 본체 -----------------------------------------------------------------

def run(args):
    warnings = []

    # [1] 환경 점검 — root / gpiod / chip / /dev/mem / padctl 되읽기
    step(1, "환경 점검")
    if os.geteuid() != 0:
        raise Fail("root 권한이 아니다 (/dev/mem 으로 padctl 을 고쳐야 한다)",
                   f"sudo {os.path.basename(sys.argv[0])} 로 실행")
    if not os.path.exists(f"/dev/{args.chip}"):
        raise Fail(f"/dev/{args.chip} 가 없다", "ls /dev/gpiochip* 로 확인")

    drift = _palette_drift()

    leds = Leds(brightness=args.brightness, chip=args.chip)
    try:
        problems = (_check_pad("pin23/SCK (spi1_sck_pz3)", leds.pad_sck)
                    + _check_pad("pin19/DATA (spi1_mosi_pz5)", leds.pad_mosi))
        if problems:
            raise Fail("padctl 이 LED 동작 상태로 안 잡혔다", " / ".join(problems))
        ok()
        note(f"{leds.api}, padctl sck=0x{leds.pad_sck:08X} "
             f"mosi=0x{leds.pad_mosi:08X}, 밝기 {leds.master:.2f}")
        for d in drift:
            warnings.append(f"팔레트 어긋남 — {d}")

        hold, short = args.hold, args.hold * 0.4

        # [2] 채널 점검 — 팔레트만으로는 죽은 채널이 다른 색에 묻힐 수 있다
        step(2, "채널 점검 (R/G/B 단독)")
        for color in ((1.0, 0, 0), (0, 1.0, 0), (0, 0, 1.0)):
            leds.draw([color] * NUM_LED)
            time.sleep(short)
        leds.draw(OFF3)
        ok()

        # [3..8] 실제로 쓰는 화면 그대로
        step(3, "LISTENING  청록 고정")
        leds.animate(a_listening, hold)
        ok()

        step(4, "CAPTURING  청록 + 음성 크기")
        leds.animate(a_capturing, hold, level_fn=_fake_level)
        ok()

        step(5, "THINKING   보라 흐름")
        leds.animate(a_thinking, hold)
        ok()

        step(6, "SPEAKING   따뜻한 흰색 파동")
        leds.animate(a_speaking, hold,
                     level_fn=lambda t: _fake_level(t, speaking=True))
        ok()

        step(7, "MUTE       중앙 빨강 은은하게")
        leds.animate(a_mute, hold)
        ok()

        step(8, "일회성     BOOT / ACK / ERROR")
        for fn, dur in ((a_boot, 0.90), (a_ack, 0.35), (a_error, 0.90)):
            leds.animate(fn, dur)
            leds.draw(OFF3)
            time.sleep(0.45)
        ok()

        # 마지막 프레임 속도 — 크게 느려졌으면 뭔가 이상한 것
        t0 = time.perf_counter()
        for _ in range(20):
            leds.draw(OFF3)
        ms = (time.perf_counter() - t0) / 20 * 1000
        if ms > 20:
            warnings.append(f"프레임 전송이 느리다 ({ms:.1f} ms/frame, 보통 ~2 ms)")

        if args.confirm:
            print()
            try:
                answer = input("  위 화면이 LED 3개에서 모두 제대로 보였나? "
                               "[y/N] ").strip().lower()
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
        description="ReSpeaker 2-Mics Pi HAT v2.0 LED 검수 (root 필요). "
                    "led_controller.py 가 실제로 쓰는 색으로 보여준다.")
    ap.add_argument("-b", "--brightness", type=float, default=0.45,
                    help="0~1 (기본 0.45). 밤에는 0.05 정도")
    ap.add_argument("-t", "--hold", type=float, default=3.0,
                    help="상태 하나를 유지할 초 (기본 3)")
    ap.add_argument("--fast", action="store_true",
                    help="짧게: 상태당 1초")
    ap.add_argument("--confirm", action="store_true",
                    help="마지막에 육안 확인 y/n 을 물어본다")
    ap.add_argument("--chip", default="gpiochip0", help="기본 gpiochip0")
    args = ap.parse_args()

    if args.fast:
        args.hold = 1.0
    if args.brightness > 1.0:
        # 예전 판은 0~31 정수였다. 그 습관으로 들어온 값을 받아준다.
        note(f"밝기 {args.brightness:g} 를 0~1 기준 "
             f"{args.brightness / MAX_BRIGHTNESS:.2f} 로 해석한다")
        args.brightness = args.brightness / MAX_BRIGHTNESS

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
