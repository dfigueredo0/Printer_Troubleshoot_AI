from zt411_agent.agent.tools import snmp_walk
import json
from datetime import datetime, timezone

IP = "192.168.99.10"

snapshot = snmp_walk(IP, "1.3.6.1.4.1.10642.2.3", max_rows=100)
fixture = {
    "captured_at": datetime.now(timezone.utc).isoformat(),
    "printer_state": "media_out + ribbon_out (consumables removed)",
    "rows": snapshot.output['rows'],
}
with open("/tmp/zt411_media_ribbon_out.json", "w") as f:
    json.dump(fixture, f, indent=2)
print(f"Captured {len(snapshot.output['rows'])} OIDs in error state")