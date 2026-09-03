# 2026-09-04 클라우드 서버 · DB 갱신 계획

**아직 실행 전 — 계획 문서만.** DB 갱신(Phase D)은 다른 세션에서 로컬 데이터를 채우는 작업이 끝나기 전까지 보류.

## 현재 상황

```
origin/main (GitHub, 서버에 반영된 상태): 51d8628
local HEAD (아직 push 전):                bdca24e   ← 2 커밋 앞섬
```

| 커밋 | 내용 | 스키마/데이터 영향 |
|---|---|---|
| `09c8051` 운영상태/혼잡도 1차 구현 | `serving_api.py`·`serving.py` 수정, `tests/` 신규, `pyproject.toml`에 `httpx` 추가(dev 전용) | 없음 |
| `bdca24e` `/api/stores/snapshot` 신규 API | `schemas.py`·`serving_api.py`·`serving.py` 수정, `docs/API_REFERENCE.md` 재생성 | 없음 |

`sql/schema.sql`, `scripts/` 모두 변경 없음 — 이 2개 커밋은 **순수 코드 배포**로 끝난다. 별도로 데이터베이스 자체를 최신 날짜까지 채우는 작업은 지금 다른 세션이 진행 중이므로, 이 문서에서는 **코드 배포(A~C)**와 **DB 이관(D)**을 분리해서 다룬다.

---

## Phase A — GitHub 동기화

로컬 2개 커밋을 원격에 반영.

```bash
cd db
git push origin main
```

## Phase B — 원격을 git 기반 배포로 전환 (오늘 하는 이유: 이후 갱신을 한 줄로 줄이기 위해)

지금 `/opt/kepco-ami`는 최초 배포 때 `tar`로 통째로 복사해 둔 일반 디렉터리라 git 이력이 없다. 매번 바뀐 파일을 손으로 골라 `scp`하는 대신, **제자리에서 git 저장소로 전환**한다 — 다운타임 없이 가능한 방법:

```bash
ssh -i base-cloud.pem root@49.50.140.204

cd /opt/kepco-ami
git init
git remote add origin https://github.com/CloakingGhost/kepco-ami.git
git fetch origin
git checkout -B main origin/main
```

**왜 안전한가**: `.env`, `.venv/`, `output/generated/`, `__pycache__/`는 저장소의 `.gitignore`에 이미 포함돼 있어서 `checkout`이 그 파일들을 건드리지 않는다. 지금 디렉터리의 코드 파일들은 이미 `51d8628` 상태와 동일하므로, `checkout`은 정확히 바뀐 파일(`serving_api.py`, `schemas.py`, `serving.py`, `pyproject.toml`, `uv.lock`, `docs/`, `tests/`)만 갱신한다.

```bash
git status   # .env/.venv/output이 "untracked"로만 보이는지 확인 (삭제 안 됐는지)
```

**전환 이후부터는 갱신이 이렇게 줄어든다**:
```bash
cd /opt/kepco-ami && git pull && uv sync && systemctl restart kepco-ami
```

## Phase C — 반영 및 검증

```bash
uv sync                                  # uv.lock 갱신 반영 (httpx는 dev 전용이라 프로덕션엔 사실상 무영향)
systemctl restart kepco-ami
systemctl is-active kepco-ami
curl -s https://look-ami.duckdns.org/api/stores/snapshot?date=2026-06-15&time=14:00 | head -c 200
```

**배포 전 권장(선택)**: 로컬에서 `pytest`로 새 엔드포인트 회귀 테스트 후 배포하면 더 안전 — `test-agent` 서브에이전트가 이 프로젝트(`db/tests/`) 전용으로 이미 준비돼 있으니 원하면 위임 가능.

---

## Phase D — 데이터베이스 갱신 (보류 — 다른 세션 작업 완료 후)

### 진행 조건
로컬에서 데이터 채우기 작업(추정: `06_generate_synthetic_timeseries.py` → `07_load_synthetic_timeseries.py` → 필요시 `09`/`10` 재실행)이 끝났는지 아래로 확인:

```bash
docker exec places-api-project-postgres-1 psql -U postgres -d contest_db -c \
  "SELECT max(ts), count(*) FROM meter_timeseries;"
```
지난 확인 시점(`2026-07-01`, 305,604행)보다 늘어나 있고, 다른 세션에서 "끝났다"는 신호가 오면 진행.

### 이관 절차 (지난번과 동일한 방식 재사용 — 검증된 방법)

```bash
# 1) 로컬 덤프 (컨테이너 내부에서 — 인코딩 깨짐 방지)
docker exec places-api-project-postgres-1 pg_dump -U postgres -d contest_db -F c -f /tmp/contest_db.dump
docker cp places-api-project-postgres-1:/tmp/contest_db.dump ./contest_db.dump

# 2) 원격 전송
scp -i base-cloud.pem ./contest_db.dump root@49.50.140.204:/tmp/

# 3) 원격 복원
ssh -i base-cloud.pem root@49.50.140.204
docker cp /tmp/contest_db.dump kepco-ami-postgres-1:/tmp/
docker exec kepco-ami-postgres-1 pg_restore -U postgres -d contest_db --clean --if-exists /tmp/contest_db.dump

# 4) 검증 — 로컬/원격 행 수·최신 날짜 일치 확인
docker exec kepco-ami-postgres-1 psql -U postgres -d contest_db -c \
  "SELECT max(ts), count(*) FROM meter_timeseries;"

# 5) 정리
rm -f ./contest_db.dump
ssh -i base-cloud.pem root@49.50.140.204 "rm -f /tmp/contest_db.dump; docker exec kepco-ami-postgres-1 rm -f /tmp/contest_db.dump"
```

전체 재덤프를 쓰는 이유: 지금 규모(<100MB)에서는 증분보다 전체 재이관이 더 단순하고, 행 수 비교로 검증하기도 쉽다. `meter_timeseries`·`store_operating_hours` 적재 로직 자체가 `ON CONFLICT DO NOTHING`이라 증분 방식으로 바꿔도 안전하지만, 지금은 그럴 이유가 없다.

---

## 요약: 지금 vs 나중

| 지금 (코드) | 나중 (DB, 대기 중) |
|---|---|
| Phase A: `git push` | Phase D 진행 조건 확인 |
| Phase B: 원격 git 전환 | 덤프 → scp → restore |
| Phase C: `uv sync` + 재기동 + 검증 | 행 수/날짜 검증 |
