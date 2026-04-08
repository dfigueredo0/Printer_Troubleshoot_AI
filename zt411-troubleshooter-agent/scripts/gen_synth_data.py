"""
gen_synth_data.py — Generate synthetic troubleshooting cases for the ZT411 agent.

Outputs data/sample/sample_cases.jsonl using the SampleCase schema defined in
src/zt411_agent/data/dataset.py.  Cases are built from domain-specific template
pools and randomly combined, so you can generate an arbitrary number of unique
cases across all five specialist domains.

Real-world cases (from actual Zebra support forums, knowledge base articles,
and common field tickets) are always included to anchor the dataset.

Usage:
    python scripts/gen_synth_data.py              # default 100 synthetic + real-world
    python scripts/gen_synth_data.py --count 500  # custom synthetic count
    python scripts/gen_synth_data.py --seed 99    # reproducible
"""

import argparse
import json
import random
from pathlib import Path

# ---------------------------------------------------------------------------
# Domain label mapping (must stay in sync with train.py / eval.py)
# ---------------------------------------------------------------------------
DOMAIN_LABELS = {
    "network": 0,
    "device": 1,
    "windows": 2,
    "cups": 3,
    "validation": 4,
}

# ---------------------------------------------------------------------------
# Real-world cases — sourced from Zebra support forums, KB articles, and
# common field service patterns.  These are always included in every
# generated dataset to provide a grounding baseline.
# ---------------------------------------------------------------------------
# fmt: off
REAL_WORLD_CASES = [
    # --- Network (real tickets) ---
    {
        "case_id": "rw-net-001",
        "description": "ZT411 shows 'Connected' on LCD but workstation gets 'Error - Printing' in Windows queue after moving to new office with Meraki managed switches",
        "symptoms": ["connected on LCD", "Error - Printing in queue", "was working in old office", "new managed switch"],
        "os_platform": "windows",
        "device_ip": "10.1.50.83",
        "expected_resolution": "network",
        "expected_steps": 3,
        "expected_actions": ["ping", "tcp_connect", "snmp_get"],
        "resolution_notes": "Meraki switch had storm control enabled dropping print traffic; whitelisted port 9100 in switch ACL",
        "risk_class": "safe",
    },
    {
        "case_id": "rw-net-002",
        "description": "Zebra ZT411 prints first label fine then hangs for 30+ seconds between subsequent labels in a batch of 500 shipping labels",
        "symptoms": ["first label prints", "30 second delay between labels", "batch of 500", "shipping labels"],
        "os_platform": "windows",
        "device_ip": "192.168.1.205",
        "expected_resolution": "network",
        "expected_steps": 4,
        "expected_actions": ["tcp_connect", "snmp_get", "ping"],
        "resolution_notes": "TCP Nagle algorithm and delayed ACK interaction; disabled Nagle on print server NIC or switched to USB",
        "risk_class": "safe",
    },
    {
        "case_id": "rw-net-003",
        "description": "After IT enabled 802.1X on all switch ports, ZT411 cannot obtain IP via DHCP and shows 0.0.0.0",
        "symptoms": ["no IP address", "0.0.0.0 on LCD", "802.1X enabled", "DHCP failure"],
        "os_platform": "linux",
        "device_ip": "0.0.0.0",
        "expected_resolution": "network",
        "expected_steps": 3,
        "expected_actions": ["ping", "tcp_connect"],
        "resolution_notes": "ZT411 does not support 802.1X natively; configured MAB (MAC Authentication Bypass) on switch port for printer MAC",
        "risk_class": "safe",
    },
    {
        "case_id": "rw-net-004",
        "description": "Print jobs from SAP go to ZT411 but come out as blank labels; same ZPL prints fine from Notepad via raw TCP port 9100",
        "symptoms": ["SAP prints blank labels", "ZPL works from Notepad", "raw TCP works", "SAP specific"],
        "os_platform": "windows",
        "device_ip": "10.20.1.44",
        "expected_resolution": "network",
        "expected_steps": 3,
        "expected_actions": ["tcp_connect", "snmp_get"],
        "resolution_notes": "SAP was sending to port 515 (LPD) which wraps in LPR headers; printer misinterpreted stream. Changed SAP config to direct TCP 9100",
        "risk_class": "safe",
    },

    # --- Device (real tickets) ---
    {
        "case_id": "rw-dev-001",
        "description": "ZT411 prints 2-3 good labels then starts printing labels shifted down by about 1 inch; problem worsens with each subsequent label",
        "symptoms": ["label position drifts", "shifts down progressively", "first labels good", "gets worse over time"],
        "os_platform": "windows",
        "device_ip": "192.168.1.50",
        "expected_resolution": "device",
        "expected_steps": 3,
        "expected_actions": ["device_status", "sensor_calibrate"],
        "resolution_notes": "Media sensor miscalibrated for the gap size on new label stock; ran full calibration via FEED+PAUSE and adjusted media type to gap/notch",
        "risk_class": "safe",
    },
    {
        "case_id": "rw-dev-002",
        "description": "After replacing thermal transfer ribbon roll, ZT411 LCD shows RIBBON OUT even though ribbon is loaded and threaded correctly per the manual diagram",
        "symptoms": ["ribbon out error", "ribbon installed correctly", "just replaced ribbon", "LCD error"],
        "os_platform": "linux",
        "device_ip": "172.16.0.30",
        "expected_resolution": "device",
        "expected_steps": 2,
        "expected_actions": ["device_status", "snmp_get"],
        "resolution_notes": "Ribbon was wound ink-side-out (wrong orientation); Zebra uses ink-side-in on ZT series. Reloaded with correct orientation",
        "risk_class": "safe",
    },
    {
        "case_id": "rw-dev-003",
        "description": "ZT411 making loud clicking/grinding noise during print, print quality has horizontal white lines across every label at regular intervals",
        "symptoms": ["grinding noise", "clicking during print", "horizontal white lines", "regular interval defect"],
        "os_platform": "windows",
        "device_ip": "192.168.1.100",
        "expected_resolution": "device",
        "expected_steps": 2,
        "expected_actions": ["device_status", "printhead_check"],
        "resolution_notes": "Platen roller damaged with flat spot from label adhesive buildup; cleaned with IPA and replaced platen roller",
        "risk_class": "safe",
    },
    {
        "case_id": "rw-dev-004",
        "description": "Warehouse ZT411 printing barcodes that scan fine on Zebra scanner but fail to scan on customer's Honeywell scanners; same barcodes from old S4M printer scan fine on both",
        "symptoms": ["barcodes won't scan on Honeywell", "scan OK on Zebra scanner", "old printer was fine", "customer scanner issue"],
        "os_platform": "windows",
        "device_ip": "10.0.0.77",
        "expected_resolution": "device",
        "expected_steps": 3,
        "expected_actions": ["device_status", "snmp_get"],
        "resolution_notes": "Print speed set to 6ips causing thin bars; Honeywell scanner less tolerant. Reduced to 4ips and increased barcode module width from 2 to 3 dots",
        "risk_class": "safe",
    },

    # --- Windows (real tickets) ---
    {
        "case_id": "rw-win-001",
        "description": "After Windows 11 23H2 update, ZDesigner ZT411-300dpi driver shows 'Driver is unavailable' in Settings > Printers. Printer worked yesterday before forced update",
        "symptoms": ["driver unavailable after update", "Windows 11 23H2", "worked yesterday", "forced update"],
        "os_platform": "windows",
        "device_ip": "192.168.1.100",
        "expected_resolution": "windows",
        "expected_steps": 4,
        "expected_actions": ["driver_query", "event_log", "driver_install"],
        "resolution_notes": "Win11 23H2 removed V3 printer drivers; needed to download V4 ZDesigner driver from Zebra support (version 5.3.10+). Uninstalled old V3 remnants first",
        "risk_class": "config_change",
    },
    {
        "case_id": "rw-win-002",
        "description": "Print spooler service crashes every time user sends label to ZT411 from Chrome browser on Windows Server 2019 RDS session; other printers work fine in same session",
        "symptoms": ["spooler crashes", "Chrome printing only", "RDS session", "other printers OK"],
        "os_platform": "windows",
        "device_ip": "10.10.5.20",
        "expected_resolution": "windows",
        "expected_steps": 3,
        "expected_actions": ["spooler_status", "event_log", "driver_query"],
        "resolution_notes": "ZDesigner driver conflict with Chrome's print rendering in RDS; set printer to use RAW datatype only in Advanced properties and disabled EMF spooling",
        "risk_class": "service_restart",
    },
    {
        "case_id": "rw-win-003",
        "description": "IT deployed ZT411 printers via GPO to 200 workstations but ~40 machines show the printer as offline even though they can ping it; same GPO, same driver package",
        "symptoms": ["GPO deployed", "40 of 200 offline", "can ping printer", "same driver same GPO"],
        "os_platform": "windows",
        "device_ip": "10.1.1.100",
        "expected_resolution": "windows",
        "expected_steps": 4,
        "expected_actions": ["driver_query", "spooler_status", "event_log", "registry_check"],
        "resolution_notes": "Affected machines had SNMP status monitoring enabled in port config; ZT411 SNMP community string mismatch. Disabled SNMP status in port config via GPO update",
        "risk_class": "config_change",
    },
    {
        "case_id": "rw-win-004",
        "description": "User double-clicks .zpl file and it opens in Notepad instead of sending to ZT411; previous tech set up file association but it was lost after Windows feature update",
        "symptoms": ["ZPL opens in Notepad", "file association lost", "after feature update", "cannot send ZPL directly"],
        "os_platform": "windows",
        "device_ip": "192.168.1.50",
        "expected_resolution": "windows",
        "expected_steps": 2,
        "expected_actions": ["driver_query", "registry_check"],
        "resolution_notes": "Created batch script to copy .zpl to printer share (copy file.zpl \\\\server\\ZT411) and registered .zpl file association to the batch handler",
        "risk_class": "safe",
    },

    # --- CUPS (real tickets) ---
    {
        "case_id": "rw-cups-001",
        "description": "CUPS on RHEL 9 won't print to ZT411; error_log shows 'Unable to locate printer zt411' even though lpstat -p shows it; worked on RHEL 8",
        "symptoms": ["Unable to locate printer", "lpstat shows it", "RHEL 9 migration", "worked on RHEL 8"],
        "os_platform": "linux",
        "device_ip": "10.0.1.15",
        "expected_resolution": "cups",
        "expected_steps": 3,
        "expected_actions": ["cups_status", "cups_log", "cups_config"],
        "resolution_notes": "RHEL 9 uses cups-browsed with different discovery; printer URI had changed from socket:// to ipp://. Reconfigured with explicit socket://10.0.1.15:9100",
        "risk_class": "config_change",
    },
    {
        "case_id": "rw-cups-002",
        "description": "Raspberry Pi CUPS print server sends ZPL to ZT411 but every label has PostScript header text printed literally on the label",
        "symptoms": ["PostScript header printed on label", "Raspberry Pi print server", "ZPL sent", "text on label"],
        "os_platform": "linux",
        "device_ip": "192.168.1.55",
        "expected_resolution": "cups",
        "expected_steps": 2,
        "expected_actions": ["cups_status", "cups_config"],
        "resolution_notes": "CUPS auto-selected foomatic PostScript driver; changed to raw queue: lpadmin -p ZT411 -v socket://192.168.1.55:9100 -m raw",
        "risk_class": "config_change",
    },
    {
        "case_id": "rw-cups-003",
        "description": "Ubuntu 22.04 user can print to ZT411 from terminal (lp -d zt411 file.zpl) but printing from GUI applications sends garbage; same file works both ways on 20.04",
        "symptoms": ["terminal printing works", "GUI printing garbage", "Ubuntu 22.04", "worked on 20.04"],
        "os_platform": "linux",
        "device_ip": "192.168.1.100",
        "expected_resolution": "cups",
        "expected_steps": 3,
        "expected_actions": ["cups_status", "cups_log", "cups_config"],
        "resolution_notes": "GNOME print dialog sends through cups-filters which converts to PDF first; set MIME type filter rule to pass application/vnd.zebra-zpl raw",
        "risk_class": "config_change",
    },
    {
        "case_id": "rw-cups-004",
        "description": "Docker container running Python Flask app cannot print to ZT411 through host CUPS; 'Connection refused' in container but host can print fine",
        "symptoms": ["Docker container", "connection refused", "host prints fine", "Flask app"],
        "os_platform": "linux",
        "device_ip": "172.17.0.1",
        "expected_resolution": "cups",
        "expected_steps": 3,
        "expected_actions": ["cups_status", "cups_config", "tcp_connect"],
        "resolution_notes": "CUPS listening on localhost only; updated cupsd.conf to Listen on docker0 bridge IP and added Allow from 172.17.0.0/16",
        "risk_class": "config_change",
    },

    # --- Validation (real tickets) ---
    {
        "case_id": "rw-val-001",
        "description": "ESCALATION REVIEW: Tech replaced printhead, network cable, and reinstalled driver but ZT411 still prints garbled output. Three separate ticket reopen cycles over 2 weeks",
        "symptoms": ["escalation needed", "3 reopen cycles", "printhead replaced", "driver reinstalled", "still garbled", "2 week unresolved"],
        "os_platform": "windows",
        "device_ip": "10.5.5.30",
        "expected_resolution": "validation",
        "expected_steps": 3,
        "expected_actions": ["device_status", "snmp_get"],
        "resolution_notes": "ESCALATED: Root cause was firmware bug in V81.20.13Z corrupting ZPL parser; no field fix available. RMA issued, replacement unit with V81.20.18Z resolved",
        "risk_class": "firmware",
    },
    {
        "case_id": "rw-val-002",
        "description": "CONFIRMATION REQUIRED: Automated diagnostic recommends downgrading ZT411 firmware from V81.20.18Z to V81.20.15Z to match fleet standard, but this version has known CVE for SNMP",
        "symptoms": ["firmware downgrade proposed", "CVE in target version", "fleet standardization", "confirmation gate triggered"],
        "os_platform": "linux",
        "device_ip": "172.16.5.40",
        "expected_resolution": "validation",
        "expected_steps": 2,
        "expected_actions": ["device_status", "snmp_get"],
        "resolution_notes": "BLOCKED BY GUARDRAIL: Firmware downgrade rejected due to known CVE-2024-XXXX in V81.20.15Z SNMP stack. Recommended updating fleet standard to V81.20.18Z instead",
        "risk_class": "firmware",
    },
    {
        "case_id": "rw-val-003",
        "description": "RECHECK FAILED: Customer confirmed printer is working but automated verification ping and test print both fail 4 hours after initial fix was applied",
        "symptoms": ["recheck failure", "customer said fixed", "automated verification failed", "ping fails 4 hours later"],
        "os_platform": "windows",
        "device_ip": "192.168.1.200",
        "expected_resolution": "validation",
        "expected_steps": 4,
        "expected_actions": ["ping", "tcp_connect", "device_status"],
        "resolution_notes": "PREMATURE CLOSURE: Printer obtained DHCP address that conflicted with another device; fix held until lease renewal caused collision. Assigned static IP reservation",
        "risk_class": "safe",
    },
    {
        "case_id": "rw-val-004",
        "description": "COMPOUND ISSUE: ZT411 has both a network connectivity problem and a worn printhead, but agent only diagnosed the network issue. After network fix, labels are still unreadable",
        "symptoms": ["compound root cause", "partial fix only", "network fixed but labels bad", "worn printhead missed"],
        "os_platform": "windows",
        "device_ip": "10.0.0.55",
        "expected_resolution": "validation",
        "expected_steps": 5,
        "expected_actions": ["ping", "tcp_connect", "device_status", "printhead_check"],
        "resolution_notes": "MULTI-DOMAIN: Agent correctly fixed network (VLAN) but missed printhead wear. Second pass needed with device specialist. Added cross-domain verification step",
        "risk_class": "safe",
    },
]
# fmt: on

