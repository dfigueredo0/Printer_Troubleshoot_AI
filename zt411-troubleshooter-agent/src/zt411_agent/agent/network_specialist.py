from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
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

    # Ports we always want to verify for a ZT411
    EXPECTED_PORTS = [9100, 515, 631] # RAW, LPD, IPP (Maybe change to be configurable in the future?)

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
        """
        Execute network diagnostics and update state.
        """
        
        #TODO:  replace each section with real tool calls (socket connect, subprocess ping, SNMP sysDescr query, etc.).
        logger.info("NetworkSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []

        # ------------------------------------------------------------------
        # 1. Reachability probe (ping / ICMP)
        # ------------------------------------------------------------------
        if state.network.reachable is None and state.device.ip != "unknown":
            # TODO: replace with real ping tool call
            # result = tools.ping(state.device.ip)
            # state.network.reachable = result.success
            # state.network.latency_ms = result.latency_ms
            placeholder = f"ICMP probe to {state.device.ip} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="ping",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"ping {state.device.ip}")

        # ------------------------------------------------------------------
        # 2. Port probes for RAW/LPD/IPP
        # ------------------------------------------------------------------
        if state.network.reachable is True:
            unchecked = [p for p in self.EXPECTED_PORTS if p not in state.network.port_open]
            for port in unchecked:
                # TODO: replace with real socket probe
                # open_ = tools.tcp_connect(state.device.ip, port)
                # state.network.port_open[port] = open_
                placeholder = f"TCP probe {state.device.ip}:{port} — not yet implemented"
                ev = state.add_evidence(
                    specialist=self.name,
                    source=f"port_probe_{port}",
                    content=placeholder,
                )
                evidence_items.append(ev.evidence_id)
            actions_taken.append(f"port probes {unchecked}")

        # ------------------------------------------------------------------
        # 3. DNS resolution check
        # ------------------------------------------------------------------
        if state.device.hostname != "unknown" and not state.network.dns_resolved:
            # TODO: real DNS lookup
            placeholder = f"DNS lookup for {state.device.hostname} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="dns",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"dns lookup {state.device.hostname}")

        # ------------------------------------------------------------------
        # 4. Identity check — wrong device at IP?
        # ------------------------------------------------------------------
        if state.network.reachable is True and not state.network.snmp_sys_descr:
            # TODO: SNMP sysDescr + HTTP banner check
            placeholder = f"SNMP sysDescr on {state.device.ip} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="snmp_identity",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp identity check")

        entry = state.log_action(
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