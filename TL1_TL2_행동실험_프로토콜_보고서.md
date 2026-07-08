# TL1 / TL2 행동실험 프로토콜 상세 보고서

> 대상 코드: `task_temp.py` — `Task._run_temperature_lift()`, `Task.TL1()`, `Task.TL2()`, `TL1_Task`, `TL2_Task`
> 실행 진입점: `maintemp.py` (task == "TL1" / "TL2")
> 작성 기준일: 2026-07-08

---

## 0. 한눈에 보기

TL1과 TL2는 **Temperature Lift(온도 상승) 훈련 과제**로, 최종 목표 과제(Left=온도상승 / Right=온도하강 / No-choice=attenuation, optimal range 유지)를 학습시키기 위한 **초기 단계 훈련 과제**이다. 두 과제 모두 다음의 단순한 행동–결과 수반성(action–outcome contingency)을 마우스에게 각인시키는 것을 목적으로 한다.

- **왼쪽 코찌르기(left nose-poke) → 온도 상승(warming)**
- **선택 없음(no-choice) → 온도 하강(cooling)**
- 연속 drift(자동 온도 변화)는 **꺼져 있음** — 온도는 오직 trial 이벤트(선택/무선택) 시점에만 계단식으로 변한다.

즉 이 단계에서는 아직 Right choice, attenuation, state, optimal range 개념은 도입하지 않고, "**내가 왼쪽을 찌르면 따뜻해지고, 가만히 있으면 추워진다**"는 가장 기본적인 인과관계만 학습시킨다. 시작 온도가 낮게(기본 10°C) 설정되어 있어 마우스가 따뜻함을 얻기 위해 능동적으로 poke하도록 유도한다.

TL1과 TL2는 **동일한 trial 구조(이벤트 순서)** 를 공유하며, 차이는 **① 온도 변화량(outcome 크기)**, **② 시간 창(choice/feedback window) 길이**, **③ 변화량의 무작위성 유무**뿐이다.

| 구분 | TL1 | TL2 |
|---|---|---|
| 성격 | 1차 훈련 (크고 명확한 피드백) | 2차 훈련 (작고 다양한 피드백) |
| Left poke 결과 | **+5.0°C 고정** | **+3.0 / +3.5 / +4.0°C 중 무작위** |
| No-choice 결과 | **−5.0°C 고정** | **−1.5 / −2.0 / −2.5°C 중 무작위** |
| Choice window | 사용자 입력, 기본 **20초** | **10초** |
| Feedback window | **40초** | **20초** |
| 무작위 bag | 미사용(고정값) | 20-trial balanced random bag |
| 세션 시간 | 기본 60분 | 기본 60분 |

---

## 1. 목적 및 설계 의도

### 1.1 왜 Left-only인가
최종 목표 과제는 좌/우 양방향 선택이지만, 처음부터 양방향을 학습시키기는 어렵다. TL 단계에서는 **한쪽(왼쪽) 방향의 poke만 유효**하게 만들어, "poke = 온도 변화 발생"이라는 핵심 연합을 먼저 확립한다. 오른쪽 등 다른 센서의 poke는 모두 무시된다.

### 1.2 왜 No-choice에 냉각을 거는가
단순히 poke만 보상(온도 상승)하면, 마우스가 목표 온도에 도달한 뒤 더 이상 행동할 이유가 사라진다. TL은 **무선택 시 온도를 떨어뜨림**으로써, 마우스가 따뜻함을 유지하려면 **계속 능동적으로 poke해야 하는** 구조를 만든다. 이는 최종 과제의 attenuation 개념(가만히 있으면 온도가 optimal에서 벗어남)을 단순화한 형태로 볼 수 있다.

### 1.3 왜 continuous drift를 껐는가
온도가 시간에 따라 연속적으로 흐르지 않고 **trial 이벤트 시점에만 계단식**으로 바뀐다. 덕분에 "언제 온도가, 얼마나, 왜 바뀌었는가"가 이벤트 로그에 명확히 기록되어, 후속 행동 모델링(현재 온도 / 결과 이력 / 무선택 이력의 분리)이 훨씬 쉬워진다. (보고서 8장 참조)

