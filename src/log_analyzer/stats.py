import mmap
import statistics
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .reader import DataLogReader, StartRecordData


# --- Shared analysis infrastructure ---


class Mode(Enum):
    DISABLED = "Disabled"
    AUTONOMOUS = "Autonomous"
    TELEOP = "Teleop"
    TEST = "Test"


DS_ENABLED_KEY = "/DriverStation/Enabled"
DS_AUTONOMOUS_KEY = "/DriverStation/Autonomous"
DS_TEST_KEY = "/DriverStation/Test"

_DS_KEYS = {DS_ENABLED_KEY, DS_AUTONOMOUS_KEY, DS_TEST_KEY}


class ModeTracker:
    """Tracks the current robot mode from DriverStation log entries.

    Feed it Start, Finish, and data records as you iterate. It maintains
    the current mode state so any analysis can bucket values by mode.
    """

    def __init__(self):
        self._entry_ids: dict[str, int | None] = {
            DS_ENABLED_KEY: None,
            DS_AUTONOMOUS_KEY: None,
            DS_TEST_KEY: None,
        }
        self._enabled = False
        self._autonomous = False
        self._test = False

    def handle_start(self, data: StartRecordData):
        """Register a DriverStation entry if applicable."""
        if data.name in _DS_KEYS:
            self._entry_ids[data.name] = data.entry

    def handle_finish(self, entry_id: int):
        """Clear a DriverStation entry if it was finished."""
        for key, eid in self._entry_ids.items():
            if entry_id == eid:
                self._entry_ids[key] = None

    def handle_data(self, record) -> bool:
        """Update mode state from a data record. Returns True if consumed."""
        if record.entry == self._entry_ids[DS_ENABLED_KEY]:
            self._enabled = record.get_boolean()
            return True
        if record.entry == self._entry_ids[DS_AUTONOMOUS_KEY]:
            self._autonomous = record.get_boolean()
            return True
        if record.entry == self._entry_ids[DS_TEST_KEY]:
            self._test = record.get_boolean()
            return True
        return False

    @property
    def mode(self) -> Mode:
        if not self._enabled:
            return Mode.DISABLED
        if self._autonomous:
            return Mode.AUTONOMOUS
        if self._test:
            return Mode.TEST
        return Mode.TELEOP


def _compute_stats(vals: list[float]) -> dict[str, float] | None:
    """Compute summary statistics for a list of values."""
    if not vals:
        return None
    sorted_vals = sorted(vals)
    n = len(sorted_vals)

    def percentile(p: float) -> float:
        return sorted_vals[min(int(n * p), n - 1)]

    return {
        "min": min(vals),
        "median": statistics.median(vals),
        "mean": statistics.mean(vals),
        "p80": percentile(0.80),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": max(vals),
        "count": n,
    }


@dataclass
class ByMode:
    """Per-mode grouping of float values with stats access."""

    overall: list[float] = field(default_factory=list, repr=False)
    disabled: list[float] = field(default_factory=list, repr=False)
    enabled: list[float] = field(default_factory=list, repr=False)
    autonomous: list[float] = field(default_factory=list, repr=False)
    teleop: list[float] = field(default_factory=list, repr=False)
    test: list[float] = field(default_factory=list, repr=False)

    def append(self, value: float, mode: Mode):
        self.overall.append(value)
        if mode == Mode.DISABLED:
            self.disabled.append(value)
        else:
            self.enabled.append(value)
            if mode == Mode.AUTONOMOUS:
                self.autonomous.append(value)
            elif mode == Mode.TEST:
                self.test.append(value)
            else:
                self.teleop.append(value)

    def stats(self, section: str = "overall") -> dict[str, float] | None:
        """Get stats for a section: overall, disabled, enabled, autonomous, teleop, test."""
        return _compute_stats(getattr(self, section))

    DISPLAY_SECTIONS = ["disabled", "enabled", "autonomous", "teleop", "test"]
    """Sections to show in output (beyond overall)."""


# --- Log summary ---


@dataclass
class LogSummary:
    """Basic summary of a .wpilog file."""

    duration_secs: float
    num_entries: int
    num_data_records: int
    entries: list[StartRecordData] = field(repr=False)


