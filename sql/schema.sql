-- ============================================================
-- AMI-상가 매칭 DB 스키마 (화곡동 파일럿, A선로 한정)
--
-- ENUM 대신 CHECK 제약을 채택한 이유: 이 프로젝트는 공모전 데모라 상태 라벨
-- ('영업중'/'휴무추정' 등)이 발표 준비 과정에서 바뀔 가능성이 실무적으로 높다.
-- Postgres ENUM은 값 추가는 쉽지만 삭제/이름변경이 번거롭고(참조 객체를 전부
-- 다시 만져야 함) psycopg2/pandas 적재 시 커스텀 타입 어댑터도 신경 써야 한다.
-- CHECK는 그냥 TEXT라서 pandas to_sql/psycopg2 execute_values가 별도 처리
-- 없이 그대로 꽂히고, 값 목록을 바꿀 때도 ALTER TABLE ... DROP/ADD CONSTRAINT
-- 한 번이면 된다. 값 유효성 보장이라는 본래 목적은 CHECK로도 동일하게 충족된다.
-- ============================================================

-- 1) 계기 마스터
-- 스코프: A선로 전체 64개 계기(상업용/비상업용, 매칭 성공 여부 무관하게 전부 적재).
-- 이유: match_status 컬럼으로 "매칭 안 된 계기가 왜 안 됐는지"까지 테이블 자체에서
-- 드러나게 하기 위함 (예: eligible_unmatched=화곡동에 KSIC 일치 상가가 없었음).
CREATE TABLE IF NOT EXISTS meters (
    meter_id            TEXT PRIMARY KEY,               -- 예: 'A-L-58' (step1_meta_classified.csv의 meter_id, 총괄표<->시계열 조인키)
    line_name            TEXT NOT NULL,                  -- 선로명. 이번 적재는 'A'만 대상
                                                          -- (파일럿 선로 확정 사유는 data/03_지역특성_규모_업종_분석보고서.md 참고:
                                                          --  상업용 87.5%, 소규모 68.8%, "먹자골목·사무실 밀집형" 성격)
    section_no           TEXT,                           -- 구간번호. *** 배전선로상의 상대 위치 순번일 뿐 실제 지리 좌표가 아님 -
                                                          -- "지역"으로 오인해 위치 추정에 쓰지 말 것 (원본 데이터엔 주소/좌표 필드 자체가 없음) ***
    supply_type           TEXT,                           -- 공급방식 원문(예: '840 삼상4선(22.9kV-y)')
    contract_power_kw     DOUBLE PRECISION,               -- 수전전력(kW, 계약전력). 매장매칭 스케일 배제 규칙(<50kW)의 기준값이자
                                                          -- 혼잡도 이용률(%) = 실측kWh/contract_power_kw 계산의 분모
    contract_type         TEXT,                           -- 계약종별 원문 (요금 분석용, 이번 범위(영업유무/혼잡도)엔 직접 쓰이지 않음)
    usage_purpose         TEXT,                           -- 사용용도 원문('02 상업용' 등) - 매장매칭 적격 판정의 1차 조건
    multiplier            DOUBLE PRECISION,               -- 배수(변성기 배수).
                                                          -- *** 절대 재곱 금지: meter_timeseries의 kWh 값에는 이미 이 배수가 반영된
                                                          -- 최종값이 들어있다 (원본 한전 PDF 근거, data/04_AMI컬럼_외부API_필드_선정_분석보고서.md
                                                          -- 1-1절에 문서 간 모순 정정 내역 기록됨 - 데이터_컬럼_완전_설명서.md의 "재곱하라"는
                                                          -- 서술은 원본과 모순되는 오류이므로 따르지 말 것). 이 컬럼은 규모 프록시/정합성
                                                          -- 검증용으로만 보관하고 계산에 재사용하지 않는다. ***
    has_der               TEXT,                           -- 전기차/분산형 여부 원문(대부분 공백, 드물게 'PPA/태양광(299kW)' 등)
    main_product           TEXT,                           -- 주생산품 원문
    ksic_code               TEXT,                           -- 산업분류 원문(5자리 숫자 접두 + 한글 라벨, 예 '68112 비주거용 건물 임대업')
    category_class           TEXT,                           -- 카테고리분류 (02_match_store.py category_class() 파생 로직과 동일)
    power_class               TEXT,                           -- 전력분류: 규모x전압 조합 (02_match_store.py power_class() 파생 로직과 동일)
    data_completeness         NUMERIC(5, 2),                  -- 실측 구간 결측률(%). meter_summary.csv 원본값을 그대로 믿지 않고
                                                              -- 02_load_meta_and_timeseries.py가 timeseries_clean.pkl 실측 min/max로
                                                              -- 그리드 크기를 동적 계산해 재산출한다(8736 고정 분모는 실제 91일 range와
                                                              -- 어긋남 - 아래 data_resolution 조사 과정에서 확인).
    data_resolution            TEXT NOT NULL DEFAULT '15min'
        CHECK (data_resolution IN ('15min', '1hour')),
        -- '1hour': 이 계기는 recv_kWh가 매시 정각에만 리포트되고 15/30/45분은 항상 NULL/결측
        -- (91일 전체 예외 0건 확인 - 무작위 결측이 아니라 계기 통신 사양 차이). A/B/C 선로
        -- 전체 129개 중 27개(21%)에서 재현되는 계기군 특성. 매장매칭된 21개 중 5개
        -- (A-L-16/19/49/58/70)가 여기 해당하며, 완전성(위 data_completeness)이 25%로 낮게 찍히는
        -- 근본 원인이 바로 이것이다. 프론트/API 소비자가 "결측"과 "원래 이 해상도"를 구분할 수
        -- 있도록 숨기지 않고 명시한다(해상도를 억지로 15분으로 부풀리지 않는다는 원칙).
    match_status               TEXT NOT NULL DEFAULT 'ineligible'
        CHECK (match_status IN ('matched', 'eligible_unmatched', 'ineligible')),
        -- matched            : 화곡동 상가와 실제 1:1 매칭됨 (stores 테이블에 대응 행 존재)
        -- eligible_unmatched : 매장매칭 적격(상업용/저압/<50kW)이었지만 화곡동에 KSIC 일치 후보가
        --                      없었거나, 같은 KSIC 코드를 공유하는 다른 계기에게 후보 풀이 먼저 소진됨
        -- ineligible         : 애초에 매장매칭 부적격(비상업용/특고압/중규모 이상)
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2) 15분 단위 시계열
-- 스코프: 화곡동에 실제 매칭된 계기(match_status='matched', 약 21개)로 한정.
-- 이유: 나머지 계기는 운영시간 정보가 없어 영업유무/혼잡도 판정 자체가 정의되지 않고,
-- 스코프를 좁히면 데이터량이 21개 x 약 155일 x 96슬롯 ≈ 31만행으로 가벼워져 반복 실행이 빨라진다.
CREATE TABLE IF NOT EXISTS meter_timeseries (
    meter_id                      TEXT NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
    ts                             TIMESTAMP NOT NULL,        -- 원본 AMI 시각(KST, tz 정보 없음) 그대로 사용 - tz 변환 불필요(원본 자체가 로컬시)
    received_active_power_kwh     DOUBLE PRECISION,          -- recv_kWh("유효전력"). 이미 배수가 반영된 최종값 - 절대 재곱하지 말 것(위 multiplier 주석 참고)
    generated_active_power_kwh    DOUBLE PRECISION,          -- gen_kWh. 대부분 NULL(전체 계기의 86.8%가 태양광 등 분산전원 없음)
    voltage_a  DOUBLE PRECISION,
    voltage_b  DOUBLE PRECISION,
    voltage_c  DOUBLE PRECISION,
    current_a  DOUBLE PRECISION,
    current_b  DOUBLE PRECISION,
    current_c  DOUBLE PRECISION,
    is_synthetic                   BOOLEAN NOT NULL,          -- false=실측(2026-04-01~06-30), true=합성(2026-07-01~오늘)
                                                              -- 날짜별 서빙 로직(ami_db.serving)은 "이 날짜가 실측이냐 합성이냐"를 판단하는
                                                              -- 별도 분기 코드가 전혀 없다 - 이 플래그만 그대로 읽어서 응답에 실어 보내면 끝난다.
    is_redistributed                BOOLEAN NOT NULL DEFAULT FALSE,
                                                              -- true = 이 행의 recv_kWh는 정각 실측이 아니라, 같은 업종 코호트의
                                                              -- 시간 내 상대 형태(hourly ratio table)를 그 시간대 정각 실측값에
                                                              -- 앵커링해 추정한 값(resolution.redistribute_hourly_store 참고).
                                                              -- data_resolution='1hour'인 계기 중 A-L-58(한식 코호트 충분)에만
                                                              -- 적용되고, 원래 행이 아예 없던 15/30/45분 슬롯도 이 값이 true인 채로
                                                              -- 새로 생성된다. 나머지 4개 1hour 계기는 이 재분배를 적용하지 않고
                                                              -- 결측을 결측 그대로(NULL) 남긴다(해상도 플래그만으로 투명화).
    PRIMARY KEY (meter_id, ts)
);
-- (meter_id, ts) PK 자체가 "특정 계기의 날짜범위 조회"에 이미 최적 인덱스라 별도 복합 인덱스는 불필요.
-- ts 단독 인덱스는 "특정 시각의 전 매장 스냅샷"(혼잡도 대시보드용 조회)을 위해 추가.
CREATE INDEX IF NOT EXISTS idx_meter_timeseries_ts ON meter_timeseries (ts);

