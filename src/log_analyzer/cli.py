from pathlib import Path

import click

from .stats import CYCLE_TIME_KEY, analyze_cycle_times, summarize


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


@cli.command()
@click.argument("logfile", type=click.Path(exists=True, path_type=Path))
def timings(logfile: Path):
    """Analyze cycle times from a .wpilog file."""
    try:
        report = analyze_cycle_times(logfile)
    except ValueError as e:
        raise click.ClickException(str(e))

    stats = report.stats
    if stats:
        click.echo(f"Cycle times ({CYCLE_TIME_KEY}):")
        click.echo(f"  samples:  {stats['count']:,}")
        click.echo(f"  min:      {stats['min']:.2f} ms")
        click.echo(f"  median:   {stats['median']:.2f} ms")
        click.echo(f"  mean:     {stats['mean']:.2f} ms")
        click.echo(f"  p95:      {stats['p95']:.2f} ms")
        click.echo(f"  max:      {stats['max']:.2f} ms")
        if stats["max"] > 20.0:
            click.secho(
                f"  ⚠ {sum(1 for v in report.values_ms if v > 20.0)} cycles exceeded 20ms",
                fg="yellow",
            )
    else:
        click.echo(f"No cycle time data found (looked for {CYCLE_TIME_KEY})")
