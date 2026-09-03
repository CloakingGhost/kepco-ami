# 2026-09-03 배포 작업 기록

**목표**: 화곡동 파일럿 AMI 서빙 스택(PostgreSQL + FastAPI)을 로컬 개발 환경에서 네이버 클라우드 플랫폼(NCP) 서버로 수동 배포.

**최종 결과**: `https://look-ami.duckdns.org/docs` — HTTPS로 정상 서비스 중.

---

## 0. 사전 상태

- NCP 서버(Rocky Linux 9.8, `49.50.140.204`)는 이전에 `init_script.sh`로 초기화 완료 (Docker, Nginx, Node.js 22, uv, 스왑 1GB, 타임존 등).
- 배포 전 검토한 리스크: 한글 인코딩 깨짐, 원격 디스크 용량 한계, 주기적 로컬→원격 동기화 시 중복/에러.
  - 로컬 DB 용량 82MB, `server_encoding=UTF8` 확인.
  - 적재 스크립트(`02_load_meta_and_timeseries.py`, `07_load_synthetic_timeseries.py`)가 이미 `ON CONFLICT DO NOTHING`으로 멱등하게 설계되어 있음을 확인.

## 1. GitHub 저장소 구성

- `db/` 폴더(독립 uv 프로젝트)를 레포 루트로 `git init`.
- `.gitignore`가 `.env`, `.venv/`, `output/generated/` 등을 이미 제외 — 비밀값 커밋 없음.
- 원격: `https://github.com/CloakingGhost/kepco-ami` (`main` 브랜치)

| 커밋 | 내용 |
|---|---|
| `b276a30` | 초기 커밋 (AMI 매칭 파이프라인 + 조회용 FastAPI) |
| `a54f40c` | Postgres 포트를 `127.0.0.1`로 제한 (보안) |
| `51d8628` | `.env`의 DuckDNS 변수로 인한 Settings 검증 실패 수정 |

## 2. NCP 원격 배포 (PostgreSQL + FastAPI)

- SSH 키(`base-cloud.pem`)로 `root@49.50.140.204` 접속 확인.
- 코드를 `tar` + `ssh` 파이프로 `/opt/kepco-ami`에 전송 (`.git`/`.venv`/`__pycache__`/`.env` 제외).
- `.env`는 scp로 별도 전송 (비밀값 노출 없이).
- `docker-compose.yml`의 Postgres 포트를 `5432:5432` → `127.0.0.1:5432:5432`로 수정 후 `docker compose up -d` (컨테이너: `kepco-ami-postgres-1`, `postgres:16`).
- 데이터 이관: 로컬 `pg_dump -F c` → scp 전송 → 원격 `pg_restore --clean --if-exists`.
  - **검증**: `meter_timeseries` **305,604행** 로컬=원격 완전 일치, 테이블 용량 동일 수준, UTF8 인코딩 유지.
- `uv sync`로 의존성 설치 (fastapi, uvicorn, psycopg2-binary, pandas 등 34개 패키지).
- `systemd` 유닛 `kepco-ami.service` 등록 (`uv run uvicorn app.serving_api:app`, `Restart=on-failure`).
- `firewalld`에서 `8000/tcp` 개방 + NCP **ACG**에서 `8000` 인바운드 규칙 추가(사용자 실행) → 외부 접속 확인 (`http://49.50.140.204:8000/docs` → 200).

## 3. Nginx 리버스 프록시 + Let's Encrypt HTTPS

- 도메인: **DuckDNS** `look-ami.duckdns.org`
- **DuckDNS 갱신**: `/root/scripts/duckdns_update.sh` — `.env`의 `DUCK_DOMAIN`/`DUCK_TOKEN`을 읽어 IP 업데이트. `cron`으로 **5분마다** 실행.
- NCP ACG에 `80`, `443` 인바운드 규칙 추가(사용자 실행) → Let's Encrypt HTTP-01 챌린지 통과.
- `certbot --nginx`로 인증서 발급 → `/etc/nginx/conf.d/kepco-ami.conf`에 SSL 서버 블록 + HTTP→HTTPS 301 리다이렉트 자동 구성.
  - 인증서 만료일: **2026-12-02**
- **자동 갱신**: `/root/scripts/renew_cert.sh` — `certbot renew --quiet` + `nginx -t && nginx -s reload`. `cron`으로 **매일 03:17 / 15:17** 실행.
  - certbot이 자체 설치한 `certbot-renew.timer`는 중복 방지를 위해 비활성화하고, 위 스크립트로 일원화.
  - `certbot renew --dry-run` 및 스크립트 실제 실행으로 정상 동작 확인.
- **보안 강화**: FastAPI를 `--host 0.0.0.0` → `--host 127.0.0.1`로 변경(Nginx만 진입점이 되도록), `firewalld`에서 `8000/tcp` 제거. NCP ACG의 `8000` 규칙도 이후 사용자가 직접 제거.

