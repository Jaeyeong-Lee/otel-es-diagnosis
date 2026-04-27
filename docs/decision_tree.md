# 결과 해석 및 의사결정 트리

## 실험 결과에 따른 판단 흐름

```
실험 A 실행 (ES 직접 전송)
│
├── 누락 발생
│   │
│   ├── Bulk 응답에 mapper_parsing_exception
│   │   └── → [ES-1] ECS 매핑 충돌
│   │
│   ├── Bulk 응답에 es_rejected_execution_exception
│   │   └── → [ES-2] Write thread pool 포화
│   │
│   └── Bulk 응답 에러 없는데 누락
│       └── → VERIFY_WAIT_SEC 10~30초로 늘려서 재실험
│           └── 그래도 누락 → [ES-3] 기타 ES 내부 문제
│
└── 누락 없음
    │
    실험 B — STEADY / BURST 각각 별도 실행
    │  (주의: --scenario both는 run_id 공유로 count 오염됨 → 별도 실행 필수)
    │
    ├── STEADY 누락 있음
    │   │
    │   실험 D 실행 (Java Log4j2 SocketAppender)
    │   │
    │   ├── 누락 없음  → [EXP_B_ARTIFACT] exp_b 구현 문제 (연결 즉시 close)
    │   │               실제 운영 스택은 정상. exp_b 코드 수정 필요.
    │   │
    │   └── 누락 있음  → [SOCK-REAL] 실제 운영 스택에서도 드롭 발생
    │                    → [SOCK-1] 확인 항목 참고
    │
    ├── BURST만 누락   → [SOCK-2] 버스트 시 Collector 큐 초과
    │
    └── 둘 다 없음
        └── → [UNKNOWN] 낮은 부하에서는 재현 안 됨
            → 더 높은 동시성으로 재실험
            → Collector debug 로그 활성화
            → 특정 transaction.name 패턴 필터 확인
```

---

## 시나리오별 조치

### [ES-1] ECS 매핑 충돌

**확인:**
```bash
# 매핑 조회
python exp_c_es_metrics.py --mode mapping

# 직접 확인
curl http://localhost:9200/logs-*/_mapping | python -m json.tool | grep -A3 "type"
```

**원인:** 동일 필드가 다른 인덱스에서 서로 다른 타입으로 정의됨.
예) `event.sequence`가 어떤 인덱스에서는 `long`, 다른 곳에서는 `keyword`.

**조치:**
1. 문제 필드 특정 후 ILM/인덱스 템플릿에서 타입 통일
2. 기존 인덱스는 reindex 필요 (운영 중이면 신규 인덱스부터 적용)

```bash
# 인덱스 템플릿 확인
curl http://localhost:9200/_index_template/logs-*
```

---

### [ES-2] Write Thread Pool 포화

**확인:**
```bash
curl "http://localhost:9200/_cat/thread_pool/write?v"
# rejected 컬럼이 0보다 크면 포화 상태
```

**원인:** ES write 스레드 풀이 인덱싱 요청을 처리하지 못하고 거절.
보통 bulk 요청 크기가 너무 크거나 ES 리소스 부족.

**조치 (단기):**
- OTel Collector의 `batch` processor에서 `send_batch_size` 줄이기 (예: 1000 → 200)
- `timeout` 늘리기

```yaml
processors:
  batch:
    timeout: 10s
    send_batch_size: 200
```

**조치 (중기):**
- ES 노드 리소스 점검 (heap, CPU)
- write thread pool 크기 조정 (`thread_pool.write.size`)

---

### [EXP_B_ARTIFACT] exp_b 구현 아티팩트

**원인:** exp_b(Python)는 청크마다 TCP 연결을 새로 열고 즉시 닫습니다.
실제 Log4j2 SocketAppender는 하나의 연결을 유지하므로 동작 방식이 다릅니다.

```python
# exp_b의 현재 구조 (문제)
with socket.create_connection(...) as s:
    s.sendall(data)
# with 종료 → 즉시 close() → Collector가 다 읽기 전에 연결 끊김 가능
```

**의미:** exp_b에서 30% 누락이 나왔더라도 exp_d(Java)에서 누락이 없으면
실제 운영 환경은 정상이고, 실험 방법론의 문제입니다.

---

### [SOCK-REAL] 실제 SocketAppender에서 드롭

**exp_d에서도 누락이 발생하는 경우 확인할 것:**

1. **JVM 종료 타이밍 (가장 흔한 원인)**
   - JVM shutdown hook 실행 시 Log4j2가 닫히면서 내부 버퍼가 flush되기 전에 소켓 close
   - log4j2.xml에 `shutdownHook="disable"` 설정 후 앱 코드에서 명시적으로 `LogManager.shutdown()` 호출

2. **AsyncAppender 큐 미소진**
   - AsyncAppender 내부 LMAX Disruptor 큐에 쌓인 로그가 flush되기 전에 종료
   - `log4j2.xml`에서 AsyncAppender의 `shutdownTimeout` 설정 확인

3. **reconnect 중 드롭**
   - Collector 재시작 또는 네트워크 순단 시 reconnect 전 로그 유실
   - `reconnectionDelayMillis` 값 확인, 재연결 중 로그를 별도 버퍼에 보관하는 구조 검토

4. **SO_LINGER 설정**
   - `l_linger=0`이면 close() 즉시 RST 전송 → 미전송 데이터 유실
   - JVM 기본값은 linger 없음이나, 네트워크 장비/설정에 따라 다를 수 있음

---

### [SOCK-1/2] Socket 구간 드롭

**원인:**
- Collector 과부하 시 tcplog receiver 큐 초과
- 네트워크 순단 시 SocketAppender가 재연결 전 데이터 유실

**조치:** filelog receiver 전환

```
변경 파일:
  log4j2_file_appender.xml        → FileAppender 추가
  collector_filelog_snippet.yaml  → filelog receiver 파이프라인 추가
```

**전환 전략 (안전하게):**
1. SocketAppender + FileAppender 병행 운영
2. 실험 B를 filelog 기준으로 재실행, 누락율 비교
3. 누락 사라지면 SocketAppender 제거

---

### [UNKNOWN] 재현 불가

**추가 실험:**

1. **더 높은 동시성:**
```python
# exp_b_socket.py 설정 조정
SCENARIO_STEADY = {
    "total": 5000,
    "threads": 50,
    "interval_sec": 0.1,
}
```

2. **Collector debug 로그 활성화:**
```yaml
# otelcol config.yaml
service:
  telemetry:
    logs:
      level: debug
```
재시작 후 실험 B 실행, Collector 로그에서 drop/error 메시지 확인.

3. **Collector 메트릭 확인:**
```bash
curl http://collector-host:8888/metrics | grep -E "dropped|failed|refused"
```

4. **특정 필드 패턴 확인:**
- Collector pipeline에 `filter` processor가 있는지 확인
- 특정 `transaction.name` 값이 필터링되고 있을 가능성
```bash
grep -i "filter\|drop" /path/to/collector/config.yaml
```
