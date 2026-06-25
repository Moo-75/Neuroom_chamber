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
echo "[1/8] 라이브러리 설치 ..."
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
echo "[2/8] 사용자(${USER}) 그룹 권한 추가 ..."
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
echo "[3/8] Arduino udev 규칙(/dev/arduino) 설치 ..."
sudo tee /etc/udev/rules.d/99-arduino.rules >/dev/null <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="0043", SYMLINK+="arduino", MODE="0666", ENV{ID_MM_DEVICE_IGNORE}="1"
EOF
sudo udevadm control --reload && sudo udevadm trigger

# -----------------------------------------------------------------------------
# 4) ModemManager 비활성화 (새로 꽂힌 /dev/ttyACM* 를 모뎀으로 오인해 점유 방지)
# -----------------------------------------------------------------------------
echo ""
echo "[4/8] ModemManager 비활성화 ..."
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
echo "[5/8] 시리얼 콘솔 / 자동로그인 / 시간대 / WiFi 국가 설정 ..."
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
echo "[6/8] DISPLAY=:0 영속화 (~/.bashrc) ..."
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
# 7) 마우스 커서 자동 숨김 (labwc / Wayland)
#    이 시스템은 Raspberry Pi Connect 화면 공유 때문에 Wayland(labwc)를 쓴다.
#    Wayland에서는 pygame.mouse.set_visible(False) / unclutter / xdotool 같은
#    X11 방식이 모두 무시된다(커서를 labwc 컴포지터가 직접 그림).
#    → labwc의 'HideCursor' 액션 + swayidle(idle 감지) + wtype(키 입력 시뮬레이션)으로
#      "N초 멈추면 숨김 / 움직이면 표시"를 구현한다. task 중에는 마우스를 안 쓰므로
#      계속 숨겨진 상태가 유지된다.
#    참고: https://github.com/labwc/labwc/discussions/3190
# -----------------------------------------------------------------------------
echo ""
echo "[7/8] 마우스 커서 자동 숨김(labwc HideCursor + swayidle) 설정 ..."
sudo apt remove -y unclutter unclutter-xfixes xdotool 2>/dev/null || true  # X11 잔재 제거
sudo apt install -y swayidle wtype

# labwc 사용자 설정 준비(기본 설정을 복사해 패널/단축키가 깨지지 않게)
mkdir -p "${HOME}/.config/labwc"
[ -f "${HOME}/.config/labwc/rc.xml" ]    || cp /etc/xdg/labwc/rc.xml    "${HOME}/.config/labwc/rc.xml" 2>/dev/null || true
[ -f "${HOME}/.config/labwc/autostart" ] || cp /etc/xdg/labwc/autostart "${HOME}/.config/labwc/autostart" 2>/dev/null || true
touch "${HOME}/.config/labwc/rc.xml" "${HOME}/.config/labwc/autostart"

# HideCursor 키바인드(Alt+Super+h)를 rc.xml의 <keyboard> 안에 추가
if grep -q 'HideCursor' "${HOME}/.config/labwc/rc.xml"; then
    echo "  - HideCursor 키바인드 이미 있음"
elif grep -q '<keyboard>' "${HOME}/.config/labwc/rc.xml"; then
    sed -i '0,/<keyboard>/s//<keyboard>\n    <keybind key="A-W-h"><action name="HideCursor"\/><\/keybind>/' "${HOME}/.config/labwc/rc.xml"
    echo "  - HideCursor 키바인드 추가됨"
else
    echo "  ! rc.xml 에 <keyboard> 섹션이 없어 키바인드 자동 추가 실패 → 수동 추가 필요"
fi

# swayidle 자동 실행(2초 멈추면 숨김 / 움직이면 표시)
if grep -q 'swayidle' "${HOME}/.config/labwc/autostart"; then
    echo "  - swayidle autostart 이미 있음"
else
    echo "swayidle -w timeout 2 'wtype -M alt -M logo -P h' &" >> "${HOME}/.config/labwc/autostart"
    echo "  - swayidle autostart 등록됨 (재부팅 후 적용)"
fi

# -----------------------------------------------------------------------------
# 8) 챔버 코드 클론(또는 갱신)
# -----------------------------------------------------------------------------
echo ""
echo "[8/8] 챔버 코드 받기 -> ${CLONE_DIR}"
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
