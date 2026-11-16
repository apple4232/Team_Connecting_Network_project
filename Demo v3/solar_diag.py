#!/usr/bin/env python3
"""3차 반복 · Solar 장애 진단: 조회 도구 → 요청 생성 → 호출 → 평가.

역할 분담 원칙: Python(ftclient)의 감지·재접속·이어받기는 Solar 응답이나
API 상태와 완전히 독립이다. 이 스크립트는 observe_run.py 가 이미 남긴
관측 자료를 읽어 Solar 에게 진단을 요청하고 사전 기준으로 평가만 한다.

Solar 조회 도구 (2개로 제한):
  get_packet_summary(run_id)  실제 캡처를 분석한 패킷 요약.
                              요약이 없으면 {available:false, reason} 반환
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
  실제 호출  --real 지정 및 UPSTAGE_API_KEY 환경변수 설정
  모의 검증  기본 모드 또는 --mock 지정 시 로컬 규칙 기반 진단만 실행

실행:
  python solar_diag.py --all            # 모든 실행 사례 진단+평가표
  python solar_diag.py --run run-...    # 단일 사례
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import pathlib
import sys
import urllib.request

from demo import force_utf8_stdout, load_jsonl
from packet_capture import SUMMARY_FILE

BASE = pathlib.Path(__file__).resolve().parent
ART = BASE / "diag_artifacts"

SOLAR_URL = "https://api.upstage.ai/v1/chat/completions"
SOLAR_MODEL = os.environ.get("UPSTAGE_MODEL", "solar-pro4")
API_KEY_ENV = "UPSTAGE_API_KEY"

STATUSES = ("early_connection_close", "timeout_or_stall",
            "no_failure_observed", "insufficient_evidence")

# 사전 평가 기준: 실행 전에 정의. 케이스명은 팀 평가용이며 Solar 에게는
# 보내지 않는다. "support_events" = 진단을 뒷받침하는 이벤트 유형 집합.
CRITERIA = {
    "normal": "no_failure_observed",
    "early_close": "early_connection_close",
    "stall": "timeout_or_stall",
    "insufficient": "insufficient_evidence",
}
INTERRUPTION_LABELS = {
    "connection closed": "early_connection_close",
    "connection reset": "early_connection_close",
    "recv timeout": "timeout_or_stall",
}

SYSTEM_PROMPT = """당신은 네트워크 파일 전송 로그 진단기다. 아래 조회 도구 결과만을 근거로 진단한다.

조회 도구(2개):
- get_packet_summary: 패킷 캡처 요약. available=true 이면 TCP 스트림별 집계 및
  실제 프레임을 가리키는 P로 시작하는 evidence_id가 포함된다.
- get_app_log_events: 응용 관측 이벤트. source=server_app/client_app, ts는 같은 호스트 벽시계다.

진단 상태(정확히 하나 선택):
- early_connection_close: 예상 크기 도달 전 연결이 닫혀 전송 중단.
  근거 예: INTERRUPTED(reason="connection closed" 또는 "connection reset"),
  CONNECTION_CLOSED(file_pos < 전체), 이후 REQUEST(offset>0) 재접속.
- timeout_or_stall: 수신 대기 시간 초과로 중단. 근거 예: INTERRUPTED(reason="recv timeout").
- no_failure_observed: 중단이나 연결 시도 실패 없이 TRANSFER_COMPLETED 와 SHA256_OK가 관찰됨.
- insufficient_evidence: 어느 진단도 확정할 관측이 부족. 이때는 판단을 보류해야 한다.

