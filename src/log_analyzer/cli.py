from pathlib import Path

import click

from .stats import (
    CYCLE_TIME_KEY,
    ByMode,
    SignalCorrelation,
    _compute_stats,
    analyze_brownout_correlation,
    analyze_cycle_times,
    analyze_mechanisms,
    analyze_power,
    summarize,
)


@click.group()
def cli():
    """Analyze FRC robot .wpilog files."""


@cli.command()
@click.argument("logfile", type=click.Path(exists=True, path_type=Path))
def summary(logfile: Path):
    """Print a summary of a .wpilog file."""
    try:
        result = summarize(logfile)
    except ValueError as e:
        raise click.ClickException(str(e))

    mins, secs = divmod(result.duration_secs, 60)
    click.echo(f"Duration:      {int(mins)}m {secs:.1f}s")
    click.echo(f"Entries:       {result.num_entries}")
    click.echo(f"Data records:  {result.num_data_records:,}")

    click.echo()
    click.echo("Logged entries:")
    for entry in sorted(result.entries, key=lambda e: e.name):
        click.echo(f"  {entry.name} ({entry.type})")


def _print_cycle_stats(label: str, vals: list[float]):
    """Print a block of cycle time statistics from raw values."""
    stats = _compute_stats(vals)
    if stats is None:
        return
    click.echo(f"{label}:")
    click.echo(f"  samples:  {stats['count']:,}")
    click.echo(f"  min:      {stats['min']:.2f} ms")
    click.echo(f"  median:   {stats['median']:.2f} ms")
    click.echo(f"  mean:     {stats['mean']:.2f} ms")
    click.echo(f"  p95:      {stats['p95']:.2f} ms")
    click.echo(f"  max:      {stats['max']:.2f} ms")
    # Overrun count is cycle-time-specific (not a generic stat), so we
    # compute it here from the raw list.
    overruns = sum(1 for v in vals if v > 20.0)
    if overruns:
        click.secho(f"  ⚠ {overruns} cycles exceeded 20ms", fg="yellow")


@cli.command()
@click.argument("logfile", type=click.Path(exists=True, path_type=Path))
def timings(logfile: Path):
    """Analyze cycle times from a .wpilog file."""
    try:
        report = analyze_cycle_times(logfile)
    except ValueError as e:
        raise click.ClickException(str(e))

    if not report.has_data:
        click.echo(f"No cycle time data found (looked for {CYCLE_TIME_KEY})")
        return

    ct = report.cycle_times
    _print_cycle_stats("Overall", ct.overall)

    for section in ByMode.DISPLAY_SECTIONS:
        vals = getattr(ct, section)
        if vals:
            click.echo()
            _print_cycle_stats(section.capitalize(), vals)


def _format_timestamp(us: int) -> str:
    """Format a microsecond timestamp as mm:ss.s relative to log start."""
    secs = us / 1_000_000
    mins, secs = divmod(secs, 60)
    return f"{int(mins)}:{secs:04.1f} ({us})"


# --- Power command output helpers ---

# Mode iteration order for per-mode breakdowns in tables. "overall" first, then
# the five display modes from ByMode (disabled, enabled, autonomous, teleop, test).
_POWER_MODES = ["overall", *ByMode.DISPLAY_SECTIONS]


def _section(title: str):
    """Print a bold section header with surrounding whitespace."""
    click.echo()
    click.secho(title, bold=True)
    click.echo()


def _stats_table_header() -> str:
    """Header for the unified stats table (voltage / current)."""
    return (
        f"  {'':<7}  {'Mode':<11}  {'Samples':>8}  "
        f"{'Min':>8} {'Mean':>8} {'Median':>8} "
        f"{'P80':>8} {'P95':>8} {'P99':>8} {'Max':>8}"
    )


def _print_stats_header():
    header = _stats_table_header()
    click.echo(header)
    click.echo("  " + "-" * (len(header) - 2))


def _print_stats_row(label: str, mode: str, s: dict[str, float], show_label: bool):
    label_cell = f"{label:<7}" if show_label else f"{'':<7}"
    click.echo(
        f"  {label_cell}  {mode:<11}  {int(s['count']):>8,}  "
        f"{s['min']:>8.2f} {s['mean']:>8.2f} {s['median']:>8.2f} "
        f"{s['p80']:>8.2f} {s['p95']:>8.2f} {s['p99']:>8.2f} {s['max']:>8.2f}"
    )


def _print_by_mode_block(label: str, by_mode):
    """Print a label block showing stats for each mode that has data."""
    first = True
    for mode in _POWER_MODES:
        s = by_mode.stats(mode)
        if s is None or s["count"] == 0:
            continue
        _print_stats_row(label, mode, s, show_label=first)
        first = False


