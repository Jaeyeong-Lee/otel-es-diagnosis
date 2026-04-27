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

---

## 2026-04-20 — 실험 목적 및 실행 전략 정리

### 각 실험이 답하는 질문

**exp_c — ES 상태 점검**
- 질문: "ES 자체가 지금 건강한가?"
- `thread_pool rejected > 0` → ES가 쓰기 요청을 거부 중
- `index_failed > 0` → 인덱싱 실패 실제로 있음
- mapping 타입 충돌 → 특정 필드에서 도큐먼트 거부
- 한계: ES에 문제가 있다는 건 알 수 있지만, 그게 누락의 원인인지는 단독으로 확정 불가

**exp_a — ES 직접 전송**
- 질문: "ES에 직접 넣으면 누락이 생기나?"
- 누락 없음 → ES 정상. 문제는 Collector 또는 Socket 구간
- 누락 있음 → ES 자체가 문제
- **단독으로 "ES가 문제인지 아닌지" 확정 가능. 가장 먼저 실행.**

**exp_b — Socket 경유 전송**
- 질문: "현재 스택(Socket → Collector → ES) 에서 누락이 생기나?"
- 누락 있음 → Socket 또는 Collector 구간에서 드롭
- 한계: 누락 위치는 exp_a 결과와 비교해야 확정됨

### 실험 간 관계

```
exp_a 정상 + exp_b 누락  →  Socket/Collector 구간 문제  →  filelog 전환 검토
exp_a 누락               →  ES 문제  →  exp_c 결과로 세부 원인 확인
둘 다 정상               →  낮은 부하에서는 재현 안 됨  →  부하 높여서 재실험
```

exp_a 결과만으로 방향이 잡힘. **exp_a를 먼저 돌리고 결과 보고 나머지 결정.**

---

---

## 2026-04-27 — 실험 결과 분석 및 "client 빨리 닫힘" 가설

### 실험 B 결과 (--scenario both로 실행)
- STEADY: 30% 누락
- BURST: -2% 누락 (음수)

### -2% 원인 확정
`--scenario both`로 실행 시 STEADY와 BURST가 **같은 run_id를 공유**하는 버그.
BURST count 시점에 STEADY의 늦게 인덱싱된 잔여 문서가 합산됨 → stored > sent → 음수 누락율.
→ **각 시나리오를 별도로 실행해야 함 (알려진 이슈였으나 미수정 상태)**

### 외부 LLM 의견: "client가 빨리 닫아서"
Collector metrics 확인 결과 receiver/exporter 문제 없음.
"client(Java App)가 소켓을 너무 빨리 닫는 것"이 원인 가능성 제기.

**이 가설의 유효성 검토:**
- 실제로 가능한 메커니즘: TCP close 시 OS 송신 버퍼 미전송 데이터 유실
- **단, exp_b(Python)는 청크마다 연결을 새로 열고 즉시 닫는 구조** → 실제 Log4j2와 다름
- Log4j2 SocketAppender는 persistent connection → "빨리 닫힘"은 JVM 종료 시에만 해당

### 결론 및 다음 액션
exp_b 30% 누락이 실제 운영 문제인지 실험 구현 문제인지 불분명.
**exp_d(Java Log4j2)를 실행해서 구분해야 함:**
- exp_d 누락 없음 → exp_b 구현 아티팩트. 운영 스택은 정상.
- exp_d 누락 있음 → 실제 문제. JVM 종료 타이밍, AsyncAppender flush, reconnect 구간 확인.

### AsyncAppender 관련 추가 확인

현재 앱이 AsyncAppender 사용 중. JVM 종료 시 내부 큐 미소진이 유실 원인일 수 있음.
`shutdownTimeout`은 JVM 종료 시 큐를 비울 때까지 기다리는 최대 시간 (기본값이 짧거나 0).
→ Log4j2 설정만으로 해결 가능. Collector 변경 불필요.
→ 상세: `docs/async_appender_shutdown.md`

미완료:
- [ ] exp_b --scenario steady / burst 각각 별도 실행
- [ ] exp_d (Java) 실행
- [ ] exp_d 누락 없음 → 운영 정상 확인
- [ ] exp_d 누락 있음 → async_appender_shutdown.md 방법 1~3 적용

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
