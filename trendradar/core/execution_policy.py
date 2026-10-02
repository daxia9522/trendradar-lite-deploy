"""Pure analyze/push eligibility; successful execution is recorded by callers."""

from dataclasses import dataclass
from typing import Mapping, Optional


FORCE_RUN_ENV = "TRENDRADAR_FORCE_RUN"


def is_manual_force_run(environ: Mapping[str, str]) -> bool:
    """Interpret only the existing explicit CLI/workflow force markers."""
    return (
        environ.get(FORCE_RUN_ENV) == "1"
        or environ.get("WORKFLOW_EVENT_NAME") == "workflow_dispatch"
        or environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    )


@dataclass(frozen=True)
class ExecutionPolicy:
    """A decision over explicit state, with no scheduler/storage side effects."""

    scheduled: bool
    once: bool
    period_key: Optional[str]
    manual_force: bool = False

    @property
    def check_history(self) -> bool:
        # Do not touch execution storage for an inactive, non-forced action.
        return bool((self.scheduled or self.manual_force) and self.once and self.period_key)

    def allows(self, already_executed: bool = False) -> bool:
        return bool(
            self.manual_force
            or (self.scheduled and not (self.once and self.period_key and already_executed))
        )
