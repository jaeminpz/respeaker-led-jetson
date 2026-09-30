#!/bin/bash
# ReSpeaker 2-Mics Pi HAT v2.0 — LED 검수 런처 (USB 에서 바로 실행)
#
# 왜 확장자가 .bat 인가
# ---------------------
# 도스 배치파일이 아니다. 내용은 그냥 bash 스크립트다.
# USB 가 FAT32/exFAT 로 포맷되어 있으면 리눅스가 `showexec` 옵션으로 마운트하는데,
# 이때 실행 권한을 받는 건 .bat / .exe / .com 뿐이다. .sh 는 chmod +x 를 해도
# 조용히 무시되어 "허가 거부" 가 난다. .exe 는 도스 실행파일로 인식되어 파일
# 관리자가 실행을 거부한다. 그래서 남는 선택지가 .bat 이고, 내용이 셰밴으로
# 시작하므로 MIME 은 application/x-shellscript 로 잡힌다.
#
# 쓰는 법
# -------
#   파일 관리자에서: 이 파일을 오른쪽 클릭 -> "프로그램으로 실행"
#   터미널에서:      bash LED테스트.bat
#
# 하는 일
# -------
#   1. 파일 관리자에서 눌렀으면 터미널 창을 열어 그 안에서 다시 실행한다
#      (진행 상황과 실패 사유를 봐야 하므로)
#   2. root 권한으로 올린다 (/dev/mem 으로 padctl 을 고쳐야 한다)
#   3. USB 는 실행 권한이 제한되므로 검사 파일을 /tmp 로 복사해서 돌린다
#   4. 끝나면 결과를 남기고 Enter 를 기다린다 (창이 바로 닫히지 않게)

set -u

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
HERE="$(dirname "$SELF")"
TITLE="ReSpeaker LED 검수"

# --- 1단계: 터미널 확보 -------------------------------------------------------
# 파일 관리자에서 누르면 표준입출력이 터미널이 아니다. 그대로 돌리면 진행 상황도
# sudo 암호 입력도 안 보이므로, 터미널 창을 열어 그 안에서 자신을 다시 실행한다.
if [ "${1:-}" != "--in-terminal" ] && [ "${1:-}" != "--as-root" ]; then
    if [ ! -t 0 ] || [ ! -t 1 ]; then
        if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
            echo "터미널도 화면도 없다. 터미널에서 'bash \"$SELF\"' 로 실행해라." >&2
            exit 1
        fi
        for T in gnome-terminal xfce4-terminal mate-terminal konsole xterm; do
            command -v "$T" >/dev/null 2>&1 || continue
            case "$T" in
                gnome-terminal)
                    exec "$T" --title="$TITLE" -- bash "$SELF" --in-terminal ;;
                xterm)
                    exec "$T" -T "$TITLE" -e bash "$SELF" --in-terminal ;;
                *)
                    exec "$T" -e "bash '$SELF' --in-terminal" ;;
            esac
        done
        # 터미널 에뮬레이터가 하나도 없다 — 최소한 알리기라도 한다
        if command -v zenity >/dev/null 2>&1; then
            zenity --error --title="$TITLE" \
                   --text="터미널 프로그램을 찾지 못했다.\n터미널에서 실행해라:\n\nbash \"$SELF\"" 2>/dev/null
        fi
        exit 1
    fi
fi

echo "======================================"
echo " $TITLE"
echo "======================================"
echo

# --- 2단계: root 권한 ---------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    echo "root 권한이 필요하다 — /dev/mem 으로 padctl 레지스터를 고쳐야 한다."
    echo "암호를 물으면 이 기기의 사용자 암호를 넣어라."
    echo
    exec sudo -- bash "$SELF" --as-root
fi

# --- 3단계: USB 밖으로 복사해서 실행 ------------------------------------------
# FAT/exFAT USB 는 파이썬 파일에 실행 권한을 주지 못하고, noexec 로 마운트되는
# 경우도 있다. 로컬 디스크로 옮겨서 돌리면 어느 쪽이든 걸리지 않는다.
if [ ! -d "$HERE/files" ]; then
    echo "오류: $HERE/files 폴더가 없다. USB 내용이 온전한지 확인해라." >&2
    echo
    read -rsp "창을 닫으려면 Enter..." _
    exit 1
fi

WORK="$(mktemp -d /tmp/ledtest.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
cp "$HERE"/files/*.py "$WORK"/ || {
    echo "오류: 파일 복사 실패" >&2
    read -rsp "창을 닫으려면 Enter..." _
    exit 1
}

# --- 4단계: 검수 --------------------------------------------------------------
python3 "$WORK/led_test.py" --confirm
RC=$?

echo
case $RC in
    0) echo "==> 통과. LED 정상." ;;
    *) echo "==> 실패 (코드 $RC). 위 메시지의 조치 힌트를 보라." ;;
esac
echo
echo "다른 것도 해보려면 터미널에서:"
echo "  sudo python3 $HERE/files/led_controller.py demo     # 상태 표시 9종"
echo "  sudo python3 $HERE/files/respeaker_led.py demo      # 단순 색 확인"
echo
read -rsp "창을 닫으려면 Enter..." _
echo
exit $RC
