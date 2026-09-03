# -*- coding: utf-8 -*-
"""
2-4단계: 화곡동 매칭 상가의 운영시간을 Google Places에서 캐시-우선으로 동기화한다.

예전엔 04_fetch_google_places_hours.py(파일 저장만) + 05_load_google_places_hours.py
(파일→DB 적재) 2단계였다. 그 분리의 존재 이유는 "재실행 시 API 재호출 없이 파일만
다시 읽게 하려는 것"이었는데, 이제 ami_db.places_sync.get_or_fetch_store_hours()가
DB(store_operating_hours)에 이미 google_places 행이 있으면 API 자체를 호출하지
않으므로 그 이유가 사라졌다 - 그래서 한 스크립트로 합쳤다(생성 파일 없음, DB가
곧 캐시). 06→07(합성 시계열)의 파일 분리는 이유가 다르다(API 재호출 방지가 아니라
CPU 비용이 드는 재계산 방지) - 그쪽은 그대로 유지.

또한 예전엔 places-api-project 서버(포트 8000)가 떠 있어야만 동작했지만, 이제
Google Places 클라이언트를 인프로세스로 호출하므로 그 서버가 필요 없다.

매장마다 새 DB 연결을 연다(매장별 독립 트랜잭션) - Google API 호출(네트워크)
도중 한 매장이 실패해도 이미 성공한 매장들의 적재 결과가 롤백되지 않게 하기 위함.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.config import settings  # noqa: E402
from ami_db.db import get_raw_connection  # noqa: E402
from ami_db.places_client import GooglePlacesClient  # noqa: E402
from ami_db.places_sync import get_or_fetch_store_hours, load_cached_hours  # noqa: E402

CALL_INTERVAL_SEC = 0.2  # Google Places 쿼터 배려용 호출 간 대기 (캐시 히트 시엔 대기하지 않음)


def main() -> None:
    location = f"서울특별시 강서구 {settings.target_dong}"
    client = GooglePlacesClient(settings.google_places_api_key)

    with get_raw_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT store_id, meter_id, name FROM stores ORDER BY store_id")
        stores = cur.fetchall()

    print(f"{len(stores)}개 매장 운영시간 동기화 시작 (캐시 미스 건만 실제 호출)")

    cache_hit = ok = no_info = 0

    for store_id, meter_id, name in stores:
        with get_raw_connection() as conn:
            already_cached = load_cached_hours(conn, store_id) is not None
            rows = get_or_fetch_store_hours(client, conn, store_id, name, location)

        if already_cached:
            cache_hit += 1
            print(f"  [캐시] {name} ({meter_id})")
            continue

        if rows is None:
            # get_or_fetch_store_hours가 검색 실패/정보 없음을 구분하지 않고 둘 다 None을
            # 반환하므로 여기서도 하나로 묶어 로그를 남긴다 - 어느 쪽이든 조치는 동일
            # (ksic_estimate 유지, 재시도는 다음 실행 때 자동으로 다시 시도됨).
            no_info += 1
            print(f"  [정보없음/실패] {name} ({meter_id}) - ksic_estimate 유지됨")
        else:
            ok += 1
            print(f"  [OK] {name} ({meter_id})")

        time.sleep(CALL_INTERVAL_SEC)

    print(
        f"\n캐시 히트: {cache_hit}건 / 신규 확보: {ok}건 / "
        f"정보없음·실패(ksic_estimate 유지): {no_info}건"
    )


if __name__ == "__main__":
    main()
