# AMI 영업유무·혼잡도·안전감지 API 레퍼런스

> 이 문서는 `scripts/generate_api_docs.py`가 FastAPI OpenAPI 스키마에서 **자동 생성**했습니다.
> 코드(엔드포인트·모델)를 고쳤다면 재생성하세요: `uv run python scripts/generate_api_docs.py`
>
> 버전: `0.1.0` · 생성 시각: `2026-09-08 15:07 UTC`

**Base URL(로컬)**: `http://localhost:8000`  (Swagger UI: `http://localhost:8000/docs`)

화곡동 파일럿 21개 매장의 영업유무/혼잡도/이상치를 조회하는 API. 데이터는 db/scripts/00~10 배치가 미리 계산해 PostgreSQL에 적재해 둔 것을 그대로 읽기만 한다. 조회 가능 날짜 범위: 2026-04-01 ~ 2026-07-31 (2026-06-30까지는 실측, 7월은 안전감지 데모용 합성 구간).

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

final_status는 내부 4값 중 '예외영업'을 '영업종료'로 접어 영업중/휴무추정/영업종료
3값으로만 내려준다.

**기준 시각**: body에 date/time을 같이 주면 그 시점 기준으로 영업상태를 판정한다.
목록(`/api/stores/snapshot`)에서 사용자가 고른 날짜·시각을 그대로 넘기면 목록과
상세가 항상 같은 상태를 보여준다(안 넘기면 서버의 현재 시각 기준이라 목록이
과거 시각을 보고 있을 때 둘이 어긋난다).

store_id가 1~21 범위를 벗어나면 404, date/time 형식이 잘못되면 400.

**응답 (200)** — `StoreDetailResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `rating` | `number \| null` | google_places_cache 최신 행의 raw_response_json.rating. Google Places 정보가 없으면 null |
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
| `date` | query | `string` |  |  | 조회할 날짜, 'YY-MM-DD' 형식(연도 2자리). 범위: 26-04-01 ~ 26-06-30 (AMI 샘플데이터 실측 구간). |
| `time` | query | `string` |  |  | 조회할 시각, 'HH:MM' 형식(00:00~23:45, 15분 단위만 허용: 00/15/30/45). |

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

### `POST /api/stores/status/current`
**매장 1곳 현재 상태**

store_id를 body로 받는다(URL에 store_id를 노출하지 않으려고 POST를 씀).
예시: body `{"store_id": 1}` -> 못난이찹쌀꽈배기의 현재 상태.
store_id 범위를 벗어나거나(1~21이 아니거나) 아직 상태가 계산되지 않았으면 404.

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

### `POST /api/stores/hours`
**매장 1곳의 요일별 운영시간**

store_id를 body로 받는다(URL에 store_id를 노출하지 않으려고 POST를 씀).
예시: body `{"store_id": 1}` -> 못난이찹쌀꽈배기의 요일별(월~일) 운영시간.
google_places 실측이 있으면 그걸, 없으면 ksic_estimate(업종코드 기반 추정)를
반환한다 - 각 행의 `source`로 어느 쪽인지 구분된다. store_id 범위를 벗어나면 404.

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

### `POST /api/stores/status/day`
**매장 1곳의 하루 상태+전력 타임라인**

store_id를 body로 받는다(URL에 store_id를 노출하지 않으려고 POST를 씀).

예시: body `{"store_id": 1, "date": "2026-05-15"}` -> 15분 슬롯별
schedule_status/power_status/final_status/congestion_level(정수 0~3) + 전력값(kWh).
화면의 "시간대별 전력" 차트를 이 한 번의 호출로 그릴 수 있다.

**기준 시각**: body에 time을 같이 주면 그 시각 이후(미래) 슬롯은 잘라서 응답에서
제외한다(96개보다 적을 수 있음) - 화면에서 사용자가 고른 기준 시각을 그대로 넘기면
차트가 그 시각 이후로 새지 않는다. 생략하면 서버의 현재 시:분 기준.

store_id 범위를 벗어나면 404, date/time이 조회 가능 범위 밖이면 400.

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
| `final_status` | `string` | '영업중' | '휴무추정' | '영업종료'('예외영업'은 '영업종료'로 단순화) |
| `congestion_level` | `integer` | 혼잡도 코드. 0=해당없음 | 1=여유 | 2=보통 | 3=혼잡 |
| `received_active_power_kwh` | `number \| null` | 15분 유효전력(kWh). 결측이면 null |
| `is_synthetic` | `boolean` | false=실측(2026-04-01~06-30), true=합성(2026-07-01~오늘) |
| `is_redistributed` | `boolean` | true면 이 슬롯의 전력값이 실제 15분 단위 실측이 아니라, 같은 업종 코호트의 시간 내 상대 형태를 정각 실측값에 앵커링해 추정한 값(data_resolution='1hour' 계기 중 한식 코호트가 충분한 경우에만 적용됨) |

</details>

---

## 안전감지

### `GET /api/anomalies`
**위기 감지 이벤트 목록 (관리자 화면용)**

기본값 그대로 실행하면 전체 기간(2026-04-01\~2026-07-31)의 최신 50건이 반환된다.

**안전 등급 3단계**: `일반`(평상시) / `주의`(사고가 나기에 충분한 조건 - 점검 필요) /
`위험`(실제 사고 발생 - 즉시 조치). `일반`은 "아무 규칙에도 안 걸린 상태"라 이벤트로
저장되지 않으므로, 이 목록에는 `주의`와 `위험`만 나온다. 특정 슬롯에 이벤트가 없으면
그 슬롯은 `일반`으로 해석하면 된다.

**페이징**: 응답의 `total`이 필터 조건에 걸리는 전체 건수다. 다음 페이지는
`offset`을 `limit`만큼 늘려서 다시 호출한다(예: `?limit=50&offset=50`).
`offset >= total`이면 빈 배열이 온다.

**판정 범위**: "영업종료 이후"(closed_hours)에만 판정한다 - 영업시간 중 전력이 높은 건
혼잡도(`congestion_level`)가 설명할 몫이지 안전 이슈가 아니라고 본다
(data/프로젝트개요.md 안전감지 항목).

판정 규칙과 그 전기설비 기준 근거(KEC 212 등)는 `db/docs/안전감지_이상치_판정기준.md` 참고.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `level` | query | `string` |  |  | 안전 등급 필터. 비우면 주의+위험 전부. 평상시('일반')는 이벤트로 저장되지 않으므로 이 목록에 나오지 않는다. |
| `meter_id` | query | `string` |  |  | 특정 계기만 조회. 비우면 21개 매장 전체. |
| `since` | query | `string` |  |  | 조회 시작일(포함). 데이터 범위: 2026-04-01 ~ 2026-07-31. |
| `until` | query | `string` |  |  | 조회 종료일(그날 23:59:59까지 포함). 데이터 범위: 2026-04-01 ~ 2026-07-31. |
| `limit` | query | `integer` |  |  | 한 페이지에 반환할 최대 건수. |
| `offset` | query | `integer` |  |  | 건너뛸 건수. 다음 페이지는 offset += limit. |

**응답 (200)** — `AnomalyListResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `total` | `integer` | 필터 조건에 걸리는 전체 건수(이번 페이지 건수가 아님). 페이지 수 = ceil(total / limit). |
| `limit` | `integer` | 이번 요청의 페이지 크기 |
| `offset` | `integer` | 이번 요청이 건너뛴 건수 |
| `anomalies` | `AnomalySchema[]` | 이번 페이지의 이벤트(최신순) |

