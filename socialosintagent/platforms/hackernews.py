import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from ..cache import CACHE_EXPIRY_HOURS, MAX_CACHE_ITEMS, CacheManager
from ..exceptions import RateLimitExceededError, UserNotFoundError
from ..llm import LLMAnalyzer
from ..utils import get_sort_key

logger = logging.getLogger("SocialOSINTAgent.platforms.hackernews")

REQUEST_TIMEOUT = 20.0
# Default used if no count is specified in the fetch plan
DEFAULT_FETCH_LIMIT = 100
# Algolia API has a max of 1000 hits per page
ALGOLIA_MAX_HITS = 1000

def fetch_data(
    username: str,
    cache: CacheManager,
    llm: LLMAnalyzer, # Not used, but kept for consistent signature
    force_refresh: bool = False,
    fetch_limit: int = DEFAULT_FETCH_LIMIT,
) -> Optional[Dict[str, Any]]:
    """Fetches user activity from HackerNews via Algolia API."""
    
    cached_data = cache.load("hackernews", username)
    if cache.is_offline:
        return cached_data or {"timestamp": datetime.now(timezone.utc).isoformat(), "items": [], "stats": {}}

    if not force_refresh and cached_data and (datetime.now(timezone.utc) - get_sort_key(cached_data, "timestamp")) < timedelta(hours=CACHE_EXPIRY_HOURS):
        if len(cached_data.get("items", [])) >= fetch_limit:
            return cached_data

    logger.info(f"Fetching HackerNews data for {username} (Force Refresh: {force_refresh}, Limit: {fetch_limit})")
    
    existing_items = cached_data.get("items", []) if not force_refresh and cached_data else []
    
    # Only use incremental fetch if we're not force refreshing AND not trying to "load more"
    use_incremental_fetch = not force_refresh and fetch_limit <= len(existing_items)
    latest_timestamp_i = max((item.get("created_at_i", 0) for item in existing_items), default=0) if use_incremental_fetch else 0

    try:
        base_url = "https://hn.algolia.com/api/v1/search_by_date"
        params: Dict[str, Any] = {
            "tags": f"author_{quote_plus(username)}", 
            "hitsPerPage": min(fetch_limit, ALGOLIA_MAX_HITS)
        }
        if latest_timestamp_i > 0:
            params["numericFilters"] = f"created_at_i>{latest_timestamp_i}"

        new_items_data = []
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(base_url, params=params)
            response.raise_for_status()
            data = response.json()

        for hit in data.get("hits", []):
            item_type = "comment" if "comment" in hit.get("_tags", []) else "story"
            cleaned_text = ""
            raw_text = hit.get("story_text") or hit.get("comment_text") or ""
            if raw_text:
                cleaned_text = BeautifulSoup(raw_text, "html.parser").get_text(separator=" ", strip=True)

            item_data = {
                "objectID": hit.get("objectID"), "type": item_type,
                "title": hit.get("title"), "url": hit.get("url"),
                "points": hit.get("points"), "num_comments": hit.get("num_comments"),
                "story_id": hit.get("story_id"), "parent_id": hit.get("parent_id"),
                "created_at_i": hit.get("created_at_i"),
                "created_at": datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc).isoformat(),
                "text": cleaned_text
            }
            new_items_data.append(item_data)
            
        combined = new_items_data + existing_items
        final_items = sorted(list({i['objectID']: i for i in combined}.values()), key=lambda x: get_sort_key(x, "created_at"), reverse=True)[:max(fetch_limit, MAX_CACHE_ITEMS)]
        
        story_items = [s for s in final_items if s.get("type") == "story"]
        comment_items = [c for c in final_items if c.get("type") == "comment"]
        stats = {
            "total_items_cached": len(final_items),
            "total_stories_cached": len(story_items),
            "total_comments_cached": len(comment_items),
            "average_story_points": round(sum(s.get("points", 0) or 0 for s in story_items) / max(1, len(story_items)), 2),
            "average_comment_points": round(sum(c.get("points", 0) or 0 for c in comment_items) / max(1, len(comment_items)), 2),
        }

        final_data = {"items": final_items, "stats": stats}
        cache.save("hackernews", username, final_data)
        return final_data

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            raise RateLimitExceededError("HackerNews API rate limited.")
        if e.response.status_code == 400 and "invalid tag name" in e.response.text.lower():
            raise UserNotFoundError(f"HackerNews username '{username}' seems invalid (invalid tag).")
        logger.error(f"HN Algolia API HTTP error for {username}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching HN data for {username}: {e}", exc_info=True)
        return None