# -*- coding: utf-8 -*-
"""
증분 안전감지 배치 - 15분마다 도는 실시간형 감지.

10_detect_anomalies.py와의 차이:
  - 10번: 전체 재계산. 매칭 계기의 기존 이벤트를 **전부 DELETE**하고 4개월치를 다시 판정한다.
          결과가 항상 최신 규칙과 일치한다는 장점이 있지만, 15분마다 돌릴 수는 없다.
  - 이 스크립트: **최근 구간만** 판정하고 새로 발견한 슬롯만 INSERT한다. 기존 행을 지우지
          않으므로 언제 돌려도 안전하고, 알림 발송 여부(notified_at)도 보존된다.

멱등성: UNIQUE(meter_id, detected_at) 제약(01_create_schema.py의 migrate_anomaly_slot_unique)에
기대어 ON CONFLICT DO NOTHING으로 넣는다. 지속 조건 판정에 최대 3시간 lookback이 필요해
실행할 때마다 창이 겹치는데, 겹친 슬롯은 조용히 무시된다.

세 가지 모드:
    --now TS         지정 시각 기준 한 번만 판정(기본값: service_now)
    --replay A B     과거 구간을 15분씩 흘려보내며 여러 번 판정(실시간 시뮬레이션)
    --watch          실제 시계로 --interval-sec마다 반복

사용 예:
    uv run python scripts/21_detect_anomalies_incremental.py --now 2026-07-02T03:00
    uv run python scripts/21_detect_anomalies_incremental.py \\
        --replay 2026-07-02T01:00 2026-07-02T04:00 --notify
    uv run python scripts/21_detect_anomalies_incremental.py --watch --interval-sec 900
"""
import argparse
import sys
import time as time_mod
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.anomaly import compute_group_thresholds, detect_events  # noqa: E402
from ami_db.db import get_raw_connection  # noqa: E402
from ami_db.serving import service_now  # noqa: E402
from ami_db.synthetic import compute_night_baseline  # noqa: E402

# 지속 조건이 가장 긴 규칙이 연속부하 180분이라, 그 창이 통째로 들어오도록 여유를 둔다.
# 짧게 잡으면 "3시간 지속"이 창 경계에서 잘려 감지되지 않는다.
DEFAULT_LOOKBACK_HOURS = 6

INSERT_COLUMNS = (
    "meter_id", "detected_at", "level", "rule_triggered",
    "metric_value", "threshold_value", "notified_at",
)


def load_meter_ids(conn) -> list[str]:
    """판정 대상 = 상가와 매칭된 계기. 파일이 아니라 DB에서 읽는다(아래 docstring 참고)."""
    with conn.cursor() as cur:
        cur.execute("SELECT meter_id FROM stores ORDER BY store_id")
        return [r[0] for r in cur.fetchall()]


