#!/usr/bin/env python3
"""로컬 파일 수신 클라이언트 (127.0.0.1 전용).

- 파일을 <output>.part 임시 파일로 받고, 완료 검증(크기 + SHA-256)을
  통과해야 임시 파일을 최종 출력 파일로 바꾼다.
- 장애 감지: 예상 크기 전에 연결이 닫히거나 리셋되거나 수신이
  타임아웃되면 전송 중단으로 기록한다.
- 자동 복구: 제한된 횟수 안에서 재접속하며, 재개 위치는 임시 파일에
  실제로 저장된 바이트 수(디스크에서 다시 읽음)를 사용한다.
  접속이나 헤더 교환이 실패해도 남은 횟수 내에서 다시 시도한다.
  재접속마다 파일 식별값(SHA-256)을 다시 확인해 달라지면 종료한다.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import socket
import sys
from functools import partial

from ftprotocol import (
    CHUNK_SIZE,
    ConnectionClosed,
    JsonlLog,
    ProtocolError,
    recv_frame,
    send_frame,
    sha256_file,
)
from monitor import TransferFailure, receive_chunk
from recovery import receive_with_recovery, reset_for_restart
from recovery_policy import RECOVERY_MODES, STRATEGY_SOURCES, mock_proposal
from solar_recovery import solar_proposal

LOOPBACK = "127.0.0.1"


def force_utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def receive_attempt(temp: pathlib.Path, log: JsonlLog, args: argparse.Namespace,
                    meta: dict | None, offset: int, attempt: int, mode: str | None
                    ) -> tuple[dict | None, TransferFailure | None]:
    try:
        sock = socket.create_connection((args.host, args.port), timeout=args.timeout)
    except OSError as exc:
        return meta, TransferFailure("connect", str(exc))

    with sock:
        if attempt:
            print("Reconnected")
            log.write("RECONNECTED", attempt=attempt)
        try:
            send_frame(sock, {"file_id": args.file_id, "offset": offset})
        except OSError as exc:
            return meta, TransferFailure("request", str(exc))
        log.write("REQUEST", file_id=args.file_id, offset=offset)
        try:
            resp = recv_frame(sock)
        except (ConnectionClosed, OSError) as exc:
            return meta, TransferFailure("response_header", str(exc))
        if resp.get("status") != "ok":
            raise ProtocolError(
                f"server error {resp.get('code')}: {resp.get('message')}")
        if resp.get("file_id") != args.file_id:
            raise ProtocolError("response file_id does not match the requested file")
        confirmed_offset = resp.get("offset")
        if type(confirmed_offset) is not int or confirmed_offset != offset:
            raise ProtocolError(
                f"resume offset mismatch: requested {offset}, "
                f"server confirmed {confirmed_offset!r}")
        size, digest = resp.get("size"), resp.get("sha256")
        if type(size) is not int or size < offset:
            raise ProtocolError(f"invalid response size: {size!r}")
        if not isinstance(digest, str) or len(digest) != 64 or any(
                c not in "0123456789abcdef" for c in digest):
            raise ProtocolError("invalid response SHA-256")
        if meta is None:
            meta = resp
            print("Transfer started")
            log.write("TRANSFER_START", offset=0,
                      expected_size=meta["size"], sha256=meta["sha256"])
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
                      recovery_mode=mode)
        # 파일 식별값을 확인한 뒤에만 restart 모드의 .part를 비운다.
        reset_for_restart(temp, log, mode)

        remaining = meta["size"] - offset
        with temp.open("ab", buffering=0) as fh:
            while remaining > 0:
                chunk, failure = receive_chunk(sock, min(CHUNK_SIZE, remaining))
                if failure is not None:
                    return meta, TransferFailure(
                        failure.phase, failure.reason, expected_saved_bytes=size - remaining)
                if fh.write(chunk) != len(chunk):
                    raise OSError("임시 파일에 받은 데이터를 모두 기록하지 못했습니다")
                remaining -= len(chunk)
    return meta, None


def transfer(out: pathlib.Path, temp: pathlib.Path, log: JsonlLog,
             args: argparse.Namespace) -> int:
    strategy_source = getattr(args, "strategy_source", "fixed")
    advisor = {"mock": mock_proposal, "solar": partial(solar_proposal, log=log)}.get(strategy_source)
    meta = receive_with_recovery(
        temp, log, partial(receive_attempt, temp, log, args),
        recovery_mode=args.recovery_mode, retries=args.retries, retry_delay=args.retry_delay,
        strategy_source=strategy_source, advisor=advisor,
    )
    if meta is None:
        return 1

    # 완료 검증: 최종 크기와 SHA-256이 모두 일치해야 성공.
    # TRANSFER_COMPLETED 는 본문 수신 완료 시점(검증 전)에 기록해
    # 복구 시간 측정 기준으로 쓴다.
    expected = meta["size"]
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
    ap.add_argument("--recovery-mode", choices=RECOVERY_MODES,
                    default="resume",
                    help="기본 복구 방식: resume=저장된 위치부터 이어받기"
                         "(기본), restart=처음부터 다시 받기")
    ap.add_argument("--strategy-source", choices=STRATEGY_SOURCES, default="fixed",
                    help="복구 전략 제안 방식: fixed=기본 복구 설정, mock=모의 응답, solar=실제 Solar")
    ap.add_argument("--retries", type=int, default=3,
                    help="재접속 최대 시도 횟수 (기본 3)")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="수신 대기 시간 제한(초)")
    ap.add_argument("--retry-delay", type=float, default=0.5,
                    help="재접속 전 대기(초)")
    args = ap.parse_args()

    if args.host != LOOPBACK:
        ap.error(f"--host 는 {LOOPBACK} 만 허용합니다")
    if args.retries < 0:
        ap.error("--retries 는 0 이상이어야 합니다")

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
