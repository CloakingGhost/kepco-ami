# -*- coding: utf-8 -*-
"""
Google Places API 클라이언트 (places-api-project/src/places_api/client.py에서 포팅).

places-api-project는 이제 서버로 띄우지 않는다(아카이브) - 프론트가 실제로 필요로
하는 데이터(좌표/운영상태/DB)가 전부 이 서브프로젝트에 있어서, "운영시간을 얻으려고
별도 HTTP 서버를 하나 더 띄워야 하는" 구조가 불필요한 운영 부담이었기 때문이다.
이 파일은 원본의 search_by_name()/get_details()만 가져왔다 - 지도 실시간 검색용
search_nearby()/search_by_name_multi()는 우리 화곡동 21개 고정 상가 DB와 무관해서
포팅하지 않는다(필요해지면 원본 places-api-project/src/places_api/client.py 참고).

원본과의 차이(포팅 시 의도적으로 바꾼 부분):
- get_details()/search_by_name()이 PlaceInfo 하나만 반환하던 것을, 튜플로 바꿔서
  Google 원본 응답 dict도 같이 반환한다. google_places_cache.raw_response_json
  (NOT NULL, "HTTP 응답 원문 전체" 보관용)을 채우려고 추가 API 호출을 하지 않기
  위함 - 이미 받은 응답을 재사용한다.
"""
from typing import Optional

from googlemaps import Client
from dataclasses import dataclass
from enum import Enum


class BusinessStatus(Enum):
    """운영 상태 열거형 (Google Places business_status 코드 매핑)."""

    OPERATIONAL = ("OPERATIONAL", "영업중")
    CLOSED_TEMPORARILY = ("CLOSED_TEMPORARILY", "임시휴업")
    CLOSED_PERMANENTLY = ("CLOSED_PERMANENTLY", "폐업")
    UNKNOWN = ("UNKNOWN", "알 수 없음")

    @classmethod
    def from_code(cls, code: str) -> "BusinessStatus":
        for status in cls:
            if status.value[0] == code:
                return status
        return cls.UNKNOWN

    @property
    def label(self) -> str:
        return self.value[1]


@dataclass
class BusinessHours:
    """운영시간 정보. weekday_text(요일별 한글 문장)가 있으면 그걸 우선 쓴다."""

    weekday_text: Optional[list[str]] = None
    open_now: Optional[bool] = None
    periods: Optional[list[dict]] = None

    def _format_periods(self) -> list[str]:
        """weekday_text가 없을 때 periods(요일/시각 구조화 데이터)를 텍스트로 변환."""
        if not self.periods:
            return ["정보 없음"]

        days = ["일", "월", "화", "수", "목", "금", "토"]
        hours_dict: dict[str, list[str]] = {}

        for period in self.periods:
            day_name = days[period["open"]["day"]]
            open_time = self._format_time(period["open"].get("time", ""))
            close_time = (
                self._format_time(period.get("close", {}).get("time", ""))
                if period.get("close")
                else "자정"
            )
            hours_dict.setdefault(day_name, []).append(f"{open_time} ~ {close_time}")

        result = [f"{day}요일: {', '.join(hours_dict[day])}" for day in days if day in hours_dict]
        return result if result else ["정보 없음"]

    @staticmethod
    def _format_time(time_str: str) -> str:
        """시간 포맷팅 (0900 -> 09:00)."""
        if not time_str or len(time_str) < 4:
            return time_str
        return f"{time_str[:2]}:{time_str[2:]}"

    def get_formatted_hours(self) -> list[str]:
        if self.weekday_text:
            return self.weekday_text
        return self._format_periods()


@dataclass
class PlaceInfo:
    """매장 상세정보 (Google Places Details 응답을 파싱한 결과)."""

    name: str
    address: str
    phone: str
    website: str
    business_status: str
    business_status_label: str
    hours: BusinessHours
    rating: Optional[float] = None
    review_count: Optional[int] = None
    open_now: Optional[bool] = None


