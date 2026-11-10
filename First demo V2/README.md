## 1차 구현 · 이어받기를 지원하는 TCP 파일 전송

TCP 서버와 클라이언트는 같은 컴퓨터의 `127.0.0.1`에서 동작합니다. 전송에는 Python 표준 라이브러리만 사용합니다.

| 파일 | 역할 |
|---|---|
| `ftprotocol.py` | 공통 전송 규칙을 정의합니다. 길이 정보가 앞에 붙는 JSON 프레임, 지정된 바이트 수만큼 읽는 `recv_exact`, SHA-256 계산, JSONL 기록 기능을 제공합니다. |
| `ftserver.py` | 파일 하나를 요청받은 바이트 위치부터 전송합니다. `--inject-at N`을 지정하면 첫 연결을 N바이트 지점에서 한 번 끊습니다. |
| `ftclient.py` | 받은 데이터를 `.part` 임시 파일에 저장합니다. 연결의 조기 종료, 리셋, 수신 시간 초과를 감지하며, 접속이나 헤더 교환에 실패하면 재시도합니다. 전송이 끝나면 파일 크기와 SHA-256을 검증합니다. |
| `demo.py` | 명령 하나로 테스트 파일을 만들고, 정상 전송과 장애 주입 전송을 차례로 실행한 뒤 결과를 검증하고 실행을 정리합니다. |
| `test_error_paths.py` | 오류 상황을 검사합니다. 재접속 실패와 응답 헤더가 중간에 끊기는 상황을 거친 뒤 전송을 재개하는 테스트가 포함됩니다. |

클라이언트는 요청 프레임에 `{"file_id","offset"}`을 보냅니다. 서버는 응답 헤더 `{"status","file_id","offset","size","sha256"}`를 보낸 뒤 구분자 없이 파일 본문 `size-offset`바이트를 이어서 보냅니다. TCP에서는 한 번의 `recv` 호출에서 헤더 일부만 들어올 수 있으므로 `recv_exact`로 필요한 바이트를 모두 읽습니다. 재접속할 때마다 디스크에서 `.part`의 크기를 다시 읽고, 실제 저장된 바이트 수를 재개 위치로 요청합니다.

```bash
python demo.py              # 정상 전송과 장애 주입을 검사하며, 모두 통과하면 종료 코드 0
python test_error_paths.py  # 오류 상황 5개를 검사하며, 모두 통과하면 종료 코드 0
```

검증 환경은 Windows 10, Git Bash, Python 3.12.2입니다. 4,194,304바이트 파일을 사용했고, SHA-256은 `5bf7ee3d…e55f4c`입니다.

- **정상 전송.** 해시가 일치했고 재접속은 0회였습니다.
- **1,398,101바이트 지점에서 장애 주입.** `Connection interrupted → Reconnected → Resuming from byte 1398101 → Transfer completed → SHA-256 verified` 순서로 실행됐습니다. 최종 해시가 일치했고 장애 주입 기록은 정확히 1건이었습니다.
- **오류 처리.** 출력 파일이 이미 있으면 보존하고 종료 코드 3을 반환합니다. 재시도 횟수를 모두 쓰면 `.part`를 보존하고 종료 코드 1을 반환합니다. 파일 식별값이 바뀌면 종료 코드 2를 반환합니다. 잘못된 위치나 파일을 요청하면 서버가 각각 `INVALID_OFFSET`, `FILE_NOT_FOUND`로 응답합니다.

실행별 JSONL 로그는 `demo_artifacts/<case>/logs/`에 저장합니다. 관측 로그인 `server.jsonl`과 `client.jsonl`에는 시각, 이벤트 유형, 바이트 위치를 기록합니다. 장애 주입 기록은 `injection.jsonl`에 `class: "injection"`으로 따로 저장합니다. 이 기록은 모델에 전달하는 관측 자료에서 제외하고, 나중에 진단 결과를 채점하는 기준으로 사용합니다.

## 2차 구현 · 처음부터 받기와 이어받기 비교

`ftclient.py --recovery-mode resume|restart`로 복구 방식을 선택합니다. 기본값은 `resume`입니다. 재접속한 뒤 파일 식별값을 다시 확인합니다. `restart`는 `.part`를 지우고 0바이트부터 요청하며, `resume`은 실제로 저장된 위치부터 요청합니다. 두 방식에 같은 파일 식별값 검증, 재시도 제한, 오류 처리, 최종 검증을 적용합니다.

```bash
python compare_recovery.py   # 두 방식에 같은 파일, 중단 위치, 시간 제한, 재시도 설정을 사용
```

