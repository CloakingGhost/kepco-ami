# -*- coding: utf-8 -*-
"""
serving_api.py 응답 스키마.

Swagger(`/docs`)에서 "이 엔드포인트가 뭘 돌려주는지"를 미리 보여주기 위한
용도로 별도 파일로 분리했다(엔드포인트 함수 정의와 응답 모양 정의를 섞으면
serving_api.py가 너무 길어짐). places-api-project/src/places_api/models.py와
같은 이유로 분리한 것.
"""
from __future__ import annotations

from datetime import datetime, time

from pydantic import BaseModel, Field


class StoreSchema(BaseModel):
    store_id: int = Field(examples=[1])
    meter_id: str = Field(examples=["A-L-11"])
    name: str = Field(examples=["못난이찹쌀꽈배기"])
    branch_name: str | None = Field(default=None, examples=[None])
    road_address: str = Field(examples=["서울특별시 강서구 강서로12길 5"])
    building_name: str | None = None
    floor_info: str | None = Field(default=None, examples=["1"])
    longitude: float | None = Field(default=None, examples=[126.847366517265])
    latitude: float | None = Field(default=None, examples=[37.5315885537006])
    biz_category_large: str | None = Field(default=None, examples=["음식"])
    biz_category_mid: str | None = Field(default=None, examples=["기타 간이"])
    dong_name: str = Field(examples=["화곡동"])
    match_note: str = Field(
        examples=["업종코드 기반 통계적 근사 매칭이며 실제 매장 확인 매칭이 아니고, 지역(동)도 데모 일관성을 위해 임의 지정된 것으로 AMI 데이터 자체의 실제 위치가 아님"]
    )
    data_resolution: str = Field(
        default="15min", examples=["15min"],
        description="'15min'(정상) | '1hour' - 이 계기가 recv_kWh를 매시 정각에만 리포트하는 계기면 '1hour'. "
                    "결측이 아니라 계기 자체의 리포트 주기 특성 - 숨기지 않고 그대로 노출한다.",
    )


class StoreListResponse(BaseModel):
    stores: list[StoreSchema]


class CurrentStatusSchema(BaseModel):
    store_id: int = Field(examples=[1])
    name: str = Field(examples=["못난이찹쌀꽈배기"])
    longitude: float | None = None
    latitude: float | None = None
    ts: datetime = Field(description="이 상태를 계산한 15분 슬롯 시각(현재 시각 이하 중 가장 최근)")
    schedule_status: str = Field(examples=["open_hours"], description="'open_hours' | 'closed_hours' - 운영시간표 기준 판정")
    power_status: str = Field(examples=["active"], description="'active' | 'low' - 야간 baseline 대비 실측 전력 기준 판정")
    final_status: str = Field(examples=["영업중"], description="'영업중' | '휴무추정' | '예외영업' | '영업종료'")
    congestion_level: str | None = Field(default=None, examples=["중"], description="'상' | '중' | '하' | null(영업중이 아니면 null)")


class CurrentStatusOneSchema(BaseModel):
    store_id: int = Field(examples=[1])
    name: str = Field(examples=["못난이찹쌀꽈배기"])
    ts: datetime = Field(description="이 상태를 계산한 15분 슬롯 시각(현재 시각 이하 중 가장 최근)")
    schedule_status: str = Field(examples=["open_hours"], description="'open_hours' | 'closed_hours' - 운영시간표 기준 판정")
    power_status: str = Field(examples=["active"], description="'active' | 'low' - 야간 baseline 대비 실측 전력 기준 판정")
    final_status: str = Field(examples=["영업중"], description="'영업중' | '휴무추정' | '예외영업' | '영업종료'")
    congestion_level: str | None = Field(default=None, examples=["상"], description="'상' | '중' | '하' | null(영업중이 아니면 null)")


class CurrentStatusListResponse(BaseModel):
    stores: list[CurrentStatusSchema]


class StoreHoursRow(BaseModel):
    day_of_week: int = Field(examples=[0], description="0=월요일 ... 6=일요일")
    open_time: time | None = Field(default=None, examples=["10:00:00"], description="휴무일이면 null")
    close_time: time | None = Field(default=None, examples=["22:00:00"], description="휴무일이면 null")
    is_closed: bool = Field(examples=[False])
    is_24h: bool = Field(examples=[False])
    source: str = Field(examples=["google_places"], description="'google_places'(실측) | 'ksic_estimate'(업종코드 기반 추정) - 실측이 있으면 항상 실측 우선")


