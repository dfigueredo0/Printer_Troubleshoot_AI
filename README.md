# zt411-troubleshooter-agent

A dual-mode, agentic Zebra ZT411 troubleshooting system that can operate in both cloud-connected and offline/limited-connectivity environments to diagnose and resolve printer issues across the OS/queue/driver, network, and device/hardware layers. The system should choose the lowest-risk, highest-information next step, ground every recommendation and action in observable evidence and retrieved Zebra/internal documentation, enforce policy-based safety controls that vary by runtime mode, and maintain a complete audit trail of decisions, tool outputs, confirmations, and outcomes.

## Quick Start

```bash
poetry install
python scripts/gen_synth_data.py
make train
make eval
make serve
```