@cli.command()
@click.argument("logfile", type=click.Path(exists=True, path_type=Path))
@click.option("--breaker-rating", default=40.0, show_default=True,
              help="Per-channel breaker rating (amps) for trip simulation.")
@click.option("--main-breaker-rating", default=120.0, show_default=True,
              help="Main breaker rating (amps) for trip simulation.")
def power(logfile: Path, breaker_rating: float, main_breaker_rating: float):
    """Analyze power usage from a .wpilog file."""
    try:
        report = analyze_power(
            logfile,
            breaker_rating=breaker_rating,
            main_breaker_rating=main_breaker_rating,
        )
    except ValueError as e:
        raise click.ClickException(str(e))

    if not report.has_data:
        click.echo("No power data found.")
        return

    # ---- Voltage ----
    if report.voltage.overall:
        _section("VOLTAGE  (volts)")
        _print_stats_header()
        _print_by_mode_block("Battery", report.voltage)

    # ---- Current (total + per-channel, unified) ----
    active_channels = [
        cs for cs in report.channel_stats
        if (s := cs.stats("overall")) is not None and s["max"] > 0
    ]
    has_total = bool(report.total_current.overall)
    if has_total or active_channels:
        _section("CURRENT  (amps)")
        _print_stats_header()
        if has_total:
            _print_by_mode_block("Total", report.total_current)
        for cs in active_channels:
            click.echo()  # blank line between labels
            _print_by_mode_block(f"Ch {cs.channel}", cs.by_mode)
        if active_channels:
            click.echo()
            click.echo(
                f"  ({len(active_channels)} active channels of {report.num_channels} total; "
                "inactive channels omitted.)"
            )
        # Diagnostic line about Total source — channel-summed is denser and
        # more trustworthy; reported is change-filtered by PDH firmware.
        if has_total:
            click.echo()
            if report.total_source == "summed":
                click.echo(
                    "  (Total = Σ channel currents at the channel cadence. "
                    "The raw /PowerDistribution/TotalCurrent signal is"
                )
                click.echo(
                    "   change-filtered and much sparser, so it's not used here.)"
                )
                if (report.reported_total_peak is not None
                        and report.total_current.overall):
                    summed_peak = max(report.total_current.overall)
                    if summed_peak > 0:
                        diff_pct = abs(
                            report.reported_total_peak - summed_peak
                        ) / summed_peak * 100.0
                        if diff_pct > 10.0:
                            click.secho(
                                f"  ⚠ Reported TotalCurrent peak "
                                f"{report.reported_total_peak:.0f}A vs summed peak "
                                f"{summed_peak:.0f}A ({diff_pct:.0f}% gap) "
                                "— check PDH wiring.",
                                fg="yellow",
                            )
            else:
                click.echo(
                    "  (Total taken from raw /PowerDistribution/TotalCurrent "
                    "— no per-channel data in this log.)"
                )

    # ---- Breaker simulation ----
    if report.breaker_sim_results:
        _section(
            f"BREAKER SIMULATION  (main {main_breaker_rating:.0f}A, "
            f"channels {breaker_rating:.0f}A)"
        )
        click.echo(
            "  Thermal I²t model: heat += (I² − rating²)·dt, trip at 9 × rating².  "
            "Heat in A²·s."
        )
        click.echo()
        group_header = (
            f"  {'':<7}  {'':>5}  {'':>6}  "
            f"{'Current (A)':^42}  "
            f"{'Overage':^17}  "
            f"{'Thermal (A²·s)':^52}  "
            f"{'Stress %':^33}  "
            f"{'':>5}"
        )
        col_header = (
            f"  {'Breaker':<7}  {'Rate':>5}  {'Samps':>6}  "
            f"{'Peak':>7} {'P99':>7} {'P95':>7} {'P80':>7} {'Mean':>7}  "
            f"{'Peak':>7} {'Time':>8}  "
            f"{'Peak':>9} {'P99':>9} {'P95':>9} {'P80':>9} {'Mean':>9}  "
            f"{'Peak':>6} {'P99':>6} {'P95':>6} {'P80':>6} {'Mean':>6}  "
            f"{'Trips':>5}"
        )
        click.echo(group_header)
        click.echo(col_header)
        click.echo("  " + "-" * (len(col_header) - 2))
        for r in report.breaker_sim_results:
            line = (
                f"  {r.label:<7}  {r.rating:>4.0f}A  {r.sample_count:>6,}  "
                f"{r.peak_current:>6.1f}A {r.p99_current:>6.1f}A {r.p95_current:>6.1f}A {r.p80_current:>6.1f}A {r.mean_current:>6.1f}A  "
                f"{r.peak_overage:>+6.1f}A {r.time_over_rating_s:>7.2f}s  "
                f"{r.peak_heat:>9.0f} {r.p99_heat:>9.0f} {r.p95_heat:>9.0f} {r.p80_heat:>9.0f} {r.mean_heat:>9.0f}  "
                f"{r.peak_stress_pct:>5.1f}% {r.p99_stress_pct:>5.1f}% {r.p95_stress_pct:>5.1f}% {r.p80_stress_pct:>5.1f}% {r.mean_stress_pct:>5.1f}%  "
                f"{r.trip_count:>5}"
            )
            if r.trip_count > 0:
                click.secho(line, fg="red")
            elif r.peak_stress_pct >= 50.0:
                click.secho(line, fg="yellow")
            else:
                click.echo(line)

        # Thresholds reference — tiny so it fits under the table naturally.
        click.echo()
        seen_ratings: dict[float, float] = {}
        for r in report.breaker_sim_results:
            seen_ratings.setdefault(r.rating, r.heat_threshold)
        thresholds = "  ".join(
            f"{rating:.0f}A → {thresh:,.0f}"
            for rating, thresh in sorted(seen_ratings.items())
        )
        click.echo(f"  Trip thresholds (A²·s):  {thresholds}")

    # ---- Events ----
    has_brownouts = bool(report.brownout_events)
    has_trips = bool(report.breaker_trip_events)
    _section("EVENTS")
    if has_brownouts:
        click.secho(f"  Brownouts ({len(report.brownout_events)}):", fg="red")
        for evt in report.brownout_events:
            end = _format_timestamp(evt.end_us) if evt.end_us else "end of log"
            duration = ""
            if evt.end_us:
                dur_ms = (evt.end_us - evt.start_us) / 1_000
                duration = f" ({dur_ms:.0f}ms)"
            click.echo(
                f"    {_format_timestamp(evt.start_us)} → {end}{duration}  "
                f"[{evt.mode.value}]"
            )
    else:
        click.echo("  No brownouts detected.")

    if has_trips:
        click.echo()
        click.secho(f"  Simulated breaker trips ({len(report.breaker_trip_events)}):", fg="red")
        for evt in report.breaker_trip_events:
            click.echo(
                f"    {evt.label:<6} at {_format_timestamp(evt.timestamp_us)}  "
                f"(peak {evt.peak_current:.1f}A)"
            )
    else:
        click.echo("  No simulated breaker trips.")

    # ---- How to read this ----
    _section("NOTES")
    click.echo("  Stats columns:")
    click.echo("    Min / Max         — extremes (single worst/best sample)")
    click.echo("    Mean              — arithmetic average; skewed by spikes")
    click.echo("    Median            — 50th percentile; robust to spikes")
    click.echo("    P80 / P95 / P99   — percentiles; the \"tails\"")
    click.echo("                        (e.g. P99 = value exceeded by only 1% of samples)")
    click.echo()
    click.echo("  Modes:")
    click.echo("    overall      — every sample in the log")
    click.echo("    disabled     — robot disabled by driver station")
    click.echo("    enabled      — any non-disabled state (auto + teleop + test)")
    click.echo("    autonomous / teleop / test — specific enabled modes")
    click.echo()
    click.echo("  Voltage thresholds:")
    click.echo("    > 12.0 V     — healthy battery at rest")
    click.echo("    10–12 V      — normal under load")
    click.echo("    < 7.5 V      — warning: heavy sag, check battery health")
    click.echo("    < 7.0 V      — brownout: roboRIO disables actuators")
    click.echo()
    click.echo("  Breaker simulation:")
    click.echo("    FRC auto-resetting breakers trip on accumulated heat (I²·t),")
    click.echo("    not instantaneous current. A 40A breaker holds 40A forever but")
    click.echo("    trips in ~3s at 80A. The simulator integrates heat at rate")
    click.echo("    (I² − rating²) and trips at 9 × rating² (calibrated to the real")
    click.echo("    MX5 curve). After a trip it enters a 5 s cooldown — real breakers")
    click.echo("    take 5–15 s, so cooldown here is on the short end.")
    click.echo()
    click.echo("    Peak Stress % = peak_heat / trip_threshold. Read it as:")
    click.echo("      < 25 %     — fine, lots of headroom")
    click.echo("     25–50 %     — notable load but safe")
    click.echo("     50–90 %     — ⚠ got meaningfully close to tripping")
    click.echo("     90–100 %    — almost tripped")
    click.echo("      = 100 %    — trip event recorded (see EVENTS)")
    click.echo()
    click.echo("    Mean Stress % tells you how loaded the breaker was on average.")
    click.echo("    A high Peak with low Mean = brief overload. A high Mean = sustained.")
    click.echo()
    click.echo("    Overage columns: peak amps above rating, and total seconds above.")
    click.echo("    A breaker with +20A peak over rating for 5s is far more stressed")
    click.echo("    than one with +5A for 5s, because the I² term is nonlinear.")
    click.echo()
    click.echo("  Data-quality filters (applied silently):")
    click.echo("    • Voltage samples of exactly 0.0 V are dropped (non-physical;")
    click.echo("      PDH reports 0 at init and on transient CAN hiccups).")
    click.echo("    • The breaker sim clamps per-step dt to 100 ms. A change-filtered")
    click.echo("      signal with multi-second gaps (common on /TotalCurrent) would")
    click.echo("      otherwise integrate fake heat across the gap. As a side effect,")
    click.echo("      the \"Time over rating\" column is a lower bound on wall-clock")
    click.echo("      time — we can't know what happened inside a gap.")