### 1.4 TL1 → TL2 진행 논리
- **TL1**: 변화량이 ±5°C로 크고 고정되어 있어, poke의 결과가 즉각적·명확하다. 연합 형성 초기에 적합.
- **TL2**: 변화량이 작아지고(+3~4 / −1.5~−2.5) 무작위화되어, 마우스가 단일 결과값을 외우는 대신 "poke는 대체로 따뜻하게, 무선택은 대체로 차갑게 만든다"는 일반화된 수반성을 학습하도록 한다. 또한 시간 창이 짧아져(20s→10s, 40s→20s) 과제 템포가 빨라지고 trial 수가 늘어난다.

---

## 2. Trial 구조 (공통)

TL1과 TL2는 아래 이벤트 순서를 동일하게 따르며, 구간 길이만 다르다. 한 세션은 `task_time`(기본 60분)이 경과할 때까지 trial을 반복한다.

```
[SessionStart 로깅]
      │
      ▼
┌──────────────────────── Trial N ────────────────────────┐
│ (1) Trial 시작                                            │
│     - target 온도 hold, attenuation=0 재설정              │
│     - reward.give(0.1) → 솔레노이드 밸브 0.1초 소리 cue    │
│     - "TrialStart" 로깅                                   │
│                                                          │
│ (2) Choice window  (TL1: 기본 20s / TL2: 10s)             │
│     - 화면 좌하단에 'cold' cue를 1초 주기로 점멸           │
│     - 왼쪽 센서 rising edge(0→1) 감지 = 유효 선택          │
│     - 코가 들어온 즉시 처리, 코 빼기를 기다리지 않음        │
│     - 다른 poke는 무시                                    │
│                                                          │
│   ├─ [Left poke 발생] ────────────────────────────────┐  │
│   │   - reward.give(0.1) → poke 확인 소리 cue          │  │
│   │   - bump = warming 값 선택 (TL1:+5 / TL2:bag)      │  │
│   │   - new_target = base + bump, [10,40]°C clamp     │  │
│   │   - SET_TEMP(new_target), 흰 화면 표시             │  │
│   │   - "LeftPoke" 로깅 (Choice=l, RT 기록)            │  │
│   │                                                    │  │
│   └─ [무선택 / window 만료] ──────────────────────────┐  │
│       - 흰 화면 표시                                    │  │
│       - drop = cooling 값 선택 (TL1:−5 / TL2:bag)      │  │
│       - new_target = base − drop, [10,40]°C clamp     │  │
│       - SET_TEMP(new_target)                          │  │
│       - "NoChoice" 로깅 (Choice=n)                    │  │
│                                                        │  │
│ (3) Feedback window  (TL1: 40s / TL2: 20s)            │  │
│     - 흰 화면 유지, 모든 poke 무시                     │  │
│     - 선택된 결과 target 온도를 유지                    │  │
│     - "FeedbackEnd" 로깅                              │  │
│                                                        │  │
│ (4) Trial 종료 → 세션 시간 남으면 Trial N+1 시작        │  │
└──────────────────────────────────────────────────────┘  │
      │
      ▼
[SessionEnd 로깅, 초록 화면 표시, stop_event set]
```

### 2.1 주요 세부 동작 (코드 기준)

- **Rising edge 감지**: `s[1] == 1 and prev_left == 0` 조건으로, 센서값이 0→1로 바뀌는 순간(코가 포트에 막 진입)을 유효 poke로 인정한다. 계속 눌러져 있는 상태는 중복 인정하지 않는다. 폴링 간격은 `SENSOR_POLL_WAIT_MS`.
- **즉시 처리**: poke가 감지되면 코를 빼기를 기다리지 않고 즉시 소리 cue·온도 변경·화면 전환을 시작한다.
- **Cue 점멸**: choice window 동안 `screen.display_temp_cue("cold", bottom_gap_fraction=0.2)`와 `screen.show()`를 `blink_period`(1.0초) 주기로 번갈아 표시해, 좌하단 영역에서 cue가 깜빡이게 한다. (진입 직후 첫 프레임은 강제 ON)
- **세션 종료 인터럽트**: choice window 및 feedback window 루프 내부에서 매번 전체 세션 시간 초과를 검사하며, 초과 시 `session_done=True`로 즉시 세션을 종료한다.
- **화면 상태**: choice 처리 후 흰 화면(`state=["w"]`), 세션 종료 시 초록 화면(`state=["g"]`).

