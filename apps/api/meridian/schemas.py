from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(StrictModel):
    status: Literal["ok", "ready"]
    service: str


class Problem(StrictModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str


class Grant(StrictModel):
    role: str
    scope: str


class Me(StrictModel):
    id: str
    display_name: str
    grants: list[Grant]
    capabilities: list[str]
    csrf_token: str
    expires_at: datetime
    policy_version: str = "authz-v4"
    synthetic_only: bool = True


class Overview(StrictModel):
    as_of: datetime
    source: str
    demo_accounts: int
    pending_events: int
    processed_events: int
    worker_last_seen: datetime | None
    seed_version: str
    ai_enabled: bool = False
    phase: str


class IdentityEventPayload(StrictModel):
    entity_type: Literal["principal"]
    entity_id: str


class SourceEventPayload(StrictModel):
    entity_type: Literal["source_batch"]
    entity_id: str


class ScenarioEventPayload(StrictModel):
    entity_type: Literal["scenario"]
    entity_id: UUID
    revision_id: UUID
    source_batch_id: UUID


class CompProposalEventPayload(StrictModel):
    entity_type: Literal["comp_proposal"]
    entity_id: UUID
    cycle_id: UUID
    revision_id: UUID
    source_batch_id: UUID


class CompCycleEventPayload(StrictModel):
    entity_type: Literal["comp_cycle"]
    entity_id: UUID
    lock_id: UUID
    source_batch_id: UUID


class TalentCycleEventPayload(StrictModel):
    entity_type: Literal["talent_cycle"]
    entity_id: UUID
    lock_id: UUID
    source_batch_id: UUID


class AnalyticsEventPayload(StrictModel):
    entity_type: Literal["analytics_run"]
    entity_id: UUID
    source_batch_id: UUID
    inputs_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class ReplayEventPayload(StrictModel):
    entity_type: Literal["replay_capsule"]
    entity_id: UUID
    attempt_id: UUID
    capsule_sha256: str = Field(pattern="^[0-9a-f]{64}$")


class EventEnvelope(StrictModel):
    event_id: UUID
    event_type: Literal[
        "identity.login.v1",
        "identity.logout.v1",
        "source_batch.received.v1",
        "source_batch.published.v1",
        "scenario.revision_created.v1",
        "scenario.approved.v1",
        "comp.proposal_revision_created.v1",
        "comp.proposal_submitted.v1",
        "comp.proposal_returned.v1",
        "comp.cycle_locked.v1",
        "comp.export_created.v1",
        "talent.review_locked.v1",
        "analytics.run_requested.v1",
        "governance.replay_recorded.v1",
    ]
    schema_version: Literal[1]
    aggregate_id: str
    aggregate_version: int
    source_batch_id: UUID | None = None
    occurred_at: datetime
    correlation_id: UUID
    payload: (
        IdentityEventPayload
        | SourceEventPayload
        | ScenarioEventPayload
        | CompProposalEventPayload
        | CompCycleEventPayload
        | TalentCycleEventPayload
        | AnalyticsEventPayload
        | ReplayEventPayload
    )

    @model_validator(mode="after")
    def validate_binding(self) -> "EventEnvelope":
        kind = (
            "principal"
            if self.event_type.startswith("identity.")
            else "scenario"
            if self.event_type.startswith("scenario.")
            else "comp_proposal"
            if self.event_type.startswith("comp.proposal_")
            else "comp_cycle"
            if self.event_type.startswith("comp.")
            else "talent_cycle"
            if self.event_type.startswith("talent.")
            else "analytics_run"
            if self.event_type.startswith("analytics.")
            else "replay_capsule"
            if self.event_type.startswith("governance.")
            else "source_batch"
        )
        if self.payload.entity_type != kind or self.aggregate_id != str(self.payload.entity_id):
            raise ValueError("Event aggregate and payload binding mismatch")
        if kind == "source_batch" and str(self.source_batch_id) != self.aggregate_id:
            raise ValueError("Source event requires its exact source batch provenance")
        if (
            isinstance(
                self.payload,
                (
                    ScenarioEventPayload,
                    CompProposalEventPayload,
                    CompCycleEventPayload,
                    TalentCycleEventPayload,
                    AnalyticsEventPayload,
                ),
            )
            and self.source_batch_id != self.payload.source_batch_id
        ):
            raise ValueError("Scenario event requires its pinned source batch provenance")
        return self
