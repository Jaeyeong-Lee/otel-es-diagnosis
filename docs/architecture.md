# 현재 아키텍처 및 프로토콜 분석

## 현재 스택

```
┌─────────────────┐     ECS JSON      ┌──────────────────────────┐     Bulk API     ┌────────────┐
│   Java App      │ ──────────────▶   │   OTel Collector         │ ──────────────▶  │     ES     │
│  (Log4j2)       │   TCP Socket      │   (tcplog receiver)      │                  │   8.x      │
│  SocketAppender │                   │   → elasticsearch exporter│                  │  logs-*    │
└─────────────────┘                   └──────────────────────────┘                  └────────────┘
```

### 각 구간 상세

**App → Collector (Socket)**
- Log4j2 `SocketAppender`가 TCP 소켓으로 연결 유지
- ECS JSON 포맷으로 직렬화 후 전송 (Log4j2 ECS Layout 사용)
- 한 줄에 JSON 하나, `\n` 구분
- OTel Collector의 `tcplog` receiver가 수신

**Collector 내부**
- `tcplog` receiver → (별도 파싱 없음, ECS JSON을 body로 수신) → `batch` processor → `elasticsearch` exporter
- App에서 이미 ECS JSON을 만들어 보내므로 Collector에서 파싱 단계 없음

**Collector → ES (Bulk API)**
- `elasticsearch` exporter가 ES Bulk API로 배치 전송
- HTTP 200 응답을 받아도 응답 body 안에 `errors: true`가 있을 수 있음
- 기본 설정에서는 이 partial error를 감지하지 않을 수 있음

---

## Socket 방식의 구조적 한계

### ACK 단계별 비교

```
[TCP ACK]  App의 OS 버퍼 → Collector의 OS 버퍼 도달 확인
           └ 여기까지만 보장됨

[미보장]   Collector가 실제로 읽었는가?
           Collector 내부 큐에 들어갔는가?
           ES로 전송 성공했는가?
           ES가 실제로 인덱싱했는가?
```

### 유실 발생 가능 지점

| 지점 | 상황 | 감지 가능? |
|---|---|---|
| App 소켓 버퍼 | Collector 다운 또는 연결 끊김 | SocketAppender 예외 (간헐적) |
| tcplog receiver 큐 | Collector 과부하 | 감지 불가 |
| Collector sending_queue | ES 느릴 때 큐 초과 | Collector 메트릭 필요 |
| ES Bulk API | partial error (errors: true) | 응답 파싱 필요 |
| ES write thread pool | rejected | ES 메트릭 필요 |
| ES 매핑 충돌 | 필드 타입 불일치 | Bulk 응답 items 분석 필요 |

---

## 대안 프로토콜 비교

### 방안 1: Filelog Receiver (권장 — 단기)

```
┌─────────────┐   ECS JSON    ┌──────────┐    ┌──────────────────────┐    ┌────────┐
│  Java App   │ ──────────▶   │  파일    │ ←─ │  OTel Collector      │ ─▶ │  ES    │
│  Log4j2     │   FileAppender│  (디스크)│    │  (filelog receiver)  │    │        │
└─────────────┘               └──────────┘    └──────────────────────┘    └────────┘
```

**장점:**
- 파일은 디스크에 영속 → Collector 다운돼도 로그 유실 없음
- filelog receiver가 읽은 offset 추적 → 재시작 시 이어서 처리
- 앱 변경 최소 (`log4j2.xml`만 수정, 코드 변경 없음)
- ECS 포맷 그대로 유지

**단점:**
- 디스크 I/O 추가 발생
- 파일 로테이션 정책 관리 필요
- Collector → ES 구간 유실은 여전히 감지 어려움

---

### 방안 2: OTLP (권장 — 중장기)

```
┌─────────────┐   OTLP gRPC   ┌──────────────────────────┐    ┌────────┐
│  Java App   │ ──────────▶   │  OTel Collector          │ ─▶ │  ES    │
│  Log4j2     │               │  (otlp receiver)         │    │        │
│  +OTel Agent│               └──────────────────────────┘    └────────┘
└─────────────┘
```

**OTLP 특징:**
- 앱 레벨 ACK: Collector가 "수신 완료" 응답을 반환
- 내장 retry + 지수 백오프
- backpressure: Collector 느리면 앱에 신호
- protobuf 직렬화: JSON보다 파싱 오버헤드 없음, 페이로드 작음

**전환 방법 (코드 변경 없음):**
```bash
java -javaagent:opentelemetry-javaagent.jar \
     -Dotel.exporter.otlp.endpoint=http://collector-host:4317 \
     -Dotel.service.name=your-service \
     -jar your-app.jar
```

**OTel Collector 설정 추가:**
```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: "0.0.0.0:4317"
      http:
        endpoint: "0.0.0.0:4318"
```

**전제 조건:**
- 내부망에서 Collector 4317 포트 접근 가능 여부 확인
- 기존 APM 앱이 이미 OTLP를 사용하고 있다면 동일 경로 재사용 가능
- Collector `elasticsearch` exporter에 `mapping.mode: ecs` 설정 필요

---

## 프로토콜 선택 기준

| 상황 | 권장 |
|---|---|
| 빠르게 Socket 문제 해결, 앱 배포 최소화 | Filelog receiver |
| 트레이스 연동도 같이 할 예정 | OTLP Agent |
| 현재 APM 앱이 OTLP 사용 중 | OTLP Agent (경로 재사용) |
| 디스크 용량/I/O 여유 없음 | OTLP Agent |
