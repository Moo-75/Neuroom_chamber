# Neuroom Chamber — 새 라즈베리파이 세팅 가이드

행동실험 챔버를 구동하던 라즈베리파이를 **새 하드웨어 + 최신 OS**로 옮길 때의 전체 세팅 절차.

- **대상 하드웨어:** Raspberry Pi 4
- **대상 OS:** Raspberry Pi OS (64-bit), Debian **Trixie**
- **카메라:** USB 웹캠

이 챔버는 **두 대의 컴퓨터**로 동작한다. 마이그레이션은 사실상 라즈베리파이 쪽 환경 재구축이다.

| 담당 | 역할 | 코드 |
|---|---|---|
| **라즈베리파이** | nose-poke 센서 입력, LED/모터/TTL 출력, 시각 cue 표시, 영상 녹화, Arduino로 시리얼 명령 | `maze.py`, `maintemp.py`, `task_temp.py`, `data_export.py` |
| **Arduino (Uno R3)** | 펠티어 온도 측정(서미스터 2개) + zone 비례 PWM 제어 | `peltier_operating_system.ino` |

> Arduino 펌웨어는 라즈베리파이 OS와 무관하다. **기존 Arduino 보드를 그대로 옮기면** 그쪽은 변경 없이 동작한다.
> Pi ↔ Arduino 는 USB 시리얼(`/dev/arduino`) + 텍스트 프로토콜(`GET_TEMP`, `SET_TEMP,xx`, `START`, `STOP`)로만 연결된다.

---

## 시리얼이 두 종류라는 점에 주의

새 OS에서 가장 흔히 깨지는 부분이다. 이 시스템에는 **서로 다른 두 개의 시리얼**이 있다.

1. **Arduino 시리얼 (USB)** — 펠티어 제어용. `/dev/arduino`(udev 심볼릭 링크). VID `2341`:PID `0043`.
2. **PuTTY 콘솔 시리얼 (GPIO UART)** — `GPIO14/15 → USB-TTL 어댑터 → Windows PC COM → PuTTY`. 챔버 Pi에 로그인하는 시리얼 콘솔. `enable_uart=1` + `console=serial0,115200` 필요.

둘 다 새 OS에서 따로 설정해야 한다. (둘 다 시스템 설정 문제이지 코드 문제가 아님.)

> **GPIO/TTL 자체는 추가 시스템 설정이 없다.** 핀 모드/방향/풀다운은 전부 코드에서 런타임에 `GPIO.setmode/setup`으로 잡으며, 구 이미지의 `config.txt`에도 GPIO 관련 dtoverlay가 전혀 없었다.

---

## Phase 0 — SD카드 굽기 (PC의 Raspberry Pi Imager)

1. OS 선택: **Raspberry Pi OS (64-bit)** — Trixie (권장 항목)
2. "다음" → **사용자 지정(⚙️ Edit Settings)** 에서 미리 설정 (시간 절약):
   - **사용자 이름: `pi`** ← ⚠️ 반드시 `pi` (코드/경로가 `/home/pi` 기준; 레거시 `neuroom.py`에 절대경로 존재)
   - 비밀번호 설정
   - 호스트네임 (원하는 이름)
   - **Wi-Fi**: SSID/비밀번호, **국가 KR**
   - **로케일**: 시간대 **Asia/Seoul**
   - **SSH 사용** 체크 (권장)
3. 굽기 → 새 Pi에 삽입 → **디스플레이 · Arduino(USB) · USB-TTL 어댑터를 모두 연결한 상태로** 부팅

---

## Phase 1 — 자동 셋업 스크립트

부팅 후 터미널/SSH에서:

```bash
cd ~/Desktop                 # 또는 스크립트를 둔 위치
# (Windows에서 편집해 CRLF가 섞였다면) 줄바꿈 정리:
sed -i 's/\r$//' setup_new_pi.sh
chmod +x setup_new_pi.sh
./setup_new_pi.sh
sudo reboot
```

스크립트가 처리하는 것:

1. **라이브러리**(apt): `git python3-rpi-lgpio python3-pygame python3-opencv python3-serial python3-pytz python3-numpy`
2. **그룹 권한**: `gpio,dialout,video,i2c,spi,audio,input`
3. **Arduino udev 규칙**(`/dev/arduino`) + ModemManager 무시 플래그
4. **ModemManager 비활성화**
5. **시리얼 콘솔(PuTTY) + 데스크톱 자동로그인 + 시간대 + WiFi 국가**
6. **챔버 코드 클론** → `~/Desktop/Neuroom_chamber`

