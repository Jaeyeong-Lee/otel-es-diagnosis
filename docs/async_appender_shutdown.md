# AsyncAppender 종료 시 데이터 유실 — 원인 및 해결

## 핵심 요약

AsyncAppender를 사용하면 로그가 앱 스레드 → 내부 큐 → 별도 스레드 → SocketAppender 순으로 전달됩니다.
JVM 종료 시 내부 큐가 비워지기 전에 소켓이 닫히면 큐에 남은 로그가 유실됩니다.
**Log4j2 설정 변경만으로 해결 가능하며, Collector 변경은 불필요합니다.**

---

## 구조

```
App Thread → [AsyncAppender 내부 큐] → Worker Thread → SocketAppender → Collector
                                                 ↑
                              JVM 종료 시 이 Worker가 큐를 다 비우기 전에 종료되면 유실
```

---

## shutdownTimeout이란

AsyncAppender(또는 AsyncLogger)가 JVM 종료 시 내부 큐를 drain하기 위해 기다리는 최대 시간.

- 기본값: Log4j2 2.x 기준 `0`ms (즉시 종료) 또는 버전마다 다름
- 값이 너무 짧으면 큐에 남은 로그를 버리고 종료 → **데이터 유실**

---

## exp_d에서 누락이 있다면

Collector receiver/exporter 메트릭이 정상이고 exp_d에서 누락이 나오면,
**JVM 종료 시 AsyncAppender 큐 미소진**이 원인일 가능성이 높습니다.

확인 방법:
- 앱 정상 운영 중(종료 없이)에는 누락이 없는지 확인
- JVM 종료 직전 로그에서 누락이 집중되는지 확인

---

## 해결 방법 (Log4j2 설정만으로 가능)

### 방법 1 — shutdownTimeout 설정 (AsyncAppender 사용 시)

```xml
<Configuration shutdownTimeout="5000">  <!-- 종료 시 최대 5초 대기 -->
    <Appenders>
        <Async name="Async" shutdownTimeout="5000">
            <AppenderRef ref="SocketAppender"/>
        </Async>
    </Appenders>
</Configuration>
```

### 방법 2 — AsyncLogger 전역 timeout 설정 (AsyncLogger 사용 시)

```xml
<!-- log4j2.xml -->
<Configuration>
    <Properties>
        <Property name="log4j2.asyncLoggerTimeout">5000</Property>
    </Properties>
</Configuration>
```

또는 JVM 옵션:
```bash
-Dlog4j2.asyncLoggerTimeout=5000
```

### 방법 3 — 명시적 shutdown 호출 (가장 확실)

앱 종료 로직에서 Log4j2를 마지막에 명시적으로 종료:

```java
// Spring Boot라면 @PreDestroy 또는 shutdown hook
Runtime.getRuntime().addShutdownHook(new Thread(() -> {
    // 다른 리소스 정리 먼저
    LogManager.shutdown();  // 마지막에 호출
}));
```

`log4j2.xml`에서 자동 shutdown hook은 비활성화:
```xml
<Configuration shutdownHook="disable">
```

---

## 현재 상황 정리

| 실험 | 결과 | 해석 |
|---|---|---|
| exp_b STEADY | 30% 누락 | exp_b 구현 문제 가능성 (청크마다 연결 close) |
| exp_b BURST | -2% (run_id 버그) | 무효 결과, 별도 실행 필요 |
| exp_d | 미실행 | **우선 실행 필요** |

## 다음 액션

1. `exp_b --scenario steady` 단독 실행
2. `exp_b --scenario burst` 단독 실행
3. `exp_d` (Java) 실행

**exp_d 누락 없음** → exp_b 구현 아티팩트. 운영 정상.
**exp_d 누락 있음** → 위 방법 1~3 중 적용. Collector 변경 불필요.
