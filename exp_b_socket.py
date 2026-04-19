"""
실험 B: Socket → tcplog receiver → ES (현재 스택 시뮬레이션)
목적: 현재 Log4j2 SocketAppender 방식 재현 후 누락율 측정
      실험 A 결과와 비교해서 누락이 Socket 구간에서 발생하는지 확인

시나리오:
  1. STEADY  — 정속 전송 (1000건, 10분 분산)
  2. BURST   — 비정기 버스트 (30~40건씩 불규칙 간격)
"""

import json
import socket
import time
import uuid
import threading
import random
import argparse
from datetime import datetime, timezone

import requests

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
COLLECTOR_HOST = "localhost"
COLLECTOR_PORT = 54525       # OTel Collector tcplog receiver 포트
ES_HOST = "http://localhost:9200"
ES_INDEX = "logs-*"          # 실제 저장되는 인덱스 패턴
VERIFY_WAIT_SEC = 10         # 전송 후 ES 반영 대기

# 시나리오별 설정
SCENARIO_STEADY = {
    "total": 1000,
    "threads": 10,
    "interval_sec": 0.6,     # 건당 평균 간격 (10분에 1000건 ≈ 0.6초)
}
SCENARIO_BURST = {
    "total": 500,
    "burst_size_range": (30, 40),
    "burst_interval_range": (1.0, 5.0),   # 버스트 사이 대기 (초)
    "threads": 5,
}

# ──────────────────────────────────────────────
# 전역 상태
# ──────────────────────────────────────────────
run_id = str(uuid.uuid4())
send_count = 0
send_lock = threading.Lock()


def build_ecs_line(seq: int) -> str:
    """Log4j2 ECS layout과 동일한 포맷의 JSON 한 줄 생성."""
    doc = {
        "@timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "ecs": {"version": "1.6.0"},
        "log": {"level": "INFO", "logger": "exp.b.test"},
        "message": f"exp-b socket test seq={seq}",
        "service": {"name": "exp-b-test"},
        "labels": {
            "run_id": run_id,
        },
        "event": {
            "sequence": seq,
        },
        "process": {
            "thread": {"name": threading.current_thread().name}
        },
    }
    return json.dumps(doc)


def send_via_socket(lines: list[str]):
    """TCP 소켓으로 ECS JSON 라인 전송. 연결은 매 청크마다 재사용 또는 재연결."""
    try:
        with socket.create_connection((COLLECTOR_HOST, COLLECTOR_PORT), timeout=10) as s:
            for line in lines:
                s.sendall((line + "\n").encode("utf-8"))
    except Exception as e:
        print(f"  [WARN] 소켓 전송 실패: {e}")


def steady_worker(seqs: list[int], interval: float):
    for seq in seqs:
        line = build_ecs_line(seq)
        send_via_socket([line])
        with send_lock:
            global send_count
            send_count += 1
        time.sleep(interval)


def burst_worker(seqs: list[int]):
    i = 0
    while i < len(seqs):
        burst_size = random.randint(*SCENARIO_BURST["burst_size_range"])
        chunk = seqs[i:i + burst_size]
        lines = [build_ecs_line(s) for s in chunk]
        send_via_socket(lines)
        with send_lock:
            global send_count
            send_count += len(chunk)
        i += burst_size
        wait = random.uniform(*SCENARIO_BURST["burst_interval_range"])
        time.sleep(wait)


def count_in_es(index: str) -> int:
    query = {"query": {"term": {"labels.run_id": run_id}}}
    try:
        resp = requests.post(f"{ES_HOST}/{index}/_count", json=query, timeout=10)
        return resp.json().get("count", -1)
    except Exception as e:
        print(f"  [ERROR] ES count 실패: {e}")
        return -1


def run_steady():
    cfg = SCENARIO_STEADY
    total = cfg["total"]
    threads_n = cfg["threads"]
    interval = cfg["interval_sec"]

    print(f"[시나리오 STEADY]  총 {total}건 / {threads_n}스레드 / 건당 {interval}초 간격")
    seqs = list(range(total))
    chunk = total // threads_n
    thread_seqs = [seqs[i:i + chunk] for i in range(0, total, chunk)]

    start = time.time()
    ts = [threading.Thread(target=steady_worker, args=(s, interval), name=f"steady-{i}")
          for i, s in enumerate(thread_seqs)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.time() - start
    return total, elapsed


def run_burst():
    cfg = SCENARIO_BURST
    total = cfg["total"]
    threads_n = cfg["threads"]

    print(f"[시나리오 BURST]  총 {total}건 / {threads_n}스레드 / {cfg['burst_size_range']}건씩 비정기")
    seqs = list(range(total))
    chunk = total // threads_n
    thread_seqs = [seqs[i:i + chunk] for i in range(0, total, chunk)]

    start = time.time()
    ts = [threading.Thread(target=burst_worker, args=(s,), name=f"burst-{i}")
          for i, s in enumerate(thread_seqs)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.time() - start
    return total, elapsed


def report(label: str, sent: int, elapsed: float):
    print(f"\n  전송 완료: {elapsed:.1f}초")
    print(f"  ES 반영 대기 {VERIFY_WAIT_SEC}초...")
    time.sleep(VERIFY_WAIT_SEC)

    stored = count_in_es(ES_INDEX)
    loss = sent - stored
    rate = loss / sent * 100 if sent > 0 else 0

    print(f"\n{'──'*20}")
    print(f"  [{label}]")
    print(f"  전송: {sent}건")
    print(f"  저장: {stored}건")
    print(f"  누락: {loss}건  ({rate:.2f}%)")
    print(f"{'──'*20}")

    if loss == 0:
        print("  결과: 이 시나리오에서는 누락 없음.")
    else:
        print("  결과: 누락 발생. 실험 A 결과와 비교:")
        print("    - 실험 A 누락 없음 + 실험 B 누락 있음 → Socket/tcplog 구간 문제")
        print("    - 실험 A도 누락 있음 → ES 레이어 문제")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["steady", "burst", "both"], default="both")
    args = parser.parse_args()

    print(f"[실험 B] Socket → tcplog → ES 재현")
    print(f"  run_id     : {run_id}")
    print(f"  collector  : {COLLECTOR_HOST}:{COLLECTOR_PORT}")
    print(f"  es index   : {ES_INDEX}")
    print()

    global send_count
    if args.scenario in ("steady", "both"):
        send_count = 0
        sent, elapsed = run_steady()
        report("STEADY", sent, elapsed)

    if args.scenario in ("burst", "both"):
        send_count = 0
        run_id_saved = run_id  # burst는 새 run_id 사용
        sent, elapsed = run_burst()
        report("BURST", sent, elapsed)


if __name__ == "__main__":
    main()