비교 지표는 실제 로그에서 집계합니다.

- **재접속 요청 위치**는 첫 `RECONNECTED` 이후 클라이언트가 보낸 `REQUEST`의 `offset`입니다.
- **재접속 후 전송량**은 첫 연결을 제외한 모든 연결에서 서버가 기록한 `bytes_sent`의 합입니다. 프로토콜 헤더를 제외한 파일 본문만 집계하며, 여러 번 재접속한 경우 모두 더합니다.
- **복구 시간**은 첫 `RECONNECTED`부터 `TRANSFER_COMPLETED`까지입니다. 두 방식 모두 클라이언트에서 경과 시간을 재는 시계인 단조 시계의 `mono` 값을 사용합니다. 파일 식별값을 다시 확인하는 시간과 후속 재시도 시간은 포함하고, 마지막 해시 검증 시간은 제외합니다.

같은 환경에서 4,194,304바이트 파일을 1,398,101바이트 지점에서 끊어 측정했습니다.

| 방식 | 재접속 요청 위치 | 재접속 후 전송량 (예상값) | 복구 시간 |
|---|---|---|---|
| restart | 0 | 4,194,304 B (= 4,194,304) | 15–78 ms |
| resume | 1,398,101 | 2,796,203 B (= 2,796,203) | 15–47 ms |

이어받기는 재접속 후 전송량을 정확히 1,398,101 B(약 33%) 줄였습니다. 남은 전송량은 파일 크기에서 재개 위치를 뺀 값으로, 실행마다 같은 절감량이 나왔습니다. 루프백 환경에서 두 방식의 시간 차이는 측정 시계의 해상도 범위 안에 있어, 확인된 개선 효과는 전송량 절감입니다. 두 지표의 요약은 `compare_artifacts/summary.json`에 저장합니다.

## 3차 구현 A · 장애 진단을 위한 관측 자료 수집

`observe_run.py`는 서버와 클라이언트를 실행한 뒤 진단용 자료를 모읍니다. Python 프로그램은 Solar와 독립적으로 복구를 수행합니다.

```bash
python observe_run.py            # 애플리케이션 로그로 정상, 조기 종료, 증거 부족 사례를 관측
python test_packet_capture.py    # Wireshark 없이 패킷 파서와 캡처 상태를 검사
```

실행마다 `diag_artifacts/run-<ts>-<n>/`에 `evidence.jsonl`, `run_meta.json`, 원본 로그를 저장합니다. 애플리케이션 관측 자료에는 시각, 이벤트 유형, 바이트 위치가 들어갑니다. 파일 경로, 식별자, 해시, 장애 주입 기록은 별도 로그에 보관합니다. 증거 부족 사례인 `insufficient`에서는 연결과 전송 시작 이벤트만 공개하고 패킷 캡처를 생략합니다.

### 데모 패킷 캡처하기

Wireshark의 Dumpcap과 TShark를 사용합니다. Windows에서 루프백 패킷을 캡처하려면 Npcap이 필요합니다. 인터페이스 목록을 확인해 해당 컴퓨터의 루프백 장치를 선택하세요. 아래 명령은 Windows의 기본 Wireshark 설치 경로를 사용합니다.

```powershell
& "$env:ProgramFiles\Wireshark\dumpcap.exe" -D
python observe_run.py --capture-interface '\Device\NPF_Loopback'
python solar_diag.py --all --mock
```

Linux에서는 `--capture-interface lo`를 사용합니다. macOS에서 일반적인 루프백 이름은 `lo0`입니다. `dumpcap -D` 목록에 표시된 루프백 인터페이스 번호를 입력해도 됩니다. 프로그램은 데모 포트에서 로컬 주소끼리 주고받은 IPv4 TCP 패킷만 캡처합니다. 발표용 컴퓨터에서 기본값인 5초가 부족하면 `--capture-seconds 10`을 사용하세요.

- `capture.pcapng`는 Wireshark에서 열 수 있습니다. `packet_summary.json`에는 TCP 스트림별 집계와 SYN, SYN/ACK, FIN, RST, 재전송으로 추정되는 패킷의 프레임 번호가 담깁니다. `P1` 같은 패킷 근거 ID는 캡처 파일의 프레임 번호를 가리킵니다.
- `run_meta.json`에는 도구 발견 여부, 캡처 상태, 요약 생성 상태를 각각 기록합니다. 전송 실험 전체가 캡처 범위에 들어오고 TShark가 조건에 맞는 TCP 패킷을 찾은 경우에만 `capture.available`이 참이 됩니다. 캡처를 요청한 사례에서 패킷 근거를 확보하지 못하면 종료 코드 1을 반환합니다. 애플리케이션 관측 자료와 오류 내용은 파일에 남습니다.
- [실제 캡처 예시(Sample Evidence)](Sample%20Evidence/)에서 정상 전송의 TCP 스트림 1개와 조기 종료 사례의 스트림 2개를 비교할 수 있습니다. 조기 종료 사례의 애플리케이션 기록에는 첫 스트림이 닫힌 뒤 클라이언트가 `1398101`바이트부터 요청한 내용이 있습니다.