# --- Cycle time analysis ---


CYCLE_TIME_KEY = "/RealOutputs/LoggedRobot/FullCycleMS"


@dataclass
class CycleTimeReport:
    """Cycle time analysis from a .wpilog file."""

    cycle_times: ByMode

    @property
    def has_data(self) -> bool:
        return len(self.cycle_times.overall) > 0


# --- Power analysis ---


CHANNEL_CURRENT_KEY = "/PowerDistribution/ChannelCurrent"
TOTAL_CURRENT_KEY = "/PowerDistribution/TotalCurrent"
VOLTAGE_KEY = "/PowerDistribution/Voltage"
BROWNED_OUT_KEY = "/SystemStats/BrownedOut"


@dataclass
class ChannelStats:
    """Per-mode current statistics for a single PDH channel."""

    channel: int
    by_mode: ByMode

    def stats(self, section: str = "overall") -> dict[str, float] | None:
        """Stats for a section: overall, disabled, enabled, autonomous, teleop, test."""
        return self.by_mode.stats(section)


@dataclass
class BrownoutEvent:
    """A period where the roboRIO reported a brownout."""

    start_us: int
    end_us: int | None  # None if log ended during brownout
    mode: Mode


@dataclass
class BreakerTripEvent:
    """A simulated breaker trip based on thermal accumulation."""

    label: str  # "Main" or "Ch N"
    timestamp_us: int
    peak_current: float


@dataclass
class BreakerSimResult:
    """End-of-log summary of a simulated breaker's stress.

    Exposed even when no trip occurred so teams can see how close they got.
    Includes both current stats and raw thermal-model state so the underlying
    assumptions of the simulation can be audited.

    Heat is in A²·s (I²t integral); ``heat_threshold`` is the value at which
    the simulator decides the breaker has tripped (calibrated to 9 × rating²).
    """

    label: str
    rating: float
    sample_count: int
    # Current stats (amps) over all samples fed to the simulator
    peak_current: float
    p99_current: float
    p95_current: float
    p80_current: float
    mean_current: float
    # How far over rating
    peak_overage: float        # max(current - rating), amps
    time_over_rating_s: float  # cumulative seconds with current > rating
    # Raw thermal state (units: A²·s)
    heat_threshold: float
    peak_heat: float
    p99_heat: float
    p95_heat: float
    p80_heat: float
    mean_heat: float
    trip_count: int

    @property
    def peak_stress_pct(self) -> float:
        """Peak heat as a percentage of the trip threshold."""
        return (self.peak_heat / self.heat_threshold) * 100.0 if self.heat_threshold > 0 else 0.0

    @property
    def mean_stress_pct(self) -> float:
        """Mean heat as a percentage of the trip threshold."""
        return (self.mean_heat / self.heat_threshold) * 100.0 if self.heat_threshold > 0 else 0.0

    @property
    def p99_stress_pct(self) -> float:
        return (self.p99_heat / self.heat_threshold) * 100.0 if self.heat_threshold > 0 else 0.0

    @property
    def p95_stress_pct(self) -> float:
        return (self.p95_heat / self.heat_threshold) * 100.0 if self.heat_threshold > 0 else 0.0

    @property
    def p80_stress_pct(self) -> float:
        return (self.p80_heat / self.heat_threshold) * 100.0 if self.heat_threshold > 0 else 0.0


class _MainBreakerChannel:
    """Bundles a Main-breaker simulator with the bookkeeping needed to feed
    it a stream of (timestamp, amps) samples and emit trip events.

    ``analyze_power`` runs two of these in parallel — one driven by
    Σ-channels, one by raw /PowerDistribution/TotalCurrent — and picks
    whichever actually saw data at the end.
    """

    def __init__(self, rating: float):
        self.sim = _BreakerSimulator("Main", rating)
        self.trip_events: list[BreakerTripEvent] = []
        self._peak_since_trip = 0.0
        self._last_ts: int | None = None

    def ingest(self, amps: float, timestamp_us: int) -> None:
        dt = 0.0
        if self._last_ts is not None:
            dt = (timestamp_us - self._last_ts) / 1_000_000
        self._last_ts = timestamp_us
        if amps > self._peak_since_trip:
            self._peak_since_trip = amps
        if dt > 0 and self.sim.update(amps, dt):
            self.trip_events.append(BreakerTripEvent(
                label="Main",
                timestamp_us=timestamp_us,
                peak_current=self._peak_since_trip,
            ))
            self._peak_since_trip = 0.0


