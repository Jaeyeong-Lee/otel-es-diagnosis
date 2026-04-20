import json
import pathlib

_cfg = json.loads((pathlib.Path(__file__).parent / "config.json").read_text())
_exp = _cfg["experiment"]

ES_HOST             = _cfg["es_host"]
ES_INDEX_DIRECT     = _cfg["es_index_direct"]
ES_INDEX_PATTERN    = _cfg["es_index_pattern"]
COLLECTOR_HOST      = _cfg["collector_host"]
COLLECTOR_PORT      = _cfg["collector_port"]
VERIFY_WAIT_SEC     = _cfg["verify_wait_sec"]

TOTAL_DOCS          = _exp["total_docs"]
THREADS             = _exp["threads"]
BULK_SIZE           = _exp["bulk_size"]
STEADY_INTERVAL_SEC = _exp["steady_interval_sec"]
BURST_SIZE_MIN      = _exp["burst_size_min"]
BURST_SIZE_MAX      = _exp["burst_size_max"]
BURST_INTERVAL_MIN  = _exp["burst_interval_min"]
BURST_INTERVAL_MAX  = _exp["burst_interval_max"]