## 4. 트러블슈팅

- **dnf 동시 실행으로 메모리 스왑 과다** (서버 메모리 765Mi로 협소) — SSH 클라이언트 타임아웃으로 여러 `dnf install`이 중복 실행됨. 프로세스 정리 후 백그라운드(`nohup` + `disown`)로 단일 실행하여 해결.
- **`systemctl reload nginx` 실패** (`Failed to set up mount namespacing` — systemd 네임스페이스 오류). `systemctl restart nginx` 또는 `nginx -s reload` 직접 호출로 우회.
- **FastAPI 크래시 루프**: `.env`에 `DUCK_DOMAIN`/`DUCK_EMAIL`/`DUCK_TOKEN`을 추가한 뒤 앱을 재기동하자 pydantic Settings가 "정의되지 않은 필드"로 거부. 로컬에 이미 준비돼 있던 `config.py`의 `extra = "ignore"` 수정이 커밋되지 않은 상태였음을 발견 → 커밋(`51d8628`)·원격 반영으로 해결.
- **디스크 사용률 76~77%, `docker system df`는 reclaimable 0%**: `du -sh`로 디렉터리별로 뜯어보니 실제 원인은 이미지·볼륨이 아니라 오늘 설치 작업 중 쌓인 캐시 — `/var/cache/dnf` 123M, `/root/.cache/uv` 319M. `dnf clean all` + `uv cache clean`으로 약 440MB 회수.
  - `uv cache clean`이 `Timeout (300s) when waiting for lock on /root/.cache/uv` 로 실패. 원인은 `kepco-ami.service`가 `uv run uvicorn ...`으로 상시 실행되며 uv 캐시 락을 계속 점유하고 있었기 때문. `ExecStart`를 `uv run` 대신 venv의 uvicorn 바이너리(`/opt/kepco-ami/.venv/bin/uvicorn`)를 직접 실행하도록 변경 → 락 해제 + 기동 속도도 개선.
  - `/root/scripts/system_cleanup.sh` 작성: dnf/uv 캐시 정리 + `docker system prune -f` + 정리 전/후 디스크·메모리·`contest_db` 용량을 `/var/log/system_cleanup.log`에 기록, 디스크 85% 이상이면 경고 로그. `cron`으로 **매주 일요일 04:00** 실행 등록 (`/etc/cron.d/kepco-ami`).
  - 최초 실행 결과: 디스크 여유 2.2G → 2.3G, 메모리 available 268Mi → 332Mi.

## 최종 상태

| 항목 | 값 |
|---|---|
| API | `https://look-ami.duckdns.org/docs` — HTTP 200 |
| HTTP 접속 | 301로 HTTPS 리다이렉트 |
| 인증서 만료 / 갱신 | 2026-12-02 / cron 자동 갱신 (매일 2회) |
| DB 노출 | `127.0.0.1:5432` (외부 비공개) |
| App 노출 | `127.0.0.1:8000` (Nginx 경유만 가능, 직접 접근 차단) |
| GitHub | `main` · 커밋 3개 |
| 서버 리소스 | 메모리 765Mi(여유 ~313Mi) · 디스크 9.0G 중 사용 76%(여유 2.3G) |
| 캐시/디스크 정리 | `/root/scripts/system_cleanup.sh`, cron 매주 일 04:00 |

## 후속 권장 사항

- 메모리·디스크 여유가 크지 않다 — 캐시 정리는 자동화했으니(`system_cleanup.sh`), 로그(`/var/log/system_cleanup.log`)에 쌓이는 디스크/메모리/DB 용량 추이를 가끔 확인할 것.
- **[정정]** 합성 시계열은 자동으로 쌓이지 않는다. `06_generate_synthetic_timeseries.py`는 실행 시점의 `date.today()`를 한 번 읽어 "실측 종료일(2026-06-30) 다음날 ~ 실행 시점"까지만 생성하는 **1회성 배치**이고, `app/`·`src/`·`scripts/` 전체에 cron·스케줄러·백그라운드 작업이 전혀 없다. `app/serving_api.py`는 조회 전용(SELECT)이라 FastAPI 실행 여부와도 무관하다. 실제로 DB의 마지막 데이터는 최초 파이프라인 실행 시점 기준 `2026-07-01`에 멈춰 있다 — 최신 날짜까지 채우려면 `06`→`07`(상태·이상치 재계산이 필요하면 `09`→`10`까지)을 수동으로 다시 실행해야 하며, 이를 주기적으로 자동화하고 싶다면 별도 cron을 새로 만들어야 한다(현재는 없음).
- `meter_timeseries`·`store_operating_hours` 적재 로직이 멱등(`ON CONFLICT DO NOTHING`)하게 짜여 있어, 위 배치를 나중에 재실행하거나 재배포·재동기화하더라도 중복 없이 안전하다.
