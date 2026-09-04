# -*- coding: utf-8 -*-
"""
14단계: 실제 Google 영업시간이 없는 나머지 10개 매장(store_id 1,2,3,5,8,10,11,16,17,20)을
강서구 전체(시군구명 기준, 1만개+ 후보) 범위에서 재매칭한다.

12/13단계가 화곡동(법정동명) -> 강서구(시군구명) 순으로 범위를 넓혀가며 7개 매장을
처리했는데, 나머지 10개도 같은 문제(이름/업체 자체는 맞지만 Google에 영업시간이
등록 안 됨, 또는 애초에 지역검증에서 아예 탈락해 캐시 자체가 없음 - store_id 3, 20)를
안고 있다는 게 확인됐다. "화곡동 안에서만 찾다가 실패"가 반복되는 걸 막기 위해
이번엔 처음부터 시군구명='강서구'로 후보를 뽑는다 - 13단계에서 이미 검증된 전략
(같은 KSIC 5자리 유지 + 지역만 구 단위로 확대)을 10개 매장에 동시 적용하는 것뿐,
새로운 알고리즘은 아니다.

검증 기준(12/13단계와 동일, 3가지 모두 통과해야 채택):
  1. formatted_address에 "강서구" 포함(지역 검증)
  2. 후보 상호명과 Google이 찾은 이름의 유사도(name_similarity) >= 0.5
  3. Google에 실제 영업시간 정보가 있음(is_no_info()가 False)

이미 21개 매장에 배정된 (상호명, 도로명주소) 쌍은 후보 풀에서 제외한다(중복 배정 방지).
"""
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.config import DATA_DIR, settings  # noqa: E402
from ami_db.db import get_raw_connection  # noqa: E402
from ami_db.hours_parser import is_no_info  # noqa: E402
from ami_db.matching import STORE_USECOLS, ksic5  # noqa: E402
from ami_db.places_client import GooglePlacesClient  # noqa: E402
from ami_db.places_sync import name_similarity, save_verified_place  # noqa: E402

TARGET_STORE_IDS = [1, 2, 3, 5, 8, 10, 11, 16, 17, 20]
NAME_SIMILARITY_THRESHOLD = 0.5
MAX_CANDIDATES_PER_STORE = 40
CALL_INTERVAL_SEC = 0.2
RNG_SEED = 202


def ami_ksic5(code: str) -> str | None:
    if not isinstance(code, str):
        return None
    m = re.match(r"\s*(\d{5})", code)
    return m.group(1) if m else None


def main() -> None:
    gu_name = "강서구"
    location = f"서울특별시 {gu_name}"
    store_csv = DATA_DIR / "소상공인시장진흥공단_상가(상권)정보_서울_202606.csv"
    client = GooglePlacesClient(settings.google_places_api_key)

    print(f"[1/3] 상가정보 CSV 로드 및 시군구명='{gu_name}' 필터")
    store_df = pd.read_csv(store_csv, usecols=STORE_USECOLS, encoding="utf-8-sig", dtype=str)
    store_df = store_df[store_df["시군구명"] == gu_name].copy()
    store_df["ksic5"] = store_df["표준산업분류코드"].apply(ksic5)
    store_df["경도_f"] = pd.to_numeric(store_df["경도"], errors="coerce")
    store_df["위도_f"] = pd.to_numeric(store_df["위도"], errors="coerce")
    print(f"  -> 강서구 전체 후보 {len(store_df)}건")

    with get_raw_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, road_address FROM stores")
        used_pairs = {(n, a) for n, a in cur.fetchall()}

        cur.execute(
            "SELECT s.store_id, s.meter_id, s.name, m.ksic_code "
            "FROM stores s JOIN meters m ON m.meter_id = s.meter_id "
            "WHERE s.store_id = ANY(%s) ORDER BY s.store_id",
            (TARGET_STORE_IDS,),
        )
        targets = cur.fetchall()

    print(f"[2/3] 교체 대상 {len(targets)}개 매장, 이미 배정된 후보 {len(used_pairs)}건 제외 후 후보 탐색 시작")

    results = []
    for store_id, meter_id, old_name, meter_ksic_code in targets:
        code5 = ami_ksic5(meter_ksic_code)
        pool = store_df[store_df["ksic5"] == code5].copy()
        pool = pool[~pool.apply(lambda r: (r["상호명"], r["도로명주소"]) in used_pairs, axis=1)]
        pool = pool.dropna(subset=["경도_f", "위도_f"]).reset_index(drop=True)

        rng = np.random.default_rng(RNG_SEED + store_id)
        order = rng.permutation(len(pool))

        print(f"\n[{store_id}] {old_name} (meter={meter_id}, ksic5={code5}) - "
              f"강서구 전체 후보 {len(pool)}개 중 최대 {MAX_CANDIDATES_PER_STORE}개 시도")

        picked = None
        for i in order[:MAX_CANDIDATES_PER_STORE]:
            cand = pool.iloc[int(i)]
            cand_name = cand["상호명"]
            near = (cand["위도_f"], cand["경도_f"])

            place_info, raw, place_id = client.search_by_name(cand_name, location, near=near)
            time.sleep(CALL_INTERVAL_SEC)
            if place_info is None:
                print(f'    ✗ "{cand_name}" - 검색결과없음')
                continue

            address = (raw or {}).get("formatted_address", "")
            if "강서구" not in address:
                print(f'    ✗ "{cand_name}" -> "{place_info.name}" - 지역검증 실패 ({address})')
                continue

            sim = name_similarity(cand_name, place_info.name)
            if sim < NAME_SIMILARITY_THRESHOLD:
                print(f'    ✗ "{cand_name}" -> "{place_info.name}" - 이름유사도 {sim:.2f} < {NAME_SIMILARITY_THRESHOLD}')
                continue

            if is_no_info(place_info.hours.get_formatted_hours()):
                print(f'    ✗ "{cand_name}" -> "{place_info.name}" - Google에 영업시간 정보 없음')
                continue

            print(f'    ✓ "{cand_name}" -> "{place_info.name}" (유사도 {sim:.2f}, 영업시간 확보, {address})')
            picked = (cand, place_info, raw, place_id)
            break

        if picked is None:
            print(f"  [{store_id}] 검증 통과 후보 없음 - 기존 정체성(\"{old_name}\") 유지")
            results.append((store_id, old_name, None))
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
        results.append((store_id, old_name, cand["상호명"]))
        print(f'  [{store_id}] 교체 완료: "{old_name}" -> "{cand["상호명"]}"')

    print("\n[3/3] === 요약 ===")
    for store_id, old_name, new_name in results:
        status = f'"{old_name}" -> "{new_name}"' if new_name else f'"{old_name}" 유지(검증 실패)'
        print(f"  store_id={store_id}: {status}")

    print(
        "\n⚠️  store_operating_hours가 바뀐 매장이 있으므로 "
        "09_compute_operating_status.py를 반드시 다시 실행해서 store_operating_status를 갱신하세요."
    )


if __name__ == "__main__":
    main()
