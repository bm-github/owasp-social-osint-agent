import json
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import DateTimeEncoder, get_sort_key

logger = logging.getLogger("SocialOSINTAgent.cache")

CACHE_EXPIRY_HOURS = 24
MAX_CACHE_ITEMS = 200

class CacheManager:
    def __init__(self, base_dir: Path, is_offline: bool):
        self.base_dir = base_dir
        self.is_offline = is_offline
        self.cache_dir = self.base_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    @lru_cache(maxsize=128)
    def get_cache_path(self, platform: str, username: str) -> Path:
        """Generates a consistent local path for cache files."""
        safe_username = "".join(c if c.isalnum() or c in ["-", "_", ".", "@"] else "_" for c in username)
        safe_username = safe_username[:100]
        return self.cache_dir / f"{platform}_{safe_username}.json"

    def load(self, platform: str, username: str) -> Optional[Dict[str, Any]]:
        """Loads data from a user's cache file if fresh or if in offline mode."""
        cache_path = self.get_cache_path(platform, username)
        if not cache_path.exists():
            logger.debug(f"Cache miss (file not found): {cache_path}")
            return None
        try:
            logger.debug(f"Attempting to load cache: {cache_path}")
            data = json.loads(cache_path.read_text(encoding="utf-8"))

            if "timestamp" not in data:
                logger.warning(f"Cache file for {platform}/{username} is missing timestamp. Discarding.")
                cache_path.unlink(missing_ok=True)
                return None
            
            timestamp = get_sort_key(data, "timestamp")

            required_keys = ["timestamp"]
            if platform == "mastodon": required_keys.extend(["posts", "user_info", "stats"])
            elif platform == "twitter": required_keys.extend(["tweets", "user_info"])
            elif platform == "reddit": required_keys.extend(["submissions", "comments", "stats"])
            elif platform == "bluesky": required_keys.extend(["posts", "stats"])
            elif platform == "hackernews":
                if "submissions" in data and "items" not in data: # Old cache
                    required_keys.extend(["submissions", "stats"])
                else:
                    required_keys.extend(["items", "stats"])

            if any(key not in data for key in required_keys):
                logger.warning(f"Cache file for {platform}/{username} is incomplete. Discarding.")
                cache_path.unlink(missing_ok=True)
                return None

            if platform == "hackernews" and "submissions" in data and "items" not in data:
                data["items"] = data.pop("submissions")
                logger.debug(f"Migrated 'submissions' to 'items' for legacy HackerNews cache: {cache_path}")

            if self.is_offline:
                logger.info(f"Offline mode: Using cache for {platform}/{username}.")
                return data

            is_fresh = (datetime.now(timezone.utc) - timestamp) < timedelta(hours=CACHE_EXPIRY_HOURS)
            if is_fresh:
                logger.info(f"Cache hit and valid (fresh) for {platform}/{username}")
                return data
            else:
                logger.info(f"Cache expired for {platform}/{username}. Returning stale data for incremental baseline.")
                return data

        except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
            logger.warning(f"Failed to load or parse cache for {platform}/{username}: {e}. Discarding cache.")
            cache_path.unlink(missing_ok=True)
            return None
        except Exception as e:
            logger.error(f"Unexpected error loading cache for {platform}/{username}: {e}", exc_info=True)
            cache_path.unlink(missing_ok=True)
            return None

    def save(self, platform: str, username: str, data: Dict[str, Any]):
        """Saves data to a user's cache file."""
        cache_path = self.get_cache_path(platform, username)
        try:
            sort_key_map = {
                "twitter": [("tweets", "created_at")],
                "reddit": [("submissions", "created_utc"), ("comments", "created_utc")],
                "bluesky": [("posts", "created_at")],
                "hackernews": [("items", "created_at")],
                "mastodon": [("posts", "created_at")],
            }
            if platform in sort_key_map:
                for list_key, dt_key in sort_key_map[platform]:
                    if list_key in data and isinstance(data[list_key], list):
                        data[list_key].sort(key=lambda x: get_sort_key(x, dt_key), reverse=True)
                        logger.debug(f"Sorted '{list_key}' for {platform}/{username} by '{dt_key}'.")

            data["timestamp"] = datetime.now(timezone.utc)
            cache_path.write_text(json.dumps(data, indent=2, cls=DateTimeEncoder), encoding="utf-8")
            logger.info(f"Saved cache for {platform}/{username} to {cache_path}")
        except TypeError as e:
            logger.error(f"Failed to serialize data for {platform}/{username} cache: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Failed to save cache for {platform}/{username}: {e}", exc_info=True)