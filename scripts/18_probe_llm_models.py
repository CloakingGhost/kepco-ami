# -*- coding: utf-8 -*-
"""
설명 계층(ami_db.narrate)에 쓸 LLM 모델 비교 실험 재현용.

결과 기록과 판정 근거는 docs/LLM_모델선정_비교실험.md에 있다. 이 스크립트는 그 표를
다시 만들 수 있게 하려고 남긴 것이고, 운영 파이프라인(00~10)과는 무관하다.

주의: /v1/models 목록에 있어도 실제 추론은 404가 나는 모델이 많다(실측 13종 중 9종).
그래서 "목록 조회 -> 후보 선정 -> 실제 호출"의 3단계를 그대로 밟는다.

사용:
    uv run python scripts/18_probe_llm_models.py            # 채택/차선 모델만 재확인
    uv run python scripts/18_probe_llm_models.py --all      # 문서의 13종 전체 재시도
    uv run python scripts/18_probe_llm_models.py --list     # 이 키로 보이는 모델 목록만
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ami_db.config import settings  # noqa: E402
from ami_db.narrate import API_URL, SYSTEM_PROMPT  # noqa: E402

MODELS_URL = "https://integrate.api.nvidia.com/v1/models"

# docs/LLM_모델선정_비교실험.md 3절 표와 같은 순서.
ALL_CANDIDATES = [
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "mistralai/mistral-large-2-instruct",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "google/gemma-4-31b-it",
    "google/gemma-3-12b-it",
    "deepseek-ai/deepseek-v4-flash-0731",
    "nvidia/nemotron-3-super-120b-a12b",
    "moonshotai/kimi-k2.6",
    "openai/gpt-oss-20b",
    "nvidia/llama-3.1-nemotron-51b-instruct",
    "nvidia/nemotron-nano-3-30b-a3b",
    "mistralai/mistral-nemotron",
    "nv-mistralai/mistral-nemo-12b-instruct",
]
DEFAULT_CANDIDATES = ["mistralai/mistral-nemotron", "google/gemma-4-31b-it"]

# 실제 DB에 있는 위험 데모 이벤트(A-L-60 충북식당). 지어낸 입력이 아니라 실측값이다.
PROBE_EVENT = {
    "매장명": "충북식당",
    "업종": "한식 음식점",
    "계약전력_kW": 45,
    "안전등급": "위험",
    "발동규칙": "KEC 212.3 표 212.3-2 산업용 배선차단기 기준 - 계약전력 130%가 60분 지속",
    "감지시각": "2026-07-02 02:15",
    "관측_전력량_kWh_15분": 18.95,
    "임계_전력량_kWh_15분": 14.63,
    "판정_범위": "영업시간 외(비영업시간)",
}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.nvidia_api_key}",
        "Content-Type": "application/json",
    }


def list_models() -> list[str]:
    req = urllib.request.Request(MODELS_URL, headers=_headers())
    with urllib.request.urlopen(req, timeout=30) as res:
        return sorted(m["id"] for m in json.load(res).get("data", []))


def probe(model: str, max_tokens: int = 700, timeout: int = 120) -> tuple[str, float]:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(PROBE_EVENT, ensure_ascii=False, indent=2)},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers=_headers())
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as res:
        payload = json.load(res)
    return payload["choices"][0]["message"]["content"], time.time() - started


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="문서의 13종 전체 재시도")
    parser.add_argument("--list", action="store_true", help="이 키로 보이는 모델 목록만 출력")
    args = parser.parse_args()

    if not settings.nvidia_api_key:
        sys.exit("NVIDIA_API_KEY가 없습니다 (db/.env 확인).")

    if args.list:
        ids = list_models()
        print(f"총 {len(ids)}개\n" + "\n".join(ids))
        return

    candidates = ALL_CANDIDATES if args.all else DEFAULT_CANDIDATES
    print(f"{len(candidates)}종 시도 (콜드스타트 영향을 보려고 모델당 2회 호출)\n")

    for model in candidates:
        print("=" * 70)
        print(model)
        for attempt in (1, 2):
            try:
                out, elapsed = probe(model)
                print(f"  [{attempt}회차 {elapsed:.1f}s] {' '.join(out.split())[:300]}")
            except urllib.error.HTTPError as e:
                print(f"  [{attempt}회차] HTTP {e.code} - 추론 불가")
                break
            except Exception as e:  # noqa: BLE001
                print(f"  [{attempt}회차] {type(e).__name__}")
                break
            sys.stdout.flush()


if __name__ == "__main__":
    main()
