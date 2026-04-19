# OTel-ES 데이터 누락 진단

## 파일 구성

| 파일 | 역할 |
|---|---|
| `diagnosis_guide.md` | 전체 분석 문서 (문제 → 실험 → 결과 해석) |
| `exp_a_es_direct.py` | ES 직접 전송 — ES 레이어 격리 테스트 |
| `exp_b_socket.py` | Socket → tcplog 재현 — 현재 스택 시뮬레이션 |
| `exp_c_es_metrics.py` | ES 메트릭 모니터링 |
| `collector_filelog_snippet.yaml` | filelog receiver 추가 Collector 설정 |
| `log4j2_file_appender.xml` | FileAppender 추가 log4j2.xml |

## 준비

```bash
pip install requests
```

## 실행 순서

```bash
# 1. ES 기본 상태 먼저 확인
python exp_c_es_metrics.py --mode once

# 2. 실험 A — ES 레이어 격리 (ES_HOST, INDEX 설정 확인 후 실행)
python exp_a_es_direct.py

# 3. 실험 B — 현재 스택 재현 (COLLECTOR_HOST/PORT, ES_HOST 설정 확인 후 실행)
python exp_b_socket.py --scenario both

# 4. 실험 A/B 병행 시 ES 모니터링
python exp_c_es_metrics.py --mode monitor --interval 5

# 5. 매핑 상세 확인
python exp_c_es_metrics.py --mode mapping
```

## 각 스크립트 상단 설정값 변경 필요

```python
# exp_a_es_direct.py
ES_HOST = "http://localhost:9200"   # ES 주소
INDEX = "logs-test"                  # 테스트용 인덱스 (운영 인덱스와 분리 권장)

# exp_b_socket.py
COLLECTOR_HOST = "localhost"         # OTel Collector 주소
COLLECTOR_PORT = 54525               # tcplog receiver 포트
ES_HOST = "http://localhost:9200"
ES_INDEX = "logs-*"

# exp_c_es_metrics.py
ES_HOST = "http://localhost:9200"
TARGET_INDEX = "logs-*"
```

## 결과 해석 요약

- **실험 A 정상 + 실험 B 누락** → Socket/tcplog 구간 문제 → filelog 전환 (`collector_filelog_snippet.yaml`, `log4j2_file_appender.xml` 참고)
- **실험 A도 누락** → ES 레이어 문제 → `exp_c_es_metrics.py` 결과에서 rejected/mapping 오류 확인
- 상세 해석은 `diagnosis_guide.md` 5절 참고
