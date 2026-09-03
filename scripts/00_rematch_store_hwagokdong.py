# -*- coding: utf-8 -*-
"""
2-1단계: 화곡동 단일 지역으로 좁힌 AMI 계기 <-> 상가정보 재매칭.

입력(읽기 전용, 원본 파이프라인 산출물):
  visualize_analyis_data/output/step1_meta_classified.csv

출력(신규 생성 - DB에는 바로 적재하지 않고 파일로만 저장한다.
     "생성과 적재를 분리해서 재실행 시 재계산 없이 파일만 다시 읽게 한다"는
     이번 작업의 원칙 때문 - 다음 단계인 02_load_meta_and_timeseries.py가 이 파일을 읽는다):
  db/output/generated/store_ami_matched_화곡동.csv
  db/output/generated/store_ami_excluded_화곡동.csv

5건 미만이 나오면(데모 풀로 너무 적으면) "인접동 확대 여부"는 사람이 결정할
사항이라 여기서 자동으로 확대하지 않고 경고만 출력한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.config import DATA_DIR, GENERATED_DIR, VIZ_OUTPUT, settings  # noqa: E402
from ami_db.matching import load_dong_stores, load_eligible_meters, match_meters_to_stores  # noqa: E402


def main() -> None:
    step1_csv = VIZ_OUTPUT / "step1_meta_classified.csv"
    store_csv = DATA_DIR / "소상공인시장진흥공단_상가(상권)정보_서울_202606.csv"

    print(f"[1/3] 매장매칭 적격 계기 로드: {step1_csv}")
    meters = load_eligible_meters(step1_csv, settings.target_line)
    print(f"  -> {settings.target_line}선로 매칭 적격 계기 {len(meters)}건")

    print(f"[2/3] 상가정보 CSV 로드 및 법정동명='{settings.target_dong}' 필터")
    stores = load_dong_stores(store_csv, settings.target_dong)
    print(f"  -> {settings.target_dong} 후보 상가 {len(stores)}건")

    print(f"[3/3] KSIC 5자리 완전일치 매칭 (seed={settings.rng_seed})")
    matched, excluded = match_meters_to_stores(
        meters, stores, seed=settings.rng_seed, dong_name=settings.target_dong,
    )

    print(f"\n매칭 성공: {len(matched)}건 / 매칭 실패: {len(excluded)}건")
    if len(matched) < 5:
        print(
            "\n⚠️  경고: 매칭 성공 건수가 5건 미만입니다. 데모 풀로 부족할 수 있습니다.\n"
            "   인접 동으로 후보를 확대할지는 사람이 결정할 사항이므로 여기서는\n"
            "   자동으로 확대하지 않습니다 - TARGET_DONG 값을 바꿔 재실행하거나\n"
            "   팀과 상의 후 진행하세요."
        )

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    matched_path = GENERATED_DIR / f"store_ami_matched_{settings.target_dong}.csv"
    excluded_path = GENERATED_DIR / f"store_ami_excluded_{settings.target_dong}.csv"
    matched.to_csv(matched_path, index=False, encoding="utf-8-sig")
    excluded.to_csv(excluded_path, index=False, encoding="utf-8-sig")
    print(f"\n저장: {matched_path}")
    print(f"저장: {excluded_path}")


if __name__ == "__main__":
    main()
