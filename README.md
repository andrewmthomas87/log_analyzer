# log-analyzer

CLI tool for analyzing `.wpilog` files from FRC robots running [AdvantageKit](https://docs.advantagekit.org/).

## Setup

Requires Python 3.11+.

```sh
uv venv && uv pip install -e .
```

## Usage

```sh
log-analyzer summary path/to/robot.wpilog
```

## The .wpilog format

`.wpilog` is a binary log format defined by [WPILib](https://docs.wpilib.org/en/stable/docs/software/telemetry/datalog.html). Key things to know:

- **Structure**: A 12-byte header (`WPILOG` magic + version + extra header length), followed by a flat sequence of variable-length records. Each record has an entry ID, timestamp (microseconds), and payload.
- **Control records** (entry ID 0) register entries with a name, type string (e.g. `"double"`, `"boolean"`, `"struct:Pose2d"`), and metadata. **Data records** (entry ID > 0) carry the actual values.
- **No index or framing** — the file must be read sequentially from the start; a truncated file (e.g. from a power loss on the robot) is valid up to the last complete record.
- **Timestamps are not guaranteed to be ordered** across different entries.

### AdvantageKit conventions

AdvantageKit logs all robot state every 20ms cycle. Entry names use slash-delimited paths under well-known prefixes:

| Prefix | Contents |
|---|---|
| `/RealOutputs/` | Output values from real robot execution |
| `/ReplayOutputs/` | Output values from log replay simulation |
| `/DriverStation/` | Driver station and controller data |
| `/SystemStats/` | Battery voltage, CAN status, etc. |

Timing data lives at `/RealOutputs/LoggedRobot/FullCycleMS`, `/RealOutputs/LoggedRobot/UserCodeMs`, etc.

### Parser

The reader in `src/log_analyzer/reader.py` is adapted from WPILib's [reference Python implementation](https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/examples/printlog/datalog.py). The full format spec is in the [allwpilib repo](https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/doc/datalog.adoc).
