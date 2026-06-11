"""Command-line interface — used by cron jobs and local dev."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import click

from sidira_crawler import __version__
from sidira_crawler.crawler import run_crawl
from sidira_crawler.walker import DEFAULT_DEPTH
from sidira_crawler.walker import walk as run_walk


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """sidira-crawler — Feature Pack producer for the Sidira simulation engine."""


@cli.command()
@click.option("--customer", required=True, help="Customer ID (e.g. acme)")
@click.option(
    "--snapshot",
    required=True,
    help="Snapshot version tag (e.g. 2026-W21). Used as the immutable pack version.",
)
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to customer config YAML. Defaults to config/customers/<customer>.yaml.",
)
def run(customer: str, snapshot: str, config_path: str | None) -> None:
    """Run a full weekly crawl for a customer."""
    uri = run_crawl(customer_id=customer, snapshot_version=snapshot, config_path=config_path)
    click.echo(f"Pack written to: {uri}")


@cli.command()
@click.option("--url", required=True, help="Entry URL to walk from.")
@click.option("--role", required=True, help="Role label recorded on observations (e.g. guest).")
@click.option("--username", default=None, help="Username/email for login walls (optional).")
@click.option("--password", default=None, help="Password for login walls (optional).")
@click.option(
    "--depth",
    default=DEFAULT_DEPTH,
    show_default=True,
    type=int,
    help="Depth budget: user-action hops from the entry URL.",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    help="Write the RawWalk as JSON to this path.",
)
def walk(
    url: str,
    role: str,
    username: str | None,
    password: str | None,
    depth: int,
    out_path: str | None,
) -> None:
    """Run a single ad-hoc walk (local dev) and print a one-line summary."""
    result = run_walk(url, role, username=username, password=password, depth=depth)
    if out_path:
        Path(out_path).write_text(json.dumps(asdict(result), indent=2))
    click.echo(
        f"Walked {url} as {role}: "
        f"{len(result.observations)} screens, {len(result.transitions)} transitions"
        + (f" -> {out_path}" if out_path else "")
    )


if __name__ == "__main__":
    cli()