def _build_breaker_sim_result(
    sim: "_BreakerSimulator", current_samples: list[float]
) -> BreakerSimResult:
    """Combine a simulator's thermal state with its current samples into a result."""
    cur = _compute_stats(current_samples)
    heat = _compute_stats(sim.heat_samples)
    return BreakerSimResult(
        label=sim.label,
        rating=sim.rating,
        sample_count=int(cur["count"]) if cur else 0,
        peak_current=cur["max"] if cur else 0.0,
        p99_current=cur["p99"] if cur else 0.0,
        p95_current=cur["p95"] if cur else 0.0,
        p80_current=cur["p80"] if cur else 0.0,
        mean_current=cur["mean"] if cur else 0.0,
        peak_overage=sim.peak_overage,
        time_over_rating_s=sim.time_over_rating_s,
        heat_threshold=sim.threshold,
        peak_heat=sim.peak_heat,
        p99_heat=heat["p99"] if heat else 0.0,
        p95_heat=heat["p95"] if heat else 0.0,
        p80_heat=heat["p80"] if heat else 0.0,
        mean_heat=heat["mean"] if heat else 0.0,
        trip_count=sim.trip_count,
    )


@dataclass
class PowerReport:
    """Power usage analysis from a .wpilog file."""

    voltage: ByMode
    total_current: ByMode
    channel_stats: list[ChannelStats]
    brownout_events: list[BrownoutEvent]
    breaker_trip_events: list[BreakerTripEvent]
    breaker_sim_results: list[BreakerSimResult]  # one entry per simulated breaker
    num_channels: int
    # How `total_current` and the Main breaker sim were derived:
    #   "summed"   — Σ ChannelCurrent[i] at the channel cadence (preferred;
    #                denser and consistent with per-channel means/sim)
    #   "reported" — raw /PowerDistribution/TotalCurrent (fallback when no
    #                ChannelCurrent entry exists; this signal is typically
    #                change-filtered so it is much sparser than per-channel)
    total_source: str = "summed"
    # Peak of the raw reported TotalCurrent, when present, for sanity-checking
    # against the summed peak. None when the log had no reported Total entry.
    reported_total_peak: float | None = None

    @property
    def has_data(self) -> bool:
        return len(self.total_current.overall) > 0 or len(self.voltage.overall) > 0


# Real FRC auto-resetting breakers take ~5-15s to cool and reset. During
# the cooldown, the circuit is open and no current flows through that branch
# — but the log may still show residual readings. We suppress re-trips until
# the cooldown elapses to avoid emitting many events for one overload.
_BREAKER_COOLDOWN_SECONDS = 5.0

# Upper bound on the per-step dt we'll integrate across. Signals like
# PDH TotalCurrent are often logged with a change-filter, so the gap between
# consecutive samples can be seconds even though the underlying signal is
# changing faster. Holding the last value across a multi-second gap causes
# runaway heat accumulation. 0.1s = 5× the 20ms robot loop, which is wide
# enough to tolerate occasional scheduling jitter but prevents pathological
# integration across big gaps.
_MAX_SIM_DT = 0.1


