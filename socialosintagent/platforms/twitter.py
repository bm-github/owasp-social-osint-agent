import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import tweepy
from .base_fetcher import BaseFetcher
from ..utils import NormalizedMedia, NormalizedPost, NormalizedProfile, download_media, extract_and_resolve_urls

logger = logging.getLogger("SocialOSINTAgent.platforms.twitter")

XQUIK_API_URL = "https://xquik.com/api/v1"
REQUEST_TIMEOUT = 20.0


def uses_xquik_backend() -> bool:
    return os.getenv("TWITTER_BACKEND", "").lower() == "xquik"


class TwitterFetcher(BaseFetcher):
    def __init__(self):
        super().__init__(platform_name="twitter")

    def _fetch_profile(self, username: str, **kwargs) -> Optional[NormalizedProfile]:
        if uses_xquik_backend():
            return self._fetch_xquik_profile(username)

        client: tweepy.Client = kwargs.get("client")
        res = client.get_user(
            username=username,
            user_fields=["created_at", "public_metrics", "description", "location", "verified"]
        )
        if not res or not res.data: return None
        u = res.data
        return NormalizedProfile(
            platform="twitter", id=str(u.id), username=u.username,
            display_name=u.name, bio=u.description, created_at=u.created_at,
            profile_url=f"https://twitter.com/{u.username}",
            metrics={
                "followers": u.public_metrics.get("followers_count", 0),
                "post_count": u.public_metrics.get("tweet_count", 0),
                "location": u.location or "N/A"
            }
        )

    def _fetch_batch(self, username: str, profile: NormalizedProfile, needed: int, state: Any, **kwargs) -> Tuple[List[Any], Any]:
        if uses_xquik_backend():
            return self._fetch_xquik_batch(profile, needed, state)

        client = kwargs.get("client")
        limit = max(min(needed, 100), 5)
        res = client.get_users_tweets(
            id=profile["id"], 
            max_results=limit, 
            pagination_token=state,
            tweet_fields=["created_at", "public_metrics", "attachments", "in_reply_to_user_id", "author_id"],
            expansions=["attachments.media_keys", "author_id"],
            media_fields=["url", "preview_image_url", "type"],
            user_fields=["username"]
        )
        if not res or not res.data: return [], None
        
        media_map = {m.media_key: m for m in res.includes.get("media", [])} if res.includes else {}
        user_map = {u.id: u for u in res.includes.get("users", [])} if res.includes else {}
        
        wrapped_items = [{"tweet": t, "media_map": media_map, "user_map": user_map} for t in res.data]
        return wrapped_items, res.meta.get("next_token")

    def _normalize(self, item: Any, profile: NormalizedProfile, **kwargs) -> NormalizedPost:
        if uses_xquik_backend():
            return self._normalize_xquik(item, profile, **kwargs)

        t, media_map, user_map = item["tweet"], item["media_map"], item["user_map"]
        cache, allow_ext, client = kwargs.get("cache"), kwargs.get("allow_external_media", False), kwargs.get("client")
        
        media_items = []
        if t.attachments and "media_keys" in t.attachments:
            for k in t.attachments["media_keys"]:
                if m := media_map.get(k):
                    url = m.url or m.preview_image_url
 
                    if path := download_media(cache.base_dir, url, cache.is_offline, "twitter", {"bearer_token": client.bearer_token}, allow_ext):
                        media_items.append(
                            NormalizedMedia(
                                url=url,
                                local_path=str(path),
                                type=self._normalize_media_type(m.type),
                            )
                        )
        
        author_user = user_map.get(t.author_id)
        author_handle = author_user.username if author_user else profile["username"]
        
        return NormalizedPost(
            platform="twitter", id=str(t.id), created_at=t.created_at, 
            author_username=author_handle, text=t.text, media=media_items, 
            external_links=extract_and_resolve_urls(t.text),
            post_url=f"https://twitter.com/i/status/{t.id}",
            metrics={"likes": t.public_metrics.get("like_count", 0), "reposts": t.public_metrics.get("retweet_count", 0)},
            type="reply" if t.in_reply_to_user_id else "post"
        )

    def _xquik_api_base_url(self) -> str:
        return os.getenv("XQUIK_API_BASE_URL", XQUIK_API_URL).rstrip("/")

    def _xquik_headers(self) -> Dict[str, str]:
        api_key = os.getenv("XQUIK_API_KEY")
        if not api_key:
            raise RuntimeError("XQUIK_API_KEY not set.")
        return {"x-api-key": api_key}

    def _xquik_get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        response = httpx.get(
            f"{self._xquik_api_base_url()}{path}",
            headers=self._xquik_headers(),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _fetch_xquik_profile(self, username: str) -> Optional[NormalizedProfile]:
        user = self._xquik_get(f"/x/users/{username.lstrip('@')}")
        if not user:
            return None

        return NormalizedProfile(
            platform="twitter",
            id=str(user.get("id", "")),
            username=user.get("username") or username.lstrip("@"),
            display_name=user.get("name"),
            bio=user.get("description"),
            created_at=self._parse_xquik_datetime(user.get("createdAt")),
            profile_url=f"https://twitter.com/{user.get('username') or username.lstrip('@')}",
            metrics={
                "followers": user.get("followers", 0),
                "post_count": user.get("statusesCount", 0),
                "location": user.get("location") or "N/A",
            },
        )

    def _fetch_xquik_batch(
        self,
        profile: NormalizedProfile,
        needed: int,
        state: Any,
    ) -> Tuple[List[Any], Any]:
        params: Dict[str, Any] = {
            "includeReplies": True,
            "pageSize": max(min(needed, 100), 1),
        }
        if state:
            params["cursor"] = state

        data = self._xquik_get(f"/x/users/{profile['username']}/tweets", params=params)
        tweets = data.get("tweets", [])
        next_cursor = data.get("next_cursor") if data.get("has_next_page") else None
        return tweets, next_cursor

    def _normalize_xquik(
        self,
        tweet: Dict[str, Any],
        profile: NormalizedProfile,
        **kwargs,
    ) -> NormalizedPost:
        cache = kwargs.get("cache")
        allow_ext = kwargs.get("allow_external_media", False)
        author = tweet.get("author") or {}
        author_handle = author.get("username") or profile["username"]

        media_items: List[NormalizedMedia] = []
        for media in tweet.get("media", []) or []:
            url = media.get("mediaUrl") or media.get("url")
            if not url:
                continue
            media_type = self._normalize_media_type(media.get("type"))
            local_path = None
            if cache:
                local_path = download_media(cache.base_dir, url, cache.is_offline, "twitter", None, allow_ext)
            media_items.append(
                NormalizedMedia(
                    url=url,
                    local_path=str(local_path) if local_path else None,
                    type=media_type,
                )
            )

        return NormalizedPost(
            platform="twitter",
            id=str(tweet.get("id", "")),
            created_at=self._parse_xquik_datetime(tweet.get("createdAt")),
            author_username=author_handle,
            text=tweet.get("text", ""),
            media=media_items,
            external_links=extract_and_resolve_urls(tweet.get("text", "")),
            post_url=tweet.get("url") or f"https://twitter.com/i/status/{tweet.get('id')}",
            metrics={
                "likes": tweet.get("likeCount", 0),
                "reposts": tweet.get("retweetCount", 0),
            },
            type="reply" if tweet.get("isReply") or tweet.get("inReplyToId") else "post",
        )

    def _parse_xquik_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    def _normalize_media_type(self, value: Optional[str]) -> str:
        if value == "photo":
            return "image"
        if value == "animated_gif":
            return "gif"
        return value or "image"

def fetch_data(**kwargs):
    return TwitterFetcher().fetch_data(kwargs.pop("username"), kwargs.pop("cache"), **kwargs)
