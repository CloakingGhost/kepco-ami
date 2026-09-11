# AMI 영업유무·혼잡도·안전감지 API 레퍼런스

> 이 문서는 `scripts/generate_api_docs.py`가 FastAPI OpenAPI 스키마에서 **자동 생성**했습니다.
> 코드(엔드포인트·모델)를 고쳤다면 재생성하세요: `uv run python scripts/generate_api_docs.py`
>
> 버전: `0.1.0` · 생성 시각: `2026-09-11 04:19 UTC`

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
| `data_resolution` | `string` | '15min' | '1hour'. 현재 21개 매장 전부 '15min'이다 - 원천 계기가 1시간 적산값만 보고하는 5개 매장도 적재 단계에서 그 1시간을 이루는 15분 구간 4개로 분해했기 때문(분해된 행은 시계열의 is_redistributed=true). |

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

조회 가능 날짜 범위는 2026-04-01~2026-07-31이다. 06-30까지가 실측이고 7월은
안전감지 데모용 합성 구간이라, 7월을 조회하면 화면에서 합성 데이터임을 안내해야 한다
(원래는 실측 구간만 받았는데, 안전감지 데모가 7월에만 있어 지도와 안전감지 패널이
서로 다른 날짜를 봐야 하는 문제가 있어 넓혔다).

계기 해상도: 21개 매장 전부 15분 데이터를 가진다 - 원천 계기가 1시간 적산값만 보고하는
5개 매장도 적재 단계에서 15분 구간 4개로 분해했다(해당 값은 시계열 API의
is_redistributed=true로 구분). 매장별로 그 시점 데이터 자체가 없으면 상태 관련 필드가
전부 null이 되고 message에 안내 문구가 채워진다.

final_status는 내부 4값 중 '예외영업'을 '영업종료'로 접어 영업중/휴무추정/영업종료
3값으로만 내려준다(일반 사용자는 영업 중인지 아닌지만 판단하면 되기 때문).

congestion_level은 문자열이 아니라 정수 코드로 내려온다: 0=해당없음(영업중이
아니거나 데이터 없음) | 1=하 | 2=중 | 3=상.

date/time 형식이 잘못됐거나 date가 조회 가능 범위를 벗어나면 400.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `date` | query | `string` |  |  | 조회할 날짜, 'YY-MM-DD' 형식(연도 2자리). 범위: 26-04-01 ~ 26-07-31 (2026-06-30까지 실측, 7월은 안전감지 데모용 합성 구간). |
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
| `data_resolution` | `string` | '15min'(rows 하루 96행, 현재 전 매장) | '1hour'(결측 슬롯 판정이 생략돼 96행보다 짧을 수 있음). |
| `rows` | `DayStatusRow[]` | 15분 슬롯당 1행(하루 96행). 기준 시각 이후 슬롯은 잘려서 더 적을 수 있다. |

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
| `is_redistributed` | `boolean` | true면 이 슬롯의 전력값은 계기가 15분 단위로 직접 잰 값이 아니라, 원천의 1시간 적산값을 그 1시간을 이루는 15분 구간 4개에 전압×전류 비율로 나눠 담은 값이다(4개 합은 실측 적산값과 일치). 원천이 1시간 적산만 보고하는 5개 매장의 모든 슬롯이 해당하고, 전압·전류가 없는 1개 매장은 4등분. |

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

### `GET /api/anomalies/snapshot`
**특정 시점 위기 감지 스냅샷 (화면 표시용)**

화면에 즉시 띄울 매장만 골라 돌려주는 스냅샷 API - 관리자용 전체 이력 조회(`GET /api/anomalies`)와
달리, 아래 두 상황 중 하나에 걸린 매장만 `alerts`에 담는다. 둘 다 아니면(대부분의 매장·시각)
응답은 `count=0`, `alerts=[]`다.

