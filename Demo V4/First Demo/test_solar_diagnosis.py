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
                             "saved_bytes": 5, "expected_size": 31}],
            "stall": [{"event": "INTERRUPTED", "reason": "recv timeout",
                       "saved_bytes": 5, "expected_size": 31}],
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
                response = {"content": json.dumps(verdict), "model": "fixture-model"}
                with patch.object(solar_diag, "ART", root), patch.object(
                        solar_diag, "call_solar", return_value=response) as call:
                    result = solar_diag.diagnose("run-test", root / "success", "fixture-secret")
                    self.assertTrue(result["solar"]["diagnosis_match"])
                    self.assertTrue(result["solar"]["evidence_supports"])
                    self.assertTrue(result["solar"]["withheld_match"])
                    request = call.call_args.args[0]
                    user = json.loads(request["messages"][1]["content"])
                    self.assertNotIn("case", user)
                    self.assertNotIn("fixture-secret", json.dumps(request))
                    response["content"] = "not JSON"
                    failed = solar_diag.diagnose("run-test", root / "failed", "fixture-secret")
                    self.assertIn("error", failed)
                    self.assertNotIn("solar", failed)
                    self.assertTrue((root / "failed" / "error.txt").is_file())


if __name__ == "__main__":
    unittest.main()
