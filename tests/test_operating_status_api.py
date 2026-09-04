# -*- coding: utf-8 -*-
"""
1순위 기능(영업유무·혼잡도) API 4개 엔드포인트 검증:
  - GET /api/stores/status
  - GET /api/stores/{store_id}/status/current
  - GET /api/stores/{store_id}/hours
  - GET /api/stores/{store_id}/status

시나리오 표는 db/tests/README에 두지 않고(요청받지 않았으므로) 이 파일의 각 테스트
docstring/이름에 그대로 옮겨 적는다. 화곡동 파일럿 21개 매장(match_status='matched')만
유효 대상이고, 조회 가능 날짜는 EARLIEST_SAMPLE_DATE(2026-04-01)~LATEST_SERVICE_DATE
(2026-07-31)이다 - 상한이 date.today()가 아니라 고정 날짜인 이유는
ami_db/serving.py의 LATEST_SERVICE_DATE 주석 참고.

날짜 선택 근거:
  - REAL_DATE(2026-05-15)는 app/serving_api.py의 EXAMPLE_DATE_REAL과 동일값 - 실측
    구간(is_synthetic=false)의 "가장자리가 아닌" 날짜라 store_id=1이 정확히 96행
    (15분 x 24시간)을 갖는다 (2026-04-01처럼 그리드 첫날은 0시 슬롯이 비어 95행이 되는
    엣지케이스가 있어 정상 케이스로는 부적합해서 피했다).
  - SYNTHETIC_DATE(2026-07-15)도 EXAMPLE_DATE_SYNTHETIC과 동일 - 합성 구간
    (is_synthetic=true) 대표 날짜.
"""
from __future__ import annotations

from datetime import date, timedelta

from ami_db.serving import EARLIEST_SAMPLE_DATE, LATEST_SERVICE_DATE

VALID_STORE_ID = 1  # 못난이찹쌀꽈배기 - google_places 없어 ksic_estimate 폴백 케이스 (data/serving_api.py EXAMPLE_STORE_ID와 동일)
ONE_HOUR_STORE_ID = 2  # meters.data_resolution='1hour'인 계기(A-L-16)를 쓰는 매장
NONEXISTENT_STORE_ID = 999  # 21개 매장 범위(1~21) 밖 - stores 테이블에 존재하지 않음

REAL_DATE = date(2026, 5, 15)        # 실측 구간, 그리드 가장자리 아님(96행)
SYNTHETIC_DATE = date(2026, 7, 15)   # 합성 구간, 그리드 가장자리 아님(96행)


# ============================================================
# GET /api/stores/status - 전체 매장 현재 상태
# ============================================================

class TestListCurrentStatus:
    def test_normal_returns_all_21_stores_with_valid_schema(self, client):
        """정상: 파라미터 없이 조회 -> 200, 21개 매장 전부, 각 행 필드 타입/congestion_level 규칙 일치."""
        resp = client.get("/api/stores/status")
        assert resp.status_code == 200
        body = resp.json()
        stores = body["stores"]
        assert len(stores) == 21

        seen_ids = set()
        for row in stores:
            seen_ids.add(row["store_id"])
            assert isinstance(row["store_id"], int)
            assert isinstance(row["name"], str)
            assert row["schedule_status"] in ("open_hours", "closed_hours")
            assert row["power_status"] in ("active", "low")
            assert row["final_status"] in ("영업중", "휴무추정", "예외영업", "영업종료")
            # CHECK 제약(schema.sql)과 일치해야 함: final_status가 '영업중'이 아니면
            # congestion_level은 반드시 null이어야 한다.
            if row["final_status"] == "영업중":
                assert row["congestion_level"] in ("상", "중", "하")
            else:
                assert row["congestion_level"] is None
        assert seen_ids == set(range(1, 22))


# ============================================================
# GET /api/stores/{store_id}/status/current - 매장 1곳 현재 상태
# ============================================================

