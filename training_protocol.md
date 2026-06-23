# Temperature-Based Operant Learning Training Protocol (Revised v2)

## 목표 Task 개요
- **Left choice** → 온도 상승 (Hot)
- **Right choice** → 온도 하강 (Cool)
- **No choice** → Attenuation 적용 (state에 따라 방향 결정)
- **목표**: Mouse가 optimal 온도 range를 찾아 유지하도록 학습
- **Reward**: 온도 변화만 사용 (Water reward 없음)

---

## 🌡️ 온도 설정

| 구분 | 하한 | 상한 | 설명 |
|------|------|------|------|
| **Optimal range** | 27°C | 33°C | 목표 온도 범위 (쾌적 구간) |
| **Choice 한계** | 23°C | 37°C | Choice로 도달 가능한 범위 (non-aversive) |
| **Attenuation 한계** | 10°C | 50°C | Attenuation으로 도달 가능한 범위 |

---

## ⏱️ 타이밍 파라미터 (문헌 기반)

### 공통 파라미터

| 파라미터 | 값 | 근거 |
|----------|-----|------|
| **세션 시간** | 30-40분 | 마우스 피로 방지, 일반적 operant 세션 |
| **최대 trial 수** | 60-80 trials/세션 | 학습 효율과 피로 균형 |
| **온도 변화 속도** | ~0.5°C/sec | Peltier 모듈 현실적 속도 (2°C 변화 시 ~4초) |

### Trial 구조 타이밍

| 구성 요소 | Stage 1 | Stage 2 | Stage 3 | 근거 |
|-----------|---------|---------|---------|------|
| **Trial cue** | 1초 | 1초 | 1초 | 밸브 소리 또는 tone |
| **Choice window** | 30초 | 20초 | 15초 | 점진적 단축으로 난이도 증가 |
| **온도 변화 시간** | 10초 | 10초 | 10초 | 2°C 변화 기준, Peltier 속도 고려 |
| **온도 유지 시간** (No choice 시) | 10초 | 10초 | 10초 | 변화 시간과 동일 |
| **ITI (Inter-Trial Interval)** | 10초 | 8초 | 5초 | 점진적 단축, 문헌 권장 5-15초 |
| **1 Trial 총 시간** | ~51초 | ~39초 | ~31초 | - |

---

## Training Stage 설계 (3단계)

> **Note**: Habituation은 별도로 진행

---

### 📋 Stage 1: Poke-Temperature Association (3-5일)
**목표**: Nose poke가 온도 변화를 유발한다는 것을 학습

#### 온도 설정
| 항목 | 설정 |
|------|------|
| **시작 온도** | Choice 한계 경계 (23°C 또는 37°C, 세션마다 교대) |
| **온도 변화량** | +/-2°C per choice |
| **Attenuation** | ❌ OFF |
| **State** | ❌ 없음 |

#### 타이밍 설정
| 항목 | 설정 |
|------|------|
| **Trial cue** | 1초 (밸브 소리) |
| **Choice window** | 30초 |
| **온도 변화 시간** | 10초 (poke 후) |
| **온도 유지 시간** | 10초 (no choice 시) |
| **ITI** | 10초 (고정) |
| **세션 시간** | 30분 또는 40 trials |

#### Trial 흐름도
```
[Trial Cue 1s]
    ↓
[Choice Window 30s]
    ├── [Poke 발생] → [Sound Feedback] → [온도 변화 10s] → [ITI 10s] → Next Trial
    └── [No Poke] → [온도 유지 10s] → [ITI 10s] → Next Trial
```

#### Trial 타임라인 예시
```
0s      1s              31s     41s     51s
│──Cue──│──Choice Window──│──Temp──│──ITI──│
         ↑ Poke 가능      ↑ 변화   ↑ 대기
```

**성공 기준**: 
- 불쾌 온도에서 올바른 방향 선택률 70% 이상
- Trial당 poke 비율 60% 이상

---

### 📋 Stage 2: Attenuation + State Introduction (5-7일)
**목표**: Attenuation과 State 개념 학습

#### 온도 설정
| 항목 | 설정 |
|------|------|
| **시작 온도** | Optimal 경계 근처 (25°C 또는 35°C, 세션마다 교대) |
| **온도 변화량** | +/-2°C per choice |
| **Attenuation** | ✅ ON (0.02°C/초 = 1.2°C/분) |
| **State** | ✅ 있음 (Hot state / Cold state) |
| **State cue** | 화면 색상 (Hot=붉은색, Cold=푸른색) |

#### 타이밍 설정
| 항목 | 설정 |
|------|------|
| **Trial cue** | 1초 |
| **Choice window** | 20초 |
| **온도 변화 시간** | 10초 |
| **온도 유지 시간** | 10초 (+ Attenuation 적용) |
| **ITI** | 8초 (고정) |
| **세션 시간** | 35분 또는 50 trials |

#### Attenuation 계산
```
Attenuation 속도: 0.02°C/초 = 1.2°C/분

No choice 1회당 Attenuation:
= 0.02 × (온도 유지 10초 + ITI 8초)
= 0.02 × 18초
= 0.36°C

연속 No choice 시:
- 5회: 1.8°C 변화
- 10회: 3.6°C 변화
- Optimal(30°C) → Choice 한계(23°C): 약 20회 no choice 필요
```

