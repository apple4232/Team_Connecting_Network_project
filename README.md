# Network Connectivity Failure Detection and Automatic Recovery System

Team Connecting · Computer Networks Team Project

## Problem

Interrupted data transfers caused by unstable network connectivity in environments such as university buildings.

## Data and Methods

Reproduce network failures in a controlled environment and collect packet captures and application logs to analyze TCP retransmissions, response delays, and connection state changes.

Use Upstage Solar LLM for evidence-based failure diagnosis and a Python program for reconnection and transfer resumption. Evaluate diagnostic accuracy and recovery outcomes against predefined criteria.

## Expected Deliverable

A prototype system integrating network failure detection, LLM-based diagnosis, and automatic reconnection and transfer resumption.

## Iteration 1 — Resumable TCP file transfer

Local TCP server/client on `127.0.0.1`, Python standard library only.

| File | Role |
|---|---|
| `ftprotocol.py` | Shared protocol: length-prefixed JSON frames, `recv_exact`, SHA-256, JSONL logger |
| `ftserver.py` | TCP server serving one file from a requested offset; `--inject-at N` cuts the first connection once at byte N |
| `ftclient.py` | Client storing to a `.part` temp file, detecting interruption (early close / recv timeout), auto-reconnect and resume, final size+SHA-256 verification |
| `demo.py` | One command: create test file → run server+client (normal, then fault-injected) → verify → clean up |
| `test_error_paths.py` | Error-path regression tests (existing output, retry exhaustion, identity change, server error frames) |

Protocol: request frame `{"file_id","offset"}`; response header frame `{"status","file_id","offset","size","sha256"}` followed by `size-offset` file bytes with no delimiter. Headers are read with `recv_exact` (never assume one `recv` == one message); the resume offset is the byte count actually stored in the `.part` file, re-read from disk on every reconnect.

```bash
python demo.py              # normal + fault-injected cases; exit 0 when all checks pass
python test_error_paths.py  # 4 error-path tests; exit 0 when all pass
```

Verified results (Windows 10, Git Bash, Python 3.12.2; 4,194,304-byte file, SHA-256 `5bf7ee3d…e55f4c`):

- Normal transfer: hashes match, 0 reconnects.
- Fault injection at byte 1,398,101: `Connection interrupted → Reconnected → Resuming from byte 1398101 → Transfer completed → SHA-256 verified`; final hash matches; exactly 1 injection record.
- Error paths: duplicate output → exit 3 (no overwrite), retry exhaustion → exit 1 (`.part` kept), identity change → exit 2, server rejects `INVALID_OFFSET` / `FILE_NOT_FOUND`.

Logs (JSONL, per run under `demo_artifacts/<case>/logs/`): observation logs `server.jsonl` / `client.jsonl` carry time, event type, and byte positions; fault-injection records are kept separately in `injection.jsonl` (`class: "injection"`) so diagnosis can later be scored against ground truth the model never sees.

## Iteration 2 — Restart vs resume comparison

`ftclient.py --recovery-mode resume|restart` (default `resume`): after reconnecting and re-verifying the file identity, `restart` discards the `.part` file and requests offset 0; `resume` requests the byte position actually stored. Both modes share identity verification, retry limits, error handling, and final verification.

```bash
python compare_recovery.py   # same file, same cut point, same timeout/retries for both modes
```

Metric definitions (all aggregated from actual logs, never hardcoded in the table):

- **Reconnect offset**: the client `REQUEST` offset after the first `RECONNECTED`.
- **Recovered bytes**: sum of server `bytes_sent` over all connections except the first one (file body only, protocol headers excluded, multiple reconnects summed).
- **Recovery time**: first `RECONNECTED` → `TRANSFER_COMPLETED`, measured on the client's monotonic clock (log `mono` field), so both modes share the same definition; includes identity re-verification and any further retries, excludes final hash verification.

Verified results (same environment, 4,194,304-byte file, cut at byte 1,398,101):

| Mode | Reconnect offset | Recovered bytes (expected) | Recovery time |
|---|---|---|---|
| restart | 0 | 4,194,304 B (= 4,194,304) | 15–78 ms |
| resume | 1,398,101 | 2,796,203 B (= 2,796,203) | 15–47 ms |

