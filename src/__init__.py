"""
UnattendedBot8300 — Autonomous AI Agent for Facebook Page Management

This module provides the main entry point and core components.
"""

__version__ = "0.1.0"
__author__ = "Nous Research"

from .config import Config, load_config
from .storage import Storage
from .fb_client import FacebookClient
from .memory import MemoryStore
from .proposed_actions import ProposedAction, ProposedActionQueue
from .wake_cycles import WakeCycle, WakeCycleLogger
from .safety import SafetyPolicy
from .facebook_transport import (
    FacebookTransport, get_transport, supported_transports,
    TransportError, NotLoggedInError, PageNotFoundError,
    SelectorError, CheckpointError, NavigationError, TransportUnavailableError,
)
from .facebook_camoufox import CamoufoxTransport


# Default entry point
def main():
    """Main entry point for CLI access."""
    import sys
    from .cli import main as cli_main
    sys.exit(cli_main())