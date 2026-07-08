#!/usr/bin/env bash
# =============================================================================
#  Chamber camera diagnostic
#
#  목적: "같은 코드/RAM/설정인데 어떤 챔버는 카메라 select() timeout이 나고
#         어떤 챔버는 멀쩡하다"의 근본 원인을 추측이 아니라 증거로 규명한다.
#
#  두 가지 모드:
#    snapshot : 정적 하드웨어/설정을 덤프한다.
#               → 잘되는 챔버와 안 되는 챔버에서 각각 실행해 'diff' 로 비교.
#    monitor  : 실험을 돌리면서 (1) 커널 로그(swiotlb/usb), (2) 전원 throttle,
#               (3) 앱 출력(타임스탬프된 [Camera] 로그)을 동시에 파일로 남긴다.
#               → 카메라 실패 시각과 swiotlb/throttle 시각을 초 단위로 대조.
#
#  사용:
#    chmod +x chamber_diag.sh
#    ./chamber_diag.sh snapshot          # 각 챔버에서 1회
#    ./chamber_diag.sh monitor           # 실험하며 (maintemp.py 를 대신 실행)
# =============================================================================
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="$(hostname)"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUTDIR="${HOME}/chamber_diag"
mkdir -p "${OUTDIR}"

snapshot() {
    local f="${OUTDIR}/snapshot_${HOST}_${STAMP}.txt"
    {
        echo "===== HOST ====="; hostname
        echo "===== MODEL ====="; tr -d '\0' < /proc/device-tree/model 2>/dev/null; echo
        echo "===== KERNEL ====="; uname -a
        echo "===== RAM ====="; free -h
        echo "===== GPU/CMA mem ====="
        vcgencmd get_mem arm; vcgencmd get_mem gpu
        echo "===== /proc/cmdline (swiotlb/cma 확인) ====="; cat /proc/cmdline
        echo "===== config.txt ====="
        cat /boot/firmware/config.txt 2>/dev/null || cat /boot/config.txt 2>/dev/null
        echo "===== throttle/volt/temp (지금) ====="
        vcgencmd get_throttled; vcgencmd measure_volts; vcgencmd measure_temp
        echo "===== USB 토폴로지 (카메라가 무엇과 허브를 공유하나) ====="
        lsusb -t; echo; lsusb
        echo "===== 카메라 이름/협상된 포맷 ====="
        cat /sys/class/video4linux/video0/name 2>/dev/null; echo
        v4l2-ctl -d /dev/video0 --all 2>/dev/null
        echo "===== 디스플레이(DRM) 상태/해상도 ====="
        for s in /sys/class/drm/card*-*/status; do
            [ -f "$s" ] && echo "$s = $(cat "$s")"
        done
        for m in /sys/class/drm/card*-*/modes; do
            [ -f "$m" ] && echo "$m -> $(head -1 "$m")"
        done
        echo "===== swiotlb 관련 부팅 로그 ====="
        dmesg | grep -i swiotlb || echo "(swiotlb 로그 없음)"
    } > "${f}" 2>&1
    echo "wrote ${f}"
    echo "→ 잘되는 챔버와 안 되는 챔버에서 각각 실행 후 두 파일을 diff 하세요."
}

monitor() {
    local kern="${OUTDIR}/kern_${HOST}_${STAMP}.log"
    local thr="${OUTDIR}/throttle_${HOST}_${STAMP}.log"
    local app="${OUTDIR}/app_${HOST}_${STAMP}.log"

    echo "로그 파일:"
    echo "  커널   : ${kern}"
    echo "  throttle: ${thr}"
    echo "  앱     : ${app}"
    echo ""

    # (1) 커널 로그를 벽시계 시각(-T)으로 따라가며 저장
    sudo dmesg -wT >> "${kern}" 2>&1 &
    local kpid=$!
    # (2) 2초마다 전원 throttle 상태 기록
    ( while true; do
        echo "$(date '+%F %T') $(vcgencmd get_throttled) volt=$(vcgencmd measure_volts core) temp=$(vcgencmd measure_temp)"
        sleep 2
      done >> "${thr}" 2>&1 ) &
    local tpid=$!

    cleanup() { kill "${kpid}" "${tpid}" 2>/dev/null; echo ""; echo "로거 종료. 로그는 ${OUTDIR} 에 있음."; }
    trap cleanup EXIT INT TERM

    echo ">>> 이제 maintemp.py 를 실행합니다. 평소처럼 프롬프트에 입력하세요."
    echo ">>> 카메라 오류가 나면 그대로 두고 관찰한 뒤 종료(Ctrl-C)하면 됩니다."
    echo ""
    # script: 대화형 입력을 유지하면서 앱 출력(타임스탬프된 [Camera] 로그 포함)을 파일로도 캡처
    cd "${SCRIPT_DIR}"
    script -q -f -c "python3 maintemp.py" "${app}"
}

case "${1:-}" in
    snapshot) snapshot ;;
    monitor)  monitor ;;
    *) echo "usage: $0 {snapshot|monitor}"; exit 1 ;;
esac