Byte savings: resume transfers exactly 1,398,101 B (~33%) less — a structural difference (size − offset) reproduced identically on every run. Recovery-time differences were within timer-resolution noise at loopback speed, so faster recovery was **not** claimed; savings and time are reported separately. Summary: `compare_artifacts/summary.json`.

## Iteration 3a — Evidence collection for failure diagnosis

`observe_run.py` re-runs cases with the existing server/client (Python recovery is untouched and independent of any LLM) and distills observation logs into diagnosis-ready evidence.

```bash
python observe_run.py            # cases: normal / early_close / insufficient
```

- Output per run under `diag_artifacts/run-<ts>-<n>/`: `evidence.jsonl` (whitelisted events only), `run_meta.json` (recovery outcome, capture-tool status), raw logs.
- Evidence records carry `evidence_id`, `source` (`server_app`/`client_app`), wall-clock `ts` (same host clock, so server/client lines form one timeline), event type, and byte positions. Whitelist excludes file paths, file identifiers, SHA-256 values, injection records (`injection.jsonl` is kept apart as ground truth), and case names — so nothing reveals the answer to the diagnoser.
- The `insufficient` case intentionally keeps only connection-accept/start events (no completion/interruption), representing an under-observed run.
- Capture-tool status is probed, not installed or elevated: on this machine (Windows 10, no admin rights, tshark/tcpdump/Npcap absent; built-in `pktmon` present but requires admin) capture is recorded as unavailable and **no packet evidence is produced — packet capture is UNVERIFIED in this environment**. FIN/RST/SYN and TCP retransmission are therefore reported only if actually captured.

## Iteration 3b — Solar diagnosis and evaluation

`solar_diag.py` exposes two lookup tools to the model — `get_packet_summary(run_id)` (returns `available:false` + reason in this environment) and `get_app_log_events(run_id)` (whitelisted evidence) — sends only those observation results, and requires a structured verdict: `{diagnosis, evidence_ids, withheld, missing_evidence}`.

```bash
python solar_diag.py --all        # diagnose every run + score against predefined criteria
python solar_diag.py --run <id>   # single run
```

Diagnosis states (scoped to what is actually observable): `early_connection_close` (close before expected size, e.g. `INTERRUPTED reason="connection closed"` / `CONNECTION_CLOSED file_pos < size`, optionally followed by `REQUEST offset>0`), `timeout_or_stall` (`recv timeout`), `no_failure_observed` (completion + hash verification, no interruption), `insufficient_evidence` (nothing conclusive → withholding judgment and naming missing evidence is the correct answer).

Evaluation is predefined per case (expected diagnosis, expected withholding, supporting event types) and separates **evidence IDs existing** from **evidence actually supporting the diagnosis**. Python recovery outcomes are recorded in a separate column and never depend on the LLM or API state.

Verification status (as of this commit):

- **Mock verification: PASSED (3/3 cases)** — run without `UPSTAGE_API_KEY`, exercising the lookup tools, request building, response parsing, and scoring end-to-end with a local rule-based responder. This validates the pipeline only, **not** Solar's diagnostic quality.
- **Real Solar API call: NOT YET RUN** — set `UPSTAGE_API_KEY` and run `python solar_diag.py --all` (or `--real`); results are stored and labeled separately from mock runs.
- **Packet capture: UNVERIFIED** (see 3a); the `packet_loss` case (expected `timeout_or_stall`) is reserved for when loss injection is actually reproduced.

Latest mock result table (Windows 10, Python 3.12.2; all three cases recovered successfully and hash-matched, recorded independently of the diagnosis):

| Case | Expected | Mock diagnosis | Match | Withheld (exp/act) | IDs exist | Supports | Recovery |
|---|---|---|---|---|---|---|---|
| normal | no_failure_observed | no_failure_observed | O | no/no | O | O | success |
| early_close | early_connection_close | early_connection_close | O | no/no | O | O | success |
| insufficient | insufficient_evidence | insufficient_evidence | O | yes/yes | O | O | success |

Parser/evaluator negative tests passed: rejects JSON-less and unknown-diagnosis responses, detects fabricated evidence IDs (exist=X), and distinguishes existing-but-unsupporting citations (exist=O / supports=X). Artifacts: `diag_artifacts/solar/<run>/` (`request.json`, `response_raw.txt`, `parsed.json`, `evaluation.json`).
