"""
Structured model decision contract for UnattendedBot8300.

Defines the strict validated decision format produced by the Hermes/model layer.
All model outputs must conform to this schema before they reach persistence or side effects.
"""

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, Any
import json

from pydantic import BaseModel, Field, field_validator


class ActionType(str, Enum):
    """Types of actions the agent can propose."""
    POST = "POST"
    REPLY = "REPLY"
    MEMORY = "MEMORY"
    NOTHING = "NOTHING"
    # Future placeholder - not yet implemented
    EXPLORE = "EXPLORE"


class DecisionModel(BaseModel):
    """
    Strict validated structure for model decisions.
    
    This is the contract between the Hermes/Model layer and the deterministic
    Python tooling layer. The model produces this structure, which is then
    validated and processed.
    """
    
    action_type: ActionType = Field(..., description="The action type decided by the model")
    
    # Content for POST/REPLY actions
    message: Optional[str] = Field(None, max_length=10000, description="Content to post or reply with")
    
    # Target object ID for REPLY actions
    target_object_id: Optional[str] = Field(None, description="Facebook object ID to target (comment/post)")
    
    # Memory action fields
    memory_kind: Optional[str] = Field(None, description="Kind of memory to store (lore, event, etc.)")
    memory_content: Optional[str] = Field(None, description="Content to store as memory")
    memory_tags: Optional[list[str]] = Field(default_factory=list, description="Tags for memory")
    
    # Decision metadata (not hidden chain-of-thought, just summary)
    decision_summary: str = Field(..., description="Short, human-readable summary of the decision reason")
    
    # Optional memory candidates for future reference
    suggested_memory_ids: Optional[list[int]] = Field(default=None, description="Suggested memory IDs to reference")
    
    @field_validator('message')
    @classmethod
    def validate_message(cls, v: Optional[str], info) -> Optional[str]:
        """Validate message content for POST/REPLY actions."""
        if v is None:
            return v
        if len(v.strip()) == 0:
            raise ValueError("Message cannot be empty if provided")
        return v.strip()
    
    @field_validator('decision_summary')
    @classmethod
    def validate_summary(cls, v: str) -> str:
        """Validate decision summary is meaningful."""
        if not v or not v.strip():
            raise ValueError("decision_summary is required and must not be empty")
        return v.strip()
    
    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
    }
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for persistence."""
        result = {
            "action_type": self.action_type.value,
            "decision_summary": self.decision_summary,
        }
        if self.message is not None:
            result["message"] = self.message
        if self.target_object_id is not None:
            result["target_object_id"] = self.target_object_id
        if self.memory_kind is not None:
            result["memory_kind"] = self.memory_kind
        if self.memory_content is not None:
            result["memory_content"] = self.memory_content
        if self.memory_tags:
            result["memory_tags"] = self.memory_tags
        if self.suggested_memory_ids is not None:
            result["suggested_memory_ids"] = self.suggested_memory_ids
        return result
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionModel":
        """Create from dictionary, with validation."""
        # Map field names from raw API format
        mapped = {}
        for key, value in data.items():
            if key == "target_object_id" or key == "targetId":
                mapped["target_object_id"] = value
            elif key == "memory_kind" or key == "memoryKind":
                mapped["memory_kind"] = value
            elif key == "memory_content" or key == "memoryContent":
                mapped["memory_content"] = value
            elif key == "memory_tags" or key == "memoryTags":
                mapped["memory_tags"] = value
            else:
                mapped[key] = value
        return cls(**mapped)


def validate_decision(data: dict[str, Any]) -> tuple[DecisionModel | None, str | None]:
    """
    Validate a raw model decision dict.
    
    Returns:
        (DecisionModel, None) if valid
        (None, error_message) if invalid
    """
    try:
        decision = DecisionModel.from_dict(data)
        return decision, None
    except Exception as e:
        return None, f"Invalid decision format: {e}"


def decision_to_json(decision: DecisionModel) -> str:
    """Serialize decision to JSON string."""
    return json.dumps(decision.to_dict())


def json_to_decision(json_str: str) -> DecisionModel:
    """Deserialize decision from JSON string."""
    data = json.loads(json_str)
    return DecisionModel.from_dict(data)


# === Convenience constructors ===

def make_post_decision(message: str, reason: str) -> DecisionModel:
    """Create a POST decision."""
    return DecisionModel(
        action_type=ActionType.POST,
        message=message,
        decision_summary=reason,
    )


def make_reply_decision(target_object_id: str, message: str, reason: str) -> DecisionModel:
    """Create a REPLY decision."""
    return DecisionModel(
        action_type=ActionType.REPLY,
        target_object_id=target_object_id,
        message=message,
        decision_summary=reason,
    )


def make_memory_decision(kind: str, content: str, reason: str, 
                         tags: Optional[list[str]] = None) -> DecisionModel:
    """Create a MEMORY decision."""
    return DecisionModel(
        action_type=ActionType.MEMORY,
        memory_kind=kind,
        memory_content=content,
        memory_tags=tags or [],
        decision_summary=reason,
    )


def make_nothing_decision(reason: str) -> DecisionModel:
    """Create a NOTHING decision (valid when agent chooses no action)."""
    return DecisionModel(
        action_type=ActionType.NOTHING,
        decision_summary=reason,
    )