<details><summary><code>AnomalySchema</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `event_id` | `integer` |  |
| `meter_id` | `string` |  |
| `store_name` | `string \| null` |  |
| `detected_at` | `string` |  |
| `level` | `string` | 안전 등급. '주의'(사고가 나기에 충분한 조건 - 점검 필요) | '위험'(실제 사고 발생 - 즉시 조치). 아무 규칙에도 안 걸린 평상시는 '일반'이며, 이벤트 자체가 생성되지 않으므로 이 목록에는 나오지 않는다. |
| `rule_triggered` | `string` | 발동한 규칙. 'kec212_overload_130pct_60min'(위험: 계약전력 130%가 60분 지속 - 위험을 만드는 유일한 규칙) | 'continuous_load_80pct_180min'(주의: 계약전력 80%가 3시간 지속) | 'empty_store_baseline_3x_60min'(주의: 매장 자신의 '진짜폐점' 시간대 baseline에서 크게 벗어나 60분 지속). 근거는 db/docs/안전감지_이상치_판정기준.md 참고. |
| `metric_value` | `number` | 실제 관측된 유효전력(kWh, 15분 슬롯 값) |
| `threshold_value` | `number` | 그 규칙이 넘어섰다고 판정한 임계치(kWh, 15분 슬롯 값) |
| `notified_at` | `string \| null` | 알림 발송 연동은 이번 범위 밖이라 항상 null |

</details>

---

## 원시 전력값

### `POST /api/meters/timeseries`
**계기 1곳의 하루 원시 전력값**

meter_id를 body로 받는다(URL에 meter_id를 노출하지 않으려고 POST를 씀).

15분 단위 전력값(kWh) + 각 슬롯의 혼잡도(정수 0~3)/영업상태를 반환한다 - 차트를
이 한 번의 호출로 그릴 수 있게 하기 위함. body에 time을 주면 그 시각 이후(미래)
슬롯은 잘라서 응답에서 제외한다.

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
| `congestion_level` | `integer` | 혼잡도 코드. 0=해당없음(영업중이 아니거나 판정 없음) | 1=여유 | 2=보통 | 3=혼잡. 차트에 그대로 시리즈로 그릴 수 있도록 문자열이 아닌 정수로 내려간다. |
| `final_status` | `string \| null` | '영업중' | '휴무추정' | '영업종료' | null(판정 없음) |
| `is_synthetic` | `boolean` |  |
| `is_redistributed` | `boolean` | true면 실제 15분 단위 실측이 아니라 코호트 비율로 추정한 값(DayStatusRow.is_redistributed와 동일 의미) |

</details>

---

### `POST /api/stores/timeseries`
**매장 1곳의 하루 원시 전력값**

store_id를 body로 받는다(URL에 store_id를 노출하지 않으려고 POST를 씀).
meter_timeseries와 동일하나 store_id(상가 기준)로 조회한다.

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
| `congestion_level` | `integer` | 혼잡도 코드. 0=해당없음(영업중이 아니거나 판정 없음) | 1=여유 | 2=보통 | 3=혼잡. 차트에 그대로 시리즈로 그릴 수 있도록 문자열이 아닌 정수로 내려간다. |
| `final_status` | `string \| null` | '영업중' | '휴무추정' | '영업종료' | null(판정 없음) |
| `is_synthetic` | `boolean` |  |
| `is_redistributed` | `boolean` | true면 실제 15분 단위 실측이 아니라 코호트 비율로 추정한 값(DayStatusRow.is_redistributed와 동일 의미) |

</details>

---
