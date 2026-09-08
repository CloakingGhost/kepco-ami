# -*- coding: utf-8 -*-
"""
2-7단계(2차 우선순위): anomaly_events 배치 계산/적재.

판정 규칙 3종(위험 1개, 주의 2개)과 그 근거는 전부 ami_db.anomaly 모듈
docstring에 정리돼 있다. 요약하면:
  - 위험 = 계약전력 130%가 60분 지속(KEC 212.3 산업용 표) - 물리적으로 검증된
           유일한 위험 신호. 통계 기반 개인화 규칙은 "위험"을 만들지 않는다
           (이유: anomaly.py "규칙이 겹칠 때" 앞 단락 참고).
  - 주의 = 계약전력 80%가 3시간 지속(연속부하 80% 규칙) 또는
           매장 자신의 "진짜폐점" baseline에서 1.5x 이상 튀어 60분 지속

"영업종료 이후" 개념(data/프로젝트개요.md)을 구현하기 위해, 09단계에서 이미
계산해 둔 store_operating_status.schedule_status='closed_hours'인 슬롯을 1차로
거르고, 그 안에서도 그 매장의 (요일,슬롯)별 실측 중앙값이 night_baseline에
가까운(=원래도 조용한) 슬롯만 최종 판정 대상으로 삼는다(anomaly.py의
compute_group_thresholds/is_quiet_slot) - 영업시간표 자체가 준비시간을 못 가르거나
틀린 경우(실측 검증됨)를 데이터 기반으로 걸러내기 위함.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.anomaly import compute_group_thresholds, detect_events  # noqa: E402
from ami_db.config import GENERATED_DIR, VIZ_OUTPUT, settings  # noqa: E402
from ami_db.db import bulk_insert, get_raw_connection  # noqa: E402
from ami_db.synthetic import compute_night_baseline  # noqa: E402


def main() -> None:
    matched_csv = GENERATED_DIR / f"store_ami_matched_{settings.target_dong}.csv"
    meter_ids = pd.read_csv(matched_csv, encoding="utf-8-sig")["meter_id"].tolist()

    real_ts_all = pd.read_pickle(VIZ_OUTPUT / "timeseries_clean.pkl")[["meter_id", "time", "recv_kWh"]]

    total = 0
    with get_raw_connection() as conn:
        # 세 규칙 전부 매장마다 다른 contract_power_kw를 기준으로 삼는다 - 전 매장에
        # 같은 절대 kWh 컷을 쓰면 그 자체로 "전역 컷"이 되어, 이 프로젝트가
        # congestion_level에서 이미 피해 온 함정을 안전감지에서 반복하게 된다.
        with conn.cursor() as cur:
            cur.execute("SELECT meter_id, contract_power_kw FROM meters WHERE meter_id = ANY(%s)", (meter_ids,))
            contract_power_by_meter = dict(cur.fetchall())

        # anomaly_events는 event_id가 SERIAL PK라 자연 유니크 제약(ON CONFLICT 대상)이
        # 없다. 재실행 시 중복 적재되지 않도록 이번 스코프(매칭된 21개 계기)의 기존
        # 이벤트를 먼저 지우고 다시 계산한다(멱등성 보장, 이 서브프로젝트 다른 로더들과
        # 동일한 "완전 교체" 정책).
        with conn.cursor() as cur:
            cur.execute("DELETE FROM anomaly_events WHERE meter_id = ANY(%s)", (meter_ids,))

        for meter_id in meter_ids:
            real_ts = real_ts_all[real_ts_all["meter_id"] == meter_id]
            if real_ts.empty:
                continue
            night_baseline = compute_night_baseline(real_ts)
            eligibility, baseline_median, baseline_floor = compute_group_thresholds(real_ts, night_baseline)
            contract_power_kw = contract_power_by_meter.get(meter_id)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT mt.ts, mt.received_active_power_kwh
                    FROM meter_timeseries mt
                    JOIN stores s ON s.meter_id = mt.meter_id
                    JOIN store_operating_status sos ON sos.store_id = s.store_id AND sos.ts = mt.ts
                    WHERE mt.meter_id = %s AND sos.schedule_status = 'closed_hours'
                    ORDER BY mt.ts
                    """,
                    (meter_id,),
                )
                closed_hours_ts = pd.DataFrame(cur.fetchall(), columns=["ts", "recv_kWh"])
            if closed_hours_ts.empty:
                continue

            flagged = detect_events(closed_hours_ts, eligibility, contract_power_kw, baseline_median, baseline_floor)
            if flagged.empty:
                continue

            rows = [
                (meter_id, r.ts, r.level, r.rule_triggered, float(r.recv_kWh), float(r.threshold_value), None)
                for r in flagged.itertuples(index=False)
            ]
            n = bulk_insert(
                conn, "anomaly_events",
                ["meter_id", "detected_at", "level", "rule_triggered", "metric_value", "threshold_value", "notified_at"],
                rows,
            )
            total += n
            counts = flagged["level"].value_counts().to_dict()
            print(f"  {meter_id}: {n}건 (위험 {counts.get('위험', 0)} / 주의 {counts.get('주의', 0)})")

    print(f"\nanomaly_events 적재 총 {total}건")


if __name__ == "__main__":
    main()
