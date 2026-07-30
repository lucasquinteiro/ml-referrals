"""
Bootstrap + configuration for the ml-referrals project.

Self-contained project: own virtualenv, own lib/. Mirrors the layout of the
twitter-updates project so the two feel the same to operate.

Credentials are resolved from the environment. `bootstrap()` loads them from
(in order, without overriding anything already set):
  1. the real process environment   (GitHub Actions secrets, your shell)
  2. <project>/.env                  (local development)
  3. <ai>/twitter-updates/.env       (optional local convenience fallback)

load_dotenv(override=False) never clobbers existing vars, so real env vars
(CI secrets) always win, then the local .env, then twitter-updates'.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
AI_ROOT = PROJECT_DIR.parent

# Local state (gitignored): SQLite price history + Playwright browser profile.
STATE_DIR = PROJECT_DIR / "state"
DB_PATH = STATE_DIR / "ml_referrals.db"
BROWSER_PROFILE_DIR = STATE_DIR / "browser-profile"

ENV_FILE = PROJECT_DIR / ".env"
TWITTER_UPDATES_ENV_FILE = AI_ROOT / "twitter-updates" / ".env"

CONFIG_JSON = PROJECT_DIR / "config.json"

_BOOTSTRAPPED = False


def bootstrap() -> None:
    """Load env vars and ensure the state dir exists. Idempotent."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return

    try:
        from dotenv import load_dotenv

        if ENV_FILE.is_file():
            load_dotenv(ENV_FILE, override=False)
        if TWITTER_UPDATES_ENV_FILE.is_file():
            load_dotenv(TWITTER_UPDATES_ENV_FILE, override=False)
    except ImportError:
        pass

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _BOOTSTRAPPED = True


