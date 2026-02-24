"""
Owns: everything from app → Windows print subsystem → driver → port monitor.
Enumerate printers/queues, status, jobs, stuck jobs, error states (pywin32 / PowerShell).
Validate driver name/version, isolation mode, package type, conflicts (e.g., “Type 3” vs “Type 4”), recent updates.
Port config sanity: Standard TCP/IP port, WSD, USB, LPR/RAW, SNMP status on port.
Low-risk fixes: restart spooler, pause/resume queue, re-enable offline, clear single job (with confirmation rules), reprint test page.
Evidence: spooler state, queue/job snapshots, driver metadata, event log snippets.
"""