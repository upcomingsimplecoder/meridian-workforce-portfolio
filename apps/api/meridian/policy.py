"""Versioned policy definitions. Changing permissions requires a new sealed version."""

import hashlib
import json
from typing import Any

POLICY_ID = "authz-v5"

CAPABILITIES_V1: dict[str, set[str]] = {
    "hr": {
        "people.read",
        "people.pay",
        "people.export",
        "ingestion.write",
        "planning.write",
        "comp.manage",
        "reviews.manage",
        "audit.read",
    },
    "finance": {
        "people.read",
        "people.pay",
        "people.export",
        "planning.write",
        "comp.read",
        "audit.read",
    },
    "manager": {"people.read", "people.pay", "people.export", "comp.propose", "reviews.write"},
    "approver": {"people.read", "people.pay", "comp.approve", "reviews.approve"},
    "executive": {"analytics.read"},
    "auditor": {"audit.read"},
    "steward": {"people.read", "ingestion.write"},
}

# Keep the sealed historical definition byte-for-byte reproducible. Finance read access
# never authorizes a signature; new write/export/evidence capabilities are explicit.
CAPABILITIES_V2 = {role: set(values) for role, values in CAPABILITIES_V1.items()}
CAPABILITIES_V2["hr"].update({"comp.hr.signoff", "comp.export", "comp.evidence"})
CAPABILITIES_V2["finance"].update({"comp.finance.signoff", "comp.export", "comp.evidence"})
CAPABILITIES_V2["auditor"].add("comp.evidence")
POLICY_V2_DEFINITION = {role: sorted(values) for role, values in CAPABILITIES_V2.items()}
CAPABILITIES = {role: set(values) for role, values in CAPABILITIES_V2.items()}
for role in ("hr", "finance"):
    CAPABILITIES[role].update({"analytics.full", "analytics.manage"})
CAPABILITIES["auditor"].add("analytics.evidence")
POLICY_V3_DEFINITION = {role: sorted(values) for role, values in CAPABILITIES.items()}
for role in ("hr", "finance"):
    CAPABILITIES[role].update({"replay.full", "replay.evidence", "replay.verify", "replay.export"})
CAPABILITIES["auditor"].update({"replay.evidence", "replay.verify", "replay.export"})
POLICY_V1_DEFINITION = {role: sorted(values) for role, values in CAPABILITIES_V1.items()}


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


POLICY_V4_DEFINITION = {role: sorted(values) for role, values in CAPABILITIES.items()}
for role in ("hr", "finance", "auditor"):
    CAPABILITIES[role].add("operations.read")

POLICY_DEFINITION = {role: sorted(values) for role, values in CAPABILITIES.items()}