def load_meter_context(conn, meter_ids: list[str]) -> dict:
    """
    계기별 판정 기준(진짜폐점 eligibility, baseline)을 실측 데이터로 미리 계산한다.

    **입력을 전부 DB에서 읽는다.** 10단계 배치는 로컬 분석 산출물(timeseries_clean.pkl,
    store_ami_matched CSV)을 읽는데, 그 파일들은 운영 서버에 없다(배포 대상이 db/ 저장소
    뿐이고 pkl은 gitignore). 15분마다 도는 잡이 로컬 아티팩트에 의존하면 서버에서 아예
    돌지 않으므로, 같은 실측 데이터를 meter_timeseries(is_synthetic=false)에서 가져온다.

    기준은 **항상 실측(2026-04~06)으로만** 잡는다 - 합성/신규 구간을 섞으면 이상치가
    기준에 흡수돼 스스로를 정상으로 만들어 버린다(09단계 주석의 자기순환 문제와 동일).
    실행마다 다시 계산하므로 15분 배치로는 다소 무겁지만, 21개 계기 규모에서는
    수 초라 실용상 문제가 없다. 계기 수가 늘면 이 결과를 캐시할 자리다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT meter_id, contract_power_kw FROM meters WHERE meter_id = ANY(%s)", (meter_ids,)
        )
        contract_by_meter = dict(cur.fetchall())

        # compute_group_thresholds/compute_night_baseline은 'time'/'recv_kWh' 컬럼명을 기대한다
        # (원본 pkl 스키마 관례) - DB 컬럼명을 거기에 맞춰 읽는다.
        cur.execute(
            """
            SELECT meter_id, ts AS time, received_active_power_kwh AS recv_kWh
            FROM meter_timeseries
            WHERE meter_id = ANY(%s) AND is_synthetic = false
            ORDER BY meter_id, ts
            """,
            (meter_ids,),
        )
        real_all = pd.DataFrame(cur.fetchall(), columns=["meter_id", "time", "recv_kWh"])
    real_all["time"] = pd.to_datetime(real_all["time"])
    real_all["recv_kWh"] = pd.to_numeric(real_all["recv_kWh"], errors="coerce")

    context = {}
    for meter_id in meter_ids:
        real_ts = real_all[real_all["meter_id"] == meter_id]
        if real_ts.empty:
            continue
        night_baseline = compute_night_baseline(real_ts)
        eligibility, baseline_median, baseline_floor = compute_group_thresholds(
            real_ts, night_baseline
        )
        context[meter_id] = {
            "eligibility": eligibility,
            "baseline_median": baseline_median,
            "baseline_floor": baseline_floor,
            "contract_power_kw": contract_by_meter.get(meter_id),
        }
    return context


def detect_tick(conn, context: dict, now: datetime, lookback_hours: int) -> list[dict]:
    """
    [now - lookback, now] 구간을 판정하고 **새로 발견한 이벤트만** 적재해 그 목록을 돌려준다.

    반환 목록은 이번 tick에서 처음 감지된 것만 담긴다 - 이미 DB에 있던 슬롯은
    ON CONFLICT로 걸러지므로 알림 대상이 되지 않는다(재실행해도 같은 알림이 반복되지 않음).
    """
    window_start = now - timedelta(hours=lookback_hours)
    newly: list[dict] = []

    for meter_id, ctx in context.items():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mt.ts, mt.received_active_power_kwh
                FROM meter_timeseries mt
                JOIN stores s ON s.meter_id = mt.meter_id
                JOIN store_operating_status sos ON sos.store_id = s.store_id AND sos.ts = mt.ts
                WHERE mt.meter_id = %s AND sos.schedule_status = 'closed_hours'
                  AND mt.ts > %s AND mt.ts <= %s
                ORDER BY mt.ts
                """,
                (meter_id, window_start, now),
            )
            window = pd.DataFrame(cur.fetchall(), columns=["ts", "recv_kWh"])
        if window.empty:
            continue

        flagged = detect_events(
            window, ctx["eligibility"], ctx["contract_power_kw"],
            ctx["baseline_median"], ctx["baseline_floor"],
        )
        if flagged.empty:
            continue

        for row in flagged.itertuples(index=False):
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO anomaly_events ({", ".join(INSERT_COLUMNS)})
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (meter_id, detected_at) DO NOTHING
                    RETURNING event_id
                    """,
                    (meter_id, row.ts, row.level, row.rule_triggered,
                     float(row.recv_kWh), float(row.threshold_value), None),
                )
                inserted = cur.fetchone()
            if inserted:  # RETURNING이 비면 이미 있던 슬롯 - 알림 대상 아님
                newly.append({
                    "event_id": inserted[0], "meter_id": meter_id, "detected_at": row.ts,
                    "level": row.level, "rule_triggered": row.rule_triggered,
                    "metric_value": float(row.recv_kWh),
                    "threshold_value": float(row.threshold_value),
                })
    return newly


def notify(conn, events: list[dict]) -> None:
    """
    새 이벤트에 대한 알림 발송 지점.

    **실제 문자·메일 발송은 구현하지 않는다**(범위 밖). 대신 발송될 내용을 화면에 찍고
    notified_at을 채워, 같은 이벤트로 두 번 알리지 않는 흐름만 완성해 둔다. 실제 채널을
    붙일 때 이 함수 안만 바꾸면 되도록 경계를 여기로 모았다.

    문구는 LLM 설명 계층(ami_db.narrate)을 재사용하되, 외부 API가 느리거나 죽어 있어도
    배치가 멈추면 안 되므로 캐시 우선(prefer_cache=True)으로 호출하고 실패하면 건너뛴다.
    """
    if not events:
        return

    from ami_db.narrate import narrate_event
    from ami_db.serving import get_anomaly_event
    from ami_db.db import get_engine

    engine = get_engine()
    for ev in events:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.store_id FROM stores s WHERE s.meter_id = %s", (ev["meter_id"],)
            )
            row = cur.fetchone()
        if not row:
            continue
        store_id = row[0]

        message = None
        try:
            full = get_anomaly_event(engine, store_id, ev["detected_at"])
            if full:
                message = narrate_event(full, prefer_cache=True).owner_sms
        except Exception as e:  # noqa: BLE001 - 문구 생성 실패가 배치를 멈추면 안 된다
            print(f"    (설명 생성 건너뜀: {type(e).__name__})")

        if message is None:  # LLM을 못 쓰는 상황에서도 알림 자체는 나가야 한다
            message = (f"[{ev['level']}] {ev['detected_at']:%m-%d %H:%M} 비영업시간 전력 이상 감지 "
                       f"(관측 {ev['metric_value']:.2f}kWh / 임계 {ev['threshold_value']:.2f}kWh)")

        print(f"    [발송] store_id={store_id} :: {message[:110]}")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE anomaly_events SET notified_at = now() WHERE event_id = %s",
                (ev["event_id"],),
            )


def run_tick(conn, context: dict, now: datetime, lookback_hours: int, do_notify: bool) -> int:
    newly = detect_tick(conn, context, now, lookback_hours)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    if not newly:
        print(f"  [{stamp}] 새 이벤트 없음")
        return 0

    counts = {}
    for ev in newly:
        counts[ev["level"]] = counts.get(ev["level"], 0) + 1
    summary = " / ".join(f"{k} {v}건" for k, v in sorted(counts.items()))
    print(f"  [{stamp}] 신규 {len(newly)}건 ({summary})")
    for ev in newly:
        print(f"    - {ev['meter_id']} {ev['detected_at']:%H:%M} {ev['level']} "
              f"{ev['rule_triggered']}")
    if do_notify:
        notify(conn, newly)
    return len(newly)


def _parse_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        sys.exit(f"시각 형식이 올바르지 않습니다: {value!r} (예: 2026-07-02T03:00)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", help="이 시각 기준으로 1회 판정 (기본: 서버 현재)")
    parser.add_argument("--replay", nargs=2, metavar=("FROM", "TO"),
                        help="과거 구간을 15분씩 흘려보내며 반복 판정")
    parser.add_argument("--watch", action="store_true", help="실제 시계로 반복 실행")
    parser.add_argument("--interval-sec", type=int, default=900, help="--watch 주기(기본 900=15분)")
    parser.add_argument("--step-minutes", type=int, default=15, help="--replay 진행 간격")
    parser.add_argument("--delay-sec", type=float, default=0.0, help="--replay 각 tick 사이 대기")
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--notify", action="store_true", help="새 이벤트에 알림 발송(콘솔 출력 + notified_at)")
    args = parser.parse_args()

    with get_raw_connection() as conn:
        meter_ids = load_meter_ids(conn)
        print(f"판정 기준 계산 중 ({len(meter_ids)}개 계기, 실측 구간 기준)...")
        context = load_meter_context(conn, meter_ids)
        print(f"  준비 완료: {len(context)}개 계기\n")

        if args.replay:
            start, end = _parse_ts(args.replay[0]), _parse_ts(args.replay[1])
            step = timedelta(minutes=args.step_minutes)
            print(f"재생 모드: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M} "
                  f"({args.step_minutes}분 간격)\n")
            total, tick = 0, start
            while tick <= end:
                total += run_tick(conn, context, tick, args.lookback_hours, args.notify)
                tick += step
                if args.delay_sec:
                    time_mod.sleep(args.delay_sec)
            print(f"\n재생 완료 - 신규 이벤트 총 {total}건")
            return

        if args.watch:
            print(f"감시 모드: {args.interval_sec}초마다 실행 (Ctrl+C로 종료)\n")
            try:
                while True:
                    run_tick(conn, context, service_now(), args.lookback_hours, args.notify)
                    time_mod.sleep(args.interval_sec)
            except KeyboardInterrupt:
                print("\n종료합니다.")
            return

        now = _parse_ts(args.now) if args.now else service_now()
        run_tick(conn, context, now, args.lookback_hours, args.notify)


if __name__ == "__main__":
    main()
