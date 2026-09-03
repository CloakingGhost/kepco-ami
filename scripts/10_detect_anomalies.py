# -*- coding: utf-8 -*-
"""
2-7단계(2차 우선순위): anomaly_events 배치 계산/적재.

임계치(group_iqr_1.5x=주의, group_iqr_3x=위험)의 최종 계수는 이번 범위에서
확정하지 않고 문서화만 한다(계획서 '후속 논의' 항목) - 실제 안전감지 기능
구현 시 팀이 조정할 여지를 남겨둔다.

"영업종료 이후" 개념(data/프로젝트개요.md)을 구현하기 위해, 09단계에서 이미
계산해 둔 store_operating_status.schedule_status='closed_hours'인 슬롯만
이상치 후보로 본다 - 영업시간 중 반짝 스파이크는 "혼잡"이지 안전 이슈가 아니다.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.anomaly import compute_group_thresholds, flag_anomalies  # noqa: E402
from ami_db.config import GENERATED_DIR, VIZ_OUTPUT, settings  # noqa: E402
from ami_db.db import bulk_insert, get_raw_connection  # noqa: E402

ATTENTION_MULT = 1.5
DANGER_MULT = 3.0


def main() -> None:
    matched_csv = GENERATED_DIR / f"store_ami_matched_{settings.target_dong}.csv"
    meter_ids = pd.read_csv(matched_csv, encoding="utf-8-sig")["meter_id"].tolist()

    real_ts_all = pd.read_pickle(VIZ_OUTPUT / "timeseries_clean.pkl")[["meter_id", "time", "recv_kWh"]]

    total = 0
    with get_raw_connection() as conn:
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
            thresholds = compute_group_thresholds(real_ts)

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

            flagged = flag_anomalies(closed_hours_ts, thresholds, ATTENTION_MULT, DANGER_MULT)
            flagged = flagged[flagged["level"].notna()]

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
            if n:
                print(f"  {meter_id}: {n}건 감지")

    print(f"\nanomaly_events 적재 총 {total}건")


if __name__ == "__main__":
    main()
