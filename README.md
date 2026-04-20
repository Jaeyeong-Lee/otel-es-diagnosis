# OTel-ES 데이터 누락 진단

## 파일 구성

```
otel-es-diagnosis/
├── config.json                     ← 설정값 중앙 관리 (여기만 수정)
├── config.py                       ← Python용 config.json 로더
├── exp_a_es_direct.py              ← 실험 A: ES 직접 전송 (ES 레이어 격리)
├── exp_b_socket.py                 ← 실험 B: Socket → tcplog 재현
├── exp_c_es_metrics.py             ← 실험 C: ES 메트릭 모니터링
├── java-exp/                       ← 실험 D: Java Log4j2 SocketAppender 직접 재현
│   ├── pom.xml
│   └── src/main/java/com/experiment/ExpD.java
├── collector_filelog_snippet.yaml  ← filelog receiver 전환 설정
├── log4j2_file_appender.xml        ← FileAppender 전환 설정
└── docs/                           ← 상세 문서
```

---

## 설정 변경 (config.json)

모든 실험 스크립트(Python, Java)는 `config.json`을 공유합니다. **여기만 수정하면 됩니다.**

```json
{
  "es_host": "http://localhost:9200",
  "es_index_direct": "logs-test",
  "es_index_pattern": "logs-*",
  "collector_host": "localhost",
  "collector_port": 54525,
  "verify_wait_sec": 10,
  "experiment": {
    "total_docs": 1000,
    "threads": 10,
    "bulk_size": 50,
    "steady_interval_sec": 0.6,
    "burst_size_min": 30,
    "burst_size_max": 40,
    "burst_interval_min": 1.0,
    "burst_interval_max": 5.0
  }
}
```

---

## Python 환경 준비

```bash
# 가상환경 생성 (최초 1회)
python3 -m venv .venv

# 활성화 (매 세션마다)
source .venv/bin/activate        # macOS / Linux
.venv\Scripts\activate           # Windows

# 의존성 설치 (최초 1회)
pip install -r requirements.txt
```

---

## Java 환경 준비 및 빌드

```bash
cd java-exp

# 빌드 (최초 1회 또는 코드 변경 시)
mvn package -q

# 빌드 확인
ls target/otel-es-diagnosis-1.0-SNAPSHOT.jar
```

---

## 실행 순서

### 1. ES 기본 상태 확인 (실험 전 baseline)

```bash
python exp_c_es_metrics.py --mode once
```

### 2. 실험 A — ES 레이어 격리

```bash
python exp_a_es_direct.py
```

### 3. 실험 B — 현재 스택(Python socket) 재현

```bash
# 정속 + 버스트 모두
python exp_b_socket.py --scenario both

# 각각 별도 실행 (결과를 명확히 분리할 때)
python exp_b_socket.py --scenario steady
python exp_b_socket.py --scenario burst
```

### 4. 실험 D — Log4j2 SocketAppender 직접 재현 (Java)

```bash
cd java-exp
java -jar target/otel-es-diagnosis-1.0-SNAPSHOT.jar
# config.json 경로 명시가 필요한 경우
java -jar target/otel-es-diagnosis-1.0-SNAPSHOT.jar ../config.json
```

### 5. 실험 중 ES 모니터링 (별도 터미널)

```bash
python exp_c_es_metrics.py --mode monitor --interval 5

# 매핑 상세 확인
python exp_c_es_metrics.py --mode mapping
```

---

## ECS 필드 일치 여부

각 실험이 ES에 저장하는 필드 구조와 **카운트 쿼리 기준 필드**:

| 필드 | exp_a (Python) | exp_b (Python) | exp_d (Java) |
|---|---|---|---|
| **카운트 기준** | `labels.run_id` | `labels.run_id` | `labels.run_id` |
| timestamp | `@timestamp` | `@timestamp` | `@timestamp` |
| log level | `log.level` | `log.level` | `log.level` |
| service | `service.name` | `service.name` | `service.name` |
| 시퀀스 | `event.sequence` (숫자) | message에 포함 | message에 포함 |

- **카운트 쿼리 기준 필드(`labels.run_id`)는 세 실험 모두 동일** → 누락율 비교 유효
- Java ECS Layout은 ThreadContext(MDC)를 `labels.*`로 매핑하므로 `run_id` → `labels.run_id` ✓
- 점(.) 포함 ThreadContext 키는 ES `labels` 매핑 오류 유발 → Java에서는 사용 안 함

---

## 결과 해석

| 실험 A | 실험 B / D | 결론 |
|---|---|---|
| 누락 없음 | 누락 있음 | Socket/Collector 구간 문제 → filelog 전환 검토 |
| 누락 있음 | — | ES 레이어 문제 → exp_c 결과 확인 |
| 누락 없음 | 누락 없음 | 낮은 부하에서 재현 안 됨 → 부하 높여서 재실험 |

상세 해석 → `docs/decision_tree.md`
