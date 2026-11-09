#!/usr/bin/env python3
"""한 번의 명령으로 전체 데모를 실행한다.

테스트 파일 생성 → 서버 시작(필요시 장애 주입) → 클라이언트 실행 →
결과 검증 → 서버 정리. 기본적으로 '정상 전송'과 '장애 주입' 두 케이스를
연속 실행하고 성공 여부를 요약해 종료 코드로 알린다 (모두 통과 시 0).

실행 예:
    python demo.py                 # 두 케이스 모두
    python demo.py --case fault    # 장애 주입 케이스만
    python demo.py --size 8388608  # 테스트 파일 크기 변경 (기본 4 MiB)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import shutil
import subprocess
import sys
import time

from ftprotocol import sha256_file

BASE = pathlib.Path(__file__).resolve().parent
ART = BASE / "demo_artifacts"

# 각 케이스에서 터미널에 이 순서의 흐름이 나타나야 한다.
EXPECTED_FLOW = {
    "normal": ["Transfer started", "Transfer completed", "SHA-256 verified"],
    "fault": ["Transfer started", "Connection interrupted", "Reconnected",
              "Resuming from byte", "Transfer completed", "SHA-256 verified"],
}


def force_utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def rand_bytes(rng: random.Random, n: int) -> bytes:
    if hasattr(rng, "randbytes"):
        return rng.randbytes(n)
    return bytes(rng.getrandbits(8) for _ in range(n))  # Python < 3.9 대비


def make_test_file(path: pathlib.Path, size: int, seed: int) -> None:
    rng = random.Random(seed)
    with path.open("wb") as fh:
        left = size
        while left > 0:
            n = min(1 << 20, left)
            fh.write(rand_bytes(rng, n))
            left -= n


def wait_server_ready(proc: subprocess.Popen, stdout_path: pathlib.Path,
                      timeout: float) -> None:
    """서버 stdout 에 'listening' 줄이 나올 때까지 폴링한다.

    TCP probe 연결을 쓰지 않아 관측 로그에 불필요한 REQUEST_FAILED 가
    남지 않는다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"서버가 일찍 종료됨 (exit {proc.returncode}) — "
                f"{stdout_path} 확인")
        try:
            text = stdout_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "[server] listening" in text:
            return
        time.sleep(0.05)
    raise RuntimeError(f"서버가 {timeout}초 안에 준비되지 않음 ({stdout_path})")


def run_case(name: str, port: int, test_file: pathlib.Path,
             out_dir: pathlib.Path, inject_at: int | None):
    """서버 서브프로세스를 띄우고 클라이언트를 실행한다. 서버는 항상 정리."""
    print(f"\n=== case: {name} ===")
    if out_dir.exists():
        shutil.rmtree(out_dir)  # 이전 실행 결과로 client 가 exit 3 되지 않게 초기화
    out_dir.mkdir(parents=True)
    logs = out_dir / "logs"
    server_stdout = out_dir / "server_stdout.txt"

    server_cmd = [sys.executable, str(BASE / "ftserver.py"),
                  "--port", str(port), "--file", str(test_file),
                  "--log-dir", str(logs)]
    if inject_at is not None:
        server_cmd += ["--inject-at", str(inject_at)]
    print(f"[demo] starting server on port {port}"
          + (f" with fault injection at byte {inject_at}"
             if inject_at is not None else ""))

    with server_stdout.open("w", encoding="utf-8") as sfh:
        proc = subprocess.Popen(server_cmd, stdout=sfh,
                                stderr=subprocess.STDOUT)
    try:
        wait_server_ready(proc, server_stdout, timeout=5.0)
        client_cmd = [sys.executable, str(BASE / "ftclient.py"),
                      "--port", str(port), "--file-id", test_file.name,
                      "--output", str(out_dir / "received.bin"),
                      "--log-dir", str(logs)]
        print("[demo] running client")
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
        print(f"[demo] client exit code: {rc}")
        return rc, lines, logs
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        print(f"[demo] server stopped (stdout: {server_stdout.name}, "
              f"logs: {logs.name}/)")


