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

> Arduino 펌웨어는 라즈베리파이 OS와 무관하다. Arduino 보드를 그대로 옮기거나 새 보드에 같은 펌웨어를 올리면 그쪽은 변경 없이 동작한다.
> Pi ↔ Arduino 는 USB 시리얼(`/dev/arduino`) + 텍스트 프로토콜(`GET_TEMP`, `SET_TEMP,xx`, `START`, `STOP`)로만 연결된다.

---

## 핵심: 새 OS에서 깨지는 3가지 (전부 환경 문제, 코드 문제 아님)

마이그레이션에서 실제로 막혔던 지점과 결론:

1. **GPIO — classic `RPi.GPIO` 써야 함 (rpi-lgpio 아님)**
   이 코드는 같은 poke 입력핀을 `sensor_worker`(자식 프로세스)와 `Task`(메인)에서 **동시에 setup** 한다.
   - classic `RPi.GPIO`(`/dev/gpiomem` mmap): 비배타적 → 허용 (원래 코드가 기대하는 동작).
   - `rpi-lgpio`(gpiochip): **배타적 점유** → 두 번째 claim에서 `lgpio.error: 'GPIO busy'`.
   - **Pi 4는 classic `RPi.GPIO`가 정답.** (rpi-lgpio는 Pi 5로 갈 때만 의미. 그땐 멀티프로세스 핀 공유 구조를 코드에서 손봐야 함.)

2. **Arduino 온도 "제어"가 안 됨 (읽기는 됨)**
   제어는 Arduino `is_running==true`일 때만 동작하는데, Pi는 `START`를 seton 경로에서만 보냈다. 구 OS는 시리얼 open 때 Arduino가 자동리셋되어 `is_running=true`로 시작했지만, 새 OS는 `dtr=False`를 제대로 존중해 리셋이 안 일어나 직전 `STOP` 상태가 남는다.
   → **수정:** `maintemp.py`의 `peltier_worker`에서 모듈 생성 직후 `peltier.start_control()`를 명시적으로 호출(자동리셋에 의존 X).

3. **pygame이 화면에 안 뜸**
   시리얼 콘솔(tty)에서 실행하면 `DISPLAY`가 비어 있어 X 서버(:0)에 못 그린다.
   → **해결:** 데스크톱 자동로그인으로 X 서버(:0)를 띄워두고, 콘솔에서 `export DISPLAY=:0`. (`~/.bashrc`에 박아 영속화 — setup 스크립트가 처리.)

> **GPIO/TTL 자체는 추가 시스템 설정이 없다.** 핀 모드/방향/풀다운은 전부 코드에서 런타임에 `GPIO.setmode/setup`으로 잡으며, 구 이미지의 `config.txt`에도 GPIO 관련 dtoverlay가 없었다.

---

## 시리얼이 두 종류라는 점에 주의

이 시스템에는 **서로 다른 두 개의 시리얼**이 있다.

1. **Arduino 시리얼 (USB)** — 펠티어 제어용. `/dev/arduino`(udev 심볼릭 링크).
2. **PuTTY 콘솔 시리얼 (GPIO UART)** — `GPIO14/15 → USB-TTL 어댑터 → Windows PC COM → PuTTY`. 챔버 Pi에 로그인하는 시리얼 콘솔. `enable_uart=1` + `console=serial0,115200` 필요. **실험은 이 콘솔에서 실행한다.**

---

## Phase 0 — SD카드 굽기 (PC의 Raspberry Pi Imager)

1. OS 선택: **Raspberry Pi OS (64-bit)** — Trixie (권장 항목)
2. "다음" → **사용자 지정(⚙️ Edit Settings)** 에서 미리 설정 (시간 절약):
   - 사용자 이름 / 비밀번호 (레거시 `neuroom.py`에 `/home/pi` 절대경로가 있으니 `pi`를 쓰면 가장 안전)
   - 호스트네임 (원하는 이름)
   - **Wi-Fi**: SSID/비밀번호, **국가 KR**
   - **로케일**: 시간대 **Asia/Seoul**
   - **SSH 사용** 체크 (권장)
3. 굽기 → 새 Pi에 삽입 → **디스플레이 · Arduino(USB) · USB-TTL 어댑터를 모두 연결한 상태로** 부팅

