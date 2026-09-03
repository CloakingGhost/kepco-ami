# -*- coding: utf-8 -*-
"""
.env 로드 설정.

places-api-project/src/places_api/config.py와 동일한 패턴(pydantic-settings)을
써서 레포 전체에서 설정 로딩 방식을 일관되게 유지한다.
"""
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# 이 파일 위치 기준으로 db/ 루트를 찾는다 (스크립트를 어느 작업 디렉터리에서 실행해도 .env를 찾도록).
DB_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = DB_ROOT.parent

load_dotenv(DB_ROOT / ".env")


class Settings(BaseSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "contest_db"
    db_user: str = "postgres"
    db_password: str = "postgres"

    # 기본값 빈 문자열 - 필수 필드로 만들면 Google Places와 무관한 스크립트/엔드포인트까지
    # .env에 이 키가 없으면 죽는다. 실제 검증은 사용 시점(GooglePlacesClient.__init__)에서 한다.
    google_places_api_key: str = ""

    # 파일럿 범위 고정값. 근거는 .env.example 주석 참고
    # (A선로 = 골목상권형 성격 확정, 화곡동 = 법정동명 단일값 매칭 + places-api-project 예제와의 일관성).
    target_line: str = "A"
    target_dong: str = "화곡동"

    rng_seed: int = 42

    class Config:
        env_file = str(DB_ROOT / ".env")
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"  # .env엔 배포 준비용 DUCK_DOMAIN 등 이 앱이 안 쓰는 값도 있음 - 모르는 키를 에러로 만들지 않음

    @property
    def db_url(self) -> str:
        """SQLAlchemy용 연결 문자열."""
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()

# 원본 파이프라인(visualize_analyis_data) 산출물 경로 - 읽기 전용으로만 참조한다.
# 이 서브프로젝트가 만드는 모든 신규 산출물은 DB_ROOT/output/generated 아래에만 쓴다.
VIZ_OUTPUT = REPO_ROOT / "visualize_analyis_data" / "output"
DATA_DIR = REPO_ROOT / "data"
GENERATED_DIR = DB_ROOT / "output" / "generated"
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