class GooglePlacesClient:
    """Google Places API 클라이언트 (운영시간 조회 전용 서브셋)."""

    def __init__(self, api_key: str):
        """
        api_key는 호출부(ami_db.places_sync)가 settings.google_places_api_key를
        넘겨준다 - config.py의 Settings가 이 값을 필수로 강제하지 않는 이유는
        Google Places와 무관한 스크립트/엔드포인트까지 .env에 이 키가 없으면
        죽어버리는 걸 막기 위함이고, 실제 검증은 여기(사용 시점)에서 한다.
        """
        if not api_key:
            raise ValueError(
                "❌ GOOGLE_PLACES_API_KEY가 설정되지 않았습니다. db/.env 파일을 확인하세요."
            )
        self.api_key = api_key
        self.client = Client(key=self.api_key, timeout=10)

    def get_details(self, place_id: str) -> tuple[Optional[PlaceInfo], Optional[dict]]:
        """
        Place ID로 상세정보 조회.

        Returns:
            (PlaceInfo, raw dict) 튜플. 실패 시 (None, None).
            raw dict는 Google 응답의 result 부분 원문 - google_places_cache.raw_response_json에
            그대로 저장하기 위함(재파싱/디버깅용, 우리가 파싱한 필드 외의 정보도 보존).
        """
        try:
            place_details = self.client.place(
                place_id=place_id,
                language="ko",
                fields=[
                    "name",
                    "formatted_address",
                    "business_status",
                    "opening_hours",
                    "formatted_phone_number",
                    "website",
                    "rating",
                    "user_ratings_total",
                ],
            )

            result = place_details.get("result")
            if not result:
                print("❌ 상세정보 조회 실패")
                return None, None

            opening_hours_data = result.get("opening_hours", {})

            place_info = PlaceInfo(
                name=result.get("name", ""),
                address=result.get("formatted_address", ""),
                phone=result.get("formatted_phone_number", "정보 없음"),
                website=result.get("website", "정보 없음"),
                business_status=result.get("business_status", "UNKNOWN"),
                business_status_label=BusinessStatus.from_code(
                    result.get("business_status", "UNKNOWN")
                ).label,
                hours=BusinessHours(
                    weekday_text=opening_hours_data.get("weekday_text"),
                    open_now=opening_hours_data.get("open_now"),
                    periods=opening_hours_data.get("periods"),
                ),
                rating=result.get("rating"),
                review_count=result.get("user_ratings_total"),
                open_now=opening_hours_data.get("open_now"),
            )

            return place_info, result

        except Exception as e:
            print(f"❌ 상세정보 조회 오류: {str(e)}")
            return None, None

    def search_by_name(
        self,
        place_name: str,
        location: str = "대한민국",
        *,
        near: tuple[float, float] | None = None,
        radius_m: int = 500,
    ) -> tuple[Optional[PlaceInfo], Optional[dict], Optional[str]]:
        """
        매장 이름으로 검색 (텍스트 검색 1건 + 상세조회 1건, 총 2회 API 호출).

        near=(latitude, longitude)를 주면 Text Search에 location/radius 편향을 걸어
        그 좌표 반경 radius_m 이내의 결과를 우선하게 만든다 - 이름만으로 검색하면
        동명이인 상호나 전혀 다른 지역(예: 제주도)의 결과가 1위로 잡히는 사례가
        실측으로 확인돼서(우리 상가 DB의 21개 매장 중 다수가 이미 알고 있는 정확한
        좌표(stores.latitude/longitude, 소상공인시장진흥공단 CSV 출처)를 갖고 있으므로
        그 좌표로 검색을 지리적으로 좁히는 것. near가 없으면 기존과 동일하게 동작한다.

        Returns:
            (PlaceInfo, raw dict, place_id) 튜플. 실패 시 (None, None, None).
            place_id는 google_places_cache.place_id에 저장하기 위함 - get_details()가
            내부적으로 이미 알고 있는 값이라 추가 조회 없이 그대로 전달한다.
        """
        print(f'🔍 검색 중: "{place_name}" in "{location}"\n')

        try:
            search_kwargs: dict[str, object] = {"query": f"{place_name} {location}", "language": "ko"}
            if near is not None:
                search_kwargs["location"] = near
                search_kwargs["radius"] = radius_m
            search_results = self.client.places(**search_kwargs)

            if not search_results.get("results"):
                print(f'❌ "{place_name}" 검색 결과가 없습니다.')
                return None, None, None

            place = search_results["results"][0]
            place_id = place["place_id"]

            print(f'✅ "{place["name"]}" 발견됨\n')

            place_info, raw = self.get_details(place_id)
            return place_info, raw, place_id

        except Exception as e:
            print(f"❌ 검색 오류: {str(e)}")
            return None, None, None
