"""usage — 소스에서 식별자가 실제로 쓰인 곳을 찾는다.

echo 가 '구조'를 보여준다면 이 에이전트는 '실제 작업'을 보여준다.
core/ 의 도구들이 왜 그렇게 생겼는지가 여기서 드러난다.

    - ident_pattern : IF_A 를 TOTAL_AMT 사용처로 세지 않기 위해
    - strip_comments: 주석·문자열 속 언급을 사용처로 세지 않기 위해
                      (mask_strings=True)
    - read_text     : 사내 파일이 cp949 라 utf-8 로만 읽으면 깨지므로
    - cache         : 파일 스캔이 비싸고 '찾는 이름'과는 무관하므로

단독 실행:
    python agents/usage/agent.py TOTAL_AMT
    python agents/usage/agent.py TOTAL_AMT --root SAMPLE --detail summary

챗봇에서:
    mcp_server/tools/usage.py 가 MCP 툴/리소스로 감싼다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from langgraph.graph import END, START, StateGraph  # noqa: E402

from config import SOURCE_ROOTS, USE_LLM  # noqa: E402
from core.cache import cached  # noqa: E402
from core.text import ident_pattern, read_text_with_encoding, strip_comments  # noqa: E402

Detail = Literal["full", "summary", "minimal"]

# 확장자 → strip_comments 가 아는 언어. 여기 없는 확장자는 스캔하지 않는다.
# '무엇을 안 보는지'가 명시되어야 결과가 비었을 때 원인을 안다.
LANG_BY_SUFFIX: dict[str, str] = {
    ".c": "c", ".h": "c", ".cpp": "cpp", ".java": "java", ".cs": "cs",
    ".js": "js", ".ts": "ts", ".pc": "pro",
    ".sql": "sql", ".pks": "plsql", ".pkb": "plsql",
    ".py": "python",
}

# 한 번에 훑을 파일 수 상한. 사내 소스 트리는 크고, 로컬 LLM 이 붙은
# 요청 하나가 수만 개 파일을 읽기 시작하면 챗봇이 통째로 멎는다.
MAX_FILES = 3000


class UsageState(TypedDict, total=False):
    name: str
    root_label: str
    root_path: str
    files_scanned: int
    unreadable: list[str]
    hits: list[dict[str, Any]]     # {file, line, text}
    used: str                      # yes | no | unknown
    decided_by: str                # rule | llm | static | fallback
    rule: str
    evidence: str
    comment: str
    warnings: list[str]


# --------------------------------------------------------------------------
# 파일 스캔 — 비싼 쪽
# --------------------------------------------------------------------------


@cached(ttl=300, maxsize=8, key=lambda root_path: root_path)
def scan_files(root_path: str) -> list[str]:
    """루트 아래에서 스캔 대상 파일 목록을 만든다.

    캐시 키가 root_path 하나뿐인 것이 핵심이다. 비싼 것은 디렉터리를
    훑는 일이고, 그 결과는 '어떤 식별자를 찾는가'와 무관하다.
    찾는 이름까지 키에 넣으면 이름을 바꿀 때마다 트리를 다시 훑는다
    — 캐시가 있으나 마나 해진다.
    """
    base = Path(root_path)
    if not base.is_dir():
        return []
    files: list[str] = []
    for p in sorted(base.rglob("*")):
        if len(files) >= MAX_FILES:
            break
        if p.is_file() and p.suffix.lower() in LANG_BY_SUFFIX:
            files.append(str(p))
    return files


# --------------------------------------------------------------------------
# 노드 1 — 루트 결정
# --------------------------------------------------------------------------


def resolve_root(state: UsageState) -> UsageState:
    """어느 소스 루트를 볼지 정한다. 설정은 config.SOURCE_ROOTS 뿐이다."""
    label = (state.get("root_label") or "").strip()
    if label:
        if label not in SOURCE_ROOTS:
            return {
                "root_path": "",
                "used": "unknown",
                "decided_by": "fallback",
                "rule": "unknown-root",
                "evidence": label,
                "warnings": [
                    f"'{label}' 은 설정에 없는 소스 루트다 — 확인 필요 "
                    f"(설정된 루트: {', '.join(SOURCE_ROOTS)})"
                ],
            }
        return {"root_label": label, "root_path": SOURCE_ROOTS[label]}

    # 라벨을 안 주면 첫 번째 루트. 여러 루트를 동시에 훑지 않는 이유는
    # 요청 하나가 사내 소스 전체를 읽는 사고를 막기 위해서다.
    first = next(iter(SOURCE_ROOTS.items()))
    return {"root_label": first[0], "root_path": first[1]}


# --------------------------------------------------------------------------
# 노드 2 — 검색
# --------------------------------------------------------------------------


def search(state: UsageState) -> UsageState:
    """주석·문자열을 뺀 본문에서 식별자를 찾는다."""
    if not state.get("root_path"):
        return {}                      # resolve_root 가 이미 판정했다

    name = (state.get("name") or "").strip()
    if not name:
        return {
            "used": "unknown",
            "decided_by": "fallback",
            "rule": "empty-name",
            "evidence": "",
            "warnings": ["찾을 식별자가 비어 있다 — 확인 필요"],
        }

    files = scan_files(state["root_path"])
    pattern = ident_pattern(name)
    hits: list[dict[str, Any]] = []
    unreadable: list[str] = []

    for path in files:
        try:
            text, _enc = read_text_with_encoding(path)
        except OSError as e:
            # 못 읽은 파일을 조용히 건너뛰면 '사용처 없음'과 구분되지 않는다.
            # 누락은 오탐보다 나쁘므로 목록에 남긴다.
            unreadable.append(f"{path}: {e}")
            continue

        lang = LANG_BY_SUFFIX.get(Path(path).suffix.lower(), "c")
        # mask_strings=True 인 이유: 로그 문구나 SQL 문자열에 적힌 이름은
        # 사용처가 아니다. 그냥 두면 'TOTAL_AMT is not a hit' 같은 문자열이
        # 사용처로 잡힌다.
        body = strip_comments(text, lang, mask_strings=True)
        # strip_comments 가 개행을 보존하므로 본문의 줄 번호가 원문과 같다.
        # 그래서 매칭은 본문에서 하고 근거는 원문 줄로 보여줄 수 있다.
        original = text.splitlines()
        for no, line in enumerate(body.splitlines(), start=1):
            if pattern.search(line):
                hits.append(
                    {
                        "file": path,
                        "line": no,
                        "text": (original[no - 1] if no <= len(original) else line).strip(),
                    }
                )

    warnings = [f"읽지 못한 파일 {len(unreadable)}건 — 확인 필요"] if unreadable else []

    if not files:
        # '못 봤다'와 '없다'를 같은 이름으로 쓰지 않는다.
        return {
            "files_scanned": 0, "hits": [], "unreadable": unreadable,
            "used": "unknown", "decided_by": "fallback", "rule": "no-files",
            "evidence": state["root_path"],
            "warnings": warnings
            + [f"'{state['root_path']}' 아래에 스캔할 소스가 없다 — 확인 필요"],
        }

    if hits:
        first = hits[0]
        return {
            "files_scanned": len(files), "hits": hits, "unreadable": unreadable,
            "used": "yes", "decided_by": "rule", "rule": "ident-found",
            # 근거는 실제 매칭된 줄 그대로. 이게 없으면 오판을 못 잡는다.
            "evidence": f"{Path(first['file']).name}:{first['line']} {first['text']}",
            "warnings": warnings,
        }

    return {
        "files_scanned": len(files), "hits": [], "unreadable": unreadable,
        "used": "no", "decided_by": "rule", "rule": "no-hit",
        "evidence": f"{len(files)}개 파일에서 매칭 없음",
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# 노드 3 — LLM 한마디 (선택)
# --------------------------------------------------------------------------


def explain(state: UsageState) -> UsageState:
    """USE_LLM 이 False 면 아무것도 하지 않는다.

    판정 자체는 규칙이 끝냈다. LLM 은 설명만 붙인다 — 판정을 LLM 에
    맡기면 같은 질문에 다른 답이 나오고 근거도 남지 않는다.
    """
    if not USE_LLM or state.get("used") == "unknown":
        return {}

    warnings = list(state.get("warnings") or [])
    try:
        from core.llm import get_llm

        top = state.get("hits") or []
        lines = "\n".join(f"- {h['file']}:{h['line']} {h['text']}" for h in top[:5])
        prompt = (
            f"식별자 '{state.get('name')}' 의 사용처를 한 문장으로 요약해라. "
            "추측하지 말고 아래 목록에 있는 내용만 말해라.\n"
            f"{lines or '(사용처 없음)'}"
        )
        return {"comment": str(get_llm().invoke(prompt).content).strip(),
                "warnings": warnings}
    except Exception as e:
        # 실패를 삼키지 않는다. 결과에 남겨 사용자에게 전달되게 한다.
        warnings.append(f"LLM 호출 실패 — 확인 필요: {type(e).__name__}: {e}")
        return {"comment": "", "warnings": warnings}


@cached(ttl=3600, maxsize=1, key=lambda: "graph")
def _graph():
    """컴파일된 그래프를 재사용한다(매 호출 재컴파일할 이유가 없다)."""
    g = StateGraph(UsageState)
    g.add_node("resolve_root", resolve_root)
    g.add_node("search", search)
    g.add_node("explain", explain)
    g.add_edge(START, "resolve_root")
    g.add_edge("resolve_root", "search")
    g.add_edge("search", "explain")
    g.add_edge("explain", END)
    return g.compile()


def run_usage(name: str, root: str = "", detail: Detail = "full") -> dict[str, Any]:
    """진입점.

        full    : 매칭된 줄 전부 (화면용, MCP 리소스로 나간다)
        summary : 건수와 파일 목록 일부 (LLM 컨텍스트에 들어간다)
        minimal : 판정만

    full 을 챗봇 툴에서 돌려주면 수백 줄이 매 턴 컨텍스트를 먹는다.
    로컬 LLM 에서는 그것만으로 대화가 무너진다.
    """
    state: UsageState = _graph().invoke({"name": name, "root_label": root})

    base = {
        "name": name,
        "used": state.get("used"),
        "decided_by": state.get("decided_by"),
        "rule": state.get("rule"),
    }
    if detail == "minimal":
        return base

    hits = state.get("hits") or []
    files = sorted({h["file"] for h in hits})

    if detail == "summary":
        return {
            **base,
            "root": state.get("root_label"),
            "hit_count": len(hits),
            "files_scanned": state.get("files_scanned", 0),
            # 파일 목록도 상위 5개까지만. 전체는 리소스로 가져간다.
            "files": [Path(f).name for f in files[:5]],
            "more_files": max(0, len(files) - 5),
            "comment": state.get("comment") or "",
            "warnings": state.get("warnings") or [],
        }

    return {
        **base,
        "root": state.get("root_label"),
        "root_path": state.get("root_path"),
        "hit_count": len(hits),
        "files_scanned": state.get("files_scanned", 0),
        "hits": hits,
        "evidence": state.get("evidence"),
        "unreadable": state.get("unreadable") or [],
        "comment": state.get("comment") or "",
        "warnings": state.get("warnings") or [],
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="식별자 사용처 찾기")
    ap.add_argument("name", help="찾을 식별자")
    ap.add_argument("--root", default="", help=f"소스 루트 라벨 ({', '.join(SOURCE_ROOTS)})")
    ap.add_argument("--detail", choices=["full", "summary", "minimal"], default="full")
    args = ap.parse_args()

    result = run_usage(args.name, root=args.root, detail=args.detail)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 규칙별 집계. 무엇이 어떤 근거로 판정됐는지 콘솔에도 남긴다.
    print(f"\n[판정] {result['used']} — {result['decided_by']}/{result['rule']}")
    print(f"[캐시] 파일 스캔 {scan_files.cache.stats()}")
