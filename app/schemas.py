# -*- coding: utf-8 -*-
"""
serving_api.py 응답 스키마.

Swagger(`/docs`)에서 "이 엔드포인트가 뭘 돌려주는지"를 미리 보여주기 위한
용도로 별도 파일로 분리했다(엔드포인트 함수 정의와 응답 모양 정의를 섞으면
serving_api.py가 너무 길어짐). places-api-project/src/places_api/models.py와
같은 이유로 분리한 것.
"""
from __future__ import annotations

from datetime import date as date_type, datetime, time
from typing import Literal

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
        description="'15min' | '1hour'. 현재 21개 매장 전부 '15min'이다 - 원천 계기가 1시간 적산값만 "
                    "보고하는 5개 매장도 적재 단계에서 그 1시간을 이루는 15분 구간 4개로 분해했기 때문"
                    "(분해된 행은 시계열의 is_redistributed=true).",
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
    final_status: str = Field(description="'영업중' | '휴무추정' | '영업종료'('예외영업'은 '영업종료'로 단순화)")
    congestion_level: int = Field(
        examples=[2], description="혼잡도 코드. 0=해당없음 | 1=여유 | 2=보통 | 3=혼잡"
    )
    received_active_power_kwh: float | None = Field(default=None, description="15분 유효전력(kWh). 결측이면 null")
    is_synthetic: bool = Field(description="false=실측(2026-04-01~06-30), true=합성(2026-07-01~오늘)")
    is_redistributed: bool = Field(
        description="true면 이 슬롯의 전력값은 계기가 15분 단위로 직접 잰 값이 아니라, 원천의 1시간 "
                    "적산값을 그 1시간을 이루는 15분 구간 4개에 전압×전류 비율로 나눠 담은 값이다(4개 합은 "
                    "실측 적산값과 일치). 원천이 1시간 적산만 보고하는 5개 매장의 모든 슬롯이 해당하고, "
                    "전압·전류가 없는 1개 매장은 4등분."
    )


