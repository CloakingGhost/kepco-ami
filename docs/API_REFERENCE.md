# AMI 영업유무·혼잡도·안전감지 API 레퍼런스

> 이 문서는 `scripts/generate_api_docs.py`가 FastAPI OpenAPI 스키마에서 **자동 생성**했습니다.
> 코드(엔드포인트·모델)를 고쳤다면 재생성하세요: `uv run python scripts/generate_api_docs.py`
>
> 버전: `0.1.0` · 생성 시각: `2026-09-02 17:12 UTC`

**Base URL(로컬)**: `http://localhost:8000`  (Swagger UI: `http://localhost:8000/docs`)

화곡동 파일럿 21개 매장의 영업유무/혼잡도/이상치를 조회하는 API. 데이터는 db/scripts/00~10 배치가 미리 계산해 PostgreSQL에 적재해 둔 것을 그대로 읽기만 한다. 조회 가능 날짜 범위: 2026-04-01 ~ 오늘.

## 목차
- [기본](#기본)
- [매장정보](#매장정보)
- [영업유무·혼잡도](#영업유무혼잡도)
- [안전감지](#안전감지)
- [원시 전력값](#원시-전력값)

## 기본

### `GET /health`
**헬스체크**

---

## 매장정보

### `GET /api/stores`
**매장 목록**

화곡동에 매칭된 21개 매장의 기본정보(이름/주소/좌표/업종) 전체. 파라미터 없음 - 바로 실행.

**응답 (200)** — `StoreListResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `stores` | `StoreSchema[]` |  |

<details><summary><code>StoreSchema</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `store_id` | `integer` |  |
| `meter_id` | `string` |  |
| `name` | `string` |  |
| `branch_name` | `string \| null` |  |
| `road_address` | `string` |  |
| `building_name` | `string \| null` |  |
| `floor_info` | `string \| null` |  |
| `longitude` | `number \| null` |  |
| `latitude` | `number \| null` |  |
| `biz_category_large` | `string \| null` |  |
| `biz_category_mid` | `string \| null` |  |
| `dong_name` | `string` |  |
| `match_note` | `string` |  |
| `data_resolution` | `string` | '15min'(정상) | '1hour' - 이 계기가 recv_kWh를 매시 정각에만 리포트하는 계기면 '1hour'. 결측이 아니라 계기 자체의 리포트 주기 특성 - 숨기지 않고 그대로 노출한다. |

</details>

---

## 영업유무·혼잡도

### `GET /api/stores/status`
**전체 매장 현재 상태 (메인 화면용)**

21개 매장 전부의 **지금 이 순간** 영업유무(final_status)와 혼잡도(congestion_level)를
한 번에 반환한다. 파라미터 없음 - 바로 실행. 지도/목록 메인 화면이 이 엔드포인트
하나만 호출하면 마커 색칠까지 끝난다(final_status로 아이콘, congestion_level로 색상).

**응답 (200)** — `CurrentStatusListResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `stores` | `CurrentStatusSchema[]` |  |

<details><summary><code>CurrentStatusSchema</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `store_id` | `integer` |  |
| `name` | `string` |  |
| `longitude` | `number \| null` |  |
| `latitude` | `number \| null` |  |
| `ts` | `string` | 이 상태를 계산한 15분 슬롯 시각(현재 시각 이하 중 가장 최근) |
| `schedule_status` | `string` | 'open_hours' | 'closed_hours' - 운영시간표 기준 판정 |
| `power_status` | `string` | 'active' | 'low' - 야간 baseline 대비 실측 전력 기준 판정 |
| `final_status` | `string` | '영업중' | '휴무추정' | '예외영업' | '영업종료' |
| `congestion_level` | `string \| null` | '상' | '중' | '하' | null(영업중이 아니면 null) |

</details>

---

### `GET /api/stores/{store_id}/status/current`
**매장 1곳 현재 상태**

예시: `/api/stores/1/status/current` -> 못난이찹쌀꽈배기의 현재 상태.
store_id 범위를 벗어나거나(1~21이 아니거나) 아직 상태가 계산되지 않았으면 404.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `store_id` | path | `integer` | O | 1 | 상가 ID (1~21) |

**응답 (200)** — `CurrentStatusOneSchema`

| 필드 | 타입 | 설명 |
|---|---|---|
| `store_id` | `integer` |  |
| `name` | `string` |  |
| `ts` | `string` | 이 상태를 계산한 15분 슬롯 시각(현재 시각 이하 중 가장 최근) |
| `schedule_status` | `string` | 'open_hours' | 'closed_hours' - 운영시간표 기준 판정 |
| `power_status` | `string` | 'active' | 'low' - 야간 baseline 대비 실측 전력 기준 판정 |
| `final_status` | `string` | '영업중' | '휴무추정' | '예외영업' | '영업종료' |
| `congestion_level` | `string \| null` | '상' | '중' | '하' | null(영업중이 아니면 null) |

---

### `GET /api/stores/{store_id}/hours`
**매장 1곳의 요일별 운영시간**

예시: `/api/stores/1/hours` -> 못난이찹쌀꽈배기의 요일별(월~일) 운영시간.
google_places 실측이 있으면 그걸, 없으면 ksic_estimate(업종코드 기반 추정)를
반환한다 - 각 행의 `source`로 어느 쪽인지 구분된다. store_id 범위를 벗어나면 404.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `store_id` | path | `integer` | O | 1 | 상가 ID (1~21) |

**응답 (200)** — `StoreHoursResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `store_id` | `integer` |  |
| `hours` | `StoreHoursRow[]` | 요일별 0~7행. day_of_week 기준 최대 7행(요일당 1행, google_places 우선) |

<details><summary><code>StoreHoursRow</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `day_of_week` | `integer` | 0=월요일 ... 6=일요일 |
| `open_time` | `string \| null` | 휴무일이면 null |
| `close_time` | `string \| null` | 휴무일이면 null |
| `is_closed` | `boolean` |  |
| `is_24h` | `boolean` |  |
| `source` | `string` | 'google_places'(실측) | 'ksic_estimate'(업종코드 기반 추정) - 실측이 있으면 항상 실측 우선 |

</details>

---

### `GET /api/stores/{store_id}/status`
**매장 1곳의 하루 상태+전력 타임라인**

예시: `/api/stores/1/status?date=2026-08-15` -> 96개(15분×24시간) 슬롯의
schedule_status/power_status/final_status/congestion_level + 실제 전력값(kWh).
data/images/user-메인-*.png의 "오늘 시간대별" 차트를 이 한 번의 호출로 그릴 수 있다.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `store_id` | path | `integer` | O | 1 | 상가 ID (1~21) |
| `date` | query | `string` | O | 2026-05-15, 2026-08-15 | 조회할 날짜. 2026-04-01~2026-06-30은 실측, 2026-07-01~오늘은 합성 데이터(is_synthetic로 구분됨). |

**응답 (200)** — `DayStatusResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `store_id` | `integer` |  |
| `date` | `string` |  |
| `data_resolution` | `string` | '15min'(정상, rows 96행) | '1hour' - 1hour인 매장은 결측 슬롯의 판정 자체를 생략하므로 rows 길이가 96보다 짧을 수 있다(빈 슬롯=결측 gap으로 해석할 것). |
| `rows` | `DayStatusRow[]` | 15분 슬롯당 1행. data_resolution='1hour'인 매장은 96행보다 적을 수 있다. |

<details><summary><code>DayStatusRow</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `ts` | `string` |  |
| `schedule_status` | `string` |  |
| `power_status` | `string` |  |
| `final_status` | `string` |  |
| `congestion_level` | `string \| null` |  |
| `received_active_power_kwh` | `number \| null` | 15분 유효전력(kWh). 결측이면 null |
| `is_synthetic` | `boolean` | false=실측(2026-04-01~06-30), true=합성(2026-07-01~오늘) |
| `is_redistributed` | `boolean` | true면 이 슬롯의 전력값이 실제 15분 단위 실측이 아니라, 같은 업종 코호트의 시간 내 상대 형태를 정각 실측값에 앵커링해 추정한 값(data_resolution='1hour' 계기 중 한식 코호트가 충분한 경우에만 적용됨) |

</details>

---

## 안전감지

### `GET /api/anomalies`
**이상치 이벤트 목록 (관리자 화면용)**

예시: `/api/anomalies?level=위험&limit=10` -> 위험 등급 최신 10건.
파라미터를 하나도 안 주면(`/api/anomalies`) 전체 계기의 최신 이상치 200건이 반환된다.
각 행에 매장명(store_name)까지 조인되어 있어 계기번호를 몰라도 바로 알아볼 수 있다.
안전감지는 "영업종료 이후"에만 판정한다(영업시간 중 스파이크는 혼잡도 문제일 뿐 안전 이슈가 아니라고
봄 - data/프로젝트개요.md 안전감지 항목 참고).

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `level` | query | `string` |  | 위험 | 심각도 필터. 비우면 전체. |
| `meter_id` | query | `string` |  | A-L-71 | 특정 계기만 조회하고 싶을 때. |
| `since` | query | `string` |  | 2026-08-01 | 이 날짜 이후(포함)만 조회. |
| `until` | query | `string` |  | 2026-09-01 | 이 날짜 이전(포함)까지만 조회. |
| `limit` | query | `integer` |  | 50 | 최대 반환 건수(최신순). |

**응답 (200)** — `AnomalyListResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `anomalies` | `AnomalySchema[]` |  |

<details><summary><code>AnomalySchema</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `event_id` | `integer` |  |
| `meter_id` | `string` |  |
| `store_name` | `string \| null` |  |
| `detected_at` | `string` |  |
| `level` | `string` | '주의' | '위험' |
| `rule_triggered` | `string` |  |
| `metric_value` | `number` | 실제 관측된 유효전력(kWh) |
| `threshold_value` | `number` | 이 값을 넘으면 해당 level로 판정된 임계치 |
| `notified_at` | `string \| null` | 문자 발송 연동은 이번 범위 밖이라 항상 null |

</details>

---

## 원시 전력값

### `GET /api/meters/{meter_id}/timeseries`
**계기 1곳의 하루 원시 전력값**

상태 판정 없이 15분 단위 전력값(kWh)만 필요할 때 쓴다. 상태+전력을 같이 보려면
`/api/stores/{store_id}/status`를 대신 쓰는 게 낫다(이 엔드포인트는 순수 원시값용).

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `meter_id` | path | `string` | O | A-L-11 | 계기번호(예: 'A-L-11') |
| `date` | query | `string` | O | 2026-05-15, 2026-08-15 | 조회할 날짜 |

**응답 (200)** — `MeterTimeseriesResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `meter_id` | `string` |  |
| `date` | `string` |  |
| `is_synthetic` | `boolean \| null` |  |
| `data_resolution` | `string` | '15min'(정상, rows 96행) | '1hour' - 1hour 계기는 rows 길이가 96보다 짧을 수 있다. |
| `rows` | `TimeseriesRow[]` | 15분 슬롯당 1행. data_resolution='1hour'이면 96행보다 적을 수 있다. |

<details><summary><code>TimeseriesRow</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `ts` | `string` |  |
| `received_active_power_kwh` | `number \| null` |  |
| `is_synthetic` | `boolean` |  |
| `is_redistributed` | `boolean` | true면 실제 15분 단위 실측이 아니라 코호트 비율로 추정한 값(DayStatusRow.is_redistributed와 동일 의미) |

</details>

---

### `GET /api/stores/{store_id}/timeseries`
**매장 1곳의 하루 원시 전력값**

meter_timeseries와 동일하나 store_id(상가 기준)로 조회한다.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `store_id` | path | `integer` | O | 1 | 상가 ID (1~21) |
| `date` | query | `string` | O | 2026-05-15, 2026-08-15 | 조회할 날짜 |

**응답 (200)** — `StoreTimeseriesResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `store_id` | `integer` |  |
| `meter_id` | `string` |  |
| `date` | `string` |  |
| `is_synthetic` | `boolean \| null` |  |
| `data_resolution` | `string` | '15min'(정상, rows 96행) | '1hour' - 1hour 계기는 rows 길이가 96보다 짧을 수 있다. |
| `rows` | `TimeseriesRow[]` | 15분 슬롯당 1행. data_resolution='1hour'이면 96행보다 적을 수 있다. |

<details><summary><code>TimeseriesRow</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `ts` | `string` |  |
| `received_active_power_kwh` | `number \| null` |  |
| `is_synthetic` | `boolean` |  |
| `is_redistributed` | `boolean` | true면 실제 15분 단위 실측이 아니라 코호트 비율로 추정한 값(DayStatusRow.is_redistributed와 동일 의미) |

</details>

---