-- 3) 화곡동 재매칭 결과 (계기 1개 : 상가 1개, 02_match_store.py와 동일한 무작위 비복원추출 규칙 재현)
CREATE TABLE IF NOT EXISTS stores (
    store_id             SERIAL PRIMARY KEY,
    meter_id              TEXT NOT NULL UNIQUE REFERENCES meters(meter_id) ON DELETE CASCADE,
    name                   TEXT NOT NULL,                 -- 상호명
    branch_name            TEXT,                           -- 지점명 (결측 다수, nullable)
    road_address            TEXT,                           -- 도로명주소
    building_name            TEXT,                           -- 건물명 (원본 결측 다수, nullable)
    floor_info                TEXT,                           -- 층정보 (원본 결측 다수, nullable)
    longitude                 DOUBLE PRECISION,              -- 경도. 원본 CSV엔 문자열로 들어있어 로더에서 CAST 필요
    latitude                  DOUBLE PRECISION,              -- 위도
    biz_category_large        TEXT,                           -- 상권업종대분류명
    biz_category_mid          TEXT,                           -- 상권업종중분류명
    ksic_code                  TEXT,                           -- 표준산업분류코드 원문(알파벳 대분류 접두 포함, 예 'I56111')
    dong_name                  TEXT NOT NULL DEFAULT '화곡동', -- 이번 파일럿에서 선택한 단일 동(고정값). 법정동명 기준(행정동명은 화곡1~8동으로 쪼개져 단일값이 아님)
    match_note                  TEXT NOT NULL DEFAULT
        '업종코드 기반 통계적 근사 매칭이며 실제 매장 확인 매칭이 아니고, 지역(동)도 데모 일관성을 위해 임의 지정된 것으로 AMI 데이터 자체의 실제 위치가 아님',
        -- AMI 원본에는 주소/좌표 필드가 전혀 없다(data/03_지역특성_규모_업종_분석보고서.md 0장).
        -- 이 문구는 UI/발표 자료에서 "실제 매장 위치"인 것처럼 오인되지 않도록 데이터 자체에 박아두는 안전장치.
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_stores_dong_name ON stores (dong_name);

-- 4) 운영시간 (source별 이력 보존 - google_places 매칭이 성공해도 ksic_estimate 행은 지우지 않는다.
--    PK가 (store_id, source, day_of_week)라서 두 source가 같은 요일에 대해 자연스럽게 공존한다.
--    "추정치인지 실측인지" 구분이 필요할 때 source 컬럼만 보면 되므로 별도 플래그가 필요 없다.)
CREATE TABLE IF NOT EXISTS store_operating_hours (
    store_id         INTEGER NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
    source            TEXT NOT NULL CHECK (source IN ('ksic_estimate', 'google_places')),
    day_of_week       SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),  -- 0=월요일 ... 6=일요일
    open_time          TIME,             -- is_closed=true인 요일은 NULL 허용
    close_time          TIME,
    is_closed            BOOLEAN NOT NULL DEFAULT FALSE,
    is_24h                BOOLEAN NOT NULL DEFAULT FALSE,   -- true면 open_time=00:00/close_time=23:59로 채워 range 쿼리 편의성 확보
    raw_hours_text        TEXT,           -- google_places 원문 한 줄(예: '월요일: 10:00 ~ 22:00'). ksic_estimate 소스는 NULL
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, source, day_of_week)
);

