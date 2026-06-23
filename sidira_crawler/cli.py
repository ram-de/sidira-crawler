"""Command-line interface — used by cron jobs and local dev."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import click

from sidira_crawler import __version__
from sidira_crawler.crawler import run_crawl
from sidira_crawler.honesty import HonestyGateError
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
    """Run a full weekly crawl for a customer.

    On an honesty-gate failure the crawl publishes nothing and this command
    prints the gate report to stderr and exits non-zero, so a failed cron run
    is loud and the prior snapshot's ``latest`` pointer is left intact.
    """
    try:
        uri = run_crawl(customer_id=customer, snapshot_version=snapshot, config_path=config_path)
    except HonestyGateError as exc:
        click.echo(str(exc), err=True)
        raise click.ClickException("honesty gate failed — nothing was published") from exc
    click.echo(f"Pack written to: {uri}")


@cli.command(name="run-dispatch")
@click.option("--customer", required=True, help="Customer ID (Clerk org id for a real tenant).")
@click.option(
    "--crawl-date",
    required=True,
    help="Walk date (YYYY-MM-DD). Used for the default snapshot tag.",
)
@click.option(
    "--snapshot",
    default=None,
    help="Snapshot version tag. Empty = ISO week of --crawl-date.",
)
@click.option(
    "--config-json-env",
    default="CONFIG_JSON",
    show_default=True,
    help=(
        "Name of the env var holding the simulator's dispatch config_json. Read "
        "from the environment, never from a shell arg, so its contents cannot inject."
    ),
)
def run_dispatch(
    customer: str, crawl_date: str, snapshot: str | None, config_json_env: str
) -> None:
    """Run a production crawl from the simulator's dispatch ``config_json`` (#23).

    Bridges the dispatch payload (sidira-simulator #206) and the credentials
    pulled into ``SIDIRA_CRAWL_CREDENTIALS`` into a ``CustomerConfig``, then runs
    the full honesty-gated chain (walk → gate → synthesize → publish). On a gate
    failure nothing is published and this exits non-zero, leaving the prior
    snapshot's ``latest`` pointer intact.
    """
    from sidira_crawler.dispatch import apply_pulled_credentials, build_customer_config
    from sidira_crawler.synthesis.pack import weekly_bucket

    raw = (os.environ.get(config_json_env) or "").strip()
    if not raw:
        raise click.UsageError(f"{config_json_env} is empty — no dispatch config to crawl")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"{config_json_env} is not valid JSON: {exc}") from exc

    try:
        config = build_customer_config(payload, customer_id=customer)
        applied = apply_pulled_credentials(config)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"applied credentials for roles: {', '.join(applied)}")

    snapshot = snapshot or weekly_bucket(crawl_date)
    try:
        uri = run_crawl(customer_id=customer, snapshot_version=snapshot, config=config)
    except HonestyGateError as exc:
        click.echo(str(exc), err=True)
        raise click.ClickException("honesty gate failed — nothing was published") from exc
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


@cli.command()
@click.option(
    "--raw",
    "raw_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Schema-2 raw.jsonl produced by a sandbox walk.",
)
@click.option("--customer", required=True, help="Customer ID the pack is written under.")
@click.option(
    "--snapshot",
    default=None,
    help="Snapshot version tag (e.g. 2026-W24). Defaults to the ISO week of the walk's run_date.",
)
@click.option(
    "--product-id",
    default="default",
    show_default=True,
    help=(
        "Crawl setup id (sidira-simulator #215/#245). 'default' keeps the legacy "
        "customers/<id>/featurepacks path; any other value writes under "
        "customers/<id>/setups/<product-id>/featurepacks so setups never collide."
    ),
)
@click.option(
    "--out",
    "out_dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Write the pack to this local directory instead of the env-selected store.",
)
def synthesize(
    raw_path: str,
    customer: str,
    snapshot: str | None,
    product_id: str,
    out_dir: str | None,
) -> None:
    """Synthesize a Feature Pack from a raw walk (Phase-3 capability synthesis)."""
    from sidira_crawler.llm import get_client
    from sidira_crawler.storage import (
        LocalArtifactStore,
        get_store,
        publish_latest,
        write_diff,
    )
    from sidira_crawler.synthesis.graph import load_raw_jsonl
    from sidira_crawler.synthesis.pack import synthesize_pack, weekly_bucket

    graph = load_raw_jsonl(raw_path)
    if snapshot is None:
        if not graph.run_date:
            raise click.UsageError("--snapshot is required when raw.jsonl has no run_date")
        snapshot = weekly_bucket(graph.run_date)

    pack = synthesize_pack(
        graph,
        get_client(),
        customer_id=customer,
        snapshot_version=snapshot,
        product_id=product_id,
    )

    store = LocalArtifactStore(out_dir) if out_dir else get_store()
    uri = store.write_pack(pack)
    write_diff(
        store,
        customer,
        snapshot,
        {
            "customer_id": customer,
            "snapshot_version": snapshot,
            "prior_snapshot": None,
            "changes": [],
            "note": "diff implementation pending",
        },
        product_id=product_id,
    )
    publish_latest(store, customer, snapshot, product_id=product_id)
    click.echo(
        f"Synthesized {len(pack.capabilities)} capabilities"
        f" ({', '.join(c.name for c in pack.capabilities)})"
    )
    click.echo(f"Pack written to: {uri}")


if __name__ == "__main__":
    cli()