class _BreakerSimulator:
    """Simulates thermal breaker behavior for one channel.

    Uses an I²t thermal model: heat accumulates when current exceeds the
    breaker rating² and dissipates proportional to rating² when below. A trip
    is flagged when accumulated heat crosses the threshold, after which the
    simulator enters a cooldown state and ignores current for
    ``_BREAKER_COOLDOWN_SECONDS`` before resuming.

    Calibrated so a 40A breaker trips in ~3s at 80A sustained (threshold
    = 9 × rating²; at 80A net rate = 4800/s → ~3s to reach 14400).
    """

    def __init__(self, label: str, rating: float):
        self.label = label
        self.rating = rating
        self.heat = 0.0
        self.threshold = rating * rating * 9.0
        self._rating_sq = rating * rating
        self._cooldown_remaining = 0.0
        # Stress tracking (always recorded, independent of trips)
        self.peak_current = 0.0
        self.time_over_rating_s = 0.0
        self.peak_heat = 0.0
        self.peak_overage = 0.0  # max(current - rating) ever seen
        self.trip_count = 0
        # Heat sampled on every update so we can compute mean/p95 at the end.
        # At ~50Hz × match length this is a few thousand floats per breaker —
        # negligible compared to raw current data.
        self.heat_samples: list[float] = []

    def update(self, current: float, dt_seconds: float) -> bool:
        """Update thermal state. Returns True if breaker tripped this step."""
        # Clamp dt so a sparsely-sampled signal (change-filter deadbands) can't
        # drive the I²t integral to nonsense. See _MAX_SIM_DT.
        if dt_seconds > _MAX_SIM_DT:
            dt_seconds = _MAX_SIM_DT
        if current > self.peak_current:
            self.peak_current = current
        overage = current - self.rating
        if overage > self.peak_overage:
            self.peak_overage = overage
        if current > self.rating and dt_seconds > 0:
            self.time_over_rating_s += dt_seconds

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= dt_seconds
            if self._cooldown_remaining <= 0:
                self._cooldown_remaining = 0.0
                self.heat = 0.0
            self.heat_samples.append(self.heat)
            return False

        i_sq = current * current
        if i_sq > self._rating_sq:
            self.heat += (i_sq - self._rating_sq) * dt_seconds
        else:
            self.heat = max(0.0, self.heat - self._rating_sq * dt_seconds)

        if self.heat > self.peak_heat:
            self.peak_heat = self.heat
        self.heat_samples.append(self.heat)

        if self.heat >= self.threshold:
            self._cooldown_remaining = _BREAKER_COOLDOWN_SECONDS
            self.trip_count += 1
            return True
        return False




# --- File parsing ---


def _open_reader(path: Path):
    """Open a .wpilog file and return (mmap, reader). Caller must close mmap."""
    f = open(path, "rb")
    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    f.close()
    reader = DataLogReader(mm)
    if not reader:
        mm.close()
        raise ValueError(f"Invalid .wpilog file: {path}")
    return mm, reader


def summarize(path: Path) -> LogSummary:
    """Parse a .wpilog file and return a basic summary."""
    mm, reader = _open_reader(path)
    try:
        entries: dict[int, StartRecordData] = {}
        first_timestamp: int | None = None
        last_timestamp: int | None = None
        num_data_records = 0

        for record in reader:
            if record.is_start():
                data = record.get_start_data()
                entries[data.entry] = data
            elif not record.is_control():
                num_data_records += 1
                if first_timestamp is None:
                    first_timestamp = record.timestamp
                last_timestamp = record.timestamp
    finally:
        mm.close()

    if first_timestamp is not None and last_timestamp is not None:
        duration_secs = (last_timestamp - first_timestamp) / 1_000_000
    else:
        duration_secs = 0.0

    return LogSummary(
        duration_secs=duration_secs,
        num_entries=len(entries),
        num_data_records=num_data_records,
        entries=list(entries.values()),
    )


def analyze_cycle_times(path: Path) -> CycleTimeReport:
    """Parse a .wpilog file and extract cycle time data by mode."""
    mm, reader = _open_reader(path)
    try:
        entries: dict[int, StartRecordData] = {}
        cycle_time_entry_id: int | None = None
        tracker = ModeTracker()
        cycle_times = ByMode()

        for record in reader:
            if record.is_start():
                data = record.get_start_data()
                entries[data.entry] = data
                tracker.handle_start(data)
                if data.name == CYCLE_TIME_KEY:
                    cycle_time_entry_id = data.entry
            elif record.is_finish():
                entry_id = record.get_finish_entry()
                tracker.handle_finish(entry_id)
                if entry_id == cycle_time_entry_id:
                    cycle_time_entry_id = None
            elif not record.is_control():
                tracker.handle_data(record)
                if record.entry == cycle_time_entry_id:
                    entry = entries.get(record.entry)
                    if entry and entry.type == "double":
                        cycle_times.append(record.get_double(), tracker.mode)
    finally:
        mm.close()

    return CycleTimeReport(cycle_times=cycle_times)


