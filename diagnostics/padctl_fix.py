#!/usr/bin/env python3
"""헤더 pin 19/21/23/24 패드를 SPI1 기능으로 런타임 전환한다.

부트로더(MB1 BCT)가 이 패드들을 TRISTATE=1 / sfsel=GPIO 로 남겨두기 때문에
SPI 컨트롤러 출력도, GPIO 출력도 핀까지 가지 못한다. 여기서 하는 일:
  bit 4  TRISTATE    1 -> 0   (출력 드라이버 켜기)
  bit 10 GPIO_SF_SEL 1 -> 0   (패드를 SPI 컨트롤러에 연결)
  bit[1:0] MUX       -> 0     (SPI1)  * 이미 0이라 실제로는 변화 없음

MISO(pin 21)는 입력이라 TRISTATE를 그대로 둔다 — HAT에서 미연결이기도 하다.

이 변경은 레지스터에만 남으므로 재부팅하면 원래대로 돌아간다.
  적용:   sudo python3 padctl_fix.py
  되돌림: sudo python3 padctl_fix.py --restore
"""

import argparse
import mmap
import os
import struct

PADCTL_BASE = 0x02430000
PAGE = 4096

TRISTATE = 1 << 4
GPIO_SF_SEL = 1 << 10
MUX_MASK = 0x3

# 부트 직후 읽은 원래 값 (되돌릴 때 사용)
ORIGINAL = {
    0xD040: 0x00000454,  # spi1_mosi_pz5  pin 19
    0xD028: 0x00001454,  # spi1_sck_pz3   pin 23
    0xD008: 0x00000458,  # spi1_cs0_pz6   pin 24
}

NAMES = {
    0xD040: "spi1_mosi_pz5 (pin 19 MOSI)",
    0xD028: "spi1_sck_pz3  (pin 23 SCLK)",
    0xD008: "spi1_cs0_pz6  (pin 24 CE0) ",
}


def rw(fd, off, new=None):
    addr = PADCTL_BASE + off
    base = addr & ~(PAGE - 1)
    m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED,
                  mmap.PROT_READ | (mmap.PROT_WRITE if new is not None else 0),
                  offset=base)
    idx = addr - base
    old = struct.unpack("<I", m[idx:idx + 4])[0]
    if new is not None:
        m[idx:idx + 4] = struct.pack("<I", new)
        old = struct.unpack("<I", m[idx:idx + 4])[0]
    m.close()
    return old


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", action="store_true", help="원래 값으로 복원")
    args = ap.parse_args()

    flags = os.O_RDWR | os.O_SYNC
    fd = os.open("/dev/mem", flags)
    try:
        for off, name in NAMES.items():
            cur = rw(fd, off)
            if args.restore:
                new = ORIGINAL[off]
            else:
                new = (cur & ~(TRISTATE | GPIO_SF_SEL | MUX_MASK))  # SPI1 = mux 0
            got = rw(fd, off, new)
            print(f"{name}: 0x{cur:08x} -> 0x{got:08x}"
                  f"  [{'TRISTATE' if got & TRISTATE else 'driving':<8}"
                  f" {'GPIO' if got & GPIO_SF_SEL else 'SFIO'}]")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
