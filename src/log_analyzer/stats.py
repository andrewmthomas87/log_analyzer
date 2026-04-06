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
    p95_idx = int(len(sorted_vals) * 0.95)
    return {
        "min": min(vals),
        "median": statistics.median(vals),
        "mean": statistics.mean(vals),
        "p95": sorted_vals[min(p95_idx, len(sorted_vals) - 1)],
        "max": max(vals),
        "count": len(vals),
        "overruns_20ms": sum(1 for v in vals if v > 20.0),
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