---

## 3. 온도 규칙

### 3.1 공통 설정

| 항목 | 값 |
|---|---|
| 기본 시작 목표 온도 (`start_temp`) | **10.0°C** |
| 온도 clamp 범위 (`temp_min`~`temp_max`) | **10.0 ~ 40.0°C** |
| Continuous drift | **OFF** (attenuation을 매 이벤트마다 0으로 재설정) |
| Choice window 중 목표 온도 | 선택이 발생하기 전까지 **일정하게 유지** |

- **시작 온도 override**: 과제 시작 전 `maintemp.py`에서 온도 set-on을 사용했다면, `shared_data["initial_target_temp"]` 값을 세션 시작 온도로 사용한다. 이 값이 10~40°C를 벗어나면 clamp되며, clamp 사실이 콘솔에 출력된다.
- **결과 온도 계산의 기준(base)**: `outcome_base_temp()`는 **현재 target_temp를 우선** 기준으로 삼고(없으면 현재 측정 온도, 그것도 없으면 start_temp) 여기에 outcome delta를 더한다. 즉 온도 변화는 실측 온도가 아니라 **명령된 목표 온도(setpoint)에 누적**된다.

### 3.2 TL1 온도 규칙

```
Left poke  → outcome_delta = +5.0°C  (고정)
No-choice  → outcome_delta = −5.0°C  (고정)
new_target = clamp(base_temp + outcome_delta, 10.0, 40.0)
```

`bump_choices=(5.0,)`, `no_choice_drop_choices=(5.0,)` — 단일 값이므로 무작위성이 없다.

### 3.3 TL2 온도 규칙

```
Left poke  → outcome_delta ∈ {+3.0, +3.5, +4.0}°C  (20-trial balanced random bag)
No-choice  → outcome_delta ∈ {−1.5, −2.0, −2.5}°C  (20-trial balanced random bag)
new_target = clamp(base_temp ± outcome_delta, 10.0, 40.0)
```

`bump_choices=(3.0, 3.5, 4.0)`, `no_choice_drop_choices=(1.5, 2.0, 2.5)`. 두 계열 모두 아래 4장의 balanced random bag 규칙으로 값을 추출한다.

> **설계상 비대칭**: TL2에서 warming(+3~4°C, 평균 +3.5)이 cooling(−1.5~−2.5°C, 평균 −2.0)보다 크다. 이는 poke의 보상 효과를 무선택 처벌보다 크게 유지하면서도, 무선택 냉각을 작게 나눠 마우스가 온도 하강을 서서히 경험하도록 한다.

---

## 4. Balanced Random Bag (TL2 전용)

TL2의 온도 변화량은 매 trial 완전 무작위가 아니라, **블록 단위로 균형 잡힌 무작위(balanced random)** 방식으로 추출된다. 구현은 `make_balanced_bag()`.

### 4.1 규칙
- 블록 크기 = 20 trial (`bump_balance_block`, `no_choice_balance_block` 모두 20).
- 후보값 3개를 20개 슬롯에 배분: `20 // 3 = 6`개씩 기본 배정, 나머지 `20 % 3 = 2`개를 추가 배정 → **7 / 7 / 6** 분포.
- 추가 배정을 받는 값(=7회)은 블록마다 `block_index`에 따라 **순환(rotate)** 한다. 따라서 장기적으로는 세 값이 고르게 나온다.
- 각 블록 내부는 `random.shuffle()`로 섞여, trial 단위 예측 불가능성을 유지한다.
- bag이 비면 다음 블록을 생성한다 (`next_bump()`, `next_no_choice_drop()`).

### 4.2 값
```
Warming bag  : [+3.0, +3.5, +4.0]
Cooling bag  : [−1.5, −2.0, −2.5]   (코드상 drop = {1.5, 2.0, 2.5}, 적용 시 부호 반전)
```

