# 실험 상세 가이드

## 실험 전 준비

### 의존성 설치

```bash
pip install requests
```

### 설정값 확인 (각 스크립트 상단)

| 스크립트 | 변경 필요 설정 |
|---|---|
| `exp_a_es_direct.py` | `ES_HOST`, `INDEX` |
| `exp_b_socket.py` | `COLLECTOR_HOST`, `COLLECTOR_PORT`, `ES_HOST`, `ES_INDEX` |
| `exp_c_es_metrics.py` | `ES_HOST`, `TARGET_INDEX` |

### tcplog receiver 포트 확인

OTel Collector 설정 파일에서 `tcplog` receiver 포트 확인:

```yaml
receivers:
  tcplog:
    listen_address: "0.0.0.0:54525"   # 이 포트를 exp_b의 COLLECTOR_PORT에 입력
```

---

## 실험 C — ES 메트릭 모니터링 (먼저 실행)

**목적:** 실험 전 ES 기준선(baseline) 확인. 실험 중 병행 실행하면 부하 시 변화 포착 가능.

```bash
# 1회 점검 (실험 전 baseline)
python exp_c_es_metrics.py --mode once

# 실험 A/B 실행 중 병행 모니터링
python exp_c_es_metrics.py --mode monitor --interval 5

# 매핑 상세 확인 (타입 충돌 의심 시)
python exp_c_es_metrics.py --mode mapping
```

### 주요 확인 항목

**thread_pool/write 출력 예시:**
```
node_name    name   active  queue  rejected  completed
my-node      write       2      0         0     123456
```
- `rejected > 0` → ES write 스레드 풀 포화 → 인덱싱 거절 발생 중

**index stats:**
- `index_failed > 0` → 실제 인덱싱 실패 있음 → Bulk 응답 에러 분석 필요

**cluster health:**
- `yellow` → unassigned replica shard 존재 (단일 노드면 정상)
- `red` → primary shard 문제 → 즉시 조사 필요

---

## 실험 A — ES 직접 전송

**목적:** Collector를 완전히 배제하고 ES만 격리해서 테스트.

```bash
python exp_a_es_direct.py
```

### 동작 흐름

```
Python (멀티스레드) → ES Bulk API → logs-test 인덱스
                    ↓
         응답의 errors/items 분석
                    ↓
         VERIFY_WAIT_SEC 후 ES count 쿼리
                    ↓
         전송 수 vs 저장 수 비교
```

### 출력 예시

```
[실험 A] ES 직접 전송 테스트
  run_id  : f47ac10b-58cc-4372-a567-0e02b2c3d479
  index   : logs-test
  총 문서  : 1000건  /  스레드: 10개  /  bulk size: 50

  전송 완료: 3.2초
  Bulk API 에러 항목 수: 3건

  [Bulk 에러 상세]
    seq=142  status=400  error={'type': 'mapper_parsing_exception', ...}
    seq=381  status=400  error={'type': 'mapper_parsing_exception', ...}
    ...

  ES 반영 대기 5초...

──────────────────────────────
  전송: 1000건
  저장: 997건
  누락: 3건  (0.30%)
──────────────────────────────
  결과: ES 레이어에서 누락 발생. ...
```

### 해석

| 결과 | 의미 |
|---|---|
| 누락 0건, Bulk 에러 0건 | ES 레이어 정상 |
| 누락 있음 + `mapper_parsing_exception` | ECS 매핑 타입 충돌 → 매핑 전수 조사 |
| 누락 있음 + `es_rejected_execution_exception` | write thread pool 포화 → ES 리소스 점검 |
| 누락 있음 + 에러 없음 | 비동기 인덱싱 지연 가능 → `VERIFY_WAIT_SEC` 늘려서 재시도 |

### 설정 조정

```python
TOTAL_DOCS = 1000       # 총 전송 건수
THREADS = 10            # 동시 스레드 수
BULK_SIZE = 50          # 한 번의 Bulk 요청 크기
VERIFY_WAIT_SEC = 5     # 전송 후 ES 반영 대기 시간
```

---

## 실험 B — Socket 방식 재현

**목적:** 현재 Log4j2 SocketAppender 스택을 Python으로 재현. 실험 A 결과와 비교.

```bash
# 두 시나리오 모두
python exp_b_socket.py --scenario both

# 정속만
python exp_b_socket.py --scenario steady

# 버스트만
python exp_b_socket.py --scenario burst
```

### 시나리오 설명

**STEADY (정속):**
- 목적: 일반적인 운영 트래픽 모사
- 기본값: 1000건, 10개 스레드, 건당 0.6초 간격 (10분에 1000건)
- 누락 발생 시: 지속적인 소켓 연결에서도 드롭이 생기는지 확인

**BURST (버스트):**
- 목적: 급격한 트래픽 증가 시 Collector 큐 초과 여부 확인
- 기본값: 500건, 30~40건씩 1~5초 간격으로 비정기 전송
- 누락 발생 시: 버스트 시점에 Collector 큐가 넘치는지 확인

### 실험 B vs 실험 A 비교 해석

| 실험 A | 실험 B | 결론 |
|---|---|---|
| 누락 없음 | 누락 있음 | **Socket/tcplog 구간 문제** → filelog 전환 검토 |
| 누락 있음 | 누락 있음 | ES 레이어 문제 (Socket 추가 문제일 수도) |
| 누락 없음 | 누락 없음 | 낮은 부하에서는 정상 → 더 높은 부하로 재실험 또는 특정 조건 탐색 |
| 누락 있음 | 누락 없음 | 비정상적 — ES_INDEX 설정 확인 필요 |

### 주의: BURST 시나리오 별도 run_id

현재 코드에서 STEADY와 BURST가 같은 `run_id`를 공유하므로 `--scenario both` 실행 시 count가 섞입니다.
정확한 측정을 위해 각각 별도로 실행하는 것을 권장합니다:

```bash
python exp_b_socket.py --scenario steady
# 결과 확인 후
python exp_b_socket.py --scenario burst
```

---

## Collector 메트릭 확인 (실험 C 보완)

OTel Collector가 `8888` 포트로 Prometheus 메트릭을 노출하는 경우 아래로 직접 확인 가능:

```bash
curl http://collector-host:8888/metrics | grep -E "dropped|failed|queue"
```

**핵심 메트릭:**

| 메트릭명 | 의미 |
|---|---|
| `otelcol_exporter_send_failed_log_records` | Collector → ES 전송 실패 건수 |
| `otelcol_processor_dropped_log_records` | processor 단계 드롭 건수 |
| `otelcol_exporter_queue_size` | 현재 export 큐 크기 |
| `otelcol_receiver_refused_log_records` | receiver가 거부한 건수 |

이 메트릭이 0이 아니면 Collector 자체에서 드롭이 발생하고 있는 것입니다.

---

## 실험 후 filelog 전환 테스트

실험 B에서 누락이 확인된 경우 (`docs/architecture.md` 참고):

### 1. log4j2.xml 수정

`log4j2_file_appender.xml` 참고해서 FileAppender 추가 (SocketAppender와 병행 가능).

### 2. Collector 설정 수정

`collector_filelog_snippet.yaml` 참고해서 `filelog` receiver 및 파이프라인 추가.

### 3. 동일 조건으로 실험 B 재실행

같은 STEADY / BURST 조건으로 재실행 후 누락율 비교.
누락이 사라지면 → Socket이 원인이었음 확정.