# --------------------------------------------------------------------------
# Settings (config.json overrides these defaults)
# --------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    # ---- what to look for -------------------------------------------------
    # The keyword list that drives everything. A product is a candidate when
    # its title matches one of these (see `keyword_match_mode`).
    # Each entry is either a plain string or an object:
    #   {"term": "notebook", "label": "Notebooks",
    #    "exclude": ["funda", "cargador"], "min_discount_pct": 25}
    "keywords": [],
    # "any_word"  -> every word of the term must appear somewhere in the title
    # "phrase"    -> the term must appear as a contiguous substring
    "keyword_match_mode": "any_word",
    # Words that disqualify a product no matter which keyword matched.
    # Useful to filter accessories out of "notebook", "iphone", etc.
    "global_exclude": ["funda", "case ", "protector", "repuesto", "replica"],

    # ---- site -------------------------------------------------------------
    # Which MercadoLibre site to crawl. The /ofertas page of this host is the
    # source: it is publicly readable, unlike /listado search which now sits
    # behind a login wall.
    "site": "https://www.mercadolibre.com.ar",
    # How many /ofertas pages to crawl per run (~45 products per page).
    "pages_per_run": 12,
    # Seconds between page loads, randomised by +/-50% so the interval isn't
    # itself a bot signature. Be polite — this is someone's site, and the
    # whole pipeline depends on not being flagged.
    "delay_between_pages_sec": 4.0,
    # Fail the run if fewer than this many products were scraped (catches the
    # case where MercadoLibre changed their markup and every selector broke).
    "min_products_expected": 20,

    # ---- what counts as an offer worth posting ----------------------------
    # Minimum discount MercadoLibre itself advertises on the card. Anything
    # below this never gets posted at all.
    "min_discount_pct": 20,
    # The tier you actually want to be posting. The queue is always ordered
    # best-discount-first, so this changes nothing about *what* gets picked —
    # it's the line below which runs say so, telling you the good stock ran out
    # and it's time to re-ingest. 0 disables the notice.
    "preferred_min_discount_pct": 60,
    # Price band, in ARS. 0 = no bound.
    "min_price": 0,
    "max_price": 0,
    # Skip items with a seller rating below this (0 = don't care). Cards
    # without any rating are kept.
    "min_rating": 0.0,
    # Don't re-post the same product within this many days.
    "repost_cooldown_days": 21,
    # simulate/offers/post refuse to build tweets from data older than this,
    # since prices move and offers expire. Must exceed the gap between ingest
    # runs or posting stalls: with a daily ingest and hourly posting, the last
    # posts of the day are ~24h behind, so this allows 24h plus a buffer.
    "max_data_age_hours": 26,

    # ---- affiliate --------------------------------------------------------
    # Your affiliate tag + tool id from the Mercado Libre affiliate dashboard
    # (Programa de Afiliados). Both also readable from env as
    # ML_AFFILIATE_TAG / ML_AFFILIATE_TOOL_ID, which win over these.
    "affiliate": {
        "tag": "",
        "tool_id": "",
        # Use MercadoLibre's own link builder (real meli.la short links with a
        # signed ref=) when a session is available. Falls back to the param
        # form below whenever it isn't.
        "use_link_builder": True,
        # Only mint links for offers at or above this discount.
        "min_discount_for_link": 40,
        # Hard ceiling per `./run links` run. Minting is deliberately a
        # separate, opt-in command: a burst of link creations is account-level
        # activity on the one account whose standing the whole business rests
        # on, so it should never ride along with a scrape.
        "max_links_per_ingest": 10,
        # Links are minted in small batches with a pause between them, rather
        # than one call carrying everything.
        "link_batch_size": 5,
        "delay_between_link_batches_sec": 25,
        # createLink is tried over plain HTTP first. This allows falling back
        # to driving a real browser when the session isn't accepted that way.
        "allow_browser_fallback": True,
        # Extra query params appended to every affiliate link (fallback form).
        "extra_params": {"forceInApp": "true"},
    },

    # ---- posting ----------------------------------------------------------
    # Posting is one tweet per run, always — a burst of affiliate links reads
    # as spam, and one at a time keeps every publish reviewable.
    # Prefer the LLM for tweet copy; falls back to templates when no API key
    # is configured or the call fails.
    "use_llm_for_copy": True,
    "tweet_language": "es-AR",
    # Optional signature line appended to every tweet (brand sign-off). The 🦈
    # already leads each template, so this defaults off; set e.g. "🦈 Shark Deals".
    "tweet_signature": "",
    # Where `post` publishes:
    #   "twitter_api"     the official X API (developer app keys; recommended)
    #   "twitter_cookie"  the old cookie/session poster (no API needed)
    #   "slack"           a Slack channel via SLACK_WEBHOOK_URL — a simulator
    "post_target": "twitter_api",
    # When posting to X, also mirror the tweet to Slack, so the channel is a
    # running log of what actually went out.
    "mirror_to_slack": True,
    # What picture to attach:
    #   "product"    the product photo, uploaded natively (no browser)
    #   "screenshot" MercadoLibre's own compact offer card (photo, title,
    #                discount, price) — found by searching the product's keyword,
    #                on a clean white canvas (no blurred backdrop)
    #   "none"       text only
    # Any failure falls back down this list rather than losing the tweet.
    "tweet_image_mode": "product",
    # Appended to every tweet (kept short — links eat 23 chars).
    "tweet_hashtags": ["#Ofertas"],
    # Affiliate-disclosure suffix. Required by most jurisdictions and by X's
    # own rules for paid/affiliate links. Empty string disables it.
    "tweet_disclosure": "Link de afiliado",

    # ---- Mercado Libre rate gate ------------------------------------------
    # Every ML request routes through lib/mlgate. Nothing here can burst: a
    # jittered floor between requests, a rolling per-account budget, and a
    # circuit breaker that stops an account cold the moment it sees a wall.
    # Budgets are per identity — "anon" is anonymous /ofertas (IP throttle
    # only), "scraping" is the burner on search, "affiliate" is link minting.
    "ml_gate": {
        "min_interval_sec": 45.0,
        "jitter": 0.5,
        "max_per_hour": {"anon": 120, "scraping": 40, "affiliate": 8},
        "max_per_day": {"anon": 800, "scraping": 200, "affiliate": 30},
        "cooldown_sec": 3600,
        "max_single_wait_sec": 900,
    },

    # ---- storage ----------------------------------------------------------
    # "sqlite"   -> state/ml_referrals.db (local, gitignored)
    # "supabase" -> shared Postgres; required in GitHub Actions, since a runner
    #               is ephemeral and a local file would be discarded.
    # Override per-run with the ML_STORE env var.
    "store": "supabase",

    # ---- LLM (same providers as twitter-updates) --------------------------
    "llm": {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
    },
    "llm_fallback": {
        "model": "llama-3.1-8b-instant",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
    },
}


class Settings:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    def __getattr__(self, name: str) -> Any:
        try:
            return self._d[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def get(self, name: str, default: Any = None) -> Any:
        return self._d.get(name, default)

    @property
    def store(self) -> str:
        """Backend name. ML_STORE wins, so a local run can stay on SQLite
        without editing (and accidentally committing) config.json."""
        return (os.getenv("ML_STORE") or self._d.get("store") or "sqlite").strip()

    # ---- affiliate credentials (env wins over config.json) ---------------

    @property
    def affiliate_tag(self) -> str:
        return (os.getenv("ML_AFFILIATE_TAG") or self.affiliate.get("tag") or "").strip()

    @property
    def affiliate_tool_id(self) -> str:
        return (
            os.getenv("ML_AFFILIATE_TOOL_ID") or self.affiliate.get("tool_id") or ""
        ).strip()

    # ---- LLM credentials -------------------------------------------------

    @property
    def llm_api_key(self) -> str | None:
        return os.getenv(self.llm.get("api_key_env", "GROQ_API_KEY"))

    @property
    def llm_fallback_api_key(self) -> str | None:
        fb = self._d.get("llm_fallback") or {}
        env = fb.get("api_key_env")
        return os.getenv(env) if env else None

    def as_dict(self) -> dict[str, Any]:
        return dict(self._d)


def load_settings() -> Settings:
    """Merge config.json over the built-in defaults (one level deep for dicts)."""
    data = json.loads(json.dumps(_DEFAULTS))  # deep copy
    if CONFIG_JSON.is_file():
        user = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
    return Settings(data)
