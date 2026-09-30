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
[1/6] 환경 점검 ...................... OK
      gpiod 1.x, padctl sck=0x00001044 mosi=0x00000044, 밝기 12/31
[2/6] RED   (3s) ................. OK
...
결과: PASS
```

종료 코드는 PASS 0 / FAIL 1 / 중단 130 이라 상위 검수 스크립트에 물릴 수 있다.

| 옵션 | 뜻 |
|---|---|
| `--fast` | 짧게 (hold 0.6s, spin 2바퀴) — 반복 검수용 |
| `--confirm` | 마지막에 육안 확인 y/n 을 물어보고 답을 PASS/FAIL 에 반영 |
| `-b/-t/-d/-r` | 밝기 / 단색 유지 초 / 점멸 간격 / spin 바퀴 수 |

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
