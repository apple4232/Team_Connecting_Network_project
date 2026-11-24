"""실제 진단 경로의 응답 검증과 실패 기록을 외부 API 없이 검사한다."""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import solar_diag


class SolarDiagnosisTest(unittest.TestCase):
    def test_real_diagnosis_evaluation_and_failed_response(self):
        cases = {
            "normal": [{"event": "TRANSFER_COMPLETED"}, {"event": "SHA256_OK"}],
            "early_close": [{"event": "INTERRUPTED", "reason": "connection closed",
                             "saved_bytes": 5, "expected_size": 31},
                            {"event": "TRANSFER_COMPLETED"}, {"event": "SHA256_OK"}],
            "stall": [{"event": "INTERRUPTED", "reason": "recv timeout",
                       "saved_bytes": 5, "expected_size": 31},
                      {"event": "TRANSFER_COMPLETED"}, {"event": "SHA256_OK"}],
            "insufficient": [{"event": "TRANSFER_START"}],
        }
        for case, events in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                run = root / "run-test"
                run.mkdir()
                meta = {"case": case, "capture": {}, "client_exit": 0, "recovered": True,
                        "final_hash_match": True, "resume_offset": 5}
                (run / "run_meta.json").write_text(json.dumps(meta), encoding="utf-8")
                for number, event in enumerate(events, 1):
                    event["evidence_id"] = f"E{number:03d}"
                (run / "evidence.jsonl").write_text(
                    "\n".join(json.dumps(e) for e in events), encoding="utf-8")
                withheld = case == "insufficient"
                verdict = {"diagnosis": solar_diag.CRITERIA[case], "withheld": withheld,
                           "evidence_ids": [] if withheld else [e["evidence_id"] for e in events],
                           "missing_evidence": ["completion or interruption"] if withheld else []}
                verdict.update({
                    "interruption_evidence_ids": [e["evidence_id"] for e in events if e["event"] == "INTERRUPTED"],
                    "transfer_outcome": "not_verified" if withheld else "verified_complete",
                    "outcome_evidence_ids": [e["evidence_id"] for e in events
                                             if e["event"] in ("TRANSFER_COMPLETED", "SHA256_OK")],
                })
                response = {"content": json.dumps(verdict), "model": "fixture-model"}
                with patch.object(solar_diag, "ART", root), patch.object(
                        solar_diag, "call_solar", return_value=response) as call:
                    result = solar_diag.diagnose("run-test", root / "success", "fixture-secret")
                    self.assertTrue(result["solar"]["diagnosis_match"])
                    self.assertTrue(result["solar"]["evidence_supports"])
                    self.assertTrue(result["solar"]["withheld_match"])
                    self.assertTrue(result["solar"]["observations_correct"])
                    request = call.call_args.args[0]
                    user = json.loads(request["messages"][1]["content"])
                    self.assertNotIn("case", user)
                    self.assertNotIn("fixture-secret", json.dumps(request))
                    response["content"] = "not JSON"
                    failed = solar_diag.diagnose("run-test", root / "failed", "fixture-secret")
                    self.assertIn("error", failed)
                    self.assertNotIn("solar", failed)
                    self.assertTrue((root / "failed" / "error.txt").is_file())

    def test_missing_observations_are_rejected_for_new_responses(self):
        legacy = {"diagnosis": "no_failure_observed", "evidence_ids": ["E001", "E002"],
                  "withheld": False, "missing_evidence": []}
        self.assertEqual(solar_diag.parse_diagnosis(json.dumps(legacy)), legacy)
        with self.assertRaises(ValueError):
            solar_diag.parse_diagnosis(json.dumps(legacy), require_observations=True)

    def test_omitted_interruption_and_wrong_final_outcome_fail_evaluation(self):
        events = [
            {"evidence_id": "E001", "event": "INTERRUPTED", "reason": "connection closed",
             "saved_bytes": 5, "expected_size": 31},
            {"evidence_id": "E002", "event": "TRANSFER_COMPLETED"},
            {"evidence_id": "E003", "event": "SHA256_OK"},
        ]
        meta = {"case": "early_close", "client_exit": 0, "recovered": True,
                "final_hash_match": True}
        reply = {"diagnosis": "early_connection_close", "evidence_ids": ["E001"],
                 "withheld": False, "missing_evidence": [],
                 "interruption_evidence_ids": ["E001"], "transfer_outcome": "verified_complete",
                 "outcome_evidence_ids": ["E002", "E003"]}
        with patch.object(solar_diag, "load_meta", return_value=meta), patch.object(
                solar_diag, "get_app_log_events", return_value=events), patch.object(
                solar_diag, "get_packet_summary", return_value={}):
            self.assertTrue(solar_diag.evaluate("run-test", reply)["observations_correct"])
            for fields in ({"interruption_evidence_ids": []},
                           {"transfer_outcome": "not_verified", "outcome_evidence_ids": []},
                           {"outcome_evidence_ids": ["E999"]}):
                with self.subTest(fields=fields):
                    result = solar_diag.evaluate("run-test", dict(reply, **fields))
                    self.assertTrue(result["diagnosis_match"])
                    self.assertFalse(result["observations_correct"])
            wrong = dict(reply, diagnosis="no_failure_observed", evidence_ids=["E002", "E003"])
            result = solar_diag.evaluate("run-test", wrong)
            self.assertEqual(result["solar_diagnosis"], "no_failure_observed")
            self.assertFalse(result["diagnosis_match"])
            self.assertFalse(result["evidence_supports"])


if __name__ == "__main__":
    unittest.main()
