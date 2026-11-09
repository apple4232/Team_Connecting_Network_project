#!/usr/bin/env python3
"""로컬 파일 수신 클라이언트 (127.0.0.1 전용).

- 파일을 <output>.part 임시 파일로 받고, 완료 검증(크기 + SHA-256)을
  통과해야 임시 파일을 최종 출력 파일로 바꾼다.
- 장애 감지: 예상 크기에 도달하기 전에 연결이 닫히거나(recv == b"")
  수신 시간이 초과되면(socket.timeout) 전송 중단으로 기록한다.
- 자동 복구: 제한된 횟수 안에서 재접속하며, 재개 위치는 임시 파일에
  실제로 저장된 바이트 수(디스크에서 다시 읽음)를 사용한다.
  재접속마다 파일 식별값(SHA-256)을 다시 확인해 달라지면 종료한다.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import socket
import sys
import time

from ftprotocol import (
    CHUNK_SIZE,
    JsonlLog,
    ProtocolError,
    recv_frame,
    send_frame,
    sha256_file,
)

LOOPBACK = "127.0.0.1"


def force_utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def transfer(out: pathlib.Path, temp: pathlib.Path, log: JsonlLog,
             args: argparse.Namespace) -> int:
    meta: dict | None = None          # 첫 연결의 응답 (size/sha256 기준)
    retries_left = args.retries
    is_retry = False
    expected = -1
    interrupt_reason: str | None = None

    while True:
        # 재개 위치 = 임시 파일에 실제로 저장된 바이트 수.
        # restart 방식의 재접속에서는 처음부터(offset 0) 다시 요청한다.
        if is_retry and args.recovery_mode == "restart":
            offset = 0
        else:
            offset = temp.stat().st_size if temp.exists() else 0
        interrupt_reason = None
        with socket.create_connection((args.host, args.port),
                                      timeout=args.timeout) as sock:
            if is_retry:
                print("Reconnected")
                log.write("RECONNECTED",
                          attempt=args.retries - retries_left)
            sock.settimeout(args.timeout)
            send_frame(sock, {"file_id": args.file_id, "offset": offset})
            log.write("REQUEST", file_id=args.file_id, offset=offset)

            resp = recv_frame(sock)
            if resp.get("status") != "ok":
                raise ProtocolError(
                    f"server error {resp.get('code')}: {resp.get('message')}")
            if meta is None:
                meta = resp
                expected = resp["size"]
                print("Transfer started")
                log.write("TRANSFER_START", offset=0,
                          expected_size=expected, sha256=resp["sha256"])
            else:
                if resp["sha256"] != meta["sha256"]:
                    raise ProtocolError(
                        "file identity changed between connections: "
                        f"{meta['sha256']} -> {resp['sha256']}")
                if resp["size"] != meta["size"]:
                    raise ProtocolError(
                        f"file size changed: {meta['size']} -> {resp['size']}")
                print(f"Resuming from byte {offset}")
                log.write("RESUME_REQUEST", offset=offset,
                          remaining=resp["size"] - offset,
                          recovery_mode=args.recovery_mode)
            if resp["offset"] != offset:
                raise ProtocolError(
                    f"resume offset mismatch: requested {offset}, "
                    f"server confirmed {resp['offset']}")
            # restart 방식: 파일 식별값 확인이 끝난 뒤 .part 를 비우고
            # 처음부터 다시 받는다 (식별값이 다르면 삭제하지 않고 종료).
            if is_retry and args.recovery_mode == "restart" and temp.exists():
                discarded = temp.stat().st_size
                temp.unlink()
                log.write("RESTART_RESET", discarded_bytes=discarded,
                          recovery_mode="restart")

            remaining = expected - offset
            stored = 0
            with temp.open("ab", buffering=0) as fh:
                while stored < remaining:
                    try:
                        chunk = sock.recv(CHUNK_SIZE)
                    except socket.timeout:
                        interrupt_reason = "recv timeout"
                        break
                    if not chunk:
                        interrupt_reason = "connection closed"
                        break
                    room = remaining - stored
                    data = chunk if len(chunk) <= room else chunk[:room]
                    fh.write(data)
                    stored += len(data)

        if interrupt_reason is None:
            break  # 이 연결에서 수신 완료

        saved = temp.stat().st_size if temp.exists() else 0
        print("Connection interrupted")
        print(f"  reason: {interrupt_reason}, saved {saved} of {expected} bytes")
        log.write("INTERRUPTED", reason=interrupt_reason,
                  saved_bytes=saved, expected_size=expected)
        if retries_left <= 0:
            print(f"[client] 재접속 한도({args.retries}회) 초과: "
                  f"{interrupt_reason}. 임시 파일 유지: {temp}")
            log.write("GAVE_UP", reason=interrupt_reason, saved_bytes=saved)
            return 1
        retries_left -= 1
        time.sleep(args.retry_delay)
        is_retry = True

    # 완료 검증: 최종 크기와 SHA-256이 모두 일치해야 성공.
    # TRANSFER_COMPLETED 는 본문 수신 완료 시점(검증 전)에 기록해
    # 복구 시간 측정 기준으로 쓴다.
    final_size = temp.stat().st_size
    log.write("TRANSFER_COMPLETED", bytes=final_size)
    if final_size != expected:
        print(f"[client] 크기 불일치: 기대 {expected}, 실제 {final_size}")
        log.write("ERROR", code="SIZE_MISMATCH",
                  expected=expected, actual=final_size)
        return 1
    print("Transfer completed")

    actual_digest = sha256_file(temp)
    if actual_digest != meta["sha256"]:
        print(f"[client] SHA-256 불일치: 기대 {meta['sha256']}, "
              f"실제 {actual_digest}")
        log.write("SHA256_MISMATCH", expected=meta["sha256"],
                  actual=actual_digest)
        return 1
    print("SHA-256 verified")
    print(f"  sha256: {actual_digest}")
    log.write("SHA256_OK", sha256=actual_digest, bytes=final_size)

    os.replace(temp, out)
    print(f"[client] saved to {out}")
    log.write("CLIENT_DONE", output=str(out), bytes=final_size)
    return 0


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="로컬 파일 수신 클라이언트 (127.0.0.1 전용, 끊기면 이어받기)")
    ap.add_argument("--host", default=LOOPBACK,
                    help=f"접속 주소 ({LOOPBACK} 만 허용, 기본값)")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--file-id", required=True, help="요청할 파일 식별자(이름)")
    ap.add_argument("--output", type=pathlib.Path, required=True,
                    help="완료 시 저장할 출력 파일 경로")
    ap.add_argument("--log-dir", type=pathlib.Path, default=pathlib.Path("logs"))
    ap.add_argument("--recovery-mode", choices=["resume", "restart"],
                    default="resume",
                    help="재접속 시 복구 방식: resume=저장된 위치부터 이어받기"
                         "(기본), restart=.part 를 비우고 처음부터 다시 받기")
    ap.add_argument("--retries", type=int, default=3,
                    help="재접속 최대 시도 횟수 (기본 3)")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="수신 대기 시간 제한(초)")
    ap.add_argument("--retry-delay", type=float, default=0.5,
                    help="재접속 전 대기(초)")
    args = ap.parse_args()

    if args.host != LOOPBACK:
        ap.error(f"--host 는 {LOOPBACK} 만 허용합니다")

    out = args.output.resolve()
    temp = out.with_name(out.name + ".part")
    if out.exists():
        print(f"[client] 출력 파일이 이미 있습니다: {out} — 덮어쓰지 않고 종료합니다")
        return 3

    args.log_dir.mkdir(parents=True, exist_ok=True)
    log = JsonlLog(args.log_dir / "client.jsonl", "client", "observation")
    log.write("CLIENT_START", file_id=args.file_id, output=str(out),
              retries=args.retries, timeout=args.timeout,
              recovery_mode=args.recovery_mode)
    if temp.exists():
        temp.unlink()  # 이전 실행의 임시 파일 — 신원을 보증할 수 없으므로 초기화
        log.write("TEMP_RESET", path=str(temp),
                  reason="stale temp file from previous run")

    try:
        return transfer(out, temp, log, args)
    except ProtocolError as exc:
        print(f"[client] 프로토콜 오류로 종료: {exc}")
        log.write("ERROR", code="PROTOCOL", message=str(exc))
        return 2
    except OSError as exc:
        print(f"[client] 네트워크 오류로 종료: {exc}")
        log.write("ERROR", code="NETWORK", message=repr(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
