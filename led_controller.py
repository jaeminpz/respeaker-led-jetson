#!/usr/bin/env python3
"""음성 어시스턴트용 LED 상태 표시 — ReSpeaker 2-Mics Pi HAT v2.0 / Jetson Orin Nano.

`respeaker_led.APA102` 위에 상태 기계를 얹은 것이다. 음성 파이프라인은 상태만
던지고, 실제 그리기는 여기 전용 스레드가 맡는다.

    from led_controller import LedController, State

    with LedController(brightness=0.45) as led:
        led.boot()                       # 부팅 훑기 (일회성)
        led.set_phase(State.LISTENING)   # 웨이크워드 감지
        led.set_level(rms)               # 마이크 크기 (0~1) — 계속 갱신
        led.set_phase(State.THINKING)    # LLM 추론 중
        led.set_phase(State.SPEAKING)    # TTS 재생 중
        led.set_phase(State.IDLE)
        led.set_muted(True)              # 버튼으로 마이크 끔
        led.error()                      # 네트워크 실패 등

왜 전용 스레드인가
------------------
1. gpiod 라인은 한 프로세스만 잡을 수 있다. 이벤트마다 스크립트를 띄우는 방식은
   EBUSY 로 실패한다. LED 는 상시 떠 있는 하나가 소유해야 한다.
2. 비트뱅잉은 블로킹이다 (프레임당 ~1.8ms). 애니메이션을 음성 파이프라인과 같은
   스레드에서 돌리면 인식이 끊긴다.
3. 라인 요청 -> padctl 수정 순서 제약이 있어 재연결이 싸지 않다.

우선순위
--------
LED 가 3개뿐이라 두 상태를 동시에 못 보여준다. 겹치면 높은 쪽이 이긴다:

    ERROR > MUTE > BOOT > ACK > SPEAKING > THINKING > CAPTURING > LISTENING > IDLE

ERROR 를 MUTE 위에 둔 이유: 마이크를 끈 상태일수록 오류를 알아야 하고, 둘 다
빨강이라 "마이크가 살아 있다" 는 오해를 주지 않는다. 오류 표시가 끝나면 곧바로
MUTE 로 돌아온다. MUTE 를 덮는 것 중 마이크가 켜진 듯 보이는 상태는 없다.

밝기
----
전역 밝기(APA102 의 5비트)는 31 로 고정하고 dimming 은 RGB 값으로 한다.
전역 밝기 필드는 색 PWM 보다 훨씬 낮은 주파수로 동작해서 낮은 값에서 깜빡임이
보일 수 있기 때문이다. RGB 로 줄이면 8비트 해상도를 그대로 쓴다.

root 권한이 필요하다 (/dev/mem 으로 padctl 수정).
"""

import argparse
import atexit
import math
import signal
import sys
import threading
import time
from enum import IntEnum

from respeaker_led import APA102, NUM_LED

__all__ = ["LedController", "State"]


class State(IntEnum):
    """값이 곧 우선순위다. 큰 쪽이 이긴다."""

    IDLE = 0
    LISTENING = 30      # 웨이크워드 감지, 사용자 입 열기를 기다림
    CAPTURING = 40      # 사용자가 말하는 중 (set_level 로 크기 반영)
    THINKING = 50       # LLM/처리 중
    SPEAKING = 60       # TTS 재생 중 (set_level 로 파동 반영)
    ACK = 70            # 명령 수행 완료 (일회성)
    BOOT = 80           # 기동 (일회성)
    MUTE = 90           # 마이크 꺼짐
    ERROR = 100         # 오류 (일회성)


#: set_phase 로 설정하는, 서로 배타적인 상태들
PHASES = frozenset({State.IDLE, State.LISTENING, State.CAPTURING,
                    State.THINKING, State.SPEAKING})

#: pulse 로 띄우는 일회성 상태와 기본 지속 시간(초)
TRANSIENTS = {State.BOOT: 0.90, State.ACK: 0.35, State.ERROR: 0.90}

# --- 색 (0~1 실수) ------------------------------------------------------------
C_CYAN = (0.00, 0.70, 1.00)     # 듣는 중
C_PURPLE = (0.60, 0.00, 1.00)   # 생각 중
C_WARM = (1.00, 0.66, 0.31)     # 말하는 중
C_RED = (1.00, 0.00, 0.00)      # 오류 / 마이크 꺼짐
C_GREEN = (0.00, 1.00, 0.24)    # 확인
C_WHITE = (1.00, 1.00, 1.00)    # 기동

