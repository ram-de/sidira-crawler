"""Command-line interface — used by cron jobs and local dev."""

from __future__ import annotations

import click

from sidira_crawler import __version__
from sidira_crawler.crawler import run_crawl


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


if __name__ == "__main__":
    cli()
