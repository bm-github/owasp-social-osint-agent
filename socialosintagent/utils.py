import hashlib
import json
import logging
import re
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict
from urllib.parse import urlparse
import httpx
import tweepy
from openai import RateLimitError
from rich.panel import Panel

from .exceptions import RateLimitExceededError

logger = logging.getLogger("SocialOSINTAgent.utils")

SAFE_CDN_DOMAINS = {
    "twitter": ["pbs.twimg.com", "video.twimg.com"],
    "reddit": [
        "i.redd.it", "preview.redd.it", "v.redd.it", "external-preview.redd.it", 
        "www.redditstatic.com", "b.thumbs.redditmedia.com"
        # Note: i.imgur.com is excluded by default for strictness, 
        # but is generally considered a 'safe' CDN vs a private server.
    ],
    "bluesky": ["cdn.bsky.app", "cdn.bsky.social"],
    "mastodon": ["mastodon.social", "files.mastodon.social"]
}

# Allow adding extra domains via .env (e.g., EXTRA_REDDIT_CDNS="i.imgur.com,custom.cdn.com")
for platform in SAFE_CDN_DOMAINS.keys():
    env_var = f"EXTRA_{platform.upper()}_CDNS"
    if extra := os.getenv(env_var):
        SAFE_CDN_DOMAINS[platform].extend([d.strip() for d in extra.split(",")])

class NormalizedMedia(TypedDict, total=False):
    url: str
    local_path: Optional[str]
    type: str  # 'image', 'video', 'gif'
    analysis: Optional[str]

class NormalizedPost(TypedDict, total=False):
    platform: str
    id: str
    created_at: datetime
    author_username: str
    text: str
    media: List[NormalizedMedia]
    external_links: List[str]
    post_url: str
    metrics: Dict[str, int]
    type: str  # 'post', 'comment', 'submission', 'reply', 'repost', 'PushEvent', etc.
    context: Optional[Dict[str, Any]] # For replies, quotes, repo names, etc.

class NormalizedProfile(TypedDict, total=False):
    platform: str
    id: str
    username: str
    display_name: Optional[str]
    bio: Optional[str]
    created_at: Optional[datetime]
    profile_url: str
    metrics: Dict[str, int]

class UserData(TypedDict, total=False):
    profile: NormalizedProfile
    posts: List[NormalizedPost]
    timestamp: datetime
    stats: Dict[str, Any]

logger = logging.getLogger("SocialOSINTAgent.utils")

REQUEST_TIMEOUT = 20.0

# Module-level persistent HTTP client with connection pooling.
# Reusing a single client avoids the overhead of creating a new connection
# for every media download. Thread-safe for concurrent requests.
_shared_http_client: Optional[httpx.Client] = None


def _get_http_client() -> httpx.Client:
    """Returns a shared httpx.Client with connection pooling."""
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.Client(
            follow_redirects=True,
            timeout=REQUEST_TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_http_client
SUPPORTED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
URL_REGEX = re.compile(r'((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:\'".,<>?«»“”‘’]))')

class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

def get_sort_key(item: Any, dt_key: str) -> datetime:
    dt_val = item.get(dt_key)
    if isinstance(dt_val, str):
        try:
            iso_value = f"{dt_val[:-1]}+00:00" if dt_val.endswith("Z") else dt_val
            dt_obj = datetime.fromisoformat(iso_value)
            return dt_obj if dt_obj.tzinfo else dt_obj.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    elif isinstance(dt_val, datetime):
        return dt_val if dt_val.tzinfo else dt_val.replace(tzinfo=timezone.utc)
    elif isinstance(dt_val, (int, float)):
        try:
            return datetime.fromtimestamp(dt_val, tz=timezone.utc)
        except (ValueError, OSError):
            return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)

def sanitize_username(username: str) -> str:
    normalized_user = unicodedata.normalize('NFKC', username)
    sanitized_user = "".join(ch for ch in normalized_user if unicodedata.category(ch)[0] != 'C')
    if sanitized_user != username:
        logger.info(f"Sanitized username. Original: '{username}', Sanitized: '{sanitized_user}'")
    return sanitized_user

