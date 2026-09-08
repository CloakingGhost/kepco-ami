# -*- coding: utf-8 -*-
"""
안전감지 2순위 기능(화면 표시용 스냅샷) API 검증:
  - GET /api/anomalies/snapshot

관리자용 전체 이력 조회(GET /api/anomalies)와 달리, 이 엔드포인트는 "지금 화면에
띄울 매장만" 골라 응답한다(둘 다 안 걸리면 21개 매장 중 그 매장은 alerts에 아예
안 나온다 - count=0/alerts=[]가 정상인 경우가 대다수).

두 트리거는 OR 관계:
  1) 즉시위험: 조회 시점(date+time 슬롯)에 anomaly_events.level='위험' 행이 있는 매장.
  2) 주의반복: 조회 시점 기준 최근 24시간(CAUTION_REPEAT_WINDOW_HOURS) 안에서,
     같은 매장의 level='주의' 행을 시간순으로 볼 때 슬롯 간격이 15분
     (EPISODE_GAP_MINUTES)을 넘으면 별개 사건(episode)으로 나누고, 그렇게 나눈
     서로 다른 사건이 3개(CAUTION_REPEAT_THRESHOLD) 이상이면 트리거. repeat_count는
     사건 개수이지 원시 행 개수가 아니다.

시나리오 표 (docker-compose Postgres, contest_db - 06→07→09→10 배치 재실행 후 상태):

| # | 입력 (date, time)         | 기대 status | 기대 응답 핵심 필드                                              |
|---|---------------------------|-------------|-------------------------------------------------------------------|
| 1 | 26-07-02, 02:15           | 200         | count=1, alerts[0].store_id=12, level=위험, trigger_reason=즉시위험, repeat_count=None |
| 2 | 26-07-02, 03:00           | 200         | count=0 (위험 슬롯 4개가 02:00~02:45로 끝났으므로 store_id=12 없음) |
| 3 | 26-07-22, 06:00           | 200         | count=1, alerts[0].store_id=15, level=주의, trigger_reason=주의반복, repeat_count=3 |
| 4 | 26-07-22, 02:00           | 200         | count=0 (1번째 사건만 지남, 반복 조건 3회 미충족)                  |
| 5 | 26-07-23, 05:45           | 200         | count=0 (24시간 롤링창 경계 - 아래 관찰 결과 및 검산 참고)         |
| 6 | 26-07-23, 01:00           | 200         | count=1, repeat_count=3 (아래 관찰 결과 및 검산 참고)              |
| 7 | 26-05-09, 19:15           | 200         | count=0, alerts=[] (실측 구간의 평범한 하루)                       |
| 8 | date=2026-07-02(4자리 연도)| 400         | 형식 오류                                                          |
| 9 | time=02:10(15분 단위 아님) | 400         | 형식 오류                                                          |
| 10| date/time 둘 다 생략       | 200         | 서버 "현재" 기준 - 상태 예측은 안 함, 200만 확인                   |

#5/#6 검산 (window_start는 배타, reference_ts는 포함 - `detected_at > window_start
AND detected_at <= reference_ts`):
  - 소문난순대(store_id=15)의 주의 이벤트는 2026-07-22 01:00~01:45 / 03:00~03:45 /
    05:00~05:45 세 사건.
  - #6 (reference=2026-07-23 01:00): window_start=2026-07-22 01:00(배타). 1번째
    사건 중 01:00 슬롯만 정확히 window_start와 같아서 창에서 빠지고 01:15~01:45는
    남는다 - 이 3개 행도 서로 15분 간격이라 여전히 "1개 사건"으로 묶인다. 2·3번째
    사건은 통째로 창 안에 있다. 그래서 사건 수는 그대로 3개 -> repeat_count=3,
    count=1이 논리적으로 맞다. 실제 호출 결과도 이와 일치한다.
  - #5 (reference=2026-07-23 05:45): window_start=2026-07-22 05:45(배타). 세
    사건의 슬롯(01:00~01:45, 03:00~03:45, 05:00~05:45)이 전부 window_start
    "이하"라서(3번째 사건의 마지막 슬롯조차 window_start와 정확히 같아 배타 조건에
    걸려 제외됨) 창 안에 남는 행이 하나도 없다. 그래서 사건 수는 0개 ->
    count=0이 논리적으로 맞다(아래 힌트에서 "2·3번째 사건은 창 안에 있지만 사건
    2개뿐이라 미충족"이라 예상했던 것과는 다른 경로지만, 결과는 동일하게 count=0
    이고 실제 관찰과도 일치한다 - 버그 아님).
"""
from __future__ import annotations

DANGER_STORE_ID = 12   # 충북식당 - meter_id=A-L-60, 2026-07-02 02:00~02:45 '위험' 데모
REPEAT_STORE_ID = 15   # 소문난순대 - meter_id=A-L-63, 2026-07-22 01/03/05시대 '주의반복' 데모


# ============================================================
# GET /api/anomalies/snapshot - 특정 시점 위기 감지 스냅샷 (화면 표시용)
# ============================================================

