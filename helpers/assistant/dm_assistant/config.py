from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = _env("GEMINI_API_KEY")
    gemini_model: str = _env("GEMINI_MODEL", "gemini-3.8-flash")
    # Tried in order if the main model is unavailable (comma-separated)
    gemini_fallback_models: tuple[str, ...] = tuple(
        m.strip() for m in _env("GEMINI_FALLBACK_MODELS").split(",") if m.strip()
    )
    # Empty = use the model's default temperature (recommended for newer Gemini models)
    gemini_temperature: float | None = float(_env("GEMINI_TEMPERATURE")) if _env("GEMINI_TEMPERATURE") else None
    my_name: str = _env("LINKEDIN_MY_NAME")
    data_dir: Path = Path(_env("DATA_DIR", "./data")).resolve()
    
    run_times: tuple[str, ...] = tuple(
        t.strip() for t in _env("RUN_TIMES", "09:00,13:00,17:00").split(",") if t.strip()
    )
    timezone: str = _env("TIMEZONE")

    max_threads_per_run: int = int(_env("MAX_THREADS_PER_RUN", "15"))
    max_messages_per_thread: int = int(_env("MAX_MESSAGES_PER_THREAD", "30"))
    headless: bool = _bool("HEADLESS", False)
    browser_channel: str = _env("BROWSER_CHANNEL")

    # --- connection-note follow-ups ---
    conversation_scan_depth: int = int(_env("CONVERSATION_SCAN_DEPTH", "40"))
    max_followup_checks_per_run: int = int(_env("MAX_FOLLOWUP_CHECKS_PER_RUN", "10"))
    connection_note_max_chars: int = int(_env("CONNECTION_NOTE_MAX_CHARS", "300"))
    followup_max_age_days: int = int(_env("FOLLOWUP_MAX_AGE_DAYS", "14"))  # 0 = no age limit
    
    enable_direct_send: bool = _bool("ENABLE_DIRECT_SEND", True)
    meeting_link: str = _env("MEETING_LINK")

    host: str = _env("HOST", "127.0.0.1")
    port: int = int(_env("PORT", "5055"))
    open_browser_on_new_drafts: bool = _bool("OPEN_BROWSER_ON_NEW_DRAFTS", True)
    llm_min_interval_s: float = float(_env("LLM_MIN_INTERVAL_SECONDS", "7"))

    # --- channels ---
    linkedin_enabled: bool = _bool("LINKEDIN_ENABLED", True)

    # --- email (Namecheap Private Email defaults) ---
    email_address: str = _env("EMAIL_ADDRESS")
    email_password: str = _env("EMAIL_PASSWORD")
    email_display_name: str = _env("EMAIL_DISPLAY_NAME")
    email_aliases: str = _env("EMAIL_ALIASES")
    email_imap_host: str = _env("EMAIL_IMAP_HOST", "mail.privateemail.com")
    email_imap_port: int = int(_env("EMAIL_IMAP_PORT", "993"))
    email_smtp_host: str = _env("EMAIL_SMTP_HOST", "mail.privateemail.com")
    email_smtp_port: int = int(_env("EMAIL_SMTP_PORT", "465"))
    email_sent_folder: str = _env("EMAIL_SENT_FOLDER")
    email_lookback_days: int = int(_env("EMAIL_LOOKBACK_DAYS", "3"))
    email_max_per_run: int = int(_env("EMAIL_MAX_PER_RUN", "20"))
    email_body_max_chars: int = int(_env("EMAIL_BODY_MAX_CHARS", "6000"))
    email_quote_original: bool = _bool("EMAIL_QUOTE_ORIGINAL", True)
    email_signature: str = _env("EMAIL_SIGNATURE").replace("\\n", "\n")
    email_webmail_url: str = _env("EMAIL_WEBMAIL_URL", "https://privateemail.com/appsuite/")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def my_names(self) -> list[str]:
        return [n.strip().lower() for n in self.my_name.split(",") if n.strip()]

    @property
    def display_name(self) -> str:
        names = [n.strip() for n in self.my_name.split(",") if n.strip()]
        return names[0] if names else "the user"

    @property
    def ui_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    @property
    def email_enabled(self) -> bool:
        return bool(self.email_address and self.email_password)

    @property
    def email_addresses(self) -> set[str]:
        extra = {a.strip().lower() for a in self.email_aliases.split(",") if a.strip()}
        return ({self.email_address.lower()} if self.email_address else set()) | extra

    @property
    def email_from_name(self) -> str:
        return self.email_display_name or (self.display_name if self.my_name else "")

settings = Settings()