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

패킷 캡처: 시작 전 도구를 감지해 run_meta 에 기록한다. 이 환경
(Windows, 관리자 권한 없음, tshark/tcpdump 미설치)에서는 캡처가 불가하므로
capture.available=false 로 남고, 패킷 소스 증거는 생성되지 않는다.
캡처 시도 절차는 README 의 '패킷 캡처 · 손실 실험 미검증' 절 참조.

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
import shutil
import subprocess
import sys
import time

from demo import force_utf8_stdout, load_jsonl, make_test_file, wait_server_ready
from ftprotocol import sha256_file

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
    "RECONNECTED": ("attempt",),
    "REQUEST": ("offset",),
    "TRANSFER_COMPLETED": ("bytes",),
    # SHA256_OK → 해시 값 자체는 노출하지 않고 '검증 통과' 사실만
    "SHA256_OK": ("bytes",),
}
# insufficient 사례에서만 남길 이벤트 (나머지는 관측되지 않은 것으로)
INSUFFICIENT_KEEP = {"ACCEPTED", "TRANSFER_START", "SEND_START"}


def detect_capture_tools() -> dict:
    """캡처 도구 감지. 설치/권한 상승은 하지 않고 사실만 기록한다."""
    import shutil as _sh

    detected = []
    for tool in ("tshark", "tcpdump", "windump", "dumpcap"):
        if _sh.which(tool):
            detected.append(tool)
    admin = False
    try:  # net session 은 관리자 권한에서만 성공
        r = subprocess.run(["net", "session"], capture_output=True, timeout=5)
        admin = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    if _sh.which("pktmon"):
        detected.append("pktmon(내장, 관리자 권한 필요)")
    reasons = []
    if not detected:
        reasons.append("캡처 도구 없음(tshark/tcpdump/Npcap 미설치)")
    if "pktmon" in detected and not admin:
        reasons.append("pktmon 은 있으나 관리자 권한 없음")
    available = any(t in detected for t in ("tshark", "tcpdump", "windump",
                                            "dumpcap"))
    return {"tools_detected": detected, "admin": admin,
            "available": available,
            "reason": None if available else "; ".join(reasons)
                      or "권한 부족"}


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
        description="3차: 사례 재현 + 진단용 증거 생성 (패킷 캡처는 감지만)")
    ap.add_argument("--case", choices=["normal", "early_close",
                                        "insufficient", "all"],
                    default="all")
    ap.add_argument("--size", type=int, default=4 << 20)
    ap.add_argument("--seed", type=int, default=20261109)
    ap.add_argument("--port", type=int, default=9280,
                    help="기본 포트 (케이스마다 +1)")
    args = ap.parse_args()

    cases = (["normal", "early_close", "insufficient"]
             if args.case == "all" else [args.case])
    ART.mkdir(exist_ok=True)
    test_file = ART / "testfile.bin"
    print(f"[observe] creating test file: {test_file} ({args.size} bytes)")
    make_test_file(test_file, args.size, args.seed)

    capture = detect_capture_tools()
    print(f"[observe] capture tools: {capture['tools_detected'] or '없음'} "
          f"/ admin={capture['admin']} → available={capture['available']}")

    manifest = []
    for i, case in enumerate(cases):
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        run_id = f"run-{ts}-{i + 1:02d}"  # 케이스명 미포함(정답 노출 방지)
        out_dir = ART / run_id
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        print(f"[observe] case={case} → {run_id} (port {args.port + i})")
        result = run_experiment(case, args.port + i, test_file, out_dir,
                                args.size)
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
        manifest.append({"run_id": run_id, "case": case})

    if capture["available"]:
        print("[observe] 주의: 캡처 도구가 감지되었지만 이 스크립트는 아직 "
              "캡처를 수행하지 않는다 (README 절차 참조)")
    print("[observe] 완료. 진단: python solar_diag.py --all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
