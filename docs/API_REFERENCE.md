# AMI 영업유무·혼잡도·안전감지 API 레퍼런스

> 이 문서는 `scripts/generate_api_docs.py`가 FastAPI OpenAPI 스키마에서 **자동 생성**했습니다.
> 코드(엔드포인트·모델)를 고쳤다면 재생성하세요: `uv run python scripts/generate_api_docs.py`
>
> 버전: `0.1.0` · 생성 시각: `2026-09-03 17:07 UTC`

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

### `POST /api/stores`
**매장 1곳 상세 조회**

store_id를 body로 받는다(GET 경로에 store_id를 노출하지 않으려고 POST를 씀 - 목록
조회용 GET /api/stores와 경로는 같지만 메서드가 달라 공존한다).

평점/전화번호/웹사이트(Google Places, google_places_cache 최신 행) + 요일별(월~일)
영업시간 + 지금 이 순간의 영업상태/혼잡도를 한 번에 반환한다. Google Places 정보나
현재 상태 데이터가 없으면 해당 필드는 null이 되고 message에 안내 문구가 채워진다
(매장 자체는 존재하므로 404가 아니라 200으로 응답).

congestion_level은 정수 코드로 내려온다: 0=해당없음(영업중이 아니거나 데이터 없음)
| 1=하 | 2=중 | 3=상. final_status는 내부 4값 중 '예외영업'을 '영업종료'로 접어
영업중/휴무추정/영업종료 3값으로만 내려준다.

store_id가 1~21 범위를 벗어나면 404.

**응답 (200)** — `StoreDetailResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `rating` | `number \| null` | google_places_cache 최신 행의 raw_response_json.rating. Google Places 정보가 없으면 null |
| `congestion_level` | `integer` | 혼잡도 코드. 0=해당없음(영업중이 아니거나 현재 상태 데이터 없음) | 1=하 | 2=중 | 3=상 |
| `name` | `string` | stores.name |
| `formatted_phone_number` | `string \| null` | raw_response_json.formatted_phone_number. 010 등 휴대폰 번호인 경우도 있음. 정보 없으면 null |
| `road_address` | `string` | stores.road_address |
| `weekday` | `object` | monday~sunday 키의 요일별 영업시간 문자열(google_places 우선/ksic_estimate 폴백). '휴무' | '24시간' | '정보없음' 가능 |
| `schedule_status` | `string \| null` | 'open_hours' | 'closed_hours' | null(현재 상태 데이터 없음) |
| `power_status` | `string \| null` | 'active' | 'low' | null(현재 상태 데이터 없음) |
| `final_status` | `string \| null` | '영업중' | '휴무추정' | '영업종료' | null(현재 상태 데이터 없음) - 내부 4값 중 '예외영업'은 '영업종료'로 단순화됨 |
| `biz_category_large` | `string \| null` | stores.biz_category_large |
| `website` | `string \| null` | raw_response_json.website. 없는 경우도 있음 |
| `message` | `string \| null` | Google Places 정보 부재/현재 상태 데이터 부재 등 안내 문구. 문제 없으면 null |

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

### `GET /api/stores/snapshot`
**특정 날짜·시각의 전체 매장 스냅샷**

예시: `/api/stores/snapshot?date=26-05-09&time=19:15` -> 그 시점 21개 매장의
위치/영업상태/혼잡도/전력사용량을 한 번에 반환한다.

조회 가능 날짜 범위는 AMI 샘플데이터의 시작일~마지막일인 2026-04-01~2026-06-30로
고정된다(다른 엔드포인트처럼 오늘까지의 합성 구간을 포함하지 않음).

계기 해상도 처리: data_resolution='1hour'인 매장(5개)은 15/30/45분 슬롯이 애초에
없으므로 입력 시각의 "시"만 사용해 정각 데이터를 가져온다(예: 19:15 입력 -> 19:00 슬롯).
나머지 '15min' 매장은 입력 시각을 그대로 사용한다. 매장별로 그 시점 데이터 자체가
없으면(정각 슬롯 결측 등) 상태 관련 필드가 전부 null이 되고 message에 안내 문구가 채워진다.

final_status는 내부 4값 중 '예외영업'을 '영업종료'로 접어 영업중/휴무추정/영업종료
3값으로만 내려준다(일반 사용자는 영업 중인지 아닌지만 판단하면 되기 때문).

congestion_level은 문자열이 아니라 정수 코드로 내려온다: 0=해당없음(영업중이
아니거나 데이터 없음) | 1=하 | 2=중 | 3=상.

date/time 형식이 잘못됐거나 date가 조회 가능 범위를 벗어나면 400.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `date` | query | `string` | O | 26-05-09 | 조회할 날짜, 'YY-MM-DD' 형식(연도 2자리). 범위: 26-04-01 ~ 26-06-30 (AMI 샘플데이터 실측 구간). |
| `time` | query | `string` | O | 19:15 | 조회할 시각, 'HH:MM' 형식(00:00~23:45, 15분 단위만 허용: 00/15/30/45). |

**응답 (200)** — `StoreSnapshotResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `count` | `integer` |  |
| `date` | `string` | 입력값 그대로 |
| `time` | `string` | 입력값 그대로(계기별 해상도 적용은 서버 내부에서 처리됨) |
| `items` | `StoreSnapshotGroup[]` |  |

<details><summary><code>StoreSnapshotGroup</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `line_name` | `string` |  |
| `stores` | `StoreSnapshotItem[]` |  |

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
