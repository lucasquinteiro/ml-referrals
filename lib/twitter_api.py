"""
Post to X through the official API (v2 + v1.1 media), via tweepy.

The alternative to the cookie poster in twitter_post.py. This is the supported
path: a developer app's OAuth 1.0a user-context keys, no browser, no session to
keep warm. Media still goes through the v1.1 `media/upload` endpoint (v2 has no
upload of its own), then the tweet is created on v2 with the returned media id.

Credentials (from developer.x.com → your app → Keys and tokens), in .env:
    TWITTER_API_KEY              (consumer/API key)
    TWITTER_API_SECRET           (consumer/API secret)
    TWITTER_ACCESS_TOKEN         (access token, user context)
    TWITTER_ACCESS_TOKEN_SECRET  (access token secret)

The app must have Read+Write permission and the access token must be
regenerated *after* setting that, or posting 403s.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from lib.log import log_step, log_warn

_ENV = ("TWITTER_API_KEY", "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")

UA = "ml-referrals/1.0"


class TwitterAPIError(RuntimeError):
    pass


class MissingCredentials(TwitterAPIError):
    """No API keys configured — caller may choose to skip rather than fail."""


def have_credentials() -> bool:
    return all(os.getenv(k) for k in _ENV)


class TwitterAPIPoster:
    """Posts via the official API. Same surface as TwitterPoster: post(text,
    image_url=, image_path=) -> tweet id. `dry_run` never calls the network."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        if not have_credentials() and not dry_run:
            raise MissingCredentials(
                "Twitter API keys missing. Set " + ", ".join(_ENV) + " in .env "
                "(developer.x.com → your app → Keys and tokens)."
            )

    def _clients(self):
        import tweepy

        ck, cs = os.getenv("TWITTER_API_KEY"), os.getenv("TWITTER_API_SECRET")
        at, ats = os.getenv("TWITTER_ACCESS_TOKEN"), os.getenv("TWITTER_ACCESS_TOKEN_SECRET")
        # v1.1 for media upload, v2 for the tweet itself.
        api_v1 = tweepy.API(tweepy.OAuth1UserHandler(ck, cs, at, ats))
        client_v2 = tweepy.Client(
            consumer_key=ck, consumer_secret=cs,
            access_token=at, access_token_secret=ats,
        )
        return api_v1, client_v2

    def _local_image(self, image_path: Optional[str], image_url: Optional[str]):
        """A local file to upload. Downloads image_url to a temp file if needed.
        Returns (path, is_temp)."""
        if image_path and Path(image_path).is_file():
            return image_path, False
        if image_url and image_url.startswith("http"):
            import httpx

            try:
                r = httpx.get(image_url, timeout=30, headers={"User-Agent": UA})
                if r.status_code == 200 and r.content:
                    suffix = ".png" if "png" in r.headers.get("content-type", "") else ".jpg"
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                    tmp.write(r.content)
                    tmp.close()
                    return tmp.name, True
            except Exception as e:  # noqa: BLE001 - text-only is acceptable
                log_warn(f"could not fetch image for upload ({type(e).__name__}: {e})")
        return None, False

    def post(self, text: str, *, image_url: Optional[str] = None,
             image_path: Optional[str] = None) -> Optional[str]:
        if self.dry_run:
            src = image_path or image_url
            log_step(f"[dry-run] would tweet via API:\n{text}"
                     + (f"\n[with image: {src}]" if src else ""))
            return None

        api_v1, client_v2 = self._clients()

        media_ids = None
        path, is_temp = self._local_image(image_path, image_url)
        if path:
            try:
                media = api_v1.media_upload(filename=path)
                media_ids = [media.media_id_string]
                log_step("uploaded image via API")
            except Exception as e:  # noqa: BLE001 - post text-only rather than fail
                log_warn(f"media upload failed ({type(e).__name__}: {e}); text-only")
            finally:
                if is_temp:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        try:
            resp = client_v2.create_tweet(text=text, media_ids=media_ids)
        except Exception as e:  # noqa: BLE001
            raise TwitterAPIError(f"create_tweet failed: {type(e).__name__}: {e}") from e

        tid = None
        if resp and getattr(resp, "data", None):
            tid = str(resp.data.get("id")) if isinstance(resp.data, dict) else None
        return tid
