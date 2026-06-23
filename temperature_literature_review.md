# C57BL/6 마우스 바닥판 온도: 문헌 기반 정리

## 📚 검색된 문헌 요약

### 🌡️ Thermoneutral Zone (TNZ, 열중성대)
| 파라미터 | 값 | 출처 |
|----------|-----|------|
| **TNZ 범위** | 29-31°C | NIH |
| **정밀 TNZ 포인트** | 28.91 ± 0.15°C | Diabetes Journals |
| **선호 온도 (24h 평균)** | 27.7-28.6°C | NIH |
| **휴식 시 선호 온도** | 30-32°C | NIH, PLOS |
| **활동 시 선호 온도** | ~26°C | NIH |

> **Key Point**: C57BL/6 마우스는 **29-31°C**에서 가장 편안하며, 표준 실험실 온도(20-22°C)는 만성적인 cold stress를 유발함

---

### 🔥 Hot (Heat) Aversion Thresholds
| 온도 | 반응 | 출처 |
|------|------|------|
| ~**44.5°C** | Nociceptive threshold 시작점 (increasing temp hot plate) | NIH |
| ~**50°C** | 일반적인 paw withdrawal 발생 | Frontiers |
| **52-55°C** | 표준 hot plate test 온도 (paw licking, flicking) | Conduct Science |
| **>55°C** | 조직 손상 위험 | 다수 |

> **Key Point**: **44-45°C**부터 통증 반응이 시작되고, **50°C** 이상은 명확한 nociceptive (pain) response 유발

---

### ❄️ Cold Aversion Thresholds
| 온도 | 반응 | 출처 |
|------|------|------|
| **<20°C** | Cold avoidance behavior 시작 | J Neurosci |
| **10-15°C** | Sensory neuron activation, aversive but tolerable | ACS, NIH |
| **5°C** | Strong avoidance (wild-type mice cross once, then avoid) | ResearchGate |
| **0-2°C** | Noxious cold, pain-level response | Campden Instruments |

> **Key Point**: **15-20°C** 이하부터 불쾌감 시작, **10°C** 이하는 강한 회피 반응, **5°C** 이하는 noxious cold

---

## 🎯 프로젝트 적용 온도 권장안

### 바닥판 조건 고려사항
- **재질**: 알루미늄 + 테이프 한 겹
- **열전달**: 테이프로 인해 직접 금속 접촉보다 열전달이 약간 완화됨
- **체감 온도**: 실제 plate 온도보다 약간 완화될 수 있음 (테이프 절연 효과)

### 권장 온도 범위

| 구분 | 권장 온도 | 근거 |
|------|-----------|------|
| **Optimal range** | **28-32°C** | TNZ 기반, 쾌적 구간 |
| **Choice 한계 (lower)** | **20-22°C** | Cold avoidance 시작점 바로 위 |
| **Choice 한계 (upper)** | **38-40°C** | Nociceptive threshold (44°C) 미만 |
| **Attenuation 한계 (lower)** | **10-12°C** | Strong cold avoidance but not noxious |
| **Attenuation 한계 (upper)** | **45-48°C** | Near nociceptive threshold |

---

## ⚠️ 안전 고려사항

### 절대 사용 금지 온도
| 구분 | 온도 | 위험 |
|------|------|------|
| **극저온** | <5°C | Tissue damage, frostbite risk |
| **극고온** | >50°C | 화상 위험, 윤리적 문제 |

### 장시간 노출 주의
| 온도 범위 | 최대 권장 노출 시간 |
|-----------|---------------------|
| 10-15°C | 2-3분 이내 |
| 40-45°C | 1-2분 이내 |
| 45-48°C | 30초 이내 (attenuation으로만 도달) |

---

## 📊 기존 설정 vs 권장 설정 비교

| 항목 | 현재 설정 | 문헌 기반 권장 | 비고 |
|------|-----------|----------------|------|
| Optimal range | 27-33°C | **28-32°C** | TNZ와 더 일치 |
| Choice 한계 (lower) | 23°C | **20-22°C** | Cold avoidance 직전 |
| Choice 한계 (upper) | 37°C | **38-40°C** | 약간 확장 가능 |
| Attenuation 한계 (lower) | 10°C | **10-12°C** | 유지 또는 약간 상향 |
| Attenuation 한계 (upper) | 50°C | **45-48°C** | 안전을 위해 하향 권장 |

---

## 🔬 추가 고려사항

### 성별 차이
- **암컷 마우스**가 수컷보다 약간 더 따뜻한 온도 선호
- 실험 시 성별 균형 고려 필요

### 시간대별 차이
- **Light phase (휴식)**: 30-32°C 선호
- **Dark phase (활동)**: 26°C 정도로 낮은 온도 선호
- 실험 시간대 일정하게 유지 권장

### 테이프 효과
- 알루미늄 직접 접촉 대비 열전달 지연 예상
- 실제 역치는 순수 hot/cold plate 연구보다 1-2°C 여유 있을 수 있음
- **권장**: 실제 마우스 반응 관찰하며 미세 조정

---

## 📖 주요 참고 문헌 Source

1. NIH (National Institutes of Health) - 다수 논문
2. Diabetes Journals - Thermoneutral point 정밀 측정
3. Frontiers in Neuroscience - Hot plate test
4. J Neuroscience - Cold avoidance behavior
5. Conduct Science - Hot/Cold plate protocols
6. PLOS ONE - Thermal preference studies

---

## 🔄 Attenuation 속도 설정 근거

### 마우스 온도 감지 역치
| 항목 | 값 | 출처 |
|------|-----|------|
| **최소 감지 가능 변화** | ~0.5°C | NIH, BioRxiv |
| **확실한 구별 역치** | 2.5-4°C | NIH |
| **Subliminal 변화 속도 (인간)** | <0.5°C/분 | ResearchGate |

### Oscillation Masking 효과
현재 챔버는 Bang-bang control로 인해 **±1°C oscillation** 발생:

> **"Sensory adaptation improves signal-to-noise ratio for rapid changes, while filtering out constant or slowly changing temperatures."** (MDC Berlin)

- Thermoreceptor는 **상대적 변화**를 감지하도록 설계됨
- Oscillation에 적응하면 **느린 drift 감지가 더 어려워짐**
- Slow drift가 oscillation noise에 **마스킹**됨

### 현재 Attenuation 설정 (유지)
| Stage | 속도 | 10분간 변화 | Oscillation 대비 | 결정 |
|-------|------|-------------|------------------|------|
| **Stage 2** | 0.02°C/초 (1.2°C/분) | 12°C | >> ±1°C | ✅ 유지 |
| **Stage 3** | 0.03°C/초 (1.8°C/분) | 18°C | >> ±1°C | ✅ 유지 |

> **결론**: 챔버의 oscillation이 attenuation drift를 부분적으로 마스킹하므로, 현재 설정 속도가 적절함

