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

# 데이터 이전 대상 리눅스 서버 (migrate_to_server.py 무입력 전송용). 필요시 수정.
SERVER_TARGET="user@10.140.5.118"
SERVER_PORT="6022"
SERVER_DEST="/data/Siheon_chamber_data"
SERVER_PASSWORD="Dsng1830"

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
echo "[1/10] 라이브러리 설치 ..."
sudo apt update
sudo apt install -y git python3-dev \
        python3-pygame python3-opencv \
        python3-serial python3-pytz python3-numpy \
        ffmpeg v4l-utils \
        gstreamer1.0-tools gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly gstreamer1.0-libav

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
echo "[2/10] 사용자(${USER}) 그룹 권한 추가 ..."
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
echo "[3/10] Arduino udev 규칙(/dev/arduino) 설치 ..."
sudo tee /etc/udev/rules.d/99-arduino.rules >/dev/null <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{idProduct}=="0043", SYMLINK+="arduino", MODE="0666", ENV{ID_MM_DEVICE_IGNORE}="1"
EOF
sudo udevadm control --reload && sudo udevadm trigger

# -----------------------------------------------------------------------------
# 4) ModemManager 비활성화 (새로 꽂힌 /dev/ttyACM* 를 모뎀으로 오인해 점유 방지)
# -----------------------------------------------------------------------------
echo ""
echo "[4/10] ModemManager 비활성화 ..."
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
echo "[5/10] 시리얼 콘솔 / 자동로그인 / 시간대 / WiFi 국가 설정 ..."
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
echo "[6/10] DISPLAY=:0 영속화 (~/.bashrc) ..."
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
#      "N초 멈추면 숨김 / 움직이면 표시"를 구현한다. task/터치를 안 쓰는 동안은
#      계속 숨겨진 상태가 유지된다.
#    참고: https://github.com/labwc/labwc/discussions/3190
#
#    주의:
#    - labwc는 시스템(/etc/xdg/labwc/autostart)과 사용자 autostart를 '둘 다' 실행한다.
#      사용자 autostart를 시스템 것 복사로 만들면 패널(wf-panel-pi)이 두 번 떠서
#      작업표시줄이 두 줄이 된다. → 사용자 autostart에는 'swayidle 줄만' 넣는다.
#    - rc.xml은 사용자 것이 있으면 그걸 쓴다(RPi 기본은 루트가 <openbox_config>이고
#      <keyboard> 섹션이 없을 수 있음). 그래서 <keyboard>가 없으면 닫는 루트 태그
#      (</openbox_config> 또는 </labwc_config>) 앞에 <keyboard> 블록째로 넣는다.
# -----------------------------------------------------------------------------
echo ""
echo "[7/10] 마우스 커서 자동 숨김(labwc HideCursor + swayidle) 설정 ..."
sudo apt remove -y unclutter unclutter-xfixes xdotool 2>/dev/null || true  # X11 잔재 제거
sudo apt install -y swayidle wtype

mkdir -p "${HOME}/.config/labwc"
RC_XML="${HOME}/.config/labwc/rc.xml"
AUTOSTART="${HOME}/.config/labwc/autostart"

# --- HideCursor 키바인드(Alt+Super+h)를 rc.xml에 추가 ---
# rc.xml이 없으면 최소 유효 파일 생성(이미 있으면 사용자 설정 보존)
if [ ! -f "${RC_XML}" ]; then
    cat > "${RC_XML}" <<'EOF'
<?xml version="1.0"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
</openbox_config>
EOF
fi

if grep -q 'HideCursor' "${RC_XML}"; then
    echo "  - HideCursor 키바인드 이미 있음"
elif grep -q '<keyboard>' "${RC_XML}"; then
    # 기존 <keyboard> 섹션 안에 keybind 삽입
    sed -i '0,/<keyboard>/s##<keyboard>\n    <keybind key="A-W-h"><action name="HideCursor"/></keybind>#' "${RC_XML}"
    echo "  - HideCursor 키바인드 추가됨(<keyboard> 안에)"
elif grep -q '</openbox_config>' "${RC_XML}"; then
    # <keyboard>가 없으면 닫는 루트 태그 앞에 블록째로 삽입
    sed -i 's#</openbox_config>#  <keyboard>\n    <keybind key="A-W-h"><action name="HideCursor"/></keybind>\n  </keyboard>\n</openbox_config>#' "${RC_XML}"
    echo "  - HideCursor 키바인드 추가됨(<keyboard> 블록 생성)"
