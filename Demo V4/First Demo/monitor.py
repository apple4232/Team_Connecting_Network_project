"""파일 수신 중 발생한 연결 장애를 감지하고 관측 로그를 남긴다."""
from __future__ import annotations

import socket
from dataclasses import dataclass

from ftprotocol import JsonlLog


@dataclass(frozen=True)
class TransferFailure:
    phase: str
    reason: str
    expected_saved_bytes: int | None = None


def receive_chunk(sock: socket.socket, limit: int) -> tuple[bytes, TransferFailure | None]:
    """본문 수신 실패를 진단 스크립트가 사용하는 사유로 표현한다."""
    try:
        chunk = sock.recv(limit)
    except socket.timeout:
        return b"", TransferFailure("body", "recv timeout")
    except ConnectionResetError:
        return b"", TransferFailure("body", "connection reset")
    except OSError:
        return b"", TransferFailure("body", "recv failed")
    if not chunk:
        return b"", TransferFailure("body", "connection closed")
    return chunk, None


def record_failure(log: JsonlLog, failure: TransferFailure, *, attempt: int,
                   saved_bytes: int, expected_size: int | None) -> None:
    if failure.phase == "body":
        print("Connection interrupted")
        print(f"  reason: {failure.reason}, saved {saved_bytes} of {expected_size} bytes")
        log.write("INTERRUPTED", reason=failure.reason, saved_bytes=saved_bytes,
                  expected_size=expected_size)
    else:
        print(f"[client] 연결 시도 실패 (단계: {failure.phase}): {failure.reason}")
        log.write("ATTEMPT_FAILED", attempt=attempt, phase=failure.phase,
                  reason=failure.reason, saved_bytes=saved_bytes)
