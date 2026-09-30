#!/usr/bin/env python3
"""LED 배선/바이트순서 확인용 테스트.

순서대로: 전체 빨강 -> 전체 초록 -> 전체 파랑 -> LED 하나씩 흰색 -> 끄기.
화면에 출력되는 색 이름과 실제로 보이는 색이 다르면 order= 를 바꿔야 한다.
"""

import sys
import time

from apa102 import APA102

ORDER = sys.argv[1] if len(sys.argv) > 1 else "bgr"


def main():
    with APA102(num_led=3, bus=0, device=0, order=ORDER) as leds:
        for name, rgb in [("RED", (255, 0, 0)), ("GREEN", (0, 255, 0)), ("BLUE", (0, 0, 255))]:
            print(f"전체 {name} -- 실제로 무슨 색으로 보이나요?")
            leds.fill(*rgb, brightness=10)
            leds.show()
            time.sleep(2)

        for i in range(3):
            print(f"LED {i} 만 흰색")
            leds.fill(0, 0, 0, 0)
            leds.set_pixel(i, 255, 255, 255, brightness=10)
            leds.show()
            time.sleep(1)

        print("끄기")


if __name__ == "__main__":
    main()
