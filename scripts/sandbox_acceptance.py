#!/usr/bin/env python3
"""Sandbox acceptance harness — runs the walker against a customer sandbox and
judges the raw output against the sandbox's ground-truth manifest.

Two-part gate
-------------
1. The walker ENGINE (``sidira_crawler``) is generic: it must contain zero
   customer-specific selectors, routes, or copy.
2. The acceptance JUDGE (this script + a judge config JSON) is allowed to know
   the sandbox: it scores walk outcomes against the ground-truth manifest
   shipped inside the sandbox. All sandbox knowledge lives in the judge config
   (e.g. ``acceptance/streamly-sandbox.json``) — this script itself is a
   sandbox-agnostic harness.

Matching is deterministic: every matcher is a data-driven predicate over the
raw walk output (routes, screen-id flag tokens, data-testids, visible copy,
URLs, transition triggers). There is no fuzzy scoring.

Modes
-----
- ``--image <ref>``: CI mode. Boots a FRESH docker container per walk on an
  ephemeral port, waits for /healthz, removes the container afterwards. The
  ground-truth manifest is extracted from the first booted container.
- ``--server-cmd '<cmd>'``: local mode. Starts a fresh server process per walk
  (``{port}`` in the command is substituted; ``PORT`` is also set in the
  environment), waits for /healthz, kills the process group afterwards.
  Requires ``--manifest`` pointing at the ground-truth manifest on disk.

Entry paths
-----------
Each role may declare several ``entry_paths`` in the judge config; every entry
path gets its own walk on a freshly booted sandbox (and a fresh browser). An
entry may be a plain path string (walked at the config-wide default depth) or
``{"path": "/...", "depth": N}`` — secondary entries exist to reach specific
deep states, not to re-explore everything, so they can carry a smaller depth
budget than the primary walk. For judging, a role's walks are MERGED for
screen coverage (union of observations by screen_id), but transition matchers
are evaluated against each individual walk — never against the merged graph,
because stitching edges from different walks would fabricate paths no single
walk took (login-roundtrip's downstream reachability must hold within one
walk's transition graph).

Parallelism
-----------
``--jobs N`` runs up to N walks at once (default 1 = sequential). Every walk
already boots its own sandbox and its own browser, so walks are independent;
the walker drives *sync* Playwright, which must not be shared across threads,
so parallelism is PROCESS-level (a spawn-based worker pool). Ports are
preassigned per walk (bound simultaneously, so they are distinct) before any
worker starts, results are collected and re-ordered into fixed plan order
before raw.jsonl is written, and a crashed walk fails the gate with its error
visible — the judged result is identical to a sequential run.

Gates (any failure exits nonzero): a role below its coverage threshold, a
missing mustDiscover entry, a determinism mismatch (two identical walks of the
determinism role's primary entry path must observe identical screen_id sets),
or a walk crash.

The judge functions are pure and importable — tests/test_acceptance.py loads
this module directly and exercises them with synthetic walks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# ---------------------------------------------------------------------------
# Pure judge: screen-id parsing and matchers
# ---------------------------------------------------------------------------

#: Trailing structural-digest token in a walker screen_id, e.g. ``s2ac4f9``.
_DIGEST_RE = re.compile(r"^s[0-9a-f]{6}$")


def screen_id_parts(screen_id: str) -> tuple[str, frozenset[str]]:
    """Split a walker screen_id into (route, flag tokens).

    ``/browse::exhausted+s2ac4f9`` -> (``/browse``, {``exhausted``}). The
    structural digest token is dropped — judges match on routes and semantic
    flags, never on the digest.
    """
    route, _, state = screen_id.partition("::")
    flags = frozenset(t for t in state.split("+") if t and not _DIGEST_RE.match(t))
    return route, flags


def normalize_route(path: str) -> str:
    """Collapse likely-ID path segments the same way the walker reports routes
    (digit-bearing segments become ``:id``): ``/watch/t-101`` -> ``/watch/:id``.

    Reimplemented here on purpose — the judge never imports engine internals.
    """
    path = path.split("?")[0].split("#")[0]
    segments = [s for s in path.split("/") if s]
    return "/" + "/".join(":id" if any(c.isdigit() for c in s) else s for s in segments)


def _obs_text(obs: dict) -> str:
    return "\n".join(list(obs.get("headings", [])) + list(obs.get("visible_copy", [])))


def match_observation(matcher: dict, obs: dict) -> bool:
    """Evaluate one observation matcher against one observation. All present
    predicate keys must hold (AND semantics)."""
    route, flags = screen_id_parts(obs["screen_id"])
    if "route" in matcher and matcher["route"] != route:
        return False
    if any(f not in flags for f in matcher.get("flags", [])):
        return False
    if any(f in flags for f in matcher.get("flags_absent", [])):
        return False
    if "url_contains" in matcher and matcher["url_contains"] not in obs.get("url", ""):
        return False
    if "testid" in matcher and matcher["testid"] not in obs.get("data_testids", []):
        return False
    if "testid_absent" in matcher and matcher["testid_absent"] in obs.get("data_testids", []):
        return False
    if "copy_regex" in matcher and not re.search(matcher["copy_regex"], _obs_text(obs)):
        return False
    return True


def match_screen(matcher: dict, walk: dict) -> str | None:
    """Return the screen_id of the first observation matching, else None."""
    for obs in walk.get("observations", []):
        if match_observation(matcher, obs):
            return obs["screen_id"]
    return None


def match_transition_generic(matcher: dict, walk: dict) -> str | None:
    """Plain transition matcher: trigger regex + from/to route + flag predicates."""
    for tr in walk.get("transitions", []):
        if "trigger_regex" in matcher and not re.search(matcher["trigger_regex"], tr["trigger"]):
            continue
        from_route, from_flags = screen_id_parts(tr["from_screen_id"])
        to_route, to_flags = screen_id_parts(tr["to_screen_id"])
        if "from_route" in matcher and matcher["from_route"] != from_route:
            continue
        if "to_route" in matcher and matcher["to_route"] != to_route:
            continue
        if any(f not in from_flags for f in matcher.get("from_flags", [])):
            continue
        if any(f not in to_flags for f in matcher.get("to_flags", [])):
            continue
        return f"{tr['from_screen_id']} -> {tr['to_screen_id']} ({tr['trigger']})"
    return None


def match_login_roundtrip(matcher: dict, walk: dict) -> str | None:
    """Login ?next= round-trip: there exists a login-submit transition out of a
    login screen where a ``next=`` parameter is carried through the submit, and
    the decoded next target route is reachable downstream of the submit in the
    transition graph.

    The parameter may sit on either end of the submit transition: on the login
    screen's own URL (the classic ``/login?next=...`` wall) or on the screen
    the submit lands on (the app forwards the sanitized next to its post-login
    interstitial, e.g. ``/profiles?next=...``). Both are the same evidence —
    redirect state survived the login submit — and the engine's one-observation-
    per-screen_id dedupe (query strings don't fork screen identity) means the
    wall URL may only be visible on the landing side. Either way the decoded
    target must actually be reached downstream; nothing passes on the param
    alone.
    """
    trigger = matcher.get("trigger", "submit login form")
    param = matcher.get("param", "next")
    login_flag = matcher.get("login_flag", "login")
    obs_by_id = {o["screen_id"]: o for o in walk.get("observations", [])}
    downstream: dict[str, list[str]] = {}
    for tr in walk.get("transitions", []):
        downstream.setdefault(tr["from_screen_id"], []).append(tr["to_screen_id"])

    def param_value(screen_id: str) -> str | None:
        obs = obs_by_id.get(screen_id)
        if obs is None:
            return None
        values = parse_qs(urlsplit(obs.get("url", "")).query).get(param)
        return values[0] if values and values[0] else None

    for tr in walk.get("transitions", []):
        if tr["trigger"] != trigger:
            continue
        from_obs = obs_by_id.get(tr["from_screen_id"])
        if from_obs is None:
            continue
        _, flags = screen_id_parts(from_obs["screen_id"])
        if login_flag not in flags:
            continue
        next_value = param_value(tr["from_screen_id"]) or param_value(tr["to_screen_id"])
        if next_value is None:
            continue
        target_route = normalize_route(next_value)
        # BFS downstream of the submit's landing screen.
        seen: set[str] = set()
        stack = [tr["to_screen_id"]]
        while stack:
            sid = stack.pop()
            if sid in seen:
                continue
            seen.add(sid)
            if screen_id_parts(sid)[0] == target_route:
                return (
                    f"{tr['from_screen_id']} -> {tr['to_screen_id']} "
                    f"(next={next_value!r} reaches {target_route} via {sid})"
                )
            stack.extend(downstream.get(sid, ()))
    return None


def match_scroll_growth(matcher: dict, walk: dict) -> str | None:
    """Scroll growth: a scroll transition on the given route whose to-screen
    differs from its from-screen (i.e. scrolling changed the screen state)."""
    trigger = matcher.get("trigger", "scroll feed")
    for tr in walk.get("transitions", []):
        if tr["trigger"] != trigger:
            continue
        if tr["from_screen_id"] == tr["to_screen_id"]:
            continue
        if "route" in matcher and screen_id_parts(tr["from_screen_id"])[0] != matcher["route"]:
            continue
        return f"{tr['from_screen_id']} -> {tr['to_screen_id']} ({tr['trigger']})"
    return None


_TRANSITION_MATCHERS = {
    "transition": match_transition_generic,
    "login-roundtrip": match_login_roundtrip,
    "scroll-growth": match_scroll_growth,
}


def match_transition(matcher: dict, walk: dict) -> str | None:
    kind = matcher.get("kind", "transition")
    fn = _TRANSITION_MATCHERS.get(kind)
    if fn is None:
        raise ValueError(f"unknown transition matcher kind: {kind!r}")
    return fn(matcher, walk)


# ---------------------------------------------------------------------------
# Pure judge: coverage, mustDiscover, determinism, verdict
# ---------------------------------------------------------------------------


def validate_config(manifest: dict, config: dict) -> None:
    """Hard-error on judge-config gaps so screens can't silently score as
    unmatchable (or worse, get quietly skipped)."""
    missing = [s["id"] for s in manifest["screens"] if s["id"] not in config["screen_matchers"]]
    if missing:
        raise SystemExit(f"judge config has no screen matcher for: {', '.join(missing)}")
    must_transitions = [e["id"] for e in manifest.get("mustDiscover", []) if e["kind"] == "transition"]
    missing = [t for t in must_transitions if t not in config.get("transition_matchers", {})]
    if missing:
        raise SystemExit(f"judge config has no transition matcher for: {', '.join(missing)}")
    for role, spec in config["roles"].items():
        paths = spec.get("entry_paths")
        if paths is None:
            continue
        if not isinstance(paths, list) or not paths or not all(map(_entry_ok, paths)):
            raise SystemExit(
                f"role {role!r}: entry_paths entries must be '/'-prefixed path strings "
                "or {'path': '/...', 'depth': N >= 1} objects (non-empty list)"
            )


def _entry_ok(item) -> bool:
    """One entry_paths element: a '/'-prefixed string, or a dict with a
    '/'-prefixed ``path`` and an optional positive-int ``depth``."""
    if isinstance(item, str):
        return item.startswith("/")
    if isinstance(item, dict):
        path = item.get("path")
        depth = item.get("depth", 1)
        return (
            isinstance(path, str)
            and path.startswith("/")
            and isinstance(depth, int)
            and not isinstance(depth, bool)
            and depth >= 1
        )
    return False


def role_entry_specs(config: dict, role: str) -> list[tuple[str, int]]:
    """(entry path, walk depth) pairs for one role's walks; one fresh-sandbox
    walk per pair. Plain string entries use the config-wide default depth;
    ``{"path": ..., "depth": N}`` entries override depth for that walk alone.
    Defaults to the config-wide ``entry_path`` (itself defaulting to "/")."""
    default_depth = config.get("depth", 6)
    items = config["roles"][role].get("entry_paths")
    if not items:
        return [(config.get("entry_path", "/"), default_depth)]
    return [
        (item, default_depth)
        if isinstance(item, str)
        else (item["path"], item.get("depth", default_depth))
        for item in items
    ]


def role_entry_paths(config: dict, role: str) -> list[str]:
    """Entry paths for one role's walks (depths dropped)."""
    return [path for path, _ in role_entry_specs(config, role)]


def merge_walks(walks: dict[str, dict]) -> dict:
    """Union of one role's per-entry-path walks — for SCREEN coverage only.

    Observations are united by (screen_id, url) — NOT screen_id alone: the
    same screen_id can be observed at different URLs across walks (the login
    wall at /login?next=… digests identically to the plain /login page), and
    a url_contains matcher needs the variant that carries its evidence.
    Transitions are united by (from, to, trigger). Both keep entry-path order
    (first occurrence wins). Transition matchers must NOT run on the merged
    dict: edges stitched from different walks would fabricate paths no single
    walk took (e.g. a login-roundtrip whose next= evidence and downstream
    reachability come from two unrelated walks).
    """
    merged_obs: dict[tuple[str, str], dict] = {}
    merged_trs: dict[tuple[str, str, str], dict] = {}
    for w in walks.values():
        for o in w.get("observations", []):
            merged_obs.setdefault((o["screen_id"], o.get("url", "")), o)
        for t in w.get("transitions", []):
            key = (t["from_screen_id"], t["to_screen_id"], t["trigger"])
            merged_trs.setdefault(key, t)
    return {
        "observations": list(merged_obs.values()),
        "transitions": list(merged_trs.values()),
    }


def evaluate_role(manifest: dict, config: dict, role: str, walks: dict[str, dict] | None) -> dict:
    """Score one role's walks (keyed by entry path, merged for screen
    coverage) against every manifest screen visible to it."""
    merged = merge_walks(walks) if walks else None
    rows = []
    for screen in manifest["screens"]:
        if role not in screen["roles"]:
            continue
        matcher = config["screen_matchers"][screen["id"]]
        evidence = match_screen(matcher, merged) if merged is not None else None
        rows.append(
            {
                "id": screen["id"],
                "route": screen["route"],
                "state": screen["state"],
                "matched": evidence is not None,
                "evidence": evidence,
            }
        )
    matched = sum(1 for r in rows if r["matched"])
    total = len(rows)
    ratio = matched / total if total else 1.0
    threshold = config["roles"][role]["threshold"]
    return {
        "role": role,
        "rows": rows,
        "matched": matched,
        "total": total,
        "ratio": ratio,
        "threshold": threshold,
        "passed": ratio >= threshold,
    }


def evaluate_must_discover(
    manifest: dict, config: dict, walks: dict[str, dict[str, dict]]
) -> list[dict]:
    """Check every mustDiscover entry against the walks of the roles that the
    manifest says can reach it. Found in any one such walk counts. Screens are
    checked against a role's merged walks; transitions against each individual
    walk (a transition path must exist within a single walk's graph)."""
    screens_by_id = {s["id"]: s for s in manifest["screens"]}
    transitions_by_id = {t["id"]: t for t in manifest["transitions"]}
    results = []
    for entry in manifest.get("mustDiscover", []):
        eid, kind = entry["id"], entry["kind"]
        if kind == "screen":
            spec = screens_by_id[eid]
            matcher = config["screen_matchers"][eid]
            fn = match_screen
        else:
            spec = transitions_by_id[eid]
            matcher = config["transition_matchers"][eid]
            fn = match_transition
        evidence = None
        evidence_role = None
        for role in spec["roles"]:
            role_walks = walks.get(role)
            if not role_walks:
                continue
            candidates = [merge_walks(role_walks)] if kind == "screen" else list(role_walks.values())
            for candidate in candidates:
                evidence = fn(matcher, candidate)
                if evidence is not None:
                    evidence_role = role
                    break
            if evidence is not None:
                break
        results.append(
            {
                "id": eid,
                "kind": kind,
                "found": evidence is not None,
                "role": evidence_role,
                "evidence": evidence,
                "why": entry.get("why", ""),
            }
        )
    return results


def compare_determinism(first: dict | None, second: dict | None) -> dict:
    """Two walks of the same role must observe identical screen_id sets."""
    if first is None or second is None:
        return {"passed": False, "only_first": [], "only_second": [], "comparable": False}
    a = {o["screen_id"] for o in first.get("observations", [])}
    b = {o["screen_id"] for o in second.get("observations", [])}
    return {
        "passed": a == b,
        "only_first": sorted(a - b),
        "only_second": sorted(b - a),
        "comparable": True,
    }


def judge(
    manifest: dict,
    config: dict,
    walks: dict[str, dict[str, dict]],
    determinism_rewalk: dict | None,
    crashes: dict[str, str],
) -> dict:
    """Aggregate all gates into one verdict dict. ``walks`` is keyed by role,
    then by entry path."""
    det_role = config.get("determinism_role", "member")
    det_path = role_entry_paths(config, det_role)[0]
    roles = [
        evaluate_role(manifest, config, role, walks.get(role)) for role in config["roles"]
    ]
    must = evaluate_must_discover(manifest, config, walks)
    determinism = compare_determinism(
        (walks.get(det_role) or {}).get(det_path), determinism_rewalk
    )
    passed = (
        all(r["passed"] for r in roles)
        and all(m["found"] for m in must)
        and determinism["passed"]
        and not crashes
    )
    return {
        "roles": roles,
        "must_discover": must,
        "determinism": determinism,
        "determinism_role": det_role,
        "determinism_path": det_path,
        "crashes": crashes,
        "passed": passed,
    }


def render_report(
    verdict: dict,
    timings: dict[str, float] | None = None,
    elapsed: float | None = None,
) -> str:
    """Human-readable coverage report for stdout."""
    lines: list[str] = []
    out = lines.append
    for result in verdict["roles"]:
        out("")
        out(f"=== role: {result['role']} ===")
        width = max((len(r["id"]) for r in result["rows"]), default=10) + 2
        for row in result["rows"]:
            mark = "MATCHED" if row["matched"] else "missed "
            evidence = f"  <- {row['evidence']}" if row["evidence"] else ""
            out(f"  {row['id']:<{width}} {mark}{evidence}")
        gate = "PASS" if result["passed"] else "FAIL"
        out(
            f"  coverage: {result['matched']}/{result['total']} = {result['ratio']:.3f} "
            f"(threshold {result['threshold']}) -> {gate}"
        )
    out("")
    out("=== mustDiscover ===")
    for entry in verdict["must_discover"]:
        if entry["found"]:
            out(f"  {entry['id']} ({entry['kind']}): FOUND in {entry['role']} walk")
            out(f"      {entry['evidence']}")
        else:
            out(f"  {entry['id']} ({entry['kind']}): MISSING")
    out("")
    det = verdict["determinism"]
    out(
        f"=== determinism ({verdict['determinism_role']} walked twice "
        f"from {verdict.get('determinism_path', '/')}) ==="
    )
    if not det.get("comparable", True):
        out("  NOT COMPARABLE (a walk crashed) -> FAIL")
    elif det["passed"]:
        out("  identical screen_id sets -> PASS")
    else:
        out("  screen_id sets differ -> FAIL")
        for sid in det["only_first"]:
            out(f"    only in first walk:  {sid}")
        for sid in det["only_second"]:
            out(f"    only in second walk: {sid}")
    if verdict["crashes"]:
        out("")
        out("=== crashes ===")
        for label, err in verdict["crashes"].items():
            out(f"  {label}: {err}")
    if timings:
        out("")
        out("=== wall-clock ===")
        for label, seconds in timings.items():
            out(f"  {label}: {seconds:.1f}s")
        out(f"  total walk time: {sum(timings.values()):.1f}s")
        if elapsed is not None:
            out(f"  elapsed (wall-clock): {elapsed:.1f}s")
    out("")
    out(f"ACCEPTANCE: {'PASS' if verdict['passed'] else 'FAIL'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration: boot a fresh sandbox per walk (docker image or local command)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _preassign_ports(count: int) -> list[int]:
    """Reserve ``count`` distinct free ports up front — all sockets are held
    open simultaneously, so the kernel cannot hand the same port out twice.
    Parallel walks each get their own preassigned port, which removes the
    pick-then-bind race two concurrent workers would otherwise have."""
    socks: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            socks.append(sock)
        return [sock.getsockname()[1] for sock in socks]
    finally:
        for sock in socks:
            sock.close()


def _wait_healthz(base_url: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"sandbox at {base_url} not healthy after {timeout:.0f}s")


class ImageSandbox:
    """One fresh docker container per walk, removed afterwards."""

    def __init__(self, image: str, container_port: int = 3000) -> None:
        self.image = image
        self.container_port = container_port
        self._name: str | None = None

    def boot(self, label: str, port: int | None = None) -> str:
        port = port if port is not None else _free_port()
        # Walk labels carry "@" and "/" (role@entry_path) — not valid in
        # docker container names.
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-") or "walk"
        self._name = f"sandbox-walk-{safe}-{port}-{os.getpid()}"
        subprocess.run(
            [
                "docker", "run", "--detach",
                "--name", self._name,
                "--publish", f"127.0.0.1:{port}:{self.container_port}",
                self.image,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_healthz(base_url)
        except Exception:
            self.stop()
            raise
        return base_url

    def stop(self) -> None:
        if self._name:
            subprocess.run(
                ["docker", "rm", "--force", self._name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._name = None

    def extract_manifest(self, manifest_path_in_image: str, dest: Path) -> None:
        if not self._name:
            raise RuntimeError("no running container to extract the manifest from")
        subprocess.run(
            ["docker", "cp", f"{self._name}:{manifest_path_in_image}", str(dest)],
            check=True,
            stdout=subprocess.DEVNULL,
        )


class ServerCmdSandbox:
    """One fresh local server process (group) per walk, killed afterwards."""

    def __init__(self, server_cmd: str) -> None:
        self.server_cmd = server_cmd
        self._proc: subprocess.Popen | None = None

    def boot(self, label: str, port: int | None = None) -> str:
        port = port if port is not None else _free_port()
        env = dict(os.environ, PORT=str(port))
        self._proc = subprocess.Popen(
            self.server_cmd.replace("{port}", str(port)),
            shell=True,
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_healthz(base_url)
        except Exception:
            self.stop()
            raise
        return base_url

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                self._proc.wait(timeout=5)
        except ProcessLookupError:
            pass
        self._proc = None


def run_walk(
    sandbox, label: str, role: str, creds: dict, entry_path: str, depth: int,
    port: int | None = None,
) -> dict:
    """Boot a fresh sandbox, run one walk with a fresh browser, tear down."""
    from dataclasses import asdict

    from sidira_crawler.walker import walk

    base_url = sandbox.boot(label, port=port)
    try:
        raw = walk(
            base_url.rstrip("/") + entry_path,
            role,
            username=creds.get("username"),
            password=creds.get("password"),
            depth=depth,
        )
        return asdict(raw)
    finally:
        sandbox.stop()


def execute_walk_task(task: dict) -> dict:
    """Run one planned walk. Never raises: a crash comes back as the
    ``error`` field so the parent fails the gate with the error visible even
    when the walk ran in a worker process. Top-level (picklable) on purpose —
    with ``--jobs N`` this is the function the spawn-based pool executes."""
    started = time.monotonic()
    try:
        sandbox = (
            ImageSandbox(task["image"]) if task["image"] else ServerCmdSandbox(task["server_cmd"])
        )
        walk = run_walk(
            sandbox, task["label"], task["role"], task["creds"],
            task["entry_path"], task["depth"], port=task["port"],
        )
        return {"label": task["label"], "walk": walk, "error": None,
                "seconds": time.monotonic() - started}
    except Exception as exc:
        return {"label": task["label"], "walk": None,
                "error": f"{type(exc).__name__}: {exc}",
                "seconds": time.monotonic() - started}


def _report_progress(result: dict) -> None:
    if result["error"] is not None:
        print(
            f"[sandbox-acceptance] walk {result['label']} CRASHED: {result['error']}",
            file=sys.stderr,
        )
    else:
        walk = result["walk"]
        print(
            f"[sandbox-acceptance] walk {result['label']}: "
            f"{len(walk['observations'])} screens, "
            f"{len(walk['transitions'])} transitions in {result['seconds']:.1f}s",
            file=sys.stderr,
        )


def order_results(tasks: list[dict], by_label: dict[str, dict]) -> list[dict]:
    """Re-order completed walk results into fixed plan order. raw.jsonl line
    order (and everything downstream) must not depend on which parallel walk
    finished first."""
    return [by_label[task["label"]] for task in tasks]


def run_plan(tasks: list[dict], jobs: int) -> list[dict]:
    """Execute every planned walk; return results in PLAN order regardless of
    completion order. ``jobs=1`` runs inline (sequential, no workers); with
    ``jobs>1`` each walk runs in its own spawn-based worker process — the
    walker drives sync Playwright, which must never be shared across threads,
    so parallelism is process-level and every walk keeps its own sandbox,
    browser, and preassigned port."""
    if jobs == 1:
        results = []
        for task in tasks:
            result = execute_walk_task(task)
            _report_progress(result)
            results.append(result)
        return results

    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, as_completed

    by_label: dict[str, dict] = {}
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=jobs, mp_context=context) as pool:
        futures = {pool.submit(execute_walk_task, task): task["label"] for task in tasks}
        for future in as_completed(futures):
            label = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # worker process died (signal, OOM, …)
                result = {"label": label, "walk": None,
                          "error": f"{type(exc).__name__}: {exc}", "seconds": 0.0}
            by_label[result["label"]] = result
            _report_progress(result)
    return order_results(tasks, by_label)


def assemble_results(
    tasks: list[dict], results: list[dict], determinism_label: str
) -> tuple[dict[str, dict[str, dict]], dict | None, dict[str, str], dict[str, float], list]:
    """Fold per-walk results (already in plan order) into the judge's inputs:
    (walks by role by entry path, determinism re-walk, crashes, timings,
    labelled walks for raw.jsonl). Pure — hermetic tests exercise ordering
    and crash propagation without booting anything."""
    walks: dict[str, dict[str, dict]] = {}
    rewalk: dict | None = None
    crashes: dict[str, str] = {}
    timings: dict[str, float] = {}
    labelled: list[tuple[str, str, str, dict]] = []
    for task, result in zip(tasks, results):
        label = task["label"]
        timings[label] = result["seconds"]
        if result["error"] is not None:
            crashes[label] = result["error"]
            continue
        walk = result["walk"]
        labelled.append((label, task["role"], task["entry_path"], walk))
        if label == determinism_label:
            rewalk = walk
        else:
            walks.setdefault(task["role"], {})[task["entry_path"]] = walk
    return walks, rewalk, crashes, timings, labelled


def write_raw_jsonl(
    out_path: Path,
    meta: dict,
    labelled_walks: list[tuple[str, str, str, dict]],
) -> None:
    """raw.jsonl: a meta header line, then one line per observation and per
    transition, each tagged with role, entry path, walk label, and kind."""
    with out_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": "meta", **meta}, ensure_ascii=False) + "\n")
        for label, role, entry_path, walk in labelled_walks:
            for obs in walk.get("observations", []):
                record = {
                    "kind": "observation", "walk": label, "role": role,
                    "entry_path": entry_path, **obs,
                }
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            for tr in walk.get("transitions", []):
                record = {
                    "kind": "transition", "walk": label, "role": role,
                    "entry_path": entry_path, **tr,
                }
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="Sandbox docker image ref (CI mode).")
    source.add_argument(
        "--server-cmd",
        help="Shell command that serves the sandbox (local mode). '{port}' is "
        "substituted and PORT is set in the environment.",
    )
    parser.add_argument("--config", required=True, help="Judge config JSON.")
    parser.add_argument(
        "--manifest",
        help="Ground-truth manifest path (required with --server-cmd; overrides "
        "image extraction).",
    )
    parser.add_argument(
        "--manifest-path-in-image",
        default="/app/ground-truth/manifest.json",
        help="Where the manifest lives inside the image (docker cp source).",
    )
    parser.add_argument("--run-date", required=True, help="Run date (e.g. 2026-06-11).")
    parser.add_argument("--out", default="raw.jsonl", help="raw.jsonl output path.")
    parser.add_argument(
        "--depth", type=int, default=None,
        help="Override walk depth for EVERY walk (including per-entry overrides).",
    )
    parser.add_argument(
        "--jobs", type=int, default=1,
        help="Run up to N walks in parallel, each in its own worker process "
        "with its own sandbox and browser. Default 1 = sequential.",
    )
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    default_depth = args.depth if args.depth is not None else config.get("depth", 6)
    det_role = config.get("determinism_role", "member")

    if args.server_cmd and not args.manifest:
        parser.error("--server-cmd requires --manifest")

    def make_sandbox():
        if args.image:
            return ImageSandbox(args.image)
        return ServerCmdSandbox(args.server_cmd)

    # Manifest: from disk, or extracted out of a booted container (image mode).
    if args.manifest:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    else:
        sandbox = make_sandbox()
        sandbox.boot("manifest")
        try:
            dest = Path(args.out).parent / "manifest.extracted.json"
            sandbox.extract_manifest(args.manifest_path_in_image, dest)
            manifest = json.loads(dest.read_text(encoding="utf-8"))
        finally:
            sandbox.stop()

    validate_config(manifest, config)

    from sidira_crawler import __version__ as walker_version

    # Plan: one walk per (role, entry path), plus a second determinism walk of
    # the determinism role's primary entry path. Fresh sandbox, fresh browser,
    # and a preassigned (distinct) port per walk. The plan order is fixed;
    # parallel completion order never leaks into the output.
    det_path, det_depth = role_entry_specs(config, det_role)[0]
    plan: list[tuple[str, str, str, int]] = []
    for role in config["roles"]:
        for path, entry_depth in role_entry_specs(config, role):
            plan.append((f"{role}@{path}", role, path, entry_depth))
    determinism_label = f"{det_role}-determinism@{det_path}"
    plan.append((determinism_label, det_role, det_path, det_depth))

    ports = _preassign_ports(len(plan))
    tasks = [
        {
            "label": label,
            "role": role,
            "entry_path": path,
            "depth": args.depth if args.depth is not None else entry_depth,
            "port": port,
            "creds": config["roles"][role],
            "image": args.image or None,
            "server_cmd": args.server_cmd or None,
        }
        for (label, role, path, entry_depth), port in zip(plan, ports)
    ]

    run_started = time.monotonic()
    results = run_plan(tasks, args.jobs)
    elapsed = time.monotonic() - run_started
    walks, rewalk, crashes, timings, labelled = assemble_results(
        tasks, results, determinism_label
    )

    meta = {
        "schema": 2,
        "image": args.image or None,
        "server_cmd": args.server_cmd or None,
        "run_date": args.run_date,
        "walker_version": walker_version,
        "depth": default_depth,
        "walk_depths": {task["label"]: task["depth"] for task in tasks},
        "jobs": args.jobs,
        "judge_config": str(args.config),
        "roles": list(config["roles"]),
        "entry_paths": {role: role_entry_paths(config, role) for role in config["roles"]},
        "determinism_role": det_role,
        "determinism_entry_path": det_path,
    }
    write_raw_jsonl(Path(args.out), meta, labelled)

    verdict = judge(manifest, config, walks, rewalk, crashes)
    print(render_report(verdict, timings, elapsed))
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