def extract_and_resolve_urls(text: str) -> List[str]:
    if not text:
        return []
    matches = URL_REGEX.findall(text)
    return [match[0] for match in matches]

def handle_rate_limit(console, platform_context: str, exception: Exception, should_raise: bool = True):
    error_message = f"{platform_context} API rate limit exceeded."
    reset_info = ""

    original_exc = getattr(exception, 'original_exception', None)

    if isinstance(original_exc, RateLimitError):
        error_message = f"LLM API ({platform_context}) rate limit exceeded."
        if hasattr(original_exc, 'response') and original_exc.response:
            headers = original_exc.response.headers
            retry_after = headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                reset_info = f"Try again in {int(retry_after) + 5} seconds."
    
    elif isinstance(original_exc, tweepy.TooManyRequests):
        headers = original_exc.response.headers
        reset_timestamp = headers.get("x-rate-limit-reset")
        if reset_timestamp and reset_timestamp.isdigit():
            reset_time = datetime.fromtimestamp(int(reset_timestamp), tz=timezone.utc)
            now = datetime.now(timezone.utc)
            wait_duration = reset_time - now
            if wait_duration.total_seconds() > 0:
                minutes, seconds = divmod(int(wait_duration.total_seconds()), 60)
                reset_info = f"The API rate limit will reset in approximately {minutes} minute(s) and {seconds} second(s)."
    
    console.print(
        Panel(f"[bold red]Rate Limit Encountered: {platform_context}[/bold red]\n{error_message}\n{reset_info}",
              title="🚫 Rate Limit", border_style="red")
    )
    
    if should_raise:
        raise RateLimitExceededError(error_message + f" ({reset_info})", original_exception=original_exc)

def download_media(base_dir: Path, url: str, is_offline: bool, platform: str, auth_details: Optional[Dict[str, Any]] = None, allow_external: bool = False) -> Optional[Path]:
    if not is_offline and not allow_external: # Validate Domain against Safe List
        domain = urlparse(url).netloc.lower()
        if platform in SAFE_CDN_DOMAINS:
            if domain not in SAFE_CDN_DOMAINS[platform]:
                logger.warning(f"Security: Blocked download from external domain '{domain}' for {platform}. Use --unsafe-allow-external-media to bypass.")
                return None

    url_hash = hashlib.md5(url.encode()).hexdigest()
    media_dir = base_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
  
    for ext in SUPPORTED_IMAGE_EXTENSIONS + [".mp4", ".webm"]:
        existing_path = media_dir / f"{url_hash}{ext}"
        if existing_path.exists():
            logger.debug(f"Media cache hit: {existing_path}")
            return existing_path

    if is_offline:
        logger.warning(f"Offline mode: Media {url} not in local cache. Skipping download.")
        return None

    valid_types = { "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "video/mp4": ".mp4", "video/webm": ".webm"}
    try:
        auth_headers = {}
        if auth_details:
            if platform == "twitter" and auth_details.get("bearer_token"):
                auth_headers["Authorization"] = f"Bearer {auth_details['bearer_token']}"
            elif platform == "bluesky" and auth_details.get("access_jwt"):
                auth_headers["Authorization"] = f"Bearer {auth_details['access_jwt']}"

        headers = {"User-Agent": "SocialOSINTAgent", **auth_headers}
        client = _get_http_client()
        resp = client.get(url, headers=headers)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "").split(';')[0].strip()
        ext = valid_types.get(content_type)
        if not ext:
            logger.warning(f"Unsupported media type '{content_type}' for URL: {url}.")
            return None
            
        final_path = media_dir / f"{url_hash}{ext}"
        final_path.write_bytes(resp.content)
        return final_path
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise RateLimitExceededError(f"{platform} Media Download")
        logger.error(f"HTTP error downloading {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Media download failed for {url}: {e}", exc_info=False)
        return None
