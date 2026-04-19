# 진단 과정 로그

> 이 파일은 문제 분석 과정에서 나눈 대화와 판단 흐름을 기록합니다.
> 새로운 컨텍스트에서 이어서 작업할 때 빠르게 상황을 파악하기 위한 용도입니다.

---

## 2026-04-20 — 초기 분석 및 실험 설계

### 시작점: 문제 정의

**현상:** 멀티스레드 환경에서 특정 `transaction.name`을 포함한 데이터가 간헐적으로 누락됨.
ES Bulk API에서 HTTP 200 OK를 받고 있음에도 실제 데이터가 유실되는 상황.

**현재 스택:**
```
Java App → Log4j2 (ECS JSON) → SocketAppender → OTel Collector (tcplog) → ES 8.x (logs-*)
```

---

### 주요 고민 흐름

#### 1. tcplog에서 파싱 없음 확인

초기에 "Collector에서 ECS JSON 파싱 실패로 누락되는 것 아닌가?" 라는 가설이 있었으나,
앱에서 이미 ECS JSON을 만들어서 보내고 있으므로 Collector는 별도 파싱을 하지 않음.
→ **파싱 실패에 의한 누락 가능성 낮음**으로 정리.

#### 2. Socket 방식의 근본 한계 발견

대화 중 Socket(SocketAppender)의 ACK 구조를 짚어봄:
- TCP ACK = OS 버퍼 수준의 확인. 앱이 실제로 처리했다는 의미가 아님
- Log4j2 SocketAppender는 소켓에 쓰면 "전송 완료"로 간주
- Collector가 다운되거나 큐가 차면 해당 순간 로그는 복구 불가
- 현재 구조에서는 누락이 발생해도 감지할 방법이 없음

#### 3. OTLP 프로토콜 도입 가능성 검토

OTLP(OpenTelemetry Protocol)로 전환하면 앱 레벨 ACK와 내장 retry가 가능함을 확인.
두 가지 방법 검토:
- **방법 1:** OpenTelemetry Log4j2 Appender (의존성 추가 + log4j2.xml 수정)
- **방법 2:** Java Auto-instrumentation Agent (JVM 옵션 추가만, 코드 변경 없음)

**결론:** Agent 방식이 적합. 이유:
- 현재 앱에 OTel SDK 없음 → Appender 방식은 의존성 추가 필요
- Agent는 JVM 옵션 하나로 Log4j2 로그 자동 캡처 가능
- 이미 APM 앱에서 OTLP 사용 중 → 내부망 경로 검증됨, Collector에 `otlp` receiver 있을 가능성 높음

*단, 당장 전환하지는 않기로 함. 먼저 문제 레이어를 특정하는 게 우선.*

#### 4. Filelog Receiver 방안 도출

"Socket이 문제라면 File로 우회하면 되지 않나?" 라는 아이디어에서 출발.

**파일 방식의 핵심 이점:**
- 파일은 디스크에 영속 → Collector가 죽어도 로그 안 사라짐
- filelog receiver가 읽은 offset을 추적 → 재시작 시 이어서 처리
- 앱 코드 변경 없음, `log4j2.xml`만 수정
- ECS 포맷 그대로 유지 가능

**진단 도구로도 활용 가능:**
- filelog 전환 후 누락이 사라지면 → Socket 구간이 원인
- 여전히 누락이 있으면 → Collector → ES 구간이 원인

#### 5. 실험 설계 방향 결정

레이어를 격리해서 순서대로 테스트하는 방식 채택:

```
실험 C (ES 기준선) → 실험 A (ES 격리) → 실험 B (Socket 경유) → 비교
```

실험 B에서 누락 발생 시 → filelog 전환 후 동일 조건으로 재실험.

---

### 현재 미결 사항

| 항목 | 상태 | 비고 |
|---|---|---|
| Collector config 확인 | 미확인 | `otlp` receiver 존재 여부, `elasticsearch` exporter 설정 |
| tcplog receiver 포트 | 미확인 | `exp_b_socket.py` 실행 전 확인 필요 |
| Collector 메트릭 포트 | 미확인 | 기본값 8888, 활성화 여부 확인 필요 |
| exp_b run_id 버그 | 알려진 이슈 | STEADY/BURST 병행 시 count 섞임 → 각각 별도 실행으로 우회 |
| Collector debug 로그 | 미설정 | 누락 재현 시 활성화 예정 |

---

### 다음 액션 (2026-04-21 사내망 실험)

1. `exp_c_es_metrics.py --mode once` → ES 기준선 확인
2. `exp_a_es_direct.py` → ES 레이어 격리 테스트
3. `exp_b_socket.py --scenario steady` → 현재 스택 재현
4. `exp_b_socket.py --scenario burst` → 버스트 조건 재현
5. A vs B 비교 → `docs/decision_tree.md` 참고해서 다음 단계 결정

---

## 향후 기록 형식

```
## YYYY-MM-DD — [실험명 또는 변경 내용]

### 실험 조건
- 시나리오:
- 설정값:

### 결과
- 전송:     건
- 저장:     건
- 누락:     건 (  %)

### 관찰 사항
- 

### 결론 / 다음 단계
- 
```
