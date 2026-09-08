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


def _upsert_google_hours(conn, store_id: int, rows: list[HoursRow]) -> None:
    """store_operating_hours에 google_places 소스 7행을 upsert(둘 이상 호출부가 공유하는 부분만 추출)."""
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


def reload_cached_hours(conn, store_id: int, hours_raw: list[str]) -> list[HoursRow] | None:
    """
    이미 google_places_cache에 저장된 hours_raw(원문 배열)를 Google을 다시 호출하지
    않고 hours_parser로 재파싱해 store_operating_hours만 다시 upsert한다.
    hours_parser.py의 파싱 규칙이 바뀌었을 때(예: 오전/오후 마커 생략 처리 수정)
    이미 캐싱된 원문으로 재계산하는 용도 - scripts/17_reparse_operating_hours.py 전용.

    Returns: 재파싱해 반영한 HoursRow 7개, hours_raw가 "정보 없음"뿐이면 None
        (아무것도 건드리지 않는다 - ksic_estimate 폴백이 그대로 유효하기 때문).
    """
    if is_no_info(hours_raw):
        return None
    rows = parse_places_hours_lines(hours_raw)
    _upsert_google_hours(conn, store_id, rows)
    return rows


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
    _upsert_google_hours(conn, store_id, rows)

    _save_places_cache(
        conn, store_id, place_id, place_info.name, lines, place_info.open_now,
        place_info.business_status, place_info.business_status_label, raw or {},
    )

    return rows


# 매칭된 place의 formatted_address가 이 문자열을 포함하지 않으면 무조건 버린다("완전히
# 다른 지역으로 가면 안 됨" 요구사항의 하드 게이트). 화곡동이 속한 자치구 - 21개 매장
# 전부 stores.road_address가 이미 "서울특별시 강서구 ..."로 시작한다(00_rematch_
# store_hwagokdong.py가 소상공인시장진흥공단 CSV를 법정동명='화곡동'으로 필터링해서
# 만든 데이터라 전부 강서구 소속). "화곡동"까지 강제하지 않는 이유: 도로명이 인접
# 행정구역 이름을 따르는 경우가 있어(예: 강서로5나길) 실제로 맞는 매장인데도 주소
# 문자열에 "화곡동"이 안 보일 수 있음 - 자치구 단위가 오탐 없이 안전한 하한선.
REQUIRED_ADDRESS_SUBSTRING = "강서구"


def refresh_store_places_cache(
    client: GooglePlacesClient,
    conn,
    store_id: int,
    store_name: str,
    location: str,
    latitude: float | None = None,
    longitude: float | None = None,
) -> bool:
    """
    캐시 존재 여부를 확인하지 않고 항상 Google을 호출해 google_places_cache에 새 행을
    적재한다(강제 갱신용 - get_or_fetch_store_hours()는 store_operating_hours에 이미
    google_places 행이 있으면 API 자체를 호출하지 않아 "이미 캐시된 매장을 다시
    갱신"하는 용도로 못 씀. scripts/11_refresh_google_places.py 전용).

    get_or_fetch_store_hours()는 운영시간 정보가 없으면("정보 없음") caching 자체를
    건너뛰어 rating/전화번호/웹사이트처럼 운영시간과 무관한 정보까지 함께 버렸다 -
    이 함수는 그 결합을 깨서, place가 검색되기만 하면(운영시간 유무와 무관하게)
    캐시는 항상 저장하고, 운영시간이 실제로 있을 때만 store_operating_hours를
    추가로 upsert한다(store_id의 상세조회 API가 rating/전화번호/웹사이트를
    운영시간 등록 여부와 무관하게 보여줄 수 있어야 하기 때문).

    latitude/longitude(stores 테이블에 이미 있는, 소상공인시장진흥공단 CSV 출처
    좌표)를 주면 GooglePlacesClient.search_by_name()에 그대로 전달해 검색 자체를
    그 좌표 근방으로 편향시킨다 - 이름만으로 검색하면 동명이인 상호나 완전히 다른
    지역(제주도 등)의 결과가 1위로 잡히는 사례가 실측으로 확인됐기 때문(21개 중
    11개가 오매칭이었음). 그렇게 찾은 결과라도 formatted_address에
    REQUIRED_ADDRESS_SUBSTRING이 없으면 마지막 방어선으로 거부한다.

    Returns: 검증까지 통과한 place_info를 캐시에 저장했으면 True. 검색 자체가
        실패했거나 지역 검증에서 걸러졌으면 False(아무것도 저장하지 않음).
    """
    near = (latitude, longitude) if latitude is not None and longitude is not None else None
    place_info, raw, place_id = client.search_by_name(store_name, location, near=near)
    if place_info is None:
        return False

    address = (raw or {}).get("formatted_address", "")
    if REQUIRED_ADDRESS_SUBSTRING not in address:
        print(f'  ⚠️  지역 검증 실패 - "{place_info.name}" 주소="{address}" ("{REQUIRED_ADDRESS_SUBSTRING}" 미포함, 폐기)')
        return False

    save_verified_place(conn, store_id, place_id, place_info, raw or {})
    return True


