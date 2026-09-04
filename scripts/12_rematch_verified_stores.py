# -*- coding: utf-8 -*-
"""
12단계: Google Places에서 실명 확인이 안 되는 매장의 정체성을 완전히 교체한다.

배경: 11단계(scripts/11_refresh_google_places.py)가 좌표 편향 + 강서구 지역검증까지
넣었는데도, 7개 매장(store_id 6,7,9,12,14,15,18)은 원래 매칭된 상호가 Google에 그
이름으로 등록돼 있지 않아 검색이 "강서구 안의 다른 실존 업체"로 대체되는 문제가
남았다(예: '마티니'→'피티하모니 강서구청점'). 이대로 두면 상세조회 API(POST
/api/stores)가 엉뚱한 업체의 평점/전화번호/웹사이트를 정답인 것처럼 보여준다.

1차 실행(TARGET_STORE_IDS=7개 전체) 결과 검수에서 또 다른 문제가 드러났다: 이름/지역만
맞는 첫 후보를 그냥 채택해버려서, 7개 중 5개(코지/늘봄/청진동해장촌/우리집밥/
와이제이푸드)는 실제로는 Google에 영업시간 정보가 없는 업체였다 - "영업시간이 안
나온다"는 지적을 받고서야 REQUIRE_REAL_HOURS 검증을 추가했다. 화곡동 후보가 KSIC
코드당 수백~수천 개나 있는데 정보 없는 후보로 타협할 이유가 없다는 게 요지 - 이제
TARGET_STORE_IDS는 그 5개만 다시 돌리는 상태로 맞춰져 있다(2개는 이미 영업시간
확보돼 재실행 대상에서 뺐다).

해결: 00_rematch_store_hwagokdong.py/matching.py가 원래 쓰던 "5자리 KSIC 완전일치"
후보 풀(동일 화곡동, 동일 세부 업종)에서, 이미 21개 매장에 배정된 후보를 제외한
나머지 중 Google이 실제로 그 이름의 업체를 찾아주는 후보가 나올 때까지 순서대로
시도한다. CSV에는 "매장 규모"를 나타내는 수치 컬럼이 아예 없어서(면적/직원수/매출
없음) 원래 알고리즘이 쓰던 "동일 KSIC 5자리"(같은 세부 업종 = 실무적으로 규모대가
비슷한 유일한 대용 신호) 제약은 그대로 유지하고, 거기에 Google 실명 검증만 추가한다.

검증 기준: (1) formatted_address에 "강서구" 포함(지역 검증, 11단계와 동일)
(2) 후보 상호명과 Google이 찾은 이름의 유사도(ami_db.places_sync.name_similarity)가
임계치 이상 - 완전히 동떨어진 업체(예: PT장, 다른 상호의 식당)를 걸러내기 위함.

성공하면: stores 테이블의 정체성 컬럼(name/branch_name/road_address/building_name/
floor_info/longitude/latitude/biz_category_large/biz_category_mid/ksic_code)을
새 후보로 교체하고, google_places_cache/store_operating_hours(google_places)에
Google 응답을 바로 적재한다(검증 단계에서 이미 받은 응답을 재사용 - API 재호출 안 함).
ksic_code(5자리)는 그대로라 store_operating_hours의 ksic_estimate 추정시간
(estimate_hours()가 code5 prefix만 보고 정하므로)은 안 바뀐다 - 건드리지 않는다.

이 스크립트를 실행한 뒤에는 반드시 09_compute_operating_status.py를 다시 돌려야
한다 - store_operating_hours가 바뀐 매장의 schedule_status/final_status/
congestion_level이 store_operating_status에 아직 예전 값 그대로 남아있기 때문
(그 파일은 이 스크립트가 자동으로 호출하지 않는다 - 21개 매장 전체를 매번 재계산하는
무거운 배치라 명시적으로 별도 실행하게 분리해뒀다).
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

# 1차 실행(전체 7개) 결과 검수에서 이름/지역만 맞고 실제 영업시간 정보가 없는 업체를
# 첫 매치로 그냥 채택해버린 게 드러났다(코지/늘봄/청진동해장촌/우리집밥/와이제이푸드 5개
# - is_no_info()로 걸러졌어야 하는데 이 스크립트가 애초에 그 기준 자체가 없었음).
# 화곡동 후보가 KSIC코드당 수백~수천 개인데 굳이 정보 없는 후보로 타협할 이유가 없어서,
# 이번엔 "영업시간까지 실제로 등록된 후보"만 통과시키고 나머지 2개(다이소목동/놀부부대찌개,
# 이미 영업시간 확보됨)는 다시 건드리지 않는다.
TARGET_STORE_IDS = [7, 9, 12, 15, 18]
NAME_SIMILARITY_THRESHOLD = 0.5
REQUIRE_REAL_HOURS = True  # True면 Google에 영업시간 자체가 없는 후보("정보 없음")는 탈락
MAX_CANDIDATES_PER_STORE = 60  # 검증 기준이 하나 늘어난 만큼(영업시간까지) 상한도 올림
CALL_INTERVAL_SEC = 0.2
RNG_SEED = 43  # 42로 이미 시도했던 후보(=이미 stores에 배정됨)는 used_pairs로 자동 제외되지만,
               # 시드까지 바꿔 순서 자체를 다르게 섞어 앞쪽에서 겹칠 확률을 줄인다.


def ami_ksic5(code: str) -> str | None:
    """meters.ksic_code(예: '56111 한식 일반 음식점업') -> 5자리 숫자."""
    if not isinstance(code, str):
        return None
    m = re.match(r"\s*(\d{5})", code)
    return m.group(1) if m else None


def main() -> None:
    dong_name = settings.target_dong
    location = f"서울특별시 강서구 {dong_name}"
    store_csv = DATA_DIR / "소상공인시장진흥공단_상가(상권)정보_서울_202606.csv"
    client = GooglePlacesClient(settings.google_places_api_key)

    print(f"[1/3] 상가정보 CSV 로드 및 법정동명='{dong_name}' 필터")
    store_df = pd.read_csv(store_csv, usecols=STORE_USECOLS, encoding="utf-8-sig", dtype=str)
    store_df = store_df[store_df["법정동명"] == dong_name].copy()
    store_df["ksic5"] = store_df["표준산업분류코드"].apply(ksic5)
    store_df["경도_f"] = pd.to_numeric(store_df["경도"], errors="coerce")
    store_df["위도_f"] = pd.to_numeric(store_df["위도"], errors="coerce")

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

        rng = np.random.default_rng(RNG_SEED + store_id)  # 매장별로 다른 순서, 재실행 시 재현 가능
        order = rng.permutation(len(pool))

        print(f"\n[{store_id}] {old_name} (meter={meter_id}, ksic5={code5}) - "
              f"후보 {len(pool)}개 중 최대 {MAX_CANDIDATES_PER_STORE}개 시도")

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

            if REQUIRE_REAL_HOURS and is_no_info(place_info.hours.get_formatted_hours()):
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
