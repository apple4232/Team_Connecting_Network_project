"""실제 Solar 전략 제안, 파일 복구, 사후 장애 진단을 한 번에 실행한다."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import subprocess
import sys
import tempfile

from demo import force_utf8_stdout, load_jsonl
from observe_run import CASES, capture_interface
from solar_diag import API_KEY_ENV, SOLAR_MODEL

BASE = pathlib.Path(__file__).resolve().parent


def main() -> int:
    force_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", nargs="+", choices=CASES, default=["early_close"])
    parser.add_argument("--art-dir", type=pathlib.Path,
                        help="새 결과 폴더. 기본값은 시스템 임시 폴더 아래 새 폴더")
    parser.add_argument("--capture-interface", type=capture_interface)
    parser.add_argument("--capture-seconds", type=int, default=10)
    args = parser.parse_args()
    if len(set(args.case)) != len(args.case):
        parser.error("같은 사례를 두 번 지정할 수 없습니다")
    if not 1 <= args.capture_seconds <= 60:
        parser.error("--capture-seconds는 1~60 사이여야 합니다")
    if not os.environ.get(API_KEY_ENV, "").strip():
        if not sys.stdin.isatty():
            parser.error(f"{API_KEY_ENV} 환경변수를 설정하거나 터미널에서 실행하세요")
        key = getpass.getpass("Upstage API key: ").strip()
        if not key:
            parser.error("API 키가 필요합니다")
        os.environ[API_KEY_ENV] = key

    if args.art_dir is None:
        art = pathlib.Path(tempfile.mkdtemp(prefix="connecting-solar-"))
    else:
        art = args.art_dir.resolve()
        if art.exists():
            parser.error("기존 결과를 보존하려면 새 --art-dir 경로를 지정하세요")
        art.mkdir(parents=True)
    print(f"[solar-demo] 모델={SOLAR_MODEL} · 결과={art}")
    print("[solar-demo] 기본 조기 종료 사례는 전략 제안 1회와 장애 진단 1회를 요청합니다.")
    observe = [sys.executable, str(BASE / "observe_run.py"), "--case", *args.case,
               "--strategy-source", "solar", "--art-dir", str(art),
               "--capture-seconds", str(args.capture_seconds)]
    if args.capture_interface:
        observe += ["--capture-interface", args.capture_interface]
    observed = subprocess.run(observe).returncode

    runs = sorted(art.glob("run-*/run_meta.json"))
    if len(runs) != len(args.case):
        print("[solar-demo] 관측 자료 생성이 완료되지 않았습니다. 결과 폴더를 확인하세요.")
        return 1
    diagnosis = subprocess.run([
        sys.executable, str(BASE / "solar_diag.py"), "--all", "--real",
        "--art-dir", str(art),
    ]).returncode
    results = []
    for path in runs:
        meta = json.loads(path.read_text(encoding="utf-8"))
        events = load_jsonl(path.parent / "logs" / "client.jsonl")
        requested = any(e["event"] == "SOLAR_ADVICE_REQUEST" for e in events)
        responded = any(e["event"] == "SOLAR_ADVICE_RESPONSE" for e in events)
        applied = any(e["event"] == "RECOVERY_DECISION" and e.get("source") == "solar"
                      for e in events)
        needed = meta["case"] != "normal"
        advice_ok = not needed or (requested and responded and applied)
        verified = meta["final_hash_match"] and meta["final_size_match"]
        results.append({"run_id": meta["run_id"], "case": meta["case"],
                        "solar_advice_requested": requested, "solar_advice_received": responded,
                        "solar_strategy_applied": applied, "strategy_verified": advice_ok,
                        "transfer_verified": verified,
                        "scenario_reproduced": meta["scenario_reproduced"]})
        print(f"[solar-demo] {meta['case']} · 실제응답={responded} · "
              f"Solar전략적용={applied} · 해시검증={verified}")
    passed = observed == 0 and diagnosis == 0 and all(
        row["strategy_verified"] and row["transfer_verified"] and row["scenario_reproduced"]
        for row in results)
    summary = {"mode": "REAL", "model": SOLAR_MODEL, "passed": passed,
               "observation_exit": observed, "diagnosis_exit": diagnosis, "runs": results}
    (art / "solar_demo_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[solar-demo] {'PASS' if passed else 'FAIL'} · {art / 'solar_demo_summary.json'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