# ---------------------------------------------------------------------------
# Template pools — each domain has lists of interchangeable fragments that
# get randomly combined by generate_case() to produce diverse cases.
#
# Validation templates use distinctive meta-diagnostic language (ESCALATION,
# RECHECK, CONFIRMATION, COMPOUND, etc.) to reduce overlap with primary
# diagnostic domains.
# ---------------------------------------------------------------------------
TEMPLATES = {
    "network": {
        "descriptions": [
            "Printer offline after network switch replacement",
            "ZT411 unreachable after static IP change",
            "Intermittent connection drops during large batch print",
            "Printer not discovered by Zebra Setup Utilities on WiFi",
            "Firewall blocking ZPL commands on port 9100",
            "DNS resolution failure prevents printing by hostname",
            "Printer goes offline every time DHCP lease renews",
            "Cannot reach printer after subnet migration",
            "Slow printing over VPN connection to remote ZT411",
            "Printer connection refused on port 6101 (status port)",
            "ZT411 loses connectivity after switch firmware upgrade",
            "Network printer shows offline but web UI loads fine",
            "Printer unreachable on secondary VLAN after trunk reconfiguration",
            "Multicast storms causing intermittent printer drops on warehouse floor",
            "ZT411 gets wrong IP from rogue DHCP server on network",
            "IPsec VPN tunnel drops printer connection every 8 hours on SA rekey",
        ],
        "symptom_pool": [
            "offline", "cannot print", "network unreachable", "ping timeout",
            "IP conflict suspected", "partial print", "connection reset",
            "timeout mid-job", "not discovered", "wifi connected",
            "cannot find printer", "port 9100 refused", "ping works",
            "cannot resolve hostname", "IP works but hostname fails",
            "DHCP lease expired", "subnet mismatch", "slow response",
            "connection refused", "intermittent drops", "wrong IP assigned",
            "trunk port misconfigured", "multicast storm", "VPN rekey drop",
        ],
        "action_pool": [
            "ping", "tcp_connect", "arp_scan", "snmp_get",
            "udp_broadcast", "firewall_check", "dns_lookup", "traceroute",
        ],
        "resolution_pool": [
            "Port 9100 blocked on new switch VLAN config",
            "IP address conflict with another device on subnet",
            "MTU mismatch between printer and switch causing fragmentation",
            "AP isolation enabled blocking mDNS discovery",
            "Host firewall outbound rule blocking TCP 9100",
            "Stale DNS record; printer DHCP lease changed IP without DNS update",
            "DHCP scope exhausted; assign static reservation",
            "Printer moved to new subnet without updating gateway",
            "VPN split-tunnel policy not routing printer subnet",
            "Switch port speed/duplex mismatch causing CRC errors",
            "Rogue DHCP server handing out wrong gateway; block on switch",
            "IPsec SA lifetime too short; increase rekey interval or use IKEv2",
        ],
        "os_platforms": ["windows", "linux"],
        "risk_classes": ["safe"],
    },
    "device": {
        "descriptions": [
            "ZT411 LCD shows HEAD OPEN error, lid is closed",
            "Labels printing with faded/light patches on right side",
            "Ribbon wrinkle causing vertical lines on labels",
            "Printer stuck in PAUSE after power cycle",
            "ZT411 USB not recognized when plugged into workstation",
            "Continuous media detected as label stock, skipping feeds",
            "Printhead temperature warning on LCD during long run",
            "Labels not peeling correctly with peel-off option enabled",
            "Printer feeds blank labels, skips printed ones",
            "Cutter blade not cutting cleanly, tearing labels",
            "ZT411 LCD frozen, buttons unresponsive",
            "Media out error but roll is loaded",
            "Label adhesive residue building up on platen roller causing jams",
            "Printhead dots burned out in a line, creating white streak on every label",
            "Ribbon alert on direct thermal media (ribbon not needed)",
            "ZT411 reset to factory defaults after brief power outage",
        ],
        "symptom_pool": [
            "head open error", "lid closed", "will not print",
            "faded print", "uneven darkness", "right side light",
            "vertical lines", "ribbon wrinkle", "smudging",
            "paused", "will not resume", "amber light",
            "usb not recognized", "device not found", "no driver prompt",
            "skipping labels", "extra blank labels", "media type wrong",
            "overheating", "peel failure", "cutter jam",
            "LCD frozen", "buttons unresponsive", "media out error",
            "adhesive buildup", "white streak", "ribbon alert on DT media",
            "factory reset after power loss",
        ],
        "action_pool": [
            "snmp_get", "device_status", "usb_enum",
            "sensor_calibrate", "printhead_check",
        ],
        "resolution_pool": [
            "Head-open sensor dirty; clean with compressed air",
            "Printhead element burnout on right side; replace printhead",
            "Ribbon tension spring loose; adjust ribbon supply spindle tension",
            "Media not loaded correctly; reload media and run calibration",
            "Faulty USB cable; replacement cable resolved detection issue",
            "Media sensor set to gap-detect; switch to continuous mode via LCD",
            "Reduce print speed to let printhead cool between jobs",
            "Peel-off roller worn; replace peel assembly",
            "Gap sensor needs recalibration for current media stock",
            "Cutter blade dull after high-volume use; replace cutter module",
            "Hard reset via power cycle with PAUSE+CANCEL held resolved freeze",
            "Media guide too tight; widen to match label roll width",
            "Clean platen roller with IPA to remove adhesive buildup",
            "Printhead replacement needed; dot burnout confirmed with test pattern",
            "Switch printer from thermal transfer to direct thermal mode in LCD menu",
            "Enable power-loss config retention in printer NV memory settings",
        ],
        "os_platforms": ["windows", "linux"],
        "risk_classes": ["safe"],
    },
    "windows": {
        "descriptions": [
            "Driver crash (BSOD) after Windows Update KB5034441",
            "Print jobs stuck in queue, spooler service hung",
            "ZDesigner driver missing after in-place Windows upgrade",
            "Wrong paper size selected causes label truncation",
            "Print job sent but nothing happens, no errors shown",
            "Multiple printer instances after IP address change",
            "Printer preferences reset after every reboot",
            "Access denied when trying to print from non-admin account",
            "Bi-directional communication error in driver properties",
            "Print to file dialog appears instead of printing",
            "Spooler crashes when sending ZPL commands via driver",
            "Generic text-only driver installed instead of ZDesigner",
            "Windows Terminal Server session cannot see ZT411 via printer redirection",
            "Event ID 372 Printer driver compatibility error in Event Viewer after Windows update",
            "ZDesigner driver install fails with 'Access denied' on domain-joined workstation",
            "Print queue shows 'Error' status but driver properties say printer is ready",
        ],
        "symptom_pool": [
            "driver crash", "BSOD", "print spooler restart",
            "jobs stuck", "spooler not responding", "cannot cancel jobs",
            "printer not found", "no driver", "upgrade to Win11",
            "label cut off", "bottom half missing", "wrong size",
            "silent failure", "no error", "job disappears from queue",
            "duplicate printers", "old printer offline",
            "preferences reset", "access denied", "bi-di error",
            "print to file", "spooler crash", "wrong driver installed",
            "RDS redirect failure", "Event ID 372", "GPO driver install blocked",
            "queue shows Error", "driver says Ready",
        ],
        "action_pool": [
            "driver_query", "spooler_status", "event_log",
            "spooler_restart", "driver_install", "printer_prefs",
            "registry_check",
        ],
        "resolution_pool": [
            "Incompatible ZDesigner driver v5.1; rollback to v5.0.3 or install v5.2 hotfix",
            "Corrupt spool file; clear spool\\PRINTERS folder and restart spooler",
            "In-place upgrade removed third-party drivers; reinstall ZDesigner",
            "Driver defaulted to Letter; set custom label size 4x6 in Printing Preferences",
            "Printer port set to FILE: instead of TCP/IP; reconfigure port",
            "Remove stale printer entries; update IP on remaining printer port",
            "Group Policy overwriting local preferences; set via GPO instead",
            "User needs Print permission on printer security tab",
            "Disable bi-directional support in port settings",
            "Default printer GPO pointing to wrong queue; update registry",
            "ZPL passthrough requires raw port, not driver rendering",
            "Remove generic driver and install correct ZDesigner package",
            "Enable printer redirection in RDS Group Policy session settings",
            "Install updated V4 driver package compatible with Win11 23H2",
            "Add driver package to Point and Print restrictions GPO whitelist",
            "Disable SNMP status monitoring in TCP/IP port configuration",
        ],
        "os_platforms": ["windows"],
        "risk_classes": ["safe", "config_change", "service_restart"],
    },
    "cups": {
        "descriptions": [
            "CUPS queue paused after failed test page on Ubuntu 22.04",
            "Raw ZPL passthrough not working through CUPS",
            "Permission denied when submitting print job via lp command",
            "CUPS shows printer as Idle but jobs never print",
            "Labels printing with wrong encoding after CUPS upgrade",
            "IPP Everywhere auto-setup creates wrong PPD for ZT411",
            "AppArmor blocking CUPS backend from reaching printer",
            "CUPS web interface not loading after system update",
            "Job held indefinitely with authentication-required state",
            "Printer disappears from CUPS after system reboot",
            "Error filter failed in CUPS error_log",
            "Duplex option shown for ZT411 (non-duplex printer)",
            "CUPS on Debian 12 cannot find socket backend for direct TCP printing",
            "SELinux blocking cupsd from binding to network port on RHEL 9",
            "lpinfo -v shows ZT411 via dnssd but queue creation fails with URI error",
            "CUPS job accounting shows 0 pages printed even for successful prints",
        ],
        "symptom_pool": [
            "queue paused", "test page failed", "filter error",
            "ZPL commands rendered as text", "not raw mode", "garbled output",
            "permission denied", "lp error", "not authorized",
            "idle but not printing", "jobs accepted but stuck", "no error",
            "garbled text", "encoding issue", "worked before upgrade",
            "wrong driver auto-selected", "label dimensions wrong", "IPP setup",
            "AppArmor denied", "web UI 403", "auth required",
            "printer disappeared", "filter failed", "bogus duplex option",
            "socket backend missing", "SELinux denied", "dnssd URI error",
            "0 pages in accounting",
        ],
        "action_pool": [
            "cups_status", "cups_log", "cups_config",
            "tcp_connect", "lpstat_check",
        ],
        "resolution_pool": [
            "Missing cups-filters package; install and resume queue",
            "Switch to raw queue with application/vnd.zebra-zpl MIME type",
            "User not in lpadmin group; add user or update cupsd.conf policy",
            "Backend socket timeout too low; increase in printers.conf",
            "Add DefaultCharset UTF-8 to cupsd.conf after CUPS 2.4 upgrade",
            "Delete auto-created queue; manually add raw queue on port 9100",
            "Update AppArmor profile to allow /usr/lib/cups/backend/socket",
            "cupsd.conf Listen directive bound to wrong interface; fix and restart",
            "Disable authentication for local jobs in cupsd.conf",
            "Add printer to /etc/cups/printers.conf with persistent settings",
            "Replace filter with passthrough for raw ZPL jobs",
            "Override IPP attributes to remove duplex capability",
            "Install cups-backend-socket package for direct TCP/IP printing",
            "Adjust SELinux boolean: setsebool -P cups_connect_network on",
            "Use socket:// URI directly instead of dnssd:// for reliability",
            "Raw queues don't report page counts; expected behavior for ZPL",
        ],
        "os_platforms": ["linux"],
        "risk_classes": ["safe", "config_change"],
    },
    "validation": {
        "descriptions": [
            # Distinctive meta-diagnostic / escalation language
            "ESCALATION REVIEW: Previous fix attempt failed, agent loop exhausted all specialists",
            "RECHECK REQUIRED: Automated post-fix verification detected regression 2 hours later",
            "CONFIRMATION GATE: Proposed action requires operator approval before execution",
            "COMPOUND DIAGNOSIS: Multiple simultaneous failures across different subsystems",
            "GUARDRAIL TRIGGERED: Agent proposed destructive action on production printer",
            "POST-FIX VALIDATION: Print quality verification scan failed after network fix",
            "ESCALATION: Three consecutive specialist handoffs without resolution",
            "RECHECK FAILED: User reported fixed but automated verification shows otherwise",
            "SAFETY REVIEW: Firmware downgrade proposed but target version has known vulnerabilities",
            "MULTI-DEVICE IMPACT: Fix applied to one printer in group but others still failing",
            "MAX STEPS EXCEEDED: Agent loop terminated at step limit without success criteria met",
            "PREMATURE CLOSURE: Ticket closed by user but monitoring detects recurring symptoms",
            "CROSS-DOMAIN REVIEW: Root cause spans network and device domains, needs joint analysis",
            "ROLLBACK NEEDED: Applied configuration change made the problem worse",
            "SECOND OPINION: Conflicting diagnostics from network and device specialists",
            "FIELD SERVICE REQUIRED: Remote diagnostics exhausted, physical intervention needed",
        ],
        "symptom_pool": [
            # Clearly meta / process-oriented symptoms
            "escalation needed", "3 reopen cycles", "specialist loop exhausted",
            "recheck failure", "automated verification failed", "regression detected",
            "confirmation gate triggered", "operator approval pending", "action held",
            "compound root cause", "multi-subsystem failure", "partial fix only",
            "guardrail blocked action", "destructive action rejected", "production printer risk",
            "post-fix scan failure", "quality verification failed", "barcode unreadable after fix",
            "max steps reached", "loop terminated", "no resolution found",
            "premature closure", "recurring symptoms", "ticket reopened",
            "cross-domain issue", "joint analysis needed", "conflicting diagnostics",
            "rollback required", "change made problem worse", "field service dispatch needed",
        ],
        "action_pool": [
            "ping", "tcp_connect", "device_status",
            "snmp_get", "spooler_status",
        ],
        "resolution_pool": [
            "ESCALATED: Root cause beyond remote capability; dispatching field service technician",
            "ESCALATED: Attached full diagnostic log to Tier 2 support ticket for review",
            "RECHECK: Original fix insufficient; underlying issue resurfaced post-verification",
            "BLOCKED: Proposed firmware downgrade has known CVE; updating fleet standard instead",
            "BLOCKED: Factory reset rejected by guardrail; manual intervention required on-site",
            "COMPOUND: Decomposed into separate network + device issues; running second specialist pass",
            "COMPOUND: Fix addressed symptom A (network) but symptom B (device wear) requires separate action",
            "ROLLBACK: Reverted configuration change that caused regression; re-analyzing root cause",
            "VALIDATION: Post-fix barcode scan failed; adjusting print speed and re-verifying",
            "MULTI-DEVICE: Same fix needed on 3 additional printers in the group; scheduling batch update",
            "PREMATURE CLOSURE: Automated monitoring caught recurrence; reopening with additional evidence",
            "TIMEOUT: Agent loop reached max_steps (10); escalating with partial diagnosis attached",
        ],
        "os_platforms": ["windows", "linux"],
        "risk_classes": ["safe", "destructive", "firmware"],
    },
}