1. **즉시위험**: 조회 시점의 슬롯에 `위험` 이벤트가 있는 매장(`kec212_overload_130pct_60min`).
2. **주의반복**: 조회 시점 기준 최근 24시간 안에 서로 다른 `주의` 사건(슬롯 간격이 15분을
   넘으면 별개 사건으로 취급)이 3번 이상 있는 매장. 사건 하나가 이미 여러 슬롯(행)에 걸치므로
   원시 행 개수가 아니라 사건 개수로 센다 - 자세한 근거는 `db/docs/안전감지_이상치_판정기준.md` 참고.

date/time을 둘 다 생략하면 서버 "현재" 기준(`ami_db.serving.service_now`)으로 조회한다 -
단, 실측 데이터는 2026-06-30에서 끝나고 위험/주의 데모는 7월 합성 구간에만 있으므로, 데모
확인 목적이라면 위 예시값(위험: 26-07-02 02:15, 주의반복: 26-07-22 06:00)으로 직접 지정해서
호출해야 한다.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `date` | query | `string` |  |  | 조회할 날짜, 'YY-MM-DD' 형식(연도 2자리). 범위: 26-04-01 ~ 26-07-31. 생략하면 time과 무관하게 서버의 현재 날짜를 쓴다. |
| `time` | query | `string` |  |  | 조회할 시각, 'HH:MM' 형식(00:00~23:45, 15분 단위만 허용: 00/15/30/45). 생략하면 date와 무관하게 서버의 현재 시:분(15분 단위로 내림)을 쓴다. |

**응답 (200)** — `AnomalySnapshotResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `date` | `string` | 조회 기준 날짜 'YY-MM-DD'. 생략 시 서버 현재 날짜 |
| `time` | `string` | 조회 기준 시각 'HH:MM'. 생략 시 서버 현재 시:분(15분 단위로 내림) |
| `count` | `integer` | alerts 배열 길이 |
| `alerts` | `AnomalySnapshotAlert[]` | 화면에 표시해야 할 매장 목록. 위험/주의 어느 쪽에도 안 걸리는 매장(대부분)은 나오지 않는다. |

<details><summary><code>AnomalySnapshotAlert</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `store_id` | `integer` | stores.store_id |
| `store_name` | `string \| null` |  |
| `meter_id` | `string` |  |
| `level` | `string` | '주의' | '위험' |
| `trigger_reason` | `string` | '즉시위험'(조회 시점 슬롯에 위험 이벤트 존재) | '주의반복'(최근 24시간 내 서로 다른 주의 사건이 3회 이상) |
| `rule_triggered` | `string` | 가장 최근에 발동한 규칙. 상세는 db/docs/안전감지_이상치_판정기준.md 참고. |
| `detected_at` | `string` | '즉시위험'은 조회 시점 슬롯, '주의반복'은 최근 사건의 슬롯 |
| `metric_value` | `number` | detected_at 시점에 실제 관측된 유효전력(kWh, 15분 슬롯 값) |
| `threshold_value` | `number` | 그 규칙이 넘어섰다고 판정한 임계치(kWh, 15분 슬롯 값) |
| `repeat_count` | `integer \| null` | '주의반복'일 때만 값이 있음 - 최근 24시간 내 서로 다른 주의 사건(episode) 개수. 슬롯(15분) 원시 행 개수가 아니다(사건 하나도 여러 슬롯에 걸쳐 여러 행으로 남으므로). |

</details>

---

### `GET /api/anomalies/period`
**그 달 1일부터 지정 시점까지 누적 조회**

**그 달 1일 00:00부터 요청한 시점까지**의 안전감지 이력을 매장별로 묶어서 돌려준다.

스냅샷(`/api/anomalies/snapshot`)은 "지금 이 15분 슬롯"만 보기 때문에 사건이 없는
시각을 고르면 늘 비어 있다. 이 엔드포인트는 누적 구간을 보므로 "이번 달에 어느
매장에 무슨 일이 있었는지"를 한 번에 확인할 수 있다.

예: `?date=26-05-13&time=16:00` -> **2026-05-01 00:00 ~ 2026-05-13 16:00** 구간

**사건(episode) 단위로 묶어서 준다.** `anomaly_events`는 15분 슬롯당 1행이라 60분짜리
사건 하나가 4행으로 남는데, 그대로 내려보내면 화면에서 같은 사건이 네 번 반복되는
것처럼 보인다. 슬롯 간격이 15분을 넘거나 등급·규칙이 바뀌면 다른 사건으로 끊는다.

매장 정렬은 **위험이 있는 매장 먼저, 그다음 건수 많은 순**이라 앞에서부터 그리면 된다.

**파라미터**

| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |
|---|---|---|:---:|---|---|
| `date` | query | `string` |  |  | 조회 기준 날짜 'YY-MM-DD'. 범위: 26-04-01 ~ 26-07-31. 생략하면 서버 현재 날짜. |
| `time` | query | `string` |  |  | 조회 기준 시각 'HH:MM'(15분 단위). 생략하면 서버 현재 시:분. |

**응답 (200)** — `AnomalyPeriodResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `date` | `string` | 조회 기준 날짜(입력값 또는 서버 현재) |
| `time` | `string` |  |
| `from_ts` | `string` | 그 달 1일 00:00 |
| `to_ts` | `string` | 조회 기준 시점 |
| `store_count` | `integer` | 기간 내 이벤트가 있었던 매장 수 |
| `total_events` | `integer` |  |
| `danger_count` | `integer` |  |
| `caution_count` | `integer` |  |
| `stores` | `AnomalyPeriodStore[]` | 매장별 누적 결과. 위험이 있는 매장이 먼저, 그다음 건수가 많은 순. |

