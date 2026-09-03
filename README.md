# db/ — AMI-상가 매칭 PostgreSQL 파이프라인

`places-api-project`와 동일한 패턴(uv 관리 서브프로젝트, 독립 `.venv`/`pyproject.toml`/`uv.lock`)으로 만들어졌다.

## ~~왜 별도 서브프로젝트인가~~ 현재는 두 프로젝트를 하나로 합침

이번 작업에 필요한 의존성(psycopg2, SQLAlchemy)은 기존 `visualize_analyis_data/scripts` 파이프라인(pandas/numpy만 있으면 됨)과 성격이 다르고, `places-api-project`는 Google Places 전용이라 DB 코드를 거기 얹으면 관심사가 섞인다. 레포에 이미 확립된 "서브프로젝트 = 독립 uv 환경" 컨벤션을 그대로 따랐다.

## 실행 순서

```bash
# 0) Postgres 컨테이너 (이 프로젝트 자체 compose 파일 없음 - places-api-project 것을 재사용)
docker compose -f ../places-api-project/docker-compose.yml up -d

uv sync
cp .env.example .env   # 필요 시 값 수정 (기본값이 이미 compose 설정과 일치)

uv run python scripts/00_rematch_store_hwagokdong.py
uv run python scripts/01_create_schema.py --reset
uv run python scripts/02_load_meta_and_timeseries.py

uv run python scripts/04_sync_google_places_hours.py

uv run python scripts/06_generate_synthetic_timeseries.py
uv run python scripts/07_load_synthetic_timeseries.py

uv run python scripts/08_serve_day_cli.py --meter-id A-L-58 --date 2026-08-15
uv run python scripts/09_compute_operating_status.py
uv run python scripts/10_detect_anomalies.py

# 조회용 API 서버 (포트 8000 - 유일한 런타임 서버, 아래 "API 서버" 참고)
uv run uvicorn app.serving_api:app --reload
# Swagger UI: http://localhost:8000/docs (파라미터마다 예시값이 채워져 있어 바로 Try it out 가능)
# 정적 레퍼런스: docs/API_REFERENCE.md (코드 수정 시 재생성: uv run python scripts/generate_api_docs.py)
```

## API 서버

`db/app/serving_api.py`(포트 8000)가 유일한 런타임 API 서버다. `data/프로젝트개요.md` 개발우선순위 1번(영업유무·혼잡도)을 완전히 커버하고, 2번(안전감지)은 "이상치 조회"까지만 커버(알림 문자 발송은 범위 밖 - `notified_at` 항상 NULL). 3번(전력관리 효율화)은 팀 분석 보고서에서 이미 보류 확정, 4번(SNS)은 미구현.

Google Places 호출은 04단계 배치 스크립트 실행 중에만 인프로세스로 발생한다(`src/ami_db/places_client.py`, `places_sync.py`) - 별도 HTTP 서버나 두 번째 포트가 필요하지 않다. `places-api-project`(포트 8000)는 더 이상 이 파이프라인에 관여하지 않으며 아카이브 상태로만 보존된다(포팅 경위는 그 프로젝트의 README 참고).

## 폴더 구조

- `sql/schema.sql` — 7개 테이블 DDL 단일 소스
- `src/ami_db/` — 공용 로직 (matching/hours_parser/synthetic/status/anomaly/serving/db/config/places_client/places_sync)
- `scripts/` — 번호 순서대로 실행하는 배치 스크립트. 각 단계는 대부분 "생성(파일 저장)"과 "적재(DB 쓰기)"를 분리했다 — 재실행 시 재계산/재호출 없이 파일만 다시 읽을 수 있도록(예: 06/07). **04단계(Google Places 동기화)는 예외**로, DB 자체가 캐시 역할을 하므로(이미 `source='google_places'` 행이 있으면 API를 호출하지 않음) 파일 분리 없이 캐시 확인→호출→적재를 한 스크립트가 전부 수행한다.
- `output/generated/` — 이 서브프로젝트가 새로 만든 산출물(재매칭 결과/합성 시계열). `visualize_analyis_data/output/`은 원본 파이프라인 산출물이라 건드리지 않고 읽기 전용으로만 참조한다.
- `app/serving_api.py` — 조회용 FastAPI (포트 8000, 유일한 런타임 서버)

## 스코프

- 파일럿 선로: A선로 (근거: `data/03_지역특성_규모_업종_분석보고서.md`)
- 파일럿 지역: 화곡동, 법정동명 기준 (근거: `.env.example` 주석 참고)
- `meters` 마스터 테이블은 A선로 전체 64개를 다 적재하지만, `meter_timeseries`/`store_operating_status`/`anomaly_events`처럼 무거운 테이블은 화곡동에 실제 매칭된 계기(~21개)로만 스코프를 좁혔다 — 나머지는 운영시간 정보가 없어 영업유무 판정 자체가 정의되지 않기 때문.
