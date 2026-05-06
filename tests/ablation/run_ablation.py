#!/usr/bin/env python3
"""
Per-stage ablation harness for the federated ZTA control plane.

Goal
----
Quantify the marginal contribution of each pipeline stage to overall defense
success. For each scenario in the attack corpus, run the request through the
sidecar in N+1 different configurations:

    * BASELINE     — every stage enabled (current production-equivalent)
    * NO_WAF       — WAF disabled
    * NO_MICROSEG  — micro-segmentation disabled
    * NO_TRUST     — trust scorer disabled
    * NO_BEHAVIOR  — behavior PDP disabled
    * NO_REVOC     — revocation dispatcher disabled
    * NO_OPA       — authorization PDP disabled

JWT auth and DLP are intentionally NOT ablated:
    * JWT auth removal would invalidate the entire identity model and is
      structurally different from an "ablation" — every other stage assumes
      a parsed identity. Disabling it is closer to "remove the system" than
      "remove a stage."
    * DLP is response-side and does not contribute to deny decisions on the
      request path — ablating it would not change attack-blocking outcomes.

For each (scenario, configuration) we record:
    - HTTP status code
    - which stage was responsible for the deny (resolved from response
      headers + body — see resolve_denying_stage())
    - request latency in milliseconds

Outputs
-------
1. A CSV with one row per (scenario, configuration) — used for the appendix.
2. A pivot table (CSV + Markdown) with one row per scenario and one column
   per configuration, cells showing the denying stage. This is the primary
   table for the ablation section of the paper.
3. A summary block: per-configuration deny rate, per-stage attribution
   counts, and the marginal contribution of each stage (defined as the
   number of scenarios that change from "denied" to "allowed" when that
   stage is removed).

Usage
-----
From the project root, with the testbed up and healthy:

    python tests/ablation/run_ablation.py

To restrict the corpus (e.g., a smoke run during development):

    python tests/ablation/run_ablation.py --max-scenarios 5

To skip docker reconfig (e.g., when iterating on the resolver only):

    python tests/ablation/run_ablation.py --no-docker

Reproducibility
---------------
The harness writes all of its outputs to a single directory tagged with a
UTC timestamp, so the CSV in the appendix is unambiguously linked to the
configuration that produced it. Use --output-dir to override.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx


# =============================================================================
# Configuration — paths, ports, fixed agent identity for the harness
# =============================================================================

REPO_ROOT      = Path(__file__).resolve().parents[2]
COMPOSE_FILE   = REPO_ROOT / "docker-compose.zta.yml"
PROMPTS_DIR    = REPO_ROOT / "test-driver" / "prompts"

AIRLINE_SIDECAR = "http://localhost:19091"
AUTH_URL        = "http://localhost:8180"
DISPATCHER_URL  = "http://localhost:8196"
TRUST_SCORER_URL= "http://localhost:8190"
AUDIT_URL       = "http://localhost:8195"

AGENT_ID            = "supervisor-agent"
AGENT_SHARED_SECRET = "zta-agent-shared-secret"

# Sidecars whose env we mutate when a stage is ablated. Picking just the
# airline sidecar keeps the run fast — we route every scenario through it.
TARGET_SIDECAR = "airline-agent-sidecar"


# =============================================================================
# Stage attribution — read response and decide which stage denied the request
# =============================================================================

# The sidecar advertises stage-specific headers and body shapes. We resolve
# in priority order: the FIRST middleware to deny in the pipeline is the one
# we want to credit. The order below mirrors the actual pipeline order in
# Program.cs so that, e.g., a WAF block is reported as "WAF" even if a later
# stage would also have blocked it.

STAGE_ORDER = [
    "WAF",
    "JWT",
    "REVOCATION",
    "MICROSEG",
    "TRUST_SCORER",
    "BEHAVIOR_PDP",
    "OPA",
    "UPSTREAM",
]


def resolve_denying_stage(status: int, headers: dict, body_text: str) -> str:
    """Return which pipeline stage produced the response.

    "ALLOWED" means the request reached upstream (status < 400 and a
    successful body). Any deny-status response gets attributed to whichever
    stage's signature appears in headers/body.
    """
    if status < 400:
        return "ALLOWED"

    # Stage-specific headers added by the sidecar middleware
    if headers.get("X-ZTA-WAF") == "blocked":
        return "WAF"
    if headers.get("X-ZTA-Revoked") == "agent" or headers.get("X-ZTA-Revoked") == "jti":
        return "REVOCATION"
    if headers.get("X-ZTA-Behavior-Band") == "block":
        return "BEHAVIOR_PDP"
    if headers.get("X-ZTA-Policy") == "denied":
        return "OPA"

    # Body-text fallbacks (the sidecar returns structured JSON on deny)
    bt = body_text.lower() if body_text else ""
    if "trust threshold not met" in bt or "trust_band" in bt and "block" in bt:
        return "TRUST_SCORER"
    if "agent not allowed" in bt or "micro-seg" in bt or "not in registry" in bt:
        return "MICROSEG"
    if "agent is quarantined" in bt:
        return "REVOCATION"
    if "behavior pdp blocked" in bt:
        return "BEHAVIOR_PDP"
    if "policy engine denied" in bt:
        return "OPA"
    if "policy engine unreachable" in bt:
        return "OPA"

    # Status-based fallbacks
    if status == 401:
        return "JWT"
    if status == 503:
        return "OPA"  # most likely OPA fail-closed, but flagged for review

    return f"UNKNOWN({status})"


# =============================================================================
# Ablation configurations
# =============================================================================
#
# Each configuration is a dict of env-var overrides applied to the target
# sidecar. The empty dict is the baseline (all defaults, every stage on).
# We use "false" string values because the sidecar reads env vars literally.

@dataclass
class Configuration:
    name: str
    description: str
    env: dict[str, str] = field(default_factory=dict)


CONFIGURATIONS: list[Configuration] = [
    Configuration(
        name="BASELINE",
        description="All stages enabled (production-equivalent)",
        env={},
    ),
    Configuration(
        name="NO_WAF",
        description="WAF middleware disabled",
        env={"ENABLE_WAF": "false"},
    ),
    Configuration(
        name="NO_REVOC",
        description="Revocation dispatcher check disabled",
        env={"ENABLE_REVOCATION": "false"},
    ),
    Configuration(
        name="NO_MICROSEG",
        description="Micro-segmentation (ALLOWED_SOURCES) check disabled",
        env={"ENABLE_MICROSEG": "false"},
    ),
    Configuration(
        name="NO_TRUST",
        description="Trust scorer middleware disabled",
        env={"ENABLE_TRUST_SCORER": "false"},
    ),
    Configuration(
        name="NO_BEHAVIOR",
        description="Behavior PDP middleware disabled",
        env={"ENABLE_BEHAVIOR_PDP": "false"},
    ),
    Configuration(
        name="NO_OPA",
        description="Authorization PDP (OPA) middleware disabled",
        env={"ENABLE_OPA": "false"},
    ),
]


# =============================================================================
# Scenario corpus — combine transit_trust (control-plane attacks) with a
# curated slice of prompt_injection (LLM-layer attacks). 49 total to match
# the figure cited in the paper.
# =============================================================================

@dataclass
class Scenario:
    sid: str
    description: str
    payload: dict
    headers: dict
    expected_deny: bool      # True if we expect baseline to block
    target: str = AIRLINE_SIDECAR


def load_corpus(max_scenarios: Optional[int] = None) -> list[Scenario]:
    """Build the unified scenario list from the existing prompt JSON files."""
    scenarios: list[Scenario] = []

    # 1. transit_trust: control-plane probing (missing headers, evil agent,
    #    cross-domain access, SQL in headers, etc.)
    tt_path = PROMPTS_DIR / "transit_trust.json"
    if tt_path.exists():
        for item in json.loads(tt_path.read_text()):
            scenarios.append(Scenario(
                sid=item["id"],
                description=item.get("description", ""),
                payload=item.get("payload", {"message": ""}),
                headers=item.get("headers", {}),
                expected_deny=int(item.get("expected_status", 200)) >= 400,
            ))

    # 2. prompt_injection: LLM-layer attacks. These are wrapped in an A2A
    #    JSON-RPC envelope so they hit the sidecar's full pipeline.
    pi_path = PROMPTS_DIR / "prompt_injection.json"
    if pi_path.exists():
        for item in json.loads(pi_path.read_text()):
            scenarios.append(Scenario(
                sid=item["id"],
                description=item.get("description", ""),
                payload={
                    "jsonrpc": "2.0",
                    "id": item["id"],
                    "method": "tasks/send",
                    "params": {
                        "id": f"task-{item['id']}",
                        "message": {
                            "role": "user",
                            "parts": [{"type": "text", "text": item["text"]}],
                        },
                    },
                },
                # We use the supervisor identity for these — the goal is to
                # measure whether the *content* triggers a downstream control,
                # not to mix in identity failures (covered by transit_trust).
                headers={"x-agent-id": AGENT_ID},
                expected_deny=item.get("expected_behavior", "refuse") == "refuse",
            ))

    if max_scenarios is not None:
        scenarios = scenarios[:max_scenarios]
    return scenarios


# =============================================================================
# Sidecar reconfiguration via docker compose
# =============================================================================

def apply_sidecar_env(env_overrides: dict[str, str], skip_docker: bool) -> None:
    """Recreate the target sidecar with env_overrides merged on top of its
    docker-compose env. We do this by writing a temporary override file and
    issuing `docker compose -f base.yml -f override.yml up -d <svc>`.

    The recreate is the slow step (~5–10s per configuration), but unavoidable
    because the sidecar reads env vars at startup.
    """
    if skip_docker:
        return

    # Always start from a clean override — otherwise stale env from a
    # previous configuration would leak through.
    override_path = REPO_ROOT / "docker-compose.ablation.override.yml"

    if env_overrides:
        env_lines = "\n".join(f"      - {k}={v}" for k, v in env_overrides.items())
        override_yml = (
            "services:\n"
            f"  {TARGET_SIDECAR}:\n"
            "    environment:\n"
            f"{env_lines}\n"
        )
    else:
        # Empty override file — compose merges this against the base file
        # and produces the original env unchanged. We still write the file
        # so the compose command line is uniform across configurations.
        override_yml = (
            "services:\n"
            f"  {TARGET_SIDECAR}: {{}}\n"
        )
    override_path.write_text(override_yml)

    cmd = [
        "docker", "compose",
        "-f", str(COMPOSE_FILE),
        "-f", str(override_path),
        "up", "-d",
        "--no-deps",          # don't recreate dependencies
        "--force-recreate",   # ensure env vars are re-read
        TARGET_SIDECAR,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"compose up failed:\n{res.stderr}")


def wait_sidecar_healthy(timeout_s: int = 30) -> None:
    """Poll the sidecar's diagnostic endpoint until it returns 200.

    Note the path: the sidecar exposes `/sidecar/health`, not `/sidecar`.
    `/sidecar` appears in the middleware skip list (so it bypasses the
    pipeline) but isn't routed anywhere — hitting it returns whatever
    YARP does on an unmatched route. Polling that gives a false negative.
    """
    deadline = time.time() + timeout_s
    last_err: Optional[str] = None
    while time.time() < deadline:
        try:
            r = httpx.get(f"{AIRLINE_SIDECAR}/sidecar/health", timeout=1.0)
            if r.status_code == 200:
                return
            last_err = f"status={r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(0.5)
    raise RuntimeError(
        f"sidecar did not become healthy within {timeout_s}s "
        f"(last response: {last_err})"
    )


def reset_state() -> None:
    """Clear stateful contamination between ablation configurations.

    Without this, results from one configuration leak into the next:

      1. The trust scorer's behavioral history is in-memory and accumulates
         injection events across the full session. By the second config,
         the test agent has dozens of recorded injections — every score
         starts from a depressed `b_B`, and the SL aggregator gives biased
         results.

      2. The behavior PDP's R2 rule fires when ≥3 injection events appear
         in the audit window for an agent. Because AUTO_QUARANTINE is on,
         crossing R2 calls the dispatcher to quarantine the agent for 15
         minutes. Once quarantined, every subsequent request — across all
         remaining configurations — is rejected at the revocation stage,
         attributing denials to REVOCATION that have nothing to do with
         the stage being ablated.

    Resetting between configurations restores within-config measurement
    integrity. We accept that within a single configuration the cascade
    may still trigger (which is the system working as designed); what we
    don't want is contamination between configurations.

    Both operations are best-effort. A failed reset logs a warning but
    does not abort the run — partial data is still useful.
    """
    # 1. Unquarantine the test agent at the dispatcher.
    try:
        r = httpx.post(
            f"{DISPATCHER_URL}/unquarantine/agent/{AGENT_ID}",
            params={"reason": "ablation-reset"},
            timeout=2.0,
        )
        if r.status_code == 200:
            print(f"  [reset] dispatcher: unquarantined {AGENT_ID}")
        elif r.status_code == 404:
            pass  # not quarantined; that's fine
        else:
            print(f"  [reset] dispatcher: unexpected status {r.status_code}")
    except Exception as e:
        print(f"  [reset] dispatcher: {e}")

    # 2. Clear the trust scorer's behavioral history for the test agent.
    try:
        r = httpx.delete(
            f"{TRUST_SCORER_URL}/admin/history",
            params={"agent_id": AGENT_ID},
            timeout=2.0,
        )
        if r.status_code == 200:
            n = r.json().get("deleted", 0)
            print(f"  [reset] trust-scorer: cleared {n} history events for {AGENT_ID}")
        else:
            print(f"  [reset] trust-scorer: unexpected status {r.status_code} "
                  f"(this means the patched scorer with /admin/history isn't deployed; "
                  f"results may still be contaminated)")
    except Exception as e:
        print(f"  [reset] trust-scorer: {e}")

    # 3. Clear injection-related events from the audit log for the test
    # agent. The behavior PDP queries audit on every /decide, so leftover
    # trust.injection events from a previous configuration would cause R2
    # to fire on the FIRST request of the new configuration — biasing
    # tt-003 ("valid request") to be denied at BEHAVIOR_PDP. We only delete
    # the deny-relevant event types; allowed-request events are kept for
    # the appendix CSV.
    for et in ("trust.injection", "behavior.deny", "behavior.step_up",
               "request.denied", "auth.revoked_use", "policy.deny",
               "waf.block", "microseg.deny"):
        try:
            r = httpx.delete(
                f"{AUDIT_URL}/admin/events",
                params={"agent_id": AGENT_ID, "event_type": et},
                timeout=2.0,
            )
            if r.status_code == 200:
                n = r.json().get("deleted", 0)
                if n > 0:
                    print(f"  [reset] audit: cleared {n} {et} events for {AGENT_ID}")
            else:
                print(f"  [reset] audit ({et}): unexpected status {r.status_code} "
                      f"(patched audit-logger with /admin/events not deployed; "
                      f"behavior PDP may misfire on later configurations)")
                break  # don't spam errors for every event type
        except Exception as e:
            print(f"  [reset] audit ({et}): {e}")
            break


# =============================================================================
# Token acquisition (real JWT for the supervisor identity)
# =============================================================================

_token_cache: Optional[str] = None
_token_acquired_at: float = 0.0

def get_token() -> str:
    """Acquire a JWT for AGENT_ID, cached for ~10 minutes to avoid hammering
    the auth service across 49 × 7 = 343 calls."""
    global _token_cache, _token_acquired_at
    if _token_cache is not None and (time.time() - _token_acquired_at) < 600:
        return _token_cache
    r = httpx.post(f"{AUTH_URL}/token",
                   json={"agent_id": AGENT_ID, "secret": AGENT_SHARED_SECRET},
                   timeout=5.0)
    r.raise_for_status()
    _token_cache = r.json()["access_token"]
    _token_acquired_at = time.time()
    return _token_cache


# =============================================================================
# Run a single scenario against the current sidecar configuration
# =============================================================================

@dataclass
class TrialResult:
    scenario_id: str
    configuration: str
    status: int
    stage: str
    latency_ms: float
    expected_deny: bool
    correct: bool          # True if (deny outcome) matches expected_deny


def run_scenario(scenario: Scenario, configuration: str) -> TrialResult:
    """Execute one scenario, return the resolved trial outcome."""
    headers = {
        "Content-Type": "application/json",
        # Always attach a real Bearer token so the trust scorer's identity
        # check can pass — we want to measure CONTENT-driven blocks, not
        # have everything fall over at JWT.
        "Authorization": f"Bearer {get_token()}",
    }
    headers.update(scenario.headers)

    t0 = time.perf_counter()
    try:
        r = httpx.post(
            f"{scenario.target}/a2a",
            json=scenario.payload,
            headers=headers,
            timeout=20.0,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        stage = resolve_denying_stage(r.status_code, dict(r.headers), r.text)
        status = r.status_code
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        stage = f"ERROR({type(e).__name__})"
        status = 0

    actually_denied = status >= 400 or status == 0
    correct = (actually_denied == scenario.expected_deny)

    return TrialResult(
        scenario_id=scenario.sid,
        configuration=configuration,
        status=status,
        stage=stage,
        latency_ms=elapsed_ms,
        expected_deny=scenario.expected_deny,
        correct=correct,
    )


# =============================================================================
# Reporting — build the pivot table + summary
# =============================================================================

def write_long_csv(results: list[TrialResult], out_path: Path) -> None:
    """One row per (scenario, configuration). Used for the appendix."""
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "scenario_id", "configuration", "status", "stage",
            "latency_ms", "expected_deny", "correct",
        ])
        w.writeheader()
        for r in results:
            d = asdict(r)
            d["latency_ms"] = round(d["latency_ms"], 2)
            w.writerow(d)


def write_pivot(results: list[TrialResult],
                scenarios: list[Scenario],
                configurations: list[Configuration],
                csv_path: Path,
                md_path: Path) -> None:
    """Pivot table: one row per scenario, one column per configuration,
    cell = denying stage. The descriptive 'BASELINE' column shows what
    caught it under production-equivalent settings; subsequent columns show
    what catches it once a single stage is removed."""
    by_sid_cfg: dict[tuple[str, str], TrialResult] = {
        (r.scenario_id, r.configuration): r for r in results
    }

    cfg_names = [c.name for c in configurations]
    headers = ["scenario_id", "expected", *cfg_names]

    # CSV
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for s in scenarios:
            row = [s.sid, "DENY" if s.expected_deny else "ALLOW"]
            for cn in cfg_names:
                tr = by_sid_cfg.get((s.sid, cn))
                row.append(tr.stage if tr else "MISSING")
            w.writerow(row)

    # Markdown
    md_lines = []
    md_lines.append("| " + " | ".join(headers) + " |")
    md_lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for s in scenarios:
        row = [s.sid, "DENY" if s.expected_deny else "ALLOW"]
        for cn in cfg_names:
            tr = by_sid_cfg.get((s.sid, cn))
            row.append(tr.stage if tr else "MISSING")
        md_lines.append("| " + " | ".join(row) + " |")
    md_path.write_text("\n".join(md_lines) + "\n")


def write_summary(results: list[TrialResult],
                  scenarios: list[Scenario],
                  configurations: list[Configuration],
                  out_path: Path) -> str:
    """Compute and write the headline metrics for the paper."""
    by_sid_cfg = {(r.scenario_id, r.configuration): r for r in results}

    expected_deny_sids = [s.sid for s in scenarios if s.expected_deny]
    n_expected = len(expected_deny_sids)

    lines = []
    lines.append(f"Ablation summary — {len(scenarios)} scenarios, {len(configurations)} configurations")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"Scenarios where the BASELINE configuration *should* deny (expected_deny=True): {n_expected}")
    lines.append("")

    # Per-config deny rate (limited to the expected-deny subset, which is
    # what the paper's headline number measures).
    lines.append("Per-configuration deny rate on the expected-deny subset:")
    lines.append(f"  {'configuration':<14} {'denies':>7} {'rate':>8}")
    for c in configurations:
        denies = sum(
            1 for sid in expected_deny_sids
            if (tr := by_sid_cfg.get((sid, c.name))) and tr.stage != "ALLOWED"
        )
        rate = denies / n_expected if n_expected else 0.0
        lines.append(f"  {c.name:<14} {denies:>7} {rate:>7.1%}")
    lines.append("")

    # Marginal contribution: how many scenarios change BASELINE=DENY ->
    # NO_X=ALLOWED. This is what reviewers want to see for the per-stage
    # contribution claim.
    lines.append("Marginal contribution per stage (scenarios denied at baseline but")
    lines.append("allowed when this stage is removed):")
    baseline_denied = {
        sid for sid in expected_deny_sids
        if (tr := by_sid_cfg.get((sid, "BASELINE"))) and tr.stage != "ALLOWED"
    }
    for c in configurations:
        if c.name == "BASELINE":
            continue
        leaked = [
            sid for sid in baseline_denied
            if (tr := by_sid_cfg.get((sid, c.name))) and tr.stage == "ALLOWED"
        ]
        lines.append(f"  {c.name:<14} {len(leaked):>3} scenarios   "
                     f"{', '.join(leaked[:5])}{'...' if len(leaked) > 5 else ''}")
    lines.append("")

    # Stage attribution at baseline — answers "which stage is doing the work?"
    lines.append("Stage attribution at BASELINE (which stage caught each denied scenario):")
    by_stage: dict[str, int] = {}
    for sid in expected_deny_sids:
        tr = by_sid_cfg.get((sid, "BASELINE"))
        if tr and tr.stage != "ALLOWED":
            by_stage[tr.stage] = by_stage.get(tr.stage, 0) + 1
    for stage in STAGE_ORDER:
        if stage in by_stage:
            lines.append(f"  {stage:<14} {by_stage[stage]:>3}")
    leftover = sum(v for k, v in by_stage.items() if k not in STAGE_ORDER)
    if leftover:
        lines.append(f"  OTHER          {leftover:>3}")

    body = "\n".join(lines)
    out_path.write_text(body + "\n")
    return body


# =============================================================================
# Main
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-scenarios", type=int, default=None,
                        help="cap corpus size (smoke runs)")
    parser.add_argument("--no-docker", action="store_true",
                        help="skip docker reconfig (BASELINE only)")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="override results directory")
    parser.add_argument("--allow-cascade", action="store_true",
                        help="proceed even if AUTO_QUARANTINE=true on the behavior "
                             "PDP. Without this flag, the run aborts because "
                             "auto-quarantine contaminates per-stage attribution.")
    args = parser.parse_args()

    # Sanity check: refuse to run if AUTO_QUARANTINE is on, unless the user
    # explicitly opts in. The previous version just printed a warning, but
    # several runs proceeded with contaminated data because the warning was
    # easy to miss in scrolling output. Aborting forces an explicit decision.
    try:
        r = httpx.get("http://localhost:8197/health", timeout=2.0)
        if r.status_code == 200 and r.json().get("auto_quarantine") is True:
            if not args.allow_cascade:
                print(
                    "\n"
                    "  ABORT: behavior PDP has AUTO_QUARANTINE=true.\n"
                    "  Auto-quarantine contaminates per-stage attribution by\n"
                    "  cascading denials to REVOCATION mid-run.\n"
                    "\n"
                    "  Fix this in one of two ways:\n"
                    "\n"
                    "  (a) For the clean per-stage measurement reviewers want:\n"
                    "      1. Edit docker-compose.zta.yml — under pdp-behavior,\n"
                    "         change AUTO_QUARANTINE=true to AUTO_QUARANTINE=false\n"
                    "      2. docker compose -f docker-compose.zta.yml up -d \\\n"
                    "             --force-recreate pdp-behavior\n"
                    "      3. Re-run this script\n"
                    "      4. Restore AUTO_QUARANTINE=true when done\n"
                    "\n"
                    "  (b) To measure the system as it actually behaves in\n"
                    "      production (cascade present), pass --allow-cascade.\n"
                    "      Useful for the 'federation works' demonstration but\n"
                    "      NOT for per-stage attribution.\n"
                )
                return 2
    except Exception:
        pass

    scenarios = load_corpus(max_scenarios=args.max_scenarios)
    if not scenarios:
        print("ERROR: no scenarios loaded — check that prompt JSON files exist.")
        return 1

    out_dir = args.output_dir or (
        REPO_ROOT / "tests" / "ablation" / "results" /
        datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Results -> {out_dir}")

    # If we're not reconfiguring docker, only run BASELINE.
    configs = CONFIGURATIONS if not args.no_docker else [CONFIGURATIONS[0]]

    all_results: list[TrialResult] = []
    for cfg in configs:
        print(f"\n=== Configuration: {cfg.name} — {cfg.description} ===")
        print(f"    env overrides: {cfg.env or '(none)'}")

        # Reset stateful contamination from any previous run/config.
        # See reset_state() docstring for rationale.
        reset_state()

        try:
            apply_sidecar_env(cfg.env, skip_docker=args.no_docker)
            wait_sidecar_healthy(timeout_s=30)
        except Exception as e:
            print(f"  configuration setup failed: {e}")
            return 1

        for s in scenarios:
            tr = run_scenario(s, cfg.name)
            all_results.append(tr)
            mark = "✓" if tr.correct else "✗"
            print(f"  {mark} {s.sid:<8} status={tr.status:<3} "
                  f"stage={tr.stage:<14} {tr.latency_ms:>6.1f}ms "
                  f"({s.description[:48]})")

    # Always emit all three artifacts
    write_long_csv(all_results, out_dir / "raw_trials.csv")
    write_pivot(all_results, scenarios, configs,
                csv_path=out_dir / "pivot.csv",
                md_path=out_dir / "pivot.md")
    summary = write_summary(all_results, scenarios, configs,
                            out_path=out_dir / "summary.txt")

    print("\n" + summary)
    print(f"\nArtifacts written to {out_dir}/")
    print("  raw_trials.csv  — one row per (scenario, configuration)")
    print("  pivot.csv       — table format for the paper appendix")
    print("  pivot.md        — same as pivot.csv but markdown")
    print("  summary.txt     — headline metrics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
