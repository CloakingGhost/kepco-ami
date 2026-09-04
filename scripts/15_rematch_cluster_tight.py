# -*- coding: utf-8 -*-
"""
15단계: 지도에서 한눈에 보이도록 매장들을 화곡동 중심으로 밀집시킨다.

14단계가 시군구명='강서구' 전체(2만7천+ 후보)에서 무작위 순서로 후보를 시도하다 보니
일부 매장(특히 후보 풀이 얇은 KSIC코드)이 화곡동 핵심 군집에서 3~5km 떨어진
마곡/방화 권역으로 튀는 문제가 생겼다(예: store_id=5 바빈스커피방화점은 5.4km
떨어짐). A선로 기반 데모라는 컨셉("한 동네 배전선로가 커버하는 골목상권")과도
어긋나고, 지도에 찍었을 때 한눈에 안 들어온다.

해결: 후보를 무작위가 아니라 화곡동 CSV 중심좌표(HWAGOK_CENTROID, 법정동명='화곡동'
7,934건의 평균 위경도)에서 가까운 순으로 정렬해 가장 가까운 후보부터 시도한다.
KSIC 5자리 완전일치 + 강서구 지역검증 + 이름유사도 + 실제 영업시간 존재, 4가지
기준은 12~14단계와 동일하게 유지 - "가능하면 가깝게, 안 되면 그다음으로 가까운
후보"로 우선순위만 바꾼 것이지 검증 기준을 낮추지 않는다.

대상: 14단계에서 화곡동 중심 2.5km를 벗어난 9개 매장(store_id 1,2,3,5,9,11,16,17,20).
"""
import re
import sys
import time
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.config import DATA_DIR, settings  # noqa: E402
from ami_db.db import get_raw_connection  # noqa: E402
from ami_db.hours_parser import is_no_info  # noqa: E402
from ami_db.matching import STORE_USECOLS, ksic5  # noqa: E402
from ami_db.places_client import GooglePlacesClient  # noqa: E402
from ami_db.places_sync import name_similarity, save_verified_place  # noqa: E402

TARGET_STORE_IDS = [1, 2, 3, 5, 9, 11, 16, 17, 20]
NAME_SIMILARITY_THRESHOLD = 0.5
MAX_CANDIDATES_PER_STORE = 60
CALL_INTERVAL_SEC = 0.2
HWAGOK_CENTROID = (37.53942791977127, 126.84628840419111)  # (lat, lng) - 화곡동 CSV 7,934건 평균


def ami_ksic5(code: str) -> str | None:
    if not isinstance(code, str):
        return None
    m = re.match(r"\s*(\d{5})", code)
    return m.group(1) if m else None


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dl = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(a))