출력 규칙: 반드시 아래 JSON 하나만 출력하고 다른 문장을 덧붙이지 마라.
{
  "diagnosis": "<위 4개 중 하나>",
  "evidence_ids": ["관측을 직접 뒷받침하는 evidence_id 목록 — 최소 1개, insufficient_evidence 면 빈 배열 가능"],
  "withheld": <true|false, insufficient_evidence 이면 true>,
  "missing_evidence": ["진단 확정에 필요하지만 관측되지 않은 증거 설명 — 보류 시 필수, 아니면 빈 배열"]
}
조기 종료·시간 초과는 해당 INTERRUPTED 이벤트 ID를 인용하라.
정상 전송은 TRANSFER_COMPLETED와 SHA256_OK를 각각 인용하라.
관련 패킷 ID가 있으면 보조 근거로 인용하라.
TCP 플래그와 재전송은 관측된 프레임만 설명하라. 재전송 의심 표시로 손실 원인을 단정하지 마라."""


# ---------------------------------------------------------------- 조회 도구
def get_packet_summary(run_id: str) -> dict:
    """실제 캡처에서 만든 패킷 요약만 돌려준다."""
    meta = load_meta(run_id)
    cap = meta.get("capture", {})
    path = ART / run_id / SUMMARY_FILE
    if cap.get("available") and cap.get("summary_status") == "ready" and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"available": False, "reason": cap.get("reason") or "패킷 관측 요약 없음"}


def get_app_log_events(run_id: str) -> list[dict]:
    """응용 관측 이벤트 조회 (evidence.jsonl — 이미 허용 필드만 남음)."""
    return load_jsonl(ART / run_id / "evidence.jsonl")


def load_meta(run_id: str) -> dict:
    p = ART / run_id / "run_meta.json"
    if not p.is_file():
        raise FileNotFoundError(f"알 수 없는 실행 ID: {run_id} ({p} 없음)")
    return json.loads(p.read_text(encoding="utf-8"))


def list_runs() -> list[str]:
    if not ART.is_dir():
        return []
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
    return {"model": SOLAR_MODEL, "temperature": 0, "max_tokens": 500,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "network_diagnosis", "strict": True,
                "schema": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "diagnosis": {"type": "string", "enum": list(STATUSES)},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "withheld": {"type": "boolean"},
                        "missing_evidence": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["diagnosis", "evidence_ids", "withheld", "missing_evidence"],
                },
            }},
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
            "usage": body.get("usage"), "model": body.get("model"),
            "finish": body["choices"][0].get("finish_reason")}


def mock_solar(request: dict) -> dict:
    """Solar와 같은 관측 자료로 판정하는 규칙 기반 비교 기준."""
    user = json.loads(request["messages"][1]["content"])
    events = user["get_app_log_events"]
    parsed = rule_diagnosis(events)
    return {"content": json.dumps(parsed, ensure_ascii=False), "usage": None, "finish": "mock"}


def interruption_label(event: dict) -> str | None:
    if event["event"] != "INTERRUPTED":
        return None
    saved, expected = event.get("saved_bytes"), event.get("expected_size")
    if type(saved) is int and type(expected) is int and 0 <= saved < expected:
        return INTERRUPTION_LABELS.get(event.get("reason"))
    return None


def rule_diagnosis(events: list[dict]) -> dict:
    labels = {label for event in events if (label := interruption_label(event))}
    completed = _ids_of(events, "TRANSFER_COMPLETED")
    verified = _ids_of(events, "SHA256_OK")
    interrupted = any(e["event"] in ("INTERRUPTED", "ATTEMPT_FAILED") for e in events)
    diagnosis = "insufficient_evidence"
    evidence_ids = []
    missing = ["전송을 완료했거나 중단한 이유를 확인할 수 있는 관측 이벤트"]
    if len(labels) == 1:
        diagnosis = labels.pop()
        evidence_ids = [e["evidence_id"] for e in events if interruption_label(e) == diagnosis]
        missing = []
    elif not interrupted and completed and verified:
        diagnosis = "no_failure_observed"
        evidence_ids = [completed[-1], verified[-1]]
        missing = []
    return {"diagnosis": diagnosis, "evidence_ids": evidence_ids,
            "withheld": diagnosis == "insufficient_evidence", "missing_evidence": missing}


def _ids_of(events: list[dict], event: str) -> list[str]:
    return [e["evidence_id"] for e in events if e["event"] == event]


# ------------------------------------------------------------ 파싱/평가
def parse_diagnosis(raw_content: str) -> dict:
    """응답에서 진단 JSON 추출 (```json 펜스 허용) + 스키마 검증."""
    if not isinstance(raw_content, str):
        raise ValueError("Solar 응답에 텍스트 본문이 없습니다")
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
    if type(parsed.get("withheld")) is not bool:
        raise ValueError("withheld 는 true 또는 false 여야 합니다")
    for key in ("evidence_ids", "missing_evidence"):
        values = parsed.get(key)
        if not isinstance(values, list) or not all(
                isinstance(value, str) and value.strip() for value in values):
            raise ValueError(f"{key} 는 비어 있지 않은 문자열의 배열이어야 합니다")
    withheld = parsed["diagnosis"] == "insufficient_evidence"
    if parsed["withheld"] != withheld or bool(parsed["missing_evidence"]) != withheld:
        raise ValueError("보류 여부와 missing_evidence 가 진단과 일치해야 합니다")
    if not withheld and not parsed["evidence_ids"]:
        raise ValueError("판정에는 근거 ID가 하나 이상 필요합니다")
    return parsed


def evaluate(run_id: str, parsed: dict) -> dict:
    """사전 기준 대조. 근거 '존재'와 '지지'를 구분한다."""
    meta = load_meta(run_id)
    expected = CRITERIA[meta["case"]]
    expected_withheld = expected == "insufficient_evidence"
    events = get_app_log_events(run_id)
    existing = {e["evidence_id"]: e for e in events}
    packet_ids = {e["evidence_id"] for e in get_packet_summary(run_id).get("evidence", [])}

    ids_exist = all(i in existing or i in packet_ids for i in parsed["evidence_ids"])
    cited_events = [existing[i] for i in parsed["evidence_ids"] if i in existing]
    diagnosis = parsed["diagnosis"]
    if diagnosis == "insufficient_evidence":
        supports = rule_diagnosis(events)["withheld"] and bool(parsed["missing_evidence"])
    elif diagnosis == "no_failure_observed":
        cited_types = {e["event"] for e in cited_events}
        supports = {"TRANSFER_COMPLETED", "SHA256_OK"} <= cited_types and not any(
            e["event"] in ("INTERRUPTED", "ATTEMPT_FAILED") for e in events)
    else:
        supports = any(interruption_label(e) == diagnosis for e in cited_events)

    return {
        "run_id": run_id, "case": meta["case"],
        "expected_diagnosis": expected,
        "solar_diagnosis": diagnosis,
        "diagnosis_match": diagnosis == expected,
        "expected_withheld": expected_withheld,
        "withheld": parsed["withheld"],
        "withheld_match": parsed["withheld"] == expected_withheld,
        "evidence_ids_exist": ids_exist,
        "evidence_supports": ids_exist and supports,
        "evidence_ids": parsed["evidence_ids"],
        "missing_evidence": parsed.get("missing_evidence", []),
        # Python 복구 결과 — 진단 성패와 무관하게 별도 기록
        "python_recovery": {"client_exit": meta["client_exit"],
                            "recovered": meta["recovered"],
                            "final_hash_match": meta["final_hash_match"],
                            "resume_offset": meta.get("resume_offset")},
    }


# ---------------------------------------------------------------- 실행
def diagnose(run_id: str, out: pathlib.Path, api_key: str | None) -> dict:
    out.mkdir(parents=True)
    request = build_request(run_id)
    (out / "request.json").write_text(
        json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    rule = evaluate(run_id, parse_diagnosis(mock_solar(request)["content"]))
    (out / "rule.json").write_text(
        json.dumps(rule, ensure_ascii=False, indent=2), encoding="utf-8")

    if api_key is None:
        return {"rule": rule}

    try:
        result = call_solar(request, api_key)
        (out / "response_raw.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        parsed = parse_diagnosis(result["content"])
    except (OSError, KeyError, IndexError, TypeError, ValueError) as exc:
        message = str(exc).replace(api_key, "[REDACTED]")
        (out / "error.txt").write_text(message + "\n", encoding="utf-8")
        print(f"[diag] {run_id}: 실제 Solar 진단 처리 실패 · {message}")
        return {"rule": rule, "error": message}

    ev = evaluate(run_id, parsed)
    ev["model"] = result.get("model") or SOLAR_MODEL
    (out / "solar.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"rule": rule, "solar": ev}


def report_row(result: dict) -> dict:
    rule = result["rule"]
    solar = result.get("solar", {})
    recovery = rule["python_recovery"]
    return {
        "run_id": rule["run_id"], "case": rule["case"],
        "expected": rule["expected_diagnosis"],
        "rule_diagnosis": rule["solar_diagnosis"], "rule_correct": rule["diagnosis_match"],
        "rule_evidence_ids": " ".join(rule["evidence_ids"]),
        "rule_evidence_supports": rule["evidence_supports"],
        "solar_model": solar.get("model"),
        "solar_diagnosis": solar.get("solar_diagnosis"), "solar_correct": solar.get("diagnosis_match"),
        "solar_evidence_ids": " ".join(solar.get("evidence_ids", [])),
        "solar_evidence_ids_exist": solar.get("evidence_ids_exist"),
        "solar_evidence_supports": solar.get("evidence_supports"),
        "solar_withheld": solar.get("withheld"),
        "solar_missing_evidence": " / ".join(solar.get("missing_evidence", [])),
        "solar_error": result.get("error"),
        "transfer_verified": recovery["recovered"] and recovery["final_hash_match"],
        "resume_offset": recovery["resume_offset"],
        "human_review": "",
    }


def csv_value(value):
    if isinstance(value, str) and value.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def main() -> int:
    global ART
    force_utf8_stdout()
    ap = argparse.ArgumentParser(description="규칙 기반 진단과 실제 Solar 비교")
    selection = ap.add_mutually_exclusive_group(required=True)
    selection.add_argument("--run", help="실행 ID")
    selection.add_argument("--all", action="store_true", help="저장된 모든 사례 진단")
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument("--mock", action="store_true", help="규칙 기반 진단만 실행 (기본값)")
    modes.add_argument("--real", action="store_true", help="같은 근거로 규칙과 실제 Solar 비교")
    ap.add_argument("--art-dir", type=pathlib.Path, default=ART, help="관측 결과 폴더")
    args = ap.parse_args()

    ART = args.art_dir.resolve()
    api_key = os.environ.get(API_KEY_ENV, "").strip() if args.real else None
    if args.real and not api_key:
        ap.error(f"실제 Solar 호출에는 {API_KEY_ENV} 환경변수가 필요합니다")

    recorded_runs = list_runs()
    if args.run and args.run not in recorded_runs:
        ap.error(f"관측 결과 폴더에 해당 실행 ID가 없습니다: {args.run}")
    runs = recorded_runs if args.all else [args.run]
    if not runs:
        ap.error(f"관측 결과가 없습니다: {ART}")
    if any(load_meta(run_id).get("scenario_reproduced") is False for run_id in runs):
        ap.error("재현에 실패한 사례가 있습니다. 관측 로그를 확인하고 새 평가 폴더에서 다시 실행하세요")

    mode = "REAL" if args.real else "MOCK"
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    batch = ART / "solar" / f"{stamp}-{mode}"
    rows = []
    for run_id in runs:
        row = report_row(diagnose(run_id, batch / run_id, api_key))
        rows.append(row)
        print(f"[diag] {row['case']} · 기대={row['expected']} · "
              f"규칙={row['rule_diagnosis']} · Solar={row['solar_diagnosis'] or '—'}")

    report = batch / "comparison.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({key: csv_value(value) for key, value in row.items()} for row in rows)

    for case in sorted({row["case"] for row in rows}):
        case_rows = [row for row in rows if row["case"] == case]
        total = len(case_rows)
        rule_correct = sum(row["rule_correct"] for row in case_rows)
        verified = sum(row["transfer_verified"] for row in case_rows)
        solar = [row for row in case_rows if row["solar_diagnosis"]]
        solar_correct = sum(row["solar_correct"] for row in solar)
        supported = sum(row["solar_evidence_supports"] for row in solar)
        line = f"[diag] {case} · 규칙 정답 {rule_correct}/{total} · 해시 일치 {verified}/{total}"
        if args.real:
            line += (f" · Solar 응답 {len(solar)}/{total} · 정답 {solar_correct}/{total}"
                     f" · 근거 지지 {supported}/{total}")
        print(line)

    prefix = "solar" if args.real else "rule"
    all_ok = all(row[f"{prefix}_correct"] and row[f"{prefix}_evidence_supports"] for row in rows)
    print(f"[diag] {mode} · overall: {'PASS' if all_ok else 'FAIL'}")
    print(f"[diag] 결과표: {report}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