class TestAnomalySnapshot:
    def test_normal_immediate_danger_mid_slot(self, client):
        """정상(즉시위험): 26-07-02 02:15(위험 지속 구간 한가운데) -> count=1, store_id=12, 위험/즉시위험, repeat_count=None."""
        resp = client.get("/api/anomalies/snapshot", params={"date": "26-07-02", "time": "02:15"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["date"] == "26-07-02"
        assert body["time"] == "02:15"
        assert body["count"] == 1
        alerts = body["alerts"]
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["store_id"] == DANGER_STORE_ID
        assert alert["store_name"] == "충북식당"
        assert alert["meter_id"] == "A-L-60"
        assert alert["level"] == "위험"
        assert alert["trigger_reason"] == "즉시위험"
        assert alert["rule_triggered"] == "kec212_overload_130pct_60min"
        assert alert["repeat_count"] is None
        assert isinstance(alert["metric_value"], (int, float))
        assert isinstance(alert["threshold_value"], (int, float))

    def test_boundary_immediate_danger_right_after_slots_end(self, client):
        """경계: 26-07-02 03:00(위험 슬롯은 02:00~02:45로 끝났음, 그 직후) -> store_id=12는 alerts에 없어야 함."""
        resp = client.get("/api/anomalies/snapshot", params={"date": "26-07-02", "time": "03:00"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["alerts"] == []

    def test_normal_caution_repeat_threshold_met(self, client):
        """정상(주의반복): 26-07-22 06:00(3번째 사건 종료 직후) -> count=1, store_id=15, 주의/주의반복, repeat_count=3."""
        resp = client.get("/api/anomalies/snapshot", params={"date": "26-07-22", "time": "06:00"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["date"] == "26-07-22"
        assert body["time"] == "06:00"
        assert body["count"] == 1
        alerts = body["alerts"]
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["store_id"] == REPEAT_STORE_ID
        assert alert["store_name"] == "소문난순대"
        assert alert["meter_id"] == "A-L-63"
        assert alert["level"] == "주의"
        assert alert["trigger_reason"] == "주의반복"
        assert alert["rule_triggered"] == "empty_store_baseline_3x_60min"
        assert alert["repeat_count"] == 3

    def test_boundary_caution_repeat_threshold_not_yet_met(self, client):
        """경계: 26-07-22 02:00(1번째 사건만 지남, 2·3번째는 아직) -> 반복 조건(3회) 미충족 -> count=0."""
        resp = client.get("/api/anomalies/snapshot", params={"date": "26-07-22", "time": "02:00"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["alerts"] == []

    def test_boundary_24h_window_all_episodes_rolled_out(self, client):
        """
        경계(24시간 롤링창): 26-07-23 05:45.

        window_start(=reference_ts - 24h)가 2026-07-22 05:45이고, 창 조건이
        `detected_at > window_start`(배타)라서, 3번째 사건의 마지막 슬롯(2026-07-22
        05:45)조차 window_start와 정확히 같아 제외된다. 1·2번째 사건은 더 이른
        시각이라 당연히 제외. 결과적으로 창 안에 남는 '주의' 행이 하나도 없어
        사건 수 0 -> count=0이 논리적으로 맞고, 실제 응답도 이와 일치한다.
        """
        resp = client.get("/api/anomalies/snapshot", params={"date": "26-07-23", "time": "05:45"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["alerts"] == []

    def test_boundary_24h_window_first_episode_edge_slot_dropped_but_still_counts(self, client):
        """
        경계(24시간 롤링창): 26-07-23 01:00.

        window_start=2026-07-22 01:00(배타)라서 1번째 사건의 01:00 슬롯만 창에서
        빠지고 01:15~01:45(여전히 15분 간격 연속)는 남아 "사건 1개"로 그대로
        묶인다. 2·3번째 사건은 통째로 창 안에 있다. 그래서 사건 수는 여전히
        3개 -> repeat_count=3, count=1이 논리적으로 맞고, 실제 응답도 일치한다.
        """
        resp = client.get("/api/anomalies/snapshot", params={"date": "26-07-23", "time": "01:00"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        alerts = body["alerts"]
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["store_id"] == REPEAT_STORE_ID
        assert alert["trigger_reason"] == "주의반복"
        assert alert["repeat_count"] == 3

    def test_normal_ordinary_day_no_triggers(self, client):
        """정상: 26-05-09 19:15(실측 구간의 평범한 하루) -> 아무 트리거도 없어야 함 -> count=0, alerts=[]."""
        resp = client.get("/api/anomalies/snapshot", params={"date": "26-05-09", "time": "19:15"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["date"] == "26-05-09"
        assert body["time"] == "19:15"
        assert body["count"] == 0
        assert body["alerts"] == []

    def test_boundary_date_wrong_format_4digit_year_returns_400(self, client):
        """경계: date가 'YY-MM-DD'가 아니라 4자리 연도('2026-07-02') -> 400."""
        resp = client.get("/api/anomalies/snapshot", params={"date": "2026-07-02", "time": "02:15"})
        assert resp.status_code == 400

    def test_boundary_time_not_on_15min_grid_returns_400(self, client):
        """경계: time이 15분 단위(00/15/30/45)가 아님('02:10') -> 400."""
        resp = client.get("/api/anomalies/snapshot", params={"date": "26-07-02", "time": "02:10"})
        assert resp.status_code == 400

    def test_normal_both_params_omitted_uses_server_now(self, client):
        """정상: date/time 둘 다 생략 -> 서버 현재 시각 기준으로 200 (상태값은 예측하지 않고 200만 확인)."""
        resp = client.get("/api/anomalies/snapshot")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["date"], str)
        assert isinstance(body["time"], str)
        assert isinstance(body["count"], int)
        assert isinstance(body["alerts"], list)
        assert body["count"] == len(body["alerts"])
