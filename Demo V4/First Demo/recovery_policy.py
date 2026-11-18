"""파일 전송에 허용하는 복구 전략과 재시도 한도를 정의한다."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

RECOVERY_MODES = ("resume", "restart")
STRATEGY_SOURCES = ("fixed", "mock", "solar")


@dataclass(frozen=True)
class RecoveryContext:
    symptom: str
    saved_bytes: int
    expected_saved_bytes: int | None
    expected_size: int | None
    retries_left: int

    @property
    def partial_size_changed(self) -> bool:
        return (self.expected_saved_bytes is not None
                and self.saved_bytes != self.expected_saved_bytes)


@dataclass(frozen=True)
class RecoveryDecision:
    strategy: str
    source: str
    reason: str


class AdviceUnavailable(Exception):
    """안전하게 로그에 남길 수 있는 제안 실패 사유."""


StrategyAdvisor = Callable[[RecoveryContext], str]


def choose_retry_mode(requested_mode: str, *, retries_left: int) -> str | None:
    """시도할 복구 전략을 검증한다. 한도를 소진하면 None을 반환한다."""
    if retries_left <= 0:
        return None
    if requested_mode not in RECOVERY_MODES:
        raise ValueError(f"허용되지 않은 복구 전략: {requested_mode!r}")
    return requested_mode


def mock_proposal(context: RecoveryContext) -> str:
    if context.partial_size_changed:
        return json.dumps({"strategy": "restart", "reason": "임시 파일 길이가 기록과 다릅니다."})
    return json.dumps({"strategy": "resume", "reason": "기록과 같은 위치까지 저장됐습니다."})


def choose_advised_mode(context: RecoveryContext, fallback_mode: str,
                        strategy_source: str, advisor: StrategyAdvisor | None) -> RecoveryDecision:
    if context.partial_size_changed:
        fallback_mode = "restart"
    if advisor is None:
        reason = ("임시 파일 길이 변경 확인" if context.partial_size_changed
                  else "기본 복구 설정 적용")
        return RecoveryDecision(fallback_mode, "fallback", reason)

    try:
        raw = advisor(context)
        if not isinstance(raw, str) or len(raw) > 2048:
            raise ValueError
        reply = json.loads(raw)
        if not isinstance(reply, dict) or set(reply) != {"strategy", "reason"}:
            raise ValueError
        strategy, reason = reply["strategy"], reply["reason"]
        if (strategy not in RECOVERY_MODES or not isinstance(reason, str)
                or not 0 < len(reason.strip()) <= 240):
            raise ValueError
    except AdviceUnavailable as exc:
        return RecoveryDecision(fallback_mode, "fallback", str(exc))
    except (ValueError, RecursionError):
        return RecoveryDecision(fallback_mode, "fallback", "제안 응답 형식이 올바르지 않습니다")

    allowed_strategy = "restart" if context.partial_size_changed else "resume"
    if strategy != allowed_strategy:
        return RecoveryDecision(fallback_mode, "fallback", f"{strategy} 제안의 실행 조건 불충족")
    return RecoveryDecision(strategy, strategy_source, reason.strip())