<details><summary><code>AnomalyPeriodStore</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `store_id` | `integer` |  |
| `store_name` | `string \| null` |  |
| `meter_id` | `string` |  |
| `event_count` | `integer` | 기간 내 슬롯 단위 이벤트 수 |
| `danger_count` | `integer` |  |
| `caution_count` | `integer` |  |
| `episode_count` | `integer` | 연속 슬롯을 하나로 묶은 사건 수 |
| `latest_level` | `string` |  |
| `latest_detected_at` | `string` |  |
| `episodes` | `AnomalyEpisode[]` | 시간순 사건 목록 |

</details>

---

### `POST /api/anomalies/explain`
**AI 분석 요청 (저장본이 있으면 즉시 반환, 없으면 생성 시작)**

감지 이벤트에 대한 AI 분석(점주 문자 / 점검 사유 / 신고 초안 / 대처방안 / 문의 초안)을
**요청한다.** 분석은 사용자가 요청했을 때만 만들고, 만든 결과는 DB(`anomaly_narrations`)에
저장해 다음부터는 그대로 읽는다. 감지된 이벤트마다 미리 만들어 두지 않는다.

- 이미 분석된 이벤트 -> `status='done'`과 저장된 결과를 **즉시** 반환(AI를 다시 부르지 않음)
- 처음 요청 -> 백그라운드에서 생성을 시작하고 즉시 `status='pending'`으로 응답.
  완료 여부는 `POST /api/anomalies/explain/status`로 확인한다.
- 이전 시도가 실패했으면(`failed`) 다시 요청할 때 새로 시도한다.

**AI 실패는 HTTP 에러가 아니다.** 외부 모델이 죽었거나 키가 없어도 200과 함께
`status='failed'`와 화면에 그대로 띄울 안내문(`message`)을 준다. 404는 해당
(store_id, detected_at) 감지 이벤트 자체가 없을 때뿐이다.

**LLM은 판정에 관여하지 않는다.** 등급(`level`)과 발동 규칙(`rule_triggered`)은 규칙이
이미 확정한 값이고(규칙 F1 0.698 vs Isolation Forest 0.582, KEC 212.3 법정 근거),
수치는 클라이언트 입력이 아니라 `anomaly_events`에서 다시 읽는다. 생성된 문장의 숫자는
입력 수치와 자동 대조해 **검증을 통과한 결과만 저장한다.**

