#!/usr/bin/env python3
"""로컬 파일 전송 서버 (127.0.0.1 전용).

- 하나의 파일을 서빙하며 요청의 offset 위치부터 데이터를 스트리밍한다.
- 데모 옵션 --inject-at N: 첫 연결에서 파일 위치 N 바이트에 도달하면
  해당 연결을 한 번만 끊는다. 서버는 계속 실행되어 새 연결을 받는다.
- 관측 로그(server.jsonl)와 장애 주입 기록(injection.jsonl)을 분리해
  남긴다. 관측 로그에는 주입 의도를 남기지 않는다(injection.jsonl 은
  Solar 진단 채점용으로 팀이 별도 보관).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import socket
import sys

from ftprotocol import (
    CHUNK_SIZE,
    ConnectionClosed,
    JsonlLog,
    ProtocolError,
    now_iso,
    recv_frame,
    send_frame,
    sha256_file,
)

LOOPBACK = "127.0.0.1"
PROGRESS_INTERVAL = 1 << 20  # SEND_PROGRESS 로그 간격 (바이트)


def force_utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def send_error(conn: socket.socket, obs: JsonlLog, conn_id: int,
               code: str, message: str) -> None:
    obs.write("RESPONSE_ERROR", conn_id=conn_id, code=code, message=message)
    try:
        send_frame(conn, {"status": "error", "code": code, "message": message})
    except OSError:
        pass  # 오류 응답 전달 실패는 연결 종료로 처리된다


def handle_connection(conn: socket.socket, conn_id: int,
                      file_path: pathlib.Path, file_id: str,
                      size: int, digest: str, inject_at: int | None,
                      state: dict, obs: JsonlLog, inj: JsonlLog,
                      request_timeout: float) -> None:
    conn.settimeout(request_timeout)
    try:
        req = recv_frame(conn)
    except (ConnectionClosed, ProtocolError, socket.timeout, OSError) as exc:
        obs.write("REQUEST_FAILED", conn_id=conn_id, error=repr(exc))
        return

    req_id = req.get("file_id")
    offset = req.get("offset")
    if req_id != file_id:
        send_error(conn, obs, conn_id, "FILE_NOT_FOUND",
                   f"unknown file_id: {req_id!r} (serving {file_id!r})")
        return
    if not isinstance(offset, int) or isinstance(offset, bool) \
            or offset < 0 or offset > size:
        send_error(conn, obs, conn_id, "INVALID_OFFSET",
                   f"offset {offset!r} not in [0, {size}]")
        return

    obs.write("REQUEST", conn_id=conn_id, file_id=file_id, offset=offset)
    try:
        send_frame(conn, {"status": "ok", "file_id": file_id, "offset": offset,
                          "size": size, "sha256": digest})
    except OSError as exc:
        obs.write("CONNECTION_CLOSED", conn_id=conn_id, by="client",
                  file_pos=offset, bytes_sent=0, error=repr(exc))
        return
    obs.write("SEND_START", conn_id=conn_id, offset=offset,
              remaining=size - offset)

    # 장애 주입: 아직 한 번도 사용하지 않았고 이 연결이 inject_at 지점을
    # 가로지를 때만, 정확히 inject_at 위치에서 연결을 끊는다.
    will_cut = (inject_at is not None and not state["injection_used"]
                and offset < inject_at)

    pos = offset
    sent_conn = 0
    last_progress = pos
    conn.settimeout(None)
    try:
        with file_path.open("rb", buffering=0) as fh:
            fh.seek(offset)
            while pos < size:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:  # 서빙 중 파일이 줄어든 경우
                    obs.write("FILE_TRUNCATED", conn_id=conn_id, file_pos=pos)
                    break
                if will_cut and pos + len(chunk) > inject_at:
                    chunk = chunk[: inject_at - pos]  # 정확히 inject_at 까지만
                conn.sendall(chunk)
                pos += len(chunk)
                sent_conn += len(chunk)
                if pos - last_progress >= PROGRESS_INTERVAL or pos == size:
                    obs.write("SEND_PROGRESS", conn_id=conn_id,
                              file_pos=pos, bytes_sent=sent_conn)
                    last_progress = pos
                if will_cut and pos >= inject_at:
                    state["injection_used"] = True
                    inj.write("INJECT", conn_id=conn_id, inject_at=inject_at,
                              file_pos=pos, file_id=file_id, file_size=size,
                              note="cut this connection once at inject_at")
                    obs.write("CONNECTION_CLOSED", conn_id=conn_id,
                              by="server", file_pos=pos, bytes_sent=sent_conn)
                    return  # finally 에서 close
    except OSError as exc:  # 클라이언트가 먼저 연결을 끊은 경우
        obs.write("CONNECTION_CLOSED", conn_id=conn_id, by="client",
                  file_pos=pos, bytes_sent=sent_conn, error=repr(exc))
        return
    obs.write("CONNECTION_CLOSED", conn_id=conn_id, by="server",
              file_pos=pos, bytes_sent=sent_conn)


def main() -> int:
    force_utf8_stdout()
    ap = argparse.ArgumentParser(description="로컬 파일 전송 서버 (127.0.0.1 전용)")
    ap.add_argument("--host", default=LOOPBACK,
                    help=f"리슨 주소 ({LOOPBACK} 만 허용, 기본값)")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--file", type=pathlib.Path, required=True,
                    help="서빙할 파일 경로")
    ap.add_argument("--inject-at", type=int, default=None, metavar="N",
                    help="장애 주입: 첫 연결이 파일 위치 N 바이트에 도달하면 "
                         "해당 연결을 한 번만 끊는다")
    ap.add_argument("--log-dir", type=pathlib.Path, default=pathlib.Path("logs"))
    ap.add_argument("--request-timeout", type=float, default=10.0,
                    help="요청 헤더 수신 대기 시간(초)")
    args = ap.parse_args()

    if args.host != LOOPBACK:
        ap.error(f"--host 는 {LOOPBACK} 만 허용합니다")

    file_path = args.file.resolve()
    if not file_path.is_file():
        print(f"[server] file not found: {file_path}", file=sys.stderr)
        return 2
    size = file_path.stat().st_size
    digest = sha256_file(file_path)
    file_id = file_path.name

    if args.inject_at is not None and not 0 < args.inject_at < size:
        print(f"[server] --inject-at 은 0 < N < 파일크기({size}) 이어야 합니다",
              file=sys.stderr)
        return 2

    log_dir = args.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    obs = JsonlLog(log_dir / "server.jsonl", "server", "observation")
    inj = JsonlLog(log_dir / "injection.jsonl", "server", "injection")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(8)

    state = {"injection_used": False, "conn_seq": 0}
    obs.write("SERVER_START", file=str(file_path), file_id=file_id,
              size=size, sha256=digest, port=args.port, pid=os.getpid())
    print(f"[server] listening on {args.host}:{args.port} "
          f"file_id={file_id} size={size} sha256={digest}", flush=True)
    if args.inject_at is not None:
        # 주입 설정·기록은 관측 로그가 아닌 injection.jsonl 에만 남긴다.
        inj.path.touch()
        print(f"[server] fault injection armed: cut first connection at "
              f"byte {args.inject_at} (once)", flush=True)

    try:
        while True:
            conn, addr = srv.accept()
            state["conn_seq"] += 1
            conn_id = state["conn_seq"]
            obs.write("ACCEPTED", conn_id=conn_id, peer=f"{addr[0]}:{addr[1]}")
            try:
                handle_connection(conn, conn_id, file_path, file_id, size,
                                  digest, args.inject_at, state, obs, inj,
                                  args.request_timeout)
            except Exception as exc:  # 연결 단위 오류는 서버 전체를 죽이지 않는다
                obs.write("HANDLER_ERROR", conn_id=conn_id, error=repr(exc))
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        obs.write("SERVER_STOP")
        print("[server] stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
