"""
Post a tweet using X's own web GraphQL endpoint with cookie auth.

Same approach as the rest of our pipelines: no official Twitter API, no
developer app, no OAuth. We reuse the `auth_token` + `ct0` cookies from a
logged-in browser session — exactly the credentials twitter-updates already
uses for reading — and call the endpoint x.com itself calls when you hit Post.

Two things about that endpoint drift over time, so both self-heal:
  * the GraphQL query id  -> pinned by default; on a 404 it is re-discovered
                             from the logged-in JS bundle and cached in state/.
                             Override with X_CREATE_TWEET_QUERY_ID.
  * the `features` map    -> X names the missing flags in its error response,
                             so we add them and retry once
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from lib.log import log_step, log_warn
from lib.twitter import load_cookie_pair

# The public bearer token x.com's web client ships with. Not a secret.
BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Used when bundle discovery fails. Correct as of writing; expect it to rot.
FALLBACK_QUERY_ID = "SoVnbfCycZ7fERGCwpZkYA"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# The flag set X currently requires on CreateTweet. Anything missing is added
# automatically from the API's own error message (see _post_once).
DEFAULT_FEATURES: dict[str, bool] = {
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "articles_preview_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "premium_content_api_read_enabled": False,
    "responsive_web_jetfuel_frame": False,
}


def _upsized(url: str) -> str:
    """MercadoLibre encodes the render size in the filename; the 2X variant is
    roughly 3x the pixels for the same image. Returns the original unchanged
    when it's already 2X or doesn't look like an ML image URL."""
    if not url or "_2X_" in url:
        return url
    for marker in ("D_NQ_NP_", "D_Q_NP_"):
        if marker in url:
            return url.replace(marker, marker + "2X_", 1)
    return url


class TwitterPostError(RuntimeError):
    pass


class TwitterPoster:
    """Posts tweets with cookie auth. `dry_run=True` never touches the network."""

    def __init__(self, *, cache_dir: Optional[Path] = None, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._query_id: Optional[str] = None

        pair = load_cookie_pair()
        if not pair and not dry_run:
            raise TwitterPostError(
                "No Twitter cookies found. Set TWITTER_AUTH_TOKEN and TWITTER_CT0 "
                "in .env, or drop a twitter-cookies.json next to run.py shaped "
                'like {"auth_token": "...", "ct0": "..."}. These are the same '
                "credentials the twitter-updates project uses."
            )
        self.auth_token, self.ct0 = pair or ("", "")

    # ---- query id discovery ---------------------------------------------

    @property
    def _qid_cache(self) -> Optional[Path]:
        return self.cache_dir / "create_tweet_qid.json" if self.cache_dir else None

    def _cached_qid(self) -> Optional[str]:
        p = self._qid_cache
        if not p or not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            # Re-discover weekly; X ships new bundles often.
            if time.time() - data.get("at", 0) < 7 * 86400:
                return data.get("query_id")
        except Exception:  # noqa: BLE001
            pass
        return None

    def _store_qid(self, qid: str) -> None:
        p = self._qid_cache
        if not p:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"query_id": qid, "at": time.time()}), encoding="utf-8")
        except Exception:  # noqa: BLE001 - caching is an optimisation, not required
            pass

    def _discover_query_id(self) -> str:
        """Find CreateTweet's GraphQL id by scanning X's web bundle.

        Must be done with the session cookies attached: CreateTweet only appears
        in the logged-in bundle, so an anonymous fetch of x.com never sees it.
        Only called when the current id turns out to be stale (see `post`).
        """
        from concurrent.futures import ThreadPoolExecutor

        pattern_a = re.compile(r'queryId:"([^"]+)"[^}]{0,200}?operationName:"CreateTweet"')
        pattern_b = re.compile(r'operationName:"CreateTweet".{0,300}?queryId:"([^"]+)"', re.S)

        try:
            headers = {"User-Agent": UA, "cookie": f"auth_token={self.auth_token}; ct0={self.ct0}"}
            with httpx.Client(timeout=25, headers=headers, follow_redirects=True) as c:
                home = c.get("https://x.com/home").text

                # Current layout is /x-web/x-web/assets/*.js referenced from an
                # entry chunk; older builds used /responsive-web/client-web/.
                bundles = set(re.findall(r'https://abs\.twimg\.com/[^"\'\\ )]+\.js', home))
                for entry in list(bundles):
                    if "entry-client" not in entry:
                        continue
                    base = entry.rsplit("/", 1)[0] + "/"
                    js = c.get(entry).text
                    if "CreateTweet" in js:
                        m = pattern_a.search(js) or pattern_b.search(js)
                        if m:
                            return m.group(1)
                    bundles.update(
                        base + a for a in set(re.findall(r'assets/[A-Za-z0-9_\-\.]+\.js', js))
                    )

                def scan(url: str) -> str:
                    try:
                        js = c.get(url).text
                    except Exception:  # noqa: BLE001 - skip unreachable chunks
                        return ""
                    if "CreateTweet" not in js:
                        return ""
                    m = pattern_a.search(js) or pattern_b.search(js)
                    return m.group(1) if m else ""

                with ThreadPoolExecutor(max_workers=12) as ex:
                    for found in ex.map(scan, sorted(bundles)):
                        if found:
                            return found
        except Exception as e:  # noqa: BLE001 - keep the pinned id
            log_warn(f"CreateTweet query-id discovery failed ({type(e).__name__}: {e})")
        return ""

    def query_id(self) -> str:
        """Env override > disk cache > pinned default. Discovery is lazy."""
        if self._query_id:
            return self._query_id
        self._query_id = (
            os.environ.get("X_CREATE_TWEET_QUERY_ID", "").strip()
            or self._cached_qid()
            or FALLBACK_QUERY_ID
        )
        return self._query_id

    def _refresh_query_id(self) -> bool:
        """Re-discover the query id after a 404. True when it actually changed."""
        old = self.query_id()
        found = self._discover_query_id()
        if not found or found == old:
            return False
        log_step(f"CreateTweet query id changed: {old} -> {found}")
        self._query_id = found
        self._store_qid(found)
        return True

    # ---- posting ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {BEARER}",
            "cookie": f"auth_token={self.auth_token}; ct0={self.ct0}",
            "x-csrf-token": self.ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "content-type": "application/json",
            "user-agent": UA,
            "origin": "https://x.com",
            "referer": "https://x.com/home",
        }

    # ---- media -----------------------------------------------------------

    def upload_image(self, url: str, *, timeout: float = 30.0) -> Optional[str]:
        """Fetch an image and upload it to X. Returns a media id, or None.

        Never raises: a tweet with no picture is much better than no tweet, so
        every failure here degrades to text-only.
        """
        if self.dry_run:
            return None
        try:
            with httpx.Client(timeout=timeout, headers={"User-Agent": UA}) as c:
                img = c.get(_upsized(url))
                if img.status_code != 200 or not img.content:
                    img = c.get(url)  # the upsized variant may not exist
                if img.status_code != 200 or not img.content:
                    log_warn(f"could not fetch product image ({img.status_code})")
                    return None
                content = img.content
                mime = img.headers.get("content-type", "image/jpeg").split(";")[0]

            with httpx.Client(timeout=timeout) as c:
                r = c.post(
                    "https://upload.twitter.com/1.1/media/upload.json",
                    headers={k: v for k, v in self._headers().items()
                             if k != "content-type"},
                    files={"media": ("image", content, mime)},
                )
            if r.status_code not in (200, 201):
                log_warn(f"media upload failed: HTTP {r.status_code} {r.text[:200]}")
                return None
            media_id = str(r.json().get("media_id_string") or "")
            if media_id:
                log_step(f"uploaded product image ({len(content) // 1024} KB)")
            return media_id or None
        except Exception as e:  # noqa: BLE001 - text-only is an acceptable outcome
            log_warn(f"media upload failed ({type(e).__name__}: {e})")
            return None

    # ---- posting ---------------------------------------------------------

    def _post_once(
        self, text: str, features: dict[str, bool], media_ids: Optional[list[str]] = None
    ) -> httpx.Response:
        entities = [{"media_id": m, "tagged_users": []} for m in (media_ids or [])]
        payload = {
            "variables": {
                "tweet_text": text,
                "dark_request": False,
                "media": {"media_entities": entities, "possibly_sensitive": False},
                "semantic_annotation_ids": [],
                "disallowed_reply_options": None,
            },
            "features": features,
            "queryId": self.query_id(),
        }
        url = f"https://x.com/i/api/graphql/{self.query_id()}/CreateTweet"
        with httpx.Client(timeout=30) as c:
            return c.post(url, headers=self._headers(), json=payload)

    def post(self, text: str, *, image_url: Optional[str] = None) -> Optional[str]:
        """Publish `text`, optionally with a product image attached.

        A native upload gives a full-width photo; relying on the link's own
        preview only yields Mercado Libre's small `summary` card.
        """
        if self.dry_run:
            extra = f"\n[with image: {image_url}]" if image_url else ""
            log_step(f"[dry-run] would tweet:\n{text}{extra}")
            return None

        media_ids: list[str] = []
        if image_url:
            media_id = self.upload_image(image_url)
            if media_id:
                media_ids.append(media_id)

        features = dict(DEFAULT_FEATURES)
        resp = self._post_once(text, features, media_ids)

        # A stale GraphQL query id shows up as a 404 on the endpoint itself.
        # Re-discover it from the logged-in bundle and retry once.
        if resp.status_code == 404 and self._refresh_query_id():
            resp = self._post_once(text, features, media_ids)

        # X reports unknown/missing feature flags by name — add them and retry.
        if resp.status_code == 400 and "features cannot be null" in resp.text:
            missing = re.findall(r"[\w.]+(?=,|\s|$)", resp.text.split("null:")[-1])
            added = {m: True for m in missing if m and m not in features}
            if added:
                log_warn(f"adding {len(added)} feature flag(s) X asked for; retrying")
                features.update(added)
                resp = self._post_once(text, features, media_ids)

        if resp.status_code == 403 and "could not authenticate" in resp.text.lower():
            raise TwitterPostError(
                "X rejected the cookies (403). They expire — re-export "
                "auth_token and ct0 from a logged-in x.com browser session."
            )
        if resp.status_code == 404:
            raise TwitterPostError(
                f"CreateTweet returned 404 for query id {self.query_id()}. X rotated "
                "the endpoint and auto-discovery didn't find the new one. Grab it "
                "from the CreateTweet request in your browser's Network tab and set "
                "X_CREATE_TWEET_QUERY_ID in .env."
            )
        if resp.status_code != 200:
            raise TwitterPostError(
                f"CreateTweet failed: HTTP {resp.status_code} {resp.text[:400]}"
            )

        data: dict[str, Any] = resp.json()
        if data.get("errors"):
            raise TwitterPostError(f"CreateTweet returned errors: {data['errors']}")

        try:
            result = data["data"]["create_tweet"]["tweet_results"]["result"]
            return result.get("rest_id") or result["legacy"]["id_str"]
        except (KeyError, TypeError) as e:
            raise TwitterPostError(
                f"Unexpected CreateTweet response shape ({e}): {json.dumps(data)[:400]}"
            ) from e
