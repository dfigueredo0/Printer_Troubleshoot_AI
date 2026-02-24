"""
Owns: CUPS pipeline + filters + backend + queue.
Inspect CUPS queues, job states, lpstat, lpinfo, lpoptions, device URIs (IPP, LPD, socket).
Validate PPD/driver choice (Zebra vs generic), filter errors, permissions, SELinux/AppArmor issues.
Low-risk fixes: restart CUPS, re-enable printer, cancel single job (confirm), run CUPS test print.
Evidence: CUPS logs (error_log), job attributes, device URI + resolution output.
"""