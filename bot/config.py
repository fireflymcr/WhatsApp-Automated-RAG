"""
Configuration loader for WhatsApp Bot instances.
Reads context.yaml and provides typed access to all settings.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMConfig:
    base_url: str = "http://192.168.1.32:1234/v1"
    api_key: str = "lm-studio"
    model: str = "local-model"
    temperature: float = 0.7
    max_tokens: int = 300


@dataclass
class DatabaseConfig:
    server: str = "host.docker.internal,14314"
    database: str = "master"
    username: str = "sa"
    password: str = ""


@dataclass
class MarketingConfig:
    enabled: bool = False
    cron: str = "0 9 * * 1"  # Default: Monday 9am


@dataclass
class BotConfig:
    # Identity
    instance_name: str = "default"
    business_name: str = "Business"

    # Prompts
    system_prompt: str = ""
    classification_prompt: str = ""

    # Schedule
    check_interval_minutes: int = 5
    lookback_minutes: int = 2880

    # Safety
    cooldown_minutes: int = 10
    max_replies_per_chat_per_day: int = 10
    reply_to_groups: bool = True

    # Context
    context_messages: int = 30

    # Sub-configs
    llm: LLMConfig = field(default_factory=LLMConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    marketing: MarketingConfig = field(default_factory=MarketingConfig)

    # Runtime (set from environment)
    bridge_api_url: str = "http://localhost:8080/api"
    bridge_db_path: str = "/data/bridge-store/messages.db"


def load_config(config_path: Optional[str] = None) -> BotConfig:
    """Load configuration from YAML file and environment variables."""

    # Determine config path
    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "/app/context.yaml")

    # Load YAML
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Build config
    config = BotConfig(
        instance_name=raw.get("instance_name", "default"),
        business_name=raw.get("business_name", "Business"),
        system_prompt=raw.get("system_prompt", ""),
        classification_prompt=raw.get("classification_prompt", ""),
        check_interval_minutes=raw.get("check_interval_minutes", 5),
        lookback_minutes=raw.get("lookback_minutes", 60),
        cooldown_minutes=raw.get("cooldown_minutes", 10),
        max_replies_per_chat_per_day=raw.get("max_replies_per_chat_per_day", 10),
        reply_to_groups=raw.get("reply_to_groups", True),
        context_messages=raw.get("context_messages", 30),
    )

    # LLM config
    llm_raw = raw.get("llm", {})
    config.llm = LLMConfig(
        base_url=llm_raw.get("base_url", "http://192.168.1.32:1234/v1"),
        api_key=llm_raw.get("api_key", "lm-studio"),
        model=llm_raw.get("model", "local-model"),
        temperature=llm_raw.get("temperature", 0.7),
        max_tokens=llm_raw.get("max_tokens", 300),
    )

    # Database config
    db_raw = raw.get("database", {})
    config.database = DatabaseConfig(
        server=db_raw.get("server", "host.docker.internal,14314"),
        database=db_raw.get("database", "master"),
        username=db_raw.get("username", "sa"),
        password=db_raw.get("password", ""),
    )

    # Marketing config
    mktg_raw = raw.get("marketing", {})
    config.marketing = MarketingConfig(
        enabled=mktg_raw.get("enabled", False),
        cron=mktg_raw.get("cron", "0 9 * * 1"),
    )

    # Environment overrides (Docker runtime)
    config.bridge_api_url = os.environ.get(
        "BRIDGE_API_URL", raw.get("bridge_api_url", "http://localhost:8080/api")
    )
    config.bridge_db_path = os.environ.get(
        "BRIDGE_DB_PATH", raw.get("bridge_db_path", "/data/bridge-store/messages.db")
    )

    return config
