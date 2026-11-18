"""다운로드 재시도와 임시 파일의 재개 위치를 관리한다."""
from __future__ import annotations

import pathlib
import time
from collections.abc import Callable

from ftprotocol import JsonlLog
from monitor import TransferFailure, record_failure
from recovery_policy import (
    RecoveryContext,
    StrategyAdvisor,
    choose_advised_mode,
    choose_retry_mode,
)


ReceiveAttempt = Callable[
    [dict | None, int, int, str | None], tuple[dict | None, TransferFailure | None]
]


def saved_size(temp: pathlib.Path) -> int:
    return temp.stat().st_size if temp.exists() else 0


def reset_for_restart(temp: pathlib.Path, log: JsonlLog, mode: str | None) -> None:
    """클라이언트가 서버의 파일 식별값을 확인한 뒤 호출한다."""
    if mode == "restart" and temp.exists():
        discarded = temp.stat().st_size
        temp.unlink()
        log.write("RESTART_RESET", discarded_bytes=discarded, recovery_mode=mode)


def receive_with_recovery(temp: pathlib.Path, log: JsonlLog, receive_attempt: ReceiveAttempt,
                          *, recovery_mode: str, retries: int, retry_delay: float,
                          strategy_source: str = "fixed",
                          advisor: StrategyAdvisor | None = None) -> dict | None:
    """한 번의 수신을 반복해 완료 헤더를 돌려준다. 재시도 소진 시 None을 반환한다."""
    meta = None
    mode = None
    attempt = 0
    expected_saved = None
    advisor_used = False
    while True:
        offset = 0 if mode == "restart" else saved_size(temp)
        if (advisor is not None and mode == "resume" and expected_saved is not None
                and offset != expected_saved):
            log.write("RECOVERY_OVERRIDE", attempt=attempt, strategy="restart",
                      reason="제안 이후 임시 파일 길이 변경", saved_bytes=offset,
                      expected_saved_bytes=expected_saved)
            mode, offset = "restart", 0
        meta, failure = receive_attempt(meta, offset, attempt, mode)
        if failure is None:
            return meta

        saved = saved_size(temp)
        record_failure(log, failure, attempt=attempt, saved_bytes=saved,
                       expected_size=meta["size"] if meta is not None else None)
        mode = choose_retry_mode(recovery_mode, retries_left=retries - attempt)
        if mode is None:
            print(f"[client] 재접속 한도({retries}회) 초과: "
                  f"{failure.reason}. 저장된 임시 데이터 유지.")
            log.write("GAVE_UP", phase=failure.phase, reason=failure.reason, saved_bytes=saved)
            return None
        if advisor is not None:
            if failure.expected_saved_bytes is not None:
                expected_saved = failure.expected_saved_bytes
            context = RecoveryContext(
                symptom=failure.reason if failure.phase == "body" else failure.phase,
                saved_bytes=saved, expected_saved_bytes=expected_saved,
                expected_size=meta["size"] if meta is not None else None,
                retries_left=retries - attempt,
            )
            # 저장본을 재사용할지 처음부터 받을지 판단할 근거가 생겼을 때 한 번 제안받는다.
            ask = (not advisor_used and failure.phase == "body"
                   and (saved > 0 or context.partial_size_changed))
            advisor_used = advisor_used or ask
            decision = choose_advised_mode(context, mode, strategy_source, advisor if ask else None)
            mode = decision.strategy
            log.write("RECOVERY_DECISION", attempt=attempt + 1, strategy=mode,
                      source=decision.source, reason=decision.reason, saved_bytes=saved,
                      expected_saved_bytes=expected_saved, retries_left=context.retries_left)
        time.sleep(retry_delay)
        attempt += 1
