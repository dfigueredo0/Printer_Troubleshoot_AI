"""
Owns: reachability and correctness of the path to the printer.
Ping/ARP/route sanity; DNS vs static IP mismatch; DHCP lease churn.
Port checks: 9100 (RAW), 515 (LPD), 631 (IPP), 80/443 (web mgmt) when allowed.
VLAN/firewall/proxy gotchas; MTU issues (rare but real).
Discovers “wrong device at IP” problems using MAC/OUI, SNMP sysDescr, HTTP banner.
Evidence: reachability results, port probes, DNS resolution, traceroute (where permitted).
"""