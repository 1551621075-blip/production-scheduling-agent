"""Strict public scheduling requests; identity and receiver authority are never inputs."""

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .domain import (
    MAX_HORIZON,
    MAX_ITEMS,
    MAX_ORDERS,
    MAX_STOCK,
    Hash,
    Identifier,
    Scenario,
    Snapshot,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_default=True)


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("A nonblank note is required")
    return value


Note = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_nonblank)]
InputField = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^(?:materials\[(?:[0-9]|1[0-9])\]\.initial_stock|orders\[(?:[0-9]|1[0-9])\]\.due_min)$",
    ),
]


class EmptyRequest(StrictModel):
    pass


class ActionRequest(StrictModel):
    action_id: Identifier


class SnapshotRequest(ActionRequest):
    snapshot: Snapshot


class CreateTaskRequest(ActionRequest):
    snapshot_id: Identifier
    scenario: Scenario


class InputAnswer(StrictModel):
    field: InputField
    value: Annotated[int, Field(ge=0, le=MAX_STOCK)]

    @model_validator(mode="after")
    def permitted_range(self):
        if self.field.endswith(".due_min") and self.value > MAX_HORIZON:
            raise ValueError("Due minute exceeds the planning limit")
        return self


class ResumeRequest(ActionRequest):
    answers: list[InputAnswer] = Field(min_length=1, max_length=MAX_ITEMS + MAX_ORDERS)
    note: Note

    @field_validator("answers")
    @classmethod
    def unique_fields(cls, value):
        if len({answer.field for answer in value}) != len(value):
            raise ValueError("Supply each missing field exactly once")
        return value


class ReviewRequest(ActionRequest):
    recommendation_hash: Hash
    decision: Literal["approved", "rejected"]
    note: Note


class ExecuteRequest(ActionRequest):
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit boolean confirmation is required")
        return value


class PublicationRequest(ExecuteRequest):
    recommendation_hash: Hash


class CancelPublicationRequest(ActionRequest):
    note: Note


class ReconcileRequest(ActionRequest):
    call_id: Identifier
    no_result: Literal[True]
    tokens: Annotated[int, Field(ge=0, le=100000)] | None = None
    note: Note

    @field_validator("no_result", mode="before")
    @classmethod
    def explicit_attestation(cls, value):
        if value is not True:
            raise ValueError("Explicit boolean attestation is required")
        return value
