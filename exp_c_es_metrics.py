"""
실험 C: ES 메트릭 모니터링
목적: 실험 A/B 실행 중 또는 전후로 ES 리소스 상태 확인
      thread_pool rejected, indexing 실패, 인덱스 매핑 점검
"""

import json
import time
import argparse

import requests

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
ES_HOST = "http://localhost:9200"
TARGET_INDEX = "logs-*"
MONITOR_INTERVAL_SEC = 5    # 모니터링 모드에서 갱신 주기


def get(path: str) -> dict | list:
    resp = requests.get(f"{ES_HOST}{path}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_text(path: str) -> str:
    resp = requests.get(f"{ES_HOST}{path}", timeout=10)
    resp.raise_for_status()
    return resp.text


# ──────────────────────────────────────────────
# 점검 함수들
# ──────────────────────────────────────────────

def check_thread_pool():
    print("\n[1] Write Thread Pool 상태")
    lines = get_text("/_cat/thread_pool/write?v&h=node_name,name,active,queue,rejected,completed")
    print(lines)

    # rejected > 0 이면 경고
    for line in lines.strip().split("\n")[1:]:
        parts = line.split()
        if len(parts) >= 5:
            rejected = int(parts[4]) if parts[4].isdigit() else 0
            if rejected > 0:
                print(f"  !! REJECTED 감지: {line}")


def check_index_stats():
    print("\n[2] 인덱스 Indexing 통계")
    data = get(f"/{TARGET_INDEX}/_stats/indexing,store")
    total = data.get("_all", {}).get("total", {})
    indexing = total.get("indexing", {})

    print(f"  index_total    : {indexing.get('index_total', 'N/A')}")
    print(f"  index_failed   : {indexing.get('index_failed', 'N/A')}")
    print(f"  delete_total   : {indexing.get('delete_total', 'N/A')}")

    failed = indexing.get("index_failed", 0)
    if failed and failed > 0:
        print(f"  !! index_failed > 0 — 인덱싱 실패 존재")


def check_cluster_health():
    print("\n[3] 클러스터 상태")
    data = get("/_cluster/health")
    status = data.get("status", "unknown")
    print(f"  status               : {status}")
    print(f"  active_shards        : {data.get('active_shards')}")
    print(f"  relocating_shards    : {data.get('relocating_shards')}")
    print(f"  unassigned_shards    : {data.get('unassigned_shards')}")
    print(f"  active_primary_shards: {data.get('active_primary_shards')}")
    if status != "green":
        print(f"  !! 클러스터 상태가 {status}입니다")


def check_mapping():
    print(f"\n[4] {TARGET_INDEX} 매핑 주요 필드 확인")
    try:
        data = get(f"/{TARGET_INDEX}/_mapping")
        for index_name, index_data in data.items():
            props = index_data.get("mappings", {}).get("properties", {})
            print(f"\n  인덱스: {index_name}")

            critical_fields = [
                "@timestamp", "message", "log.level",
                "event.sequence", "labels.run_id",
                "service.name", "ecs.version",
            ]
            for field in critical_fields:
                parts = field.split(".")
                node = props
                for part in parts:
                    node = node.get(part, {}).get("properties", node.get(part, {}))
                field_type = node.get("type", "object/nested")
                print(f"    {field:<30} : {field_type}")
    except Exception as e:
        print(f"  매핑 조회 실패: {e}")


def check_pending_tasks():
    print("\n[5] ES 대기 중인 클러스터 작업")
    data = get("/_cluster/pending_tasks")
    tasks = data.get("tasks", [])
    if tasks:
        for t in tasks[:10]:
            print(f"  priority={t.get('priority')}  source={t.get('source')}")
    else:
        print("  대기 작업 없음 (정상)")


def snapshot():
    print("=" * 50)
    print(f"  ES 메트릭 스냅샷  [{time.strftime('%H:%M:%S')}]")
    print("=" * 50)
    check_cluster_health()
    check_thread_pool()
    check_index_stats()
    check_pending_tasks()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["once", "monitor", "mapping"], default="once",
                        help="once: 1회 점검 / monitor: 주기적 모니터링 / mapping: 매핑만 확인")
    parser.add_argument("--interval", type=int, default=MONITOR_INTERVAL_SEC)
    args = parser.parse_args()

    if args.mode == "mapping":
        check_mapping()
    elif args.mode == "monitor":
        print(f"모니터링 모드: {args.interval}초 간격. Ctrl+C로 종료.")
        try:
            while True:
                snapshot()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n모니터링 종료.")
    else:
        snapshot()
        check_mapping()


if __name__ == "__main__":
    main()
