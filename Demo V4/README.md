# 네트워크 연결 장애 감지 및 자동 복구 시스템

Team Connecting · 컴퓨터 네트워크 팀 프로젝트

[진행 현황과 다음 검증 계획](PROGRESS.md)에서 실제 Solar 반복 평가 결과, 부분 데이터 무결성 기준, 수신 시간 초과의 원인 진단 계획을 확인할 수 있습니다.

## 문제 배경

대학 건물과 같은 환경에서는 네트워크 연결이 불안정해 데이터 전송이 도중에 끊길 수 있습니다.

## 실험 자료와 방법

통제된 환경에서 네트워크 장애를 재현합니다. 패킷 캡처와 애플리케이션 로그를 수집해 TCP 재전송, 응답 지연, 연결 상태 변화를 분석합니다.

Upstage Solar LLM은 관측 자료를 바탕으로 장애를 진단합니다. Python 프로그램은 서버에 다시 연결해 전송을 이어갑니다. 사전에 정한 기준에 따라 진단의 정확도와 복구 결과를 평가합니다.

## 목표 결과물

네트워크 장애 감지, LLM 기반 진단, 자동 재접속, 파일 전송 재개를 통합한 프로토타입입니다.

## 첫 번째 데모

데모 소스 코드는 [First Demo](First%20Demo/) 폴더에 있습니다. 프로젝트 최상위 폴더에서 아래 명령으로 이동한 뒤 데모를 실행합니다.

```bash
cd "First Demo"
```

## v3 실험 순서

### 2026년 11월 24일 실제 Solar 연동 결과

조기 종료 사례 1회에서 실제 `solar-pro4`의 `resume` 제안을 Python이 적용하고, 1,398,101바이트부터 이어받아 4,194,304바이트 파일의 크기와 SHA-256 일치를 확인했습니다. 전송 후 실제 Solar는 `early_connection_close`로 진단했고, 정답·인용 ID 존재·근거 지지·판단 보류 여부의 평가 기준을 충족했습니다. 실제 응답 모델은 `solar-pro4-260806`입니다.

| 확인 항목 | 결과 |
|---|---|
| 실제 복구 전략 응답 및 적용 | `resume`, `source=solar` |
| 이어받기 위치 | 1,398,101 B |
| 최종 파일 검증 | 크기 및 SHA-256 일치 |
| 실제 장애 진단 | `early_connection_close` |
| 근거 평가 | 인용 ID 존재 및 진단 지지 통과 |
| 패킷 캡처 | 이번 실행에서는 요청하지 않음 |

[실제 응답과 로그](Sample%20Evidence/solar_real_early_close/)에서 결과를 확인할 수 있습니다. 앞선 실행에서 Solar는 완료·해시 검증 이벤트만 인용해 `no_failure_observed`로 오판했습니다. 전체 관측 구간의 중단을 진단하도록 지침을 보완한 후 새 실행에서 위 결과를 얻었습니다. 각 실행의 원래 요청·응답과 결과표를 함께 보관했습니다. 현재 실제 연동 검증 범위는 조기 종료이며, 정상·시간 초과·증거 부족 조건의 반복 평가는 후속 과제입니다.

### 실제 Solar 진단과 복구 전략 제안 통합 실행

`First Demo` 폴더에서 아래 명령을 실행합니다. 환경 변수에 키가 없으면 가려진 입력창에서 API 키를 입력합니다. 기본 조기 종료 사례는 복구 전략 제안과 사후 장애 진단으로 실제 API를 총 두 번 요청합니다.

```powershell
python solar_demo.py
```

정상·조기 종료·수신 시간 초과·증거 부족을 모두 확인하려면 다음과 같이 실행합니다. 기본 전략 제안 횟수는 3회, 사후 진단 횟수는 4회입니다.

```powershell
python solar_demo.py --case normal early_close stall insufficient
```

패킷 캡처를 함께 요청하려면 `--capture-interface '\Device\NPF_Loopback'`을 추가합니다. 결과는 실행마다 새 시스템 임시 폴더에 저장하며 터미널에 경로를 표시합니다. `--art-dir`로 새 결과 폴더를 지정할 수도 있습니다.

