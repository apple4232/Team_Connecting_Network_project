#!/usr/bin/env python3
"""3차 반복 · Solar 장애 진단: 조회 도구 → 요청 생성 → 호출 → 평가.

역할 분담 원칙: Python(ftclient)의 감지·재접속·이어받기는 Solar 응답이나
API 상태와 완전히 독립이다. 이 스크립트는 observe_run.py 가 이미 남긴
관측 자료를 읽어 Solar 에게 진단을 요청하고 사전 기준으로 평가만 한다.

Solar 조회 도구 (2개로 제한):
  get_packet_summary(run_id)  패킷 관측 요약. 이 환경에서는 캡처가
                              불가하므로 {available:false, reason} 반환
  get_app_log_events(run_id)  evidence.jsonl 의 응용 관측 이벤트
                              (허용 필드만 — 경로/해시/주입 기록 미포함)

진단 상태 정의 (관측 가능 범위에 맞춤 — 프롬프트에도 동일하게 명시):
  no_failure_observed   관측 구간에서 전송 완료(TRANSFER_COMPLETED)와
                        해시 검증 통과(HASH_VERIFIED=SHA256_OK)가 있고
                        중단 이벤트(INTERRUPTED)가 없음
  early_connection_close 예상 크기 도달 전 연결 종료로 중단
                        (INTERRUPTED reason="connection closed" 또는
                        CONNECTION_CLOSED file_pos<remaining) 및 이후
                        재접속 REQUEST(offset>0)
  timeout_or_stall      수신 대기 시간 초과로 중단
                        (INTERRUPTED reason="recv timeout")
  insufficient_evidence 위 어느 쪽도 확정할 관측이 없음 — 이때는
                        판단 보류(withheld=true)와 필요 증거(missing_
                        evidence)를 밝히는 것이 정답

호출 모드:
  실제 호출  UPSTAGE_API_KEY 환경변수가 있을 때 (--real 로 강제)
  모의 검증  키가 없거나 --mock 지정 시. 로컬 규칙 기반 응답으로
             조회 도구·응답 파싱·평가 흐름만 검증 (Solar 성능 평가 아님)

실행:
  python solar_diag.py --all            # 모든 실행 사례 진단+평가표
  python solar_diag.py --run run-...    # 단일 사례
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

from demo import force_utf8_stdout, load_jsonl

BASE = pathlib.Path(__file__).resolve().parent
ART = BASE / "diag_artifacts"

SOLAR_URL = "https://api.upstage.ai/v1/solar/chat/completions"
SOLAR_MODEL = os.environ.get("UPSTAGE_MODEL", "solar-pro")
API_KEY_ENV = "UPSTAGE_API_KEY"

STATUSES = ("early_connection_close", "timeout_or_stall",
            "no_failure_observed", "insufficient_evidence")

# 사전 평가 기준: 실행 전에 정의. 케이스명은 팀 평가용이며 Solar 에게는
# 보내지 않는다. "support_events" = 진단을 뒷받침하는 이벤트 유형 집합.
CRITERIA = {
    "normal": {"expected": "no_failure_observed", "withheld": False,
               "support_events": {"TRANSFER_COMPLETED", "SHA256_OK"}},
    "early_close": {"expected": "early_connection_close", "withheld": False,
                    "support_events": {"INTERRUPTED", "CONNECTION_CLOSED",
                                       "REQUEST"}},
    "insufficient": {"expected": "insufficient_evidence", "withheld": True,
                     "support_events": set()},  # 확정 근거가 없음이 정상
    "packet_loss": {"expected": "timeout_or_stall", "withheld": False,
                    "support_events": {"INTERRUPTED"},
                    "note": "패킷 손실 실험 미검증 — 이 환경에서 재현 못함"},
}

SYSTEM_PROMPT = """당신은 네트워크 파일 전송 로그 진단기다. 아래 조회 도구 결과만을 근거로 진단한다.

조회 도구(2개):
- get_packet_summary: 패킷 캡처 요약. available=false 이면 패킷 관측이 없다는 뜻이다.
- get_app_log_events: 응용 관측 이벤트. source=server_app/client_app, ts는 같은 호스트 벽시계다.

