# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
# Install (editable, into venv)
uv venv && uv pip install -e .

# Run CLI
.venv/bin/log-analyzer summary path/to/file.wpilog

# Run all tests
.venv/bin/pytest tests/ -v

# Run a single test
.venv/bin/pytest tests/test_summary.py::test_cycle_times_by_mode -v
```

## Architecture

CLI tool for analyzing `.wpilog` binary log files from FRC robots running AdvantageKit.

- **`reader.py`** — Vendored and adapted from WPILib's [reference Python DataLog reader](https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/examples/printlog/datalog.py). Parses the binary format: header validation, record iteration, and typed value decoding. Intentionally kept close to upstream — avoid unnecessary style changes (e.g. the camelCase `floatStruct`/`doubleStruct` are inherited).
- **`stats.py`** — Analysis layer. Takes a file path, uses `reader.py` to iterate records, and returns dataclasses (`LogSummary`, `CycleTimeReport`). Shared infrastructure: `ModeTracker` tracks the current robot mode from DriverStation entries, and `ByMode` buckets float values by mode. New analyses should reuse these.
- **`cli.py`** — Click CLI. Thin layer that calls into `stats.py` and formats output. Subcommands go here.

Data flows one way: `cli.py` → `stats.py` → `reader.py`. The reader knows nothing about AdvantageKit conventions; AdvantageKit-specific key paths (like `/RealOutputs/LoggedRobot/FullCycleMS`) live in `stats.py`.

## .wpilog format notes

AdvantageKit entry names use slash-delimited paths. Real robot outputs live under `/RealOutputs/` (not bare names like `LoggedRobot/...`). See the README for a fuller format reference.

## Testing

Tests construct real `.wpilog` binary data using helpers (`_write_record`, `_make_start_payload`) rather than mocking the reader. New tests should follow this pattern to exercise the full parsing pipeline.