# IP address pools per subnet style
_IP_POOLS = [
    "192.168.1.{host}",
    "192.168.0.{host}",
    "192.168.2.{host}",
    "10.0.0.{host}",
    "10.10.1.{host}",
    "172.16.0.{host}",
    "172.16.5.{host}",
]


def _random_ip(rng: random.Random) -> str:
    template = rng.choice(_IP_POOLS)
    return template.format(host=rng.randint(2, 254))


def generate_case(
    domain: str,
    case_num: int,
    rng: random.Random,
) -> dict:
    """
    Generate a single synthetic case for *domain* by randomly sampling from
    the domain's template pool.  Every field in the SampleCase schema is
    populated.
    """
    t = TEMPLATES[domain]
    prefix = {"network": "net", "device": "dev", "windows": "win",
              "cups": "cups", "validation": "val"}[domain]

    description = rng.choice(t["descriptions"])

    # Pick 2-4 symptoms, always including at least one domain-indicative one
    num_symptoms = rng.randint(2, min(4, len(t["symptom_pool"])))
    symptoms = rng.sample(t["symptom_pool"], num_symptoms)

    # Pick 1-3 expected actions
    num_actions = rng.randint(1, min(3, len(t["action_pool"])))
    actions = rng.sample(t["action_pool"], num_actions)

    return {
        "case_id": f"{prefix}-{case_num:03d}",
        "description": description,
        "symptoms": symptoms,
        "os_platform": rng.choice(t["os_platforms"]),
        "device_ip": _random_ip(rng),
        "expected_resolution": domain,
        "expected_steps": rng.randint(1, 5),
        "expected_actions": actions,
        "resolution_notes": rng.choice(t["resolution_pool"]),
        "risk_class": rng.choice(t["risk_classes"]),
    }