warming bag과 cooling bag은 **독립적인 블록 인덱스**를 가지며 서로 별개로 진행된다.

> TL1은 후보값이 1개뿐이라 bag 로직을 거치더라도 항상 동일값(±5.0)이 나온다. 실질적으로 무작위성이 없다.

---

## 5. 소리 cue (Sound cue)

`reward.give()`가 이 셋업에서는 **실제 보상(물) 전달이 아니라 솔레노이드 밸브가 열리는 소리 cue**로만 쓰인다. 두 시점에서 각각 **0.1초** 길이로 호출된다.

1. **Trial 시작 cue** — 매 trial 시작 시 (`trial_start_reward=0.1`)
2. **Left poke 확인 cue** — 유효한 왼쪽 poke가 감지된 직후 (`choice_reward=0.1`)

즉 마우스는 "trial이 시작됐다"는 신호와 "내 poke가 인정됐다"는 신호를 동일한 밸브 소리로 받는다.

---

## 6. 기록 데이터 (Data export)

### 6.1 Trial-wise CSV
파일명: `TD_{mouse_id}_{session}_{timestamp}_trial-wise.csv`

컬럼:
```
mouseID, Day, Task, Trial, Time, Event,
Current_Temp, Target_Temp, Choice, Bump, RT,
OutcomeDelta, OutcomeTarget_Temp
```

#### 이벤트 종류

| Event | 의미 | 주요 필드 |
|---|---|---|
| `SessionStart` | 세션 시작 마커 | 세션 시작 시점의 현재/목표 온도 |
| `TrialStart` | Trial 시작 및 choice window 준비 | trial 시작 시점의 현재/목표 온도 |
| `LeftPoke` | 유효 왼쪽 poke 발생 | `Choice=l`, `Bump=+warming`, `RT`, `OutcomeDelta`, `OutcomeTarget_Temp` |
| `NoChoice` | choice window 내 유효 poke 없음 | `Choice=n`, `Bump=−cooling`, `OutcomeDelta`, `OutcomeTarget_Temp` |
| `FeedbackEnd` | feedback window 종료 | 최종 현재/목표 온도 + 결과 필드 반복 |
| `SessionEnd` | 세션 종료 마커 | 최종 현재/목표 온도 |

#### 필드 설명
- **RT (반응시간)**: `poke_t − (choice window 시작시각)`. choice window 시작부터 poke까지의 초. LeftPoke에서만 기록된다.
- **Bump**: 하위 호환용 필드. 선택된 결과 변화량(LeftPoke=양수, NoChoice=음수)을 기록.
- **OutcomeDelta**: 부호를 포함한 결과 변화량을 명시적으로 기록 (분석 명확성 확보용).
- **OutcomeTarget_Temp**: 해당 결과를 적용한 뒤 명령된 목표 온도.
- **Current_Temp / Target_Temp**: 이벤트 시점의 실측 평균 온도 / 명령 목표 온도.

### 6.2 온도 CSV (500ms 간격)
`data_export.write_every_n_miliseconds()`로 기록. 파일명: `Temperature_{mouse_id}_{session}_{timestamp}.csv`
```
time(s), target_temp, sensor_temp1, sensor_temp2, average_temp,
control_mode, control_rate, predicted_temp, delta_pwm, ref_pwm
```
TL1/TL2 분석에서 핵심은 `time(s)`, `target_temp`, `sensor_temp1`, `sensor_temp2`, `average_temp`.

### 6.3 센서 CSV (10ms 간격)
`sensor_worker()`로 기록. 파일명: `SensorTime_{mouse_id}_{session}_{timestamp}.csv`
```
time(s), sensor_reward, sensor_left, sensor_center, sensor_right
```

### 6.4 비디오 / 프레임 시각
카메라 초기화 성공 시 `Video_{mouse_id}_{session}_{timestamp}.mp4` 저장. 각 프레임에 세션 시간과 현재 평균 온도가 오버레이된다. H.264/GStreamer MP4 우선, 불가 시 OpenCV `mp4v` 사용. USB 카메라의 일시적 read 실패는 재시도하며 행동 과제를 중단시키지 않는다.

