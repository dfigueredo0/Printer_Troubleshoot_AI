import json
from datetime import datetime, timezone
from zt411_agent.agent.tools import snmp_walk, ipp_get_attributes

IP = "192.168.99.10"

ipp = ipp_get_attributes(IP)
walk_2_3 = snmp_walk(IP, "1.3.6.1.4.1.10642.2.3", max_rows=100)
walk_1 = snmp_walk(IP, "1.3.6.1.4.1.10642.1", max_rows=50)

fixture = {
    "captured_at": datetime.now(timezone.utc).isoformat(),
    "printer_state_human": "MEDIA OUT + RIBBON OUT + PAUSED (yellow pause LED lit, display shows all three)",
    "ipp_attributes": ipp.output,
    "snmp_walk_10642_2_3": walk_2_3.output,
    "snmp_walk_10642_1": walk_1.output,
}
with open("/tmp/zt411_fixture_media_ribbon_paused.json", "w") as f:
    json.dump(fixture, f, indent=2, default=str)
print("Fixture saved")