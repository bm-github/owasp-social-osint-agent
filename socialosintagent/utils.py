import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from openai import RateLimitError
from PIL import Image
from rich.panel import Panel

from .exceptions import RateLimitExceededError


logger = logging.getLogger("SocialOSINTAgent.utils")

REQUEST_TIMEOUT = 20.0
SUPPORTED_IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp", ".gif"]
# A reasonably good regex for finding URLs in text
URL_REGEX = r'(?:(?:https?|ftp):\/\/)?[\w/\-?=%.]+\.[\w/\-?=%.]+'


class DateTimeEncoder(json.JSONEncoder):
    """JSON encoder to handle datetime objects."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)

def get_sort_key(item: Dict[str, Any], dt_key: str) -> datetime:
    """Safely gets and parses a datetime string or object/timestamp for sorting."""
    dt_val = item.get(dt_key)
    if isinstance(dt_val, str):
        try:
            if dt_val.endswith("Z"): dt_val = dt_val[:-1] + "+00:00"
            dt_obj = datetime.fromisoformat(dt_val)
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
    """Sanitizes a username by normalizing it and stripping Unicode control characters."""
    normalized_user = unicodedata.normalize('NFKC', username)
    sanitized_user = "".join(ch for ch in normalized_user if unicodedata.category(ch)[0] != 'C')
    if sanitized_user != username:
        logger.info(f"Sanitized username. Original: '{username}', Sanitized: '{sanitized_user}'")
    return sanitized_user

def extract_and_resolve_urls(text: str) -> List[str]:
    """Extracts URLs from text. Does not resolve them for performance."""
    if not text:
        return []
    # Find all potential URLs in the text
    return re.findall(URL_REGEX, text, re.IGNORECASE)

def handle_rate_limit(console, platform_context: str, exception: Exception):
    """Handles rate limit exceptions by logging and printing a rich panel."""
    error_message = f"{platform_context} API rate limit exceeded."
    reset_info = ""
    wait_seconds = 900  # Default 15 minutes

    if isinstance(exception, RateLimitError):  # LLM rate limits
        error_message = f"LLM API ({platform_context}) rate limit exceeded."
        reset_info = "Wait a few minutes before retrying."
        if hasattr(exception, 'response') and exception.response:
            headers = exception.response.headers
            retry_after = headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                reset_info = f"Try again in {int(retry_after) + 5} seconds."
    # Add other platform-specific header parsing here if needed (e.g., for Twitter's x-rate-limit-reset)
    
    console.print(
        Panel(f"[bold red]Rate Limit Blocked: {platform_context}[/bold red]\n{error_message}\n{reset_info}",
              title="🚫 Rate Limit", border_style="red")
    )
    raise RateLimitExceededError(error_message + f" ({reset_info})")

def download_media(base_dir: Path, url: str, is_offline: bool, platform: str, auth_details: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Downloads a media file from a URL and caches it."""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    media_dir = base_dir / "media"
    
    # Check if file with any valid extension exists
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

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36", **auth_headers}
        with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT) as client:
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