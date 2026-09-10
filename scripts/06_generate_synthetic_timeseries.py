# -*- coding: utf-8 -*-
"""
2-5단계 전반: 실측 종료 시점 이후 ~ 오늘까지 합성 시계열을 생성한다(파일 저장만,
DB 쓰기 없음 - 07_load_synthetic_timeseries.py가 이 parquet을 읽어 적재한다).

반드시 04/05(Google Places 운영시간 적재) 이후에 실행해야 한다 - 휴업 시나리오를
심으려면 "그 상가가 언제 영업 중인지"(store_operating_hours)를 알아야 하기 때문.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db import resolution  # noqa: E402
from ami_db.config import GENERATED_DIR, VIZ_OUTPUT, settings  # noqa: E402
from ami_db.db import get_raw_connection  # noqa: E402
from ami_db.status import load_effective_hours  # noqa: E402
from ami_db.synthetic import (  # noqa: E402
    build_slot_profile,
    compute_night_baseline,
    inject_closure_scenario,
    inject_mock_anomaly,
    inject_sustained_load_scenario,
    select_scenarios,
    synthesize_day,
)

# 합성 구간 상한. date.today()가 아니라 2026-07-31 고정이다 - 서비스가 조회를 허용하는
# 범위(ami_db.serving.LATEST_SERVICE_DATE)와 정확히 일치시켜야, "조회는 되는데 데이터가
# 없는 날짜"나 반대로 "데이터는 있는데 조회가 막힌 날짜"가 생기지 않는다.
SYNTHETIC_END_DATE = date(2026, 7, 31)

# 안전감지 데모 시나리오.
# 실측 3개월(04-01~06-30)에는 위험 등급 상황이 실제로 0건이다(ami_db.anomaly 모듈
# docstring의 실측 검증 참고) - 실제로 전기사고가 없었으니 그게 정답이다. 그래서 감지
# 로직이 동작하는 걸 보여주려면 7월 합성 구간에 위험/주의 상황을 의도적으로 심어야 한다.
# 06의 자동 시나리오 선정(select_scenarios) 및 그 공유 rng와는 무관한 별도 rng로 주입해
# 나머지 매장의 합성값에는 일절 영향을 주지 않는다.
#
# load_ratio는 "감지 임계치"가 아니라 "실제로 주입할 부하 수준"이다 - 임계치에 딱 맞춰
# 주입하면 노이즈로 일부 슬롯이 미달돼 지속 조건이 깨진다(inject_sustained_load_scenario
# docstring 참고). 그래서 임계치(1.30 / 0.80)에서 충분히 떨어뜨려 잡았다.
MANUAL_SCENARIOS = [
    {
        "meter_id": "A-L-60", "date": date(2026, 7, 2),   # 목요일, 영업 11:00~22:00
        "start_slot": 8, "duration_slots": 4,              # 02:00~02:45 = 60분
        "load_ratio": 1.70,                                # 위험 임계 130% 초과
        "kind": "danger_overload",
        "detail": "계약전력 170%를 60분 지속 - KEC212.3(산업용 표) 위험(사고 발생) 조건",
    },
    {
        "meter_id": "A-L-65", "date": date(2026, 7, 15),  # 수요일, 00:00~03:00 폐점 확인됨
        "start_slot": 0, "duration_slots": 12,             # 00:00~02:45 = 3시간
        "load_ratio": 0.95,                                # 연속부하 80% 초과, 위험 130% 미만
        "kind": "continuous_load",
        "detail": "계약전력 95%를 3시간 지속 - 연속부하 80% 규칙 초과(사고 충분조건)",
    },
    # "주의반복"(하루 3회) 화면 표시 데모용 - A-L-63(소문난순대, 계약 13kW)에 새벽
    # 01/03/05시 세 번, 각 60분씩 별도 사건을 심는다. load_ratio=0.25는 매장 자신의
    # baseline+3x floor(약 0.648kWh)는 넉넉히 넘지만 계약전력 80%/130% 근처는 전혀
    # 아니므로 empty_store_baseline_3x_60min(주의)만 걸리고 continuous_load/danger는
    # 걸리지 않는다(3-1절 baseline 표 근거). 기존 A-L-60/A-L-65 시나리오와는 날짜가
    # 겹치지 않는다.
    {
        "meter_id": "A-L-63", "date": date(2026, 7, 22),  # 수요일
        "start_slot": 4, "duration_slots": 4,              # 01:00~01:45 = 60분 (1/3)
        "load_ratio": 0.25,
        "kind": "caution_repeat_1",
        "detail": "매장 자체 baseline+3x floor를 60분 지속 초과 - 주의반복 데모 1/3",
    },
    {
        "meter_id": "A-L-63", "date": date(2026, 7, 22),
        "start_slot": 12, "duration_slots": 4,             # 03:00~03:45 = 60분 (2/3)
        "load_ratio": 0.25,
        "kind": "caution_repeat_2",
        "detail": "매장 자체 baseline+3x floor를 60분 지속 초과 - 주의반복 데모 2/3",
    },
    {
        "meter_id": "A-L-63", "date": date(2026, 7, 22),
        "start_slot": 20, "duration_slots": 4,             # 05:00~05:45 = 60분 (3/3)
        "load_ratio": 0.25,
        "kind": "caution_repeat_3",
        "detail": "매장 자체 baseline+3x floor를 60분 지속 초과 - 주의반복 데모 3/3",
    },
    # ── 점포 확대(2026-09-10) ────────────────────────────────────────────────
    # 위 3개 매장만으로는 "한 달 동안 상권에 무슨 일이 있었나"를 볼 수 없어(기간 조회 API
    # 대상이 3개 매장뿐) 6개 매장을 7월 전반에 걸쳐 추가한다. 날짜를 흩뿌려 놓아야
    # 기간 조회(그 달 1일~요청 시점)가 조회 시점에 따라 다르게 쌓이는 걸 확인할 수 있다.
    #
    # load_ratio 산정: 주입값 = 계약전력 x load_ratio / 4 (15분 kWh). 주의(개인화 baseline)
    # 시나리오는 이 값이 그 매장의 "주의임계(baseline_median + 3 x floor)"를 넘되 계약전력
    # 80%(연속부하)·130%(KEC)에는 한참 못 미치도록 잡았다. 임계값 출처는
    # docs/안전감지_이상치_판정기준.md 3-1절 실측 baseline 표.
    # 주입 시각은 전부 새벽(00:30~05:45)이다 - "진짜폐점" 판정이 서는 시간대여야 규칙이
    # 평가 대상으로 삼는다.
    {
        "meter_id": "A-L-34", "date": date(2026, 7, 5),   # 다이소목동, 계약 25kW
        "start_slot": 12, "duration_slots": 4,             # 03:00~03:45
        "load_ratio": 1.55,                                # 위험 임계 130% 초과(두 번째 위험 매장)
        "kind": "danger_overload",
        "detail": "계약전력 155%를 60분 지속 - KEC212.3 위험 조건",
    },
    {
        "meter_id": "A-L-62", "date": date(2026, 7, 9),   # 놀부부대찌개, 계약 15kW
        "start_slot": 2, "duration_slots": 12,             # 00:30~03:15 = 3시간
        "load_ratio": 0.92,                                # 연속부하 80% 초과, 130% 미만
        "kind": "continuous_load",
        "detail": "계약전력 92%를 3시간 지속 - 연속부하 80% 규칙 초과",
    },
    {
        "meter_id": "A-L-11", "date": date(2026, 7, 11),  # 배떡, 계약 8kW (주의임계 0.456kWh)
        "start_slot": 8, "duration_slots": 4,              # 02:00~02:45
        "load_ratio": 0.30,                                # 0.60kWh - 임계 초과, 계약 30%
        "kind": "caution_baseline",
        "detail": "매장 자체 baseline+3x floor를 60분 지속 초과",
    },
    {
        "meter_id": "A-L-72", "date": date(2026, 7, 14),  # 대박해물찜, 계약 25kW (주의임계 0.520kWh)
        "start_slot": 16, "duration_slots": 4,             # 04:00~04:45
        "load_ratio": 0.15,                                # 0.94kWh - 임계 초과, 계약 15%
        "kind": "caution_baseline",
        "detail": "매장 자체 baseline+3x floor를 60분 지속 초과",
    },
    # 두찜: 하루 2회 반복 - 주의반복(3회) 문턱에 못 미치는 경우도 화면에 있어야
    # "반복 판정"이 실제로 걸러내고 있음을 보여줄 수 있다.
    {
        "meter_id": "A-L-80", "date": date(2026, 7, 18),  # 두찜강서, 계약 19kW (주의임계 0.665kWh)
        "start_slot": 4, "duration_slots": 4,              # 01:00~01:45 (1/2)
        "load_ratio": 0.20,                                # 0.95kWh - 임계 초과, 계약 20%
        "kind": "caution_baseline_1",
        "detail": "매장 자체 baseline+3x floor 초과 - 하루 2회 중 1회차",
    },
    {
        "meter_id": "A-L-80", "date": date(2026, 7, 18),
        "start_slot": 16, "duration_slots": 4,             # 04:00~04:45 (2/2)
        "load_ratio": 0.20,
        "kind": "caution_baseline_2",
        "detail": "매장 자체 baseline+3x floor 초과 - 하루 2회 중 2회차",
    },
    {
        "meter_id": "A-L-57", "date": date(2026, 7, 25),  # 분식을품다, 계약 10kW (주의임계 0.647kWh)
        "start_slot": 12, "duration_slots": 4,             # 03:00~03:45
        "load_ratio": 0.30,                                # 0.75kWh - 임계 초과, 계약 30%
        "kind": "caution_baseline",
        "detail": "매장 자체 baseline+3x floor를 60분 지속 초과",
    },
    {
        "meter_id": "A-L-34", "date": date(2026, 7, 28),  # 다이소목동 재발(위험 이후 주의)
        "start_slot": 8, "duration_slots": 4,              # 02:00~02:45
        "load_ratio": 0.30,                                # 1.88kWh - 주의임계 1.574kWh 초과
        "kind": "caution_baseline",
        "detail": "매장 자체 baseline+3x floor 초과 - 같은 매장의 위험 이후 재발 이력",
    },
]


def main() -> None:
    matched_csv = GENERATED_DIR / f"store_ami_matched_{settings.target_dong}.csv"
    matched = pd.read_csv(matched_csv, encoding="utf-8-sig")
    meter_ids = matched["meter_id"].tolist()

    print(f"[1/5] 실측 시계열 로드 및 계기별 필터: {len(meter_ids)}개 계기")
    ts_all = pd.read_pickle(VIZ_OUTPUT / "timeseries_clean.pkl")
    ts_all = ts_all[ts_all["meter_id"].isin(meter_ids)]

    with get_raw_connection() as conn:
        # meter_id -> store_id, store_id별 요일별 유효 운영시간(google_places 우선)
        with conn.cursor() as cur:
            cur.execute("SELECT meter_id, store_id FROM stores")
            meter_to_store = dict(cur.fetchall())
        hours_by_store = load_effective_hours(conn)

        # 02단계가 이미 계산해 둔 data_resolution을 재사용 - 여기서도 02와 동일한 코호트
        # 재분배를 각자 다시 계산해서 적용한다(같은 resolution.py 함수 공유, pkl을 독립적으로
        # 재읽는 기존 09단계 패턴과 동일).
        with conn.cursor() as cur:
            cur.execute("SELECT meter_id, data_resolution FROM meters WHERE meter_id = ANY(%s)", (meter_ids,))
            resolution_by_meter = dict(cur.fetchall())

        with conn.cursor() as cur:
            cur.execute(
                "SELECT meter_id, contract_power_kw FROM meters WHERE meter_id = ANY(%s)",
                ([s["meter_id"] for s in MANUAL_SCENARIOS],),
            )
            manual_contract_kw = dict(cur.fetchall())

    ratios_by_meter: dict[str, dict[int, tuple]] = {}
    meter_meta_by_meter: dict[str, dict] = {}  # 감사 리포트용 - biz_mid/biz_large/cohort_ids
    skipped_report: dict[str, str] = {}
    for meter_id in meter_ids:
        if resolution_by_meter.get(meter_id) != "1hour":
            continue
        target_row = matched.loc[matched["meter_id"] == meter_id]
        if target_row.empty:
            skipped_report[meter_id] = "matched 목록에 없음 (00단계 산출물에서 누락)"
            continue
        biz_mid = target_row["biz_category_mid"].iloc[0]
        biz_large = target_row["biz_category_large"].iloc[0]
        cohort_ids = resolution.cohort_meter_ids(matched, resolution_by_meter, meter_id, biz_mid, biz_large)
        if len(cohort_ids) < resolution.MIN_COHORT_SIZE:
            skipped_report[meter_id] = f"코호트 부족 (biz_mid={biz_mid}, biz_large={biz_large}, 확보 {len(cohort_ids)}개 < {resolution.MIN_COHORT_SIZE})"
            continue
        cohort_ts = ts_all.loc[ts_all["meter_id"].isin(cohort_ids), ["time", "recv_kWh"]]
        ratios = resolution.build_hourly_ratio_table(cohort_ts)
        if not ratios:
            skipped_report[meter_id] = f"코호트({len(cohort_ids)}개)는 확보했지만 유효한 시간대별 비율이 하나도 없음"
            continue
        ratios_by_meter[meter_id] = ratios
        meter_meta_by_meter[meter_id] = {
            "biz_category_mid": biz_mid, "biz_category_large": biz_large, "cohort_meter_ids": cohort_ids,
        }
        print(f"  -> {meter_id} 합성 프로파일도 코호트 재분배 적용 (코호트 {len(cohort_ids)}개, biz_mid={biz_mid})")

    effective_hours_by_meter = {
        m: hours_by_store.get(meter_to_store[m], {}) for m in meter_ids if m in meter_to_store
    }

    # 전 계기 공통으로 쓸 합성 날짜 범위: (모든 매칭 계기 중 실측이 가장 늦게 끝난 날짜) + 1일 ~ 오늘.
    # 가장 늦은 날짜를 기준으로 잡아야 어떤 계기도 실측 구간과 합성 구간이 겹치지 않는다.
    last_real_date = ts_all.groupby("meter_id")["time"].max().dt.date.max()
    synthetic_dates = [
        last_real_date + timedelta(days=i)
        for i in range(1, (SYNTHETIC_END_DATE - last_real_date).days + 1)
    ]
    print(f"[2/5] 합성 날짜 범위: {synthetic_dates[0] if synthetic_dates else '없음'} ~ "
          f"{SYNTHETIC_END_DATE} ({len(synthetic_dates)}일)")

    rng = np.random.default_rng(settings.rng_seed)
    manual_rng = np.random.default_rng(20260702)  # 공유 rng와 완전히 분리 - 다른 계기 데이터 불변 보장
    scenarios = select_scenarios(meter_ids, synthetic_dates, effective_hours_by_meter, rng)
    scenario_by_key = {(s.meter_id, s.target_date): s for s in scenarios}
    print(f"[3/5] 시나리오 선정: 휴업 {sum(1 for s in scenarios if s.kind=='closure')}건, "
          f"이상치 {sum(1 for s in scenarios if s.kind=='anomaly')}건")

    print("[4/5] 계기별 프로파일 산출 및 일별 합성 생성...")
    all_days = []
    scenario_log_rows = []
    for meter_id in meter_ids:
        meter_ts = ts_all[ts_all["meter_id"] == meter_id]
        if meter_ts.empty:
            print(f"  ⚠ {meter_id}: 실측 시계열이 없어 건너뜀")
            continue

        ratios = ratios_by_meter.get(meter_id)
        is_redistributed_meter = ratios is not None
        if is_redistributed_meter:
            # 프로파일 산출 전에 정각 실측을 코호트 비율로 재분배해 15/30/45분을 보충한다 -
            # 이렇게 안 하면 이 계기들의 (요일x슬롯) 그룹이 91일 내내 전부 NaN이라
            # build_slot_profile이 여전히 NaN을 그대로 반환하고 만다(정각만 프로파일링됨).
            meter_ts = resolution.redistribute_hourly_store(meter_ts[["time", "recv_kWh"]], ratios)

        profile = build_slot_profile(meter_ts)
        night_baseline = compute_night_baseline(meter_ts)

        for d in synthetic_dates:
            day_df = synthesize_day(profile, d, rng)
            scenario = scenario_by_key.get((meter_id, d))
            if scenario is not None:
                if scenario.kind == "closure":
                    day_df = inject_closure_scenario(day_df, night_baseline, scenario.slot, rng)
                elif scenario.kind == "anomaly":
                    day_df = inject_mock_anomaly(day_df, scenario.slot, multiplier=6.0, rng=rng)
                scenario_log_rows.append({
                    "meter_id": meter_id, "date": d, "kind": scenario.kind,
                    "slot": scenario.slot, "detail": scenario.detail,
                })
            for sc in MANUAL_SCENARIOS:
                cp = manual_contract_kw.get(sc["meter_id"])
                if meter_id != sc["meter_id"] or d != sc["date"] or not cp:
                    continue
                day_df = inject_sustained_load_scenario(
                    day_df, cp, sc["start_slot"], sc["duration_slots"], sc["load_ratio"], manual_rng,
                )
                scenario_log_rows.append({
                    "meter_id": meter_id, "date": d, "kind": sc["kind"], "slot": sc["start_slot"],
                    "detail": f"{sc['detail']} (계약전력 {cp}kW, 수동 주입)",
                })
            day_df["meter_id"] = meter_id
            # 재분배 적용 계기(A-L-58)는 15/30/45분 슬롯(정각이 아닌 슬롯)이 코호트 비율로
            # 추정된 프로파일에서 파생됐음을 그대로 물려준다 - 정각(slot%4==0) 슬롯은 실측
            # 정각 분포에서 샘플링된 것이라 False 유지.
            day_df["is_redistributed"] = (day_df["slot"] % 4 != 0) if is_redistributed_meter else False
            all_days.append(day_df)

    synthetic_ts = pd.concat(all_days, ignore_index=True)
    synthetic_ts = synthetic_ts[["meter_id", "ts", "recv_kWh", "is_redistributed"]]
    synthetic_ts["is_synthetic"] = True

    # 코호트 비율표는 DB/parquet 어디에도 남지 않으므로(각 실행마다 재계산 후 소비하고 버림),
    # "이 합성 데이터가 실제로 어떤 비율로 재분배됐는지" 감사용 JSON을 별도로 남긴다.
    redistributed_counts = (
        synthetic_ts[synthetic_ts["is_redistributed"]].groupby("meter_id").size().to_dict()
    )
    applied_report = {
        meter_id: {
            **meter_meta_by_meter[meter_id],
            "hourly_ratios": {str(h): list(r) for h, r in ratios_by_meter[meter_id].items()},
            "redistributed_row_count": int(redistributed_counts.get(meter_id, 0)),
        }
        for meter_id in ratios_by_meter
    }
    report_path = GENERATED_DIR / f"redistribution_ratios_synthetic_{settings.target_dong}.json"
    resolution.write_redistribution_report(
        report_path, "06_generate_synthetic_timeseries.py", applied_report, skipped_report
    )

    print(f"[5/5] 저장 중... 총 {len(synthetic_ts)}행")
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    parquet_path = GENERATED_DIR / f"synthetic_timeseries_{settings.target_dong}.parquet"
    log_path = GENERATED_DIR / f"synthetic_scenario_log_{settings.target_dong}.csv"
    synthetic_ts.to_parquet(parquet_path, index=False)
    pd.DataFrame(scenario_log_rows).to_csv(log_path, index=False, encoding="utf-8-sig")

    print(f"저장: {parquet_path}")
    print(f"저장: {report_path} (코호트 비율표 감사 리포트)")
    print(f"저장: {log_path} ({len(scenario_log_rows)}건 시나리오 기록 - QA용, DB 비적재)")


if __name__ == "__main__":
    main()
