"""Tests: build minimal .wpilog binaries in memory and verify parsing."""

import struct
from pathlib import Path

from log_analyzer.reader import DataLogReader
from log_analyzer.stats import summarize, analyze_cycle_times


def _write_record(buf: bytearray, entry: int, timestamp: int, payload: bytes):
    """Append a record to buf using minimal byte widths."""
    entry_len = max(1, (entry.bit_length() + 7) // 8) if entry else 1
    size_len = max(1, (len(payload).bit_length() + 7) // 8) if payload else 1
    ts_len = max(1, (timestamp.bit_length() + 7) // 8) if timestamp else 1

    header_byte = (entry_len - 1) | ((size_len - 1) << 2) | ((ts_len - 1) << 4)
    buf.append(header_byte)
    buf.extend(entry.to_bytes(entry_len, "little"))
    buf.extend(len(payload).to_bytes(size_len, "little"))
    buf.extend(timestamp.to_bytes(ts_len, "little"))
    buf.extend(payload)


def _make_start_payload(entry_id: int, name: str, type_str: str, metadata: str = "") -> bytes:
    """Build the payload for a Start control record."""
    payload = bytearray()
    payload.append(0x00)  # control type = Start
    payload.extend(entry_id.to_bytes(4, "little"))
    name_b = name.encode("utf-8")
    payload.extend(len(name_b).to_bytes(4, "little"))
    payload.extend(name_b)
    type_b = type_str.encode("utf-8")
    payload.extend(len(type_b).to_bytes(4, "little"))
    payload.extend(type_b)
    meta_b = metadata.encode("utf-8")
    payload.extend(len(meta_b).to_bytes(4, "little"))
    payload.extend(meta_b)
    return bytes(payload)


def build_test_wpilog(cycle_times: list[float]) -> bytes:
    """Create a minimal .wpilog with cycle time data."""
    buf = bytearray()
    # Header
    buf.extend(b"WPILOG")
    buf.extend((0x0100).to_bytes(2, "little"))  # version 1.0
    buf.extend((0).to_bytes(4, "little"))  # no extra header

    # Start record for cycle times (entry ID 1)
    start_payload = _make_start_payload(1, "/RealOutputs/LoggedRobot/FullCycleMS", "double")
    _write_record(buf, 0, 0, start_payload)

    # Data records
    for i, val in enumerate(cycle_times):
        timestamp = (i + 1) * 20_000  # 20ms apart, in microseconds
        _write_record(buf, 1, timestamp, struct.pack("<d", val))

    return bytes(buf)


def test_reader_validates_header():
    reader = DataLogReader(b"WPILOG\x00\x01\x00\x00\x00\x00")
    assert reader.is_valid()

    reader = DataLogReader(b"NOTLOG\x00\x01\x00\x00\x00\x00")
    assert not reader.is_valid()


def test_summary_basic(tmp_path: Path):
    cycle_times = [15.0, 18.0, 22.0, 16.0, 19.0, 25.0, 17.0, 14.0, 20.0, 21.0]
    log_bytes = build_test_wpilog(cycle_times)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(log_bytes)

    result = summarize(log_path)

    assert result.num_entries == 1
    assert result.num_data_records == 10
    assert result.duration_secs > 0


def test_cycle_times(tmp_path: Path):
    cycle_times = [15.0, 18.0, 22.0, 16.0, 19.0, 25.0, 17.0, 14.0, 20.0, 21.0]
    log_bytes = build_test_wpilog(cycle_times)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(log_bytes)

    report = analyze_cycle_times(log_path)

    assert report.has_data
    stats = report.stats
    assert stats is not None
    assert stats["min"] == 14.0
    assert stats["max"] == 25.0
    assert stats["count"] == 10