# --- 눈으로 보고 고쳐야 하는 값들 (CLI 로도 덮어쓸 수 있다) --------------------
THINKING_SPEED = 3.2            # 초당 몇 칸 흐르는가
THINKING_TAIL = 1.35            # 꼬리 길이 (LED 개수 단위)
SPEAKING_HZ = 1.6               # 초당 파동 몇 번
SPEAKING_SPREAD = 0.30          # 중앙 -> 바깥 지연 (주기 비율)
MUTE_LEVEL = 0.12               # 마이크 꺼짐 표시 밝기 (은은해야 한다)

OFF3 = [(0.0, 0.0, 0.0)] * NUM_LED


def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _dim(color, k):
    k = _clamp01(k)
    return (color[0] * k, color[1] * k, color[2] * k)


def _bump(e, phase):
    """0~1 주기 위상 e 에서 phase 지점에 뾰족한 봉우리. 되돌아오는 파동용."""
    return (0.5 * (1.0 + math.cos(2.0 * math.pi * (e - phase)))) ** 3


# --- 상태별 그리기 함수: (t, level) -> LED 3개의 (r,g,b) 실수 -----------------
# t 는 그 상태가 화면에 뜬 뒤 지난 초, level 은 평활된 마이크/출력 크기 0~1

def _a_idle(t, level):
    # 평상시는 완전히 꺼둔다. 항상 켜져 있으면 밤에 거슬린다.
    return OFF3


def _a_boot(t, level):
    # 왼쪽 -> 오른쪽으로 한 번 훑고 끝난다
    head = t * 4.0
    return [_dim(C_WHITE, 1.0 - abs(i - head) * 1.6) for i in range(NUM_LED)]


def _a_listening(t, level):
    # 정지 = 듣는 중. 여기서 움직이면 THINKING 과 헷갈린다.
    return [_dim(C_CYAN, 0.75)] * NUM_LED


def _a_capturing(t, level):
    # 중앙은 항상 켜두고(마이크가 살아 있다는 표시), 바깥이 목소리 크기를 탄다
    center = 0.30 + 0.70 * level
    outer = 0.05 + 0.95 * _clamp01((level - 0.12) / 0.88)
    return [_dim(C_CYAN, outer), _dim(C_CYAN, center), _dim(C_CYAN, outer)]

def _a_thinking(t, level):
    # 움직임 = 처리 중. 꼬리를 달아 방향이 보이게 한다.
    head = (t * THINKING_SPEED) % NUM_LED
    out = []
    for i in range(NUM_LED):
        d = abs(i - head)
        d = min(d, NUM_LED - d)                  # 양 끝이 이어진 것으로 본다
        # max() 를 먼저 씌우는 게 중요하다. 꼬리 밖이면 밑이 음수가 되는데
        # 파이썬에서 음수의 실수 거듭제곱은 예외가 아니라 복소수를 낸다.
        out.append(_dim(C_PURPLE, max(0.0, 1.0 - d / THINKING_TAIL) ** 1.5))
    return out


def _a_speaking(t, level):
    # 중앙에서 양쪽으로 퍼지는 파동 — THINKING 의 한쪽 흐름과 구분된다
    e = (t * SPEAKING_HZ) % 1.0
    amp = 0.35 + 0.65 * level
    center = (0.25 + 0.75 * _bump(e, 0.0)) * amp
    outer = (0.25 + 0.75 * _bump(e, SPEAKING_SPREAD)) * amp
    return [_dim(C_WARM, outer), _dim(C_WARM, center), _dim(C_WARM, outer)]


def _a_mute(t, level):
    # 중앙 하나만 은은하게. 꺼진 것과 구분되면서 방해되지 않아야 한다.
    return [(0.0, 0.0, 0.0), _dim(C_RED, MUTE_LEVEL), (0.0, 0.0, 0.0)]


def _a_error(t, level):
    # 0.15초 간격으로 두 번 깜빡이고 끝
    on = t < 0.60 and int(t / 0.15) % 2 == 0
    return [C_RED] * NUM_LED if on else OFF3


def _a_ack(t, level):
    # 짧게 한 번 — 말 없이 끝나는 명령의 확인
    return [_dim(C_GREEN, math.sin(math.pi * _clamp01(t / TRANSIENTS[State.ACK])))] * NUM_LED


