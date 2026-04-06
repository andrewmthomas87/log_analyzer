"""Tests: build minimal .wpilog binaries in memory and verify parsing."""

import struct
from pathlib import Path

from log_analyzer.reader import DataLogReader
from log_analyzer.stats import Mode, analyze_cycle_times, analyze_power, summarize


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


# --- Power analysis tests ---

_VOLTAGE_ENTRY = 10
_TOTAL_CURRENT_ENTRY = 11
_CHANNEL_CURRENT_ENTRY = 12
_BROWNED_OUT_ENTRY = 13


def _write_power_start_records(buf: bytearray, include_ds: bool = False):
    """Write Start records for power analysis entries."""
    _write_record(buf, 0, 0, _make_start_payload(
        _VOLTAGE_ENTRY, "/PowerDistribution/Voltage", "double"))
    _write_record(buf, 0, 0, _make_start_payload(
        _TOTAL_CURRENT_ENTRY, "/PowerDistribution/TotalCurrent", "double"))
    _write_record(buf, 0, 0, _make_start_payload(
        _CHANNEL_CURRENT_ENTRY, "/PowerDistribution/ChannelCurrent", "double[]"))
    _write_record(buf, 0, 0, _make_start_payload(
        _BROWNED_OUT_ENTRY, "/SystemStats/BrownedOut", "boolean"))
    if include_ds:
        _write_record(buf, 0, 0, _make_start_payload(
            _DS_ENABLED_ENTRY, "/DriverStation/Enabled", "boolean"))
        _write_record(buf, 0, 0, _make_start_payload(
            _DS_AUTONOMOUS_ENTRY, "/DriverStation/Autonomous", "boolean"))
        _write_record(buf, 0, 0, _make_start_payload(
            _DS_TEST_ENTRY, "/DriverStation/Test", "boolean"))


def _write_voltage(buf, ts, value):
    _write_record(buf, _VOLTAGE_ENTRY, ts, struct.pack("<d", value))


def _write_total_current(buf, ts, value):
    _write_record(buf, _TOTAL_CURRENT_ENTRY, ts, struct.pack("<d", value))


def _write_channel_current(buf, ts, values):
    payload = b"".join(struct.pack("<d", v) for v in values)
    _write_record(buf, _CHANNEL_CURRENT_ENTRY, ts, payload)


def _write_browned_out(buf, ts, value):
    _write_record(buf, _BROWNED_OUT_ENTRY, ts, struct.pack("?", value))


def test_power_basic(tmp_path: Path):
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    # Write 5 samples, 20ms apart
    for i in range(5):
        ts = (i + 1) * 20_000
        _write_voltage(buf, ts, 12.5 - i * 0.1)  # 12.5, 12.4, ..., 12.1
        _write_total_current(buf, ts, 10.0 + i)  # 10, 11, ..., 14
        channels = [0.0] * 24
        channels[4] = 5.0 + i  # Channel 4 draws 5-9A
        channels[5] = 2.0      # Channel 5 draws 2A constant
        _write_channel_current(buf, ts, channels)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path)

    assert report.has_data
    assert report.num_channels == 24

    v_stats = report.voltage.stats("overall")
    assert v_stats["min"] == 12.1
    assert v_stats["max"] == 12.5

    # Total is derived from Σ channels (ch4 ramps 5→9, ch5 constant 2) ⇒ 7..11
    assert report.total_source == "summed"
    tc_stats = report.total_current.stats("overall")
    assert tc_stats["min"] == 7.0
    assert tc_stats["max"] == 11.0
    # Raw reported total (10..14) is still tracked for sanity-checking
    assert report.reported_total_peak == 14.0

    # Channel stats should be sorted by overall peak desc; ch 4 peak=9, ch 5 peak=2
    # (channels with no samples are dropped; only 4 and 5 had nonzero current,
    #  but the zero-current channels still appear since they had samples)
    top = report.channel_stats[0]
    assert top.channel == 4
    assert top.stats("overall")["max"] == 9.0
    second = report.channel_stats[1]
    assert second.channel == 5
    assert second.stats("overall")["max"] == 2.0

    assert report.brownout_events == []
    assert report.breaker_trip_events == []


def test_power_brownout_detection(tmp_path: Path):
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    _write_browned_out(buf, 10_000, False)
    _write_browned_out(buf, 20_000, True)   # brownout starts
    _write_browned_out(buf, 50_000, False)  # brownout ends (30ms)
    _write_browned_out(buf, 80_000, True)   # second brownout, never ends

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path)

    assert len(report.brownout_events) == 2
    assert report.brownout_events[0].start_us == 20_000
    assert report.brownout_events[0].end_us == 50_000
    assert report.brownout_events[1].start_us == 80_000
    assert report.brownout_events[1].end_us is None


