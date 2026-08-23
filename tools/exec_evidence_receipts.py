#!/usr/bin/env python3
"""Executable reference behavior for ``bus.exec.evidence.v1`` receipts.

This is deliberately transport-free: a producer validates an envelope before
returning it and an in-memory consumer demonstrates the receipt/event dedupe
rules. Redis publishers and real consumers must still implement this contract
at their own edges; this module does not claim to exercise Redis delivery.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "bus.exec.evidence.v1.json"
SUPPORTED_MAJOR = "1"

ConsumeStatus = Literal["accepted", "duplicate_event", "duplicate_receipt"]


class ReceiptValidationError(ValueError):
    """The receipt is not an admissible bus.exec.evidence.v1 envelope."""


class UnsupportedReceiptVersion(ReceiptValidationError):
    """The consumer deliberately has no compatibility behavior for this major."""


class ReceiptIdentityConflict(ReceiptValidationError):
    """A receipt/event id was reused for a different execution attempt."""


@dataclass(frozen=True)
class Receipt:
    """Validated identity and completion view retained by the reference consumer."""

    receipt_id: str
    event_id: str
    bead_id: str
    execution_id: str
    attempt_id: str
    attempt_number: int
    completion_state: str

    @property
    def is_runtime_complete(self) -> bool:
        """Only the explicit terminal state is completion evidence.

        A started/background/source_accepted/static_contract/partial receipt is
        useful progress telemetry, but never becomes runtime evidence by virtue
        of being stored or redelivered.
        """

        return self.completion_state == "runtime_complete"


@dataclass(frozen=True)
class ConsumeResult:
    status: ConsumeStatus
    receipt: Receipt


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_receipt(envelope: dict[str, Any]) -> Receipt:
    """Validate a v1 receipt and return its normalized identity view.

    JSON Schema enforces the public shape. The envelope/data event-id equality
    is an executable cross-field invariant that JSON Schema cannot express with
    a dynamic ``const``. Unknown major versions fail closed rather than being
    coerced into v1 semantics.
    """

    if not isinstance(envelope, dict):
        raise ReceiptValidationError("receipt envelope must be an object")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise ReceiptValidationError("receipt envelope data must be an object")
    if data.get("schema_version") != SUPPORTED_MAJOR:
        raise UnsupportedReceiptVersion(
            f"unsupported bus.exec.evidence major {data.get('schema_version')!r}; "
            f"supported major is {SUPPORTED_MAJOR}"
        )
    if envelope.get("id") != data.get("event_id"):
        raise ReceiptValidationError("envelope id must equal data.event_id")
    try:
        _validator().validate(envelope)
    except ValidationError as exc:
        raise ReceiptValidationError(exc.message) from exc

    return Receipt(
        receipt_id=data["receipt_id"],
        event_id=data["event_id"],
        bead_id=data["bead_id"],
        execution_id=data["execution_id"],
        attempt_id=data["attempt_id"],
        attempt_number=data["attempt_number"],
        completion_state=data["completion_state"],
    )


class ReferenceReceiptProducer:
    """Reference producer edge that accepts only valid, supported receipts."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, envelope: dict[str, Any]) -> Receipt:
        """Validate, preserve the supplied attempt, and retain an in-memory copy."""

        receipt = validate_receipt(envelope)
        self.published.append(copy.deepcopy(envelope))
        return receipt


class ReferenceReceiptConsumer:
    """In-memory idempotency reference keyed by event and stable receipt ids."""

    def __init__(self) -> None:
        self._by_event_id: dict[str, Receipt] = {}
        self._by_receipt_id: dict[str, Receipt] = {}

    def consume(self, envelope: dict[str, Any]) -> ConsumeResult:
        """Accept one attempt once; classify later deliveries without overwriting it."""

        receipt = validate_receipt(envelope)
        seen_event = self._by_event_id.get(receipt.event_id)
        if seen_event is not None:
            if seen_event != receipt:
                raise ReceiptIdentityConflict("event_id was reused for another receipt")
            return ConsumeResult("duplicate_event", seen_event)

        seen_receipt = self._by_receipt_id.get(receipt.receipt_id)
        if seen_receipt is not None:
            if seen_receipt.execution_id != receipt.execution_id or seen_receipt.attempt_id != receipt.attempt_id:
                raise ReceiptIdentityConflict("receipt_id was reused for another execution attempt")
            self._by_event_id[receipt.event_id] = seen_receipt
            return ConsumeResult("duplicate_receipt", seen_receipt)

        self._by_event_id[receipt.event_id] = receipt
        self._by_receipt_id[receipt.receipt_id] = receipt
        return ConsumeResult("accepted", receipt)

    @property
    def accepted_receipt_count(self) -> int:
        """Number of distinct attempt receipts retained after duplicate delivery."""

        return len(self._by_receipt_id)