def analyze_power(
    path: Path,
    breaker_rating: float = 40.0,
    main_breaker_rating: float = 120.0,
) -> PowerReport:
    """Parse a .wpilog file and extract power usage data."""
    mm, reader = _open_reader(path)
    try:
        entries: dict[int, StartRecordData] = {}
        tracker = ModeTracker()

        channel_current_id: int | None = None
        total_current_id: int | None = None
        voltage_id: int | None = None
        browned_out_id: int | None = None

        voltage = ByMode()
        # Two candidate sources for "total current":
        #   summed_total  — derived from channels, preferred when available
        #   reported_total — raw PDH /TotalCurrent, used as fallback
        summed_total = ByMode()
        reported_total = ByMode()
        reported_total_peak: float | None = None
        channel_by_mode: list[ByMode] = []
        num_channels = 0

        # Brownout tracking
        brownout_events: list[BrownoutEvent] = []
        brownout_active = False
        brownout_start_us = 0
        brownout_mode = Mode.DISABLED

        # Breaker simulation
        breakers: list[_BreakerSimulator] = []
        breaker_trip_events: list[BreakerTripEvent] = []
        # Peak current since the last trip on each channel; reset after a trip
        # so a new overload reports its own peak rather than the all-time max.
        channel_peak_since_trip: list[float] = []
        last_channel_ts: int | None = None

        # Main breaker: two candidate simulators (summed-from-channels and
        # reported-TotalCurrent). At the end we pick whichever saw data;
        # summed wins when both are available.
        main_summed = _MainBreakerChannel(main_breaker_rating)
        main_reported = _MainBreakerChannel(main_breaker_rating)

        for record in reader:
            if record.is_start():
                data = record.get_start_data()
                entries[data.entry] = data
                tracker.handle_start(data)
                if data.name == CHANNEL_CURRENT_KEY:
                    channel_current_id = data.entry
                elif data.name == TOTAL_CURRENT_KEY:
                    total_current_id = data.entry
                elif data.name == VOLTAGE_KEY:
                    voltage_id = data.entry
                elif data.name == BROWNED_OUT_KEY:
                    browned_out_id = data.entry
            elif record.is_finish():
                entry_id = record.get_finish_entry()
                tracker.handle_finish(entry_id)
                if entry_id == channel_current_id:
                    channel_current_id = None
                elif entry_id == total_current_id:
                    total_current_id = None
                elif entry_id == voltage_id:
                    voltage_id = None
                elif entry_id == browned_out_id:
                    browned_out_id = None
            elif not record.is_control():
                if tracker.handle_data(record):
                    continue

                if record.entry == voltage_id:
                    entry = entries.get(record.entry)
                    if entry and entry.type == "double":
                        v = record.get_double()
                        # Drop non-physical zero readings. The PDH reports
                        # 0.0 V at init before it has a real sample, and
                        # occasionally mid-log on a CAN hiccup. A real
                        # battery is never at exactly 0 V while the log is
                        # recording, so we filter *all* exact-0.0 samples
                        # (not just the leading run). Real sag under load
                        # bottoms out around 6-8 V during brownout.
                        if v != 0.0:
                            voltage.append(v, tracker.mode)

                elif record.entry == total_current_id:
                    entry = entries.get(record.entry)
                    if entry and entry.type == "double":
                        amps = record.get_double()
                        reported_total.append(amps, tracker.mode)
                        if reported_total_peak is None or amps > reported_total_peak:
                            reported_total_peak = amps
                        # Fallback Main sim (used only if no ChannelCurrent).
                        main_reported.ingest(amps, record.timestamp)

                elif record.entry == browned_out_id:
                    entry = entries.get(record.entry)
                    if entry and entry.type == "boolean":
                        is_browned = record.get_boolean()
                        if is_browned and not brownout_active:
                            brownout_active = True
                            brownout_start_us = record.timestamp
                            brownout_mode = tracker.mode
                        elif not is_browned and brownout_active:
                            brownout_active = False
                            brownout_events.append(BrownoutEvent(
                                start_us=brownout_start_us,
                                end_us=record.timestamp,
                                mode=brownout_mode,
                            ))

                elif record.entry == channel_current_id:
                    entry = entries.get(record.entry)
                    # AdvantageKit logs all 24 PDH channels as a single
                    # double[] record per cycle, so each record gives us a
                    # consistent snapshot across channels at one timestamp.
                    # If a future logger switched to per-channel entries,
                    # the Σ-channels logic below would need to be reworked
                    # to align staggered timestamps first.
                    if entry and entry.type == "double[]":
                        currents = record.get_double_array()
                        # Initialize on first sample
                        if not channel_by_mode:
                            num_channels = len(currents)
                            channel_by_mode = [ByMode() for _ in range(num_channels)]
                            breakers = [
                                _BreakerSimulator(f"Ch {i}", breaker_rating)
                                for i in range(num_channels)
                            ]
                            channel_peak_since_trip = [0.0] * num_channels

                        dt = 0.0
                        if last_channel_ts is not None:
                            dt = (record.timestamp - last_channel_ts) / 1_000_000
                        last_channel_ts = record.timestamp

                        current_mode = tracker.mode
                        total_amps = 0.0
                        for i, amp in enumerate(currents):
                            if i < num_channels:
                                channel_by_mode[i].append(amp, current_mode)
                                total_amps += amp
                                if amp > channel_peak_since_trip[i]:
                                    channel_peak_since_trip[i] = amp
                                if dt > 0 and breakers[i].update(amp, dt):
                                    breaker_trip_events.append(BreakerTripEvent(
                                        label=f"Ch {i}",
                                        timestamp_us=record.timestamp,
                                        peak_current=channel_peak_since_trip[i],
                                    ))
                                    channel_peak_since_trip[i] = 0.0  # reset peak after trip

                        # Drive the summed Total stats + Main breaker sim at
                        # the (dense) channel cadence. This is the preferred
                        # Total source; see PowerReport.total_source.
                        summed_total.append(total_amps, current_mode)
                        main_summed.ingest(total_amps, record.timestamp)

        # Close any open brownout at end of log
        if brownout_active:
            brownout_events.append(BrownoutEvent(
                start_us=brownout_start_us,
                end_us=None,
                mode=brownout_mode,
            ))

    finally:
        mm.close()

    # Pick Total source: prefer summed-from-channels (dense, internally
    # consistent), fall back to reported /TotalCurrent when no channel data.
    if summed_total.overall:
        total_current = summed_total
        main_breaker = main_summed.sim
        breaker_trip_events.extend(main_summed.trip_events)
        total_source = "summed"
    else:
        total_current = reported_total
        main_breaker = main_reported.sim
        breaker_trip_events.extend(main_reported.trip_events)
        total_source = "reported"

    # Build per-channel stats, sorted by overall peak descending.
    # Skip channels with no samples.
    channel_stats = [
        ChannelStats(channel=i, by_mode=bm)
        for i, bm in enumerate(channel_by_mode)
        if bm.overall
    ]
    channel_stats.sort(key=lambda s: s.stats("overall")["max"], reverse=True)

    # Breaker simulation results: main first, then channels that saw any
    # current, sorted by peak stress % descending so the most-stressed show
    # up at the top.
    breaker_sim_results: list[BreakerSimResult] = []
    if total_current.overall:
        breaker_sim_results.append(
            _build_breaker_sim_result(main_breaker, total_current.overall)
        )
    channel_results = [
        _build_breaker_sim_result(b, channel_by_mode[i].overall)
        for i, b in enumerate(breakers)
        if b.peak_current > 0
    ]
    channel_results.sort(key=lambda r: r.peak_stress_pct, reverse=True)
    breaker_sim_results.extend(channel_results)

    return PowerReport(
        voltage=voltage,
        total_current=total_current,
        channel_stats=channel_stats,
        brownout_events=brownout_events,
        breaker_trip_events=breaker_trip_events,
        breaker_sim_results=breaker_sim_results,
        num_channels=num_channels,
        total_source=total_source,
        reported_total_peak=reported_total_peak,
    )