def load_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def is_subsequence(tokens: list[str], lines: list[str]) -> bool:
    it = iter(lines)
    return all(any(l.startswith(t) for l in it) for t in tokens)


def verify_case(name: str, test_file: pathlib.Path, out_file: pathlib.Path,
                lines: list[str], logs: pathlib.Path, rc: int) -> list[tuple]:
    checks: list[tuple[bool, str, str]] = []

    def check(ok: bool, label: str, detail: str) -> None:
        checks.append((ok, label, detail))

    expected_size = test_file.stat().st_size
    expected_sha = sha256_file(test_file)

    if rc == 0 and out_file.exists():
        actual_size = out_file.stat().st_size
        actual_sha = sha256_file(out_file)
        check(actual_size == expected_size, "크기 일치",
              f"수신 {actual_size} / 원본 {expected_size} bytes")
        check(actual_sha == expected_sha, "SHA-256 일치",
              f"수신 {actual_sha} / 원본 {expected_sha}")
    else:
        check(False, "클라이언트 성공",
              f"exit code {rc}, 출력 파일 존재 {out_file.exists()}")

    check(is_subsequence(EXPECTED_FLOW[name], lines), "출력 흐름",
          " -> ".join(EXPECTED_FLOW[name]))

    client_log = load_jsonl(logs / "client.jsonl")
    resume_offsets = [r.get("offset") for r in client_log
                      if r.get("event") == "RESUME_REQUEST"]
    injection_records = load_jsonl(logs / "injection.jsonl")
    if name == "fault":
        check(any(isinstance(o, int) and o > 0 for o in resume_offsets),
              "재개 위치 > 0", f"재개 위치 목록: {resume_offsets}")
        check(len(injection_records) == 1, "주입 기록 정확히 1회",
              f"{len(injection_records)}건 (injection.jsonl)")
    else:
        check(resume_offsets == [], "재접속 없음", f"재개 위치 목록: {resume_offsets}")
        check(not injection_records, "주입 기록 없음",
              f"{len(injection_records)}건 (injection.jsonl)")
    return checks


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="1차 데모: 테스트 파일 생성 → 서버/클라이언트 실행 → 검증 → 정리")
    ap.add_argument("--case", choices=["all", "normal", "fault"],
                    default="all", help="실행할 케이스 (기본: all)")
    ap.add_argument("--size", type=int, default=4 << 20,
                    help="테스트 파일 크기(바이트, 기본 4 MiB)")
    ap.add_argument("--seed", type=int, default=20261109,
                    help="테스트 파일 생성 시드")
    ap.add_argument("--port", type=int, default=9230,
                    help="기본 포트 (fault 케이스는 +1 사용)")
    args = ap.parse_args()

    ART.mkdir(exist_ok=True)
    test_file = ART / "testfile.bin"
    inject_at = args.size // 3
    if args.case in ("all", "fault") and not 0 < inject_at < args.size:
        print(f"[demo] --size 가 너무 작아 장애 주입 위치({inject_at})가 "
              f"유효하지 않습니다. size > 3 이어야 합니다.", file=sys.stderr)
        return 2

    print(f"[demo] creating test file: {test_file} "
          f"({args.size} bytes, seed {args.seed})")
    make_test_file(test_file, args.size, args.seed)
    print(f"[demo] test file sha256: {sha256_file(test_file)}")

    cases = []
    if args.case in ("all", "normal"):
        cases.append(("normal", args.port, None))
    if args.case in ("all", "fault"):
        cases.append(("fault", args.port + 1, inject_at))

    results = {}
    for name, port, inj_at in cases:
        out_dir = ART / name
        rc, lines, logs = run_case(name, port, test_file, out_dir, inj_at)
        results[name] = verify_case(name, test_file, out_dir / "received.bin",
                                    lines, logs, rc)

    print("\n=== verification summary ===")
    all_ok = True
    for name, checks in results.items():
        ok = all(c[0] for c in checks)
        all_ok = all_ok and ok
        print(f"{name}: {'PASS' if ok else 'FAIL'}")
        for c_ok, label, detail in checks:
            print(f"  [{'ok' if c_ok else 'FAIL':>4}] {label}: {detail}")
    print(f"[demo] overall: {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
