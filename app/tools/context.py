"""Runtime context passed to agent tools. The user id comes from here, never from the model."""

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    user_id: uuid.UUID
    timezone: str
    context: dict[str, Any] = field(default_factory=dict)  # preferences + notes from retrieval
