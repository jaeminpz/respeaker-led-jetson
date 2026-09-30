#!/usr/bin/env python3
"""pin 19/23 패드의 드라이브 강도를 올린다.

HAT v2.0은 LED 신호를 5V로 동작하는 SN74LVC1G17 슈미트 버퍼에 물린다.
VCC=5V일 때 이 버퍼의 VT+ 상한은 3.0~3.3V라 3.3V 로직으로는 마진이 거의 없고,
엣지가 느리면 문턱을 못 넘는다. 패드 드라이브를 키우면 엣지가 빨라져
같은 클럭에서 더 확실하게 문턱을 넘긴다.

드라이브 레지스터 필드 (pinctrl-tegra234.c 의 DRV_PINGROUP_ENTRY_Y):
    drvdn: bit 12, width 5   (풀다운 구동 강도)
    drvup: bit 20, width 5   (풀업 구동 강도)
"""

import argparse
import mmap
import os
import struct

PADCTL_BASE = 0x02430000
PAGE = 4096

DRIVE_REGS = {
    "pin 19 MOSI": 0xD044,
    "pin 23 SCLK": 0xD02C,
}

DRVDN_BIT, DRVDN_W = 12, 5
DRVUP_BIT, DRVUP_W = 20, 5


def rw(fd, off, new=None):
    addr = PADCTL_BASE + off
    base = addr & ~(PAGE - 1)
    idx = addr - base
    prot = mmap.PROT_READ | (mmap.PROT_WRITE if new is not None else 0)
    m = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, prot, offset=base)
    val = struct.unpack("<I", m[idx:idx + 4])[0]
    if new is not None:
        m[idx:idx + 4] = struct.pack("<I", new)
        val = struct.unpack("<I", m[idx:idx + 4])[0]
    m.close()
    return val


def decode(v):
    dn = (v >> DRVDN_BIT) & ((1 << DRVDN_W) - 1)
    up = (v >> DRVUP_BIT) & ((1 << DRVUP_W) - 1)
    return dn, up


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, default=31,
                    help="0~31 드라이브 강도 (기본 31 = 최대)")
    ap.add_argument("--show", action="store_true", help="현재 값만 출력")
    args = ap.parse_args()

    lvl = max(0, min(31, args.level))
    fd = os.open("/dev/mem", os.O_RDONLY if args.show else os.O_RDWR | os.O_SYNC)
    try:
        for name, off in DRIVE_REGS.items():
            cur = rw(fd, off)
            if args.show:
                dn, up = decode(cur)
                print(f"{name}: 0x{cur:08x}  drvdn={dn} drvup={up}")
                continue
            new = cur
            new &= ~(((1 << DRVDN_W) - 1) << DRVDN_BIT)
            new &= ~(((1 << DRVUP_W) - 1) << DRVUP_BIT)
            new |= (lvl << DRVDN_BIT) | (lvl << DRVUP_BIT)
            got = rw(fd, off, new)
            odn, oup = decode(cur)
            ndn, nup = decode(got)
            print(f"{name}: 0x{cur:08x} -> 0x{got:08x}   "
                  f"drvdn {odn}->{ndn}, drvup {oup}->{nup}")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