class StoreHoursResponse(BaseModel):
    store_id: int = Field(examples=[1])
    hours: list[StoreHoursRow] = Field(description="요일별 0~7행. day_of_week 기준 최대 7행(요일당 1행, google_places 우선)")


class DayStatusRow(BaseModel):
    ts: datetime
    schedule_status: str
    power_status: str
    final_status: str
    congestion_level: str | None = None
    received_active_power_kwh: float | None = Field(default=None, description="15분 유효전력(kWh). 결측이면 null")
    is_synthetic: bool = Field(description="false=실측(2026-04-01~06-30), true=합성(2026-07-01~오늘)")
    is_redistributed: bool = Field(
        description="true면 이 슬롯의 전력값이 실제 15분 단위 실측이 아니라, 같은 업종 코호트의 "
                    "시간 내 상대 형태를 정각 실측값에 앵커링해 추정한 값(data_resolution='1hour' "
                    "계기 중 한식 코호트가 충분한 경우에만 적용됨)"
    )


class DayStatusResponse(BaseModel):
    store_id: int = Field(examples=[1])
    date: str = Field(examples=["2026-08-15"])
    data_resolution: str = Field(
        default="15min", examples=["15min"],
        description="'15min'(정상, rows 96행) | '1hour' - 1hour인 매장은 결측 슬롯의 판정 자체를 "
                    "생략하므로 rows 길이가 96보다 짧을 수 있다(빈 슬롯=결측 gap으로 해석할 것).",
    )
    rows: list[DayStatusRow] = Field(
        description="15분 슬롯당 1행. data_resolution='1hour'인 매장은 96행보다 적을 수 있다."
    )


class StoreSnapshotItem(BaseModel):
    meter_id: str = Field(examples=["A-L-11"], description="stores.meter_id")
    latitude: float | None = Field(default=None, examples=[37.5315885537006], description="stores.latitude")
    longitude: float | None = Field(default=None, examples=[126.847366517265], description="stores.longitude")
    congestion_level: int = Field(
        examples=[2],
        description="혼잡도 코드. 0=해당없음(영업중이 아니거나 해당 시각 데이터 없음) | 1=하 | 2=중 | 3=상",
    )
    name: str = Field(examples=["못난이찹쌀꽈배기"], description="stores.name")
    road_address: str = Field(examples=["서울특별시 강서구 강서로12길 5"], description="stores.road_address")
    business_hours: str = Field(
        examples=["09:00-18:00"],
        description="조회 날짜의 요일 기준 영업시간(store_operating_hours). '휴무' | '24시간' | '정보없음' 가능",
    )
    schedule_status: str | None = Field(
        default=None, examples=["open_hours"],
        description="store_operating_status.schedule_status: 'open_hours' | 'closed_hours' | null(데이터 없음)",
    )
    power_status: str | None = Field(
        default=None, examples=["active"],
        description="store_operating_status.power_status: 'active' | 'low' | null(데이터 없음)",
    )
    final_status: str | None = Field(
        default=None, examples=["영업중"],
        description="'영업중' | '휴무추정' | '영업종료' | null(데이터 없음) - store_operating_status.final_status "
                    "4값 중 '예외영업'은 '영업종료'로 단순화됨",
    )
    received_active_power_kwh: float | None = Field(
        default=None, description="meter_timeseries.received_active_power_kwh(유효전력 kWh). 결측이거나 데이터 없으면 null"
    )
    biz_category_large: str | None = Field(default=None, examples=["음식"], description="stores.biz_category_large")
    message: str | None = Field(
        default=None, examples=[None],
        description="해당 시각의 데이터가 저장되어 있지 않을 때만 안내 문구가 채워짐(그 외 null)",
    )


class StoreSnapshotGroup(BaseModel):
    line_name: str = Field(examples=["A"])
    stores: list[StoreSnapshotItem]


class StoreSnapshotResponse(BaseModel):
    count: int = Field(examples=[21])
    date: str = Field(examples=["26-05-09"], description="입력값 그대로")
    time: str = Field(examples=["19:15"], description="입력값 그대로(계기별 해상도 적용은 서버 내부에서 처리됨)")
    items: list[StoreSnapshotGroup]