> `python3-rpi-lgpio`가 apt에 없으면: `sudo apt install python3-lgpio` 후
> `pip install rpi-lgpio --break-system-packages` (진짜 `RPi.GPIO`와 공존 금지).

---

## Phase 2 — GUI/수동 설정 (재부팅 후)

### 2-1. 디스플레이 180° 회전  ⚠️ 필요
구 이미지의 `lcd_rotate=2`는 Trixie(KMS)에서 동작하지 않는다. 둘 중 하나:

- **(쉬움)** 데스크톱 메뉴 → **기본 설정 → Screen Configuration** → 화면 우클릭 → **Orientation → inverted (180°)** → 저장
- **(확실)** 커널 파라미터로 고정:
  ```bash
  kmsprint | grep -i connected          # 커넥터 이름 확인 (보통 DSI-1)
  sudo nano /boot/firmware/cmdline.txt   # 같은 한 줄 끝에 한 칸 띄고 추가:
  #   video=DSI-1:800x480@60,rotate=180
  ```

### 2-2. (화면 문제 시에만) Wayland → X11
`python3 maintemp.py` 실행 시 pygame 화면이 검게/어긋나면:
```bash
sudo raspi-config   # Advanced Options → Wayland → X11 → 재부팅
```
정상이면 바꿀 필요 없다.

### 2-3. Arduino 펌웨어 (보드 재사용이면 생략)
기존 Arduino를 그대로 옮기면 할 일 없음. **새 Arduino**라면 Arduino IDE로 `peltier_operating_system.ino` 업로드 후,
`/etc/udev/rules.d/99-arduino.rules`의 `ATTRS{serial}=="14101"`을 새 보드 시리얼번호로 변경.

---

## Phase 3 — 검증

```bash
cd ~/Desktop/Neuroom_chamber

# A) Arduino USB 시리얼
lsusb | grep -i 2341          # Arduino 인식
ls -l /dev/arduino            # 심볼릭 링크 생성 확인 (핵심)
python3 test_GPIO.py          # 온도 읽기/제어

# B) GPIO 입출력 (센서/LED/모터/디스플레이)
python3 maze.py               # 메뉴에서 Sensor / Reward / Display 개별 테스트

# C) 펠티어 PWM 듀티값
python3 set_pwm.py

# D) 전체 통합
python3 maintemp.py
#   json file? -> test.json
#   protocol?  -> 예: OBT
#   mouse id / session 입력 후 task 선택
```

- **PuTTY 시리얼 콘솔**(Windows): PuTTY → Serial → 어댑터 COM 번호,
  **Speed 115200 / Data 8 / Stop 1 / Parity None / Flow control None** → 로그인 프롬프트 확인
- **카메라**: USB 웹캠 연결 상태에서 `maintemp.py` 실행 시 "connecting camera" 후 녹화 시작 확인

---

## 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `/dev/arduino` 안 생김 | `lsusb`엔 보이는데 링크 없으면 시리얼번호 불일치 → `udevadm info -a -n /dev/ttyACM0 \| grep serial`로 실제 값 확인 후 규칙 수정 |
| `python3-rpi-lgpio` 설치 실패 | `sudo apt install python3-lgpio` + `pip install rpi-lgpio --break-system-packages` (RPi.GPIO와 공존 금지) |
| 영상 녹화 코덱 경고/실패 | `task_temp.py`의 `fourcc ... 'DIVX'` → `'MJPG'`로 변경(출력 확장자도 맞춤) |
| pygame 검은 화면 | Phase 2-2: X11 전환 |
| 시리얼 권한 거부 | dialout 그룹 적용 위해 재부팅했는지 `groups`로 확인 |
| `./setup_new_pi.sh: bad interpreter` | CRLF 줄바꿈 → `sed -i 's/\r$//' setup_new_pi.sh` |

---

## 재현해야 할 시스템 설정 요약

코드 복사·라이브러리 설치 외에 OS 레벨에서 재현이 필요한 것:

1. **USB Arduino 시리얼** → `99-arduino.rules` udev (+ dialout, ModemManager 회피)
2. **PuTTY GPIO 시리얼 콘솔** → `enable_uart=1` + `console=serial0,115200` + serial-getty (= raspi-config 시리얼 켜기)
3. **디스플레이 180° 회전** → `lcd_rotate=2` 대신 KMS 회전 방식
4. **데스크톱 자동로그인** (pygame 화면용)
5. **타임존 Asia/Seoul + WiFi regdom KR**
6. **사용자 그룹** gpio · dialout · video · i2c · spi · audio · input

(1·2·4·5·6은 `setup_new_pi.sh`가 처리, 3은 Phase 2 수동)
