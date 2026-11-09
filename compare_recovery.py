#!/usr/bin/env python3
"""2차 반복 비교 실험: 처음부터 다시 받기(restart) vs 이어받기(resume).

같은 원본 파일·같은 장애 주입 위치·같은 타임아웃/재시도 설정으로 두 방식을
각각 새 서버 상태에서 실행하고, 로그에서 집계한 지표로 결과표를 출력한다.

    python compare_recovery.py [--size N] [--inject-at N] [--port P]

지표 정의 (모두 실제 로그에서 집계, 표에 하드코딩하지 않음):
- 재접속 요청 오프셋 : 첫 재접속(RECONNECTED) 이후 클라이언트가 보낸
                       REQUEST 프레임의 offset
- 복구 후 파일 데이터량: 첫 연결(conn_id 최솟값)을 제외한 서버 연결들의
                       bytes_sent 합 — 파일 본문만(헤더 제외), 재접속이
                       여러 번이면 모두 합산
- 복구 시간           : 클라이언트 첫 재접속 성공(RECONNECTED)부터 본문
                       수신 완료(TRANSFER_COMPLETED)까지. 같은 클라이언트
                       프로세스의 단조 시계(mono 필드)로 계산하므로 두
                       방식의 정의가 동일하다. 재접속 이후 식별값 확인과
                       후속 재시도 시간을 포함, 최종 해시 검증은 제외.
- SHA-256 일치        : 최종 수신 파일을 원본과 독립 재계산해 비교
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

from demo import (EXPECTED_FLOW, force_utf8_stdout, is_subsequence,
                  load_jsonl, make_test_file, wait_server_ready)
from ftprotocol import sha256_file

BASE = pathlib.Path(__file__).resolve().parent
ART = BASE / "compare_artifacts"
MODES = ("restart", "resume")  # 실행 순서


def run_mode(mode: str, port: int, test_file: pathlib.Path, inject_at: int,
             common: dict):
    """한 방식 실험: 새 서버(장애 주입 1회) + 클라이언트 1회 실행."""
    print(f"\n=== mode: {mode} ===")
    out_dir = ART / mode
    if out_dir.exists():
        shutil.rmtree(out_dir)  # 별도 출력 폴더를 항상 새로 시작
    out_dir.mkdir(parents=True)
    logs = out_dir / "logs"
    server_stdout = out_dir / "server_stdout.txt"

    server_cmd = [sys.executable, str(BASE / "ftserver.py"),
                  "--port", str(port), "--file", str(test_file),
                  "--log-dir", str(logs), "--inject-at", str(inject_at)]
    print(f"[compare] starting server on port {port} "
          f"(fault injection at byte {inject_at})")
    with server_stdout.open("w", encoding="utf-8") as sfh:
        proc = subprocess.Popen(server_cmd, stdout=sfh,
                                stderr=subprocess.STDOUT)
    try:
        wait_server_ready(proc, server_stdout, timeout=5.0)
        client_cmd = [sys.executable, str(BASE / "ftclient.py"),
                      "--port", str(port), "--file-id", test_file.name,
                      "--output", str(out_dir / "received.bin"),
                      "--log-dir", str(logs),
                      "--recovery-mode", mode,
                      "--timeout", str(common["timeout"]),
                      "--retries", str(common["retries"]),
                      "--retry-delay", str(common["retry_delay"])]
        print(f"[compare] running client (recovery-mode={mode}, "
              f"timeout={common['timeout']}s, retries={common['retries']})")
        client = subprocess.Popen(client_cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace")
        assert client.stdout is not None
        lines = []
        for raw in client.stdout:
            line = raw.rstrip("\n")
            print(line)
            lines.append(line)
        rc = client.wait()
        print(f"[compare] client exit code: {rc}")
        return rc, lines, logs
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        print("[compare] server stopped")


def collect_metrics(mode: str, test_file: pathlib.Path,
                    out_file: pathlib.Path, lines: list[str],
                    logs: pathlib.Path, rc: int) -> dict:
    m: dict = {"mode": mode}
    expected_size = test_file.stat().st_size
    m["size_ok"] = (rc == 0 and out_file.exists()
                    and out_file.stat().st_size == expected_size)
    m["sha256_ok"] = bool(m["size_ok"]
                          and sha256_file(out_file) == sha256_file(test_file))
    m["flow_ok"] = is_subsequence(EXPECTED_FLOW["fault"], lines)

    client = load_jsonl(logs / "client.jsonl")
    server = load_jsonl(logs / "server.jsonl")
    m["inject_count"] = len(load_jsonl(logs / "injection.jsonl"))

    requests = [r for r in client if r.get("event") == "REQUEST"]
    reconnects = [r for r in client if r.get("event") == "RECONNECTED"]
    completed = [r for r in client if r.get("event") == "TRANSFER_COMPLETED"]
    m["interrupted_saved"] = [r.get("saved_bytes") for r in client
                              if r.get("event") == "INTERRUPTED"]
    m["reconnect_offset"] = None
    m["recovery_ms"] = None
    if reconnects and completed:
        first_mono = reconnects[0]["mono"]
        after = [r for r in requests if r.get("mono", -1) >= first_mono]
        if after:
            m["reconnect_offset"] = after[0].get("offset")
        m["recovery_ms"] = round(
            (completed[-1]["mono"] - first_mono) * 1000.0, 3)

    # 첫 연결 = 서버가 REQUEST 를 받은 연결 중 conn_id 최솟값
    conn_ids = [r["conn_id"] for r in server if r.get("event") == "REQUEST"]
    first_conn = min(conn_ids) if conn_ids else None
    m["recovered_bytes"] = sum(
        r.get("bytes_sent", 0) for r in server
        if r.get("event") == "CONNECTION_CLOSED"
        and r.get("conn_id") != first_conn)
    m["expected_bytes"] = (expected_size - m["reconnect_offset"]
                           if isinstance(m["reconnect_offset"], int)
                           else None)
    return m


def check_metrics(m: dict, inject_at: int) -> list[tuple]:
    checks: list[tuple[bool, str, str]] = []

    def add(ok: bool, label: str, detail: str) -> None:
        checks.append((bool(ok), label, detail))

    add(m["size_ok"], "크기 일치", f"size_ok={m['size_ok']}")
    add(m["sha256_ok"], "SHA-256 일치", f"sha256_ok={m['sha256_ok']}")
    add(m["flow_ok"], "출력 흐름", " -> ".join(EXPECTED_FLOW["fault"]))
    add(m["inject_count"] == 1, "주입 정확히 1회",
        f"{m['inject_count']}건 (injection.jsonl)")
    if m["mode"] == "restart":
        add(m["reconnect_offset"] == 0, "재접속 offset == 0",
            f"offset={m['reconnect_offset']}")
    else:
        # 실제 저장 위치가 주입 위치와 다르면 둘 다 기록한다 (실패 아님)
        add(isinstance(m["reconnect_offset"], int)
            and m["reconnect_offset"] > 0, "재접속 offset > 0",
            f"offset={m['reconnect_offset']}, inject_at={inject_at}, "
            f"중단 시 저장={m['interrupted_saved']}")
    add(m["expected_bytes"] is not None
        and m["recovered_bytes"] == m["expected_bytes"],
        "복구 후 데이터량 = 기대치(크기-offset)",
        f"집계 {m['recovered_bytes']} / 기대 {m['expected_bytes']}")
    add(isinstance(m["recovery_ms"], (int, float)) and m["recovery_ms"] >= 0,
        "복구 시간 측정", f"{m['recovery_ms']} ms")
    return checks


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="2차 반복: restart vs resume 복구 방식 비교 실험")
    ap.add_argument("--size", type=int, default=4 << 20,
                    help="테스트 파일 크기(바이트, 기본 4 MiB)")
    ap.add_argument("--seed", type=int, default=20261109)
    ap.add_argument("--inject-at", type=int, default=None,
                    help="장애 주입 위치(바이트, 기본: size // 3)")
    ap.add_argument("--port", type=int, default=9270,
                    help="기본 포트 (resume 은 +1 사용)")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--retry-delay", type=float, default=0.5)
    args = ap.parse_args()

    inject_at = args.inject_at if args.inject_at is not None \
        else args.size // 3
    if not 0 < inject_at < args.size:
        print(f"[compare] --inject-at({inject_at}) 은 0 < N < {args.size} "
              f"이어야 합니다", file=sys.stderr)
        return 2

    ART.mkdir(exist_ok=True)
    test_file = ART / "testfile.bin"
    print(f"[compare] creating test file: {test_file} ({args.size} bytes, "
          f"seed {args.seed})")
    make_test_file(test_file, args.size, args.seed)
    print(f"[compare] test file sha256: {sha256_file(test_file)}")

    common = {"timeout": args.timeout, "retries": args.retries,
              "retry_delay": args.retry_delay}
    results = {}
    for i, mode in enumerate(MODES):
        rc, lines, logs = run_mode(mode, args.port + i, test_file,
                                   inject_at, common)
        m = collect_metrics(mode, test_file, ART / mode / "received.bin",
                            lines, logs, rc)
        m["client_exit"] = rc
        m["checks"] = check_metrics(m, inject_at)
        results[mode] = m

    print("\n=== comparison (aggregated from logs) ===")
    print(f"{'mode':<8} {'inject_at':>10} {'offset':>8} "
          f"{'recovered_B':>12} {'expected_B':>12} {'recovery_ms':>12} "
          f"{'sha256':>7}")
    for mode in MODES:
        m = results[mode]
        print(f"{mode:<8} {inject_at:>10} {str(m['reconnect_offset']):>8} "
              f"{m['recovered_bytes']:>12} {str(m['expected_bytes']):>12} "
              f"{str(m['recovery_ms']):>12} "
              f"{'match' if m['sha256_ok'] else 'DIFF':>7}")

    print("\n=== verification ===")
    all_ok = True
    for mode in MODES:
        ok = all(c[0] for c in results[mode]["checks"])
        all_ok = all_ok and ok
        print(f"{mode}: {'PASS' if ok else 'FAIL'}")
        for c_ok, label, detail in results[mode]["checks"]:
            print(f"  [{'ok' if c_ok else 'FAIL':>4}] {label}: {detail}")

    # 전송량 감소와 복구 시간 개선은 별개로 보고한다.
    r, s = results["restart"], results["resume"]
    if isinstance(r["recovered_bytes"], int) \
            and isinstance(s["recovered_bytes"], int):
        print(f"\n[compare] 복구 후 전송량(실측): resume {s['recovered_bytes']} B "
              f"vs restart {r['recovered_bytes']} B "
              f"→ resume 이 {r['recovered_bytes'] - s['recovered_bytes']} B 적음")
    if isinstance(r["recovery_ms"], (int, float)) \
            and isinstance(s["recovery_ms"], (int, float)):
        if s["recovery_ms"] < r["recovery_ms"]:
            verdict = "resume 이 빠름"
        elif r["recovery_ms"] < s["recovery_ms"]:
            verdict = "restart 가 빠름"
        else:
            verdict = "동일"
        print(f"[compare] 복구 시간(실측): resume {s['recovery_ms']} ms vs "
              f"restart {r['recovery_ms']} ms → {verdict}")

    summary_path = ART / "summary.json"
    summary_path.write_text(
        json.dumps({"file_size": args.size,
                    "file_sha256": sha256_file(test_file),
                    "inject_at": inject_at,
                    "client_settings": common,
                    "results": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[compare] summary saved: {summary_path}")
    print(f"[compare] overall: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