#### Trial 흐름도
```
[Trial Cue 1s] + [State Cue 표시]
    ↓
[Choice Window 20s]
    ├── [Poke 발생] → [Sound] → [온도 변화 10s] → [ITI 8s] → Next Trial
    └── [No Poke] → [온도 유지 + Attenuation 10s] → [ITI 8s] → Next Trial
```

**State 전환 규칙**:
- Optimal 밖 → Optimal 안으로 진입 시 State 전환 발생
- 뜨거운 쪽(>33°C)에서 진입: Cold state 확률 70%
- 차가운 쪽(<27°C)에서 진입: Hot state 확률 70%

**성공 기준**:
- State cue에 따른 선택 정확도 70% 이상
- Attenuation 이탈 후 3 trials 내 복귀

---

### 📋 Stage 3: Full Task - No Cue (지속)
**목표**: State cue 없이 Full task 수행

#### 온도 설정
| 항목 | 설정 |
|------|------|
| **시작 온도** | Optimal 중앙 (30°C) |
| **온도 변화량** | +/-2°C per choice |
| **Attenuation** | ✅ ON (0.03°C/초 = 1.8°C/분) |
| **State** | ✅ 있음 |
| **State cue** | ❌ 제거 |

#### 타이밍 설정
| 항목 | 설정 |
|------|------|
| **Trial cue** | 1초 |
| **Choice window** | 15초 |
| **온도 변화 시간** | 10초 |
| **온도 유지 시간** | 10초 (+ Attenuation 적용) |
| **ITI** | 5초 (고정) |
| **세션 시간** | 40분 또는 60 trials |

#### Attenuation 계산
```
Attenuation 속도: 0.03°C/초 = 1.8°C/분

No choice 1회당 Attenuation:
= 0.03 × (온도 유지 10초 + ITI 5초)
= 0.03 × 15초
= 0.45°C

연속 No choice 시:
- 5회: 2.25°C 변화
- 10회: 4.5°C 변화
- Optimal(30°C) → Choice 한계(23°C): 약 16회 no choice 필요
```

#### Trial 흐름도
```
[Trial Cue 1s]  ← State cue 없음
    ↓
[Choice Window 15s]
    ├── [Poke 발생] → [Sound] → [온도 변화 10s] → [ITI 5s] → Next Trial
    └── [No Poke] → [온도 유지 + Attenuation 10s] → [ITI 5s] → Next Trial
```

**성공 기준**:
- Optimal range 체류 비율 60% 이상
- State 전환 후 5 trials 내 적응

---

## 📊 Training Schedule 요약

| Stage | 이름 | 기간 | Trial/세션 | 1 Trial 시간 | Attenuation | State |
|-------|------|------|------------|--------------|-------------|-------|
| 1 | Poke-Temp Association | 3-5일 | ~40 | ~51초 | ❌ | ❌ |
| 2 | Attenuation + State | 5-7일 | ~50 | ~39초 | ✅ 1.2°C/분 | ✅ cue |
| 3 | Full Task | 지속 | ~60 | ~31초 | ✅ 1.8°C/분 | ✅ no cue |

**총 예상 기간**: 8-12일 + 지속

---

## Stage 진급 기준

```python
STAGE_CRITERIA = {
    "stage_1_to_2": {
        "min_days": 3,
        "poke_rate": 0.60,               # Trial당 poke 비율
        "correct_direction_rate": 0.70,  # 불쾌 온도에서 올바른 방향
    },
    "stage_2_to_3": {
        "min_days": 5,
        "state_cue_accuracy": 0.70,      # State cue에 따른 선택 정확도
        "recovery_trials": 3,            # Attenuation 이탈 후 복귀까지 trials
    },
}
```

---

## 🔧 구현 시 고려사항

### Peltier 모듈 속도 제한
- **현실적 변화 속도**: ~0.5°C/초 (2°C 변화 시 ~4초)
- **온도 변화 시간 10초**는 이 속도를 고려한 여유 시간
- 목표 온도 도달 전 다음 trial이 시작하지 않도록 주의

### 시간 유연성
- Choice window 내에서 poke가 발생하면 즉시 온도 변화 시작
- Choice window 전체를 기다릴 필요 없음

### 데이터 기록
- 매 trial: 시작온도, 선택(L/R/None), 반응시간, 종료온도
- 매 세션: State 전환 횟수, Optimal 체류 비율
- 실시간: 온도 로그 (0.5초 간격)

---

## 추가 고려사항

### 🌡️ 실험 환경
- **방 온도**: 22-25°C
- **습도**: 30-70%
- **실험 시간**: Light phase (male B6 기준)

### 🔊 Cue 사용
| Stage | Trial Cue | State Cue | Choice Feedback |
|-------|-----------|-----------|-----------------|
| 1 | 밸브 소리 1초 | ❌ | Optional sound |
| 2 | 밸브 소리 1초 | 화면 색상 | Optional sound |
| 3 | 밸브 소리 1초 | ❌ | Optional sound |
