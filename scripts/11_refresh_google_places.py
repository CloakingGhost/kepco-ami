# -*- coding: utf-8 -*-
"""
11단계: 21개 매장 전체의 Google Places 정보(평점/전화번호/웹사이트/영업상태/운영시간)를
무조건 다시 조회해 google_places_cache에 새 행으로 적재한다.

04_sync_google_places_hours.py와의 차이: 그쪽은 store_operating_hours에 이미
google_places 행이 있으면 API를 호출하지 않는 "캐시 미스만 채우기" 스크립트다.
이 스크립트는 store_id 상세조회 API(POST /api/stores)가 쓸 rating/
formatted_phone_number/website 등을 채우려고 만든 것으로, 이미 캐시가 있어도
무조건 다시 호출해 최신 값으로 갱신한다(ami_db.places_sync.refresh_store_places_cache).

좌표 편향 + 지역 검증: 이름만으로 Google Text Search를 하면 동명이인 상호나 완전히
다른 지역(제주도 등)의 결과가 1위로 잡히는 사례가 실측으로 확인됐다(21개 중 11개
오매칭). stores.latitude/longitude(소상공인시장진흥공단_상가정보_서울_202606.csv,
법정동명='화곡동' 필터 출처 - 00_rematch_store_hwagokdong.py 참고)를 검색 중심좌표로
넘겨 반경 내 결과를 우선시키고, 그렇게 찾은 결과라도 formatted_address에 "강서구"가
없으면 폐기한다(ami_db.places_sync.REQUIRED_ADDRESS_SUBSTRING).

매 실행마다 깨끗한 상태에서 다시 채운다: 기존 google_places_cache 전체와
store_operating_hours의 source='google_places' 행을 먼저 지운다. 그래야 "이번엔
지역 검증에서 걸러진" 매장이 예전(검증 로직 도입 전) 오매칭 행을 "최신"으로 계속
들고 있는 상태가 안 남는다 - 지워진 매장은 store_operating_hours의 ksic_estimate
소스로 자연히 폴백된다((store_id, source, day_of_week) PK 설계 덕분에 항상 존재).

매장마다 새 DB 연결을 연다(매장별 독립 트랜잭션) - Google API 호출(네트워크)
도중 한 매장이 실패해도 이미 성공한 매장들의 적재 결과가 롤백되지 않게 하기 위함
(04_sync_google_places_hours.py와 동일한 이유).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.config import settings  # noqa: E402
from ami_db.db import get_raw_connection  # noqa: E402
from ami_db.places_client import GooglePlacesClient  # noqa: E402
from ami_db.places_sync import refresh_store_places_cache  # noqa: E402

CALL_INTERVAL_SEC = 0.2  # Google Places 쿼터 배려용 호출 간 대기


def main() -> None:
    location = f"서울특별시 강서구 {settings.target_dong}"
    client = GooglePlacesClient(settings.google_places_api_key)

    with get_raw_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT store_id, meter_id, name, latitude, longitude FROM stores ORDER BY store_id")
        stores = cur.fetchall()
        print("기존 google_places_cache / store_operating_hours(google_places) 초기화")
        cur.execute("DELETE FROM google_places_cache")
        cur.execute("DELETE FROM store_operating_hours WHERE source = 'google_places'")

    print(f"{len(stores)}개 매장 Google Places 정보 강제 갱신 시작 (좌표 편향 + 강서구 검증)")

    ok = fail = 0
    for store_id, meter_id, name, latitude, longitude in stores:
        with get_raw_connection() as conn:
            found = refresh_store_places_cache(
                client, conn, store_id, name, location, latitude=latitude, longitude=longitude,
            )

        if found:
            ok += 1
            print(f"  [OK] {name} ({meter_id})")
        else:
            fail += 1
            print(f"  [검색결과없음/지역검증실패] {name} ({meter_id})")

        time.sleep(CALL_INTERVAL_SEC)

    print(f"\n갱신 완료: {ok}건 / 검색결과없음·지역검증실패: {fail}건")


if __name__ == "__main__":
    main()
