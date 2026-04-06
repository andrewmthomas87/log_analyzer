from pathlib import Path

import click

from .stats import CYCLE_TIME_KEY, ByMode, analyze_cycle_times, summarize


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


def _print_cycle_stats(label: str, stats: dict[str, float] | None):
    """Print a block of cycle time statistics."""
    if stats is None:
        return
    click.echo(f"{label}:")
    click.echo(f"  samples:  {stats['count']:,}")
    click.echo(f"  min:      {stats['min']:.2f} ms")
    click.echo(f"  median:   {stats['median']:.2f} ms")
    click.echo(f"  mean:     {stats['mean']:.2f} ms")
    click.echo(f"  p95:      {stats['p95']:.2f} ms")
    click.echo(f"  max:      {stats['max']:.2f} ms")
    overruns = int(stats["overruns_20ms"])
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
    _print_cycle_stats("Overall", ct.stats("overall"))

    for section in ByMode.DISPLAY_SECTIONS:
        stats = ct.stats(section)
        if stats is not None:
            click.echo()
            _print_cycle_stats(section.capitalize(), stats)
