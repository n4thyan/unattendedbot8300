"""
Configuration loader for UnattendedBot8300.

Loads settings from environment variables (.env file) and validates them.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


@dataclass
class Config:
    """Configuration settings for the agent."""
    
    # Facebook API settings
    facebook_page_id: str = ""
    facebook_page_access_token: str = ""
    facebook_app_id: str = ""
    facebook_app_secret: str = ""
    facebook_graph_api_version: str = "v26.0"
    
    # Agent modes
    unattended_bot_mode: str = "dry_run"  # dry_run or live
    approval_mode: bool = True
    
    # Rate limits
    rate_limit_posts_hourly: int = 5
    rate_limit_replies_daily: int = 50
    min_action_interval_seconds: int = 1800
    
    # Database path
    database_path: str = "data/bot.db"
    
    # Project root (for resolving relative paths)
    project_root: Path = field(default_factory=Path.cwd)
    
    @property
    def db_path(self) -> Path:
        """Return the absolute path to the SQLite database."""
        if Path(self.database_path).is_absolute():
            return Path(self.database_path)
        return self.project_root / self.database_path
    
    @property
    def fb_base_url(self) -> str:
        """Return the base URL for Facebook Graph API calls."""
        return f"https://graph.facebook.com/{self.facebook_graph_api_version}"
    
    def is_live_mode(self) -> bool:
        """Check if we're in live mode (can execute actions)."""
        return self.unattended_bot_mode == "live"
    
    def is_approval_mode(self) -> bool:
        """Check if approval mode is enabled."""
        return self.approval_mode


def load_config(env_path: Optional[Path] = None) -> Config:
    """
    Load configuration from .env file and environment.
    
    Args:
        env_path: Optional path to .env file. If None, loads from current directory.
    
    Returns:
        Config object with validated settings.
    """
    # Determine project root (where pyproject.toml lives)
    project_root = Path.cwd()
    while not (project_root / "pyproject.toml").exists():
        parent = project_root.parent
        if parent == project_root:
            break
        project_root = parent
    
    # Load .env file if it exists
    if env_path and env_path.exists():
        load_dotenv(dotenv_path=env_path)
    elif (project_root / ".env").exists():
        load_dotenv(dotenv_path=project_root / ".env")
    else:
        # Try loading from cwd
        load_dotenv()
    
    config = Config(project_root=project_root)
    
    # Facebook settings
    config.facebook_page_id = os.getenv("FACEBOOK_PAGE_ID", "")
    config.facebook_page_access_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
    config.facebook_app_id = os.getenv("FACEBOOK_APP_ID", "")
    config.facebook_app_secret = os.getenv("FACEBOOK_APP_SECRET", "")
    config.facebook_graph_api_version = os.getenv("FACEBOOK_GRAPH_API_VERSION", "v26.0")
    
    # Agent modes
    config.unattended_bot_mode = os.getenv("UNATTENDED_BOT_MODE", "dry_run")
    config.approval_mode = os.getenv("APPROVAL_MODE", "true").lower() == "true"
    
    # Rate limits
    try:
        config.rate_limit_posts_hourly = int(os.getenv("RATE_LIMIT_POSTS_HOURLY", "5"))
    except ValueError:
        config.rate_limit_posts_hourly = 5
    
    try:
        config.rate_limit_replies_daily = int(os.getenv("RATE_LIMIT_REPLIES_DAILY", "50"))
    except ValueError:
        config.rate_limit_replies_daily = 50
    
    try:
        config.min_action_interval_seconds = int(os.getenv("MIN_ACTION_INTERVAL_SECONDS", "1800"))
    except ValueError:
        config.min_action_interval_seconds = 1800
    
    # Database path
    config.database_path = os.getenv("DATABASE_PATH", "data/bot.db")
    
    return config


def validate_config(config: Config) -> list[str]:
    """
    Validate configuration and return list of missing/warning messages.
    
    Args:
        config: Configuration object to validate.
    
    Returns:
        List of error/warning messages (empty if all valid).
    """
    errors = []
    
    if not config.facebook_page_id:
        errors.append("FACEBOOK_PAGE_ID is required")
    
    if not config.facebook_page_access_token:
        errors.append("FACEBOOK_PAGE_ACCESS_TOKEN is required")
    
    if not config.facebook_graph_api_version.startswith("v"):
        errors.append(f"Invalid FACEBOOK_GRAPH_API_VERSION: {config.facebook_graph_api_version}")
    
    return errors