## 3차 구현 B · Solar 진단과 평가

`solar_diag.py`는 요청을 보내기 전에 패킷 요약과 허용된 애플리케이션 이벤트를 읽습니다. 응답에는 `{diagnosis, evidence_ids, withheld, missing_evidence}` 형식을 요구합니다. Solar에 보내는 자료에는 원본 캡처 바이트를 제외하고 요약을 담습니다.

```bash
python solar_diag.py --all        # 전체 실행을 진단하고 사전 기준에 따라 채점
python solar_diag.py --run <id>   # 지정한 실행 하나만 진단
```

진단 결과는 관측할 수 있는 범위에서 다음 네 가지로 구분합니다.

- `early_connection_close`는 예상 크기에 도달하기 전에 연결이 닫힌 경우입니다. 예를 들어 `INTERRUPTED reason="connection closed"` 또는 `CONNECTION_CLOSED file_pos < size`가 관측될 수 있습니다. 이후 `REQUEST offset>0`으로 이어받기를 요청한 기록이 남을 수도 있습니다.
- `timeout_or_stall`은 수신 대기 시간이 초과된 경우입니다. 예를 들어 `recv timeout`이 기록됩니다.
- `no_failure_observed`는 중단 기록 없이 전송 완료와 해시 검증이 확인된 경우입니다.
- `insufficient_evidence`는 확정할 근거가 부족한 경우입니다. 진단을 보류하고 어떤 증거가 더 필요한지 적습니다.

평가 기준은 사례별 기대 진단, 기대 보류 여부, 진단을 뒷받침하는 이벤트 유형으로 미리 정합니다. **인용한 근거 ID가 실제로 존재하는지**와 **그 근거가 진단을 뒷받침하는지**를 각각 평가합니다. Python의 복구 결과는 LLM 응답이나 API 상태와 독립적으로 별도 열에 기록합니다.

### 검증 현황

- **모의 검증 3/3 통과.** `UPSTAGE_API_KEY` 없이 로컬 규칙 기반 응답을 사용해 자료 조회, 요청 생성, 응답 파싱, 채점 흐름을 확인했습니다. 모의 검증의 범위는 이 처리 흐름입니다.
- **실제 Solar API 호출은 검증 대기 중입니다.** `UPSTAGE_API_KEY`를 설정하고 `python solar_diag.py --all --real`을 실행해 확인할 수 있습니다. 같은 실행을 다시 진단하면 이전 진단 결과 파일을 덮어씁니다.
- **정상 전송과 조기 종료의 루프백 캡처를 확인했습니다.** 조기 종료 사례는 서버가 전송 중 소켓을 닫아 재현합니다. 통제된 패킷 손실은 별도 실험입니다.

아래는 Windows 10과 Python 3.12.2에서 실행한 모의 결과입니다. 세 사례 모두 최종 파일의 해시가 원본과 일치했습니다. 파일 전송 결과는 진단 결과와 별도로 기록했습니다.

| 사례 | 기대 진단 | 모의 진단 | 일치 | 보류 (기대/실제) | 근거 ID 존재 | 진단을 뒷받침함 | 전송 결과 |
|---|---|---|---|---|---|---|---|
| normal | no_failure_observed | no_failure_observed | O | 아니요/아니요 | O | O | 성공 |
| early_close | early_connection_close | early_connection_close | O | 아니요/아니요 | O | O | 성공 |
| insufficient | insufficient_evidence | insufficient_evidence | O | 예/예 | O | O | 성공 |

파서와 평가기의 오류 사례도 검사했습니다. JSON이 빠졌거나 정의되지 않은 진단을 반환한 응답을 거부하고, 존재하지 않는 근거 ID를 감지합니다(존재 X). ID는 존재하지만 진단을 뒷받침하지 못한 경우도 구분합니다(존재 O, 지지 X). 진단 결과물인 `request.json`, `response_raw.txt`, `parsed.json`, `evaluation.json`은 `diag_artifacts/solar/<run>/`에 저장합니다.
