import json
import logging

from app.logging_config import JsonFormatter


def test_log_record_is_json_with_required_fields():
    record = logging.LogRecord("app.x", logging.INFO, __file__, 1, "cache_hit", None, None)
    record.input = 35
    record.duration_ms = 12.5

    entry = json.loads(JsonFormatter().format(record))

    assert entry["event"] == "cache_hit"
    assert entry["level"] == "INFO"
    assert entry["input"] == 35
    assert entry["duration_ms"] == 12.5
    assert entry["instance_hostname"]
    assert "timestamp" in entry