class DayStatusResponse(BaseModel):
    store_id: int = Field(examples=[1])
    date: str = Field(examples=["2026-08-15"])
    data_resolution: str = Field(
        default="15min", examples=["15min"],
        description="'15min'(rows 하루 96행, 현재 전 매장) | '1hour'(결측 슬롯 판정이 생략돼 96행보다 "
                    "짧을 수 있음).",
    )
    rows: list[DayStatusRow] = Field(
        description="15분 슬롯당 1행(하루 96행). 기준 시각 이후 슬롯은 잘려서 더 적을 수 있다."
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
    date: str | None = Field(
        default=None, examples=["26-05-09"],
        description="기준 날짜 'YY-MM-DD'. time과 함께 주면 그 시점 기준으로 영업상태를 판정한다 - "
                    "목록(스냅샷 API)에서 사용자가 고른 날짜·시각을 그대로 넘기면 목록과 상세의 상태가 어긋나지 않는다. "
                    "생략하면 서버의 현재 시각 기준.",
    )
    time: str | None = Field(
        default=None, examples=["19:15"],
        description="기준 시각 'HH:MM'(00:00~23:45, 15분 단위). date와 함께 사용한다.",
    )


class StoreIdRequest(BaseModel):
    store_id: int = Field(examples=[1], description="상가 ID (1~21). body로 받는 이유는 URL에 store_id를 노출하지 않기 위함")


class StoreStatusDayRequest(BaseModel):
    store_id: int = Field(examples=[1], description="상가 ID (1~21). body로 받는 이유는 URL에 store_id를 노출하지 않기 위함")
    date: date_type = Field(
        default=date_type(2026, 5, 15), examples=["2026-05-15"],
        description="조회할 날짜. 2026-04-01~2026-06-30은 실측, 2026-07-01~오늘은 합성 데이터(is_synthetic로 구분됨).",
    )
    time: str | None = Field(
        default=None, examples=["12:00"],
        description="기준 시각 'HH:MM'(15분 단위). 이 시각 이후(미래) 슬롯은 응답에서 제외한다. "
                    "생략하면 서버의 현재 시:분을 date에 붙여서 자른다(날짜와 무관하게 항상 "
                    "00:00~그 시:분까지만 나오며, 하루 전체가 새는 일은 없다).",
    )


class MeterTimeseriesRequest(BaseModel):
    meter_id: str = Field(examples=["A-L-11"], description="계기번호. body로 받는 이유는 URL에 meter_id를 노출하지 않기 위함")
    date: date_type = Field(
        default=date_type(2026, 5, 15), examples=["2026-05-15"],
        description="조회할 날짜 (2026-04-01~오늘). 2026-06-30까지 실측, 7월은 합성 구간.",
    )
    time: str | None = Field(
        default=None, examples=["12:00"],
        description="기준 시각 'HH:MM'(15분 단위). 이 시각 이후(미래) 슬롯은 응답에서 제외한다. "
                    "생략하면 서버의 현재 시:분을 date에 붙여서 자른다(날짜와 무관하게 항상 "
                    "00:00~그 시:분까지만 나오며, 하루 전체가 새는 일은 없다).",
    )


class StoreTimeseriesRequest(BaseModel):
    store_id: int = Field(examples=[1], description="상가 ID (1~21). body로 받는 이유는 URL에 store_id를 노출하지 않기 위함")
    date: date_type = Field(
        default=date_type(2026, 5, 15), examples=["2026-05-15"],
        description="조회할 날짜 (2026-04-01~오늘). 2026-06-30까지 실측, 7월은 합성 구간.",
    )
    time: str | None = Field(
        default=None, examples=["12:00"],
        description="기준 시각 'HH:MM'(15분 단위). 이 시각 이후(미래) 슬롯은 응답에서 제외한다. "
                    "생략하면 서버의 현재 시:분을 date에 붙여서 자른다(날짜와 무관하게 항상 "
                    "00:00~그 시:분까지만 나오며, 하루 전체가 새는 일은 없다).",
    )


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
    meter_id: str = Field(examples=["A-L-60"])
    store_name: str | None = Field(default=None, examples=["충북식당"])
    detected_at: datetime
    level: str = Field(
        examples=["위험"],
        description="안전 등급. '주의'(사고가 나기에 충분한 조건 - 점검 필요) | "
                    "'위험'(실제 사고 발생 - 즉시 조치). 아무 규칙에도 안 걸린 평상시는 "
                    "'일반'이며, 이벤트 자체가 생성되지 않으므로 이 목록에는 나오지 않는다.",
    )
    rule_triggered: str = Field(
        examples=["kec212_overload_130pct_60min"],
        description="발동한 규칙. 'kec212_overload_130pct_60min'(위험: 계약전력 130%가 60분 지속 - "
                    "위험을 만드는 유일한 규칙) | 'continuous_load_80pct_180min'(주의: 계약전력 80%가 "
                    "3시간 지속) | 'empty_store_baseline_3x_60min'(주의: 매장 자신의 '진짜폐점' "
                    "시간대 baseline에서 크게 벗어나 60분 지속). 근거는 db/docs/"
                    "안전감지_이상치_판정기준.md 참고.",
    )
    metric_value: float = Field(description="실제 관측된 유효전력(kWh, 15분 슬롯 값)")
    threshold_value: float = Field(description="그 규칙이 넘어섰다고 판정한 임계치(kWh, 15분 슬롯 값)")
    notified_at: datetime | None = Field(default=None, description="알림 발송 연동은 이번 범위 밖이라 항상 null")


class AnomalyListResponse(BaseModel):
    total: int = Field(
        examples=[26],
        description="필터 조건에 걸리는 전체 건수(이번 페이지 건수가 아님). "
                    "페이지 수 = ceil(total / limit).",
    )
    limit: int = Field(examples=[50], description="이번 요청의 페이지 크기")
    offset: int = Field(examples=[0], description="이번 요청이 건너뛴 건수")
    anomalies: list[AnomalySchema] = Field(description="이번 페이지의 이벤트(최신순)")


class AnomalySnapshotAlert(BaseModel):
    store_id: int = Field(examples=[12], description="stores.store_id")
    store_name: str | None = Field(default=None, examples=["충북식당"])
    meter_id: str = Field(examples=["A-L-60"])
    level: str = Field(examples=["위험"], description="'주의' | '위험'")
    trigger_reason: str = Field(
        examples=["즉시위험"],
        description="'즉시위험'(조회 시점 슬롯에 위험 이벤트 존재) | "
                    "'주의반복'(최근 24시간 내 서로 다른 주의 사건이 3회 이상)",
    )
    rule_triggered: str = Field(
        examples=["kec212_overload_130pct_60min"],
        description="가장 최근에 발동한 규칙. 상세는 db/docs/안전감지_이상치_판정기준.md 참고.",
    )
    detected_at: datetime = Field(description="'즉시위험'은 조회 시점 슬롯, '주의반복'은 최근 사건의 슬롯")
    metric_value: float = Field(description="detected_at 시점에 실제 관측된 유효전력(kWh, 15분 슬롯 값)")
    threshold_value: float = Field(description="그 규칙이 넘어섰다고 판정한 임계치(kWh, 15분 슬롯 값)")
    repeat_count: int | None = Field(
        default=None,
        description="'주의반복'일 때만 값이 있음 - 최근 24시간 내 서로 다른 주의 사건(episode) 개수. "
                    "슬롯(15분) 원시 행 개수가 아니다(사건 하나도 여러 슬롯에 걸쳐 여러 행으로 남으므로).",
    )


class AnomalySnapshotResponse(BaseModel):
    date: str = Field(examples=["26-07-02"], description="조회 기준 날짜 'YY-MM-DD'. 생략 시 서버 현재 날짜")
    time: str = Field(examples=["02:15"], description="조회 기준 시각 'HH:MM'. 생략 시 서버 현재 시:분(15분 단위로 내림)")
    count: int = Field(examples=[1], description="alerts 배열 길이")
    alerts: list[AnomalySnapshotAlert] = Field(
        description="화면에 표시해야 할 매장 목록. 위험/주의 어느 쪽에도 안 걸리는 매장(대부분)은 나오지 않는다.",
    )


class AnomalyEpisode(BaseModel):
    start_at: datetime
    end_at: datetime
    slot_count: int = Field(examples=[4], description="이 사건에 속한 15분 슬롯 수")
    duration_min: int = Field(examples=[60], description="slot_count x 15분")
    level: str = Field(examples=["주의"], description="'주의' | '위험'")
    rule_triggered: str = Field(examples=["empty_store_baseline_3x_60min"])
    max_metric_value: float = Field(description="사건 구간 중 가장 높았던 관측값(kWh, 15분)")
    threshold_value: float = Field(description="그 규칙의 임계치(kWh, 15분)")


class AnomalyPeriodStore(BaseModel):
    store_id: int = Field(examples=[5])
    store_name: str | None = Field(default=None, examples=["유림다방"])
    meter_id: str = Field(examples=["A-L-30"])
    event_count: int = Field(examples=[12], description="기간 내 슬롯 단위 이벤트 수")
    danger_count: int = Field(examples=[0])
    caution_count: int = Field(examples=[12])
    episode_count: int = Field(examples=[3], description="연속 슬롯을 하나로 묶은 사건 수")
    latest_level: str = Field(examples=["주의"])
    latest_detected_at: datetime
    episodes: list[AnomalyEpisode] = Field(description="시간순 사건 목록")


class AnomalyPeriodResponse(BaseModel):
    date: str = Field(examples=["26-05-13"], description="조회 기준 날짜(입력값 또는 서버 현재)")
    time: str = Field(examples=["16:00"])
    from_ts: datetime = Field(examples=["2026-05-01T00:00:00"], description="그 달 1일 00:00")
    to_ts: datetime = Field(examples=["2026-05-13T16:00:00"], description="조회 기준 시점")
    store_count: int = Field(examples=[4], description="기간 내 이벤트가 있었던 매장 수")
    total_events: int = Field(examples=[43])
    danger_count: int = Field(examples=[0])
    caution_count: int = Field(examples=[43])
    stores: list[AnomalyPeriodStore] = Field(
        description="매장별 누적 결과. 위험이 있는 매장이 먼저, 그다음 건수가 많은 순.",
    )


class AnomalyExplainRequest(BaseModel):
    store_id: int = Field(examples=[12], description="상가 ID (1~21). body로 받는 이유는 URL에 store_id를 노출하지 않기 위함")
    detected_at: datetime = Field(
        examples=["2026-07-02T02:15:00"],
        description="분석할 이벤트의 감지 시각(15분 슬롯). GET /api/anomalies/snapshot의 detected_at이나 "
                    "GET /api/anomalies/period의 사건 start_at을 그대로 넘기면 된다. 해당 이벤트가 없으면 404.",
    )


class AnomalyExplainResponse(BaseModel):
    status: Literal["none", "pending", "done", "failed"] = Field(
        examples=["done"],
        description="none=요청된 적 없음 | pending=AI가 생성 중(백그라운드) | done=완료(DB 저장본) | "
                    "failed=외부 AI 실패 또는 사용 불가(다시 요청 가능). "
                    "**AI 실패는 HTTP 에러가 아니라 이 값으로 온다.**",
    )
    message: str | None = Field(
        default=None,
        examples=[None],
        description="화면에 그대로 띄울 안내문(none/pending/failed일 때). done이면 null.",
    )
    store_id: int = Field(examples=[12])
    store_name: str | None = Field(default=None, examples=["충북식당"])
    detected_at: datetime
    level: str = Field(examples=["위험"], description="'주의' | '위험' - 규칙이 이미 확정한 등급(LLM이 바꾸지 않음)")
    rule_triggered: str = Field(examples=["kec212_overload_130pct_60min"])
    owner_sms: str | None = Field(
        default=None,
        examples=["어젯밤 문을 닫으신 시간에, 매장 전기설비가 감당하도록 되어 있는 양보다 훨씬 많은 전기가 ..."],
        description="점주에게 보낼 문자 초안(2~3문장, 존댓말, 일상어). status='done'일 때만 채워진다.",
    )
    admin_note: str | None = Field(
        default=None,
        examples=["KEC 212.3 산업용 배선차단기 기준에 따라 계약전력 130%가 60분 지속된 것으로 확인됨."],
        description="관리자용 점검 사유(1~2문장, 발동 규칙과 근거 수치 명시). status='done'일 때만 채워진다.",
    )
    emergency_report: str | None = Field(
        default=None,
        description="신고 접수용 초안. level='위험'일 때만 채워지고 '주의'면 null.",
    )
    next_steps: list[str] = Field(
        default_factory=list,
        examples=[["큰 기기를 동시에 켜지 않도록 사용 시간을 나눠보세요.",
                   "건물에 여유 용량이 있는지 확인해 증설이 가능한지 알아보세요."]],
        description="점주가 지금 할 수 있는 조치 2~3가지. '감지했다'로 끝내지 않고 "
                    "'그래서 뭘 하면 되는지'까지 안내하기 위한 필드. 근거는 프롬프트에 "
                    "넣어둔 참고 안내사항(한전 증설 제도 등)뿐이며 모델이 제도를 지어내지 못한다.",
    )
    inquiry_draft: str | None = Field(
        default=None,
        examples=["안녕하세요, 충북식당입니다. 최근 영업시간 외에 전기 사용량이 설비 용량을 "
                  "넘는다는 안내를 받았습니다. 저희 매장이 증설 대상인지 확인 부탁드립니다."],
        description="점주가 한전(고객센터 123 / cyber.kepco.co.kr)이나 전기공사 업체에 "
                    "그대로 보낼 수 있는 문의 초안.",
    )
    verification_passed: bool | None = Field(
        default=None,
        examples=[True],
        description="생성문에 입력에 없던 숫자가 섞였는지 자동 대조한 결과(판정 로직이 아니라 LLM 출력 검사). "
                    "서버는 검증을 통과한 결과만 저장하므로 status='done'이면 항상 true.",
    )
    unknown_numbers: list[str] = Field(
        default_factory=list,
        examples=[[]],
        description="입력 수치와 대조되지 않은 숫자 목록. 비어 있으면 통과.",
    )
    model: str | None = Field(
        default=None, examples=["nvidia/nemotron-3-super-120b-a12b"], description="생성에 사용한 모델",
    )
    elapsed_ms: int | None = Field(default=None, examples=[28800], description="AI 생성 소요 시간(ms)")
    requested_at: datetime | None = Field(default=None, description="분석을 요청한 시각")
    completed_at: datetime | None = Field(default=None, description="분석이 끝난 시각(done일 때)")


class TimeseriesRow(BaseModel):
    ts: datetime
    received_active_power_kwh: float | None = None
    congestion_level: int = Field(
        examples=[2],
        description="혼잡도 코드. 0=해당없음(영업중이 아니거나 판정 없음) | 1=여유 | 2=보통 | 3=혼잡. "
                    "차트에 그대로 시리즈로 그릴 수 있도록 문자열이 아닌 정수로 내려간다.",
    )
    final_status: str | None = Field(
        default=None, examples=["영업중"], description="'영업중' | '휴무추정' | '영업종료' | null(판정 없음)"
    )
    is_synthetic: bool
    is_redistributed: bool = Field(
        description="DayStatusRow.is_redistributed와 같은 의미 - 원천의 1시간 적산값을 15분 구간으로 나눠 담은 값이면 true"
    )


class MeterTimeseriesResponse(BaseModel):
    meter_id: str = Field(examples=["A-L-11"])
    date: str = Field(examples=["2026-08-15"])
    is_synthetic: bool | None = None
    data_resolution: str = Field(
        default="15min", examples=["15min"],
        description="'15min'(rows 하루 96행, 현재 전 계기) | '1hour'(96행보다 짧을 수 있음).",
    )
    rows: list[TimeseriesRow] = Field(description="15분 슬롯당 1행(하루 96행). 기준 시각 이후 슬롯은 잘려서 더 적을 수 있다.")


class StoreTimeseriesResponse(BaseModel):
    store_id: int = Field(examples=[1])
    meter_id: str = Field(examples=["A-L-11"])
    date: str = Field(examples=["2026-08-15"])
    is_synthetic: bool | None = None
    data_resolution: str = Field(
        default="15min", examples=["15min"],
        description="'15min'(rows 하루 96행, 현재 전 계기) | '1hour'(96행보다 짧을 수 있음).",
    )
    rows: list[TimeseriesRow] = Field(description="15분 슬롯당 1행(하루 96행). 기준 시각 이후 슬롯은 잘려서 더 적을 수 있다.")
