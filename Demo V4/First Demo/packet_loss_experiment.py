#!/usr/bin/env python3
"""격리된 Linux 루프백에서 정상 전송과 TCP 패킷 한 개 손실을 비교한다."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys

from demo import force_utf8_stdout, load_jsonl, make_test_file
from ftprotocol import sha256_file
from observe_run import run_experiment
from packet_capture import capture_packets, find_capture_tools

ART = pathlib.Path(__file__).resolve().parent / "loss_artifacts"
SIZE = 4 << 20
SEED = 20261109
PORT = 9290
TABLE = "packet_loss_demo"
CAPTURE_SECONDS = 8


def install_loss_rule() -> None:
    # numgen은 이 규칙에 매칭되는 패킷만 센다. 다섯 번째 큰 서버 패킷을 버린다.
    # 4 MiB 전송에서 카운터가 순환할 일은 없고, 실제 드롭 수도 따로 확인한다.
    rules = f"""
add table ip {TABLE}
add chain ip {TABLE} incoming {{ type filter hook input priority 0; policy accept; }}
add rule ip {TABLE} incoming iifname "lo" ip saddr 127.0.0.1 ip daddr 127.0.0.1 tcp sport {PORT} ip length > 200 numgen inc mod 1000000 4 counter drop
"""
    subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)


def count_dropped_packets() -> int:
    data = subprocess.check_output(["nft", "-j", "list", "table", "ip", TABLE])
    ruleset = json.loads(data)["nftables"]
    counters = [
        expr["counter"]["packets"]
        for entry in ruleset
        for expr in entry.get("rule", {}).get("expr", [])
        if "counter" in expr
    ]
    if len(counters) != 1:
        raise ValueError("손실 규칙의 드롭 카운터를 읽지 못했습니다")
    return counters[0]


def run_case(batch: pathlib.Path, source: pathlib.Path, case: str, tools: dict) -> dict:
    out_dir = batch / case
    out_dir.mkdir()
    with capture_packets(out_dir, "lo", PORT, CAPTURE_SECONDS, tools) as capture:
        transfer = run_experiment("normal", PORT, source, out_dir, SIZE)
        dropped = count_dropped_packets() if case == "one_drop" else 0

    streams = []
    if capture["available"]:
        summary_path = out_dir / capture["summary_file"]
        streams = json.loads(summary_path.read_text(encoding="utf-8"))["streams"]
    events = load_jsonl(out_dir / "logs" / "client.jsonl")
    offsets = [event["offset"] for event in events if event["event"] == "REQUEST"]
    retransmissions = sum(stream["suspected_retransmissions"] for stream in streams)
    same_connection = (
        len(streams) == 1 and offsets == [0] and transfer["scenario_reproduced"]
    )
    case_ok = (
        same_connection and transfer["final_size_match"] and transfer["final_hash_match"]
        and (dropped, bool(retransmissions)) == ((1, True) if case == "one_drop" else (0, False))
    )
    print(f"[packet-loss] {case}: {'PASS' if case_ok else 'FAIL'} "
          f"dropped={dropped} retransmissions={retransmissions} "
          f"streams={len(streams)} offsets={offsets} hash_match={transfer['final_hash_match']}")
    if not capture["available"]:
        print(f"[packet-loss]   캡처 상태: {capture['reason']}")
    return {
        "passed": case_ok,
        "dropped_packets": dropped,
        "suspected_retransmissions": retransmissions,
        "tcp_streams": streams,
        "same_connection": same_connection,
        "request_offsets": offsets,
        "capture": capture,
        **transfer,
    }


def isolated_experiment(parent_netns: str) -> int:
    if os.readlink("/proc/self/ns/net") == parent_netns or {
        name for _, name in socket.if_nameindex()
    } != {"lo"}:
        raise RuntimeError("실험 전용 네트워크 네임스페이스를 확인하지 못했습니다")

    # 루프백의 큰 묶음 전송을 끄고 일반적인 MTU에서 패킷 한 개를 관찰한다.
    subprocess.run(["ip", "link", "set", "dev", "lo", "mtu", "1500", "up"], check=True)
    subprocess.run(["ethtool", "-K", "lo", "tso", "off", "gso", "off", "gro", "off"], check=True)
    tools = find_capture_tools()
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    batch = ART / f"run-{stamp}"
    batch.mkdir(parents=True)
    source = batch / "testfile.bin"
    make_test_file(source, SIZE, SEED)

    normal = run_case(batch, source, "normal", tools)
    install_loss_rule()
    one_drop = run_case(batch, source, "one_drop", tools)
    overall = normal["passed"] and one_drop["passed"]
    summary = batch / "summary.json"
    summary.write_text(json.dumps({
        "passed": overall, "file_size": SIZE, "sha256": sha256_file(source),
        "port": PORT, "normal": normal, "one_drop": one_drop,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[packet-loss] summary: {summary}")
    print(f"[packet-loss] overall: {'PASS' if overall else 'FAIL'}")
    return 0 if overall else 1


def main() -> int:
    force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Linux에서 정상 TCP 파일 전송과 격리된 패킷 손실을 비교합니다.")
    parser.add_argument("--parent-netns", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if sys.platform != "linux":
        parser.error("Linux 또는 WSL 2 터미널에서 실행하세요")
    if os.geteuid() != 0:
        parser.error("격리 실험 설정을 위해 sudo python3 packet_loss_experiment.py 로 실행하세요")
    missing = [name for name in ("unshare", "ip", "ethtool", "nft", "dumpcap", "tshark")
               if not shutil.which(name)]
    if missing:
        parser.error("도구를 찾을 수 없습니다: " + ", ".join(missing))

    try:
        if args.parent_netns:
            return isolated_experiment(args.parent_netns)
        return subprocess.run([
            "unshare", "--net", "--", sys.executable, str(pathlib.Path(__file__).resolve()),
            "--parent-netns", os.readlink("/proc/self/ns/net"),
        ]).returncode
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"[packet-loss] 실험을 실행하지 못했습니다: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