def test_power_breaker_trip(tmp_path: Path):
    """Sustained 80A on a 40A breaker should trip in ~3s."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    # 20ms samples, channel 0 at 80A for 5 seconds
    for i in range(250):
        ts = (i + 1) * 20_000
        channels = [0.0] * 24
        channels[0] = 80.0
        _write_channel_current(buf, ts, channels)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path, breaker_rating=40.0)

    # Exactly one trip: the 5s log ends before the 5s cooldown completes,
    # so we should never see a second trip for this sustained overload.
    assert len(report.breaker_trip_events) == 1
    assert report.breaker_trip_events[0].label == "Ch 0"
    # Should trip somewhere around 3 seconds (3_000_000 us)
    assert 2_500_000 < report.breaker_trip_events[0].timestamp_us < 3_500_000


def test_power_breaker_trip_at_60a(tmp_path: Path):
    """60A on a 40A breaker should trip slower than 80A — around 7.2s."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    # 20ms samples, channel 0 at 60A for 9 seconds
    for i in range(450):
        ts = (i + 1) * 20_000
        channels = [0.0] * 24
        channels[0] = 60.0
        _write_channel_current(buf, ts, channels)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path, breaker_rating=40.0)

    assert len(report.breaker_trip_events) == 1
    assert report.breaker_trip_events[0].label == "Ch 0"
    # Net heat rate at 60A = 3600-1600 = 2000/s, threshold 14400 → ~7.2s
    assert 6_800_000 < report.breaker_trip_events[0].timestamp_us < 7_600_000


def test_power_breaker_cooldown_suppresses_reentry(tmp_path: Path):
    """A sustained 10-second overload should produce exactly one trip event,
    not repeat once the thermal state decays. Validates the cooldown logic."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    # 10 seconds at 80A — long enough that a naive model would trip multiple times
    for i in range(500):
        ts = (i + 1) * 20_000
        channels = [0.0] * 24
        channels[0] = 80.0
        _write_channel_current(buf, ts, channels)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path, breaker_rating=40.0)

    # First trip ~3s. Cooldown ends at ~8s, heat resets to 0, then another
    # ~3s overload to trip again = ~11s. Log only runs 10s, so exactly 1 trip.
    assert len(report.breaker_trip_events) == 1


def test_power_main_breaker_trip(tmp_path: Path):
    """Sustained 240A on a 120A main breaker should trip in ~3s."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    for i in range(250):
        ts = (i + 1) * 20_000
        _write_total_current(buf, ts, 240.0)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path, main_breaker_rating=120.0)

    # No per-channel data → falls back to raw /TotalCurrent
    assert report.total_source == "reported"
    main_trips = [e for e in report.breaker_trip_events if e.label == "Main"]
    assert len(main_trips) == 1
    assert 2_500_000 < main_trips[0].timestamp_us < 3_500_000


def test_power_sim_results_always_present(tmp_path: Path):
    """Stress summary should always be populated, even with no trips."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    # Light load: well under any breaker rating, no trips expected.
    for i in range(10):
        ts = (i + 1) * 20_000
        _write_total_current(buf, ts, 25.0)
        channels = [0.0] * 24
        channels[3] = 10.0
        _write_channel_current(buf, ts, channels)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path)

    assert report.breaker_trip_events == []
    assert len(report.breaker_sim_results) >= 2  # main + at least ch 3

    # Main breaker should be first and reflect the summed-channel peak
    # (ch3 = 10A is the only active channel, so summed Total = 10A).
    main = report.breaker_sim_results[0]
    assert main.label == "Main"
    assert main.rating == 120.0
    assert main.peak_current == 10.0
    assert main.mean_current == 10.0
    assert main.time_over_rating_s == 0.0
    assert main.peak_overage == 0.0  # clamped at 0 when never over rating
    assert main.peak_heat == 0.0
    assert main.peak_stress_pct == 0.0  # never went over rating
    assert main.heat_threshold == 9 * 120 * 120
    assert main.trip_count == 0

    # Ch 3 should appear with peak 10A, no stress
    ch3 = next(r for r in report.breaker_sim_results if r.label == "Ch 3")
    assert ch3.peak_current == 10.0
    assert ch3.trip_count == 0
    assert ch3.peak_heat == 0.0
    assert ch3.peak_stress_pct == 0.0
    assert ch3.heat_threshold == 9 * 40 * 40

    # Zero-current channels should be omitted from results
    labels = {r.label for r in report.breaker_sim_results}
    assert "Ch 0" not in labels
    assert "Ch 5" not in labels


def test_power_sim_results_record_stress_without_tripping(tmp_path: Path):
    """60A on a 40A breaker for 2s should accumulate heat but not trip."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    # 2 seconds at 60A: net rate 2000/s, threshold 14400 → reaches ~4000 (~28%)
    for i in range(100):
        ts = (i + 1) * 20_000
        channels = [0.0] * 24
        channels[0] = 60.0
        _write_channel_current(buf, ts, channels)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path, breaker_rating=40.0)

    assert report.breaker_trip_events == []
    ch0 = next(r for r in report.breaker_sim_results if r.label == "Ch 0")
    assert ch0.trip_count == 0
    assert ch0.peak_current == 60.0
    assert ch0.peak_overage == 20.0  # 60 - 40
    # Time over rating accumulates from sample 2 onward (dt is 0 on first sample)
    assert 1.9 < ch0.time_over_rating_s < 2.1
    # Peak heat ~4000 A²·s (net rate 2000 × 2s). Threshold 14400 → ~28%.
    assert 3500 < ch0.peak_heat < 4500
    assert 20.0 < ch0.peak_stress_pct < 35.0
    # Mean heat ~2000 (linearly grew from 0 to 4000)
    assert 1500 < ch0.mean_heat < 2500