elif grep -q '</labwc_config>' "${RC_XML}"; then
    sed -i 's#</labwc_config>#  <keyboard>\n    <keybind key="A-W-h"><action name="HideCursor"/></keybind>\n  </keyboard>\n</labwc_config>#' "${RC_XML}"
    echo "  - HideCursor 키바인드 추가됨(<keyboard> 블록 생성)"
else
    echo "  ! rc.xml 형식을 인식 못해 키바인드 자동 추가 실패 → 수동 추가 필요"
fi

# --- swayidle 자동 실행(2초 멈추면 숨김 / 움직이면 표시) ---
# 사용자 autostart에는 시스템 기본을 복사하지 말 것(패널 중복 방지). 우리 줄만 추가.
touch "${AUTOSTART}"
if grep -q 'swayidle' "${AUTOSTART}"; then
    echo "  - swayidle autostart 이미 있음"
else
    echo "swayidle -w timeout 2 'wtype -M alt -M logo -P h' &" >> "${AUTOSTART}"
    echo "  - swayidle autostart 등록됨 (재부팅 후 적용)"
fi

# -----------------------------------------------------------------------------
# 8) 챔버 코드 클론(또는 갱신)
# -----------------------------------------------------------------------------
echo ""
echo "[8/10] 챔버 코드 받기 -> ${CLONE_DIR}"
mkdir -p "$(dirname "${CLONE_DIR}")"
if [ -d "${CLONE_DIR}/.git" ]; then
    git -C "${CLONE_DIR}" pull --ff-only || true
else
    git clone "${REPO}" "${CLONE_DIR}"
fi

# -----------------------------------------------------------------------------
# 9) 데이터 이전(migrate_to_server.py) 무입력 세팅
#    파이 -> 리눅스 서버로 실험 폴더를 보낼 때 SSH 비밀번호를 매번 입력하지
#    않도록 sshpass 를 설치하고, 서버 접속정보/비밀번호를 ~/.bashrc 환경변수로
#    박는다. migrate_to_server.py 는 CHAMBER_SERVER_* 환경변수를 자동으로 읽는다.
#    ⚠ 비밀번호가 평문으로 ~/.bashrc 에 저장된다(랩 전용 Pi 편의를 위한 선택).
#      더 안전하게 하려면 이 단계 대신 SSH 키(ssh-copy-id -p ${SERVER_PORT})를 쓰면 된다.
# -----------------------------------------------------------------------------
echo ""
echo "[9/10] 데이터 이전(migrate_to_server) 무입력 세팅 ..."
sudo apt install -y sshpass
if grep -q 'CHAMBER_SERVER_PASSWORD' "${HOME}/.bashrc" 2>/dev/null; then
    echo "  - 서버 접속 환경변수 이미 있음"
else
    {
        echo ''
        echo '# Neuroom Chamber: migrate_to_server.py 무입력 전송용 서버 설정'
        echo "export CHAMBER_SERVER_TARGET='${SERVER_TARGET}'"
        echo "export CHAMBER_SERVER_PORT='${SERVER_PORT}'"
        echo "export CHAMBER_SERVER_DEST='${SERVER_DEST}'"
        echo "export CHAMBER_SERVER_PASSWORD='${SERVER_PASSWORD}'"
    } >> "${HOME}/.bashrc"
    echo "  - 서버 접속 환경변수 추가됨 (재로그인 후 적용)"
fi

# -----------------------------------------------------------------------------
# 10) USB 카메라(uvcvideo) 안정화 옵션
#     저가 UVC 카메라에서 간헐적으로 나는 V4L2 select() timeout(프레임 끊김)을
#     완화한다.
#       quirks=128(0x80, FIX_BANDWIDTH): USB 대역폭 과요청 교정 (timeout 주원인)
#       nodrop=1: 불완전 프레임을 버리지 않고 넘겨 read()가 멈추지 않게
#       timeout=5000: 스트리밍 제어 타임아웃(ms)
# -----------------------------------------------------------------------------
echo ""
echo "[10/10] USB 카메라(uvcvideo) 안정화 옵션 ..."
sudo tee /etc/modprobe.d/uvcvideo.conf >/dev/null <<'EOF'
options uvcvideo quirks=128 nodrop=1 timeout=5000
EOF
sudo modprobe -r uvcvideo 2>/dev/null || true
sudo modprobe uvcvideo 2>/dev/null || true
echo "  - /etc/modprobe.d/uvcvideo.conf 설치됨 (재부팅 후 확실히 적용)"

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