진단 상태(정확히 하나 선택):
- early_connection_close: 예상 크기 도달 전 연결이 닫혀 전송 중단.
  근거 예: INTERRUPTED(reason="connection closed"), CONNECTION_CLOSED(file_pos < 전체), 이후 REQUEST(offset>0) 재접속.
- timeout_or_stall: 수신 대기 시간 초과로 중단. 근거 예: INTERRUPTED(reason="recv timeout").
- no_failure_observed: 중단 없이 TRANSFER_COMPLETED 와 SHA256_OK(해시 검증 통과)가 관찰됨.
- insufficient_evidence: 어느 진단도 확정할 관측이 부족. 이때는 판단을 보류해야 한다.

출력 규칙: 반드시 아래 JSON 하나만 출력하고 다른 문장을 덧붙이지 마라.
{
  "diagnosis": "<위 4개 중 하나>",
  "evidence_ids": ["관측을 직접 뒷받침하는 evidence_id 목록 — 최소 1개, insufficient_evidence 면 빈 배열 가능"],
  "withheld": <true|false, insufficient_evidence 이면 true>,
  "missing_evidence": ["진단 확정에 필요하지만 관측되지 않은 증거 설명 — 보류 시 필수, 아니면 빈 배열"]
}
근거 없는 추측 금지: 관측에 없는 사실(FIN/RST 여부 등 패킷 정보)을 근거로 쓰지 마라."""


# ---------------------------------------------------------------- 조회 도구
def get_packet_summary(run_id: str) -> dict:
    """패킷 관측 요약 조회. 캡처 불가 환경에서는 그 사실 자체를 반환."""
    meta = load_meta(run_id)
    cap = meta.get("capture", {})
    if not cap.get("available"):
        return {"available": False,
                "reason": cap.get("reason") or "패킷 캡처 도구/권한 없음",
                "note": "이 실행에는 패킷 관측이 없다. TCP 플래그(FIN/RST)·"
                        "재전송은 관측되지 않은 것으로 간주한다."}
    # 캡처 가능 환경으로 옮기면 여기서 요약을 만들어 반환한다 (확장점)
    return {"available": True, "summary": "(캡처 요약 파서 미구현)"}


def get_app_log_events(run_id: str) -> list[dict]:
    """응용 관측 이벤트 조회 (evidence.jsonl — 이미 허용 필드만 남음)."""
    return load_jsonl(ART / run_id / "evidence.jsonl")


def load_meta(run_id: str) -> dict:
    p = ART / run_id / "run_meta.json"
    if not p.is_file():
        raise FileNotFoundError(f"알 수 없는 실행 ID: {run_id} ({p} 없음)")
    return json.loads(p.read_text(encoding="utf-8"))


def list_runs() -> list[str]:
    return sorted(d.name for d in ART.iterdir()
                  if d.is_dir() and d.name.startswith("run-")
                  and (d / "run_meta.json").is_file())


# ------------------------------------------------------------ 요청/호출
def build_request(run_id: str) -> dict:
    packet = get_packet_summary(run_id)
    events = get_app_log_events(run_id)
    user = {"run_ref": run_id,
            "get_packet_summary": packet,
            "get_app_log_events": events}
    return {"model": SOLAR_MODEL, "temperature": 0,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user",
                          "content": json.dumps(user, ensure_ascii=False)}]}


def call_solar(request: dict, api_key: str) -> dict:
    """실제 Upstage Solar 호출 (urllib, 표준 라이브러리만)."""
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        SOLAR_URL, data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return {"content": body["choices"][0]["message"]["content"],
            "usage": body.get("usage"), "finish": body["choices"][0].get(
                "finish_reason")}


def mock_solar(request: dict) -> dict:
    """모의 Solar: 파이프라인 검증용 규칙 응답 (Solar 성능 평가 아님).

    요청에 포함된 조회 도구 결과(사용자 메시지 JSON)를 그대로 읽어
    조회→판단→응답 형식이 실제 호출과 동일하게 흐르는지 확인한다.
    """
    user = json.loads(request["messages"][1]["content"])
    events = user["get_app_log_events"]
    by_event = {e["event"]: e for e in events}
    ids = [e["evidence_id"] for e in events]

    if "INTERRUPTED" in by_event:
        reason = by_event["INTERRUPTED"].get("reason", "")
        if reason == "recv timeout":
            diag, evs = "timeout_or_stall", ["INTERRUPTED"]
        elif "CONNECTION_CLOSED" in by_event:
            diag = "early_connection_close"
            evs = ["INTERRUPTED", "CONNECTION_CLOSED"]
            if "REQUEST" in by_event:
                evs.append("REQUEST")
        else:
            diag, evs = "early_connection_close", ["INTERRUPTED"]
        content = json.dumps({
            "diagnosis": diag,
            "evidence_ids": [i for e in evs for i in
                             _ids_of(events, e)],
            "withheld": False, "missing_evidence": []}, ensure_ascii=False)
    elif "TRANSFER_COMPLETED" in by_event and "SHA256_OK" in by_event:
        content = json.dumps({
            "diagnosis": "no_failure_observed",
            "evidence_ids": _ids_of(events, "TRANSFER_COMPLETED")
            + _ids_of(events, "SHA256_OK"),
            "withheld": False, "missing_evidence": []}, ensure_ascii=False)
    else:
        missing = ["전송 완료(TRANSFER_COMPLETED) 또는 중단(INTERRUPTED) 이벤트",
                   "연결 종료 방향 관측(CONNECTION_CLOSED) 또는 패킷 캡처 요약"]
        content = json.dumps({
            "diagnosis": "insufficient_evidence",
            "evidence_ids": ids[:1],
            "withheld": True, "missing_evidence": missing},
            ensure_ascii=False)
    return {"content": content, "usage": None, "finish": "mock",
            "_mock": True}


def _ids_of(events: list[dict], event: str) -> list[str]:
    return [e["evidence_id"] for e in events if e["event"] == event]


# ------------------------------------------------------------ 파싱/평가
def parse_diagnosis(raw_content: str) -> dict:
    """응답에서 진단 JSON 추출 (```json 펜스 허용) + 스키마 검증."""
    text = raw_content.strip()
    if "```" in text:
        for line_seg in text.split("```"):
            seg = line_seg.strip()
            if seg.startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{") and seg.rstrip().endswith("}"):
                text = seg
                break
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"JSON 객체를 찾을 수 없음: {raw_content[:200]!r}")
    parsed = json.loads(text[start:end + 1])
    if parsed.get("diagnosis") not in STATUSES:
        raise ValueError(f"알 수 없는 diagnosis: {parsed.get('diagnosis')!r}")
    if not isinstance(parsed.get("evidence_ids"), list):
        raise ValueError("evidence_ids 가 리스트가 아님")
    parsed["withheld"] = bool(parsed.get("withheld", False))
    return parsed