def main() -> None:
    gu_name = "강서구"
    location = f"서울특별시 {gu_name}"
    store_csv = DATA_DIR / "소상공인시장진흥공단_상가(상권)정보_서울_202606.csv"
    client = GooglePlacesClient(settings.google_places_api_key)

    print(f"[1/3] 상가정보 CSV 로드 및 시군구명='{gu_name}' 필터, 화곡동 중심좌표 기준 거리 계산")
    store_df = pd.read_csv(store_csv, usecols=STORE_USECOLS, encoding="utf-8-sig", dtype=str)
    store_df = store_df[store_df["시군구명"] == gu_name].copy()
    store_df["ksic5"] = store_df["표준산업분류코드"].apply(ksic5)
    store_df["경도_f"] = pd.to_numeric(store_df["경도"], errors="coerce")
    store_df["위도_f"] = pd.to_numeric(store_df["위도"], errors="coerce")
    store_df = store_df.dropna(subset=["경도_f", "위도_f"])
    store_df["dist_km"] = store_df.apply(
        lambda r: haversine_km(HWAGOK_CENTROID[0], HWAGOK_CENTROID[1], r["위도_f"], r["경도_f"]), axis=1
    )

    with get_raw_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, road_address FROM stores")
        used_pairs = {(n, a) for n, a in cur.fetchall()}
        used_names = {n for n, a in used_pairs}

        cur.execute(
            "SELECT s.store_id, s.meter_id, s.name, m.ksic_code "
            "FROM stores s JOIN meters m ON m.meter_id = s.meter_id "
            "WHERE s.store_id = ANY(%s) ORDER BY s.store_id",
            (TARGET_STORE_IDS,),
        )
        targets = cur.fetchall()

    print(f"[2/3] 교체 대상 {len(targets)}개 매장 - 화곡동 중심에서 가까운 후보부터 시도")

    results = []
    for store_id, meter_id, old_name, meter_ksic_code in targets:
        code5 = ami_ksic5(meter_ksic_code)
        pool = store_df[store_df["ksic5"] == code5].copy()
        pool = pool[~pool.apply(
            lambda r: (r["상호명"], r["도로명주소"]) in used_pairs or r["상호명"] in used_names, axis=1
        )]
        pool = pool.sort_values("dist_km").reset_index(drop=True)

        print(f"\n[{store_id}] {old_name} (meter={meter_id}, ksic5={code5}) - "
              f"후보 {len(pool)}개(가까운 순) 중 최대 {MAX_CANDIDATES_PER_STORE}개 시도")

        picked = None
        for _, cand in pool.head(MAX_CANDIDATES_PER_STORE).iterrows():
            cand_name = cand["상호명"]
            near = (cand["위도_f"], cand["경도_f"])

            place_info, raw, place_id = client.search_by_name(cand_name, location, near=near)
            time.sleep(CALL_INTERVAL_SEC)
            if place_info is None:
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) - 검색결과없음')
                continue

            address = (raw or {}).get("formatted_address", "")
            if "강서구" not in address:
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" - 지역검증 실패 ({address})')
                continue

            sim = name_similarity(cand_name, place_info.name)
            if sim < NAME_SIMILARITY_THRESHOLD:
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" - 이름유사도 {sim:.2f}')
                continue

            if is_no_info(place_info.hours.get_formatted_hours()):
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" - 영업시간 정보 없음')
                continue

            print(f'    ✓ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" (유사도 {sim:.2f}, {address})')
            picked = (cand, place_info, raw, place_id)
            break

        if picked is None:
            print(f"  [{store_id}] 검증 통과 후보 없음 - 기존 정체성(\"{old_name}\") 유지")
            results.append((store_id, old_name, None, None))
            continue

        cand, place_info, raw, place_id = picked
        with get_raw_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE stores SET
                    name = %s, branch_name = %s, road_address = %s, building_name = %s,
                    floor_info = %s, longitude = %s, latitude = %s,
                    biz_category_large = %s, biz_category_mid = %s, ksic_code = %s
                WHERE store_id = %s
                """,
                (
                    cand["상호명"], cand["지점명"] or None, cand["도로명주소"], cand["건물명"] or None,
                    cand["층정보"] or None, float(cand["경도_f"]), float(cand["위도_f"]),
                    cand["상권업종대분류명"], cand["상권업종중분류명"], cand["표준산업분류코드"],
                    store_id,
                ),
            )
            cur.execute("DELETE FROM google_places_cache WHERE store_id = %s", (store_id,))
            cur.execute(
                "DELETE FROM store_operating_hours WHERE store_id = %s AND source = 'google_places'",
                (store_id,),
            )
            save_verified_place(conn, store_id, place_id, place_info, raw or {})

        used_pairs.add((cand["상호명"], cand["도로명주소"]))
        used_names.add(cand["상호명"])
        results.append((store_id, old_name, cand["상호명"], cand["dist_km"]))
        print(f'  [{store_id}] 교체 완료: "{old_name}" -> "{cand["상호명"]}" ({cand["dist_km"]:.2f}km)')

    print("\n[3/3] === 요약 ===")
    for store_id, old_name, new_name, dist in results:
        if new_name:
            print(f"  store_id={store_id}: \"{old_name}\" -> \"{new_name}\" ({dist:.2f}km)")
        else:
            print(f"  store_id={store_id}: \"{old_name}\" 유지(검증 실패)")

    print(
        "\n⚠️  store_operating_hours가 바뀐 매장이 있으므로 "
        "09_compute_operating_status.py를 반드시 다시 실행해서 store_operating_status를 갱신하세요."
    )


if __name__ == "__main__":
    main()