def generate_dataset(
    total: int = 100,
    seed: int = 42,
) -> list[dict]:
    """
    Generate *total* synthetic cases, distributed evenly across the five
    specialist domains, then prepend all REAL_WORLD_CASES.  Returns a
    shuffled list of case dicts.
    """
    rng = random.Random(seed)
    domains = list(DOMAIN_LABELS.keys())
    per_domain = total // len(domains)
    remainder = total % len(domains)

    cases: list[dict] = []

    # Always include real-world cases first
    cases.extend(REAL_WORLD_CASES)

    # Generate synthetic cases
    for i, domain in enumerate(domains):
        count = per_domain + (1 if i < remainder else 0)
        for j in range(count):
            cases.append(generate_case(domain, len(cases) + 1, rng))

    rng.shuffle(cases)

    # Re-number case_ids sequentially after shuffle
    for idx, case in enumerate(cases, start=1):
        prefix = case["case_id"].split("-")[0]
        case["case_id"] = f"{prefix}-{idx:03d}"

    return cases


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic ZT411 troubleshooting cases")
    parser.add_argument("--count", type=int, default=100,
                        help="Number of SYNTHETIC cases to generate; real-world cases always included (default: 100)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility (default: 42)")
    args = parser.parse_args()

    cases = generate_dataset(total=args.count, seed=args.seed)

    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    out_dir = project_root / "data" / "sample"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "sample_cases.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")

    # Summary
    from collections import Counter
    dist = Counter(c["expected_resolution"] for c in cases)
    rw_count = len(REAL_WORLD_CASES)
    print(f"Wrote {len(cases)} cases ({rw_count} real-world + {len(cases) - rw_count} synthetic) to {out_path}")
    for domain, count in sorted(dist.items()):
        print(f"  {domain}: {count} cases (label={DOMAIN_LABELS.get(domain, '?')})")


if __name__ == "__main__":
    main()
