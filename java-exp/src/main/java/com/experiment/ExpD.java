package com.experiment;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.io.File;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 실험 D: Log4j2 SocketAppender 직접 재현
 * 목적: 실제 운영 스택(Log4j2 + ECS Layout + SocketAppender)과 동일한 방식으로
 *       멀티스레드 로그 전송 후 ES에서 누락율 측정
 *
 * 실험 B(Python socket)와 비교 포인트:
 *   - Log4j2 내부 AsyncAppender 큐 동작
 *   - SocketAppender의 persistent connection 유지 방식
 *   - 실제 ECS Layout이 생성하는 포맷
 *
 * 실행: java -jar target/otel-es-diagnosis-1.0-SNAPSHOT.jar [config.json 경로]
 *       기본값: ../config.json (java-exp 디렉토리에서 실행 시)
 */
public class ExpD {

    private static final Logger log = LogManager.getLogger(ExpD.class);
    private static final ObjectMapper mapper = new ObjectMapper();

    // config.json에서 읽어올 설정
    private static String esHost;
    private static String esIndexPattern;
    private static int totalDocs;
    private static int threads;
    private static int verifyWaitSec;
    private static String collectorHost;
    private static int collectorPort;

    public static void main(String[] args) throws Exception {
        String configPath = args.length > 0 ? args[0] : "../config.json";
        loadConfig(configPath);

        // Log4j2에 Collector 접속 정보 주입
        System.setProperty("collector.host", collectorHost);
        System.setProperty("collector.port", String.valueOf(collectorPort));
        System.setProperty("service.name", "exp-d-java");
        System.setProperty("log4j2.configurationFile", "log4j2-exp.xml");

        // Log4j2 재초기화 (시스템 프로퍼티 적용)
        org.apache.logging.log4j.core.LoggerContext ctx =
                (org.apache.logging.log4j.core.LoggerContext) LogManager.getContext(false);
        ctx.reconfigure();

        String runId = UUID.randomUUID().toString();

        System.out.println("[실험 D] Log4j2 SocketAppender 직접 재현");
        System.out.println("  run_id    : " + runId);
        System.out.println("  collector : " + collectorHost + ":" + collectorPort);
        System.out.println("  es index  : " + esIndexPattern);
        System.out.println("  총 문서   : " + totalDocs + "건 / " + threads + "스레드");
        System.out.println();

        AtomicInteger sent = new AtomicInteger(0);
        CountDownLatch latch = new CountDownLatch(threads);
        ExecutorService executor = Executors.newFixedThreadPool(threads);

        int perThread = totalDocs / threads;
        long startMs = System.currentTimeMillis();

        for (int t = 0; t < threads; t++) {
            final int offset = t * perThread;
            final int count = (t == threads - 1) ? totalDocs - offset : perThread;

            executor.submit(() -> {
                try {
                    for (int i = 0; i < count; i++) {
                        int seq = offset + i;
                        // MDC로 run_id와 sequence 주입 → ECS Layout이 labels에 포함
                        org.apache.logging.log4j.ThreadContext.put("run_id", runId);
                        org.apache.logging.log4j.ThreadContext.put("event.sequence", String.valueOf(seq));
                        log.info("exp-d test doc seq={}", seq);
                        sent.incrementAndGet();
                    }
                } finally {
                    org.apache.logging.log4j.ThreadContext.clearAll();
                    latch.countDown();
                }
            });
        }

        latch.await();
        executor.shutdown();

        double elapsed = (System.currentTimeMillis() - startMs) / 1000.0;
        System.out.printf("  전송 완료: %.1f초%n", elapsed);

        // Log4j2가 내부 버퍼를 flush할 시간 확보
        System.out.println("  Log4j2 flush 대기 3초...");
        Thread.sleep(3000);

        // Log4j2 종료 (SocketAppender flush)
        LogManager.shutdown();

        System.out.println("  ES 반영 대기 " + verifyWaitSec + "초...");
        Thread.sleep(verifyWaitSec * 1000L);

        long stored = countInEs(runId);
        long loss = totalDocs - stored;
        double lossRate = (double) loss / totalDocs * 100;

        System.out.println();
        System.out.println("──────────────────────────────");
        System.out.printf("  전송: %d건%n", totalDocs);
        System.out.printf("  저장: %d건%n", stored);
        System.out.printf("  누락: %d건  (%.2f%%)%n", loss, lossRate);
        System.out.println("──────────────────────────────");

        if (loss == 0) {
            System.out.println("  결과: Log4j2 SocketAppender 경유 누락 없음.");
            System.out.println("        실험 B(Python socket)와 비교하여 차이 확인.");
        } else {
            System.out.println("  결과: 누락 발생.");
            System.out.println("        실험 A(ES 직접) 결과와 비교:");
            System.out.println("          - 실험 A 정상 + 실험 D 누락 → Log4j2 또는 Socket 구간 문제");
            System.out.println("          - 실험 A도 누락 → ES 레이어 문제");
        }
    }

    private static void loadConfig(String path) throws Exception {
        JsonNode cfg;
        File file = new File(path);
        if (file.exists()) {
            cfg = mapper.readTree(file);
        } else {
            // classpath fallback
            try (InputStream is = ExpD.class.getResourceAsStream("/config.json")) {
                if (is == null) throw new RuntimeException("config.json not found: " + path);
                cfg = mapper.readTree(is);
            }
        }
        esHost          = cfg.get("es_host").asText();
        esIndexPattern  = cfg.get("es_index_pattern").asText();
        collectorHost   = cfg.get("collector_host").asText();
        collectorPort   = cfg.get("collector_port").asInt();
        verifyWaitSec   = cfg.get("verify_wait_sec").asInt();

        JsonNode exp    = cfg.get("experiment");
        totalDocs       = exp.get("total_docs").asInt();
        threads         = exp.get("threads").asInt();
    }

    private static long countInEs(String runId) throws Exception {
        HttpClient client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(10))
                .build();

        String query = String.format(
                "{\"query\":{\"term\":{\"labels.run_id\":\"%s\"}}}", runId);

        // logs-* 패턴에서 최근 인덱스를 대상으로 count
        String url = esHost + "/" + esIndexPattern + "/_count";

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(query))
                .timeout(Duration.ofSeconds(10))
                .build();

        try {
            HttpResponse<String> response = client.send(request,
                    HttpResponse.BodyHandlers.ofString());
            JsonNode result = mapper.readTree(response.body());
            return result.get("count").asLong();
        } catch (Exception e) {
            System.err.println("[ERROR] ES count 실패: " + e.getMessage());
            System.err.println("  ES_HOST: " + esHost);
            return -1;
        }
    }
}
