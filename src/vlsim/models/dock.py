"""Dock doors as a FIFO resource. Plain-data state so it lives in the checkpoint."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class DockQueue:
    """View over a depot's checkpointed dock state {"doors", "busy", "waiting"}."""

    data: dict[str, Any]

    @classmethod
    def new(cls, doors: int) -> DockQueue:
        return cls({"doors": doors, "busy": 0, "waiting": []})

    @property
    def busy(self) -> int:
        busy: int = self.data["busy"]
        return busy

    @property
    def waiting(self) -> list[Any]:
        waiting: list[Any] = self.data["waiting"]
        return waiting

    def arrive(self, job: Any) -> bool:
        """True if the job gets a door now; otherwise it joins the back of the queue."""
        if self.data["busy"] < self.data["doors"]:
            self.data["busy"] += 1
            return True
        self.data["waiting"].append(job)
        return False

    def release(self) -> Any | None:
        """A door frees up; returns the next waiting job (which takes the door) or None."""
        if self.data["busy"] <= 0:
            raise RuntimeError("release without a busy door")
        if self.data["waiting"]:
            return self.data["waiting"].pop(0)
        self.data["busy"] -= 1
        return None
