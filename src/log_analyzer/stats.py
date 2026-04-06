import mmap
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .reader import DataLogReader, StartRecordData


@dataclass
class LogSummary:
    """Basic summary of a .wpilog file."""

    duration_secs: float
    num_entries: int
    num_data_records: int
    entries: list[StartRecordData] = field(repr=False)


@dataclass
class CycleTimeReport:
    """Cycle time analysis from a .wpilog file."""

    values_ms: list[float] = field(repr=False)

    @property
    def has_data(self) -> bool:
        return len(self.values_ms) > 0

    @property
    def stats(self) -> dict[str, float] | None:
        if not self.has_data:
            return None
        vals = self.values_ms
        sorted_vals = sorted(vals)
        p95_idx = int(len(sorted_vals) * 0.95)
        return {
            "min": min(vals),
            "median": statistics.median(vals),
            "mean": statistics.mean(vals),
            "p95": sorted_vals[min(p95_idx, len(sorted_vals) - 1)],
            "max": max(vals),
            "count": len(vals),
        }


CYCLE_TIME_KEY = "/RealOutputs/LoggedRobot/FullCycleMS"


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
    """Parse a .wpilog file and extract cycle time data."""
    mm, reader = _open_reader(path)
    try:
        entries: dict[int, StartRecordData] = {}
        cycle_times: list[float] = []
        cycle_time_entry_id: int | None = None

        for record in reader:
            if record.is_start():
                data = record.get_start_data()
                entries[data.entry] = data
                if data.name == CYCLE_TIME_KEY:
                    cycle_time_entry_id = data.entry
            elif record.is_finish():
                entry_id = record.get_finish_entry()
                if entry_id == cycle_time_entry_id:
                    cycle_time_entry_id = None
            elif not record.is_control():
                if record.entry == cycle_time_entry_id:
                    entry = entries.get(record.entry)
                    if entry and entry.type == "double":
                        cycle_times.append(record.get_double())
    finally:
        mm.close()

    return CycleTimeReport(values_ms=cycle_times)