class StoreDetailRequest(BaseModel):
    store_id: int = Field(examples=[1], description="상가 ID (1~21). body로 받는 이유는 URL에 store_id를 노출하지 않기 위함")


class StoreDetailResponse(BaseModel):
    rating: float | None = Field(
        default=None, examples=[4.3],
        description="google_places_cache 최신 행의 raw_response_json.rating. Google Places 정보가 없으면 null",
    )
    name: str = Field(examples=["못난이찹쌀꽈배기"], description="stores.name")
    formatted_phone_number: str | None = Field(
        default=None, examples=["02-1234-5678"],
        description="raw_response_json.formatted_phone_number. 010 등 휴대폰 번호인 경우도 있음. 정보 없으면 null",
    )
    road_address: str = Field(examples=["서울특별시 강서구 강서로12길 5"], description="stores.road_address")
    weekday: dict[str, str] = Field(
        description="monday~sunday 키의 요일별 영업시간 문자열(google_places 우선/ksic_estimate 폴백). "
                    "'휴무' | '24시간' | '정보없음' 가능",
    )
    schedule_status: str | None = Field(
        default=None, examples=["open_hours"], description="'open_hours' | 'closed_hours' | null(현재 상태 데이터 없음)"
    )
    power_status: str | None = Field(
        default=None, examples=["active"], description="'active' | 'low' | null(현재 상태 데이터 없음)"
    )
    final_status: str | None = Field(
        default=None, examples=["영업중"],
        description="'영업중' | '휴무추정' | '영업종료' | null(현재 상태 데이터 없음) - 내부 4값 중 '예외영업'은 '영업종료'로 단순화됨",
    )
    biz_category_large: str | None = Field(default=None, examples=["음식"], description="stores.biz_category_large")
    website: str | None = Field(
        default=None, examples=["https://example.com"], description="raw_response_json.website. 없는 경우도 있음"
    )
    message: str | None = Field(
        default=None, examples=[None],
        description="Google Places 정보 부재/현재 상태 데이터 부재 등 안내 문구. 문제 없으면 null",
    )


class AnomalySchema(BaseModel):
    event_id: int
    meter_id: str = Field(examples=["A-L-71"])
    store_name: str | None = Field(default=None, examples=["정들옛날치킨불닭발"])
    detected_at: datetime
    level: str = Field(examples=["위험"], description="'주의' | '위험'")
    rule_triggered: str = Field(examples=["group_iqr_3.0x"])
    metric_value: float = Field(description="실제 관측된 유효전력(kWh)")
    threshold_value: float = Field(description="이 값을 넘으면 해당 level로 판정된 임계치")
    notified_at: datetime | None = Field(default=None, description="문자 발송 연동은 이번 범위 밖이라 항상 null")


class AnomalyListResponse(BaseModel):
    anomalies: list[AnomalySchema]


class TimeseriesRow(BaseModel):
    ts: datetime
    received_active_power_kwh: float | None = None
    is_synthetic: bool
    is_redistributed: bool = Field(
        description="true면 실제 15분 단위 실측이 아니라 코호트 비율로 추정한 값(DayStatusRow.is_redistributed와 동일 의미)"
    )


class MeterTimeseriesResponse(BaseModel):
    meter_id: str = Field(examples=["A-L-11"])
    date: str = Field(examples=["2026-08-15"])
    is_synthetic: bool | None = None
    data_resolution: str = Field(
        default="15min", examples=["15min"],
        description="'15min'(정상, rows 96행) | '1hour' - 1hour 계기는 rows 길이가 96보다 짧을 수 있다.",
    )
    rows: list[TimeseriesRow] = Field(description="15분 슬롯당 1행. data_resolution='1hour'이면 96행보다 적을 수 있다.")


class StoreTimeseriesResponse(BaseModel):
    store_id: int = Field(examples=[1])
    meter_id: str = Field(examples=["A-L-11"])
    date: str = Field(examples=["2026-08-15"])
    is_synthetic: bool | None = None
    data_resolution: str = Field(
        default="15min", examples=["15min"],
        description="'15min'(정상, rows 96행) | '1hour' - 1hour 계기는 rows 길이가 96보다 짧을 수 있다.",
    )
    rows: list[TimeseriesRow] = Field(description="15분 슬롯당 1행. data_resolution='1hour'이면 96행보다 적을 수 있다.")