---

## Phase 1 — 자동 셋업 스크립트

부팅 후 시리얼 콘솔/SSH에서:

```bash
cd ~/Desktop                 # 또는 스크립트를 둔 위치
sed -i 's/\r$//' setup_new_pi.sh   # (Windows 편집 시 CRLF 정리)
chmod +x setup_new_pi.sh
./setup_new_pi.sh
sudo reboot
```

---

## Video Recording Notes

- `maintemp.py` now saves video as `Video_{mouse_id}_{session}_{timestamp}.mp4`.
- `task_temp.py` opens the USB camera through V4L2 on Raspberry Pi/Linux to avoid intermittent OpenCV GStreamer capture read failures.
- H.264/GStreamer MP4 is preferred. If that writer is unavailable, OpenCV `mp4v` MP4 is used.
- Transient `cap.read()` failures are retried. Repeated unrecoverable camera failures stop video recording only; the behavioral task continues.
- Useful checks on the Raspberry Pi:
  - `gst-inspect-1.0 x264enc`
  - `v4l2-ctl --list-formats-ext`
  - `ffprobe Video_*.mp4`

스크립트가 처리하는 것:

1. **라이브러리**: `git python3-dev python3-pygame python3-opencv python3-serial python3-pytz python3-numpy ffmpeg v4l-utils gstreamer1.0-*` + **classic `RPi.GPIO`** (`pip3 install RPi.GPIO`, rpi-lgpio는 제거)
2. **그룹 권한**: `gpio,dialout,video,i2c,spi,audio,input`
3. **Arduino udev 규칙**(`/dev/arduino`, VID/PID 매칭) + ModemManager 무시 플래그
4. **ModemManager 비활성화**
5. **시리얼 콘솔(PuTTY) + 데스크톱 자동로그인 + 시간대 + WiFi 국가**
6. **`DISPLAY=:0` 영속화** (`~/.bashrc`)
7. **챔버 코드 클론** → `~/Desktop/Neuroom_chamber`

---

## Phase 2 — GUI/수동 설정 (재부팅 후)

### 2-1. 디스플레이 180° 회전
구 이미지의 `lcd_rotate=2`는 Trixie(KMS)에서 안 먹는다. 둘 중 하나:

- **(쉬움)** 데스크톱 메뉴 → **기본 설정 → Screen Configuration** → 화면 우클릭 → **Orientation → inverted (180°)** → 저장
- **(확실)** 커널 파라미터:
  ```bash
  kmsprint | grep -i connected          # 커넥터 이름 (보통 DSI-1)
  sudo nano /boot/firmware/cmdline.txt   # 같은 한 줄 끝에 한 칸 띄고 추가:
  #   video=DSI-1:800x480@60,rotate=180
  ```

### 2-2. 새 Arduino 연결 (매번)
새 Arduino를 쓸 때마다 **`link_arduino.sh`** 실행 → 연결된 보드를 자동 감지해 `/dev/arduino` 링크를 다시 만든다. (펌웨어는 Arduino IDE로 `peltier_operating_system.ino`를 올려둘 것.)
```bash
chmod +x link_arduino.sh
./link_arduino.sh
```

### 2-3. (참고) pygame이 화면에 안 뜰 때
- 실험은 **시리얼 콘솔에서 실행**하고, pygame은 데스크톱 X 서버(:0)에 그려진다. `DISPLAY=:0`이 setup 스크립트로 `~/.bashrc`에 박혀 있으므로 보통 자동 동작한다.
- 그래도 안 뜨면 콘솔에서 수동 확인: `export DISPLAY=:0` 후 재실행.
- 데스크톱 세션(자동로그인)이 떠 있어야 `:0`가 존재한다.

---

## Phase 3 — 검증

```bash
cd ~/Desktop/Neuroom_chamber
# DISPLAY=:0 는 .bashrc 로 자동 설정됨 (새 콘솔이면 적용됨)

# A) Arduino USB 시리얼
lsusb | grep -i 2341          # Arduino 인식 (정품 Uno면 2341)
ls -l /dev/arduino            # 심볼릭 링크 생성 확인 (핵심)
python3 test_GPIO.py          # 온도 읽기/제어

# B) GPIO 입출력 (센서/LED/모터/디스플레이)
python3 maze.py               # 메뉴에서 Sensor / Reward / Display 개별 테스트

# C) 전체 통합
python3 maintemp.py
#   json file? -> test.json
#   protocol?  -> 예: OBT
#   mouse id / session 입력 후 task 선택
```