# --- Mechanisms command ---


@cli.command()
@click.argument("logfile", type=click.Path(exists=True, path_type=Path))
def mechanisms(logfile: Path):
    """Analyze mechanism motor stats from a .wpilog file."""
    try:
        reports = analyze_mechanisms(logfile)
    except ValueError as e:
        raise click.ClickException(str(e))

    active = [r for r in reports if r.has_data]
    if not active:
        click.echo("No mechanism data found.")
        return

    for report in active:
        _section(report.name.upper())
        _print_stats_header()

        if report.current.overall:
            _print_by_mode_block("Cur(A)", report.current)

        if report.voltage.overall:
            click.echo()
            _print_by_mode_block("Vol(V)", report.voltage)

        if report.temperature.overall:
            click.echo()
            _print_by_mode_block("Tmp(C)", report.temperature)

        if report.velocity.overall:
            click.echo()
            vel_label = "V(r/s)" if report.vel_unit == "rad/s" else "V(RPS)"
            _print_by_mode_block(vel_label, report.velocity)

        if report.energy_wh > 0:
            click.echo()
            motor_note = " (4 motors summed)" if report.name in ("Drive Motors", "Steer Motors") else ""
            click.echo(f"  Energy: {report.energy_wh:.4f} Wh{motor_note}")

    _section("NOTES")
    click.echo("  Each row is one robot mode. Modes with no samples are omitted.")
    click.echo()
    click.echo("  Label column key:")
    click.echo("    Cur(A)   — stator current (amps)")
    click.echo("    Vol(V)   — applied motor voltage (volts; signed: negative = reverse)")
    click.echo("    Tmp(C)   — motor temperature (°C)")
    click.echo("    V(RPS)   — velocity in rotations per second")
    click.echo("    V(r/s)   — velocity in radians per second (drive/steer modules)")
    click.echo()
    click.echo("  Energy (Wh):")
    click.echo("    Integrated max(0, V × I) per motor, summed across the log.")
    click.echo("    Only counts power drawn from the battery; regenerative braking")
    click.echo("    intervals (negative V × I) contribute 0.")
    click.echo("    Uses the last-known voltage at each current sample.")
    click.echo("    dt is capped at 100 ms per step to avoid inflating energy across")
    click.echo("    log gaps. For grouped mechanisms (Drive/Steer) the total covers")
    click.echo("    all 4 motors combined.")


