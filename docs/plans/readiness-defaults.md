# Readiness defaults and managed delivery

Fast route; Markdown only. One independent PR into dev. User approved useful defaults and a supervised readiness publisher; no live deployment or pool activation.

Default Foundry/runtime activity waiting on, retain bounded waits (5 seconds/8 waiters), and use 16 web threads. Enable readiness hints when existing Cloud URL/token are present, preserving explicit rollback overrides and validation. Pool target zero and idle stopping remain unchanged. The publisher already defaults to one-second watch cadence.

Define the separate readiness publisher in staging Railway IaC, preserving the three existing services and volume. Reference existing credentials; do not duplicate secrets. Plan-only inspection must show one added publisher and no unrelated changes. A staging-promotion workflow plans and applies the reviewed infrastructure artifact; a staging-scoped GitHub credential is the only new operational input. No infrastructure is applied by this task.

Acceptance: defaults and explicit opt-outs tested, absent/partial Cloud credentials remain safe, activity fallback tests pass, publisher survives transient delivery failure, full repository validation and independent simplicity/correctness review pass. Run Railway config plan, actionlint, focused pytest and scripts/validate.py. Rollback through existing flag overrides and a reviewed revert; do not remove durable hint rows. Runtime image rollout and initial CI credential setup remain deployment prerequisites, not speed guarantees.

Delivery ends at PR readiness; no recurring monitor requested. Retain this worktree for the owner after handoff.
