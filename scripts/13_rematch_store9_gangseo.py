# -*- coding: utf-8 -*-
"""
13단계: store_id=9(KSIC 75919, 기타 사무지원 서비스업)만 단독으로 재매칭한다.

12단계(scripts/12_rematch_verified_stores.py)가 화곡동(법정동명) 범위 안에서
동일 KSIC 5자리 후보를 찾다가 store_id=9만 실패했다 - 화곡동 전체에 75919
후보가 원래 4개뿐이라(1개는 이미 다른 매장이 사용 중), 남은 3개를 전부 검증해도
Google에 영업시간이 등록된 곳이 없었음(사무지원서비스업 자체가 실적으로 Google
Business 프로필 등록률이 낮은 업종군으로 보임).

이 스크립트는 후보 소스 필터만 시군구명='강서구'로 넓힌다(법정동명='화곡동' 대신) -
"완전히 다른 지역으로 가면 안 된다"는 원칙은 지키면서(같은 강서구), 표본을
넓혀 영업시간이 실제로 등록된 후보를 찾을 여지를 만드는 것. KSIC 5자리 완전일치
제약은 그대로 유지한다.
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

STORE_ID = 9
NAME_SIMILARITY_THRESHOLD = 0.5
MAX_CANDIDATES = 40
CALL_INTERVAL_SEC = 0.2
RNG_SEED = 99


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

    print(f"[1/3] 상가정보 CSV 로드 및 시군구명='{gu_name}' 필터(store_id={STORE_ID} 단독 재매칭)")
    store_df = pd.read_csv(store_csv, usecols=STORE_USECOLS, encoding="utf-8-sig", dtype=str)
    store_df = store_df[store_df["시군구명"] == gu_name].copy()
    store_df["ksic5"] = store_df["표준산업분류코드"].apply(ksic5)
    store_df["경도_f"] = pd.to_numeric(store_df["경도"], errors="coerce")
    store_df["위도_f"] = pd.to_numeric(store_df["위도"], errors="coerce")

    with get_raw_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT name, road_address FROM stores")
        used_pairs = {(n, a) for n, a in cur.fetchall()}
        cur.execute(
            "SELECT s.store_id, s.meter_id, s.name, m.ksic_code "
            "FROM stores s JOIN meters m ON m.meter_id = s.meter_id WHERE s.store_id = %s",
            (STORE_ID,),
        )
        store_id, meter_id, old_name, meter_ksic_code = cur.fetchone()

    code5 = ami_ksic5(meter_ksic_code)
    pool = store_df[store_df["ksic5"] == code5].copy()
    pool = pool[~pool.apply(lambda r: (r["상호명"], r["도로명주소"]) in used_pairs, axis=1)]
    pool = pool.dropna(subset=["경도_f", "위도_f"]).reset_index(drop=True)

    rng = np.random.default_rng(RNG_SEED)
    order = rng.permutation(len(pool))

    print(f"[2/3] {old_name} (meter={meter_id}, ksic5={code5}) - "
          f"강서구 전체 후보 {len(pool)}개 중 최대 {MAX_CANDIDATES}개 시도")

    picked = None
    for i in order[:MAX_CANDIDATES]:
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
        print(f"\n[3/3] 검증 통과 후보 없음 - 기존 정체성(\"{old_name}\") 유지")
        return

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

    print(f'\n[3/3] 교체 완료: "{old_name}" -> "{cand["상호명"]}"')
    print("\n⚠️  09_compute_operating_status.py를 다시 실행해서 store_operating_status를 갱신하세요.")


if __name__ == "__main__":
    main()
