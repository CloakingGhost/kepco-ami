# -*- coding: utf-8 -*-
"""
화면에서 열릴 수 있는 이벤트의 설명문을 미리 생성해 캐시에 채워둔다.

왜 필요한가: 외부 호스팅 LLM(NVIDIA API)의 응답 시간이 예고 없이 요동친다. 2026-09-10
재점검에서는 **살아 있는 모델이 전부 24~57초**였다(1순위였던 mistral-nemotron은 아예 사망).
운영 타임아웃은 앞단 프록시(30초) 때문에 27초로 묶여 있으니, 이런 날엔 라이브 호출이
거의 다 실패하고 캐시본이 화면에 나간다. 즉 **시연의 신뢰성은 이 스크립트가 담보한다.**

캐시가 비어 있으면 실패가 곧 502가 된다. 실제로 그 일이 있었다: 시드 캐시에 데모 2건만
있는 상태에서 화면에 '이번 달 누적' 모드가 붙어 사건 15건이 클릭 가능해지자, 캐시에 없는
13건이 전부 502로 떨어졌다. 그래서 이 스크립트는 데모 3건이 아니라 **그 달에 화면에서
열릴 수 있는 이벤트 전부**를 채울 수 있어야 한다.

저장 위치가 두 곳인 점에 주의:
  기본       output/generated/narration_cache.json - gitignore. **로컬에서만 유효하다.**
  --seed     cache/narration_cache.json            - git 추적. 커밋해야 서버에 반영된다.
로컬에서 기본 모드로 채워 놓고 배포하면 서버 캐시는 여전히 비어 있다. 서버까지 가져가려면
--seed로 만든 뒤 커밋해야 한다.

사용:
    uv run python scripts/20_warm_narration_cache.py                       # 데모 3건만
    uv run python scripts/20_warm_narration_cache.py --month 26-07         # 그 달 사건 전부
    uv run python scripts/20_warm_narration_cache.py --month 26-07 --seed  # 시드에 저장(커밋용)
    uv run python scripts/20_warm_narration_cache.py --month 26-07 --force # 있어도 재생성
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db import narrate  # noqa: E402
from ami_db.config import settings  # noqa: E402
from ami_db.db import get_engine  # noqa: E402
from ami_db.serving import (  # noqa: E402
    get_anomaly_event,
    get_anomaly_period,
    get_anomaly_snapshot,
)

# 화면 프리셋(SafetyAlertPanel의 위험/주의반복 시나리오)이 실제로 여는 이벤트들.
# docs/안전감지_이상치_판정기준.md 5-2절의 주입 시나리오와 같다.
DEMO_EVENTS = [
    (12, datetime(2026, 7, 2, 2, 15), "위험 - A-L-60 충북식당"),
    (15, datetime(2026, 7, 22, 5, 45), "주의반복 - A-L-63 소문난순대"),
    (16, datetime(2026, 7, 15, 1, 0), "주의(연속부하) - A-L-65 와카츠"),
]

# 데모 바로가기가 여는 스냅샷 시점(ami-front useAnomalySnapshot.ts의 ANOMALY_PRESETS).
PRESET_SNAPSHOTS = [("26-07-02", "02:15"), ("26-07-22", "06:00")]


def collect_targets(engine, month: str | None) -> list[tuple[int, datetime, str]]:
    """
    화면에서 'AI 설명'이 열릴 수 있는 (store_id, detected_at) 목록.

    두 경로를 모두 훑는다 - 상세보기 모달은 사건의 **시작 슬롯**으로 설명을 요청하고
    (SafetyAlertPanel의 EpisodeExplanation), 경보 카드는 **그 스냅샷 슬롯**으로 요청한다
    (AlertCard). 둘은 같은 사건이라도 시각이 달라서 캐시 키가 갈린다.
    """
    targets: dict[tuple[int, datetime], str] = {}

    for store_id, detected_at, label in DEMO_EVENTS:
        targets[(store_id, detected_at)] = label

    for date_str, time_str in PRESET_SNAPSHOTS:
        for alert in get_anomaly_snapshot(engine, date_str, time_str)["alerts"]:
            key = (alert["store_id"], alert["detected_at"])
            targets.setdefault(key, f"프리셋 스냅샷 {date_str} {time_str} - {alert['store_name']}")

    if month:
        # 그 달 말일 23:45까지 = 그 달에 발생한 사건 전부.
        year, mon = 2000 + int(month[:2]), int(month[3:5])
        last_day = 31 if mon in (1, 3, 5, 7, 8, 10, 12) else 30 if mon != 2 else 28
        period = get_anomaly_period(engine, f"{month}-{last_day:02d}", "23:45")
        for store in period["stores"]:
            for ep in store["episodes"]:
                key = (store["store_id"], ep["start_at"])
                targets.setdefault(
                    key, f"{month} 사건 - {store['store_name']} {ep['level']} "
                         f"{ep['start_at']:%m-%d %H:%M}"
                )

    return [(sid, ts, label) for (sid, ts), label in
            sorted(targets.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def save_seed(key: str, result: narrate.NarrationResult) -> None:
    """git 추적 대상인 시드 파일에 병합 저장(--seed). 서버까지 가져가려면 커밋해야 한다."""
    cache = narrate._read_json(narrate.SEED_PATH)
    cache[key] = {
        "owner_sms": result.owner_sms,
        "admin_note": result.admin_note,
        "emergency_report": result.emergency_report,
        "next_steps": result.next_steps,
        "inquiry_draft": result.inquiry_draft,
        "model": result.model,
        "elapsed_ms": result.elapsed_ms,
        "verification_passed": result.verification_passed,
        "unknown_numbers": result.unknown_numbers,
    }
    narrate.SEED_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def reverify(engine, use_seed: bool) -> None:
    """
    이미 저장된 캐시의 숫자 검증만 다시 계산한다(외부 API 호출 없음).

    verify_numbers/build_allowed_numbers를 고쳤을 때 필요하다 - 검증 결과는 생성 시점에
    계산돼 캐시에 박히기 때문에, 검증기를 고쳐도 옛 항목은 계속 옛 판정을 들고 있다.
    화면이 검증 실패 항목을 숨기므로 그대로 두면 멀쩡한 설명이 계속 안 보인다.
    """
    path = narrate.SEED_PATH if use_seed else narrate.CACHE_PATH
    cache = narrate._read_json(path)
    changed = 0

    for key, entry in cache.items():
        store_id, _, ts = key.partition(":")
        event = get_anomaly_event(engine, int(store_id), datetime.strptime(ts, "%Y-%m-%d %H:%M"))
        if event is None:
            print(f"  {key}: DB에 이벤트가 없어 건너뜀")
            continue

        text_parts = [entry.get("owner_sms"), entry.get("admin_note"),
                      entry.get("emergency_report"), " ".join(entry.get("next_steps") or []),
                      entry.get("inquiry_draft")]
        unknown = narrate.verify_numbers(
            " ".join(p for p in text_parts if p), narrate.build_allowed_numbers(event)
        )
        before = entry.get("verification_passed")
        entry["verification_passed"] = not unknown
        entry["unknown_numbers"] = unknown
        if before != entry["verification_passed"]:
            changed += 1
            print(f"  {key}: {before} -> {not unknown}  (미확인 {unknown})")

    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for e in cache.values() if e.get("verification_passed"))
    print(f"\n재검증 {len(cache)}건 | 판정 변경 {changed}건 | 통과 {passed}/{len(cache)}")
    print(f"캐시 파일: {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", help="YY-MM. 그 달에 발생한 사건 전부를 대상에 추가")
    parser.add_argument("--force", action="store_true", help="캐시에 이미 있어도 재생성")
    parser.add_argument("--seed", action="store_true",
                        help="런타임 캐시가 아니라 git 추적 시드에 저장(배포에 포함시키려면 필수)")
    parser.add_argument("--reverify", action="store_true",
                        help="생성 없이 기존 캐시의 숫자 검증만 다시 계산(검증기를 고쳤을 때)")
    parser.add_argument("--timeout", type=int, default=120, help="모델당 대기 초(기본 120)")
    args = parser.parse_args()

    engine = get_engine()

    if args.reverify:
        reverify(engine, args.seed)
        return

    if not settings.nvidia_api_key:
        sys.exit("NVIDIA_API_KEY가 없습니다 (db/.env 확인).")

    targets = collect_targets(engine, args.month)
    cache = narrate._load_cache()
    store = save_seed if args.seed else narrate._save_cache

    print(f"대상 {len(targets)}건 | 저장 위치: "
          f"{narrate.SEED_PATH if args.seed else narrate.CACHE_PATH}")
    if args.seed:
        print("  (--seed: 생성 후 git commit 해야 서버에 반영된다)")

    ok = skipped = failed = 0
    for index, (store_id, detected_at, label) in enumerate(targets, 1):
        print(f"\n[{index}/{len(targets)}] {label}")
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

            # 대처방안이 비어 오는 경우가 있어(모델별 편차) 그때는 다음 모델로 넘긴다.
            # 캐시는 한 번 저장하면 계속 그 문장이 나가므로 반쪽짜리를 굳히면 안 된다.
            if not result.next_steps:
                print("    대처방안이 비어 다음 모델로 재시도")
                continue

            store(key, result)
            cache = narrate._load_cache()  # 시드+런타임을 합쳐 다시 읽는다(양쪽 모드 공통)
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
    print(f"캐시 파일: {narrate.SEED_PATH if args.seed else narrate.CACHE_PATH}")
    if failed:
        print("실패분은 잠시 후 다시 시도하세요(외부 모델 혼잡은 시간이 지나면 풀립니다).")


if __name__ == "__main__":
    main()