`solar_demo_summary.json`에는 실제 응답 수신, Solar 제안 적용, 파일 검증을 기록합니다. 실패 후 기본 설정으로 복구한 경우에도 `solar_strategy_applied`가 false여서 전체 성공으로 집계되지 않습니다. 정상 전송은 전략 제안이 필요하지 않습니다. 실제 진단 결과는 `solar/<시각>-REAL/comparison.csv`에 기록합니다.

`logs/client.jsonl`의 `SOLAR_ADVICE_REQUEST`, `SOLAR_ADVICE_RESPONSE`, `SOLAR_ADVICE_FAILED`에 제안 요청, 실제 응답과 모델·토큰 사용량·경과 시간, 실패 사유를 기록합니다. 최종 적용 여부는 `RECOVERY_DECISION`의 `source=solar`로 확인합니다. 이 기록은 사후 진단용 허용 이벤트 목록에서 제외되어, 진단 입력에는 기존 관측 자료만 전달됩니다. 전략 제안은 기존 설정대로 최대 3초 기다리고, 실패하면 Python이 기본 복구 설정을 적용합니다.

오프라인 검사는 `python test_recovery_strategy.py`와 `python test_solar_diagnosis.py`로 실행합니다. 외부 응답을 대체해 실제 호출 경로의 요청 형식, 기록, 오류 처리와 채점을 검사하며 실제 Solar 성능은 별도 호출 결과로 확인해야 합니다.

VS Code에서 이 프로젝트를 열고 내장 터미널을 PowerShell로 선택합니다. 아래 명령은 `First Demo` 폴더에서 한 줄씩 실행하세요. `python --version`으로 Python이 실행되는지 확인합니다. VS Code에서 이미 `$py`에 Python 경로를 설정했다면, 아래 명령의 `python`을 `& $py`로 바꿔 실행하세요.

### 1. 기존 세 사례로 실제 Solar 확인

이 작업 폴더에는 첫 실험용 관측 결과 세 개를 `First Demo/diag_artifacts/pilot`에 준비했습니다. 해당 폴더에 결과가 이미 있으면 API 키 입력으로 넘어가세요. 다른 PC에서 새로 실행할 때는 아래 명령으로 세 사례를 수집합니다.

```powershell
python observe_run.py --case normal early_close insufficient --capture-interface '\Device\NPF_Loopback' --art-dir .\diag_artifacts\pilot
python solar_diag.py --all --mock --art-dir .\diag_artifacts\pilot
```

정상과 조기 종료에서 `summary=ready available=True`를 확인합니다. 증거 부족에서는 시작 기록만 남기므로 `capture=withheld`가 나옵니다. `--mock`의 `PASS`는 규칙 기반 진단과 결과표 생성까지 확인한 결과입니다.