- **PuTTY 시리얼 콘솔**(Windows): Serial → 어댑터 COM 번호, **115200 / 8 / N / 1 / Flow control None**
- **카메라**: USB 웹캠 연결 상태에서 `maintemp.py` 실행 시 "connecting camera" 후 녹화 시작 확인

---

## 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `lgpio.error: 'GPIO busy'` | rpi-lgpio의 배타적 점유 → **classic RPi.GPIO로 교체**: `sudo apt remove -y python3-rpi-lgpio && pip3 install RPi.GPIO --break-system-packages` |
| 온도 읽기는 되는데 **제어 안 됨** | Arduino `is_running` 미설정 → `peltier_worker`에서 `peltier.start_control()` 호출 확인 (위 핵심 #2). 임시 확인: 실행 직전 Arduino 리셋/재꽂기 |
| **pygame 화면 안 뜸** | 시리얼 콘솔에 `DISPLAY` 없음 → `export DISPLAY=:0` (또는 `.bashrc` 반영 후 새 콘솔). 데스크톱 자동로그인 켜져 있어야 함 |
| `pygame.error: x11 not available` | `SDL_VIDEODRIVER=x11`이 강제됐는데 X 없음 → `unset SDL_VIDEODRIVER` 후 `DISPLAY=:0`로 실행 |
| `/dev/arduino` 안 생김 | `link_arduino.sh` 실행. 그래도 안 되면 `udevadm info -q property -n /dev/ttyACM0`로 VID/PID 확인 |
| `RPi.GPIO` import/초기화 실패 | `pip3 install --upgrade RPi.GPIO --break-system-packages`. (Pi 4에서 보통 정상) |
| 영상 녹화 코덱 경고/실패 | `setup_new_pi.sh`로 `ffmpeg`, `v4l-utils`, GStreamer H.264 패키지를 설치. `task_temp.py`는 H.264 MP4를 우선 사용하고 실패 시 `mp4v` MP4로 폴백한다. 반복적인 카메라 읽기 실패는 영상 녹화만 중단하고 행동 과제는 계속 진행한다. |
| 시리얼 권한 거부 | dialout 그룹 적용 위해 재부팅했는지 `groups`로 확인 |
| `./setup_new_pi.sh: bad interpreter` | CRLF 줄바꿈 → `sed -i 's/\r$//' setup_new_pi.sh` |

---

## 재현해야 할 시스템 설정 요약

코드 복사·라이브러리 설치 외에 OS 레벨에서 재현이 필요한 것:

1. **GPIO 라이브러리** → classic `RPi.GPIO` (rpi-lgpio 제거)
2. **USB Arduino 시리얼** → `99-arduino.rules` udev (+ dialout, ModemManager 회피) / 새 보드는 `link_arduino.sh`
3. **PuTTY GPIO 시리얼 콘솔** → `enable_uart=1` + `console=serial0,115200` (= raspi-config 시리얼 켜기)
4. **데스크톱 자동로그인 + `DISPLAY=:0` 영속화** (pygame 화면용)
5. **디스플레이 180° 회전** → `lcd_rotate=2` 대신 KMS 회전 방식
6. **타임존 Asia/Seoul + WiFi regdom KR**
7. **사용자 그룹** gpio · dialout · video · i2c · spi · audio · input

(1·2·3·4·6·7은 `setup_new_pi.sh`가 처리, 5는 Phase 2 수동 / 새 Arduino는 `link_arduino.sh`)

---

## 코드 수정 메모

`maintemp.py` `peltier_worker()` — 모듈 생성 직후 `start_control()` 추가 (온도 제어가 항상 켜진 상태로 시작하도록):

```python
def peltier_worker(command_queue, result_queue, shared_data, dict_lock, stop_event):
    peltier = maze.Peltier_module()
    peltier.use_attenuation_func = True
    peltier.start_control()              # ← 추가: is_running=true 보장 (자동리셋 비의존)
    pygame.time.wait(100)
    ...
```
