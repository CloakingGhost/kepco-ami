# -*- coding: utf-8 -*-
"""
hours_parser.py의 파싱 로직이 바뀔 때마다 Google Places를 다시 호출할 필요는 없다 -
google_places_cache.hours_raw에 원문이 이미 있으므로, 그걸 새 파서로 다시 돌려
store_operating_hours만 재적재한다.

이 스크립트가 만들어진 계기: Google 원문이 같은 오전/오후 구간 안에서 종료 시각의
마커를 생략하는 경우("오후 2:00~9:00")를 예전 파서가 마커 없는 24시간제로 오인해
저녁 영업(14:00~21:00)을 심야 철야 영업(14:00~09:00)으로 잘못 뒤집었다
(hours_parser._to_24h). 정규식을 고친 뒤(_fill_omitted_markers 추가) 이미 잘못
적재된 매장들의 store_operating_hours를 이 스크립트로 다시 계산한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.db import get_raw_connection  # noqa: E402
from ami_db.places_sync import reload_cached_hours  # noqa: E402


def main() -> None:
    with get_raw_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (store_id) store_id, hours_raw
            FROM google_places_cache
            ORDER BY store_id, fetched_at DESC
            """
        )
        cached = cur.fetchall()

    print(f"{len(cached)}개 매장의 캐시된 운영시간 원문을 새 파서로 재적재")
    reloaded = skipped = 0
    for store_id, hours_raw in cached:
        with get_raw_connection() as conn:
            rows = reload_cached_hours(conn, store_id, hours_raw or [])
        if rows is None:
            skipped += 1
            print(f"  [건너뜀] store_id={store_id} (정보 없음)")
        else:
            reloaded += 1
            print(f"  [OK] store_id={store_id}")

    print(f"\n재적재: {reloaded}건 / 건너뜀: {skipped}건")


if __name__ == "__main__":
    main()
