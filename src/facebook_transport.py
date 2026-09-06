"""
Facebook transport abstraction layer for UnattendedBot8300.

Provides a deterministic interface so the agent can switch between
transport backends without changing the model-facing observation/
context pipeline:

    model decision
        -> validation
        -> safety + rate limits
        -> proposed action queue
        -> facebook_transport (selected by FACEBOOK_TRANSPORT)
            |-> camoufox_ui  (active, experimental browser UI)
            |-> graph_api    (dormant, official Meta Graph API)

The transport is responsible ONLY for:
  - READING Facebook state (observations) for the camoufox_ui transport
  - EXECUTING approved writes via deterministic publish/reply calls

The model never receives browser-control privileges.  The model decides
WHAT to do; deterministic Python controls HOW an approved action is
performed.
"""

import os
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

from .config import Config
from .fb_observations import ObservationResult


# ── Transport exceptions ──────────────────────────────────────────────

class TransportError(Exception):
    """Base exception for transport-level failures."""


class NotLoggedInError(TransportError):
    """Facebook login is required but the browser session is not authenticated."""


class PageNotFoundError(TransportError):
    """The target Facebook Page could not be found (URL 404, wrong page, etc.)."""


class SelectorError(TransportError):
    """A UI element selector did not match — Facebook layout has changed."""


class CheckpointError(TransportError):
    """Facebook presented a security checkpoint / challenge that needs human action."""


class NavigationError(TransportError):
    """Navigation to a URL failed or timed out."""


class TransportUnavailableError(TransportError):
    """The requested transport is not installed or not available."""


@dataclass
class TransportCapabilities:
    """Static description of what a transport can do (for introspection/tests)."""
    name: str
    transport_type: str  # "graph_api" | "camoufox_ui"
    read_observations: bool = False
    can_post: bool = False
    can_reply: bool = False
    headless: bool = False
    persistent_profile: bool = False


# ── Abstract transport interface ────────────────────────────────────────

class FacebookTransport(ABC):
    """Abstract base for all Facebook transports.

    Implementations must be transport-agnostic from the wake orchestrator's
    perspective: they receive a Config and return the same
    ``ObservationResult`` / write primitives.
    """

    @abstractmethod
    def capabilities(self) -> TransportCapabilities:
        """Return static capability metadata."""

    @abstractmethod
    def observe(self) -> ObservationResult:
        """Read-only observation cycle: posts + comments on the Page.

        Returns an ObservationResult compatible with the existing
        fb_observations pipeline.
        """

    @abstractmethod
    def publish_text_status(self, message: str) -> str:
        """Publish a text status to the Page.

        **NEVER called automatically.** Only callable through:
        model decision -> validation -> safety -> rate-limit -> approval -> executor.
        """

    @abstractmethod
    def reply_to_comment(self, target_comment_id: str, message: str) -> str:
        """Reply to a Page-post comment.

        **NEVER called automatically.** Only callable through:
        model decision -> validation -> safety -> rate-limit -> approval -> executor.
        """

    # ── helpers shared by all transports ──

    def close(self) -> None:
        """Release any held browser / network resources."""
        pass


# ── Transport factory ───────────────────────────────────────────────────

_TRANSPORT_REGISTRY: dict[str, type[FacebookTransport]] = {}


def register_transport(name: str):
    """Class decorator / registration helper for transports."""

    def decorator(cls: type[FacebookTransport]) -> type[FacebookTransport]:
        _TRANSPORT_REGISTRY[name] = cls
        return cls

    return decorator


def get_transport(config: Config) -> FacebookTransport:
    """Instantiate the Facebook transport selected by ``FACEBOOK_TRANSPORT``.

    Supported values:
        graph_api   — official Meta Graph API client
        camoufox_ui — Camoufox browser UI transport
    """
    name = (config.facebook_transport or "").strip().lower()

    if name == "graph_api":
        from .fb_client import FacebookClient as _GraphClient

        class _GraphAPITransport(FacebookTransport):
            def __init__(self, cfg: Config):
                self._client = _GraphClient(cfg)
                self._config = cfg

            def capabilities(self) -> TransportCapabilities:
                return TransportCapabilities(
                    name="graph_api",
                    transport_type="graph_api",
                    read_observations=True,
                    can_post=True,
                    can_reply=True,
                    headless=False,
                    persistent_profile=False,
                )

            def observe(self) -> ObservationResult:
                from .fb_observations import (
                    ObservationCollector, PostObservation, CommentObservation,
                )
                try:
                    import sqlite3
                    from .storage import Storage
                    storage = Storage(self._config)
                    conn = storage.connect()
                    collector = ObservationCollector(conn)
                    try:
                        page_info = self._client.get_page_info()
                        posts_resp = self._client.get_my_posts(limit=10)
                        from .fb_client import extract_posts_from_response, extract_comments_from_response

                        posts_raw = extract_posts_from_response(posts_resp)
                        comments_raw: list[dict] = []
                        for post in posts_raw[:3]:
                            comments_resp = self._client.get_post_comments(post["fb_post_id"], limit=20)
                            comments_raw.extend(extract_comments_from_response(comments_resp, post["fb_post_id"]))

                        posts = [
                            PostObservation(
                                fb_post_id=p.get("fb_post_id", p.get("id")),
                                message=p.get("message", ""),
                                created_time=p.get("created_time", ""),
                                posted_by_page=p.get("posted_by_page", False),
                                like_count=p.get("like_count", 0),
                                comment_count=p.get("comment_count", 0),
                                raw=p,
                            )
                            for p in posts_raw
                        ]
                        comments = [
                            CommentObservation(
                                fb_comment_id=c.get("fb_comment_id", c.get("id")),
                                post_id=c.get("post_id"),
                                message=c.get("message", ""),
                                from_name=c.get("from_name", "Someone"),
                                from_id=c.get("from_id", ""),
                                created_time=c.get("created_time", ""),
                                parent_comment_id=c.get("parent_comment_id"),
                                like_count=c.get("like_count", 0),
                                raw=c,
                            )
                            for c in comments_raw
                        ]
                        return ObservationResult(posts=posts, comments=comments)
                    finally:
                        storage.close()
                except Exception:
                    return ObservationResult()

            def publish_text_status(self, message: str) -> str:
                result = self._client.post_status(message)
                return result.get("id", "")

            def reply_to_comment(self, target_comment_id: str, message: str) -> str:
                result = self._client.reply_to_comment(target_comment_id, message)
                return result.get("id", "")

        return _GraphAPITransport(config)

    if name == "camoufox_ui":
        from .facebook_camoufox import CamoufoxTransport
        return CamoufoxTransport(config)

    raise TransportUnavailableError(
        f"Unknown Facebook transport: {config.facebook_transport!r}. "
        f"Supported: {', '.join(['graph_api', 'camoufox_ui'])}"
    )


def supported_transports() -> list[str]:
    """Return the canonical list of supported transport names."""
    return ["graph_api", "camoufox_ui"]