ANIMATIONS = {
    State.IDLE: _a_idle,
    State.BOOT: _a_boot,
    State.LISTENING: _a_listening,
    State.CAPTURING: _a_capturing,
    State.THINKING: _a_thinking,
    State.SPEAKING: _a_speaking,
    State.MUTE: _a_mute,
    State.ERROR: _a_error,
    State.ACK: _a_ack,
}


class LedController:
    """LED 를 소유하는 전용 스레드. 상태만 던지면 알아서 그린다.

    스레드 안전하다 — set_phase / set_level / pulse 는 어느 스레드에서 불러도 된다.
    """

    def __init__(self, brightness=0.45, fps=50, chip="gpiochip0"):
        self._brightness = _clamp01(brightness)
        self._period = 1.0 / max(1, fps)
        self._chip = chip

        self._lock = threading.Lock()
        self._phase = State.IDLE
        self._muted = False
        self._transients = {}        # State -> (시작시각, 종료시각)
        self._level_target = 0.0
        self._level = 0.0

        self._leds = None
        self._thread = None
        self._stop_evt = threading.Event()
        self._closed = False
        self._last_frame = None
        self._failure = None

    # --- 수명 --------------------------------------------------------------
    def start(self):
        """LED 를 잡고 그리기 스레드를 띄운다. 실패하면 여기서 예외가 난다."""
        if self._thread is not None:
            return self
        # 전역 밝기는 31 고정, dimming 은 RGB 로 (모듈 docstring 참고)
        self._leds = APA102(brightness=31, chip=self._chip)
        atexit.register(self.stop)
        self._thread = threading.Thread(target=self._run, name="led", daemon=False)
        self._thread.start()
        return self

    @property
    def failure(self):
        """그리기 스레드가 죽었으면 그 예외, 살아 있으면 None.

        LED 가 얼어붙은 것을 호출자가 알아챌 수 있는 유일한 통로다.
        긴 루프를 도는 쪽에서 가끔 확인하는 것을 권한다.
        """
        return self._failure

    def stop(self):
        """소등하고 라인을 놓는다. 여러 번 불러도 안전하다."""
        if self._closed:
            return
        self._closed = True
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._leds is not None:
            # APA102 는 리셋이 없어서, 안 끄고 죽으면 색이 그대로 굳는다
            try:
                self._leds.close()
            finally:
                self._leds = None

    def install_signal_handlers(self):
        """SIGTERM/SIGINT 에 소등을 건다. 메인 스레드에서만 부를 수 있다."""
        def handler(signum, frame):
            self.stop()
            sys.exit(128 + signum)

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, handler)
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # --- 상태 --------------------------------------------------------------
    def set_phase(self, state):
        """서로 배타적인 진행 상태를 바꾼다 (IDLE/LISTENING/CAPTURING/...)."""
        state = State(state)
        if state not in PHASES:
            raise ValueError(
                f"{state.name} 은 phase 가 아니다. "
                f"일회성은 pulse(), 마이크는 set_muted() 를 써라. "
                f"가능한 값: {', '.join(s.name for s in sorted(PHASES))}")
        with self._lock:
            self._phase = state

    @property
    def phase(self):
        with self._lock:
            return self._phase

    def set_muted(self, muted):
        """마이크 꺼짐 표시. 다른 상태를 덮는다 (ERROR 제외)."""
        with self._lock:
            self._muted = bool(muted)

    def toggle_muted(self):
        """버튼(pin 11 / line 112)에 물리기 좋다. 바뀐 값을 돌려준다."""
        with self._lock:
            self._muted = not self._muted
            return self._muted

    @property
    def muted(self):
        with self._lock:
            return self._muted

    def pulse(self, state, duration=None):
        """일회성 표시를 띄운다 (BOOT/ACK/ERROR). 시간이 지나면 알아서 사라진다."""
        state = State(state)
        if state not in TRANSIENTS:
            raise ValueError(
                f"{state.name} 은 일회성이 아니다. "
                f"가능한 값: {', '.join(s.name for s in TRANSIENTS)}")
        d = TRANSIENTS[state] if duration is None else duration
        now = time.perf_counter()
        with self._lock:
            self._transients[state] = (now, now + d)

    def boot(self):
        self.pulse(State.BOOT)

    def ack(self):
        self.pulse(State.ACK)

    def error(self):
        self.pulse(State.ERROR)

    def set_level(self, level):
        """마이크/출력 크기 0~1. CAPTURING 과 SPEAKING 이 이걸 탄다.

        자주 불러도 된다 — 실제 반영은 그리기 스레드에서 평활해서 쓴다.
        """
        with self._lock:
            self._level_target = _clamp01(level)

    @property
    def brightness(self):
        return self._brightness

    @brightness.setter
    def brightness(self, value):
        """0~1. 밤에는 0.05 정도까지 낮추는 것을 권한다."""
        self._brightness = _clamp01(value)
        self._last_frame = None      # 정지 화면이어도 다시 그리게 한다

    # --- 그리기 스레드 -------------------------------------------------------
    def _resolve(self, now):
        """지금 그려야 할 상태와 그 상태가 뜬 시각을 고른다."""
        with self._lock:
            best, since = self._phase, None
            if self._muted and State.MUTE > best:
                best, since = State.MUTE, None
            for state, (start, deadline) in list(self._transients.items()):
                if now >= deadline:
                    del self._transients[state]
                elif state > best:
                    best, since = state, start

            # 크기는 빠르게 따라 올라가고 천천히 내려온다 (깜빡임 방지)
            target = self._level_target
            k = 0.60 if target > self._level else 0.15
            self._level += (target - self._level) * k
            level = self._level
        return best, since, level

    def _run(self):
        # 그리기 스레드가 조용히 죽으면 LED 가 마지막 프레임으로 얼어붙은 채
        # 호출자는 아무것도 모른다. 예외를 잡아 두고 failure 로 알린다.
        try:
            self._loop()
        except BaseException as e:              # noqa: BLE001 - 무엇이든 붙잡는다
            self._failure = e
            self._stop_evt.set()

    def _loop(self):
        period = self._period
        cur, cur_since = None, 0.0
        next_tick = time.perf_counter()
        while not self._stop_evt.is_set():
            now = time.perf_counter()
            state, since, level = self._resolve(now)

            if state != cur:
                cur = state
                cur_since = since if since is not None else now
            elif since is not None:
                cur_since = since

            colors = ANIMATIONS[state](now - cur_since, level)
            self._emit(colors)

            next_tick += period
            delay = next_tick - time.perf_counter()
            if delay < 0:                      # 밀렸으면 따라잡지 않고 리셋
                next_tick = time.perf_counter()
            else:
                self._stop_evt.wait(delay)

    def _emit(self, colors):
        k = self._brightness * 255.0
        frame = tuple(
            (int(_clamp01(r) * k + 0.5),
             int(_clamp01(g) * k + 0.5),
             int(_clamp01(b) * k + 0.5))
            for r, g, b in colors)
        if frame == self._last_frame:
            return                             # 정지 화면이면 전송을 건너뛴다
        self._last_frame = frame
        for i, (r, g, b) in enumerate(frame):
            self._leds.set_pixel(i, r, g, b)
        self._leds.show()


