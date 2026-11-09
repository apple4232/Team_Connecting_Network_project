#!/usr/bin/env python3
"""전송 프로토콜 공통 부분 (서버/클라이언트 공유).

프레임 형식 (요청·응답 헤더 공용):

    [4바이트 big-endian 길이][UTF-8 JSON]

요청 프레임:      {"file_id": str, "offset": int}
응답 헤더 프레임: {"status": "ok", "file_id": str, "offset": int,
                   "size": int, "sha256": str}
오류 프레임:      {"status": "error", "code": str, "message": str}

응답 헤더 프레임 바로 뒤에는 구분자 없이 파일 데이터가 (size - offset)
바이트 이어져 도착한다. TCP는 메시지 경계를 보존하지 않으므로 헤더는
반드시 recv_exact()로 정확히 잘라 읽는다. 파일 데이터는 스트림으로
취급해 청크 단위로 파일에 저장한다.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import socket

HEADER_BYTES = 4          # 길이 필드 크기 (big-endian)
MAX_FRAME = 1 << 20       # 헤더 JSON 한 프레임 최대 크기 (1 MiB)
CHUNK_SIZE = 64 * 1024    # 파일 데이터 송수신 청크 크기


class ConnectionClosed(Exception):
    """스트림이 예상 바이트 수에 못 미쳐 닫혔다."""


class ProtocolError(Exception):
    """프로토콜 오류 또는 서버 오류 응답."""


def now_iso() -> str:
    """밀리초 단위 로컬 타임존 ISO-8601 타임스탬프."""
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def send_frame(sock: socket.socket, obj: dict) -> None:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"frame too large: {len(payload)} bytes")
    sock.sendall(len(payload).to_bytes(HEADER_BYTES, "big") + payload)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """정확히 n 바이트를 읽는다. recv() 한 번이 한 메시지라 가정하지 않는다."""
    buf = bytearray()
    while len(buf) < n:
        piece = sock.recv(min(CHUNK_SIZE, n - len(buf)))
        if not piece:
            raise ConnectionClosed(f"stream closed after {len(buf)}/{n} bytes")
        buf += piece
    return bytes(buf)


def recv_frame(sock: socket.socket) -> dict:
    raw_len = recv_exact(sock, HEADER_BYTES)
    length = int.from_bytes(raw_len, "big")
    if not 0 < length <= MAX_FRAME:
        raise ProtocolError(f"invalid frame length: {length}")
    payload = recv_exact(sock, length)
    return json.loads(payload.decode("utf-8"))


def sha256_file(path: pathlib.Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with pathlib.Path(path).open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


class JsonlLog:
    """한 줄에 JSON 레코드 하나를 append 하는 로거.

    모든 레코드는 ts(시간), actor(주체), class(관측/주입), event(종류)를
    기본 필드로 갖는다. 장애 주입 기록은 class="injection" 으로 구분해
    별도 파일에 남긴다.
    """

    def __init__(self, path: pathlib.Path, actor: str, klass: str):
        self.path = pathlib.Path(path)
        self.actor = actor
        self.klass = klass

    def write(self, event: str, **fields) -> None:
        rec = {
            "ts": now_iso(),
            "actor": self.actor,
            "class": self.klass,
            "event": event,
        }
        rec.update(fields)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
