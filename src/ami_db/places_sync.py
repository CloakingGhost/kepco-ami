# -*- coding: utf-8 -*-
"""
매장 운영시간 캐시-우선(cache-aside) 동기화.

store_operating_hours에 이미 source='google_places' 행이 있으면 Google API를
호출하지 않는다 - 존재 여부만 확인하고 신선도(TTL)는 따지지 않는다(화곡동 21개
고정 데모 스코프라 의도된 단순화. 나중에 "고쳐야 할 버그"로 오해하지 말 것).
없을 때만 실제로 호출하고, 성공하면 store_operating_hours + google_places_cache에
적재한다.

이 패턴은 기존에 없었다 - places-api-project가 살아있던 시절엔 04_fetch...py가
21개 매장 전부를 무조건 HTTP로 재호출했다(DB 확인 없이). 이번에 Google 클라이언트를
인프로세스로 들여오면서 처음 만든 것이다.

호출부(04_sync_google_places_hours.py)는 매장마다 새 DB 연결을 열어 이 함수를
호출한다(매장별 독립 트랜잭션) - 네트워크 호출(Google API) 중간에 한 매장이
실패해도 이미 성공한 매장들의 결과가 롤백되지 않게 하기 위함이다.
"""
import json

from .db import bulk_insert
from .hours_parser import HoursRow, is_no_info, parse_places_hours_lines
from .places_client import GooglePlacesClient


def load_cached_hours(conn, store_id: int) -> list[HoursRow] | None:
    """
    store_operating_hours에 이미 google_places 소스 행이 있으면 그대로 반환(없으면 None).
    get_or_fetch_store_hours() 내부에서도 쓰지만, 호출부(04_sync_google_places_hours.py)가
    "캐시 히트라 API를 호출 안 함"을 로그에 정확히 남기려고 미리 한 번 더 불러도 되게
    public으로 노출해뒀다 - PK 조회라 비용이 무시할 만큼 작다(21개뿐이라 더더욱).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT day_of_week, open_time, close_time, is_closed, is_24h, raw_hours_text
            FROM store_operating_hours
            WHERE store_id = %s AND source = 'google_places'
            ORDER BY day_of_week
            """,
            (store_id,),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    return [
        HoursRow(
            day_of_week=r[0], open_time=r[1], close_time=r[2],
            is_closed=r[3], is_24h=r[4], raw_text=r[5],
        )
        for r in rows
    ]


def _save_places_cache(
    conn, store_id: int, place_id, name, hours_lines: list[str], open_now,
    business_status_code, business_status_label, raw_response: dict,
) -> None:
    """
    google_places_cache에 1행 삽입.

    bulk_insert()(execute_values)를 안 쓰는 이유: 그 함수의 기본 값 템플릿으로는
    raw_response_json 컬럼에 필요한 `%s::jsonb` 캐스트를 넣을 수 없다. 어차피 1행
    삽입이라 배치 최적화 이득도 없으므로, 예전 05_load_google_places_hours.py가
    쓰던(이미 검증된) plain cur.execute() 방식을 그대로 재사용한다.

    place_id/business_status_code/label이 예전엔 늘 NULL이었는데(당시엔 /hours
    엔드포인트만 호출해서 응답에 그 필드들이 아예 없었음), 이제 get_details()를
    인프로세스로 호출하므로 같은 호출 안에서 전부 채워진다.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO google_places_cache
                (store_id, place_id, name, hours_raw, open_now,
                 business_status_code, business_status_label, raw_response_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                store_id, place_id, name, hours_lines, open_now,
                business_status_code, business_status_label,
                json.dumps(raw_response, ensure_ascii=False, default=str),
            ),
        )


def get_or_fetch_store_hours(
    client: GooglePlacesClient, conn, store_id: int, store_name: str, location: str,
) -> list[HoursRow] | None:
    """
    캐시 확인 -> (미스 시) Google 호출 -> 적재, 순서로 처리한다.

    Returns:
        list[HoursRow] (7개) - google_places 소스로 운영시간이 확보됨(캐시 히트 또는
            신규 확보 둘 다 포함, 어느 쪽인지는 호출부가 신경 쓸 필요 없음).
        None - 확보 실패(검색 결과 없음/Google에 운영시간 미등록). 이 경우 아무것도
            쓰지 않는다 - store_operating_hours의 ksic_estimate 소스 행이 그대로
            유효한 폴백이 된다((store_id, source, day_of_week) PK 설계 덕분).
    """
    cached = load_cached_hours(conn, store_id)
    if cached is not None:
        return cached

    place_info, raw, place_id = client.search_by_name(store_name, location)
    if place_info is None:
        return None

    lines = place_info.hours.get_formatted_hours()
    if is_no_info(lines):
        return None

    rows = parse_places_hours_lines(lines)

    hours_cols = [
        "store_id", "source", "day_of_week", "open_time", "close_time",
        "is_closed", "is_24h", "raw_hours_text",
    ]
    hours_values = [
        (store_id, "google_places", r.day_of_week, r.open_time, r.close_time,
         r.is_closed, r.is_24h, r.raw_text)
        for r in rows
    ]
    bulk_insert(
        conn, "store_operating_hours", hours_cols, hours_values,
        on_conflict=(
            "(store_id, source, day_of_week) DO UPDATE SET "
            "open_time=EXCLUDED.open_time, close_time=EXCLUDED.close_time, "
            "is_closed=EXCLUDED.is_closed, is_24h=EXCLUDED.is_24h, "
            "raw_hours_text=EXCLUDED.raw_hours_text, fetched_at=now()"
        ),
    )

    _save_places_cache(
        conn, store_id, place_id, place_info.name, lines, place_info.open_now,
        place_info.business_status, place_info.business_status_label, raw or {},
    )

    return rows