def test_power_no_trip_at_rating(tmp_path: Path):
    """Current at exactly the breaker rating should not trip."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    for i in range(500):
        ts = (i + 1) * 20_000
        channels = [0.0] * 24
        channels[0] = 40.0  # exactly at rating
        _write_channel_current(buf, ts, channels)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path, breaker_rating=40.0)
    assert report.breaker_trip_events == []


def test_power_voltage_zero_filter(tmp_path: Path):
    """Exactly-0.0 V samples are non-physical and must be dropped (the PDH
    reports 0 at init and on transient CAN hiccups)."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    _write_voltage(buf, 10_000, 0.0)   # init sentinel — dropped
    _write_voltage(buf, 20_000, 0.0)   # another — dropped
    _write_voltage(buf, 30_000, 12.5)
    _write_voltage(buf, 40_000, 12.3)
    _write_voltage(buf, 50_000, 0.0)   # mid-log hiccup — also dropped
    _write_voltage(buf, 60_000, 12.4)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path)
    assert report.voltage.overall == [12.5, 12.3, 12.4]


def test_power_sparse_sampling_dt_clamp(tmp_path: Path):
    """A change-filtered signal with multi-second gaps must not trigger
    runaway heat accumulation. This simulates the real-world PDH TotalCurrent
    scenario: two samples of 240A one full second apart on a 120A main
    breaker. Without the dt clamp this would integrate a full second at
    240A (heat rate 43200/s → heat 43200, well past threshold 129600/9×1s=
    but capped). With the 0.1s clamp, at most 0.1s is integrated per step."""
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf)

    # Two samples of 240A, 1s apart (no channel data → fallback reported path)
    _write_total_current(buf, 1_000_000, 240.0)
    _write_total_current(buf, 2_000_000, 240.0)

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path, main_breaker_rating=120.0)
    main = next(r for r in report.breaker_sim_results if r.label == "Main")
    # Without the clamp, heat would be (240²−120²)·1.0 = 43200.
    # With the 0.1s clamp, heat is (240²−120²)·0.1 = 4320.
    assert main.peak_heat < 5000
    # And critically: time_over_rating is also clamped (not 1s of fake time).
    assert main.time_over_rating_s <= 0.11


def test_power_by_mode(tmp_path: Path):
    buf = bytearray()
    _write_wpilog_header(buf)
    _write_power_start_records(buf, include_ds=True)

    def channels_with(ch0: float, ch4: float) -> list[float]:
        c = [0.0] * 24
        c[0] = ch0
        c[4] = ch4
        return c

    # Disabled period
    _write_ds_state(buf, 1_000, enabled=False)
    _write_total_current(buf, 2_000, 2.0)
    _write_voltage(buf, 2_000, 12.8)
    _write_channel_current(buf, 2_000, channels_with(1.0, 0.5))

    # Auto period
    _write_ds_state(buf, 100_000, enabled=True, autonomous=True)
    _write_total_current(buf, 101_000, 50.0)
    _write_voltage(buf, 101_000, 11.5)
    _write_channel_current(buf, 101_000, channels_with(30.0, 15.0))

    # Teleop period
    _write_ds_state(buf, 200_000, enabled=True, autonomous=False)
    _write_total_current(buf, 201_000, 30.0)
    _write_voltage(buf, 201_000, 12.0)
    _write_channel_current(buf, 201_000, channels_with(20.0, 8.0))

    log_path = tmp_path / "test.wpilog"
    log_path.write_bytes(bytes(buf))

    report = analyze_power(log_path)

    # Total is summed from channels: disabled 1.0+0.5=1.5, auto 30+15=45,
    # teleop 20+8=28. (The reported /TotalCurrent values 2/50/30 are ignored
    # when per-channel data is available.)
    assert report.total_source == "summed"
    assert report.total_current.stats("disabled")["max"] == 1.5
    assert report.total_current.stats("autonomous")["max"] == 45.0
    assert report.total_current.stats("teleop")["max"] == 28.0
    assert report.voltage.stats("autonomous")["min"] == 11.5

    # Per-channel per-mode
    by_ch = {cs.channel: cs for cs in report.channel_stats}
    assert by_ch[0].stats("disabled")["max"] == 1.0
    assert by_ch[0].stats("autonomous")["max"] == 30.0
    assert by_ch[0].stats("teleop")["max"] == 20.0
    assert by_ch[4].stats("disabled")["max"] == 0.5
    assert by_ch[4].stats("autonomous")["max"] == 15.0
    assert by_ch[4].stats("teleop")["max"] == 8.0
    # Channel with no data across any mode should not appear
    assert 1 not in by_ch or by_ch[1].stats("overall")["max"] == 0.0