실제 Solar를 사용하려면 [Upstage 콘솔](https://console.upstage.ai/)의 API 키를 PowerShell 입력창에 넣습니다. 아래 명령은 키를 터미널 세션의 환경 변수에 저장합니다. 키는 콘솔의 입력 프롬프트에서 입력하세요.

```powershell
$env:UPSTAGE_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'Upstage API key' -AsSecureString)).Password
python solar_diag.py --all --real --art-dir .\diag_artifacts\pilot
```

이 실행은 세 사례에 대해 실제 Solar를 각각 호출합니다. 기본 모델은 `solar-pro4`입니다. 출력된 `comparison.csv` 경로를 열면 규칙과 Solar의 판정, 근거 ID, 해시 검증 결과를 한 줄에서 비교할 수 있습니다.

- **정상**은 `no_failure_observed`입니다. 인용한 앱 로그에 전송 완료와 해시 검증이 모두 있는지 확인합니다.
- **조기 종료**는 `early_connection_close`입니다. `INTERRUPTED`에서 저장량이 전체 크기보다 작은지 확인합니다. 패킷 ID를 인용했다면, 해당 프레임이 어느 연결의 어떤 방향인지 Wireshark에서 확인합니다.
- **증거 부족**은 `insufficient_evidence`와 판단 보류입니다. `missing_evidence`에 어떤 자료가 더 필요한지 적혀 있는지 확인합니다.

앱 근거 ID `E...`는 해당 실행의 `evidence.jsonl`에서 찾을 수 있습니다. 패킷 근거 ID `P...`의 숫자는 `capture.pcapng`에 있는 Wireshark 프레임 번호입니다.

### 2. 전송 중 데이터가 멈추는 경우 확인

```powershell
python observe_run.py --case stall --capture-interface '\Device\NPF_Loopback' --art-dir .\diag_artifacts\stall-check
python solar_diag.py --all --mock --art-dir .\diag_artifacts\stall-check
```

서버는 파일의 처음 3분의 1을 보내고 연결을 열어 둔 채 최대 2.5초 기다립니다. 클라이언트는 1초 동안 본문이 들어오지 않으면 시간 초과를 기록하고 재접속합니다. 성공 기준은 `scenario_reproduced=True`, `resume_offset=1398101`, `hash_match=True`, 진단 `timeout_or_stall`입니다. 초기 실험에 실제 Solar 응답도 남기려면 두 번째 명령에 `--real`을 사용합니다. 이 실험의 지연은 서버에서 의도적으로 만들며, 결과에는 관측된 수신 시간 초과를 기록합니다.

[`Sample Evidence/stall`](Sample%20Evidence/stall/) 예시에서는 `E008`에 수신 시간 초과가 기록됐고, Wireshark 75번 프레임에서 클라이언트가 첫 연결을 닫기 시작했습니다. 79번 프레임에서 새 연결을 요청했고, `E012`에서 1,398,101바이트부터 이어받았습니다. 프레임 번호와 증거 ID는 실행마다 달라질 수 있습니다.

### 3. 네 조건을 5회씩 반복하고 결과표 만들기

파일 크기, 캡처 설정, 모델, 정답 기준을 정한 뒤 새 폴더에 20개 사례를 수집합니다. 같은 이름의 폴더에 다시 수집하면 실행이 추가되므로, 평가를 처음부터 다시 할 때는 `evaluation-02`처럼 새 이름을 사용하세요.

```powershell
python observe_run.py --repeat 5 --capture-interface '\Device\NPF_Loopback' --art-dir .\diag_artifacts\evaluation-01
python solar_diag.py --all --mock --art-dir .\diag_artifacts\evaluation-01
python solar_diag.py --all --real --art-dir .\diag_artifacts\evaluation-01
Remove-Item Env:UPSTAGE_API_KEY
```

패킷을 요청한 15회에는 각각 기본 5초 캡처 시간을 사용합니다. 실제 Solar 단계는 키가 설정된 상태에서 20회를 호출하므로, 파일 수집과 모의 결과표를 먼저 확인한 뒤 실행하세요. 새 터미널을 열었거나 키를 지웠다면 1단계의 입력 명령으로 다시 설정합니다. 진단에 실패한 호출도 CSV에 남아 분모에 포함됩니다.

각 비교표는 `<관측 결과 폴더>/solar/<실행 시각>-REAL/comparison.csv` 또는 `<실행 시각>-MOCK/comparison.csv`에 저장됩니다. 실제 실행에서는 `solar_correct`로 정답을, `solar_evidence_supports`로 자동 기준 충족을, `transfer_verified`로 전송 및 해시 검증을 확인합니다. `human_review`에는 팀원이 인용 근거를 직접 확인한 결과를 적습니다. 조기 종료와 시간 초과의 복구는 `resume_offset`도 함께 확인하세요. 증거 부족은 관측 자료를 가린 조건이므로, 그 행의 전송 성공은 내부 실험에서 얻은 값입니다.

API 요청 형식과 지원 모델은 [Upstage Chat Completions 문서](https://console.upstage.ai/api/chat)에서 확인할 수 있습니다.

## 1차 구현 · 이어받기를 지원하는 TCP 파일 전송

TCP 서버와 클라이언트는 같은 컴퓨터의 `127.0.0.1`에서 동작합니다. 전송에는 Python 표준 라이브러리만 사용합니다.

| 파일 | 역할 |
|---|---|
| `ftprotocol.py` | 공통 전송 규칙을 정의합니다. 길이 정보가 앞에 붙는 JSON 프레임, 지정된 바이트 수만큼 읽는 `recv_exact`, SHA-256 계산, JSONL 기록 기능을 제공합니다. |
| `ftserver.py` | 파일 하나를 요청받은 바이트 위치부터 전송합니다. `--inject-at N`을 지정하면 첫 연결을 N바이트 지점에서 한 번 끊습니다. |
| `ftclient.py` | 서버와 헤더를 교환하고 받은 데이터를 `.part` 임시 파일에 저장합니다. 전송이 끝나면 파일 크기와 SHA-256을 검증합니다. |
| `monitor.py` | 연결 종료, 리셋, 수신 시간 초과를 감지하고 장애 단계, 사유, 저장된 바이트 수를 기록합니다. |
| `recovery_policy.py` | 허용된 복구 전략(`resume`, `restart`)과 남은 재시도 횟수를 확인합니다. |
| `recovery.py` | 재접속을 진행하고 임시 파일의 실제 크기로 재개 위치를 정합니다. `restart`에서는 서버의 파일 식별값이 확인된 뒤 임시 데이터를 비웁니다. |
| `demo.py` | 명령 하나로 테스트 파일을 만들고, 정상 전송과 장애 주입 전송을 차례로 실행한 뒤 결과를 검증하고 실행을 정리합니다. |
| `test_error_paths.py` | 오류 상황을 검사합니다. 재접속 실패와 응답 헤더가 중간에 끊기는 상황을 거친 뒤 전송을 재개하는 테스트가 포함됩니다. |

클라이언트는 요청 프레임에 `{"file_id","offset"}`을 보냅니다. 서버는 응답 헤더 `{"status","file_id","offset","size","sha256"}`를 보낸 뒤 구분자 없이 파일 본문 `size-offset`바이트를 이어서 보냅니다. TCP에서는 한 번의 `recv` 호출에서 헤더 일부만 들어올 수 있으므로 `recv_exact`로 필요한 바이트를 모두 읽습니다. 재접속할 때마다 디스크에서 `.part`의 크기를 다시 읽고, 실제 저장된 바이트 수를 재개 위치로 요청합니다.

기본 복구 방식은 클라이언트의 `--recovery-mode` 옵션에서 정합니다. 전송 중 전략 제안은 `--strategy-source`로 활성화할 수 있습니다. 전송 후 장애 진단은 수집된 관측 자료를 읽어 별도로 실행합니다.

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
python observe_run.py            # 정상, 조기 종료, 시간 초과, 증거 부족 사례를 관측
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

### 전송 중 복구 전략 제안

`--strategy-source mock`은 재접속 전에 모의 제안을 한 번 받고, Python에서 허용된 전략인지 확인합니다. 저장한 부분의 길이가 수신 기록과 일치하면 `resume`을 받아들입니다. 길이가 달라지면 `restart`로 처리하며, 재접속한 뒤 서버의 파일 크기와 SHA-256 식별값을 확인한 후 임시 파일을 초기화합니다. 완성 파일은 기존 크기 및 SHA-256 검증을 거칩니다.

`First Demo` 폴더에서 기존 실험 결과를 보존하면서 모의 제안을 시연할 수 있습니다.

```bash
python3 observe_run.py --case early_close --strategy-source mock --art-dir ./diag_artifacts/strategy-mock
```

`strategy=resume source=mock`, `resume_offset=1398101`, `hash_match=True`를 확인합니다. 적용한 전략과 사유는 `client.jsonl`의 `RECOVERY_DECISION`에 기록됩니다.

- `fixed`는 기본값이며 기존 `--recovery-mode`대로 동작합니다.
- `solar`는 `UPSTAGE_API_KEY`로 같은 형식의 전략 제안을 요청합니다. 수신 증상, 저장 바이트 수, 수신 기록상 저장 위치, 예상 파일 크기, 남은 재시도 횟수를 보냅니다.
- 제안을 받을 수 없거나 응답 검증에 실패하면 기본 복구 설정을 사용합니다. 부분 파일의 길이 변경을 감지한 경우에는 `restart`로 복구합니다. Solar 제안 대기에 쓰는 시간은 전송당 최대 3초입니다.
- 같은 길이를 유지한 내용 변경은 최종 SHA-256 검증으로 확인합니다.

### 패킷 한 개 손실 실험

같은 4 MiB 파일을 정상 조건과 서버에서 클라이언트로 가는 TCP 데이터 패킷 한 개를 버리는 조건에서 전송합니다. 두 조건 모두 Linux의 임시 네트워크 네임스페이스 안에서 `127.0.0.1`만 사용합니다. 패킷은 루프백 캡처에서 관측된 뒤, 클라이언트 TCP가 받기 전에 폐기됩니다. 이 규칙은 실험 프로세스가 사용하는 임시 네임스페이스에 적용됩니다.

Ubuntu 또는 Ubuntu 기반 WSL 2 터미널에서 한 번만 필요한 도구를 설치합니다.

```bash
sudo apt install iproute2 ethtool nftables tshark
```

`First Demo` 폴더에서 실험을 시작합니다. sudo 권한은 임시 네트워크와 캡처 설정에 사용합니다.

```bash
sudo python3 packet_loss_experiment.py
```

| 조건 | 드롭 수 | Wireshark 재전송 표시 | 전송을 완료한 TCP 연결 |
|---|---:|---|---|
| `normal` | 0 | 없음 | 기존 연결 1개 |
| `one_drop` | 1 | 1건 이상 | 기존 연결 1개 |

두 조건 모두 요청 오프셋은 `[0]`이고, 재접속 기록 없이 최종 파일 크기와 SHA-256이 같아야 통과합니다. 실행 결과는 `loss_artifacts/run-…/summary.json`에 기록합니다. 각 조건의 `capture.pcapng`를 Wireshark에서 열고 `tcp.analysis.retransmission || tcp.analysis.fast_retransmission || tcp.analysis.spurious_retransmission` 필터로 재전송 표시를 확인할 수 있습니다. `packet_summary.json`의 프레임 번호 및 `logs/client.jsonl`의 전송 완료 기록과 대조합니다.

기존 `early_close` 사례에서는 캡처의 TCP 스트림이 2개이며, 새 연결에서 0보다 큰 오프셋을 요청합니다. 두 실험을 비교하면 같은 TCP 연결 안의 재전송과 새 연결의 파일 이어받기를 구분할 수 있습니다.

## 3차 구현 B · Solar 진단과 평가

`solar_diag.py`는 요청을 보내기 전에 패킷 요약과 허용된 애플리케이션 이벤트를 읽습니다. Solar에 보내는 자료에는 원본 캡처 바이트를 제외하고 요약을 담습니다. 응답에는 기존 `diagnosis`, `evidence_ids`, `withheld`, `missing_evidence`에 더해 아래 관측 필드를 요구합니다.

| 필드 | 의미 |
|---|---|
| `interruption_evidence_ids` | 관측 구간 전체의 `INTERRUPTED` 이벤트 ID |
| `transfer_outcome` | 완료·해시 검증을 모두 확인하면 `verified_complete`, 확인할 수 없으면 `not_verified` |
| `outcome_evidence_ids` | 완료와 해시 검증을 뒷받침하는 이벤트 ID |

조기 종료 후 복구한 실행은 중단 근거와 `verified_complete`를 함께 반환할 수 있습니다. 비교표의 `solar_observations_correct`는 모델이 중단 이벤트를 모두 식별하고 최종 결과를 올바른 근거로 판정했는지 나타냅니다. 기존 진단·근거 평가는 유지하며, 실제 실행의 전체 통과에는 새 관측 평가도 포함합니다. 이전 형식의 저장된 응답은 기본 파서로 읽을 수 있으며, 새 실제 호출에서는 확장 필드를 필수로 검사합니다.

이 형식은 동일한 네 사례를 세 번 재진단해 12건 중 11건이 맞았고 조기 종료 판정이 달라진 결과를 바탕으로 추가했습니다. 새 형식의 실제 반복 평가에서는 진단·근거·관측 구분 12/12가 통과했고, 새로 생성한 네 사례에서도 4/4가 통과했습니다. [실제 요청과 평가 자료](Sample%20Evidence/solar_evaluation/)에서 변경 전후 응답과 각 평가 열을 비교할 수 있습니다. 데이터 무결성과 타임아웃 원인 설명의 보완 사항은 [진행 기록](PROGRESS.md)에 정리했습니다.

```bash
python solar_diag.py --all        # 규칙 기반 진단으로 전체 실행을 채점
python solar_diag.py --run <id>   # 지정한 실행 하나만 진단
```

진단 결과는 관측할 수 있는 범위에서 다음 네 가지로 구분합니다.

- `early_connection_close`는 예상 크기에 도달하기 전에 연결이 닫힌 경우입니다. 예를 들어 `INTERRUPTED reason="connection closed"` 또는 `CONNECTION_CLOSED file_pos < size`가 관측될 수 있습니다. 이후 `REQUEST offset>0`으로 이어받기를 요청한 기록이 남을 수도 있습니다.
- `timeout_or_stall`은 수신 대기 시간이 초과된 경우입니다. 예를 들어 `recv timeout`이 기록됩니다.
- `no_failure_observed`는 중단 기록 없이 전송 완료와 해시 검증이 확인된 경우입니다.
- `insufficient_evidence`는 확정할 근거가 부족한 경우입니다. 진단을 보류하고 어떤 증거가 더 필요한지 적습니다.

평가 기준은 사례별 기대 진단, 기대 보류 여부, 진단을 뒷받침하는 이벤트 유형으로 미리 정합니다. **인용한 근거 ID가 실제로 존재하는지**와 **그 근거가 진단을 뒷받침하는지**를 각각 평가합니다. Python의 복구 결과는 LLM 응답이나 API 상태와 독립적으로 별도 열에 기록합니다.

### 검증 현황

- **v2 모의 검증 3/3 통과.** 로컬 규칙 기반 응답으로 아래 세 사례의 처리 흐름을 확인했습니다. v3의 네 사례 검증 절차는 문서 위쪽에 있습니다.
- **실제 Solar API 연동을 조기 종료 사례에서 확인했습니다.** 실제 전략 적용과 사후 장애 진단 모두 통과했습니다. 위쪽 실제 연동 결과와 샘플을 참고하세요. `python solar_diag.py --all --real`은 지정한 관측 자료를 실제 모델로 진단하며, 실행마다 별도의 시각별 폴더에 결과를 저장합니다.
- **정상 전송과 조기 종료의 루프백 캡처를 확인했습니다.** 조기 종료 사례는 서버가 전송 중 소켓을 닫아 재현합니다. 통제된 패킷 손실은 별도 실험입니다.

아래는 Windows 10과 Python 3.12.2에서 실행한 모의 결과입니다. 세 사례 모두 최종 파일의 해시가 원본과 일치했습니다. 파일 전송 결과는 진단 결과와 별도로 기록했습니다.

| 사례 | 기대 진단 | 모의 진단 | 일치 | 보류 (기대/실제) | 근거 ID 존재 | 진단을 뒷받침함 | 전송 결과 |
|---|---|---|---|---|---|---|---|
| normal | no_failure_observed | no_failure_observed | O | 아니요/아니요 | O | O | 성공 |
| early_close | early_connection_close | early_connection_close | O | 아니요/아니요 | O | O | 성공 |
| insufficient | insufficient_evidence | insufficient_evidence | O | 예/예 | O | O | 성공 |

파서는 잘못된 응답 형식을 거부하고, 평가기는 존재하지 않거나 진단을 뒷받침하지 않는 근거 ID를 판별합니다. 사례별 `request.json`, `rule.json`, 실제 호출 시의 `response_raw.json`과 `solar.json`은 진단 폴더 안의 실행 ID 하위 폴더에 있습니다. 호출 또는 응답 검증이 실패하면 `error.txt`를 기록합니다.
