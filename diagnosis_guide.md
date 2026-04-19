# OTel → ES 데이터 누락 진단 가이드

작성일: 2026-04-20

---

## 1. 현재 스택 및 문제

**스택:**
```
Java App → Log4j2 (ECS JSON) → SocketAppender → OTel Collector (tcplog receiver) → ES 8.x (logs-* index)
```

**현상:**
- 멀티스레드 환경에서 특정 transaction.name 데이터 간헐적 누락
- ES Bulk API HTTP 200 OK 응답에도 불구하고 실제 데이터 유실
- 누락 재현 불가 — 발생 레이어 특정 안 됨

---

## 2. 의심 구간 정리

| 레이어 | 의심 원인 | 특이사항 |
|---|---|---|
| App → Collector | Socket 유실 | TCP ACK ≠ 앱 레벨 처리 확인. 버퍼 초과 시 조용히 드롭 |
| Collector 내부 | Queue overflow | sending_queue 초과 시 드롭 |
| Collector → ES | Bulk API partial error | HTTP 200 안에 `errors: true` 포함 가능, 감지 안 됨 |
| ES | Thread pool rejected | write 스레드 풀 포화 시 인덱싱 거절 |
| ES | Mapping conflict | ECS 필드 타입 불일치로 도큐먼트 거부 |

---

## 3. 대화를 통해 도출된 인사이트

### Socket 구간의 근본 한계
- Log4j2 SocketAppender는 TCP 소켓에 쓰면 "전송 완료"로 간주
- OTel Collector가 실제로 받아서 처리했는지 확인 방법 없음
- Collector가 다운되거나 큐가 차면 해당 순간의 로그는 복구 불가

### tcplog receiver에서 파싱 부재 확인
- App에서 이미 ECS JSON을 만들어서 전송하므로 Collector에서 별도 파싱 없음
- 따라서 파싱 실패로 인한 누락은 가능성 낮음
- 의심 구간: Socket 연결 자체 또는 Collector → ES 구간

### Filelog receiver 방안 도출
- SocketAppender → FileAppender로 변경, filelog receiver로 읽기
- 파일은 디스크에 영속 → Collector 다운돼도 로그 안 사라짐
- filelog가 읽은 offset 추적 → 재시작 시 이어서 처리
- **앱 코드 변경 없음**, log4j2.xml만 수정

### OTLP 방식 (참고용, 당장 적용 불필요)
- OTel Java Agent를 JVM에 붙이면 Log4j2 로그를 OTLP gRPC로 전송
- 앱 레벨 ACK + 내장 retry → 유실 감지 가능
- Collector에 `otlp` receiver 필요 (APM 앱이 이미 쓰고 있다면 기존 설정 재사용 가능)

---

## 4. 실험 계획

### 실험 A — ES 직접 전송 (ES 레이어 격리)
**목적:** Collector를 배제하고 ES 자체 문제인지 확인

```
Python → ES Bulk API 직접 → logs-test 인덱스
```

- `test_run_id` + `event_sequence` 포함 1000건 전송
- Bulk 응답의 `errors`, `items` 상세 분석
- 전송 후 ES에서 count 쿼리로 실제 저장 수 비교

**스크립트:** `exp_a_es_direct.py`

---

### 실험 B — Socket 방식 재현 (현재 스택 시뮬레이션)
**목적:** tcplog receiver 경유 시 누락 발생하는지 확인

```
Python → TCP Socket → OTel Collector tcplog receiver → ES
```

- 동일한 `test_run_id` + `event_sequence` 포함 ECS JSON을 소켓으로 전송
- 전송 후 ES count로 누락율 비교
- 부하 변화 시나리오: 정속(10분/1000건), 버스트(30~40건씩 비정기)

**스크립트:** `exp_b_socket.py`

---

### 실험 C — ES 메트릭 모니터링
**목적:** ES 리소스 문제 여부 확인

- `_cat/thread_pool/write?v` → rejected 카운트
- `_stats` → indexing 실패 수치
- 실험 A/B 실행 중 병행 모니터링

**스크립트:** `exp_c_es_metrics.py`

---

## 5. 결과 해석

### 시나리오 1: 실험 A 누락 없음 + 실험 B 누락 있음
**→ 문제는 Socket/tcplog 구간**

해결 방안:
1. **즉시 적용 (권장):** FileAppender + filelog receiver 전환
2. **중장기:** OTLP Agent 방식 전환

---

### 시나리오 2: 실험 A에서도 누락 발생
**→ 문제는 ES 레이어 (Collector와 무관)**

확인할 것:
- 실험 A 스크립트의 Bulk 응답 분석 결과에서 `errors: true` + `mapper_parsing_exception` 확인
- 실험 C에서 `rejected` 카운트 증가 여부
- `logs-*` 인덱스 매핑 전수 조사 (`GET /logs-*/_mapping`)

해결 방안:
- Mapping conflict → ECS 템플릿 재정의 또는 필드 타입 수정
- Thread pool rejected → ES 리소스 증설 또는 Collector batch 설정 조정

---

### 시나리오 3: 실험 A, B 모두 누락 없음
**→ 문제는 멀티스레드 타이밍 / 특정 조건**

확인할 것:
- 부하 조건 변경 (더 높은 동시성, 더 큰 데이터)
- OTel Collector 로그 레벨 debug로 상향 후 재실험
- 특정 `transaction.name` 패턴에서만 발생하는지 확인

---

## 6. Filelog 전환 시 변경 사항

### log4j2.xml 변경

```xml
<!-- 기존 SocketAppender 대신 또는 병행 -->
<RollingFile name="FileAppender" fileName="/var/log/app/app.log"
             filePattern="/var/log/app/app-%d{yyyy-MM-dd}-%i.log">
    <EcsLayout serviceName="your-service" />
    <Policies>
        <SizeBasedTriggeringPolicy size="100MB"/>
        <TimeBasedTriggeringPolicy />
    </Policies>
    <DefaultRolloverStrategy max="10"/>
</RollingFile>
```

### OTel Collector config 변경

```yaml
receivers:
  tcplog:                      # 기존 유지 (병행 실험용)
    listen_address: "0.0.0.0:54525"
  filelog:                     # 추가
    include: [/var/log/app/*.log]
    start_at: beginning
    operators:
      - type: json_parser
        timestamp:
          parse_from: attributes["@timestamp"]
          layout: '%Y-%m-%dT%H:%M:%S.%LZ'

service:
  pipelines:
    logs/file:
      receivers: [filelog]
      processors: [batch]
      exporters: [elasticsearch]
```

---

## 7. 다음 단계 체크리스트

- [ ] 실험 C 먼저 실행 (ES 기본 상태 확인)
- [ ] 실험 A 실행 (ES 레이어 격리)
- [ ] 실험 B 실행 (Socket 경유, 정속 + 버스트)
- [ ] 결과 비교 → 시나리오 판단
- [ ] 시나리오 1이면: FileAppender 전환 테스트
- [ ] 시나리오 2이면: Bulk 응답 에러 상세 분석