# --- CLI: 눈으로 보고 값을 고르기 위한 것 --------------------------------------

def _fake_level(t, speaking=False):
    """말소리 비슷한 크기 곡선. 데모에서 파동을 보려고 쓴다."""
    if speaking:
        v = 0.55 + 0.45 * math.sin(2 * math.pi * 1.1 * t)
        v *= 0.7 + 0.3 * math.sin(2 * math.pi * 0.37 * t + 1.0)
        return _clamp01(v)
    v = 0.5 + 0.5 * math.sin(2 * math.pi * 0.8 * t)
    v *= 0.5 + 0.5 * math.sin(2 * math.pi * 0.23 * t)
    return _clamp01(v ** 0.7)


class _RenderDied(Exception):
    """그리기 스레드가 죽었다. 데모를 계속해봐야 LED 는 얼어 있다."""


def _check(led):
    if led.failure is not None:
        raise _RenderDied(led.failure)


def _hold(led, state, seconds, label, level_fn=None):
    print(f"  {state.name:<10} {label}", flush=True)
    if state in PHASES:
        led.set_phase(state)
    t0 = time.perf_counter()
    while True:
        t = time.perf_counter() - t0
        if t >= seconds:
            break
        if level_fn is not None:
            led.set_level(level_fn(t))
        time.sleep(0.02)
        _check(led)
    led.set_level(0.0)
    _check(led)


