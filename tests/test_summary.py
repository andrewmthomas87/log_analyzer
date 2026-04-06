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


# Entry IDs used across test helpers
_CYCLE_ENTRY = 1
_DS_ENABLED_ENTRY = 2
_DS_AUTONOMOUS_ENTRY = 3
_DS_TEST_ENTRY = 4


def _write_wpilog_header(buf: bytearray):
    """Write the .wpilog file header."""
    buf.extend(b"WPILOG")
    buf.extend((0x0100).to_bytes(2, "little"))  # version 1.0
    buf.extend((0).to_bytes(4, "little"))  # no extra header


def _write_start_records(buf: bytearray, include_ds: bool = False):
    """Write Start control records for cycle time and optionally DriverStation entries."""
    _write_record(buf, 0, 0, _make_start_payload(
        _CYCLE_ENTRY, "/RealOutputs/LoggedRobot/FullCycleMS", "double"))
    if include_ds:
        _write_record(buf, 0, 0, _make_start_payload(
            _DS_ENABLED_ENTRY, "/DriverStation/Enabled", "boolean"))
        _write_record(buf, 0, 0, _make_start_payload(
            _DS_AUTONOMOUS_ENTRY, "/DriverStation/Autonomous", "boolean"))
        _write_record(buf, 0, 0, _make_start_payload(
            _DS_TEST_ENTRY, "/DriverStation/Test", "boolean"))


def _write_ds_state(buf: bytearray, timestamp: int,
                    enabled: bool, autonomous: bool = False, test: bool = False):
    """Write DriverStation boolean records at the given timestamp."""
    _write_record(buf, _DS_ENABLED_ENTRY, timestamp, struct.pack("?", enabled))
    _write_record(buf, _DS_AUTONOMOUS_ENTRY, timestamp, struct.pack("?", autonomous))
    _write_record(buf, _DS_TEST_ENTRY, timestamp, struct.pack("?", test))


def _write_cycle_time(buf: bytearray, timestamp: int, value: float):
    """Write a single cycle time data record."""
    _write_record(buf, _CYCLE_ENTRY, timestamp, struct.pack("<d", value))


def build_test_wpilog(cycle_times: list[float]) -> bytes:
    """Create a minimal .wpilog with cycle time data (no DriverStation entries)."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_start_records(buf, include_ds=False)

    for i, val in enumerate(cycle_times):
        timestamp = (i + 1) * 20_000  # 20ms apart, in microseconds
        _write_cycle_time(buf, timestamp, val)

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
    ct = report.cycle_times

    assert report.has_data
    stats = ct.stats()
    assert stats is not None
    assert stats["min"] == 14.0
    assert stats["max"] == 25.0
    assert stats["count"] == 10
    # Without DriverStation entries, all cycles land in disabled
    assert len(ct.disabled) == 10
    assert len(ct.enabled) == 0


def test_cycle_times_by_mode(tmp_path: Path):
    """Simulate a log with disabled -> auto -> teleop -> disabled periods."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_start_records(buf, include_ds=True)

    t = 1_000  # start timestamp in microseconds

    # Period 1: disabled (2 cycles)
    _write_ds_state(buf, t, enabled=False)
    _write_cycle_time(buf, t + 1_000, 10.0)
    _write_cycle_time(buf, t + 2_000, 11.0)

    # Period 2: autonomous (3 cycles)
    t = 100_000
    _write_ds_state(buf, t, enabled=True, autonomous=True)
    _write_cycle_time(buf, t + 1_000, 15.0)
    _write_cycle_time(buf, t + 2_000, 16.0)
    _write_cycle_time(buf, t + 3_000, 17.0)

    # Period 3: teleop (4 cycles)
    t = 200_000
    _write_ds_state(buf, t, enabled=True, autonomous=False)
    _write_cycle_time(buf, t + 1_000, 18.0)
    _write_cycle_time(buf, t + 2_000, 19.0)
    _write_cycle_time(buf, t + 3_000, 22.0)
    _write_cycle_time(buf, t + 4_000, 20.0)

    # Period 4: disabled again (1 cycle)
    t = 300_000
    _write_ds_state(buf, t, enabled=False)
    _write_cycle_time(buf, t + 1_000, 12.0)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_cycle_times(log_path)
    ct = report.cycle_times

    assert ct.stats()["count"] == 10

    # Disabled: 10, 11, 12
    assert len(ct.disabled) == 3
    assert ct.stats("disabled")["min"] == 10.0
    assert ct.stats("disabled")["max"] == 12.0

    # Enabled: auto + teleop = 7
    assert len(ct.enabled) == 7
    assert ct.stats("enabled")["count"] == 7
    assert ct.stats("enabled")["min"] == 15.0

    # Autonomous: 15, 16, 17
    assert len(ct.autonomous) == 3
    assert ct.stats("autonomous")["min"] == 15.0
    assert ct.stats("autonomous")["max"] == 17.0

    # Teleop: 18, 19, 22, 20
    assert len(ct.teleop) == 4
    assert ct.stats("teleop")["min"] == 18.0
    assert ct.stats("teleop")["max"] == 22.0

    # Test: none
    assert len(ct.test) == 0
    assert ct.stats("test") is None


def test_cycle_times_with_test_mode(tmp_path: Path):
    """Verify test mode is bucketed separately from teleop."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_start_records(buf, include_ds=True)

    t = 1_000
    _write_ds_state(buf, t, enabled=True, test=True)
    _write_cycle_time(buf, t + 1_000, 13.0)
    _write_cycle_time(buf, t + 2_000, 14.0)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_cycle_times(log_path)
    ct = report.cycle_times

    assert len(ct.test) == 2
    assert len(ct.teleop) == 0
    assert len(ct.autonomous) == 0
    assert len(ct.enabled) == 2
