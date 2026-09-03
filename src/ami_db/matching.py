# -*- coding: utf-8 -*-
"""
화곡동 단일 지역으로 좁힌 AMI 계기 <-> 상가정보 재매칭.

이 로직은 visualize_analyis_data/scripts/02_match_store.py의 매칭 규칙을
"그대로" 재현한 것이다(스케일 배제 + KSIC 5자리 완전일치 + seed=42 비복원추출).
원본 스크립트는 서울 전역을 대상으로 매칭해 store_ami_matched.csv(31건)를
만들었는데, 이번 작업은 "동네 골목상권" 컨셉과 일관되도록 상가 후보 풀을
화곡동(법정동명 기준) 하나로 좁혀 같은 규칙을 재실행하는 것뿐이다.
원본 파일(02_match_store.py)은 절대 수정하지 않는다 - 그 파일의 함수를 그대로
가져와 쓸 수도 있었지만, 이 서브프로젝트(db/)가 하나의 독립된 uv 환경으로
재현 가능해야 한다는 목표 때문에 로직만 복제하고 출처를 이렇게 주석으로 남긴다.

매칭 키: AMI `산업분류`(순수 5자리 숫자, 예 56199)
         vs 상가정보 `표준산업분류코드`(알파벳 대분류 접두 + 5자리, 예 I56199)
         -> 접두 알파벳을 제거한 5자리 숫자로 비교 (코드 레벨 일치만 채택, 텍스트 유사도 매칭 안 함)
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

# 상가정보 CSV에서 화곡동 재매칭에 필요한 컬럼만 로드한다.
# 원본 02_match_store.py의 10개 usecols에 법정동명(지역 필터용) + 건물명/층정보
# (stores 테이블 컬럼 충족용)를 추가했다.
STORE_USECOLS = [
    "상호명", "지점명", "표준산업분류코드", "표준산업분류명",
    "상권업종대분류명", "상권업종중분류명",
    "도로명주소", "건물명", "층정보", "경도", "위도",
    "시군구명", "법정동명",
]


def ksic5(code: str) -> str | None:
    """상가정보 표준산업분류코드(예: I56199) -> 순수 5자리 숫자(56199)."""
    if not isinstance(code, str):
        return None
    m = re.search(r"(\d{5})", code)
    return m.group(1) if m else None


def ami_ksic5(code: str) -> str | None:
    """AMI 산업분류(예: '56199 간이음식 포장 판매 전문점') -> 5자리 숫자."""
    if not isinstance(code, str):
        return None
    m = re.match(r"\s*(\d{5})", code)
    return m.group(1) if m else None


# 02_match_store.py의 HOURS_LOOKUP/estimate_hours()를 그대로 복제.
# Google Places 조회가 실패한(404) 상가나, 애초에 04/05단계 실행 전 임시로 쓸
# "업종코드 기반 추정 운영시간"이다 - 실측이 아님을 store_operating_hours.source='ksic_estimate'로 구분한다.
HOURS_LOOKUP = [
    (re.compile(r"^56"), "11:00-22:00"),      # 음식점/주점
    (re.compile(r"^55"), "00:00-24:00"),      # 숙박업
    (re.compile(r"^47"), "10:00-21:00"),      # 소매
    (re.compile(r"^96"), "10:00-20:00"),      # 미용 등 개인서비스
    (re.compile(r"^85"), "09:00-18:00"),      # 교육
]
DEFAULT_HOURS = "09:00-18:00"


def estimate_hours(code5: str | None) -> str:
    if code5:
        for pat, hours in HOURS_LOOKUP:
            if pat.match(code5):
                return hours
    return DEFAULT_HOURS


def load_eligible_meters(step1_classified_csv: Path, line_name: str) -> pd.DataFrame:
    """
    02_match_store.py가 이미 만들어 둔 step1_meta_classified.csv(전력분류/카테고리분류/
    매장매칭_적격 파생 완료)를 읽어, 이번 파일럿 선로 + 매칭 적격 조건으로 필터한다.

    line_name='A'로 필터하면 C선로 계기(예: 태양광이라 시계열 0행인 C-L-12)는
    이 시점에서 자연히 빠지므로 별도 예외처리가 필요 없다.
    """
    df = pd.read_csv(step1_classified_csv, encoding="utf-8-sig")
    df = df[(df["선로명"] == line_name) & (df["매장매칭_적격"] == True)].copy()  # noqa: E712
    df["ami_ksic5"] = df["산업분류"].apply(ami_ksic5)
    return df


def load_dong_stores(store_csv: Path, dong_name: str) -> pd.DataFrame:
    """
    상가정보 CSV(554,092행)를 법정동명 기준으로 필터링한다.

    법정동명을 쓰는 이유: 행정동명은 "화곡동"이라는 값 자체가 없고
    화곡1동~화곡8동(5,7동 결번)으로 쪼개져 있어 7개 값 OR 조건이 필요해진다.
    법정동명은 '화곡동' 단일값으로 7,934행이 깔끔하게 잡힌다(사전 검증 완료).
    """
    store = pd.read_csv(
        store_csv, usecols=STORE_USECOLS, encoding="utf-8-sig", dtype=str,
    )
    store = store[store["법정동명"] == dong_name].copy()
    store["ksic5"] = store["표준산업분류코드"].apply(ksic5)
    # 경도/위도는 원본이 문자열이라 DB 컬럼(DOUBLE PRECISION)에 맞춰 여기서 캐스팅한다.
    store["경도_f"] = pd.to_numeric(store["경도"], errors="coerce")
    store["위도_f"] = pd.to_numeric(store["위도"], errors="coerce")
    return store.reset_index(drop=True)


def match_meters_to_stores(
    meters_df: pd.DataFrame, stores_df: pd.DataFrame, seed: int, dong_name: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    KSIC 5자리 완전일치 + seed 고정 비복원추출(02_match_store.py와 동일 알고리즘).

    반환: (matched_df, excluded_df)
    matched_df 컬럼은 db/sql/schema.sql의 stores 테이블 컬럼과 1:1로 맞춰 뒀다
    (로더 스크립트가 별도 리네이밍 없이 그대로 insert할 수 있게).
    """
    rng = np.random.default_rng(seed)
    used_idx: set[int] = set()
    matched_rows: list[dict] = []
    excluded_rows: list[dict] = []

    for _, r in meters_df.iterrows():
        code5 = r["ami_ksic5"]
        cand = stores_df.index[stores_df["ksic5"] == code5] if code5 else pd.Index([])
        cand = cand.difference(list(used_idx))

        if len(cand) == 0:
            excluded_rows.append({
                "meter_id": r["meter_id"],
                "산업분류": r["산업분류"],
                "제외사유": f"KSIC({code5}) 일치 상가가 {dong_name}에 없거나 "
                          f"같은 코드를 공유하는 다른 계기가 후보를 먼저 소진함",
            })
            continue

        pick = cand[rng.integers(0, len(cand))]
        used_idx.add(pick)
        srow = stores_df.loc[pick]

        matched_rows.append({
            "meter_id": r["meter_id"],
            "name": srow["상호명"],
            "branch_name": srow["지점명"] or None,
            "road_address": srow["도로명주소"],
            "building_name": srow["건물명"] or None,
            "floor_info": srow["층정보"] or None,
            "longitude": srow["경도_f"],
            "latitude": srow["위도_f"],
            "biz_category_large": srow["상권업종대분류명"],
            "biz_category_mid": srow["상권업종중분류명"],
            "ksic_code": srow["표준산업분류코드"],
            "dong_name": dong_name,
            "estimated_hours_range": estimate_hours(code5),  # 예: '11:00-22:00' - hours_parser가 요일별 행으로 펼침
        })

    return pd.DataFrame(matched_rows), pd.DataFrame(excluded_rows)