def _nap(led, seconds):
    """자면서도 그리기 스레드가 살아 있는지 확인한다."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        time.sleep(0.05)
        _check(led)


def _demo(led, hold):
    print("\n데모 — 각 상태를 순서대로 보여준다\n")
    led.boot()
    _nap(led, TRANSIENTS[State.BOOT] + 0.3)
    print(f"  {'BOOT':<10} 기동 훑기 (일회성)")

    _hold(led, State.IDLE, 1.2, "평상시 — 꺼짐이 정상이다")
    _hold(led, State.LISTENING, hold, "웨이크워드 감지 — 정지")
    _hold(led, State.CAPTURING, hold, "사용자 발화 — 바깥이 목소리를 탄다",
          lambda t: _fake_level(t))
    _hold(led, State.THINKING, hold, "처리 중 — 흐름")
    _hold(led, State.SPEAKING, hold, "TTS 재생 — 퍼지는 파동",
          lambda t: _fake_level(t, speaking=True))

    led.set_phase(State.IDLE)
    led.ack()
    print(f"  {'ACK':<10} 명령 완료 (일회성)")
    _nap(led, TRANSIENTS[State.ACK] + 0.6)

    led.error()
    print(f"  {'ERROR':<10} 오류 (일회성)")
    _nap(led, TRANSIENTS[State.ERROR] + 0.6)

    print(f"  {'MUTE':<10} 마이크 꺼짐 — 중앙 하나만 은은하게")
    led.set_muted(True)
    _nap(led, hold)

    print(f"  {'MUTE':<10} + 오류: ERROR 가 MUTE 를 잠깐 덮고 되돌아온다")
    led.error()
    _nap(led, TRANSIENTS[State.ERROR] + 1.0)

    print(f"  {'MUTE':<10} + THINKING: MUTE 가 이긴다 (아무 변화 없어야 정상)")
    led.set_phase(State.THINKING)
    _nap(led, hold)

    led.set_muted(False)
    print(f"  {'THINKING':<10} 마이크 다시 켬 — 가려져 있던 상태가 드러난다")
    _nap(led, hold)

    led.set_phase(State.IDLE)
    _check(led)
    print("\n끝")


def main():
    global THINKING_SPEED, SPEAKING_HZ

    ap = argparse.ArgumentParser(
        description="음성 어시스턴트 LED 상태 표시 (root 필요)")
    ap.add_argument("command", nargs="?", default="demo",
                    help="demo | 상태 이름 하나를 계속 유지 "
                         f"({', '.join(s.name.lower() for s in State)})")
    ap.add_argument("-b", "--brightness", type=float, default=0.45,
                    help="0~1 (기본 0.45). 밤에는 0.05 정도")
    ap.add_argument("--hold", type=float, default=4.0,
                    help="데모에서 상태별 유지 초 (기본 4)")
    ap.add_argument("--fps", type=int, default=50, help="기본 50")
    ap.add_argument("--thinking-speed", type=float, default=None,
                    help=f"초당 흐르는 칸 수 (기본 {THINKING_SPEED})")
    ap.add_argument("--speaking-hz", type=float, default=None,
                    help=f"초당 파동 횟수 (기본 {SPEAKING_HZ})")
    args = ap.parse_args()

    if args.thinking_speed is not None:
        THINKING_SPEED = args.thinking_speed
    if args.speaking_hz is not None:
        SPEAKING_HZ = args.speaking_hz

    led = LedController(brightness=args.brightness, fps=args.fps)
    try:
        led.start()
    except PermissionError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    led.install_signal_handlers()

    try:
        if args.command == "demo":
            _demo(led, args.hold)
        else:
            try:
                state = State[args.command.upper()]
            except KeyError:
                print(f"오류: 모르는 상태 '{args.command}'", file=sys.stderr)
                return 2
            print(f"{state.name} 유지 중 — Ctrl-C 로 종료")
            if state in PHASES:
                led.set_phase(state)
            elif state is State.MUTE:
                led.set_muted(True)
            t0 = time.perf_counter()
            while True:
                t = time.perf_counter() - t0
                if state in TRANSIENTS:
                    led.pulse(state)          # 계속 다시 띄워서 반복 재생
                    time.sleep(TRANSIENTS[state] + 0.5)
                    continue
                if state in (State.CAPTURING, State.SPEAKING):
                    led.set_level(_fake_level(t, state is State.SPEAKING))
                time.sleep(0.02)
                _check(led)
    except _RenderDied as e:
        print(f"\n오류: 그리기 스레드가 죽었다 — {e.args[0]!r}", file=sys.stderr)
        print("LED 가 마지막 프레임에서 얼어붙는다. 위 예외를 고쳐야 한다.",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
    finally:
        led.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