class TestStoreCurrentStatus:
    def test_normal_valid_store_id(self, client):
        """정상: store_id=1 -> 200, store_id/name 일치, congestion_level 규칙 일치."""
        resp = client.get(f"/api/stores/{VALID_STORE_ID}/status/current")
        assert resp.status_code == 200
        body = resp.json()
        assert body["store_id"] == VALID_STORE_ID
        assert isinstance(body["name"], str)
        assert body["final_status"] in ("영업중", "휴무추정", "예외영업", "영업종료")
        if body["final_status"] == "영업중":
            assert body["congestion_level"] in ("상", "중", "하")
        else:
            assert body["congestion_level"] is None

    def test_boundary_nonexistent_store_id_returns_404(self, client):
        """경계: 존재하지 않는 store_id(999) -> 404."""
        resp = client.get(f"/api/stores/{NONEXISTENT_STORE_ID}/status/current")
        assert resp.status_code == 404

    def test_boundary_store_id_zero_returns_404(self, client):
        """경계: store_id=0(범위 1~21 밖, 그러나 타입은 유효한 int) -> 404."""
        resp = client.get("/api/stores/0/status/current")
        assert resp.status_code == 404


# ============================================================
# GET /api/stores/{store_id}/hours - 매장 1곳 요일별 운영시간
# ============================================================

class TestStoreHours:
    def test_normal_valid_store_id(self, client):
        """정상: store_id=1 -> 200, 1~7행, day_of_week 0~6 중복 없이, source 값 유효."""
        resp = client.get(f"/api/stores/{VALID_STORE_ID}/hours")
        assert resp.status_code == 200
        body = resp.json()
        assert body["store_id"] == VALID_STORE_ID
        hours = body["hours"]
        assert 1 <= len(hours) <= 7

        seen_days = set()
        for row in hours:
            assert 0 <= row["day_of_week"] <= 6
            assert row["day_of_week"] not in seen_days, "day_of_week 중복 - DISTINCT ON 계약 위반"
            seen_days.add(row["day_of_week"])
            assert row["source"] in ("google_places", "ksic_estimate")
            if row["is_closed"]:
                assert row["open_time"] is None
                assert row["close_time"] is None

    def test_boundary_nonexistent_store_id_returns_404(self, client):
        """경계: 존재하지 않는 store_id(999) -> 404."""
        resp = client.get(f"/api/stores/{NONEXISTENT_STORE_ID}/hours")
        assert resp.status_code == 404


# ============================================================
# GET /api/stores/{store_id}/status - 매장 1곳 하루 상태+전력 타임라인
# ============================================================

