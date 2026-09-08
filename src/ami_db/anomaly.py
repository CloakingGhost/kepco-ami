# -*- coding: utf-8 -*-
"""
안전감지 위기 감지(2-7단계, 프로젝트 2순위 기능).

## 두 등급의 정의 - 이게 설계의 출발점이다

  - **위험** = 정상적인 보호장치라면 이미 개입했어야 하는, 설계 안전마진을 벗어난
    상태(KEC 212.3 산업용 배선차단기 규약상 이미 트립됐어야 함) - 즉시 조치 대상.
  - **주의** = 아직 그 정도는 아니지만 사고가 나기에 충분한 조건 - 점검·관리 대상.

  ※ 표현 수정 기록(2026-09-05): 이전엔 "위험 = 실제로 전기사고가 발생한(발생 중인)
  상황"이라고 썼는데, 이건 과장이다. KEC 임계값은 "차단기가 이 조건에서 반드시
  동작해야 한다"는 제품 시험 규격이지 "전선이 지금 탄다"는 물리적 확정 진술이
  아니다(차단기가 실제로 트립하면 그 즉시 전력 공급이 끊겨 AMI에 후속 데이터가
  안 잡히므로, 오히려 이 조건이 "지속 관측된다"는 사실 자체가 보호장치 부재/오작동
  또는 판정 분모 자체의 오류를 시사한다 - A-L-58 사례가 후자의 실제 증거임).
  그래서 "위험"은 "사고 확정"이 아니라 "보호장치 개입 지점을 넘은, 근거 있는
  긴급 점검 트리거"로 이해해야 한다.

## 왜 통계적 이상치를 등급에 직접 매핑하면 안 되는가 (1차 설계 실패)

초기 구현은 Tukey 이상치 판정(1.5xIQR=주의, 3xIQR=위험)을 전체 시간대에 그대로
등급으로 썼다. 그 결과 실측 3개월 x 21개 매장에서 위험 4,111건 / 주의 11,656건이
나왔다 - 평균 50분에 한 번씩 전기사고가 났다는 뜻이 되어 등급 정의와 정면으로
어긋난다.

원인은 명확하다. **"통계적으로 드문 값"과 "물리적으로 위험한 값"은 다른 개념이다.**
냉장고 제상 사이클, 심야 청소, 예약 취사 같은 지극히 정상적인 활동도 그 매장의
평소 야간 분포에서 보면 3xIQR을 쉽게 넘긴다. 통계는 "평소와 다른가"를 답하지
"위험한가"를 답하지 않는다.

## closed_hours 전체를 판정 대상으로 삼아도 안 되는 이유 (2차 설계 실패)

1차 실패를 고치려고 "전기설비 절대 기준(계약전력 대비 비율) + 지속시간"으로
바꾸고, 판정 범위를 `store_operating_status.schedule_status='closed_hours'`
(영업시간표상 문 닫힌 시간) 전체로 잡았다. 그런데 21개 매장 실측 전체를 검증하니
이 범위 자체가 "진짜 빈 매장"이 아닌 슬롯을 다수 포함하고 있었다 - 예를 들어
A-L-80은 매일 09:30~10:30에, A-L-60은 매일 17:15~20:00에 반복적으로 평소보다
전력이 높았는데, 두 시간대 모두 영업시간표상으로는 "닫힌 시간"으로 등록돼 있었다.

**중요: 이걸 "두찜은 준비시간이다", "충북식당은 미등록 저녁영업이다" 식으로
설명하면 안 된다.** 이 21개 매장은 KSIC 업종코드 기반 통계적 근사 매칭으로
AMI 계기에 붙인 것이지(`stores.match_note` 참고), 그 계기 뒤에 실제로 있는
사업장을 확인한 게 아니다. 즉 "두찜"이라는 이름도, 그 매장의 등록 영업시간표도
**이 계기의 진짜 정체와 무관할 수 있다** - 그러니 "왜 그 시간에 전력을 쓰는지"는
추정할 근거도, 추정할 필요도 없다. 우리가 실제로 알 수 있는 유일한 사실은
"이 계기는 매주 이 요일, 이 시각대에 반복적으로 전력을 쓴다"는 것뿐이고, **그
이유가 영업이든 준비든 다른 무엇이든 상관없이 "계기 자신이 뭔가 자체적인 활동을
하고 있다"고만 보면 충분하다.** 등록 영업시간표(schedule_status)도 마찬가지로
이 임의 매칭에 딸려온 값이라 최종 판정 범위의 근거로 쓸 수 없다.

## 3차 설계: 스케줄도, 업종 서사도 배제하고 계기 자신의 데이터만 본다

판정 범위를 정하는 유일한 재료는 **그 계기 자신의 (요일 x 15분슬롯)별 실측
전력 이력**이다. 두 가지를 동시에 확인해야 "진짜 잠잠한 시간"이라고 인정한다 -
낮은 것만으로는 부족하다(어떤 날은 조용하고 어떤 날은 튀는 "들쑥날쑥"한 슬롯도
이미 "뭔가 자체적으로 하고 있다"는 신호이기 때문이다):

  1. **낮음(수준)**: 그 (요일,슬롯)의 3개월 중앙값이 night_baseline
     (`ami_db.synthetic.compute_night_baseline`, 0~5시 median 재사용 - "계기가
     확실히 아무 활동도 없는 시간"의 기존 검증된 기준. 이미
     `store_operating_status.power_status`(active/low 판정)에 쓰이는 것과 동일한
     개념이라 근거 일관성이 있다)의 QUIET_SLOT_MULT배 이내여야 한다.
  2. **안정적(변동)**: 같은 (요일,슬롯)의 IQR이 night_baseline의
     QUIET_STABILITY_MULT배 이내여야 한다 - 중앙값은 낮아도 가끔 크게 튀는
     불안정한 슬롯이면 탈락시킨다(그 자체가 "이따금 자체적인 활동이 있다"는 증거).

  두 조건을 **모두** 만족하는 (요일,슬롯)만 "진짜폐점"(=판정 대상)이다. 영업
  중이든, 매일 반복되는 준비 활동이든, 등록 영업시간표가 틀려서 생긴 미등록
  영업이든 - 이유를 구분하지 않고 전부 "자체적 활동"으로 취급해 판정에서 뺀다.

  `schedule_status='closed_hours'`(영업시간표)는 **값싼 1차 사전 필터로만** 계속
  쓴다(호출부 SQL, 10_detect_anomalies.py) - 명백히 영업 중인 슬롯까지 이 모듈에
  들여보내지 않아 계산량을 줄이는 용도일 뿐, 최종 판정 범위는 위 두 조건("진짜폐점"
  플래그)이 전적으로 정한다.

## 판정 규칙 3종 (위험 1개, 주의 2개)

모든 규칙은 "진짜폐점"으로 판정된 슬롯에서만 평가되고, **(1) 절대적 기준**과
**(2) 그 상태가 유지된 시간**을 함께 본다. 두 갈래로 나뉜다 - 설비가 물리적으로
고장 나는 문제(과부하)와, 빈 매장에 누군가/무언가 있어서는 안 될 전력이 잡히는
문제(침입·방치·오작동)는 서로 다른 위험이라 각각 독립적인 근거로 판정한다.

### (설비 물리 기준) [위험] RULE_DANGER_OVERLOAD

```
recv_kWh x 4 >= contract_power_kw x 1.30   가 60분(슬롯 4개) 연속 지속
```

근거: KEC(한국전기설비규정) 212.3 표 212.3-2 "산업용 배선차단기" 과전류트립
동작특성 - 정격전류 63A 이하 구간 동작전류 1.30배 / 규약시간 60분(기후에너지
환경부공고 제2025-227호, 2026-01-05 시행, PDF p.211에서 직접 확인). 상업용 계기
(usage_purpose='02 상업용')이므로 주택용 표(1.45배)가 아니라 산업용 표를 쓴다.

### (설비 물리 기준) [주의] RULE_CONTINUOUS_LOAD

```
recv_kWh x 4 >= contract_power_kw x 0.80   이 3시간(슬롯 12개) 연속 지속
```

근거: 연속부하(continuous load) 80% 규칙 - NEC 210.20(A) 및 Article 100.
KEC 2026년 개정 전문(1,234쪽)을 전수 검색했지만 '연속부하'/'3시간' 등 대응 표현이
전혀 없음을 확인했다(KEC는 IEC 60364 계보라 이 개념 자체가 없고 식 212.4-1
`IB<=IN<=IZ` 관계식으로 다르게 규정한다) - 그래서 NEC 인용을 그대로 유지한다.

### (매장별 개인화 기준) [주의] RULE_BASELINE_WARN

```
recv_kWh >= baseline_median + WARN_DEVIATION_MULT x baseline_floor   이 60분 지속
```

근거: 위 두 규칙은 계약전력이라는 명판값만 보므로, "빈 매장에서 뭔가 계속 커졌다"는
그 매장 고유의 신호를 놓친다. 이 규칙이 정확히 사용자가 원한 개념이다 - **"아무도
없어야 할 매장에서 전력사용량이 증가했다"**를 그 매장 자신의 "진짜폐점" 분포에서
얼마나 벗어났는지로 판정한다. `baseline_median`/`baseline_floor`는 "진짜폐점"으로
판정된 슬롯 전체를 매장 단위로 모아 계산한 하나의 대표값이다((요일,슬롯)마다 따로
쓰지 않는 이유는 compute_group_thresholds() docstring 참고 - 13주 표본 단위 IQR은
너무 불안정하다). 배수 1.5x는 Tukey(1977, *Exploratory Data Analysis*)의 inner
fence 관례. **증가 방향만 본다**(감소는 판정 안 함) - 사용자 요구사항 그대로
("전력사용량이 증가하여") 반영. `floor`는 `max(iqr, median의 10%,
MIN_DEVIATION_FLOOR_KWH)`로 하한을 둬 저변동 계기(A-L-37 등)에서 대기전력급 미세
변동까지 이상으로 잡히는 퇴화를 막는다.

**이 규칙은 "위험"을 만들지 않는다 - "주의"까지만이다.** 처음엔 1.5x=주의/3x=위험
2단계로 만들었는데, 21개 매장 전체 재검증에서 문제가 드러났다: 냉동고 압축기
사이클처럼 "진짜폐점" 시간에도 정상적으로 60분 이상 지속되는 주기적 활동이 있으면,
그 매장 자신의 3개월 데이터 상위 분위수는 통계적으로 반드시 몇 건씩 존재한다
(예: A-L-60은 3x 기준으로도 실측 3개월간 위험 63건이 나왔다 - "계속 안전하게
운영 중"이라는 샘플데이터의 전제와 정면으로 모순된다). 배수를 아무리 올려도
"그 데이터 자신의 극단값은 그 데이터 안에 반드시 있다"는 통계적 사실 자체를
피할 수 없다. 그래서 "위험"은 이 통계적 신호에서 떼어내 오직 물리적으로 검증된
KEC 규칙(RULE_DANGER_OVERLOAD, 실측 3개월 0건 확인됨)에만 남겼다 - "경고(주의)"가
계속 심해지다 KEC 임계까지 넘으면 그때 비로소 "위험"이 되는 흐름이, 사용자가
설명한 "경고에서 위험으로 넘어가는 추세를 추적한다"는 시나리오와 자연스럽게
맞아떨어진다(8절 "향후 확장" 참고).

### 규칙이 겹칠 때

한 슬롯이 여러 규칙에 걸릴 수 있는데 `rule_triggered`는 단일 값이라,
**RULE_DANGER_OVERLOAD > RULE_CONTINUOUS_LOAD > RULE_BASELINE_WARN** 순으로
하나만 기록한다(같은 등급이면 설비 물리 기준을 개인화 통계 기준보다 우선 - 더
직접적인 근거를 남긴다).

## 실측 검증 (2026-09-05 재설계, 21개 매장 전체 재확인)

  "진짜폐점" 필터 도입 전 발견됐던 A-L-80의 매일 09:30~10:30 반복 오탐은 필터
  도입 후 **완전히 사라졌다**(해당 시간대 이벤트 0건, 재확인 완료). A-L-60의 매일
  17:15~20:00 반복 오탐도 "매일 2시간씩"에서 "3개월 중 단 하루(06-28) 17:00 1건"
  으로 급감했다 - 반복되던 패턴이 (요일,슬롯) 자체 이력으로 대부분 "낮지도
  안정적이지도 않다"고 걸러졌고, 남은 한 건은 그 안에서도 예외적으로 튄 진짜
  이례적 상황으로 보인다(주의 등급이 원래 하려는 일 - "드물게, 점검해볼 가치가
  있을 때만" 울리는 것).

  최종 집계(실측 2026-04-01~06-30, 21개 매장): **위험 0건**(구조적으로 보장 -
  위험은 KEC 규칙에서만 나오고 그 규칙은 실측에서 0건이었다), 주의 175건(6개
  매장 - A-L-30 83 / A-L-60 59 / A-L-63 12 / A-L-65 12 / A-L-37 11 / A-L-71 9 /
  A-L-79 10, 나머지 14개 매장은 0건). 이전 단계별 시행착오 기록: 통계 직접
  매핑(1차) 위험 4,111 -> 계약전력 절대기준+closed_hours 전체(2차) 위험 0/주의 26
  -> "진짜폐점" 필터 + 개인화 규칙에 위험 등급 부여(3차 초안, 실패) 위험 63(!)/
  주의 다수 -> 위험을 KEC 전용으로 되돌리고 배수를 outer fence로 올림(최종)
  위험 0/주의 175.

  A-L-30(83건)이 가장 많은 이유는 확인 필요 항목으로 남긴다 - 다른 매장 대비
  월등히 잦아서, 이 계기 자체의 야간 패턴이 유난히 불안정하거나(방한 4kW 매장
  다수와 달리 15kW급이라 냉장/온장 설비가 있을 가능성) 아니면 아직 못 잡은
  또 다른 설계 허점의 신호일 수 있다.

## 판정 범위 - A-L-58은 왜 여전히 별도 문제인가

  "진짜폐점" 필터는 "이 시간대가 평소에도 조용하고 안정적인가"만 본다. A-L-58은
  영업시간 중에도 계약전력(5kW)의 100%를 이미 넘겨 쓰는 계기라(중앙값 이용률
  106%, 최대 순간환산 573%, 단상 220V 회로로는 물리적으로 불가능한 수준),
  "진짜폐점" 시간대 자체는 여전히 낮고 안정적이어서(closed_hours 지속 초과 0건,
  실측 확인) 이 필터로 걸러지지도, 걸러질 필요도 없다 - 문제는 판정 범위가 아니라
  `contract_power_kw` 메타데이터 자체의 신뢰도이므로 별도 데이터 정합성 과제로
  남긴다(안전감지_기준수립_조사보고서.md 7절 #7).

## 향후 확장 - AI 추세추적 + 119 자동신고 (이번 범위 밖, 설계 방향만)

  지금 구현은 슬롯 단위 점검(sustained_mask)까지다. 사용자가 최종적으로 원하는
  형태는: 주의가 감지되면 AI가 해당 매장을 지속 추적하다가, 주의~위험 구간 내에서
  전력이 계속 우상향하면(단발성 급증이 아니라 추세) 사고 발생 전에 긴급알림 +
  119 신고(수치 데이터를 근거로 첨부)까지 자동 진행하거나 점주에게 직접 연락을
  유도하는 것. 이건 RULE_BASELINE_WARN/DANGER가 만드는 "경고->위험 연속 구간"
  위에서 도는 감시 레이어라 지금 스키마(anomaly_events, 슬롯당 1행)를 그대로
  베이스로 쓸 수 있다 - 정답 라벨(실제 사고 여부)이 없어 ML 성능 검증이 불가능하다는
  기존 제약(visualize_analyis_data/docs/05_시각분석_AI방법론_보고서.md 122~123행)은
  여전히 유효해서, 이번 범위에서는 "규칙 기반 추적 로직"까지가 현실적 목표다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# --- 위험: KEC 212.3 표 212.3-2 산업용 배선차단기 (계약전력 130%, 60분 지속) ---
DANGER_OVERLOAD_RATIO = 1.30
DANGER_SUSTAINED_SLOTS = 4          # 60분 = 15분 x 4
RULE_DANGER_OVERLOAD = "kec212_overload_130pct_60min"

# --- 주의: 연속부하 80% 규칙 (NEC 210.20(A), 3시간 지속) ---
CONTINUOUS_LOAD_RATIO = 0.80
CONTINUOUS_SUSTAINED_SLOTS = 12     # 3시간 = 15분 x 12
RULE_CONTINUOUS_LOAD = "continuous_load_80pct_180min"

# --- "진짜폐점" 판별: 낮음(수준) + 안정적(변동) 두 조건을 모두 만족해야 인정 ---
# (요일,슬롯)의 3개월 중앙값이 night_baseline보다 1.5배 넘게 높으면 "낮음" 탈락.
QUIET_SLOT_MULT = 1.5
# 같은 (요일,슬롯)의 IQR이 night_baseline보다 크면 "안정적" 탈락 - 중앙값은 낮아도
# 가끔 크게 튀는 들쑥날쑥한 슬롯(그 자체가 이따금의 자체적 활동 신호)을 걸러낸다.
QUIET_STABILITY_MULT = 1.0

# --- 매장별 개인화 baseline 이탈 (진짜폐점 슬롯에서만, 60분 지속, 증가 방향만) ---
# "위험" 등급은 만들지 않는다 - 이유는 모듈 docstring "규칙이 겹칠 때" 앞 단락 참고.
# inner fence(1.5x)는 실측 검증 결과 21개 매장 3개월간 600건 넘게 잡혀(매장당 사흘에
# 한 번꼴) "주의"라는 등급에 비해 지나치게 잦았다 - outer fence(3x, Tukey 1977의
# "far out" 기준)로 올려 진짜 드문 이탈만 남긴다("위험" 자리에서 물러나며 그 자리의
# 더 엄격한 배수를 이어받는 셈).
WARN_DEVIATION_MULT = 3.0           # Tukey(1977) outer fence - 주의
BASELINE_SUSTAINED_SLOTS = 4        # 60분 = 15분 x 4
MIN_DEVIATION_FLOOR_KWH = 0.05      # 절대 최소 편차(15분당 0.05kWh=순간 0.2kW) -
                                     # 저변동 계기에서 대기전력급 미세변동까지 잡히는 걸 방지
RULE_BASELINE_WARN = "empty_store_baseline_3x_60min"

EVENT_COLUMNS = ["ts", "recv_kWh", "level", "rule_triggered", "threshold_value"]


def compute_group_thresholds(
    meter_ts: pd.DataFrame, night_baseline: float, min_deviation_floor_frac: float = 0.1
) -> tuple[pd.DataFrame, float, float]:
    """
    실측 시계열(한 계기분, 'time'/'recv_kWh' 컬럼) -> (요일,슬롯 eligibility 표, 매장
    전체 baseline_median, 매장 전체 baseline_floor) 3종 반환.

    두 단계로 나뉜다 - "이 슬롯이 판정 대상인가"와 "판정 기준값이 얼마인가"를 서로
    다른 표본으로 계산한다:

    1. **eligibility(is_quiet_slot)**: (요일,슬롯) 672개 조합별로 낮은가(중앙값 <=
       night_baseline x QUIET_SLOT_MULT) AND 안정적인가(IQR <= night_baseline x
       QUIET_STABILITY_MULT)를 그 슬롯 자신의 13주치(3개월/7일) 표본으로 판단한다.
       표본이 13개뿐이라 IQR 추정 자체는 거칠지만, 여기서는 그 값을 "OO 슬롯이
       판정 대상이냐 아니냐"는 이분법적 게이트로만 쓰므로 노이즈에 비교적 강하다.
    2. **판정 기준값(baseline_median/floor)**: 1번에서 "진짜폐점"으로 판정된 모든
       슬롯의 실측값을 **매장 전체에서 하나로 모아서** median/IQR을 계산한다(보통
       수백~수천 개 표본 - 예를 들어 슬롯의 40%가 조용하다면 3개월 x 90일 x 96슬롯
       x 40% ≈ 3,456개). (요일,슬롯) 672칸마다 따로 floor를 만들면 칸당 표본이
       13개뿐이라 IQR 추정이 극도로 불안정해지고, 그 좁은 floor를 그대로 이상치
       판정 임계값(threshold_value)으로 쓰면 정상적인 야간 노이즈까지 전부 위험/
       주의로 잡히는 폭발이 실측으로 확인됐다(2026-09-05, 21개 매장 전체에서 42건
       -> 562건으로 급증, 다수 매장에서 위험 수십 건씩 - "샘플데이터는 계속
       안전하게 운영 중"이라는 전제와 정면으로 어긋나 재설계함). 표본을 매장 전체로
       합쳐 하나의 안정적인 대표값을 쓰면 이 문제가 해결된다 - "이 매장이 진짜
       비었을 때 표준 전력이 얼마인가"라는 사용자 요구사항과도 더 잘 맞는다(굳이
       요일·슬롯마다 다른 기준을 가질 이유가 없다 - 빈 매장은 언제 비든 똑같이 비어야
       한다).

    floor = max(IQR, 중앙값의 min_deviation_floor_frac 비율, MIN_DEVIATION_FLOOR_KWH) -
    IQR이 0에 가까워지는 저변동 계기에서 아주 미세한 변동까지 전부 이상치로 잡히는 걸 방지.

    (요일, 슬롯) 672개 조합 중 일부는 실측 3개월 안에 유효한 관측치가 아예 없을 수
    있다(결측 구간이 하필 그 요일x시간대와 겹치는 경우) - 이런 조합은 groupby 결과에
    median=NaN으로 남고, 그대로 두면 병합 시 그 슬롯은 항상 "정상" 취급되어 아무리
    극단적인 값이 와도 절대 이상치로 잡히지 않는 사각지대가 생긴다(실측으로 A-L-16의
    화요일 02:30 슬롯에서 이 문제를 확인함 - 주입한 mock 스파이크가 조용히
    누락됐었음). 그래서 데이터가 없는 조합은 "계기 전체(모든 요일x시간대 통합)"
    median/IQR로 폴백시켜, 최소한의 이상치 탐지 능력을 보장한다.
    """
    df = meter_ts.copy()
    df["dow"] = df["time"].dt.dayofweek
    df["slot"] = df["time"].dt.hour * 4 + df["time"].dt.minute // 15

    grouped = (
        df.groupby(["dow", "slot"])["recv_kWh"]
        .agg(median="median", q1=lambda s: s.quantile(0.25), q3=lambda s: s.quantile(0.75))
        .reset_index()
    )
    grouped["iqr"] = grouped["q3"] - grouped["q1"]

    # 계기 전체 통합 폴백 통계 (그룹 데이터가 없을 때만 쓰임)
    overall_median = float(df["recv_kWh"].median())
    overall_iqr = float(df["recv_kWh"].quantile(0.75) - df["recv_kWh"].quantile(0.25))

    full_index = pd.MultiIndex.from_product([range(7), range(96)], names=["dow", "slot"])
    grouped = grouped.set_index(["dow", "slot"]).reindex(full_index).reset_index()
    grouped["median"] = grouped["median"].fillna(overall_median)
    grouped["iqr"] = grouped["iqr"].fillna(overall_iqr)

    night_baseline_eff = max(float(night_baseline), 0.01)
    is_low = grouped["median"] <= night_baseline_eff * QUIET_SLOT_MULT
    is_stable = grouped["iqr"] <= night_baseline_eff * QUIET_STABILITY_MULT
    grouped["is_quiet_slot"] = is_low & is_stable

    eligibility = grouped[["dow", "slot", "is_quiet_slot"]]

    quiet_pool = df.merge(eligibility, on=["dow", "slot"], how="left")
    quiet_pool = quiet_pool.loc[quiet_pool["is_quiet_slot"].fillna(False), "recv_kWh"].dropna()
    if len(quiet_pool) >= 30:  # 표본이 너무 적으면(진짜폐점 슬롯이 거의 없는 계기) 폴백
        baseline_median = float(quiet_pool.median())
        baseline_iqr = float(quiet_pool.quantile(0.75) - quiet_pool.quantile(0.25))
    else:
        baseline_median, baseline_iqr = night_baseline_eff, overall_iqr
    baseline_floor = max(baseline_iqr, min_deviation_floor_frac * abs(baseline_median), MIN_DEVIATION_FLOOR_KWH)

    return eligibility, baseline_median, baseline_floor


def sustained_mask(ts: pd.Series, condition: pd.Series, slots: int) -> pd.Series:
    """
    condition이 연속 slots개 이상 유지된 구간에 속하는 행을 True로 표시한다.

    closed_hours로 미리 걸러진 입력은 영업시간대가 빠져있어 ts가 항상 15분 간격으로
    연속이라는 보장이 없다(어젯밤 마지막 폐점 슬롯 -> 오늘 첫 폐점 슬롯 사이에
    영업시간 전체가 빠짐). ts 간격이 정확히 900초인 구간만 "연속"으로 인정해서,
    끊긴 두 구간을 하나의 지속 구간으로 잘못 잇는 것을 막는다. "진짜폐점이 아닌"
    슬롯(is_quiet_slot=False)이 condition에서 이미 False로 꺼져 있으므로, 그 슬롯이
    준비시간/미등록 영업시간이든 뭐든 지속 구간을 자동으로 끊어준다 - 별도 처리 불필요.

    윈도우 끝 슬롯만이 아니라 그 윈도우에 속한 slots개 행을 전부 True로 만든다 -
    이 프로젝트의 "모든 판정은 슬롯 단위" 관례를 지키기 위함.
    """
    cond = pd.Series(condition).reset_index(drop=True).fillna(False).astype(bool)
    gap_ok = pd.Series(ts).reset_index(drop=True).diff().dt.total_seconds().eq(900.0)

    window_end = (cond.rolling(slots).sum() == slots) & (gap_ok.rolling(slots - 1).sum() == slots - 1)
    member = pd.Series(False, index=cond.index)
    for lead in range(slots):
        member = member | window_end.shift(-lead).fillna(False)
    return member


def detect_events(
    full_ts: pd.DataFrame,
    eligibility: pd.DataFrame,
    contract_power_kw: float | None,
    baseline_median: float,
    baseline_floor: float,
) -> pd.DataFrame:
    """
    full_ts: 'ts'/'recv_kWh' 컬럼. schedule_status='closed_hours'로 이미 1차 필터된
             시계열을 넘기는 게 정상 사용법이다(필터는 호출부 책임 - 10_detect_anomalies.py).
             최종 판정 범위는 여기서 eligibility.is_quiet_slot으로 다시 한 번 좁힌다
             (모듈 docstring "3차 설계" 참고 - 영업시간표를 못 믿으므로).
    eligibility: compute_group_thresholds()가 반환한 (dow,slot,is_quiet_slot) 표
                 (판정 대상 여부만 담당 - 기준값은 아래 baseline_median/floor).
    contract_power_kw: 그 계기의 계약전력. RULE_DANGER_OVERLOAD/RULE_CONTINUOUS_LOAD
                       두 규칙의 기준이라, 없으면(NULL/0) 그 두 규칙은 평가하지 않는다
                       (개인화 baseline 규칙은 계약전력과 무관하므로 영향 없음 - 단,
                       현재 구현은 단순화를 위해 contract_power_kw가 없으면 전체를
                       빈 결과로 반환한다. 이 데이터셋 21개 계기는 전부 값이 있어
                       실무적으로 발생하지 않는다).
    baseline_median/baseline_floor: compute_group_thresholds()가 "진짜폐점"으로
                       판정된 슬롯 전체를 매장 단위로 합쳐 계산한 스칼라 값
                       (RULE_BASELINE_WARN/DANGER의 기준 - (요일,슬롯)마다 따로 안
                       쓰는 이유는 함수 docstring 참고, 13주 표본으로 계산한 슬롯별
                       IQR을 그대로 임계값에 쓰면 오탐이 폭증함을 실측으로 확인함).

    반환: 플래그된 행만 EVENT_COLUMNS로. 등급 우선순위는 위험 > 주의이고, 같은 등급
    안에서는 설비 물리 기준(KEC/NEC)이 개인화 통계 기준보다 우선한다(모듈 docstring
    "규칙이 겹칠 때" 참고).
    """
    df = full_ts.copy().sort_values("ts").reset_index(drop=True)
    if df.empty or contract_power_kw is None or pd.isna(contract_power_kw) or contract_power_kw <= 0:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    df["dow"] = df["ts"].dt.dayofweek
    df["slot"] = df["ts"].dt.hour * 4 + df["ts"].dt.minute // 15
    m = df.merge(eligibility, on=["dow", "slot"], how="left")
    # 매칭 안 된 슬롯(이론상 발생 안 함 - full_index가 672개 전부 채워져 있음)은
    # 안전하게 "진짜폐점 아님"으로 취급해 판정에서 제외한다.
    m["is_quiet_slot"] = m["is_quiet_slot"].fillna(False)
    quiet = m["is_quiet_slot"]

    # 15분 kWh를 순간 kW로 환산(x4)해 계약전력과 같은 단위로 비교한다.
    load_ratio = m["recv_kWh"] * 4 / contract_power_kw

    danger_threshold = contract_power_kw * DANGER_OVERLOAD_RATIO / 4
    continuous_threshold = contract_power_kw * CONTINUOUS_LOAD_RATIO / 4
    warn_threshold = baseline_median + WARN_DEVIATION_MULT * baseline_floor

    is_kec_danger = sustained_mask(m["ts"], quiet & (load_ratio >= DANGER_OVERLOAD_RATIO), DANGER_SUSTAINED_SLOTS)
    is_continuous = sustained_mask(m["ts"], quiet & (load_ratio >= CONTINUOUS_LOAD_RATIO), CONTINUOUS_SUSTAINED_SLOTS)

    # 증가 방향만 이상으로 본다(감소는 "더 조용해진 것"뿐이라 판정 대상이 아님) -
    # 사용자 요구사항("전력사용량이 증가하여") 그대로 반영.
    #
    # 이 규칙은 "위험"을 만들지 않는다(RULE_BASELINE_DANGER를 실제로 도입했다가
    # 재실측에서 폐기함 - 모듈 docstring 참고). 냉동고 압축기처럼 "진짜폐점" 시간에도
    # 정상적으로 오래 지속되는 주기적 활동은 통계적 이탈만으로는 위험한 과부하와
    # 구분할 수 없어서, 배수를 아무리 조정해도 실측 3개월 데이터 안에서 그 데이터
    # 자신의 상위 분위수는 반드시 몇 건씩 걸린다 - "위험(=이미 사고 조건)"이라는
    # 단정적 등급에는 안 맞는 강도다. 이 신호는 "점검해볼 가치가 있다"는 주의까지만
    # 준다 - 위험은 오직 물리적으로 검증된 KEC 규칙(RULE_DANGER_OVERLOAD)에서만 나온다.
    is_baseline_warn = sustained_mask(
        m["ts"], quiet & (m["recv_kWh"] >= warn_threshold), BASELINE_SUSTAINED_SLOTS
    )

    is_danger = is_kec_danger
    is_caution = is_continuous | is_baseline_warn

    level = np.where(is_danger, "위험", np.where(is_caution, "주의", None))
    rule = np.select(
        [is_kec_danger, is_continuous, is_baseline_warn],
        [RULE_DANGER_OVERLOAD, RULE_CONTINUOUS_LOAD, RULE_BASELINE_WARN],
        default=None,
    )
    threshold_value = np.select(
        [is_kec_danger, is_continuous, is_baseline_warn],
        [danger_threshold, continuous_threshold, warn_threshold],
        default=np.nan,
    )

    out = m.assign(level=level, rule_triggered=rule, threshold_value=threshold_value)
    return out.loc[out["level"].notna(), EVENT_COLUMNS].reset_index(drop=True)
