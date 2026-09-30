#!/usr/bin/env python3
"""Tegra234 PADCTL 핀먹스 레지스터를 /dev/mem 으로 직접 읽는다 (읽기 전용).

레지스터 비트 정의 (drivers/pinctrl/tegra/pinctrl-tegra234.c 의
PIN_PINGROUP_ENTRY_Y / PINGROUP 매크로에서):
  [1:0] MUX   - 펑션 선택 (PINGROUP 매크로의 f0..f3 중 하나)
  [3:2] PUPD  - 0=none 1=pull-down 2=pull-up
  [4]   TRISTATE - 1이면 출력 드라이버 꺼짐(하이임피던스)
  [6]   E_INPUT  - 1이면 입력 버퍼 켜짐
  [10]  GPIO_SF_SEL - 0=SFIO(특수기능) 1=GPIO
  [12]  SCHMITT
"""

import mmap
import os
import struct

PADCTL_BASE = 0x02430000
PAGE = 4096

# (표시이름, 레지스터오프셋, 헤더핀, f0..f3)
PINS = [
    ("spi1_mosi_pz5  (pin 19 MOSI)", 0xD040, ["SPI1", "RSVD1", "RSVD2", "RSVD3"]),
    ("spi1_miso_pz4  (pin 21 MISO)", 0xD018, ["SPI1", "RSVD1", "RSVD2", "RSVD3"]),
    ("spi1_sck_pz3   (pin 23 SCLK)", 0xD028, ["SPI1", "RSVD1", "RSVD2", "RSVD3"]),
    ("spi1_cs0_pz6   (pin 24 CE0) ", 0xD008, ["SPI1", "RSVD1", "RSVD2", "RSVD3"]),
    # 비교용: 오디오가 실제로 동작 중인 I2S2 핀들
    ("soc_gpio41_ph7 (pin 12 I2S) ", 0x4088, ["RSVD0", "I2S2", "RSVD2", "RSVD3"]),
    ("soc_gpio42_pi0 (pin 40 I2S) ", 0x4090, ["RSVD0", "I2S2", "RSVD2", "RSVD3"]),
    ("soc_gpio43_pi1 (pin 38 I2S) ", 0x4098, ["RSVD0", "I2S2", "RSVD2", "RSVD3"]),
    ("soc_gpio44_pi2 (pin 35 I2S) ", 0x40A0, ["RSVD0", "I2S2", "RSVD2", "RSVD3"]),
]

PUPD = {0: "none", 1: "pull-dn", 2: "pull-up", 3: "rsvd"}


def main():
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        print(f"{'pin group':<30} {'value':>10}  {'mux':<6} {'tri':<9} {'e_in':<6} "
              f"{'sfsel':<6} {'pupd'}")
        print("-" * 84)
        for name, off, funcs in PINS:
            addr = PADCTL_BASE + off
            page_base = addr & ~(PAGE - 1)
            page_off = addr - page_base
            m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ, offset=page_base)
            val = struct.unpack("<I", m[page_off:page_off + 4])[0]
            m.close()

            mux = funcs[val & 0x3]
            tri = "TRISTATE" if (val >> 4) & 1 else "driving"
            einput = "on" if (val >> 6) & 1 else "off"
            sfsel = "GPIO" if (val >> 10) & 1 else "SFIO"
            pupd = PUPD[(val >> 2) & 0x3]
            print(f"{name:<30} 0x{val:08x}  {mux:<6} {tri:<9} {einput:<6} "
                  f"{sfsel:<6} {pupd}")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
