# -*- coding: utf-8 -*-
"""
데모 이벤트의 설명문을 미리 생성해 캐시에 채워둔다(cache/narration_cache.json).

왜 필요한가: 외부 호스팅 LLM(NVIDIA API)은 응답 시간이 예고 없이 요동친다 - 같은 모델이
2.8초였다가 500 에러를 내고, 대체 모델이 70초를 쓰기도 한다(실측). 운영 타임아웃은
앞단 프록시(30초) 때문에 10초/14초로 묶여 있어서, 느린 날에는 라이브 호출이 전부
실패하고 캐시본이 화면에 나간다.

그 캐시가 비어 있거나 옛 형식이면 시연 중에 빈 화면을 보게 되므로, **발표 전에 이
스크립트를 한 번 돌려** 여유 타임아웃(기본 120초)으로 최신 프롬프트 결과를 받아 캐시에
저장해 둔다. 운영 경로의 타임아웃은 건드리지 않는다 - 여기서만 넉넉하게 기다린다.

사용:
    uv run python scripts/20_warm_narration_cache.py            # 데모 3건 갱신
    uv run python scripts/20_warm_narration_cache.py --force    # 이미 있어도 다시 생성
    uv run python scripts/20_warm_narration_cache.py --timeout 180
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db import narrate  # noqa: E402
from ami_db.config import settings  # noqa: E402
from ami_db.db import get_engine  # noqa: E402
from ami_db.serving import get_anomaly_event  # noqa: E402

# 화면 프리셋(SafetyAlertPanel의 위험/주의반복 시나리오)이 실제로 여는 이벤트들.
# docs/안전감지_이상치_판정기준.md 5-2절의 주입 시나리오와 같다.
DEMO_EVENTS = [
    (12, datetime(2026, 7, 2, 2, 15), "위험 - A-L-60 충북식당"),
    (15, datetime(2026, 7, 22, 5, 45), "주의반복 - A-L-63 소문난순대"),
    (16, datetime(2026, 7, 15, 1, 0), "주의(연속부하) - A-L-65 와카츠"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="캐시에 이미 있어도 재생성")
    parser.add_argument("--timeout", type=int, default=120, help="모델당 대기 초(기본 120)")
    args = parser.parse_args()

    if not settings.nvidia_api_key:
        sys.exit("NVIDIA_API_KEY가 없습니다 (db/.env 확인).")

    engine = get_engine()
    cache = narrate._load_cache()
    ok = skipped = failed = 0

    for store_id, detected_at, label in DEMO_EVENTS:
        print(f"\n{'=' * 66}\n{label}")
        event = get_anomaly_event(engine, store_id, detected_at)
        if event is None:
            print("  건너뜀: DB에 해당 이벤트가 없습니다")
            failed += 1
            continue

        key = narrate._cache_key(event)
        existing = cache.get(key)
        # 옛 형식(next_steps가 없던 시절)은 있어도 다시 만든다 - 화면의 '대처방안'이
        # 비어 보이는 것을 막기 위함.
        if existing and existing.get("next_steps") and not args.force:
            print("  건너뜀: 최신 형식 캐시가 이미 있습니다 (--force로 재생성)")
            skipped += 1
            continue

        for model in (settings.nvidia_model, settings.nvidia_model_fallback):
            try:
                print(f"  {model} 호출 중(최대 {args.timeout}초)...")
                result = narrate._narrate_with_model(event, model, args.timeout)
            except Exception as e:  # noqa: BLE001 - 다음 후보 모델로 넘어간다
                print(f"    실패: {type(e).__name__}: {str(e)[:80]}")
                continue

            narrate._save_cache(key, result)
            cache = narrate._load_cache()
            ok += 1
            print(f"    저장 완료 ({result.elapsed_ms}ms) | 검증={result.verification_passed} "
                  f"{result.unknown_numbers}")
            print(f"    점주: {result.owner_sms[:70]}...")
            print(f"    대처방안 {len(result.next_steps)}건 / 문의초안 "
                  f"{'있음' if result.inquiry_draft else '없음'}")
            break
        else:
            failed += 1
            print("  두 모델 모두 실패")

    print(f"\n{'=' * 66}\n생성 {ok} / 건너뜀 {skipped} / 실패 {failed}")
    print(f"캐시 파일: {narrate.CACHE_PATH}")
    if failed:
        print("실패분은 잠시 후 다시 시도하세요(외부 모델 혼잡은 시간이 지나면 풀립니다).")


if __name__ == "__main__":
    main()
