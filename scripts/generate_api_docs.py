# -*- coding: utf-8 -*-
"""
app/serving_api.py의 OpenAPI 스키마에서 db/docs/API_REFERENCE.md를 자동 생성한다.
(places-api-project/scripts/generate_api_docs.py와 동일한 목적 - Swagger를 매번
켜지 않고도 이 파일만 보면 엔드포인트/파라미터/예시를 알 수 있게 함.)

코드(엔드포인트·파라미터·응답 모델)를 수정했다면 이 문서도 재생성해야 한다:
  uv run python scripts/generate_api_docs.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.serving_api import app  # noqa: E402

DOCS_PATH = ROOT / "docs" / "API_REFERENCE.md"

TAG_ORDER = ["기본", "매장정보", "영업유무·혼잡도", "안전감지", "원시 전력값"]


def resolve_schema(schema: dict, components: dict) -> dict:
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        return components.get(name, {})
    return schema


def render_param_table(parameters: list[dict]) -> str:
    if not parameters:
        return ""
    lines = ["| 이름 | 위치 | 타입 | 필수 | 예시 | 설명 |", "|---|---|---|:---:|---|---|"]
    for p in parameters:
        schema = p.get("schema", {})
        type_ = schema.get("type", schema.get("anyOf", [{}])[0].get("type", "?"))
        examples = schema.get("examples") or ([schema["example"]] if "example" in schema else [])
        example_str = ", ".join(str(e) for e in examples) if examples else ""
        required = "O" if p.get("required") else ""
        lines.append(
            f"| `{p['name']}` | {p['in']} | `{type_}` | {required} | {example_str} | {p.get('description', '')} |"
        )
    return "\n".join(lines) + "\n"


def _field_table(props: dict) -> str:
    lines = ["| 필드 | 타입 | 설명 |", "|---|---|---|"]
    for field_name, field_schema in props.items():
        if "anyOf" in field_schema:
            types = [s.get("type", s.get("$ref", "null").split("/")[-1]) for s in field_schema["anyOf"]]
            type_ = " \\| ".join(types)
        elif field_schema.get("$ref"):
            type_ = field_schema["$ref"].split("/")[-1]
        elif field_schema.get("type") == "array" and field_schema.get("items", {}).get("$ref"):
            type_ = field_schema["items"]["$ref"].split("/")[-1] + "[]"
        elif field_schema.get("type") == "array":
            type_ = f"{field_schema['items'].get('type', '?')}[]"
        else:
            type_ = field_schema.get("type", "?")
        desc = field_schema.get("description", "")
        lines.append(f"| `{field_name}` | `{type_}` | {desc} |")
    return "\n".join(lines) + "\n"


def _referenced_schema_names(props: dict) -> list[str]:
    """이 스키마의 필드들이 참조하는 다른 스키마 이름 목록(중첩 펼침 블록 렌더링용)."""
    names = []
    for field_schema in props.values():
        ref = field_schema.get("$ref")
        if not ref and field_schema.get("type") == "array":
            ref = field_schema.get("items", {}).get("$ref")
        if not ref and "anyOf" in field_schema:
            for s in field_schema["anyOf"]:
                if s.get("$ref"):
                    ref = s["$ref"]
                    break
        if ref:
            names.append(ref.split("/")[-1])
    return names


def render_response_fields(schema_name: str, components: dict) -> str:
    """
    최상위 응답 스키마의 필드 표 + 그 필드가 참조하는 하위 스키마(예: 리스트 안의
    개별 객체 모양)를 <details> 펼침 블록으로 한 단계 더 보여준다 - 안 그러면
    "rows: DayStatusRow[]"처럼 타입 이름만 보이고 실제 안에 뭐가 들었는지
    알 수 없어서, places-api-project/docs/4_API_REFERENCE.md와 동일하게 처리한다.
    """
    schema = components.get(schema_name, {})
    props = schema.get("properties", {})
    if not props:
        return ""

    out = [_field_table(props)]
    for ref_name in _referenced_schema_names(props):
        nested = components.get(ref_name, {})
        nested_props = nested.get("properties", {})
        if not nested_props:
            continue
        out.append(
            f"<details><summary><code>{ref_name}</code> 필드 상세</summary>\n\n"
            + _field_table(nested_props)
            + "\n</details>\n"
        )
    return "\n".join(out)


def main() -> None:
    spec = app.openapi()
    components = spec.get("components", {}).get("schemas", {})

    by_tag: dict[str, list[tuple[str, str, dict]]] = {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            tag = (op.get("tags") or ["기타"])[0]
            by_tag.setdefault(tag, []).append((method.upper(), path, op))

    lines = [
        "# AMI 영업유무·혼잡도·안전감지 API 레퍼런스",
        "",
        f"> 이 문서는 `scripts/generate_api_docs.py`가 FastAPI OpenAPI 스키마에서 **자동 생성**했습니다.",
        f"> 코드(엔드포인트·모델)를 고쳤다면 재생성하세요: `uv run python scripts/generate_api_docs.py`",
        ">",
        f"> 버전: `{spec['info']['version']}` · 생성 시각: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}`",
        "",
        f"**Base URL(로컬)**: `http://localhost:8000`  (Swagger UI: `http://localhost:8000/docs`)",
        "",
        spec["info"].get("description", ""),
        "",
        "## 목차",
    ]
    tags_present = [t for t in TAG_ORDER if t in by_tag] + [t for t in by_tag if t not in TAG_ORDER]
    for t in tags_present:
        lines.append(f"- [{t}](#{t.replace(' ', '-').replace('·', '')})")
    lines.append("")

    for tag in tags_present:
        lines.append(f"## {tag}\n")
        for method, path, op in by_tag[tag]:
            lines.append(f"### `{method} {path}`")
            lines.append(f"**{op.get('summary', '')}**\n")
            if op.get("description"):
                lines.append(op["description"].strip() + "\n")

            param_table = render_param_table(op.get("parameters", []))
            if param_table:
                lines.append("**파라미터**\n")
                lines.append(param_table)

            resp_200 = op.get("responses", {}).get("200", {})
            content = resp_200.get("content", {}).get("application/json", {})
            resp_schema = content.get("schema", {})
            if "$ref" in resp_schema:
                schema_name = resp_schema["$ref"].split("/")[-1]
                lines.append(f"**응답 (200)** — `{schema_name}`\n")
                field_table = render_response_fields(schema_name, components)
                if field_table:
                    lines.append(field_table)
            lines.append("---\n")

    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"저장: {DOCS_PATH}")


if __name__ == "__main__":
    main()
