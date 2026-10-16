# ADR 0005: EXCEPTION is a terminal state in the MVP

**Status:** accepted (2026-10-16). Revisit in Phase 3.

**Context.**
- The state machine allows EXCEPTION (damage, lost, address_unresolvable) to interrupt any stage.
- In real operations some exceptions recover, for example a lost parcel that is found or an address that is fixed.
- Recovery paths need rules that the spec does not define.

**Decision.**
- In the MVP, EXCEPTION is terminal, alongside DELIVERED and RETURNED, and has no outgoing transitions.
- The state machine rejects any transition out of EXCEPTION.
- Conservation tests count a shipment as accounted for when it is DELIVERED, RETURNED or EXCEPTION, or legitimately in flight.
- Every EXCEPTION carries a `reason_code` (`damage`, `lost` or `address_unresolvable`).

**Consequences.**
- Exception rates map directly onto loss. Downstream "exception resolution" analytics have nothing to discover until Phase 3.
- Making EXCEPTION recoverable later is a state-machine change, not a schema change: `current_status` is free to move again.
