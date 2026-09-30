# ReSpeaker 2-Mics Pi HAT v2.0 — LED on Jetson Orin Nano

보드의 APA102 RGB LED 3개를 Jetson Orin Nano (L4T R36.4.7 / JetPack 6.2) 에서 구동한다.

## 쓰는 법

```bash
sudo python3 respeaker_led.py demo            # 전체 데모
sudo python3 respeaker_led.py solid red -b 12 # 빨강 (Enter 누르면 꺼짐)
sudo python3 respeaker_led.py blink white
sudo python3 respeaker_led.py spin blue
sudo python3 respeaker_led.py off
```

## 음성 어시스턴트 상태 표시 — `led_controller.py`

LED 를 소유하는 전용 스레드에 상태 기계를 얹은 것. 음성 파이프라인은 상태만 던진다.

```bash
sudo ./led_controller.py demo              # 9개 상태를 순서대로 보여준다
sudo ./led_controller.py thinking          # 한 상태만 계속 유지 (값 고를 때)
sudo ./led_controller.py thinking --thinking-speed 2.4
sudo ./led_controller.py speaking --speaking-hz 2.2
```

```python
from led_controller import LedController, State

with LedController(brightness=0.45) as led:
    led.install_signal_handlers()     # SIGTERM/SIGINT 에 소등을 건다
    led.boot()
    led.set_phase(State.LISTENING)    # 웨이크워드 감지
    led.set_level(rms)                # 마이크 크기 0~1, 계속 갱신
    led.set_phase(State.THINKING)
    led.set_phase(State.SPEAKING)
    led.set_muted(True)               # 버튼(pin 11)에 물리기
    led.error()
```

### 상태

LED 가 3개뿐이라 **색이 아니라 움직임이 주 신호다.** 방 건너편에서 작은 LED 의 색은
잘 구분되지 않고 색각 이상이면 더하다. 특히 **정지 = 듣는 중, 움직임 = 처리 중** 이
둘을 가르는 핵심이고, 색으로만 구분하면 실패한다.

| 상태 | 패턴 | 색 | 종류 |
|---|---|---|---|
| `IDLE` | 꺼짐 | — | phase |
| `LISTENING` | 3개 고정 점등 | 청록 | phase |
| `CAPTURING` | 중앙 고정 + 바깥이 목소리 크기를 탐 | 청록 | phase |
| `THINKING` | 꼬리 달린 흐름 | 보라 | phase |
| `SPEAKING` | 중앙→양쪽 퍼지는 파동 | 따뜻한 흰색 | phase |
| `BOOT` | 한 번 훑기 | 흰색 | 일회성 |
| `ACK` | 한 번 플래시 | 초록 | 일회성 |
| `ERROR` | 2회 점멸 | 빨강 | 일회성 |
| `MUTE` | 중앙 1개 은은하게 | 빨강 | 플래그 |

`IDLE` 이 꺼짐인 건 의도다. 상시 점등은 밤에 거슬린다.

`MUTE` 는 빼지 마라. 마이크 달린 기기는 마이크가 살아 있는지 사용자가 볼 수 있어야
한다. 이 보드는 버튼이 이미 있으니 (pin 11 / line 112) `toggle_muted()` 에 물리면 된다.

### 우선순위

LED 3개로 두 상태를 동시에 못 보여준다. 겹치면 높은 쪽이 이긴다:

```
ERROR > MUTE > BOOT > ACK > SPEAKING > THINKING > CAPTURING > LISTENING > IDLE
```

`ERROR` 가 `MUTE` 보다 위인 이유: 마이크를 끈 상태일수록 오류를 알아야 하고, 둘 다
빨강이라 "마이크가 살아 있다" 는 오해를 주지 않는다. 표시가 끝나면 `MUTE` 로 돌아온다.
`MUTE` 를 덮는 것 중 마이크가 켜진 듯 보이는 상태는 없다.

### 알아둘 것

- **LED 는 상시 떠 있는 단일 프로세스가 소유해야 한다.** gpiod 라인은 한 프로세스만
  잡을 수 있어서 (EBUSY), 이벤트마다 스크립트를 띄우는 방식은 불가능하다.
- **비트뱅잉은 블로킹이다.** 그래서 전용 스레드다. 음성 파이프라인과 같은 스레드에서
  돌리면 인식이 끊긴다.
- **그리기 스레드가 죽으면 LED 가 마지막 프레임으로 얼어붙는다.** 예외를 잡아
  `led.failure` 로 알리니, 긴 루프를 도는 쪽에서 가끔 확인해라.
- **종료 시 소등이 필수다.** APA102 는 리셋이 없어 안 끄고 죽으면 색이 굳는다.
  `install_signal_handlers()` / `with` / `atexit` 세 겹으로 걸어뒀다.
- **CPU**: 애니메이션 중 한 코어의 12% (50fps). 정지 화면은 동일 프레임 전송을
  생략해서 0.4%. 부담되면 `fps=30` 으로 낮춰라 (약 7%).
