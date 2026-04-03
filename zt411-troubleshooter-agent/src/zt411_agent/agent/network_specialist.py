from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from .tools import (
    ping,
    tcp_connect,
    dns_lookup,
    arp_lookup,
    oui_vendor,
    snmp_get,
    ZT411OIDs,
)
from ..state import AgentState, ActionStatus, RiskLevel

logger = logging.getLogger(__name__)


class NetworkSpecialist(Specialist):
    """
    Utility scoring logic
    ---------------------
    Base score rises when:
    * Reachability is unknown → we must probe before anything else can be ruled in/out.
    * Symptoms mention network keywords (ip, ping, timeout …).
    * Network is known unreachable → still high priority; probing may reveal why.
    * Ports are partially open/closed → worth digging further.

    Score drops when:
    * Reachability already confirmed and all expected ports are open → done here.
    * We have already visited this specialist this session → diminishing returns.
    """

    name = "network_specialist"

    # Ports to verify for a ZT411 (RAW, LPD, IPP, HTTP, HTTPS)
    EXPECTED_PORTS = [9100, 515, 631, 80, 443]

    def can_handle(self, state: AgentState) -> float:  # noqa: D401
        score = 0.0

        # --- High-value signals ---

        # 1. We don't know yet if the printer is reachable — top priority
        if state.network_unknown:
            score += 0.6

        # 2. User description / symptoms mention network-layer keywords
        if state.has_network_symptoms:
            score += 0.25

        # 3. Network is known unreachable — still very relevant
        if state.network.reachable is False:
            score += 0.45

        # 4. Some ports unchecked on an otherwise reachable printer
        if state.network.reachable is True:
            unchecked = [p for p in self.EXPECTED_PORTS if p not in state.network.port_open]
            if unchecked:
                score += 0.2 * (len(unchecked) / len(self.EXPECTED_PORTS))

        # 5. DNS info missing even though we have an IP
        if state.device.ip != "unknown" and not state.network.dns_resolved:
            score += 0.1

        # --- Diminishing returns ---

        # Already visited: cut score significantly to let other specialists get a turn
        if self.name in state.visited_specialists:
            score *= 0.4

        # All expected ports open and reachable: network is sorted
        if state.network.reachable is True and all(
            state.network.port_open.get(p) for p in self.EXPECTED_PORTS
        ):
            score = min(score, 0.05)

        return min(score, 1.0)

    def act(self, state: AgentState) -> dict[str, Any]:
        """Execute network diagnostics and update state."""
        logger.info("NetworkSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []
        ip = state.device.ip

        # ------------------------------------------------------------------
        # 1. Reachability probe (ping / ICMP)
        # ------------------------------------------------------------------
        if state.network.reachable is None and ip != "unknown":
            r = ping(ip)
            if r.success and r.output:
                reachable = bool(r.output.get("reachable", False))
                latency = r.output.get("latency_ms")
                state.network.reachable = reachable
                state.network.latency_ms = latency
                content = (
                    f"ICMP probe {ip}: reachable={reachable}"
                    + (f" latency={latency:.1f}ms" if latency is not None else "")
                )
            else:
                content = f"ping {ip} error: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="ping", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"ping {ip}")

        # ------------------------------------------------------------------
        # 2. Port probes (9100/515/631/80/443)
        # ------------------------------------------------------------------
        if ip != "unknown":
            unchecked = [p for p in self.EXPECTED_PORTS if p not in state.network.port_open]
            for port in unchecked:
                r = tcp_connect(ip, port)
                if r.success and r.output:
                    is_open = bool(r.output.get("open", False))
                    state.network.port_open[port] = is_open
                    content = f"TCP {ip}:{port} open={is_open}"
                else:
                    state.network.port_open[port] = False
                    content = f"TCP {ip}:{port} probe error: {r.error}"
                ev = state.add_evidence(
                    specialist=self.name, source=f"port_probe_{port}", content=content
                )
                evidence_items.append(ev.evidence_id)
            if unchecked:
                actions_taken.append(f"port probes {unchecked}")

        # ------------------------------------------------------------------
        # 3. DNS resolution check
        # ------------------------------------------------------------------
        if state.device.hostname != "unknown" and not state.network.dns_resolved:
            r = dns_lookup(state.device.hostname)
            if r.success and r.output:
                resolved = bool(r.output.get("resolved", False))
                resolved_ip = r.output.get("ip", "")
                state.network.dns_resolved = resolved
                if resolved_ip:
                    state.network.dns_ip = resolved_ip
                content = (
                    f"DNS lookup '{state.device.hostname}': resolved={resolved} ip='{resolved_ip}'"
                )
            else:
                content = f"DNS lookup error: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="dns", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"dns lookup {state.device.hostname}")

        # ------------------------------------------------------------------
        # 4. ARP table lookup — confirm MAC and rule out IP conflict
        # ------------------------------------------------------------------
        if ip != "unknown" and not state.network.mac_oui:
            r = arp_lookup(ip)
            if r.success and r.output:
                found = bool(r.output.get("found", False))
                mac = r.output.get("mac", "")
                if found and mac:
                    state.network.mac_oui = mac
                    # Cross-check with state.device.mac if already known
                    if state.device.mac not in ("unknown", "", mac):
                        content = (
                            f"ARP {ip}: mac={mac} — MISMATCH with expected {state.device.mac}"
                        )
                    else:
                        if state.device.mac in ("unknown", ""):
                            state.device.mac = mac
                        content = f"ARP {ip}: mac={mac}"
                else:
                    content = f"ARP {ip}: not in local cache (device may be on different subnet)"
            else:
                content = f"ARP lookup error: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="arp", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"arp lookup {ip}")

        # ------------------------------------------------------------------
        # 5. MAC OUI vendor check (only if we have a MAC)
        # ------------------------------------------------------------------
        mac_to_check = state.network.mac_oui or state.device.mac
        if mac_to_check and mac_to_check not in ("unknown", ""):
            r = oui_vendor(mac_to_check)
            if r.success and r.output:
                vendor = r.output.get("vendor", "unknown")
                oui = r.output.get("oui", "")
                content = f"OUI lookup {oui}: vendor='{vendor}'"
                if vendor.lower() not in ("unknown", "") and "zebra" not in vendor.lower():
                    content += " — WARNING: vendor does not match Zebra; IP may point to wrong device"
            else:
                content = f"OUI lookup error: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="oui_check", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("mac oui vendor check")

        # ------------------------------------------------------------------
        # 6. SNMP sysDescr identity check — confirm we're talking to the right device
        # ------------------------------------------------------------------
        if ip != "unknown" and state.network.reachable is True and not state.network.snmp_sys_descr:
            r = snmp_get(ip, ZT411OIDs.SYS_DESCR)
            if r.success and r.output:
                descr = str(r.output.get("value", "")).strip()
                state.network.snmp_sys_descr = descr
                is_zebra = "zebra" in descr.lower() or "zt" in descr.lower()
                content = f"SNMP sysDescr '{ip}': '{descr}' is_zebra={is_zebra}"
                if not is_zebra and descr:
                    content += " — WARNING: sysDescr doesn't mention Zebra; verify IP"
            else:
                content = f"SNMP sysDescr query failed: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="snmp_identity", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp identity check")

        state.log_action(
            specialist=self.name,
            action="; ".join(actions_taken) or "no-op (nothing to probe)",
            risk=RiskLevel.SAFE,
            status=ActionStatus.EXECUTED,
            result=f"collected {len(evidence_items)} evidence items",
        )

        return {
            "evidence": evidence_items,
            "actions_taken": actions_taken,
            "next_state": state,
        }
