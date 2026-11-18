"""Wireshark 없이 실행하는 패킷 요약 회귀 테스트."""
import pathlib
import unittest

from packet_capture import capture_packets, summarize_packets


class PacketCaptureTests(unittest.TestCase):
    def test_flags_streams_and_evidence_ids(self):
        rows = [
            ("1", "1780000000.100", "0", "50000", "0x0002", "", "", ""),
            ("2", "1780000000.101", "0", "9280", "0x0012", "", "", ""),
            ("3", "1780000000.102", "0", "50000", "0x0010", "", "", ""),
            ("4", "1780000000.103", "0", "9280", "0x0011", "", "", ""),
            ("5", "1780000000.104", "1", "9280", "0x0014", "", "", ""),
            ("6", "1780000000.105", "1", "9280", "0x0018", "1", "1", ""),
        ]
        summary = summarize_packets("\n".join("\t".join(row) for row in rows), 9280)
        self.assertEqual(summary["packet_count"], 6)
        first, second = summary["streams"]
        self.assertEqual((first["syn"], first["syn_ack"], first["fin"]), (1, 1, 1))
        self.assertEqual((second["rst"], second["suspected_retransmissions"]), (1, 1))
        self.assertEqual([row["evidence_id"] for row in summary["evidence"]],
                         ["P1", "P2", "P4", "P5", "P6"])
        self.assertEqual(summary["evidence"][1]["direction"], "server_to_client")

    def test_empty_summary_is_not_available(self):
        summary = summarize_packets("", 9280)
        self.assertFalse(summary["available"])
        self.assertEqual(summary["packet_count"], 0)

    def test_detected_tshark_does_not_mean_capture_available(self):
        tools = {"dumpcap": None, "tshark": "tshark"}
        with capture_packets(pathlib.Path("."), "lo", 9280, 5, tools) as state:
            self.assertEqual(state["tools_detected"], ["tshark"])
            self.assertEqual(state["capture_status"], "unavailable")
            self.assertEqual(state["summary_status"], "not_run")
            self.assertFalse(state["available"])


if __name__ == "__main__":
    unittest.main()