def evaluate(run_id: str, parsed: dict) -> dict:
    """사전 기준 대조. 근거 '존재'와 '지지'를 구분한다."""
    meta = load_meta(run_id)
    crit = CRITERIA[meta["case"]]
    events = get_app_log_events(run_id)
    existing = {e["evidence_id"]: e["event"] for e in events}

    ids_exist = all(i in existing for i in parsed["evidence_ids"])
    cited_events = {existing[i] for i in parsed["evidence_ids"]
                    if i in existing}
    supports = (cited_events & crit["support_events"]) != set() \
        if crit["support_events"] else parsed["withheld"]

    return {
        "run_id": run_id, "case": meta["case"],
        "expected_diagnosis": crit["expected"],
        "solar_diagnosis": parsed["diagnosis"],
        "diagnosis_match": parsed["diagnosis"] == crit["expected"],
        "expected_withheld": crit["withheld"],
        "withheld": parsed["withheld"],
        "withheld_match": parsed["withheld"] == crit["withheld"],
        "evidence_ids_exist": ids_exist,
        "evidence_supports": bool(supports),
        "missing_evidence": parsed.get("missing_evidence", []),
        # Python 복구 결과 — 진단 성패와 무관하게 별도 기록
        "python_recovery": {"client_exit": meta["client_exit"],
                            "recovered": meta["recovered"],
                            "final_hash_match": meta["final_hash_match"]},
    }