-- 5) 운영상태 판정 결과 (15분 슬롯마다 1행 - meter_timeseries와 동일 그리드)
CREATE TABLE IF NOT EXISTS store_operating_status (
    store_id             INTEGER NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
    ts                     TIMESTAMP NOT NULL,           -- meter_timeseries.ts와 동일 그리드
    schedule_status         TEXT NOT NULL CHECK (schedule_status IN ('open_hours', 'closed_hours')),
        -- 조회 시각을 store_operating_hours(google_places 우선, 없으면 ksic_estimate)와 비교한 결과
    power_status              TEXT NOT NULL CHECK (power_status IN ('active', 'low')),
        -- 해당 계기의 야간(0~5시) baseline 전력과 현재 전력을 비교한 결과
        -- (야간 baseline이 매우 안정적이라는 근거: data/03_지역특성_규모_업종_분석보고서.md 5-3절)
    final_status               TEXT NOT NULL CHECK (final_status IN ('영업중', '휴무추정', '예외영업', '영업종료')),
        -- 매트릭스: open_hours+active=영업중 / open_hours+low=휴무추정(프로젝트개요.md 핵심 판정 케이스:
        -- "영업중 전력량이 영업종료 시간과 같을시 영업 종료로 판단") / closed_hours+active=예외영업(심야영업 등) / closed_hours+low=영업종료
    congestion_level            TEXT CHECK (congestion_level IN ('상', '중', '하')),
        -- final_status가 '영업중'이 아니면 혼잡도 자체가 의미 없으므로 NULL 허용
    computed_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_store_operating_status_ts ON store_operating_status (ts);

-- 6) 안전감지 위기 감지 이벤트 (2차 우선순위 기능)
CREATE TABLE IF NOT EXISTS anomaly_events (
    event_id             BIGSERIAL PRIMARY KEY,
    meter_id               TEXT NOT NULL REFERENCES meters(meter_id) ON DELETE CASCADE,
    detected_at              TIMESTAMP NOT NULL,          -- 감지된 15분 슬롯의 ts
    level                     TEXT NOT NULL CHECK (level IN ('일반', '주의', '위험')),
        -- 안전 등급 3단계. 의미:
        --   일반 = 평상시(아무 규칙에도 걸리지 않은 상태)
        --   주의 = 아직 사고는 아니지만 사고가 나기에 충분한 조건 - 점검/관리 대상
        --   위험 = 실제로 전기사고가 발생한(발생 중인) 상황 - 즉시 조치 대상
        -- *** '일반'은 CHECK에는 있지만 이 테이블에 행으로 저장되지 않는다. ***
        -- 이 테이블은 "사건 기록부"라서 아무 일도 없었다는 사실까지 15분마다 적으면
        -- 21개 매장 x 4개월이 27만행 전부 '일반'으로 채워질 뿐 정보량이 0이다.
        -- 그래서 저장은 주의/위험만 하고, 조회 시 해당 슬롯에 행이 없으면 '일반'으로
        -- 해석한다(ami_db.serving의 안전 등급 조회 로직이 이 규칙을 구현한다).
        -- CHECK에 값을 남겨두는 이유는 3단계라는 도메인 어휘를 스키마에 명시하고,
        -- 나중에 "점검했고 정상이었다"를 명시적으로 기록할 필요가 생기면 바로 쓰기 위함.
    rule_triggered             TEXT NOT NULL,
        -- 어떤 규칙이 발동했는지. 값과 근거는 db/src/ami_db/anomaly.py 모듈 docstring 참고:
        --   'kec212_overload_130pct_60min'  (위험) 계약전력 130%가 60분 지속 - KEC 212.3
        --                                    표 212.3-2(산업용 배선차단기, 상업용 계기 적용).
        --                                    "위험"을 만드는 유일한 규칙 - 통계 기반 규칙은
        --                                    위험을 만들지 않는다(아래 참고).
        --   'continuous_load_80pct_180min'  (주의) 계약전력 80%가 3시간 지속 - 연속부하 80% 규칙
        --   'empty_store_baseline_3x_60min' (주의) 매장 자신의 "진짜폐점"(영업시간표가 아니라
        --                                    그 계기 자신의 (요일,슬롯) 이력으로 판별한, 평소
        --                                    조용하고 안정적인 시간) baseline에서 Tukey outer
        --                                    fence(3xIQR) 이상 벗어나 60분 지속
        -- 통계적 이상치(Tukey 등)를 그대로 등급에 매핑하지 않는 이유, 그리고 "위험"을
        -- 물리 기준 1개로만 좁힌 이유는 anomaly.py docstring 참고(실측 3개월에서 위험
        -- 4,111건 -> 통계+등급 결합 재설계 -> 그래도 개인화 규칙에 위험을 남기면 냉동고
        -- 압축기 같은 정상 주기 활동이 63건씩 잡히는 문제가 재발해 최종적으로 위험을
        -- KEC 규칙 전용으로 뺐다).
    metric_value               DOUBLE PRECISION NOT NULL,   -- 실제 관측된 recv_kWh
    threshold_value              DOUBLE PRECISION NOT NULL,   -- 그 규칙이 넘어섰다고 판정한 임계치(kWh)
    notified_at                   TIMESTAMPTZ,                 -- 알림 발송 연동은 이번 범위 밖. 항상 NULL이어도 무방
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_anomaly_events_meter_detected ON anomaly_events (meter_id, detected_at);

-- 7) Google Places 원본 응답 캐시 (재호출 없이 재사용 가능하게 원문 보관)
CREATE TABLE IF NOT EXISTS google_places_cache (
    store_id                   INTEGER NOT NULL REFERENCES stores(store_id) ON DELETE CASCADE,
    fetched_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    place_id                      TEXT,        -- 이번에 쓰는 /hours 엔드포인트(PlaceHoursOnlySchema) 응답엔 place_id 필드 자체가 없음 - 항상 NULL로 시작
    name                           TEXT,        -- 응답의 '매장명' 값
    hours_raw                       TEXT[],      -- 운영시간 원문 배열(예: ['월요일: 10:00 ~ 22:00', ...])
    open_now                         BOOLEAN,
    business_status_code              TEXT,        -- /hours 응답엔 없음(NULL) - 필요 시 전체정보 엔드포인트를 별도로 호출해야 채워짐
    business_status_label              TEXT,
    raw_response_json                   JSONB NOT NULL,   -- HTTP 응답 원문 전체(재파싱/디버깅용, 응답 스키마가 바뀌어도 원본은 항상 보존됨)
    PRIMARY KEY (store_id, fetched_at)
);