def save_verified_place(conn, store_id: int, place_id, place_info, raw: dict) -> None:
    """
    이미 검증까지 끝난(지역/이름 등) place_info를 google_places_cache + (운영시간이
    있으면) store_operating_hours에 적재한다. refresh_store_places_cache()의 저장
    부분을 그대로 추출한 것 - scripts/12_rematch_verified_stores.py처럼 후보를 여러 개
    시도하며 이름 유사도까지 직접 검증하는 호출부가, 이미 손에 쥔 place_info/raw를
    또 API 호출 없이 그대로 저장하려고 별도로 노출해뒀다.
    """
    lines = place_info.hours.get_formatted_hours()
    _save_places_cache(
        conn, store_id, place_id, place_info.name, lines, place_info.open_now,
        place_info.business_status, place_info.business_status_label, raw,
    )
    if not is_no_info(lines):
        _upsert_google_hours(conn, store_id, parse_places_hours_lines(lines))


def name_similarity(a: str, b: str) -> float:
    """
    두 상호명 문자열의 유사도(0~1). 공백을 지우고 비교한다(예: "25센치 꼬치앤오뎅바"
    vs "25센치꼬치앤오뎅바" 같은 공백 표기 차이가 실측으로 흔했음). difflib만 쓰는
    이유: 형태소 분석기 없이 표준 라이브러리만으로 "완전히 다른 상호"(예: "마티니" vs
    "피티하모니 강서구청점")와 "표기만 다른 같은 상호"를 어느 정도 구분하기에 충분하고,
    이 프로젝트가 이미 유사한 근사치 규칙(예: KSIC 완전일치)을 쓰는 것과 일관된 수준의
    엄밀함이면 됨 - 완벽한 개체명 매칭기가 필요한 게 아니라 "완전히 동떨어진 결과"만
    걸러내면 충분하기 때문.

    포함관계 shortcut에 최소 길이/비율 조건을 건 이유(실측 버그 수정): "늘봄"(2글자)이
    "강서늘봄동물병원"(8글자) 안에 우연히 부분 문자열로 들어있다는 이유만으로 예전
    코드가 유사도 1.0을 줘서, 완전히 다른 업체(동물병원)를 같은 상호로 오인했다
    (scripts/15 실행 결과 검수에서 발견됨). 이제 짧은쪽 문자열이 3글자 이상이고
    긴쪽의 40% 이상을 차지할 때만 포함관계를 "확실한 매칭"으로 인정하고, 그 외에는
    SequenceMatcher 비율로 넘겨서 우연의 부분 일치가 낮은 점수를 받게 한다.
    """
    from difflib import SequenceMatcher

    norm_a = a.replace(" ", "")
    norm_b = b.replace(" ", "")
    if not norm_a or not norm_b:
        return 0.0
    shorter, longer = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    if shorter in longer and len(shorter) >= 3 and len(shorter) / len(longer) >= 0.4:
        return 1.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()


# Google이 찾아준 이름에 이 키워드가 들어있으면 이름/지역 검증을 통과했어도 무조건
# 거부한다. 병원/의원류는 KSIC가 우연히 맞아도(예: 75919 "기타 사업지원 서비스업"에
# "늘봄" 같은 후보가 있다가 검색에서 완전히 무관한 "강서늘봄동물병원"으로 대체된 사례)
# 전력 요구량이 소상공인 골목상권 데모의 스케일(계기 전부 <50kW)과 근본적으로
# 안 맞는 업종이라 애초에 후보군에서 배제하는 게 맞다 - 이름 유사도 점수와 무관하게
# 업종 성격 자체가 이 데모(음식/소매/개인서비스 등 소규모 상가)와 어긋난다.
IMPLAUSIBLE_NAME_KEYWORDS = ["병원", "의원", "한의원", "약국"]


def is_implausible_business(name: str) -> bool:
    return any(kw in name for kw in IMPLAUSIBLE_NAME_KEYWORDS)
