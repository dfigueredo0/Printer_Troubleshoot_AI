# ZT411 Troubleshooter Agent

A dual-mode, agentic Zebra ZT411 troubleshooting system that operates in cloud-connected and offline environments to diagnose and resolve printer issues across OS/queue/driver, network, and device/hardware layers.

The system chooses the lowest-risk, highest-information next step, grounds every recommendation in observable evidence and retrieved documentation, enforces policy-based safety controls, and maintains a complete audit trail.

## Installation

### Prerequisites

- Python 3.11+ and [Poetry](https://python-poetry.org/)
- Node.js 18+ and npm
- Docker (optional, for containerized deployment)

### Backend (Python Agent)

```bash
cd zt411-troubleshooter-agent
poetry install
```

Copy the example environment file and configure API keys:

```bash
cp .env.example .env
# Edit .env with your Anthropic API key, Ollama endpoint, etc.
```

### Frontend (Next.js Console)

```bash
cd frontend
npm install
```

## Basic Usage

### 1. Start the API server

```bash
cd zt411-troubleshooter-agent
make serve
# API available at http://localhost:8000
```

### 2. Start the frontend

```bash
cd frontend
npm run dev
# Console available at http://localhost:3000
```

### 3. Create a session and diagnose

Via the web console, create a new troubleshooting session by entering symptoms, OS platform, and device IP. Then run the diagnostic loop which will route work through specialist agents (device, network, CUPS, Windows, validation) until the issue is resolved or escalated.

Via curl:

```bash
# Create session
curl -X POST http://localhost:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"symptoms": ["paper jam"], "os_platform": "windows", "device_ip": "10.0.0.50", "user_description": "Printer shows paper jam but tray is clear"}'

# Run diagnosis
curl -X POST http://localhost:8000/sessions/{session_id}/diagnose \
  -H "Content-Type: application/json" \
  -d '{"force_tier": "auto", "max_steps": 10}'
```

### Docker

```bash
cd zt411-troubleshooter-agent
make docker-build
make docker-run
# API available at http://localhost:8000
```

## Testing

### Backend tests

```bash
cd zt411-troubleshooter-agent
make test
```

### Frontend lint

```bash
cd frontend
npm run lint
```

### CI

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs the full matrix on Python 3.11 and 3.12: `poetry install` -> `make lint` -> `make test`.

## Development

### Agent Architecture

The core loop follows **PLAN -> ACT -> VALIDATE**, repeating until success, escalation, or the step limit is reached. Five specialists handle different domains:

| Specialist | Domain                       |
| ---------- | ---------------------------- |
| Device     | Device info, USB/network     |
| Network    | IP, firewall, network stack  |
| CUPS       | Linux print subsystem        |
| Windows    | Print queue, drivers         |
| Validation | Success criteria, guardrails |

The LLM planner supports three tiers (Claude API, Ollama, deterministic offline) with automatic fallback.

### Linting

```bash
cd zt411-troubleshooter-agent
make lint
```

### Training and Evaluation

```bash
cd zt411-troubleshooter-agent
python scripts/gen_synth_data.py   # Generate synthetic training data
make train                         # Train the model
make eval                          # Run evaluation
```

### Building the RAG Index

```bash
cd zt411-troubleshooter-agent
make build-rag                     # Build dataset from raw docs
make build-offline-cache           # Pre-build offline cache
```
