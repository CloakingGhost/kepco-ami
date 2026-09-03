# -*- coding: utf-8 -*-
"""
운영시간 문자열 파서 두 종류.

1) KSIC 업종코드 기반 추정 문자열("11:00-22:00" 같은 단순 범위) - 요일 구분이
   없으므로 7개 요일 전부에 동일 값을 적용한다.
2) Google Places `/hours` 응답의 `운영시간` 배열(예: "월요일: 10:00 ~ 22:00",
   "화요일: 휴무", "수요일: 24시간 영업") - 요일별로 다른 값을 가질 수 있다.

두 파서 모두 "요일별 (open_time, close_time, is_closed, is_24h)" 형태의
공통 결과(HoursRow)로 정규화해서, store_operating_hours 테이블 적재/조회 쪽
코드가 source가 어느 쪽이든 동일한 방식으로 다룰 수 있게 한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

DAY_NAME_TO_INDEX = {
    "월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3,
    "금요일": 4, "토요일": 5, "일요일": 6,
}

_ALL_CLOSED_HINTS = ("휴무", "정기휴무", "쉬는날")
_ALL_24H_HINTS = ("24시간",)

# 실측 확인 결과, 화곡동 매칭 21개 상가 중 14개(67%)는 Google Places에 실제
# 운영시간이 등록되어 있지 않고 운영시간 배열이 ["정보 없음"] 한 줄로만 온다.
# 이걸 "휴무"로 잘못 해석하면(요일별로 매칭되는 패턴이 없어 기본값 is_closed=True로
# 떨어짐) 운영상태 판정이 "이 상가는 항상 영업시간 외"로 왜곡된다.
# "정보 없음"은 "확인된 휴무"가 아니라 "Google에 데이터가 없음"이므로,
# 이런 응답은 아예 google_places 소스를 만들지 않고 ksic_estimate로 폴백시켜야 한다
# (places_sync.get_or_fetch_store_hours()에서 is_no_info()로 걸러 이 상가는 google_places
# 행 자체를 생성하지 않음).
NO_INFO_SENTINELS = ("정보 없음", "정보없음")


def is_no_info(lines: list[str]) -> bool:
    """운영시간 배열이 비어있거나 '정보 없음'류의 무의미한 응답뿐인지 판정."""
    if not lines:
        return True
    return all(any(s in line for s in NO_INFO_SENTINELS) for line in lines)

# 실제 Google Places 응답으로 확인된 형식: "오전 11:30 ~ 오후 10:00" 처럼
# 12시간제 + 오전/오후 마커가 붙어서 온다(places-api-project 시절 사전검증 호출로
# 실측 확인함 - 애초 가정했던 "10:00 ~ 22:00" 24시간제 표기가 아니었음).
# KSIC 추정 문자열("11:00-22:00")은 마커가 없는 24시간제 그대로 오므로,
# "오전"/"오후"를 옵셔널 그룹으로 둬서 두 형식을 같은 정규식으로 처리한다.
_TIME_RE = re.compile(r"(오전|오후)?\s*(\d{1,2}):(\d{2})")


@dataclass
class HoursRow:
    day_of_week: int
    open_time: time | None
    close_time: time | None
    is_closed: bool
    is_24h: bool
    raw_text: str | None = None


def _to_24h(period: str, hour: str, minute: str) -> tuple[int, int]:
    h, m = int(hour), int(minute)
    if period == "오후" and h != 12:
        h += 12
    elif period == "오전" and h == 12:
        h = 0  # '오전 12:00' = 자정
    return h, m


def _parse_range(text: str) -> tuple[time, time] | None:
    """
    '11:00-22:00'(KSIC 추정, 24시간제) / '오전 11:30 ~ 오후 10:00'(Google Places 실측,
    12시간제+오전오후) 두 형식 모두 -> (open_time, close_time)로 정규화한다.
    """
    matches = _TIME_RE.findall(text)
    if len(matches) < 2:
        return None
    open_h, open_m = _to_24h(*matches[0])
    close_h, close_m = _to_24h(*matches[1])
    # close_time이 24:00(또는 그 이상)으로 오는 경우 time()이 받아들이지 못하므로 23:59로 보정
    # (AMI 원본 시계열의 24:00 보정과 동일한 이유 - 01_prepare_data.py의 fix_2400() 참고).
    close_t = time(23, 59) if close_h >= 24 else time(close_h, close_m)
    open_t = time(open_h % 24, open_m)
    return open_t, close_t


def parse_estimate_hours_range(text: str) -> list[HoursRow]:
    """KSIC 추정 범위 문자열 -> 7개 요일 전부 동일 값으로 채운 HoursRow 리스트."""
    parsed = _parse_range(text)
    if parsed is None:
        # 파싱 실패 시 안전하게 "정보 없음 = 휴무 아님, 시간 미상"으로 처리하지 않고
        # is_closed=True로 방어적으로 처리한다 (없는 시간을 지어내지 않기 위함).
        return [
            HoursRow(day, None, None, is_closed=True, is_24h=False, raw_text=text)
            for day in range(7)
        ]
    open_t, close_t = parsed
    is_24h = open_t == time(0, 0) and close_t == time(23, 59)
    return [
        HoursRow(day, open_t, close_t, is_closed=False, is_24h=is_24h, raw_text=text)
        for day in range(7)
    ]


def parse_places_hours_lines(lines: list[str]) -> list[HoursRow]:
    """
    Google Places `운영시간` 배열(요일별 한 줄씩)을 HoursRow 리스트로 변환한다.

    7개 요일 중 응답에 없는 요일은 is_closed=True로 방어적으로 채운다
    (Google이 특정 요일을 아예 생략하는 경우가 있어, 없는 요일을 "영업"으로
    잘못 추정하지 않기 위함).
    """
    by_day: dict[int, HoursRow] = {}
    for line in lines:
        day_idx = None
        for name, idx in DAY_NAME_TO_INDEX.items():
            if line.startswith(name):
                day_idx = idx
                break
        if day_idx is None:
            continue  # 요일로 시작하지 않는 줄(예외적인 안내 문구)은 건너뜀

        if any(hint in line for hint in _ALL_CLOSED_HINTS):
            by_day[day_idx] = HoursRow(day_idx, None, None, True, False, raw_text=line)
            continue
        if any(hint in line for hint in _ALL_24H_HINTS):
            by_day[day_idx] = HoursRow(day_idx, time(0, 0), time(23, 59), False, True, raw_text=line)
            continue

        parsed = _parse_range(line)
        if parsed is None:
            # 알 수 없는 형식 -> 실측인데 파싱을 못 했다는 뜻이므로 조용히 넘기지 않고
            # 휴무로 방어 처리하되 raw_text는 그대로 남겨 나중에 사람이 확인할 수 있게 한다.
            by_day[day_idx] = HoursRow(day_idx, None, None, True, False, raw_text=line)
            continue
        open_t, close_t = parsed
        by_day[day_idx] = HoursRow(day_idx, open_t, close_t, False, False, raw_text=line)

    return [
        by_day.get(day, HoursRow(day, None, None, True, False, raw_text=None))
        for day in range(7)
    ]