# --- Brownout correlate command ---


def _corr_header():
    click.echo(
        f"  {'State':<10}  {'Samples':>8}  "
        f"{'Min':>8} {'Mean':>8} {'Median':>8} {'P95':>8} {'Max':>8}  {'ΔMean%':>8}"
    )
    click.echo("  " + "-" * 76)


def _corr_rows(corr: SignalCorrelation, brownout_event_count: int):
    ns = corr.normal_stats()
    bs = corr.brownout_stats()
    delta = corr.mean_delta_pct()
    delta_str = f"{delta:+.0f}%" if delta is not None else ""

    if ns:
        click.echo(
            f"  {'normal':<10}  {int(ns['count']):>8,}  "
            f"{ns['min']:>8.2f} {ns['mean']:>8.2f} {ns['median']:>8.2f} "
            f"{ns['p95']:>8.2f} {ns['max']:>8.2f}"
        )
    if bs:
        line = (
            f"  {'brownout':<10}  {int(bs['count']):>8,}  "
            f"{bs['min']:>8.2f} {bs['mean']:>8.2f} {bs['median']:>8.2f} "
            f"{bs['p95']:>8.2f} {bs['max']:>8.2f}  {delta_str:>8}"
        )
        if delta is not None and abs(delta) >= 100:
            click.secho(line, fg="red")
        elif delta is not None and abs(delta) >= 50:
            click.secho(line, fg="yellow")
        else:
            click.echo(line)
    elif brownout_event_count > 0:
        click.echo(f"  {'brownout':<10}  {'(no samples)':>8}")