---

## 7. 실행 방법 (`maintemp.py`)

```
[TL1] left-only, choice +5°C, no-choice −5°C (choice window 입력, 기본 20s / feedback 40s)
[TL2] left-only, choice +3/3.5/4°C, no-choice −1.5/−2/−2.5°C (choice 10s / feedback 20s)
```

- **TL1** 선택 시 `prompt_positive_float("TL1 choice window seconds (Enter=20): ", 20.0)`로 choice window를 사용자에게 입력받는다(Enter 시 20초). 이후 `TL1_Task(..., choice_window=choice_window)`로 인스턴스화되어 `self.TL1(choice_window=...)` 실행.
- **TL2** 선택 시 인자 없이 `TL2_Task(...)` → `self.TL2()` 실행 (choice/feedback window가 10/20초로 고정).

클래스 구조:
- `TL1_Task(Task)`: choice_window를 생성자 인자로 받아 저장, `task()`에서 `self.TL1(choice_window=self.choice_window)` 호출.
- `TL2_Task(Task)`: `task()`에서 `self.TL2()` 호출.
- 두 클래스 모두 공통 엔진 `Task._run_temperature_lift()`로 위임.

---

## 8. 분석 시 참고사항

행동 모델링을 위해서는 **trial-wise CSV와 500ms 온도 CSV를 세션 상대 시각(`Time` / `time(s)`)으로 병합**하는 것이 권장된다. trial 데이터는 이벤트와 결과 크기를 제공하고, 온도 CSV는 연속적인 온도 궤적을 제공한다.

권장 파생 변수:
- `phase`: choice window vs feedback window
- `choice_available`: choice window 중 poke 이전 구간에서만 True
- `time_in_choice_window`: choice window 진입 후 경과 시간
- `temperature_bin`: 온도 구간화
- `dTdt`: 온도 변화율
- `previous_choice`, `previous_outcome_delta`: 직전 trial의 선택/결과
- `no_choice_streak`: 연속 무선택 횟수
- `time_since_last_poke`: 마지막 poke 이후 경과 시간

현재 프로토콜은 결과 변화가 명시적 trial 이벤트에서만 발생하므로, 이전의 continuous-drift 버전보다 **현재 온도 / 결과 이력 / 무선택 냉각 이력을 분리**하기 쉬워 모델링 친화적이다.

---

## 9. TL1 vs TL2 요약 비교표

| 파라미터 | TL1 | TL2 |
|---|---|---|
| `bump_choices` (Left poke 결과) | `(5.0,)` → **+5.0°C 고정** | `(3.0, 3.5, 4.0)` → **balanced random** |
| `no_choice_drop_choices` (무선택 결과) | `(5.0,)` → **−5.0°C 고정** | `(1.5, 2.0, 2.5)` → **balanced random** |
| Choice window | 사용자 입력 (기본 20.0s) | **10.0s** |
| Feedback window | **40.0s** (기본값) | **20.0s** |
| Balanced bag block | 미사용(단일값) | 20-trial (7/7/6 순환) |
| 시작 온도 | 10.0°C (또는 set-on override) | 동일 |
| Clamp 범위 | 10~40°C | 동일 |
| 세션 시간 | 60분 (기본) | 60분 (기본) |
| Drift | OFF | OFF |
| 소리 cue | trial 시작 + poke 확인 (각 0.1s) | 동일 |
| 화면 cue | 좌하단 'cold' cue 1s 점멸 | 동일 |

---

## 10. 구현 위치

핵심 구현:
```
task_temp.py
  Task._run_temperature_lift()   # 공통 엔진
  Task.TL1()                     # TL1 파라미터 프리셋
  Task.TL2()                     # TL2 파라미터 프리셋
  TL1_Task                       # TL1 실행 래퍼 클래스
  TL2_Task                       # TL2 실행 래퍼 클래스
```

런타임 dispatch:
```
maintemp.py
  task == "TL1"   # choice window 입력 후 TL1_Task 실행
  task == "TL2"   # TL2_Task 실행
```
