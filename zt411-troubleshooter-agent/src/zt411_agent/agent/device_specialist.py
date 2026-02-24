"""
Owns: what the ZT411 itself says and what’s physically wrong.
SNMP queries: status, alerts, consumables, head open, media out, ribbon out, pause, error codes.
Optional IPP attributes reads where supported; web UI scrape as last resort.
Maps error codes to Zebra docs/KB.
Advises physical actions: load media/ribbon, calibrate, close head, clear jam, reset, firmware check.
Evidence: SNMP OIDs/values, device-reported alerts, error code + doc citation IDs.
"""