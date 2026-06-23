#!/usr/bin/env bash
# =============================================================================
#  Neuroom Chamber - New Raspberry Pi setup script
#  Target : Raspberry Pi 4 + Raspberry Pi OS (64-bit) "Trixie"
#  Repo   : https://github.com/Moo-75/Neuroom_chamber
#
#  실행:
#     chmod +x setup_new_pi.sh
#     ./setup_new_pi.sh
#  실행 후 반드시 재부팅:  sudo reboot
#
#  ※ Phase 0(Imager에서 사용자명 / WiFi KR / 시간대 Asia-Seoul)와
#    Phase 2(디스플레이 180도 회전)는 GUI/수동이라 이 스크립트에 없음.
#    SETUP.md 참고.
# =============================================================================
set -euo pipefail

REPO="https://github.com/Moo-75/Neuroom_chamber.git"
CLONE_DIR="${HOME}/Desktop/Neuroom_chamber"

echo "================================================================"
echo " Neuroom Chamber 새 Pi 세팅 시작"
echo "================================================================"

# -----------------------------------------------------------------------------
# 1) 필수 라이브러리
#    GPIO: Pi 4에서는 classic RPi.GPIO 사용 (rpi-lgpio 아님!)
#      - 이 코드는 같은 poke 입력핀을 sensor_worker(자식)와 Task(메인) 두
#        프로세스에서 동시에 setup 한다. classic RPi.GPIO(/dev/gpiomem mmap)는
#        비배타적이라 허용되지만, rpi-lgpio(gpiochip)는 배타적 점유라
#        'GPIO busy' 에러가 난다. → Pi 4에서는 classic RPi.GPIO가 정답.
#      - rpi-lgpio 는 Pi 5로 갈 때만 의미가 있으며, 그 경우 멀티프로세스
#        핀 공유 구조를 코드에서 손봐야 한다.
#    나머지: pygame(디스플레이) / opencv(USB웹캠) / serial(Arduino) / pytz / numpy
# -----------------------------------------------------------------------------
echo ""
echo "[1/7] 라이브러리 설치 ..."
sudo apt update
sudo apt install -y git python3-dev \
        python3-pygame python3-opencv \
        python3-serial python3-pytz python3-numpy

# classic RPi.GPIO (rpi-lgpio 가 깔려 있으면 충돌하므로 먼저 제거)
sudo apt remove -y python3-rpi-lgpio 2>/dev/null || true
pip3 install RPi.GPIO --break-system-packages
echo "  - RPi.GPIO 동작 확인:"
python3 -c "import RPi.GPIO as G; G.setwarnings(False); G.setmode(G.BCM); \
G.setup(4,G.IN,pull_up_down=G.PUD_DOWN); G.setup(4,G.IN,pull_up_down=G.PUD_DOWN); \
print('    OK, BCM4 input =', G.input(4)); G.cleanup()" \
  || echo "  ! RPi.GPIO 초기화 실패 → SETUP.md 트러블슈팅 참고"

# -----------------------------------------------------------------------------
# 2) 하드웨어 그룹 권한 (GPIO / 시리얼 / 카메라 / I2C / SPI / 오디오 / 입력)
# -----------------------------------------------------------------------------
echo ""
echo "[2/7] 사용자(${USER}) 그룹 권한 추가 ..."
sudo usermod -aG gpio,dialout,video,i2c,spi,audio,input "${USER}"

# -----------------------------------------------------------------------------
# 3) Arduino(USB) 시리얼 → /dev/arduino 심볼릭 링크 udev 규칙
#    Arduino Uno R3 = VID 2341 / PID 0043.
#    ENV{ID_MM_DEVICE_IGNORE}="1" 로 ModemManager 간섭도 동시에 차단.
#    ! 새 Arduino를 쓸 때마다 link_arduino.sh 로 자동 감지/링크하는 것을 권장.
#      (이 규칙은 VID/PID 매칭 — 같은 모델이면 그대로 동작, 다른 모델/클론이면
#       link_arduino.sh 재실행)
# -----------------------------------------------------------------------------
echo ""
echo "[3/7] Arduino udev 규칙(/dev/arduino) 설치 ..."
sudo tee /etc/udev/rules.d/99-arduino.rules >/dev/null <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="0043", SYMLINK+="arduino", MODE="0666", ENV{ID_MM_DEVICE_IGNORE}="1"
EOF
sudo udevadm control --reload && sudo udevadm trigger

