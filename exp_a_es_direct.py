"""
실험 A: ES 직접 전송 — ES 레이어 격리 테스트
목적: Collector 없이 ES Bulk API로 직접 전송해서 ES 자체 누락/에러 여부 확인
"""

import json
import time
import uuid
import threading
from datetime import datetime, timezone
from collections import defaultdict

import requests

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
ES_HOST = "http://localhost:9200"
INDEX = "logs-test"
TOTAL_DOCS = 1000
THREADS = 10
BULK_SIZE = 50          # 한 번의 Bulk 요청에 담을 문서 수
VERIFY_WAIT_SEC = 5     # 전송 후 ES 반영 대기 시간 (초)

# ──────────────────────────────────────────────
# 전역 상태
# ──────────────────────────────────────────────
run_id = str(uuid.uuid4())
errors_detail = []
lock = threading.Lock()


def build_doc(seq: int) -> dict:
    return {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "ecs": {"version": "1.6.0"},
        "log": {"level": "INFO"},
        "message": f"exp-a test doc seq={seq}",
        "service": {"name": "exp-a-test"},
        "labels": {
            "run_id": run_id,
        },
        "event": {
            "sequence": seq,
        },
    }


def bulk_send(docs: list[dict]) -> dict:
    """ES Bulk API 호출. 응답의 errors 여부와 실패 항목을 반환."""
    lines = []
    for doc in docs:
        lines.append(json.dumps({"index": {"_index": INDEX}}))
        lines.append(json.dumps(doc))
    body = "\n".join(lines) + "\n"

    resp = requests.post(
        f"{ES_HOST}/_bulk",
        data=body,
        headers={"Content-Type": "application/x-ndjson"},
        timeout=30,
    )
    return resp.json()


def analyze_bulk_response(resp: dict, seq_offset: int):
    """Bulk 응답에서 실패 항목 추출."""
    if not resp.get("errors"):
        return

    for i, item in enumerate(resp.get("items", [])):
        op = item.get("index") or item.get("create") or {}
        if op.get("error"):
            with lock:
                errors_detail.append({
                    "seq": seq_offset + i,
                    "status": op.get("status"),
                    "error": op.get("error"),
                })


def worker(seqs: list[int]):
    chunks = [seqs[i:i + BULK_SIZE] for i in range(0, len(seqs), BULK_SIZE)]
    for chunk in chunks:
        docs = [build_doc(s) for s in chunk]
        try:
            resp = bulk_send(docs)
            analyze_bulk_response(resp, chunk[0])
        except Exception as e:
            with lock:
                errors_detail.append({"seq": chunk[0], "error": str(e)})


def count_in_es() -> int:
    """ES에서 이번 run_id로 저장된 도큐먼트 수 조회."""
    query = {
        "query": {
            "term": {"labels.run_id": run_id}
        }
    }
    resp = requests.post(
        f"{ES_HOST}/{INDEX}/_count",
        json=query,
        timeout=10,
    )
    return resp.json().get("count", -1)


def check_es_connection():
    try:
        requests.get(f"{ES_HOST}/_cluster/health", timeout=5)
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] ES에 연결할 수 없습니다: {ES_HOST}")
        print("  ES_HOST 설정을 확인하고 ES가 실행 중인지 확인하세요.")
        raise SystemExit(1)


def main():
    check_es_connection()

    print(f"[실험 A] ES 직접 전송 테스트")
    print(f"  run_id  : {run_id}")
    print(f"  index   : {INDEX}")
    print(f"  총 문서  : {TOTAL_DOCS}건  /  스레드: {THREADS}개  /  bulk size: {BULK_SIZE}")
    print()

    all_seqs = list(range(TOTAL_DOCS))
    chunk_size = TOTAL_DOCS // THREADS
    thread_seqs = [all_seqs[i:i + chunk_size] for i in range(0, TOTAL_DOCS, chunk_size)]

    start = time.time()
    threads = [threading.Thread(target=worker, args=(s,)) for s in thread_seqs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    print(f"  전송 완료: {elapsed:.1f}초")
    print(f"  Bulk API 에러 항목 수: {len(errors_detail)}건")

    if errors_detail:
        print("\n  [Bulk 에러 상세]")
        for e in errors_detail[:20]:
            print(f"    seq={e.get('seq')}  status={e.get('status')}  error={e.get('error')}")
        if len(errors_detail) > 20:
            print(f"    ... 외 {len(errors_detail) - 20}건")

    print(f"\n  ES 반영 대기 {VERIFY_WAIT_SEC}초...")
    time.sleep(VERIFY_WAIT_SEC)

    stored = count_in_es()
    loss = TOTAL_DOCS - stored
    loss_rate = loss / TOTAL_DOCS * 100

    print("\n──────────────────────────────")
    print(f"  전송: {TOTAL_DOCS}건")
    print(f"  저장: {stored}건")
    print(f"  누락: {loss}건  ({loss_rate:.2f}%)")
    print("──────────────────────────────")

    if loss == 0:
        print("  결과: ES 레이어 정상. 문제는 Collector 또는 Socket 구간일 가능성 높음.")
    else:
        print("  결과: ES 레이어에서 누락 발생. Bulk 에러 상세 및 ES 메트릭 확인 필요.")
        print("        → exp_c_es_metrics.py 실행 후 thread_pool rejected / mapping 오류 확인.")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.ConnectionError:
        print(f"\n[ERROR] ES에 연결할 수 없습니다: {ES_HOST}")
        print("  ES_HOST 설정을 확인하고 ES가 실행 중인지 확인하세요.")
    except requests.exceptions.HTTPError as e:
        print(f"\n[ERROR] ES 응답 오류: {e}")
