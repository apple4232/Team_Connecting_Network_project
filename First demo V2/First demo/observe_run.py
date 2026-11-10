#!/usr/bin/env python3
"""3차 반복 · 관측 실행: 실험 재현 + 진단용 증거(evidence) 생성.

기존 ftserver/ftclient 를 subprocess 로 실행해 사례를 재현하고(복구 로직은
기존 Python 구현을 그대로 사용 — Solar 와 무관), 서버·클라이언트 관측
로그에서 진단에 필요한 항목만 골라 evidence.jsonl 로 남긴다.

사례 종류 (--case):
  normal       장애 없는 전송            → 기대: no_failure_observed
  early_close  서버가 전송 중 연결 차단  → 기대: early_connection_close
  insufficient early_close 재현 후 증거를 일부만 노출 (전송 시작·수락만)
                                          → 기대: insufficient_evidence + 보류

--capture-interface 로 캡처를 요청하면 로컬 TCP 패킷을 저장하고 요약한다.
도구 발견, 캡처 결과, 패킷 요약 상태를 run_meta 에 각각 기록한다.

출력: diag_artifacts/run-<ts>-<case>/{run_meta.json, evidence.jsonl, logs/}

정답 노출 방지: evidence 는 아래 화이트리스트 이벤트의 승인 필드만 담는다.
파일 경로, 파일 식별자, SHA-256 값, 장애 주입 설정/기록(injection.jsonl),
케이스 이름은 증거에 포함되지 않는다.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import subprocess
import sys
import time

from demo import force_utf8_stdout, load_jsonl, make_test_file, wait_server_ready
from ftprotocol import sha256_file
from packet_capture import capture_packets, find_capture_tools

BASE = pathlib.Path(__file__).resolve().parent
ART = BASE / "diag_artifacts"
LOOPBACK = "127.0.0.1"

# evidence 로 노출 허용된 이벤트와 필드 (화이트리스트)
SERVER_FIELDS = {
    "ACCEPTED": ("conn_id",),
    "REQUEST": ("conn_id", "offset"),
    "SEND_START": ("conn_id", "offset", "remaining"),
    "CONNECTION_CLOSED": ("conn_id", "by", "file_pos", "bytes_sent"),
    "REQUEST_FAILED": ("conn_id",),
}
CLIENT_FIELDS = {
    "TRANSFER_START": ("expected_size",),
    "INTERRUPTED": ("reason", "saved_bytes", "expected_size"),
    "ATTEMPT_FAILED": ("attempt", "phase", "saved_bytes"),
    "RECONNECTED": ("attempt",),
    "REQUEST": ("offset",),
    "TRANSFER_COMPLETED": ("bytes",),
    # SHA256_OK → 해시 값 자체는 노출하지 않고 '검증 통과' 사실만
    "SHA256_OK": ("bytes",),
}
# insufficient 사례에서만 남길 이벤트 (나머지는 관측되지 않은 것으로)
INSUFFICIENT_KEEP = {"ACCEPTED", "TRANSFER_START", "SEND_START"}


def capture_interface(value: str) -> str:
    if value in ("lo", "lo0", r"\Device\NPF_Loopback"):
        return value
    if value.isascii() and value.isdecimal() and int(value) > 0:
        return value
    raise argparse.ArgumentTypeError("로컬 루프백 이름(lo, lo0, NPF_Loopback)이나 인터페이스 번호를 입력하세요")


def run_experiment(case: str, port: int, test_file: pathlib.Path,
                   out_dir: pathlib.Path, size: int) -> dict:
    """케이스 1회 재현: 서버(+주입) → 클라이언트(resume). 로그 원본 보관."""
    logs = out_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    server_stdout = out_dir / "server_stdout.txt"
    inject_at = size // 3 if case in ("early_close", "insufficient") else None

    cmd = [sys.executable, str(BASE / "ftserver.py"),
           "--port", str(port), "--file", str(test_file),
           "--log-dir", str(logs)]
    if inject_at is not None:
        cmd += ["--inject-at", str(inject_at)]
    with server_stdout.open("w", encoding="utf-8") as sfh:
        proc = subprocess.Popen(cmd, stdout=sfh, stderr=subprocess.STDOUT)
    try:
        wait_server_ready(proc, server_stdout, timeout=5.0)
        client_cmd = [sys.executable, str(BASE / "ftclient.py"),
                      "--port", str(port), "--file-id", test_file.name,
                      "--output", str(out_dir / "received.bin"),
                      "--log-dir", str(logs), "--recovery-mode", "resume"]
        client = subprocess.Popen(client_cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace")
        assert client.stdout is not None
        for _ in client.stdout:
            pass  # 터미널 출력은 하지 않고 로그로 충분
        client_exit = client.wait()
        # 서버 종료 전 전송량 집계에 필요한 CONNECTION_CLOSED 로그 대기
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and proc.poll() is None:
            recs = load_jsonl(logs / "server.jsonl")
            requested = {r.get("conn_id") for r in recs
                         if r.get("event") == "REQUEST"}
            closed = {r.get("conn_id") for r in recs
                      if r.get("event") == "CONNECTION_CLOSED"}
            if requested and requested <= closed:
                break
            time.sleep(0.02)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    received = out_dir / "received.bin"
    recovered = client_exit == 0 and received.exists()
    return {
        "client_exit": client_exit,
        "recovered": bool(recovered),
        "final_hash_match": bool(recovered
                                 and sha256_file(received) == sha256_file(test_file)),
        "final_size_match": bool(recovered
                                 and received.stat().st_size == size),
    }


def build_evidence(out_dir: pathlib.Path, case: str) -> int:
    """양쪽 관측 로그를 공통 벽시계(ts) 타임라인의 evidence.jsonl 로 정리."""
    rows = []
    for rec in load_jsonl(out_dir / "logs" / "server.jsonl"):
        ev = rec.get("event")
        if ev in SERVER_FIELDS:
            row = {"source": "server_app", "ts": rec.get("ts"),
                   "event": ev}
            row.update({k: rec[k] for k in SERVER_FIELDS[ev] if k in rec})
            rows.append(row)
    for rec in load_jsonl(out_dir / "logs" / "client.jsonl"):
        ev = rec.get("event")
        if ev in CLIENT_FIELDS:
            row = {"source": "client_app", "ts": rec.get("ts"),
                   "event": ev}
            row.update({k: rec[k] for k in CLIENT_FIELDS[ev] if k in rec})
            rows.append(row)
    rows.sort(key=lambda r: (r["ts"] or "", r["source"]))
    kept = 0
    with (out_dir / "evidence.jsonl").open("w", encoding="utf-8") as fh:
        for i, row in enumerate(rows, 1):
            if case == "insufficient" \
                    and row["event"] not in INSUFFICIENT_KEEP:
                continue  # 이 사례는 관측이 부족한 상황을 재현한다
            rec = {"evidence_id": f"E{i:03d}", **row}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            kept += 1
    return kept


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="3차: 사례 재현 + 진단용 증거 생성 + 선택적 패킷 캡처")
    ap.add_argument("--case", choices=["normal", "early_close",
                                        "insufficient", "all"],
                    default="all")
    ap.add_argument("--size", type=int, default=4 << 20)
    ap.add_argument("--seed", type=int, default=20261109)
    ap.add_argument("--port", type=int, default=9280,
                    help="기본 포트 (케이스마다 +1)")
    ap.add_argument("--capture-interface", type=capture_interface,
                    help="Dumpcap 루프백 인터페이스 (지정할 때만 캡처)")
    ap.add_argument("--capture-seconds", type=int, default=5,
                    help="케이스별 최대 캡처 시간(초, 기본 5)")
    args = ap.parse_args()

    cases = (["normal", "early_close", "insufficient"]
             if args.case == "all" else [args.case])
    if not 1 <= args.port <= 65536 - len(cases):
        ap.error("--port 와 케이스별 후속 포트는 1~65535 범위여야 합니다")
    if not 1 <= args.capture_seconds <= 60:
        ap.error("--capture-seconds 는 1~60 사이여야 합니다")
    ART.mkdir(exist_ok=True)
    test_file = ART / "testfile.bin"
    print(f"[observe] creating test file: {test_file} ({args.size} bytes)")
    make_test_file(test_file, args.size, args.seed)

    tools = find_capture_tools()

    all_ok = True
    for i, case in enumerate(cases):
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
        run_id = f"run-{ts}-{i + 1:02d}"  # 케이스명 미포함(정답 노출 방지)
        out_dir = ART / run_id
        out_dir.mkdir(parents=True)
        print(f"[observe] case={case} → {run_id} (port {args.port + i})")
        port = args.port + i
        with capture_packets(out_dir, args.capture_interface, port,
                             args.capture_seconds, tools,
                             withheld=case == "insufficient") as capture:
            result = run_experiment(case, port, test_file, out_dir, args.size)
        n_ev = build_evidence(out_dir, case)
        meta = {
            "run_id": run_id, "created": ts, "case": case,
            "capture": capture, "evidence_count": n_ev, **result,
            "time_basis": "벽시계 ISO-8601(ts) — 서버·클라이언트 같은 호스트 시계",
            "observation_points": ["server_app", "client_app"],
        }
        (out_dir / "run_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[observe]   client_exit={result['client_exit']} "
              f"recovered={result['recovered']} "
              f"hash_match={result['final_hash_match']} "
              f"evidence={n_ev}건")
        print(f"[observe]   capture={capture['capture_status']} "
              f"summary={capture['summary_status']} available={capture['available']}"
              + (f" ({capture['reason']})" if capture["reason"] else ""))
        all_ok = all_ok and result["final_size_match"] and result["final_hash_match"]
        if args.capture_interface and case != "insufficient":
            all_ok = all_ok and capture["available"]

    print("[observe] 완료. 진단: python solar_diag.py --all")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
