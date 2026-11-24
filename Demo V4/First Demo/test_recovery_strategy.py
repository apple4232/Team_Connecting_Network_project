"""외부 API와 실제 소켓 없이 전송 중 전략 제안 경로를 검증한다."""
from __future__ import annotations

import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from threading import Event
from unittest.mock import patch
from urllib.error import HTTPError

import ftclient
import solar_recovery
import solar_diag
from demo import load_jsonl
from ftprotocol import JsonlLog
from recovery_policy import AdviceUnavailable, RecoveryContext

DATA = b"Check the proposed recovery strategy."
CUT = 9


class MemoryConnection:
    def __init__(self, respond, close):
        self.respond = respond
        self.close = close
        self.response = io.BytesIO()

    def sendall(self, request: bytes) -> None:
        offset = json.loads(request[4:])["offset"]
        self.response = io.BytesIO(self.respond(offset))

    def recv(self, limit: int) -> bytes:
        return self.response.read(limit)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RecoveryStrategyTest(unittest.TestCase):
    def run_transfer(self, *, strategy_source="mock", modify_partial=False,
                     change_identity=False, refuse_first_retry=False,
                     retries=2, recovery_mode="resume"):
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            out = folder / "received.bin"
            partial_file = folder / "received.bin.part"
            offsets = []
            refused = False

            def connect(address, timeout):
                nonlocal refused
                if refuse_first_retry and offsets and not refused:
                    refused = True
                    raise ConnectionRefusedError("test reconnect refused")
                first = not offsets

                def respond(offset):
                    offsets.append(offset)
                    digest = hashlib.sha256(DATA).hexdigest()
                    if change_identity and not first:
                        digest = "0" * 64
                    header = json.dumps({"status": "ok", "file_id": "test.bin", "offset": offset,
                                         "size": len(DATA), "sha256": digest}).encode()
                    data = DATA[:CUT] if first else DATA[offset:]
                    return len(header).to_bytes(4, "big") + header + data

                def close():
                    if first and modify_partial:
                        with partial_file.open("ab") as stream:
                            stream.write(b"x")

                return MemoryConnection(respond, close)

            argv = ["ftclient.py", "--port", "9240", "--file-id", "test.bin",
                    "--output", str(out), "--log-dir", str(folder),
                    "--retry-delay", "0", "--retries", str(retries),
                    "--recovery-mode", recovery_mode,
                    "--strategy-source", strategy_source]
            with patch.object(sys, "argv", argv), patch.object(
                    ftclient.socket, "create_connection", side_effect=connect), patch("builtins.print"):
                status = ftclient.main()
            events = load_jsonl(folder / "client.jsonl")
            decisions = [e for e in events if e["event"] == "RECOVERY_DECISION"]
            if status == 0:
                self.assertEqual(out.read_bytes(), DATA)
                self.assertFalse(partial_file.exists())
            else:
                self.assertFalse(out.exists())
                self.assertEqual(partial_file.read_bytes(), DATA[:CUT] + (b"x" if modify_partial else b""))
            return status, offsets, decisions

    def test_mock_resumes_intact_partial_file(self):
        status, offsets, decisions = self.run_transfer()
        self.assertEqual((status, offsets), (0, [0, CUT]))
        self.assertEqual([(d["strategy"], d["source"]) for d in decisions], [("resume", "mock")])

    def test_mock_restarts_after_partial_length_changes(self):
        status, offsets, decisions = self.run_transfer(modify_partial=True)
        self.assertEqual((status, offsets), (0, [0, 0]))
        self.assertEqual([(d["strategy"], d["source"]) for d in decisions], [("restart", "mock")])

    def test_restart_survives_a_failed_reconnect(self):
        status, offsets, decisions = self.run_transfer(modify_partial=True, refuse_first_retry=True)
        self.assertEqual((status, offsets), (0, [0, 0]))
        self.assertEqual([(d["strategy"], d["source"]) for d in decisions],
                         [("restart", "mock"), ("restart", "fallback")])

    def test_identity_mismatch_preserves_partial_data(self):
        status, offsets, decisions = self.run_transfer(modify_partial=True, change_identity=True)
        self.assertEqual((status, offsets), (2, [0, 0]))
        self.assertEqual(decisions[0]["strategy"], "restart")

    def test_invalid_or_inapplicable_proposals_use_fallback(self):
        for reply in ("not JSON", '{"strategy":"restart","reason":"again"}',
                      '{"strategy":"delete","reason":"unsupported"}'):
            with self.subTest(reply=reply), patch.object(ftclient, "mock_proposal", return_value=reply):
                status, offsets, decisions = self.run_transfer()
            self.assertEqual((status, offsets), (0, [0, CUT]))
            self.assertEqual(decisions[0]["source"], "fallback")

    def test_exhausted_retries_skip_advisor(self):
        with patch.object(ftclient, "mock_proposal") as advisor:
            self.assertEqual(self.run_transfer(retries=0), (1, [0], []))
        advisor.assert_not_called()

    def test_fixed_options_still_apply_without_advisor(self):
        with patch.object(ftclient, "mock_proposal") as advisor:
            for mode, offset in (("resume", CUT), ("restart", 0)):
                with self.subTest(mode=mode):
                    result = self.run_transfer(strategy_source="fixed", recovery_mode=mode)
                    self.assertEqual(result, (0, [0, offset], []))
        advisor.assert_not_called()

    def test_solar_success_uses_minimal_telemetry(self):
        response = {"content": '{"strategy":"resume","reason":"길이 일치"}'}
        with patch.dict(solar_recovery.os.environ, {solar_recovery.API_KEY_ENV: "fixture-key"}), \
                patch.object(solar_recovery, "call_solar", return_value=response) as call:
            status, offsets, decisions = self.run_transfer(strategy_source="solar")
        self.assertEqual((status, offsets), (0, [0, CUT]))
        self.assertEqual(decisions[0]["source"], "solar")
        call.assert_called_once()
        request, key = call.call_args.args
        self.assertEqual(key, "fixture-key")
        self.assertEqual(set(json.loads(request["messages"][1]["content"])), {
            "symptom", "saved_bytes", "expected_saved_bytes", "expected_size", "retries_left"})
        self.assertEqual(call.call_args.kwargs["timeout"], 3)

    def test_solar_http_denial_and_missing_key_use_fallback(self):
        error = HTTPError("https://example.invalid/", 403, "Forbidden", None, None)
        for key, reason in (("fixture-key", "Solar HTTP 403"), ("", "UPSTAGE_API_KEY 미설정")):
            with self.subTest(reason=reason), patch.dict(
                    solar_recovery.os.environ, {solar_recovery.API_KEY_ENV: key}), \
                    patch.object(solar_recovery, "call_solar", side_effect=error) as call:
                status, offsets, decisions = self.run_transfer(strategy_source="solar")
                self.assertEqual(call.call_count, int(bool(key)))
            self.assertEqual((status, offsets), (0, [0, CUT]))
            self.assertEqual((decisions[0]["source"], decisions[0]["reason"]), ("fallback", reason))

    def test_solar_timeout_uses_fallback(self):
        release = Event()

        def slow_request(*args, **kwargs):
            release.wait(timeout=2)
            return {"content": '{"strategy":"resume","reason":"late"}'}

        try:
            with patch.dict(solar_recovery.os.environ, {solar_recovery.API_KEY_ENV: "fixture-key"}), \
                    patch.object(solar_recovery, "SOLAR_WAIT", 0.02), \
                    patch.object(solar_recovery, "call_solar", side_effect=slow_request):
                status, offsets, decisions = self.run_transfer(strategy_source="solar")
        finally:
            release.set()
        self.assertEqual((status, offsets), (0, [0, CUT]))
        self.assertEqual((decisions[0]["source"], decisions[0]["reason"]),
                         ("fallback", "Solar 응답 대기 시간 초과"))

    def test_real_proposal_records_response_without_credentials(self):
        context = RecoveryContext("connection closed", CUT, CUT, len(DATA), 2)
        result = {"content": '{"strategy":"resume","reason":"saved bytes match"}',
                  "model": "fixture-model", "usage": {"total_tokens": 30}, "finish": "stop"}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "client.jsonl"
            log = JsonlLog(path, "client", "observation")
            with patch.dict(
                    solar_recovery.os.environ, {solar_recovery.API_KEY_ENV: "fixture-secret"}), \
                    patch.object(solar_recovery, "call_solar", return_value=result):
                self.assertEqual(solar_recovery.solar_proposal(context, log=log), result["content"])
            events = load_jsonl(path)
            self.assertEqual([e["event"] for e in events],
                             ["SOLAR_ADVICE_REQUEST", "SOLAR_ADVICE_RESPONSE"])
            self.assertEqual(events[-1]["model"], "fixture-model")
            self.assertNotIn("fixture-secret", path.read_text(encoding="utf-8"))

    def test_empty_real_response_records_failure(self):
        context = RecoveryContext("connection closed", CUT, CUT, len(DATA), 2)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "client.jsonl"
            log = JsonlLog(path, "client", "observation")
            with patch.dict(
                    solar_recovery.os.environ, {solar_recovery.API_KEY_ENV: "fixture-secret"}), \
                    patch.object(solar_recovery, "call_solar", return_value={"content": ""}):
                with self.assertRaises(AdviceUnavailable):
                    solar_recovery.solar_proposal(context, log=log)
            self.assertEqual(load_jsonl(path)[-1]["event"], "SOLAR_ADVICE_FAILED")

    def test_real_http_payload_and_truncated_response(self):
        request = {"model": "solar-pro4", "messages": [{"role": "user", "content": "OK"}]}
        body = {"id": "fixture-request", "model": "fixture-model", "usage": {},
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        for finish in ("stop", "length"):
            with self.subTest(finish=finish):
                body["choices"][0]["finish_reason"] = finish
                with patch.object(solar_diag.urllib.request, "urlopen") as opened:
                    opened.return_value.__enter__.return_value.read.return_value = json.dumps(body).encode()
                    if finish == "length":
                        with self.assertRaises(ValueError):
                            solar_diag.call_solar(request, "fixture-secret")
                    else:
                        result = solar_diag.call_solar(request, "fixture-secret")
                        self.assertEqual(result["request_id"], "fixture-request")
                        self.assertEqual(result["content"], "OK")
                    sent = opened.call_args.args[0]
                    self.assertEqual(sent.full_url, "https://api.upstage.ai/v1/chat/completions")
                    self.assertEqual(json.loads(sent.data), request)
                    self.assertEqual(sent.get_header("Authorization"), "Bearer fixture-secret")


if __name__ == "__main__":
    unittest.main()
