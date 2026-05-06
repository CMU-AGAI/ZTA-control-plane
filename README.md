# Zero Trust Control Plane for Multi-Agent LLM Systems

A federated Zero Trust Architecture (ZTA) control plane for multi-agent LLM
systems, instantiated as a multi-agent travel-planning testbed (airline,
hotel, car-rental). This repository is the code companion to the paper.

The control plane enforces a **9-stage policy pipeline** at every
agent-to-agent and agent-to-tool hop, scores trust continuously using a
Subjective Logic aggregator, and propagates revocation decisions across
all policy enforcement points. The architecture follows NIST SP 800-207
ZTA principles: every request is authenticated, authorized, and evaluated
against current trust state, with no implicit trust between components.

## Table of contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Components](#components)
- [Running the tests](#running-the-tests)
- [The demo UI](#the-demo-ui)
- [Reproducing the paper results](#reproducing-the-paper-results)
- [Repository layout](#repository-layout)
- [Troubleshooting](#troubleshooting)
- [Citation](#citation)
- [References](#references)

## Architecture

```
                        ┌──────────────────────────────────────────┐
   user request         │  Supervisor agent (LangGraph StateGraph) │
        │               └────────────────────┬─────────────────────┘
        │                                    │  A2A protocol
        │                                    ▼
        │                            ┌───────────────┐
        │                            │ ZTA sidecar   │  ◀─── 9-stage pipeline
        │                            │  (.NET YARP)  │       (per request)
        │                            └───────┬───────┘
        │                                    │
        │                            ┌───────▼───────┐
        │                            │ Domain agent  │  (airline / hotel / car-rental)
        │                            │  (LangChain)  │
        │                            └───────┬───────┘
        │                                    │  MCP (SSE)
        │                            ┌───────▼───────┐
        │                            │ ZTA sidecar   │  ◀─── 9-stage pipeline
        │                            └───────┬───────┘
        │                                    │
        │                            ┌───────▼───────┐
        │                            │ MCP server    │
        │                            │  (FastMCP)    │
        │                            └───────┬───────┘
        │                                    │
        │                            ┌───────▼───────┐
        │                            │ Backend       │  (FastAPI + SQLite)
        │                            └───────────────┘

  Federated control plane (queried by every sidecar):

     ┌─────────────┐    ┌──────────────┐    ┌─────────────────┐
     │ zta-auth    │    │ trust-scorer │    │ pdp-behavior    │
     │ (JWT)       │    │ (SL)         │    │ (anomaly)       │
     └─────────────┘    └──────┬───────┘    └────────┬────────┘
                               │                     │
                        ┌──────▼─────────────────────▼──────┐
                        │ audit-logger (SQLite, 30d retain) │
                        └──────────────────┬────────────────┘
                                           │
                              ┌────────────▼──────────┐
                              │ revocation-dispatcher │  ◀── fan-out to all PEPs
                              └───────────────────────┘
                              ┌───────────────────────┐
                              │ OPA (Rego authz PDP)  │
                              └───────────────────────┘
```

The 9 enforcement stages run on the request path inside each sidecar, in
order: Monitor → WAF → JWT → Revocation → MicroSegmentation →
TrustScorer → BehaviorPDP → OPA → DLP. A deny at any stage short-circuits
the pipeline; the denying stage is recorded in `X-ZTA-*` response
headers for audit and ablation analysis.

## Prerequisites

- Docker 24+ and Docker Compose v2
- Python 3.11+ (only for running tests and the ablation harness)
- At least one LLM API key — Groq, Anthropic, or OpenAI. Groq is
  preferred because the agents fall back to it first; the default model
  is `meta-llama/llama-4-scout-17b-16e-instruct`. Get a free key at
  <https://console.groq.com>.
- ~4 GB RAM available for the 21 containers
- Free TCP ports: `8001-8003`, `8010-8012`, `8080`, `8091-8093`,
  `8180-8181`, `8190`, `8195-8197`, `19010-19012`, `19091-19093`

You do not need .NET installed — the sidecar builds inside its container.

## Quick start

```bash
# 1. Set up environment
cp .env.example .env
# Edit .env and add at least one of GROQ_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY.

# 2. Build and start the full stack (21 containers, ~3-5 min first time)
docker compose -f docker-compose.zta.yml up --build -d

# 3. Wait for healthchecks
docker compose -f docker-compose.zta.yml ps
# All services should show "healthy" or "running" before continuing.

# 4. Smoke-test the supervisor
curl http://localhost:8080/health

# 5. Run the federated integration test (proves the closed loop works)
pip install -e .
python tests/test_federated_loop.py
# Expect: all phases pass, exit code 0.

# 6. Open the demo UI
open zta_demo_ui.html        # macOS
xdg-open zta_demo_ui.html    # Linux
# If preflight requests fail, run ./fix_cors_v2.sh once and reload.

# 7. Tear down when finished
docker compose -f docker-compose.zta.yml down -v
```

## Components

### Backend services (port 8001-8003)

FastAPI services backed by SQLite. They model the booking domain: flights,
hotels, and car rentals. SQLite databases are created on first run inside
each container.

| Service       | Port | File                          |
|---------------|------|-------------------------------|
| airline       | 8001 | `services/airline/app.py`     |
| hotel         | 8002 | `services/hotel/app.py`       |
| car-rental    | 8003 | `services/car-rental/app.py`  |

### MCP servers (port 8010-8012)

FastMCP servers using SSE transport. They expose tools to their domain
agent. Each MCP server is fronted by its own ZTA sidecar so the
agent-to-tool boundary is also subject to ZTA enforcement.

| Server         | Port | Backend         |
|----------------|------|-----------------|
| airline-mcp    | 8010 | airline:8001    |
| hotel-mcp      | 8011 | hotel:8002      |
| car-rental-mcp | 8012 | car-rental:8003 |

### Agents (port 8080, 8091-8093)

LangChain ReAct agents communicating via the A2A protocol. The
supervisor uses a LangGraph StateGraph to orchestrate the three domain
agents.

| Agent             | Port | Role                                          |
|-------------------|------|-----------------------------------------------|
| supervisor        | 8080 | User-facing entry point; routes to workers   |
| airline-agent     | 8091 | Calls airline-mcp                             |
| hotel-agent       | 8092 | Calls hotel-mcp                               |
| car-rental-agent  | 8093 | Calls car-rental-mcp                          |

The A2A library (`agents/a2a/`) implements workload identity propagation:
each request carries a JWT identifying the calling agent, which the
sidecar validates and forwards to the trust scorer and OPA.

### ZTA sidecars (port 19010-19012, 19091-19093)

A single `.NET 8` YARP-based reverse proxy
(`zta-sidecar/Program.cs`), deployed as one container per protected
upstream (3 agents + 3 MCP servers = 6 sidecars). Each sidecar runs the
9-stage pipeline against every inbound request before forwarding to its
upstream. Behavior is configured per-instance via environment variables
in `docker-compose.zta.yml`.

| Stage              | Environment toggle           | Purpose                                                   |
|--------------------|------------------------------|-----------------------------------------------------------|
| Monitor            | (always on)                  | Per-request structured logging + tracing                  |
| WAF                | (always on)                  | Inline regex match against known prompt-injection strings |
| JWT                | (always on)                  | Verifies workload identity signed by `zta-auth`           |
| Revocation         | `ENABLE_REVOCATION`          | Checks `revocation-dispatcher` for active denials         |
| MicroSegmentation  | `ENABLE_MICROSEG`            | Enforces `ALLOWED_SOURCES` allowlist                      |
| TrustScorer        | `ENABLE_TRUST_SCORER`        | Calls `trust-scorer` and applies its band decision        |
| BehaviorPDP        | `ENABLE_BEHAVIOR_PDP`        | Calls `pdp-behavior` for anomaly-band decision            |
| OPA                | `ENABLE_OPA`                 | Calls OPA for Rego-policy authz decision                  |
| DLP                | (always on)                  | Response-side scrub of sensitive payload fields           |

The toggles are used by the ablation harness to measure each stage's
marginal contribution.

### Federated control plane (port 8180, 8190, 8195-8197)

Five services queried by the sidecars. Each is independently scalable
and has its own audit trail.

| Service                | Port | Role                                                                 |
|------------------------|------|----------------------------------------------------------------------|
| zta-auth               | 8180 | Issues JWTs for agent workload identity                              |
| trust-scorer           | 8190 | Subjective Logic trust aggregation across identity / behavior / LLM  |
| audit-logger           | 8195 | Append-only event store (SQLite, 30-day retention)                   |
| revocation-dispatcher  | 8196 | Single source of truth for active revocations and quarantines       |
| pdp-behavior           | 8197 | Anomaly detection over short/long windows; emits revocation actions  |
| opa                    | 8181 | Authorization PDP (Rego policy in `zta-infrastructure/opa/policy.rego`) |

The trust scorer combines three evidence sources via Subjective Logic
fusion: identity reputation (ω_I), behavioral pattern signals (ω_B), and
LLM-layer detection (ω_L). The aggregated opinion is mapped to one of
three bands: `allow` (≥80), `step-up` (60–79), `block` (<60), with the
thresholds drawn from Kim & Lee 2025. Subjective Logic is used because it
is mathematically equivalent to Bayesian inference on Beta distributions
and provides a principled way to fuse evidence with explicit
uncertainty — it is *deterministic*, not subjective, despite the name.

## Running the tests

```bash
# Prereq: full stack is up and healthy.
pip install -e .

# Per-component smoke tests (these talk to MCP servers directly)
python tests/test_airline_mcp.py
python tests/test_hotel_mcp.py
python tests/test_car_rental_mcp.py

# A2A protocol library unit tests
python -m pytest tests/test_a2a.py -v

# Full federated integration test — proves the closed feedback loop:
#   sidecar -> trust scorer -> audit -> behavior PDP -> dispatcher -> all PEPs deny
python tests/test_federated_loop.py
python tests/test_federated_loop.py --debug   # verbose

# Run everything that doesn't require manual setup
python tests/run_all_tests.py
```

`tests/test_federated_loop.py` is the most important test — it is the
only one that exercises the full federated loop end-to-end through real
sidecars. If this passes, the system is correctly wired.

## The demo UI

`zta_demo_ui.html` is a single-file dashboard that exercises every
stage of the pipeline through three pre-built personas:

- **Security Analyst** — clean traffic, observes pipeline behavior on
  legitimate requests
- **Repeat Offender** — a sequence of escalating prompt-injection
  attempts that drives the trust scorer's identity reputation downward
  and triggers behavior-PDP quarantine
- **System Admin Upgrade** — uses zta-auth to issue elevated identity
  for legitimate-but-sensitive operations

It also shows the live Subjective Logic opinion (b/d/u bars) for the
selected agent, and demonstrates the federated quarantine loop:
revocation issued at one PEP propagates and is enforced at all six.

Open it directly with your browser. The page hits the services on
their host-mapped ports. If your browser blocks the CORS preflights
(symptom: every panel says "Load failed"), run:

```bash
./fix_cors_v2.sh
```

once. The script is idempotent and patches CORS middleware into the
five FastAPI services the UI calls directly. OPA is special-cased.

## Reproducing the paper results

### Per-stage ablation table

The harness in `tests/ablation/run_ablation.py` runs every prompt
scenario through 7 sidecar configurations (BASELINE + 6 with one stage
disabled at a time) and produces per-stage marginal-contribution
numbers.

```bash
# Quick smoke (5 scenarios, BASELINE only)
python tests/ablation/run_ablation.py --no-docker --max-scenarios 5

# Full ablation (32 scenarios × 7 configurations = 224 trials, ~5-10 min)
python tests/ablation/run_ablation.py
```

Outputs land in `tests/ablation/results/<UTC-timestamp>/`:

| File              | Use                                                         |
|-------------------|-------------------------------------------------------------|
| `raw_trials.csv`  | One row per (scenario, configuration); appendix-grade       |
| `pivot.csv`       | Scenarios × configurations matrix                           |
| `pivot.md`        | Same matrix, paste-ready into the paper                     |
| `summary.txt`     | Headline numbers: per-config deny rate, marginal contributions |

The harness mutates only the `airline-agent-sidecar` between
configurations — the other five sidecars stay in BASELINE. This isolates
the effect of each stage at a single PEP. The federated story (revocation
propagating across PEPs) is verified separately by `test_federated_loop.py`.

**Caveats reported in the paper, also worth knowing here:**

- JWT and DLP are not ablated. Removing JWT would invalidate the
  identity model every other stage depends on; DLP runs on the response
  path and does not affect deny decisions.
- Stage order matters. A WAF block on a scenario the trust scorer
  *would also* have caught is attributed to WAF because that is the
  pipeline order. The `NO_WAF` column shows what the next stage catches
  in WAF's absence, which is the right counterfactual.
- The on-disk corpus is 32 scenarios (6 transit + 26 prompt-injection).
  The remaining 17 scenarios in the original 49 are load and
  intent tests that do not have a single deny/allow ground truth and
  are not appropriate for an ablation.

## Repository layout

```
.
├── README.md                        # this file
├── .env.example                     # copy to .env and fill in
├── .gitignore
├── pyproject.toml                   # python packaging + test config
├── docker-compose.zta.yml           # canonical compose file (21 containers)
├── docker-compose.ablation.override.yml
├── fix_cors_v2.sh                   # one-shot CORS patch for the demo UI
├── zta_demo_ui.html                 # single-file dashboard
│
├── agents/                          # multi-agent runtime
│   ├── __init__.py
│   ├── a2a/                         # A2A protocol library (auth, client, server, models)
│   ├── agent-base/                  # shared base class
│   ├── supervisor/                  # LangGraph StateGraph orchestrator
│   ├── airline-agent/               # ReAct agent for airline domain
│   ├── hotel-agent/                 #     "         hotel  domain
│   ├── car-rental-agent/            #     "         car-rental domain
│   └── travel-planner/              # itinerary planner
│
├── mcp-servers/                     # FastMCP tool servers
│   ├── airline/
│   ├── hotel/
│   └── car-rental/
│
├── services/                        # backend services + control plane
│   ├── airline/                     # FastAPI + SQLite (booking domain)
│   ├── hotel/                       #
│   ├── car-rental/                  #
│   ├── auth/                        # zta-auth: JWT issuer
│   ├── trust-scorer/                # SL trust aggregator
│   ├── audit-logger/                # append-only event store
│   ├── revocation-dispatcher/       # active-revocation source of truth
│   └── pdp-behavior/                # behavioral anomaly PDP
│
├── zta-sidecar/                     # .NET YARP reverse proxy (one binary, 6 instances)
│   ├── Program.cs                   # 9-stage pipeline implementation
│   ├── ZtaSidecar.csproj
│   └── Dockerfile
│
├── zta-infrastructure/
│   └── opa/
│       ├── policy.rego              # authz policy
│       └── config.yaml
│
├── test-driver/                     # scenario runner + prompt corpus
│   └── prompts/                     # transit_trust, prompt_injection, intent, load
│
└── tests/
    ├── test_federated_loop.py       # MAIN integration test
    ├── test_a2a.py                  # A2A protocol unit tests
    ├── test_airline_mcp.py          # per-MCP smoke tests
    ├── test_hotel_mcp.py
    ├── test_car_rental_mcp.py
    ├── run_all_tests.py
    └── ablation/
        └── run_ablation.py          # per-stage ablation harness
```

## Troubleshooting

**`docker compose up` hangs at "waiting for healthy".**
The supervisor and agents have `start_period: 15-20s` healthchecks. On
first build, image pulls and .NET restore can push past the start
period — re-run `docker compose ps` after a minute. If a service is
still `unhealthy`, `docker compose logs <service>` will show the
problem.

**Demo UI shows "Load failed" on every panel.**
The five FastAPI control-plane services need CORS middleware to accept
preflight requests from `file://` origins. Run `./fix_cors_v2.sh` once.
The script is idempotent.

**`test_federated_loop.py` fails on phase 1.**
Verify all 21 containers are healthy first
(`docker compose -f docker-compose.zta.yml ps`). The test assumes the
full stack is up; it does not start anything.

**LLM provider errors.**
The agents try Groq → Anthropic → OpenAI, in that order, picking the
first key it finds. If you set multiple keys, only the first is used.

**Sidecar changes do not take effect.**
The sidecars are .NET binaries baked into the image. After editing
`zta-sidecar/Program.cs`, rebuild:

```bash
docker compose -f docker-compose.zta.yml up -d --build airline-agent-sidecar
```

(or whichever sidecar you're targeting). The ablation harness rebuilds
between configurations automatically.

**`zsh` clobbers `$PATH` when running scripts.**
Do not name shell variables `path` in zsh — it is a tied special array
that aliases `$PATH`, and assigning to it will silently break your
shell. Use `urlpath` or any other name. Inside the project's scripts,
absolute paths to binaries (`/usr/bin/curl`) are used to avoid this
class of bug.

**Out of disk after long runs.**
The audit logger keeps 30 days of events by default. To reset:

```bash
docker compose -f docker-compose.zta.yml down -v
```

The `-v` flag removes the `audit-data` volume.

## License

This work is licensed under the Creative Commons Attribution 4.0
International License (CC BY 4.0). See the `LICENSE` file for the full
legal code.

© 2026 Serena Gomez, Dr. Mohamed Farag, and the Applied Generative AI
(AGAI) Group, Carnegie Mellon University.