- **밝기**: 전역 밝기(5비트)는 31 로 고정하고 dimming 은 RGB 로 한다. 전역 밝기
  필드는 색 PWM 보다 훨씬 낮은 주파수라 낮은 값에서 깜빡임이 보일 수 있다.
  밤에는 `brightness=0.05` 정도.

## 다른 장비 검수 — `led_test.py`

같은 Jetson Orin Nano + 같은 HAT 을 여러 대 검수할 때 쓴다.
**이 파일 하나만** 있으면 된다 (`respeaker_led.py` 불필요, 의존은 `python3-gpiod` 뿐).

대상 장비에서 받는 법 — 셋 중 아무거나:

```bash
# 1) 저장소를 통째로 clone (실행 권한이 보존돼 바로 실행된다)
git clone https://github.com/jaeminpz/respeaker-led-jetson.git
cd respeaker-led-jetson && sudo ./led_test.py

# 2) 검수 스크립트 한 파일만 (raw 는 실행 권한이 안 따라오므로 python3 로 실행)
curl -fsSLO https://raw.githubusercontent.com/jaeminpz/respeaker-led-jetson/main/led_test.py
sudo python3 led_test.py

# 3) 이 장비에서 직접 복사
scp led_test.py <장비>:~/
ssh <장비> 'sudo python3 led_test.py'
```

`python3-gpiod` 가 없으면 `sudo apt install python3-libgpiod` 로 넣는다.

```
[1/8] 환경 점검 .......................... OK
      gpiod 1.x, padctl sck=0x00001044 mosi=0x00000044, 밝기 0.45
[2/8] 채널 점검 (R/G/B 단독) ............... OK
[3/8] LISTENING  청록 고정 ............... OK
[4/8] CAPTURING  청록 + 음성 크기 .......... OK
[5/8] THINKING   보라 흐름 ............... OK
[6/8] SPEAKING   따뜻한 흰색 파동 ........... OK
[7/8] MUTE       중앙 빨강 은은하게 .......... OK
[8/8] 일회성     BOOT / ACK / ERROR ..... OK

결과: PASS
```

**색은 `led_controller.py` 가 실제로 쓰는 팔레트 그대로다.** 임의의 빨강/초록/파랑이
아니라 현장에서 보게 될 화면으로 검수해야 의미가 있기 때문이다. 다만 맨 앞에 R/G/B
단독 점검을 짧게 넣었다 — 팔레트만 돌리면 한 채널이 죽어도 다른 색에 묻혀 안 보인다.

종료 코드는 PASS 0 / FAIL 1 / 중단 130 이라 상위 검수 스크립트에 물릴 수 있다.

| 옵션 | 뜻 |
|---|---|
| `--fast` | 상태당 1초로 짧게 — 반복 검수용 |
| `--confirm` | 마지막에 육안 확인 y/n 을 물어보고 답을 PASS/FAIL 에 반영 |
| `-b` | 밝기 0~1 (기본 0.45). 밤에는 0.05 정도 |
| `-t` | 상태 하나를 유지할 초 (기본 3) |

### 팔레트가 어긋나는 것 막기

이 파일은 한 파일로 배포되어야 하므로 `led_controller.py` 의 팔레트와 애니메이션을
**복사해서** 갖고 있다. 그래서 저장소 전체가 있는 환경에서 실행하면 두 구현의 출력을
1200 지점에서 비교해 어긋났으면 경고한다. 한 파일만 있는 장비에서는 조용히 건너뛴다.

```
경고: 팔레트 어긋남 — thinking: led_controller 와 다른 그림 (t=0.005, level=0.0)
```

`led_controller.py` 의 색이나 속도를 고치면 이 파일에도 같이 반영해라.

**이 보드의 대표 실패 모드는 "에러 없이 조용히 아무 일도 안 일어남"** 이므로,
padctl 을 고친 뒤 레지스터를 되읽어서 `TRISTATE` / `GPIO_SF_SEL` / `mux` 가 실제로
내려갔는지 확인한다. 셋 중 하나라도 어긋나면 LED 를 보지 않고도 FAIL 로 잡힌다.
그 외에 root 여부, `python3-gpiod` 유무(1.x/2.x 양쪽 지원), `/dev/gpiochip0` 존재,
라인 선점(EBUSY), `/dev/mem` 접근, 프레임 전송 속도를 점검한다.

LED 소자가 죽었거나 납땜이 떨어진 경우는 소프트웨어로 알 수 없으므로 색은 눈으로 봐야 한다.

라이브러리로:

```python
from respeaker_led import Pixels

with Pixels(brightness=8) as px:
    px.solid(Pixels.GREEN)
    px.one(0, Pixels.RED)          # 0번만 빨강
    px.spin(Pixels.BLUE)
    px.set_pixel(2, 255, 120, 0)   # 직접 RGB
    px.show()
```

`root 권한 필요` — padctl 레지스터를 `/dev/mem` 으로 고쳐야 한다 (아래 참고).

성능: 프레임당 약 1.8 ms (~540 fps). 애니메이션에 충분하다.

## 하드웨어

