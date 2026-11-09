#!/usr/bin/env python3
"""오류 경로 회귀 테스트: demo.py 가 커버하지 않는 실패 시나리오 검증.

실행: python test_error_paths.py   (모두 통과 시 exit 0)

검증 항목:
1. 출력 파일 중복 보호       → client exit 3, 기존 파일 유지
2. 재시도 한도 초과           → client exit 1, 실패 사유 출력, .part 유지
3. 파일 식별값 변경 감지      → client exit 2 (재시도하지 않고 종료)
4. 실제 서버 오류 응답        → INVALID_OFFSET / FILE_NOT_FOUND 프레임
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import socket

from ftprotocol import recv_frame, send_frame

BASE = pathlib.Path(__file__).resolve().parent
REAL_SERVER_PORT = 9261  # 테스트 4 전용 (서버는 테스트 중에만 살아 있음)


def force_utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)


class StubServer:
    """테스트용 로컬 서버. handler(conn, request) 가 연결 하나를 처리한다."""

    def __init__(self, handler):
        self.handler = handler
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))  # 임시 포트 자동 할당
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        self.srv.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                req = recv_frame(conn)
                self.handler(conn, req)
            except Exception:
                pass  # 연결 단위 오류는 무시 (클라이언트가 끊긴 것으로 관찰)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


def run_client(port: int, output: pathlib.Path) -> tuple[int, str]:
    cmd = [sys.executable, str(BASE / "ftclient.py"),
           "--port", str(port), "--file-id", "testfile.bin",
           "--output", str(output), "--log-dir", str(output.parent / "logs"),
           "--retry-delay", "0.05"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    return proc.returncode, (proc.stdout + proc.stderr)


def test_overwrite_protection(tmp: pathlib.Path):
    out = tmp / "exists.bin"
    out.write_bytes(b"existing-content")
    rc, text = run_client(9999, out)  # 서버 불필요 — 접속 전에 검사함
    ok = rc == 3 and "이미 있습니다" in text and out.read_bytes() == b"existing-content"
    return ok, f"exit={rc}, 기존 내용 유지={out.read_bytes() == b'existing-content'}"


def test_retry_exhaustion(tmp: pathlib.Path):
    def handler(conn, req):
        # 항상 size(4096)보다 적게 주고 연결을 끊는 불안정한 서버
        send_frame(conn, {"status": "ok", "file_id": req["file_id"],
                          "offset": req["offset"], "size": 4096,
                          "sha256": "00" * 32})
        n = max(0, 200 - req["offset"])
        if n:
            conn.sendall(b"x" * n)
        conn.close()

    srv = StubServer(handler)
    out = tmp / "retry_fail.bin"
    try:
        rc, text = run_client(srv.port, out)
        part = out.with_name(out.name + ".part")
        ok = (rc == 1
              and text.count("Connection interrupted") == 4  # 초기 + 재시도 3회
              and text.count("Reconnected") == 3
              and "재접속 한도" in text
              and part.exists() and part.stat().st_size == 200)
        return ok, (f"exit={rc}, interrupted={text.count('Connection interrupted')}, "
                    f"reconnected={text.count('Reconnected')}, "
                    f".part={part.stat().st_size if part.exists() else '없음'}바이트")
    finally:
        srv.stop()


def test_identity_change(tmp: pathlib.Path):
    state = {"conns": 0}

    def handler(conn, req):
        state["conns"] += 1
        sha = ("aa" * 32) if state["conns"] == 1 else ("bb" * 32)
        send_frame(conn, {"status": "ok", "file_id": req["file_id"],
                          "offset": req["offset"], "size": 4096,
                          "sha256": sha})
        conn.sendall(b"x" * 100)  # 첫 연결은 100바이트만 주고 끊김
        conn.close()

    srv = StubServer(handler)
    out = tmp / "identity.bin"
    try:
        rc, text = run_client(srv.port, out)
        ok = rc == 2 and "identity changed" in text
        return ok, f"exit={rc}, 식별값 변경 감지={'identity changed' in text}"
    finally:
        srv.stop()


def test_real_server_errors(tmp: pathlib.Path):
    src = tmp / "srv.bin"
    src.write_bytes(b"0123456789" * 100)  # 1000 바이트
    logdir = tmp / "srvlogs"
    proc = subprocess.Popen(
        [sys.executable, str(BASE / "ftserver.py"),
         "--port", str(REAL_SERVER_PORT), "--file", str(src),
         "--log-dir", str(logdir)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # SERVER_START 관측 로그가 남을 때까지 대기 (probe 연결 없음)
        deadline = time.monotonic() + 5.0
        started = False
        while time.monotonic() < deadline:
            f = logdir / "server.jsonl"
            if f.exists():
                lines = f.read_text(encoding="utf-8").strip().splitlines()
                if lines and json.loads(lines[0]).get("event") == "SERVER_START":
                    started = True
                    break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if not started:
            return False, "서버 기동 실패"

        codes = []
        with socket.create_connection(("127.0.0.1", REAL_SERVER_PORT),
                                      timeout=5) as s:
            send_frame(s, {"file_id": "srv.bin", "offset": 9999})
            codes.append(recv_frame(s).get("code"))
        with socket.create_connection(("127.0.0.1", REAL_SERVER_PORT),
                                      timeout=5) as s:
            send_frame(s, {"file_id": "nope.bin", "offset": 0})
            codes.append(recv_frame(s).get("code"))
        ok = codes == ["INVALID_OFFSET", "FILE_NOT_FOUND"]
        return ok, f"응답 코드={codes}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    force_utf8_stdout()
    tests = [
        ("출력 파일 중복 보호 (exit 3)", test_overwrite_protection),
        ("재시도 한도 초과 (exit 1, .part 유지)", test_retry_exhaustion),
        ("파일 식별값 변경 감지 (exit 2)", test_identity_change),
        ("서버 오류 응답 (INVALID_OFFSET/FILE_NOT_FOUND)", test_real_server_errors),
    ]
    all_ok = True
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        for name, fn in tests:
            ok, detail = fn(tmp)
            all_ok = all_ok and ok
            print(f"[{'ok' if ok else 'FAIL':>4}] {name}: {detail}")
    print("overall:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
