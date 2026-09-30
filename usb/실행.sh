#!/bin/bash
# FAT USB 에서는 이 파일이 실행 권한을 못 받는다. 터미널 전용 진입점이다.
exec bash "$(dirname "$(readlink -f "$0")")/LED테스트.bat" "$@"