# ---------------------------------------------------------------- 실행
def diagnose(run_id: str, mode: str, api_key: str | None) -> dict:
    out = ART / "solar" / run_id
    out.mkdir(parents=True, exist_ok=True)
    request = build_request(run_id)
    (out / "request.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")

    real = mode == "real" or (mode == "auto" and api_key)
    try:
        result = call_solar(request, api_key) if real \
            else mock_solar(request)
        label = "실제 호출" if not result.get("_mock") else "모의 검증(MOCK)"
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            KeyError, ValueError) as exc:
        (out / "response_raw.txt").write_text(repr(exc), encoding="utf-8")
        print(f"[diag] {run_id}: Solar 호출 실패 → {exc!r}")
        return {"run_id": run_id, "error": repr(exc), "mode": "real" if real else "mock"}

    (out / "response_raw.txt").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    parsed = parse_diagnosis(result["content"])
    (out / "parsed.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    ev = evaluate(run_id, parsed)
    ev["mode"] = "실제 Solar 호출" if real else "모의 검증(MOCK)"
    (out / "evaluation.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[diag] {run_id}: {ev['mode']} → {ev['solar_diagnosis']} "
          f"(기대 {ev['expected_diagnosis']}, "
          f"진단 {'일치' if ev['diagnosis_match'] else '불일치'}, "
          f"보류={'예' if ev['withheld'] else '아니오'}"
          f"{'/일치' if ev['withheld_match'] else '/불일치'}, "
          f"근거 존재={'예' if ev['evidence_ids_exist'] else '아니오'}, "
          f"지지={'예' if ev['evidence_supports'] else '아니오'})")
    return ev


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(description="Solar 장애 진단 + 사전 기준 평가")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run", help="실행 ID (예: run-20261109T221500-01)")
    g.add_argument("--all", action="store_true", help="모든 실행 사례 진단")
    ap.add_argument("--mock", action="store_true",
                    help="실제 호출 없이 모의 검증만")
    ap.add_argument("--real", action="store_true",
                    help="UPSTAGE_API_KEY 필요 — 실제 호출 강제")
    args = ap.parse_args()

    api_key = os.environ.get(API_KEY_ENV)
    mode = "mock" if (args.mock or not api_key) else "real"
    if args.real and not api_key:
        print(f"[diag] {API_KEY_ENV} 환경변수가 없어 실제 호출을 할 수 없다",
              file=sys.stderr)
        return 2
    if mode == "mock":
        print(f"[diag] {API_KEY_ENV} 없음 또는 --mock → 모의 검증으로 실행 "
              "(조회 도구·파싱·평가 흐름 검증용, Solar 성능 평가 아님)")
    else:
        print(f"[diag] 실제 Solar 호출: model={SOLAR_MODEL} url={SOLAR_URL}")

    runs = list_runs() if args.all else [args.run]
    results = []
    for run_id in runs:
        results.append(diagnose(run_id, mode, api_key))

    ok = [r for r in results if "error" not in r]
    print(f"\n=== 진단 결과표 ({len(ok)}/{len(results)} 사례 처리) ===")
    print(f"{'case':<13} {'기대':<22} {'Solar 진단':<22} {'일치':<4} "
          f"{'보류(기대/실제)':<12} {'근거존재':<6} {'지지':<4} {'복구':<6} {'모드'}")
    for r in results:
        if "error" in r:
            print(f"{r['run_id']}: 호출 실패 ({r['error'][:40]}...)")
            continue
        mode = "MOCK" if "모의" in r["mode"] else "REAL"
        print(f"{r['case']:<13} {r['expected_diagnosis']:<22} "
              f"{r['solar_diagnosis']:<22} "
              f"{'O' if r['diagnosis_match'] else 'X':<4} "
              f"{('예' if r['expected_withheld'] else '아니오')}/"
              f"{'예' if r['withheld'] else '아니오'}    "
              f"{'O' if r['evidence_ids_exist'] else 'X':<6} "
              f"{'O' if r['evidence_supports'] else 'X':<4} "
              f"{'성공' if r['python_recovery']['recovered'] else '실패':<6} "
              f"{mode}")

    all_ok = len(ok) == len(results) and all(
        r["diagnosis_match"] and r["withheld_match"]
        and r["evidence_ids_exist"] and r["evidence_supports"] for r in ok)
    print(f"[diag] overall: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