# -----------------------------------------------------------------------------
# 4) ModemManager 비활성화 (새로 꽂힌 /dev/ttyACM* 를 모뎀으로 오인해 점유 방지)
# -----------------------------------------------------------------------------
echo ""
echo "[4/7] ModemManager 비활성화 ..."
sudo systemctl disable --now ModemManager 2>/dev/null || true

# -----------------------------------------------------------------------------
# 5) 시스템 인터페이스 설정 (raspi-config 비대화형)
#    - 시리얼 하드웨어 ON  : enable_uart=1
#    - 시리얼 로그인 콘솔 ON: PuTTY 시리얼 접속(GPIO14/15 -> USB-TTL -> COM)
#    - 데스크톱 자동로그인  : X 서버(:0)가 떠 있어야 시리얼 콘솔에서
#                            DISPLAY=:0 로 pygame을 화면에 그릴 수 있음
#    - 시간대 / WiFi 국가   : Imager에서 이미 했으면 중복이라도 무해
# -----------------------------------------------------------------------------
echo ""
echo "[5/7] 시리얼 콘솔 / 자동로그인 / 시간대 / WiFi 국가 설정 ..."
if sudo raspi-config nonint do_serial_hw 0 2>/dev/null && \
   sudo raspi-config nonint do_serial_cons 0 2>/dev/null ; then
    echo "  - 시리얼(하드웨어+콘솔) 활성화 완료"
else
    echo "  - 신형 함수 없음 → 구형 do_serial 로 대체"
    sudo raspi-config nonint do_serial 0 || true
fi
sudo raspi-config nonint do_boot_behaviour B4 || true   # 데스크톱 자동로그인
sudo raspi-config nonint do_change_timezone Asia/Seoul || true
sudo raspi-config nonint do_wifi_country KR || true

# -----------------------------------------------------------------------------
# 6) DISPLAY 영속화
#    이 시스템은 시리얼 콘솔(tty)에서 maintemp.py 를 실행하고, pygame은
#    데스크톱 X 서버(:0)에 그린다. 콘솔 로그인마다 DISPLAY 가 비어 있으므로
#    ~/.bashrc 에 박아 매번 입력할 필요가 없게 한다.
# -----------------------------------------------------------------------------
echo ""
echo "[6/7] DISPLAY=:0 영속화 (~/.bashrc) ..."
if ! grep -q '^export DISPLAY=:0' "${HOME}/.bashrc" 2>/dev/null; then
    {
        echo ''
        echo '# Neuroom Chamber: 시리얼 콘솔에서 pygame을 데스크톱 화면(:0)에 출력'
        echo 'export DISPLAY=:0'
    } >> "${HOME}/.bashrc"
    echo "  - 추가됨"
else
    echo "  - 이미 설정돼 있음"
fi

# -----------------------------------------------------------------------------
# 7) 챔버 코드 클론(또는 갱신)
# -----------------------------------------------------------------------------
echo ""
echo "[7/7] 챔버 코드 받기 -> ${CLONE_DIR}"
mkdir -p "$(dirname "${CLONE_DIR}")"
if [ -d "${CLONE_DIR}/.git" ]; then
    git -C "${CLONE_DIR}" pull --ff-only || true
else
    git clone "${REPO}" "${CLONE_DIR}"
fi

echo ""
echo "================================================================"
echo " 완료. 이제 재부팅하세요:   sudo reboot"
echo ""
echo " 재부팅 후 남은 수동 작업(SETUP.md Phase 2):"
echo "   - 디스플레이 180도 회전 (Screen Configuration 또는 cmdline video=...)"
echo "   - 새 Arduino면 link_arduino.sh 실행해 /dev/arduino 재링크"
echo ""
echo " 검증 (시리얼 콘솔에서; DISPLAY=:0 는 .bashrc 로 자동 설정됨):"
echo "   ls -l /dev/arduino   &&   cd ${CLONE_DIR}"
echo "   python3 test_GPIO.py ; python3 maze.py ; python3 maintemp.py"
echo "================================================================"