v2.0 스키매틱(`202004059_ReSpeaker-2-Mics-Pi-HAT-V2.0_SCH_PDF_241121.pdf`) 기준:

| 신호 | 헤더 핀 | Jetson 패드 | gpiochip0 라인 |
|---|---|---|---|
| LED 데이터 (APA102 DI) | 19 | PZ.05 / `spi1_mosi_pz5` | 135 |
| LED 클럭 (APA102 CI) | 23 | PZ.03 / `spi1_sck_pz3` | 133 |
| 버튼 (active-low) | 11 | PR.04 | 112 |

- LED는 `APA102-2020-256-6A` 3개 데이지체인. LED1이 체인 첫 번째(index 0).
- **CS는 미연결** — CE0/CE1 모두 어디에도 안 붙어 있어서 SPI device 번호는 무의미.
- v2.0은 v1과 달리 LED가 **5V** 로 구동되고, 데이터/클럭이 5V로 동작하는
  `SN74LVC1G17` 슈미트 버퍼(U5/U4)를 거친다. 버퍼 입력에는 10K 풀다운이 있다.
- 프레임 포맷: start `00 00 00 00` → LED당 `[0xE0|밝기5비트, B, G, R]` → end `FF FF FF FF`.
  **바이트 순서는 BGR** 이다.

## 왜 spidev 가 아니라 GPIO 비트뱅잉인가

라즈베리파이에서는 이 핀들이 SPI0라 `spidev` 로 쏘면 되지만, Jetson에서는 동작하지 않는다.
실제로 확인한 내용:

1. **부트로더(MB1 BCT)가 pin 19/23 패드를 `TRISTATE=1` 로 남겨둔다.**
   출력 드라이버 자체가 꺼져 있어서 SPI든 GPIO든 신호가 핀까지 나가지 않는다.
   `/dev/spidev0.0` 이 존재하는 것과 핀이 실제로 연결된 것은 별개다.
   `jetson-io` 의 40핀 헤더 기능 목록에도 아무것도 enable 되어 있지 않다.

2. **padctl 의 `GPIO_SF_SEL`(bit 10) 이 0 일 때만 LED가 반응한다.**
   그런데 커널 pinctrl 에 gpio-ranges 가 등록되어 있어서
   (`gpiochip0` GPIOS[348-511] ↔ PINS[0-163]), gpiod 로 라인을 요청하는 순간
   커널이 이 비트를 1로 되돌린다.

   → **반드시 `gpiod 라인 요청` → `그 다음에 padctl 수정` 순서여야 한다.**
   순서가 뒤바뀌면 에러 없이 조용히 아무 일도 일어나지 않는다.

3. spidev 경로는 100kHz~8MHz 전부 시도했으나 동작하지 않았다.
   APA102는 클럭 동기식이라 비트뱅잉의 느린 속도가 전혀 문제되지 않으므로
   비트뱅잉으로 확정했다.

### padctl 레지스터

`drivers/pinctrl/tegra/pinctrl-tegra234.c` 기준. 베이스 `0x02430000`:

| 핀 | mux 레지스터 offset | 동작하는 값 |
|---|---|---|
| `spi1_mosi_pz5` (pin 19) | `0xD040` | `0x00000044` |
| `spi1_sck_pz3` (pin 23) | `0xD028` | `0x00001044` |

비트 정의: `[1:0]` mux, `[3:2]` pupd, `[4]` TRISTATE, `[6]` E_INPUT,
`[10]` GPIO_SF_SEL, `[12]` SCHMITT.

이 설정은 레지스터에만 존재하므로 **재부팅하면 사라진다.** 드라이버가 매번 다시
적용하므로 따로 부팅 설정을 건드릴 필요는 없다 (부팅 시 한 번 설정해두는 방식은
어차피 gpiod 요청 때 커널이 되돌려놓기 때문에 쓸 수 없다).

## diagnostics/

문제를 좁히는 과정에서 쓴 스크립트들. 일상적으로 필요하지는 않지만 재진단에 쓸 수 있다.

| 파일 | 용도 |
|---|---|
| `padctl_dump.py` | pin 19/21/23/24 와 I2S 핀의 padctl 상태 덤프 (읽기 전용) |
| `compare_configs.py` | 네 가지 구동 설정을 각각 다른 색으로 시도해 동작하는 것 가리기 |
| `verify_a.py` | 확정된 설정으로 색 순환 + 장시간 유지 |
| `padctl_fix.py` | padctl 을 SFIO+구동 으로 (SPI 경로 시도용, `--restore` 지원) |
| `drive_boost.py` | 패드 드라이브 강도 조정 (효과 없었음) |
| 나머지 | SPI/비트뱅 초기 시도들 — 동작하지 않는 경로 |

## 주의

- 오디오(`hw:APE,0`)는 이 작업과 무관하게 계속 정상 동작한다. 건드린 레지스터는
  40핀 헤더 pin 19/21/23/24 뿐이고 I2S 핀은 손대지 않았다.
- APA102는 리셋 핀이 없다. 이상한 색으로 굳으면 전원을 완전히 내려야 풀린다.