**응답 (200)** — `AnomalyExplainResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `status` | `string` | none=요청된 적 없음 | pending=AI가 생성 중(백그라운드) | done=완료(DB 저장본) | failed=외부 AI 실패 또는 사용 불가(다시 요청 가능). **AI 실패는 HTTP 에러가 아니라 이 값으로 온다.** |
| `message` | `string \| null` | 화면에 그대로 띄울 안내문(none/pending/failed일 때). done이면 null. |
| `store_id` | `integer` |  |
| `store_name` | `string \| null` |  |
| `detected_at` | `string` |  |
| `level` | `string` | '주의' | '위험' - 규칙이 이미 확정한 등급(LLM이 바꾸지 않음) |
| `rule_triggered` | `string` |  |
| `owner_sms` | `string \| null` | 점주에게 보낼 문자 초안(2~3문장, 존댓말, 일상어). status='done'일 때만 채워진다. |
| `admin_note` | `string \| null` | 관리자용 점검 사유(1~2문장, 발동 규칙과 근거 수치 명시). status='done'일 때만 채워진다. |
| `emergency_report` | `string \| null` | 신고 접수용 초안. level='위험'일 때만 채워지고 '주의'면 null. |
| `next_steps` | `string[]` | 점주가 지금 할 수 있는 조치 2~3가지. '감지했다'로 끝내지 않고 '그래서 뭘 하면 되는지'까지 안내하기 위한 필드. 근거는 프롬프트에 넣어둔 참고 안내사항(한전 증설 제도 등)뿐이며 모델이 제도를 지어내지 못한다. |
| `inquiry_draft` | `string \| null` | 점주가 한전(고객센터 123 / cyber.kepco.co.kr)이나 전기공사 업체에 그대로 보낼 수 있는 문의 초안. |
| `verification_passed` | `boolean \| null` | 생성문에 입력에 없던 숫자가 섞였는지 자동 대조한 결과(판정 로직이 아니라 LLM 출력 검사). 서버는 검증을 통과한 결과만 저장하므로 status='done'이면 항상 true. |
| `unknown_numbers` | `string[]` | 입력 수치와 대조되지 않은 숫자 목록. 비어 있으면 통과. |
| `model` | `string \| null` | 생성에 사용한 모델 |
| `elapsed_ms` | `integer \| null` | AI 생성 소요 시간(ms) |
| `requested_at` | `string \| null` | 분석을 요청한 시각 |
| `completed_at` | `string \| null` | 분석이 끝난 시각(done일 때) |

---

### `POST /api/anomalies/explain/status`
**AI 분석 상태 조회 (새로 생성하지 않음)**

AI 분석의 현재 상태를 **읽기만** 한다 - 여기서는 절대 생성을 시작하지 않는다.
`POST /api/anomalies/explain`이 `pending`을 돌려줬을 때 완료를 확인하는 용도다.

`status`: `none`(요청된 적 없음) | `pending`(생성 중) | `done`(완료, 결과 포함) |
`failed`(실패 - 다시 요청 가능). 생성 중 서버가 재기동돼 작업이 사라진 경우도
일정 시간(`NARRATION_STALE_MINUTES`)이 지나면 `failed`로 보여 다시 요청할 수 있다.

**응답 (200)** — `AnomalyExplainResponse`

| 필드 | 타입 | 설명 |
|---|---|---|
| `status` | `string` | none=요청된 적 없음 | pending=AI가 생성 중(백그라운드) | done=완료(DB 저장본) | failed=외부 AI 실패 또는 사용 불가(다시 요청 가능). **AI 실패는 HTTP 에러가 아니라 이 값으로 온다.** |
| `message` | `string \| null` | 화면에 그대로 띄울 안내문(none/pending/failed일 때). done이면 null. |
| `store_id` | `integer` |  |
| `store_name` | `string \| null` |  |
| `detected_at` | `string` |  |
| `level` | `string` | '주의' | '위험' - 규칙이 이미 확정한 등급(LLM이 바꾸지 않음) |
| `rule_triggered` | `string` |  |
| `owner_sms` | `string \| null` | 점주에게 보낼 문자 초안(2~3문장, 존댓말, 일상어). status='done'일 때만 채워진다. |
| `admin_note` | `string \| null` | 관리자용 점검 사유(1~2문장, 발동 규칙과 근거 수치 명시). status='done'일 때만 채워진다. |
| `emergency_report` | `string \| null` | 신고 접수용 초안. level='위험'일 때만 채워지고 '주의'면 null. |
| `next_steps` | `string[]` | 점주가 지금 할 수 있는 조치 2~3가지. '감지했다'로 끝내지 않고 '그래서 뭘 하면 되는지'까지 안내하기 위한 필드. 근거는 프롬프트에 넣어둔 참고 안내사항(한전 증설 제도 등)뿐이며 모델이 제도를 지어내지 못한다. |
| `inquiry_draft` | `string \| null` | 점주가 한전(고객센터 123 / cyber.kepco.co.kr)이나 전기공사 업체에 그대로 보낼 수 있는 문의 초안. |
| `verification_passed` | `boolean \| null` | 생성문에 입력에 없던 숫자가 섞였는지 자동 대조한 결과(판정 로직이 아니라 LLM 출력 검사). 서버는 검증을 통과한 결과만 저장하므로 status='done'이면 항상 true. |
| `unknown_numbers` | `string[]` | 입력 수치와 대조되지 않은 숫자 목록. 비어 있으면 통과. |
| `model` | `string \| null` | 생성에 사용한 모델 |
| `elapsed_ms` | `integer \| null` | AI 생성 소요 시간(ms) |
| `requested_at` | `string \| null` | 분석을 요청한 시각 |
| `completed_at` | `string \| null` | 분석이 끝난 시각(done일 때) |

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
| `data_resolution` | `string` | '15min'(rows 하루 96행, 현재 전 계기) | '1hour'(96행보다 짧을 수 있음). |
| `rows` | `TimeseriesRow[]` | 15분 슬롯당 1행(하루 96행). 기준 시각 이후 슬롯은 잘려서 더 적을 수 있다. |

<details><summary><code>TimeseriesRow</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `ts` | `string` |  |
| `received_active_power_kwh` | `number \| null` |  |
| `congestion_level` | `integer` | 혼잡도 코드. 0=해당없음(영업중이 아니거나 판정 없음) | 1=여유 | 2=보통 | 3=혼잡. 차트에 그대로 시리즈로 그릴 수 있도록 문자열이 아닌 정수로 내려간다. |
| `final_status` | `string \| null` | '영업중' | '휴무추정' | '영업종료' | null(판정 없음) |
| `is_synthetic` | `boolean` |  |
| `is_redistributed` | `boolean` | DayStatusRow.is_redistributed와 같은 의미 - 원천의 1시간 적산값을 15분 구간으로 나눠 담은 값이면 true |

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
| `data_resolution` | `string` | '15min'(rows 하루 96행, 현재 전 계기) | '1hour'(96행보다 짧을 수 있음). |
| `rows` | `TimeseriesRow[]` | 15분 슬롯당 1행(하루 96행). 기준 시각 이후 슬롯은 잘려서 더 적을 수 있다. |

<details><summary><code>TimeseriesRow</code> 필드 상세</summary>

| 필드 | 타입 | 설명 |
|---|---|---|
| `ts` | `string` |  |
| `received_active_power_kwh` | `number \| null` |  |
| `congestion_level` | `integer` | 혼잡도 코드. 0=해당없음(영업중이 아니거나 판정 없음) | 1=여유 | 2=보통 | 3=혼잡. 차트에 그대로 시리즈로 그릴 수 있도록 문자열이 아닌 정수로 내려간다. |
| `final_status` | `string \| null` | '영업중' | '휴무추정' | '영업종료' | null(판정 없음) |
| `is_synthetic` | `boolean` |  |
| `is_redistributed` | `boolean` | DayStatusRow.is_redistributed와 같은 의미 - 원천의 1시간 적산값을 15분 구간으로 나눠 담은 값이면 true |

</details>

---
