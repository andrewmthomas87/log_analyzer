# log-analyzer

CLI tool for analyzing `.wpilog` files from FRC robots running [AdvantageKit](https://docs.advantagekit.org/).

## Setup

Requires Python 3.11+.

```sh
uv venv && uv pip install -e .
```

## Usage

```sh
# Basic log summary (duration, entries, record count)
log-analyzer summary path/to/robot.wpilog

# Cycle time analysis by robot mode (disabled, auto, teleop, test)
log-analyzer timings path/to/robot.wpilog

# Power usage: voltage, current by channel/mode, brownouts, breaker trip risk
log-analyzer power path/to/robot.wpilog \
    [--breaker-rating 40] [--main-breaker-rating 120]
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
| `/RealOutputs/` | Output values computed by user code on the real robot |
| `/ReplayOutputs/` | Output values from log replay simulation |
| `/DriverStation/` | Driver station and controller data |
| `/SystemStats/` | roboRIO system info: `BatteryVoltage`, `BrownedOut`, `CANBus`, etc. |
| `/PowerDistribution/` | PDH/PDP readings: `Voltage`, `TotalCurrent`, `ChannelCurrent[]` |

**Inputs vs. outputs.** Hardware inputs (things the robot *reads* — PDH, DriverStation, SystemStats) live at their own top-level prefixes, **not** under `/RealOutputs/`. Only values the user code publishes get the `/RealOutputs/` prefix. When wiring a new analysis, check an actual log with `log-analyzer summary` to find the exact key path rather than guessing.

Examples:
- Cycle time: `/RealOutputs/LoggedRobot/FullCycleMS` (user-code output)
- Per-channel current: `/PowerDistribution/ChannelCurrent` (hardware input)
- Brownout flag: `/SystemStats/BrownedOut` (hardware input)

### Parser

The reader in `src/log_analyzer/reader.py` is adapted from WPILib's [reference Python implementation](https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/examples/printlog/datalog.py). The full format spec is in the [allwpilib repo](https://github.com/wpilibsuite/allwpilib/blob/main/wpiutil/doc/datalog.adoc).
