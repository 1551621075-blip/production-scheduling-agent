"""Human-only review/publication actions, outside the agent tool graph."""
from __future__ import annotations

from .domain import CompiledProblem, Plan, canonical_digest
from .errors import DomainError
from .receiver import LocalReceiver, _digest
from .storage import _canonical


class ActionService:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.receiver = LocalReceiver(self.store)
        self._capability = self.receiver._capability

    def _now(self):
        value = self.service.clock()
        if type(value) is not int or value < 0:
            raise DomainError("clock", "A valid millisecond clock is required", 409)
        return value

    @staticmethod
    def _note(note):
        if type(note) is not str or not note.strip() or len(note) > 2000:
            raise DomainError("validation", "Note must be a nonblank bounded string", 422)

    @staticmethod
    def _confirmed(confirmed):
        if confirmed is not True:
            raise DomainError("confirmation_required", "Explicit confirmation is required", 422)

    @staticmethod
    def _hash(current, expected):
        if type(expected) is not str or expected != current["content_hash"]:
            raise DomainError("recommendation_changed", "Recommendation has changed", 409)

    def _update(self, conn, principal, current, status, payload):
        return self.store.update_object(
            conn, kind=current["kind"], object_id=current["object_id"], principal=principal,
            expected_version=current["version"], status=status, payload=payload,
        )

    def _reserve(self, conn, principal, action_id, request, current):
        # Immutable request reservation, not a cached response used as receiver evidence.
        self.store.record_receipt(conn, principal, action_id, request, {
            "kind": current["kind"], "object_id": current["object_id"], "reserved": True,
        })
        self.store.audit(conn, principal, kind=current["kind"], object_id=current["object_id"], action=request["action"], detail={"action_id": action_id})

    def review(self, principal, recommendation_id, *, action_id, recommendation_hash, decision, note):
        self._note(note)
        if type(decision) is not str or decision not in ("approved", "rejected"):
            raise DomainError("validation", "Decision must be approved or rejected", 422)
        request = {"action": "review", "recommendation_id": recommendation_id,
                   "recommendation_hash": recommendation_hash, "decision": decision, "note": note}
        with self.store.transaction() as conn:
            current = self.store.get_object("recommendation", recommendation_id, principal, conn=conn)
            self.store.require(principal, current["plant_id"], {"reviewer", "admin"}, conn=conn)
            if self.store.receipt(conn, principal, action_id, request) is not None:
                return current
            self._hash(current, recommendation_hash)
            if current["status"] != "pending_review":
                raise DomainError("review_state", "Recommendation is not pending review", 409)
            payload = self.service.validate_recommendation(conn, principal, current)
            payload["review"] = {"decision": decision, "note": note, "reviewer": principal.user_id,
                                 "reviewed_at_ms": self._now(), "recommendation_hash": recommendation_hash}
            result = self._update(conn, principal, current, decision, payload)
            self._reserve(conn, principal, action_id, request, result)
            return result

    def request_publication(self, principal, recommendation_id, *, action_id, recommendation_hash, confirmed):
        self._confirmed(confirmed)
        request = {"action": "request_publication", "recommendation_id": recommendation_id,
                   "recommendation_hash": recommendation_hash, "confirmed": confirmed}
        with self.store.transaction() as conn:
            current = self.store.get_object("recommendation", recommendation_id, principal, conn=conn)
            prior = self.store.receipt(conn, principal, action_id, request)
            if prior is not None:
                return self.store.get_object("publication", prior["object_id"], principal, conn=conn)
            self._hash(current, recommendation_hash)
            if current["status"] != "approved":
                raise DomainError("approval_required", "Approved recommendation required", 409)
            approved = self.service.validate_recommendation(conn, principal, current)
            plan = Plan.model_validate(approved["candidate"])
            problem = CompiledProblem.model_validate(approved["compiled_problem"])
            operation_ids = sorted(op.operation_id for op in problem.operations)
            if not operation_ids:
                raise DomainError("empty_plan", "No remaining operations to publish", 409)
            publication_id = "pub-" + recommendation_id
            existing = conn.execute("SELECT 1 FROM objects WHERE kind='publication' AND object_id=?", (publication_id,)).fetchone()
            if existing:
                result = self.store.get_object("publication", publication_id, principal, conn=conn)
                if result["payload"]["recommendation_hash"] != recommendation_hash:
                    raise DomainError("recommendation_changed", "Publication is already frozen", 409)
                self._reserve(conn, principal, action_id, request, result)
                return result
            self.receiver.check_delivery_intent(self._capability, conn, current["plant_id"], publication_id)
            now = self._now()
            signed = self.receiver.sign_request(self._capability, {
                "protocol_version": 1, "owner": principal.user_id, "plant_id": current["plant_id"],
                "publication_id": publication_id, "recommendation_id": recommendation_id,
                "recommendation_hash": recommendation_hash, "plan_hash": canonical_digest(plan),
                "source_binding": approved["source_binding"], "operation_ids": operation_ids,
                "created_at_ms": now, "expires_at_ms": now + 86_400_000, "synthetic": True,
            })
            result = self.store.create_object(
                conn, kind="publication", object_id=publication_id, principal=principal,
                plant_id=current["plant_id"], status="requested", payload={
                    "recommendation_id": recommendation_id, "recommendation_hash": recommendation_hash,
                    "plan_hash": canonical_digest(plan), "source_binding": approved["source_binding"],
                    "signed_request": signed, "delivery_status": "not_sent", "receiver_status": "none",
                    "execution_status": "none", "receiver_sequence": 0, "receiver_receipt": None,
                    "accepted_operation_ids": [], "execution_result": None, "pending_action": None,
                    "synthetic": True,
                },
            )
            self._reserve(conn, principal, action_id, request, result)
            return result

    def _approved(self, conn, principal, request):
        recommendation = self.store.get_object("recommendation", request["recommendation_id"], principal, conn=conn)
        if recommendation["status"] != "approved" or recommendation["content_hash"] != request["recommendation_hash"] or recommendation["plant_id"] != request["plant_id"]:
            raise DomainError("recommendation_changed", "Frozen approval is no longer current", 409)
        payload = self.service.validate_recommendation(conn, principal, recommendation)
        if payload["source_binding"] != request["source_binding"] or self.service.source_binding(conn, principal, payload["snapshot_id"]) != request["source_binding"]:
            raise DomainError("source_changed", "Current source binding is required", 409)
        problem = CompiledProblem.model_validate(payload["compiled_problem"])
        plan = Plan.model_validate(payload["candidate"])
        if canonical_digest(plan) != request["plan_hash"]:
            raise DomainError("recommendation_changed", "Frozen candidate has changed", 409)
        return problem, plan

    def _intent(self, conn, principal, current, action_id, request):
        payload = current["payload"]
        if request["action"] in ("deliver_local", "advance_receiver"):
            self.receiver.reserve_delivery_intent(self._capability, conn, payload["signed_request"]["payload"])
        payload["delivery_status"] = "unknown"
        payload["pending_action"] = {"action_id": action_id, "action": request["action"],
                                     "minimum_sequence": payload["receiver_sequence"] + 1}
        status = current["status"] if payload["execution_status"] != "none" or payload["receiver_status"] == "cancelled" else "unknown"
        current = self._update(conn, principal, current, status, payload)
        self._reserve(conn, principal, action_id, request, current)
        return current

    def _reduce(self, conn, principal, current, receipt, *, definitive=False):
        latest = self.receiver.verify_receipt(self._capability, conn, principal, current["payload"]["signed_request"], receipt)
        state = latest["payload"]
        payload = current["payload"]
        previous_seq = payload["receiver_sequence"]
        if type(previous_seq) is not int or previous_seq > state["sequence"]:
            raise DomainError("receiver_sequence", "Publisher sequence is not authoritative", 409)
        if previous_seq == state["sequence"] and payload["receiver_receipt"] != latest:
            raise DomainError("receiver_sequence", "Conflicting publisher receipt", 409)
        pending = payload["pending_action"]
        if (pending is not None and pending["action"] == "cancel_publication"
                and state["receiver_status"] != "cancelled" and state["execution_status"] == "none"):
            # A pre-cancel receipt may be newer than a lost publisher ACK. Only
            # authoritative cancellation or already-started execution settles cancel.
            return current
        if (not definitive and pending is not None and state["sequence"] < pending["minimum_sequence"]
                and state["receiver_status"] != "cancelled" and state["execution_status"] == "none"):
            # An older partial receipt does not settle an in-flight advance/cancel.
            return current
        self.receiver.acknowledge_intent(self._capability, conn, payload["signed_request"]["payload"])
        payload.update(
            delivery_status="acknowledged", receiver_status=state["receiver_status"],
            execution_status=state["execution_status"], receiver_sequence=state["sequence"],
            receiver_receipt=latest, accepted_operation_ids=state["accepted_operation_ids"],
            execution_result=state["result"], pending_action=None,
        )
        status = state["receiver_status"] if state["execution_status"] == "none" else state["execution_status"]
        if current["status"] == status and current["content_hash"] == _digest(payload):
            return current
        return self._update(conn, principal, current, status, payload)

    def _known_rejection(self, principal, publication_id, action_id):
        """Settle only a synchronously rejected attempt, never a crashed unknown."""
        with self.store.transaction() as conn:
            current = self.store.get_object("publication", publication_id, principal, conn=conn)
            pending = current["payload"]["pending_action"]
            if pending is None or pending["action_id"] != action_id:
                return
            receipt = self.receiver.latest(self._capability, principal, current["payload"]["signed_request"], conn=conn)
            if receipt is not None:
                self._reduce(conn, principal, current, receipt, definitive=True)

    def _ack(self, principal, publication_id, receipt):
        with self.store.transaction() as conn:
            current = self.store.get_object("publication", publication_id, principal, conn=conn)
            return self._reduce(conn, principal, current, receipt)

    def deliver_local(self, principal, publication_id, *, action_id, fault=None):
        if fault not in (None, "after_receive"):
            raise DomainError("validation", "Unsupported local delivery fault", 422)
        request = {"action": "deliver_local", "publication_id": publication_id, "fault": fault}
        with self.store.transaction() as conn:
            current = self.store.get_object("publication", publication_id, principal, conn=conn)
            request["request_hash"] = _digest(current["payload"]["signed_request"]["payload"])
            if self.store.receipt(conn, principal, action_id, request) is not None:
                return current
            if current["payload"]["delivery_status"] != "not_sent":
                raise DomainError("delivery_state", "Delivery was already attempted; use independent readback", 409)
            self._approved(conn, principal, current["payload"]["signed_request"]["payload"])
            current = self._intent(conn, principal, current, action_id, request)
        receipt = self.receiver.receive(self._capability, principal, current["payload"]["signed_request"], validate=self._approved, now_ms=self._now())
        if fault == "after_receive":
            return self.store.get_object("publication", publication_id, principal)
        return self._ack(principal, publication_id, receipt)

    def advance_receiver(self, principal, publication_id, *, action_id):
        request = {"action": "advance_receiver", "publication_id": publication_id}
        with self.store.transaction() as conn:
            current = self.store.get_object("publication", publication_id, principal, conn=conn)
            request["request_hash"] = _digest(current["payload"]["signed_request"]["payload"])
            if self.store.receipt(conn, principal, action_id, request) is not None:
                return current
            payload = current["payload"]
            if payload["delivery_status"] != "acknowledged" or payload["receiver_status"] != "partial" or payload["execution_status"] != "none":
                raise DomainError("receiver_state", "Only acknowledged partial reception may advance", 409)
            self._approved(conn, principal, payload["signed_request"]["payload"])
            current = self._intent(conn, principal, current, action_id, request)
        try:
            receipt = self.receiver.advance(self._capability, principal, current["payload"]["signed_request"], validate=self._approved, now_ms=self._now())
        except DomainError as exc:
            if exc.code in {"plant_busy", "source_used", "operation_used", "publication_expired", "receiver_state", "source_changed", "source_stale", "source_future", "recommendation_changed", "receiver_validation"}:
                self._known_rejection(principal, publication_id, action_id)
            raise
        return self._ack(principal, publication_id, receipt)

    def readback(self, principal, publication_id, *, action_id):
        request = {"action": "readback", "publication_id": publication_id}
        with self.store.transaction() as conn:
            current = self.store.get_object("publication", publication_id, principal, conn=conn)
            request["request_hash"] = _digest(current["payload"]["signed_request"]["payload"])
            previous = self.store.receipt(conn, principal, action_id, request)
            # Even an idempotent readback consults the receiver, never its cached ACK.
            receipt = self.receiver.latest(self._capability, principal, current["payload"]["signed_request"], conn=conn)
            if receipt is not None:
                current = self._reduce(conn, principal, current, receipt)
            if previous is None:
                self._reserve(conn, principal, action_id, request, current)
            return current

    def cancel_publication(self, principal, publication_id, *, action_id, note):
        self._note(note)
        request = {"action": "cancel_publication", "publication_id": publication_id, "note": note}
        with self.store.transaction() as conn:
            current = self.store.get_object("publication", publication_id, principal, conn=conn)
            request["request_hash"] = _digest(current["payload"]["signed_request"]["payload"])
            if self.store.receipt(conn, principal, action_id, request) is not None:
                if current["payload"]["execution_status"] != "none":
                    raise DomainError("too_late", "Synthetic execution has already started", 409)
                return current
            envelope = current["payload"]["signed_request"]
            body = envelope["payload"]
            cancellation = self.receiver.sign_cancel(self._capability, {
                "protocol_version": 1, "owner": body["owner"], "plant_id": body["plant_id"],
                "publication_id": publication_id, "request_hash": _digest(body),
                "created_at_ms": self._now(), "note": note, "synthetic": True,
            })
            self._intent(conn, principal, current, action_id, request)
        try:
            receipt = self.receiver.cancel(self._capability, principal, envelope, cancellation, now_ms=self._now())
        except DomainError as exc:
            if exc.code == "too_late":
                latest = self.receiver.latest(self._capability, principal, envelope)
                if latest is not None:
                    self._ack(principal, publication_id, latest)
            raise
        return self._ack(principal, publication_id, receipt)

    def execute_synthetic(self, principal, publication_id, *, action_id, confirmed):
        self._confirmed(confirmed)
        request = {"action": "execute_synthetic", "publication_id": publication_id, "confirmed": confirmed}
        with self.store.transaction() as conn:
            current = self.store.get_object("publication", publication_id, principal, conn=conn)
            request["request_hash"] = _digest(current["payload"]["signed_request"]["payload"])
            if self.store.receipt(conn, principal, action_id, request) is not None:
                return current
            envelope = current["payload"]["signed_request"]
            if current["payload"]["delivery_status"] != "acknowledged" or current["payload"]["receiver_status"] != "accepted":
                raise DomainError("receiver_state", "Execution requires acknowledged full reception", 409)
            receipt, problem, plan = self.receiver.admit_execution(
                self._capability, conn, principal, envelope, validate=self._approved, now_ms=self._now(),
            )
            current = self._reduce(conn, principal, current, receipt)
            self._reserve(conn, principal, action_id, request, current)
            execution_id = receipt["payload"]["execution_id"]
        # Deliberately no database transaction across potentially expensive simulation.
        # BaseException leaves durable running: restart/readback must never rerun it.
        from .simulation import simulate
        try:
            result = simulate(problem, plan)
            _canonical(result)
        except Exception:
            result = {"synthetic": True, "status": "failed", "events": [], "error": "simulation_error"}
        with self.store.transaction() as conn:
            current = self.store.get_object("publication", publication_id, principal, conn=conn)
            try:
                self._approved(conn, principal, envelope["payload"])
            except DomainError as exc:
                result = {"synthetic": True, "status": "failed", "events": result.get("events", []),
                          "error": "execution_commit_changed", "cause": exc.code, "simulation_result": result}
            receipt = self.receiver.finish_execution(
                self._capability, conn, principal, envelope, execution_id=execution_id,
                result=result, now_ms=self._now(),
            )
            return self._reduce(conn, principal, current, receipt)
