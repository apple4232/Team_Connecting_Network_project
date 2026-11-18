"""복구 직전에 사용할 Solar 전략 제안 요청."""
from __future__ import annotations

import json
import os
from concurrent.futures import Future
from dataclasses import asdict
from threading import Thread
from urllib.error import HTTPError

from recovery_policy import AdviceUnavailable, RECOVERY_MODES, RecoveryContext
from solar_diag import API_KEY_ENV, SOLAR_MODEL, call_solar

SOLAR_WAIT = 3.0
SYSTEM_PROMPT = (
    "TCP 파일 전송 재접속 전략을 고르세요. 받은 JSON 정보만 사용하세요. "
    "saved_bytes와 expected_saved_bytes가 일치하면 resume을 제안하세요. "
    "두 길이가 다르면 restart를 제안하세요. 서버 파일 식별값은 Python이 재접속 후 검증합니다. "
    "strategy와 240자 이하의 한국어 reason을 담은 JSON 객체 하나만 반환하세요."
)


def solar_proposal(context: RecoveryContext) -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise AdviceUnavailable(f"{API_KEY_ENV} 미설정")
    request = {
        "model": SOLAR_MODEL,
        "temperature": 0,
        "max_tokens": 250,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(asdict(context), ensure_ascii=False)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "recovery_strategy", "strict": True, "schema": {
                "type": "object",
                "properties": {
                    "strategy": {"type": "string", "enum": list(RECOVERY_MODES)},
                    "reason": {"type": "string"},
                },
                "required": ["strategy", "reason"],
                "additionalProperties": False,
            },
        }},
    }
    response: Future[dict] = Future()

    def fetch() -> None:
        try:
            response.set_result(call_solar(request, key, timeout=SOLAR_WAIT))
        except Exception as exc:
            response.set_exception(exc)

    # 소켓 시간 제한과 별개로, 재접속이 응답 때문에 지연되는 시간을 제한한다.
    Thread(target=fetch, daemon=True).start()
    try:
        return response.result(timeout=SOLAR_WAIT)["content"]
    except TimeoutError:
        raise AdviceUnavailable("Solar 응답 대기 시간 초과") from None
    except HTTPError as exc:
        exc.close()
        raise AdviceUnavailable(f"Solar HTTP {exc.code}") from None
    except OSError:
        raise AdviceUnavailable("Solar 연결 오류") from None
    except (KeyError, IndexError, TypeError, ValueError):
        raise AdviceUnavailable("Solar 응답을 해석할 수 없습니다") from None
