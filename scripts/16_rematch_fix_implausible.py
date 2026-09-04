# -*- coding: utf-8 -*-
"""
16단계: store_id 9, 16을 다시 매칭한다.

계기는 다음과 같다:
  - store_id=9(meter A-L-49, 계약전력 3kW, ksic5=75919 기타 사업지원 서비스업):
    15단계에서 "늘봄"(2글자) 후보를 검색했더니 Google이 완전히 무관한
    "강서늘봄동물병원"을 1위로 반환했는데, 예전 name_similarity()가 "늘봄"이
    "강서늘봄동물병원" 안에 부분 문자열로 포함된다는 이유만으로 유사도 1.0을 줘서
    통과시켰다(실측 버그, ami_db.places_sync.name_similarity 주석 참고 - 최소
    길이/비율 조건을 걸어 이번에 고쳤다). 게다가 동물병원은 계약전력 3kW 소상공인
    스케일과 근본적으로 안 맞는 업종이라 ami_db.places_sync.is_implausible_business()
    denylist로도 이제 걸러진다.
  - store_id=16(meter A-L-65, 계약전력 45kW, ksic5=56122 기타 외국식 음식점업):
    "윤스시"는 이름 자체는 정확히 일치하지만(버그 아님), 계약전력 45kW는 이
    데이터셋에서 상위권에 속하는 값이라 이름만으로 봐서는 소규모 개인 스시집보다
    큰 사업장일 가능성이 높다 - 상가 CSV에 면적/직원수 같은 규모 컬럼이 없어
    수치로 검증할 수 없는 한계는 12단계 때 이미 확인됐지만, Google의
    user_ratings_total(리뷰 수)을 "얼마나 알려진/규모있는 사업장인가"의 대용
    신호로 삼아, 조건을 통과하는 후보 여러 개 중 리뷰 수가 가장 많은 곳을 고른다
    (완벽한 대용값은 아니지만 최소한 "아무 후보나 첫 번째로 받아들이기"보다는 낫다).

검증 기준: 15단계와 동일(강서구 지역검증 + 이름유사도(수정판) + 실제 영업시간
존재) + IMPLAUSIBLE_NAME_KEYWORDS denylist + 화곡동 중심 가까운 순 정렬.
store_id=16만 "통과 후보 여러 개 모아서 리뷰 수 최댓값 선택" 방식을 추가로 쓴다.
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
from ami_db.places_sync import is_implausible_business, name_similarity, save_verified_place  # noqa: E402

NAME_SIMILARITY_THRESHOLD = 0.5
CALL_INTERVAL_SEC = 0.2
HWAGOK_CENTROID = (37.53942791977127, 126.84628840419111)

# store_id=16은 계약전력이 높아(45kW) 후보를 하나만 찾고 멈추지 않고, 이 개수만큼
# 통과 후보를 모은 뒤 리뷰 수가 가장 많은 곳을 최종 선택한다.
STORE16_COLLECT_N = 8
STORE16_TRY_LIMIT = 40
STORE9_TRY_LIMIT = 40


def ami_ksic5(code: str) -> str | None:
    if not isinstance(code, str):
        return None
    m = re.match(r"\s*(\d{5})", code)
    return m.group(1) if m else None


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dl = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(a))


def apply_pick(store_id, cand, place_info, raw, place_id):
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


def main() -> None:
    gu_name = "강서구"
    location = f"서울특별시 {gu_name}"
    store_csv = DATA_DIR / "소상공인시장진흥공단_상가(상권)정보_서울_202606.csv"
    client = GooglePlacesClient(settings.google_places_api_key)

    print("[1/2] 상가정보 CSV 로드 및 화곡동 중심 거리 계산")
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
            "SELECT s.store_id, s.meter_id, s.name, m.ksic_code, m.contract_power_kw "
            "FROM stores s JOIN meters m ON m.meter_id = s.meter_id WHERE s.store_id IN (9, 16) "
            "ORDER BY s.store_id"
        )
        targets = cur.fetchall()

    for store_id, meter_id, old_name, meter_ksic_code, contract_power_kw in targets:
        code5 = ami_ksic5(meter_ksic_code)
        pool = store_df[store_df["ksic5"] == code5].copy()
        pool = pool[~pool.apply(
            lambda r: (r["상호명"], r["도로명주소"]) in used_pairs or r["상호명"] in used_names, axis=1
        )]
        pool = pool.sort_values("dist_km").reset_index(drop=True)

        try_limit = STORE16_TRY_LIMIT if store_id == 16 else STORE9_TRY_LIMIT
        print(f"\n[{store_id}] {old_name} (meter={meter_id}, ksic5={code5}, {contract_power_kw}kW) - "
              f"후보 {len(pool)}개(가까운 순) 중 최대 {try_limit}개 시도")

        candidates_passed = []  # (cand, place_info, raw, place_id, review_count)
        for _, cand in pool.head(try_limit).iterrows():
            cand_name = cand["상호명"]
            near = (cand["위도_f"], cand["경도_f"])

            place_info, raw, place_id = client.search_by_name(cand_name, location, near=near)
            time.sleep(CALL_INTERVAL_SEC)
            if place_info is None:
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) - 검색결과없음')
                continue

            address = (raw or {}).get("formatted_address", "")
            if "강서구" not in address:
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" - 지역검증 실패')
                continue

            if is_implausible_business(place_info.name):
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" - 업종 성격상 배제(병원/의원류)')
                continue

            sim = name_similarity(cand_name, place_info.name)
            if sim < NAME_SIMILARITY_THRESHOLD:
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" - 이름유사도 {sim:.2f}')
                continue

            if is_no_info(place_info.hours.get_formatted_hours()):
                print(f'    ✗ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" - 영업시간 정보 없음')
                continue

            reviews = place_info.review_count or 0
            print(f'    ✓ "{cand_name}" ({cand["dist_km"]:.2f}km) -> "{place_info.name}" '
                  f'(유사도 {sim:.2f}, 리뷰 {reviews}건, {address})')
            candidates_passed.append((cand, place_info, raw, place_id, reviews))

            if store_id != 16:
                break  # store_id=9는 15단계와 동일하게 첫 통과 후보로 확정
            if len(candidates_passed) >= STORE16_COLLECT_N:
                break  # store_id=16은 여러 개 모아서 리뷰 수로 고른다

        if not candidates_passed:
            print(f"  [{store_id}] 검증 통과 후보 없음 - 기존 정체성(\"{old_name}\") 유지")
            continue

        if store_id == 16:
            candidates_passed.sort(key=lambda t: t[4], reverse=True)
            print(f"  [{store_id}] 통과 후보 {len(candidates_passed)}개 중 리뷰 수 기준 선택: "
                  + ", ".join(f'{p[1].name}({p[4]}건)' for p in candidates_passed))

        cand, place_info, raw, place_id, reviews = candidates_passed[0]
        apply_pick(store_id, cand, place_info, raw, place_id)
        used_pairs.add((cand["상호명"], cand["도로명주소"]))
        used_names.add(cand["상호명"])
        print(f'  [{store_id}] 교체 완료: "{old_name}" -> "{cand["상호명"]}" '
              f'(Google명 "{place_info.name}", 리뷰 {reviews}건, {cand["dist_km"]:.2f}km)')

    print(
        "\n⚠️  store_operating_hours가 바뀐 매장이 있으므로 "
        "09_compute_operating_status.py를 반드시 다시 실행해서 store_operating_status를 갱신하세요."
    )


if __name__ == "__main__":
    main()