class TestStoreStatusDay:
    """
    time을 안 주면 더 이상 하루 전체가 나오지 않는다(회귀 방지 - ami_db.serving의
    _day_series_cutoff 참고: time 생략 시 컷오프는 "실제 벽시계의 시:분을 date에
    붙인 시각"이라 date가 오늘이 아니면 테스트 실행 시각에 따라 행 수가 들쭉날쭉해진다).
    그래서 하루 전체(96행)를 보려는 테스트는 time="23:45"를 명시해 마지막 슬롯까지
    받도록 고정한다.
    """

    def test_normal_real_segment(self, client):
        """정상(실측 구간): store_id=1, date=2026-05-15, time=23:45 -> 200, 96행, is_synthetic 전부 false."""
        resp = client.get(
            f"/api/stores/{VALID_STORE_ID}/status",
            params={"date": REAL_DATE.isoformat(), "time": "23:45"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["store_id"] == VALID_STORE_ID
        assert body["date"] == REAL_DATE.isoformat()
        assert body["data_resolution"] == "15min"
        rows = body["rows"]
        assert len(rows) == 96
        assert all(r["is_synthetic"] is False for r in rows)

    def test_normal_synthetic_segment(self, client):
        """정상(합성 구간): store_id=1, date=2026-08-15, time=23:45 -> 200, 96행, is_synthetic 전부 true."""
        resp = client.get(
            f"/api/stores/{VALID_STORE_ID}/status",
            params={"date": SYNTHETIC_DATE.isoformat(), "time": "23:45"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_resolution"] == "15min"
        rows = body["rows"]
        assert len(rows) == 96
        assert all(r["is_synthetic"] is True for r in rows)

    def test_normal_1hour_resolution_store_has_fewer_rows(self, client):
        """정상(1hour 계기 매장): store_id=2, date=2026-08-15, time=23:45 -> 200, data_resolution='1hour', rows<96."""
        resp = client.get(
            f"/api/stores/{ONE_HOUR_STORE_ID}/status",
            params={"date": SYNTHETIC_DATE.isoformat(), "time": "23:45"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_resolution"] == "1hour"
        assert 0 < len(body["rows"]) < 96

    def test_normal_no_time_never_leaks_past_current_time_of_day(self, client):
        """
        회귀 방지: time을 생략하면(과거 날짜를 조회해도) 실제 벽시계 시:분 이후
        슬롯은 응답에 없어야 한다 - 예전엔 date가 오늘이 아니면 컷오프가 사실상
        무제한이 되어 미래(그날 기준 "현재시간" 이후) 슬롯까지 하루 전체가
        새어나가는 버그가 있었다(프론트 상세 팝업의 시간대별 활동량 차트가
        선택 시각 이후 구간까지 그려버림).
        """
        from ami_db.serving import service_now

        resp = client.get(f"/api/stores/{VALID_STORE_ID}/status", params={"date": REAL_DATE.isoformat()})
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        cutoff_time_str = service_now().strftime("%H:%M")
        assert all(r["ts"][11:16] <= cutoff_time_str for r in rows), (
            f"컷오프({cutoff_time_str}) 이후 슬롯이 응답에 포함됨 - 하루 전체 leak 회귀"
        )

    def test_boundary_nonexistent_store_id_returns_404(self, client):
        """
        경계(회귀 방지): 존재하지 않는 store_id(999) + 유효한 날짜 -> 404여야 한다.

        수정 전 버그: get_store_status_day()가 get_store_hours()와 달리 store 존재
        여부를 확인하지 않아, JOIN 결과가 그냥 빈 리스트가 되면서 200 + rows=[]가
        반환됐다(형제 엔드포인트 /hours, /status/current는 404였음 - 계약 불일치).
        db/src/ami_db/serving.py의 get_store_status_day()에 get_store_hours()와
        동일한 existence-check(SELECT 1 FROM stores WHERE store_id=...)를 추가하고
        None을 리턴하도록 고쳐, app/serving_api.py의 store_status_day()가 이를
        404로 매핑하도록 수정했다.
        """
        resp = client.get(
            f"/api/stores/{NONEXISTENT_STORE_ID}/status", params={"date": SYNTHETIC_DATE.isoformat()}
        )
        assert resp.status_code == 404

    def test_boundary_date_before_earliest_sample_date_returns_400(self, client):
        """경계: EARLIEST_SAMPLE_DATE(2026-04-01) 이전 날짜 -> 400."""
        before = EARLIEST_SAMPLE_DATE - timedelta(days=1)
        resp = client.get(f"/api/stores/{VALID_STORE_ID}/status", params={"date": before.isoformat()})
        assert resp.status_code == 400

    def test_boundary_date_after_service_range_returns_400(self, client):
        """경계: LATEST_SERVICE_DATE(2026-07-31) 다음날 -> 400."""
        after = LATEST_SERVICE_DATE + timedelta(days=1)
        resp = client.get(f"/api/stores/{VALID_STORE_ID}/status", params={"date": after.isoformat()})
        assert resp.status_code == 400

    def test_boundary_date_equal_to_service_end_is_valid(self, client):
        """경계: LATEST_SERVICE_DATE 자체(범위의 상한, inclusive) -> 200."""
        resp = client.get(
            f"/api/stores/{VALID_STORE_ID}/status", params={"date": LATEST_SERVICE_DATE.isoformat()}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["date"] == LATEST_SERVICE_DATE.isoformat()
        assert isinstance(body["rows"], list)

    def test_schema_congestion_level_zero_iff_not_operating(self, client):
        """
        스키마 검증: congestion_level이 1~3 <=> final_status=='영업중', 그 외엔 0.

        프론트 차트가 혼잡도를 그대로 시리즈로 그릴 수 있어야 해서 이 엔드포인트의
        congestion_level은 문자열('상'/'중'/'하'/null)이 아니라 정수 코드
        (0=해당없음/1=여유/2=보통/3=혼잡)로 내려간다 - 스냅샷/상세 API와 같은 인코딩.
        """
        resp = client.get(f"/api/stores/{VALID_STORE_ID}/status", params={"date": REAL_DATE.isoformat()})
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert len(rows) > 0
        for row in rows:
            if row["final_status"] == "영업중":
                assert row["congestion_level"] in (1, 2, 3)
            else:
                assert row["congestion_level"] == 0
            # nullable 필드 타입 확인
            assert isinstance(row["is_synthetic"], bool)
            assert isinstance(row["is_redistributed"], bool)
            assert row["received_active_power_kwh"] is None or isinstance(row["received_active_power_kwh"], (int, float))