@cli.command("brownout-correlate")
@click.argument("logfile", type=click.Path(exists=True, path_type=Path))
def brownout_correlate(logfile: Path):
    """Correlate mechanism and PDH signals against brownout events."""
    try:
        report = analyze_brownout_correlation(logfile)
    except ValueError as e:
        raise click.ClickException(str(e))

    n = report.brownout_event_count
    if n == 0:
        click.echo("No brownout events detected in this log.")
        if report.has_data:
            click.echo("(Signal data was found; use 'mechanisms' or 'power' for general analysis.)")
        return

    click.secho(f"{n} brownout event(s) detected.", bold=True)

    if report.voltage.normal or report.voltage.brownout:
        _section("VOLTAGE  (volts)")
        _corr_header()
        _corr_rows(report.voltage, n)

    if report.total_current.normal or report.total_current.brownout:
        _section("TOTAL CURRENT  (amps)")
        _corr_header()
        _corr_rows(report.total_current, n)

    if report.channel_correlations:
        _section("PDH CHANNELS  (amps, ranked by brownout mean)")
        click.echo(
            f"  {'Ch':<5}  {'Norm mean':>10}  {'Norm max':>9}  "
            f"{'BO mean':>8}  {'BO max':>8}  {'BO samps':>9}  {'ΔMean%':>8}"
        )
        click.echo("  " + "-" * 72)
        for ch, corr in report.channel_correlations:
            ns = corr.normal_stats()
            bs = corr.brownout_stats()
            delta = corr.mean_delta_pct()
            delta_str = f"{delta:+.0f}%" if delta is not None else "n/a"
            nm = f"{ns['mean']:>10.2f}" if ns else f"{'—':>10}"
            nx = f"{ns['max']:>9.2f}" if ns else f"{'—':>9}"
            bm = f"{bs['mean']:>8.2f}" if bs else f"{'—':>8}"
            bx = f"{bs['max']:>8.2f}" if bs else f"{'—':>8}"
            bc = f"{int(bs['count']):>9,}" if bs else f"{'—':>9}"
            line = f"  {ch:<5}  {nm}  {nx}  {bm}  {bx}  {bc}  {delta_str:>8}"
            if delta is not None and abs(delta) >= 100:
                click.secho(line, fg="red")
            elif delta is not None and abs(delta) >= 50:
                click.secho(line, fg="yellow")
            else:
                click.echo(line)

    active_mechs = [m for m in report.mechanisms if m.has_data]
    if active_mechs:
        _section("MECHANISMS  (sorted by brownout current mean, desc)")
        for mech in active_mechs:
            click.echo()
            click.secho(f"  {mech.name}", bold=True)
            _corr_header()
            signals = [
                ("Current(A)", mech.current),
                ("Voltage(V)", mech.voltage),
                ("Temp(C)", mech.temperature),
                (f"Vel({'r/s' if mech.vel_unit == 'rad/s' else 'RPS'})", mech.velocity),
            ]
            for label, corr in signals:
                if corr.normal or corr.brownout:
                    click.echo(f"  {label}")
                    _corr_rows(corr, n)

    _section("NOTES")
    click.echo("  ΔMean% = (brownout mean − normal mean) / |normal mean| × 100.")
    click.echo("  A large positive ΔMean% on current means the mechanism drew")
    click.echo("  significantly more during brownout conditions.")
    click.echo()
    click.echo("  Causality is not guaranteed: a mechanism may be elevated *because*")
    click.echo("  another load caused the brownout, not because it caused it. Use")
    click.echo("  the 'power' command's brownout timestamps to inspect what was")
    click.echo("  happening in the seconds leading up to each event.")
    click.echo()
    click.echo("  Color coding on ΔMean%:")
    click.echo("    yellow — meaningfully elevated (|ΔMean%| ≥ 50%)")
    click.echo("    red    — strongly elevated (|ΔMean%| ≥ 100%)")
    click.echo()
    click.echo("  Mechanisms sorted by brownout-period current mean (highest first).")
    click.echo("  PDH channels sorted by brownout-period mean current (highest first).")
    click.echo("  Mechanisms with no data in this log are omitted.")
