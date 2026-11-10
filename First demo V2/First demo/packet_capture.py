"""로컬 데모 포트의 TCP 캡처 및 패킷 근거 요약."""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import shutil
import subprocess
import time
from contextlib import contextmanager

CAPTURE_FILE = "capture.pcapng"
SUMMARY_FILE = "packet_summary.json"
MAX_EVIDENCE = 60
TCP_FLAGS = (("SYN", 0x02), ("ACK", 0x10), ("FIN", 0x01), ("RST", 0x04))
TSHARK_FIELDS = (
    "frame.number", "frame.time_epoch", "tcp.stream", "tcp.srcport",
    "tcp.flags",
    "tcp.analysis.retransmission", "tcp.analysis.fast_retransmission",
    "tcp.analysis.spurious_retransmission",
)


def find_capture_tools() -> dict[str, str | None]:
    tools = {name: shutil.which(name) for name in ("dumpcap", "tshark")}
    if os.name == "nt":
        for name, path in tools.items():
            if path:
                continue
            for key in ("ProgramFiles", "ProgramFiles(x86)"):
                root = os.environ.get(key)
                if root:
                    exe = pathlib.Path(root) / "Wireshark" / f"{name}.exe"
                    if exe.is_file():
                        tools[name] = str(exe)
                        break
    return tools


def capture_ready(proc: subprocess.Popen, path: pathlib.Path) -> bool:
    deadline = time.monotonic() + 5
    while proc.poll() is None and time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return True
        time.sleep(0.05)
    return False


@contextmanager
def capture_packets(out_dir: pathlib.Path, interface: str | None, port: int,
                    seconds: int, tools: dict, *, withheld: bool = False):
    """캡처가 준비된 후 전송을 시작하고, 캡처 종료 뒤 요약을 생성한다."""
    state = {
        "tools_detected": [name for name, path in tools.items() if path],
        "capture_status": "not_requested",
        "summary_status": "not_run",
        "available": False,
        "reason": "이 실행에서 패킷 캡처를 요청하지 않았습니다",
    }
    if withheld:
        state.update(capture_status="withheld", reason="이 실행에는 패킷 관측이 없습니다")
    elif interface and not tools["dumpcap"]:
        state.update(capture_status="unavailable", reason="Dumpcap을 찾을 수 없습니다")
    if withheld or not interface or not tools["dumpcap"]:
        yield state
        return

    pcap = out_dir / CAPTURE_FILE
    capture_filter = f"ip and tcp and src host 127.0.0.1 and dst host 127.0.0.1 and port {port}"
    args = [tools["dumpcap"], "-i", interface, "-p", "-q", "-s", "128",
            "-f", capture_filter, "-a", f"duration:{seconds}",
            "-a", "filesize:16384", "-w", str(pcap)]
    with (out_dir / "capture.log").open("wb") as log:
        try:
            proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        except OSError as exc:
            log.write(str(exc).encode("utf-8"))
            proc = None
    if proc is None:
        state.update(capture_status="failed", reason="Dumpcap을 시작하지 못했습니다")
        yield state
        return

    try:
        ready = capture_ready(proc, pcap)
        if not ready:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            state.update(capture_status="failed",
                         reason="캡처를 시작하지 못했습니다. 인터페이스와 권한을 확인하세요")
            yield state
            return

        yield state
        covered_run = proc.poll() is None
        try:
            returncode = proc.wait(timeout=seconds + 3)
        except subprocess.TimeoutExpired:
            state.update(capture_status="failed", reason="캡처 프로세스가 종료되지 않았습니다")
            return
        state["capture_file"] = pcap.name
        if returncode:
            state.update(capture_status="failed", reason="캡처 도중 오류가 발생했습니다")
        elif not covered_run:
            state.update(capture_status="incomplete",
                         reason="전송 실험이 끝나기 전에 캡처가 종료됐습니다")
        else:
            state["capture_status"] = "captured"
            save_summary(out_dir, pcap, port, tools["tshark"], state)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def save_summary(out_dir: pathlib.Path, pcap: pathlib.Path, port: int,
                 tshark: str | None, state: dict) -> None:
    if not tshark:
        state.update(summary_status="unavailable", reason="TShark를 찾을 수 없습니다")
        return

    display_filter = (f"ip.src == 127.0.0.1 && ip.dst == 127.0.0.1 "
                      f"&& tcp.port == {port}")
    args = [tshark, "-n", "-r", str(pcap), "-Y", display_filter,
            "-T", "fields", "-E", "occurrence=f"]
    for field in TSHARK_FIELDS:
        args.extend(["-e", field])
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=20, check=True)
        summary = summarize_packets(result.stdout, port)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        details = getattr(exc, "stderr", None) or str(exc)
        if isinstance(details, bytes):
            details = details.decode("utf-8", errors="replace")
        (out_dir / "tshark.log").write_text(details, encoding="utf-8")
        state.update(summary_status="failed", reason="캡처에서 TCP 요약을 생성하지 못했습니다")
        return
    if summary["packet_count"] == 0:
        state.update(summary_status="empty", reason="캡처에 이 실행의 TCP 패킷이 없습니다")
        return
    path = out_dir / SUMMARY_FILE
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    state.update(summary_status="ready", available=True, summary_file=path.name, reason=None)


def summarize_packets(output: str, port: int) -> dict:
    streams: dict[int, dict] = {}
    evidence = []
    interesting_count = 0
    packet_count = 0
    for line in output.splitlines():
        frame, epoch, stream, srcport, raw_flags, retransmit, fast, spurious = line.split("\t")
        stream_id = int(stream)
        counts = streams.setdefault(stream_id, {
            "tcp_stream": stream_id, "packet_count": 0,
            "syn": 0, "syn_ack": 0, "fin": 0, "rst": 0, "suspected_retransmissions": 0,
        })
        counts["packet_count"] += 1
        packet_count += 1
        flag_bits = int(raw_flags, 16)
        flags = [name for name, mask in TCP_FLAGS if flag_bits & mask]
        if "SYN" in flags:
            counts["syn_ack" if "ACK" in flags else "syn"] += 1
        if "FIN" in flags:
            counts["fin"] += 1
        if "RST" in flags:
            counts["rst"] += 1
        suspected_retransmission = bool(retransmit or fast or spurious)
        counts["suspected_retransmissions"] += suspected_retransmission
        if not suspected_retransmission and not {"SYN", "FIN", "RST"}.intersection(flags):
            continue
        interesting_count += 1
        if len(evidence) < MAX_EVIDENCE:
            ts = datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc)
            evidence.append({
                "evidence_id": f"P{frame}", "source": "packet", "frame_number": int(frame),
                "ts": ts.isoformat(timespec="microseconds"), "tcp_stream": stream_id,
                "direction": "server_to_client" if int(srcport) == port else "client_to_server",
                "tcp_flags": flags, "suspected_retransmission": suspected_retransmission,
            })
    return {"available": packet_count > 0, "packet_count": packet_count,
            "streams": list(streams.values()), "evidence": evidence,
            "omitted_evidence_count": interesting_count - len(evidence)}
