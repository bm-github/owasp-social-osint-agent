import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import praw
import prawcore

from ..cache import CACHE_EXPIRY_HOURS, MAX_CACHE_ITEMS, CacheManager
from ..exceptions import (AccessForbiddenError, RateLimitExceededError,
                         UserNotFoundError)
from ..llm import LLMAnalyzer
from ..utils import SUPPORTED_IMAGE_EXTENSIONS, download_media, get_sort_key

logger = logging.getLogger("SocialOSINTAgent.platforms.reddit")

DEFAULT_FETCH_LIMIT = 50

def _extract_media_from_submission(submission: Any, cache: CacheManager, llm: LLMAnalyzer, username: str) -> List[Dict[str, Any]]:
    """
    Extracts, downloads, and analyzes media from a single Reddit submission.
    This is a stateless helper function.
    """
    media_items = []
    # Direct media link
    if any(submission.url.lower().endswith(ext) for ext in SUPPORTED_IMAGE_EXTENSIONS + [".mp4", ".webm"]):
        media_path = download_media(cache.base_dir, submission.url, cache.is_offline, "reddit")
        if media_path:
            analysis = llm.analyze_image(media_path, source_url=submission.url, context=f"Reddit user u/{username}'s post in r/{submission.subreddit.display_name}") if media_path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS else None
            media_items.append({"type": "image" if media_path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS else "video", "analysis": analysis, "url": submission.url, "local_path": str(media_path)})
    # Gallery
    elif getattr(submission, 'is_gallery', False) and getattr(submission, 'media_metadata', None):
        for media_id, media_item in submission.media_metadata.items():
            if 's' in media_item and 'u' in media_item['s']:
                url = media_item['s']['u']
                media_path = download_media(cache.base_dir, url, cache.is_offline, "reddit")
                if media_path:
                    analysis = llm.analyze_image(media_path, source_url=url, context=f"Reddit gallery post by u/{username}") if media_path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS else None
                    media_items.append({"type": "gallery_image", "analysis": analysis, "url": url, "local_path": str(media_path)})
    return media_items

def fetch_data(
    client: praw.Reddit,
    username: str,
    cache: CacheManager,
    llm: LLMAnalyzer,
    force_refresh: bool = False,
    fetch_limit: int = DEFAULT_FETCH_LIMIT,
) -> Optional[Dict[str, Any]]:
    """Fetches submissions, comments, and user profile for a Reddit user."""
    
    cached_data = cache.load("reddit", username)

    if cache.is_offline:
        return cached_data or {"timestamp": datetime.now(timezone.utc).isoformat(), "user_profile": {}, "submissions": [], "comments": [], "media_analysis": [], "media_paths": [], "stats": {}}
    
    cached_subs_count = len(cached_data.get("submissions", [])) if cached_data else 0
    cached_comms_count = len(cached_data.get("comments", [])) if cached_data else 0
    if not force_refresh and cached_data and (datetime.now(timezone.utc) - get_sort_key(cached_data, "timestamp")) < timedelta(hours=CACHE_EXPIRY_HOURS):
        if cached_subs_count >= fetch_limit and cached_comms_count >= fetch_limit:
            logger.info(f"Reddit cache for u/{username} is fresh and has enough items. Skipping.")
            return cached_data

    logger.info(f"Fetching Reddit data for u/{username} (Force Refresh: {force_refresh}, Limit: {fetch_limit})")
    
    all_submissions = cached_data.get("submissions", []) if not force_refresh and cached_data else []
    all_comments = cached_data.get("comments", []) if not force_refresh and cached_data else []
    
    sub_ids = {s['id'] for s in all_submissions}
    comment_ids = {c['id'] for c in all_comments}
    
    media_analysis_set = set(cached_data.get("media_analysis", [])) if cached_data and cached_data.get("media_analysis") else set()
    media_paths_set = set(cached_data.get("media_paths", [])) if cached_data and cached_data.get("media_paths") else set()
    
    user_profile = cached_data.get("user_profile") if not force_refresh and cached_data else None

    try:
        redditor = client.redditor(username)
        if not user_profile or force_refresh:
            user_profile = {
                "id": redditor.id, "name": redditor.name,
                "created_utc": datetime.fromtimestamp(redditor.created_utc, tz=timezone.utc).isoformat(),
                "link_karma": redditor.link_karma, "comment_karma": redditor.comment_karma,
                "icon_img": redditor.icon_img, "is_suspended": getattr(redditor, "is_suspended", False)
            }
        
        # Fetch Submissions
        if len(all_submissions) < fetch_limit:
            subs_params = {'limit': min(fetch_limit, 100)}
            for s in redditor.submissions.new(**subs_params):
                if s.id not in sub_ids:
                    sub_ids.add(s.id)
                    
                    submission_media = _extract_media_from_submission(s, cache, llm, username)
                    for media_item in submission_media:
                        if media_item.get("local_path"):
                            media_paths_set.add(media_item["local_path"])
                        if media_item.get("analysis"):
                            media_analysis_set.add(media_item["analysis"])
                    
                    all_submissions.append({
                        "id": s.id, "fullname": s.fullname, "title": s.title,
                        "text": s.selftext, "score": s.score, "upvote_ratio": s.upvote_ratio,
                        "subreddit": s.subreddit.display_name, "permalink": f"https://www.reddit.com{s.permalink}",
                        "created_utc": datetime.fromtimestamp(s.created_utc, tz=timezone.utc).isoformat(),
                        "link_url": s.url if not s.is_self else None,
                        "is_self": s.is_self, "over_18": s.over_18, "spoiler": s.spoiler,
                        "num_comments": s.num_comments, "media": submission_media
                    })
                if len(all_submissions) >= fetch_limit:
                    break

        # Fetch Comments
        if len(all_comments) < fetch_limit:
            comms_params = {'limit': min(fetch_limit, 100)}
            for c in redditor.comments.new(**comms_params):
                if c.id not in comment_ids:
                    comment_ids.add(c.id)
                    parent_author = c.submission.author.name if hasattr(c.submission, 'author') and c.submission.author else None
                    all_comments.append({
                        "id": c.id, "fullname": c.fullname, "text": c.body, "score": c.score,
                        "subreddit": c.subreddit.display_name, "permalink": f"https://www.reddit.com{c.permalink}",
                        "created_utc": datetime.fromtimestamp(c.created_utc, tz=timezone.utc).isoformat(),
                        "is_submitter": c.is_submitter, "parent_submission_author": parent_author
                    })
                if len(all_comments) >= fetch_limit:
                    break
        
        # Combine and save
        final_submissions = sorted(all_submissions, key=lambda x: get_sort_key(x, "created_utc"), reverse=True)[:max(fetch_limit, MAX_CACHE_ITEMS)]
        final_comments = sorted(all_comments, key=lambda x: get_sort_key(x, "created_utc"), reverse=True)[:max(fetch_limit, MAX_CACHE_ITEMS)]
        
        stats = {
            "total_submissions_cached": len(final_submissions),
            "total_comments_cached": len(final_comments),
            "submissions_with_media": len([s for s in final_submissions if s.get("media")]),
            "total_media_items_processed": len(media_paths_set),
            "avg_submission_score": round(sum(s.get("score", 0) for s in final_submissions) / max(1, len(final_submissions)), 2),
            "avg_comment_score": round(sum(c.get("score", 0) for c in final_comments) / max(1, len(final_comments)), 2),
            "avg_submission_upvote_ratio": round(sum(s.get("upvote_ratio", 0.0) or 0.0 for s in final_submissions) / max(1, len([s for s in final_submissions if s.get("upvote_ratio") is not None])), 3)
        }

        final_data = {
            "user_profile": user_profile, "submissions": final_submissions, "comments": final_comments,
            "media_analysis": sorted(list(media_analysis_set)), "media_paths": sorted(list(media_paths_set)), "stats": stats
        }
        cache.save("reddit", username, final_data)
        return final_data

    except prawcore.exceptions.NotFound:
        raise UserNotFoundError(f"Reddit user u/{username} not found.")
    except prawcore.exceptions.Forbidden:
        raise AccessForbiddenError(f"Access to Reddit user u/{username} is forbidden.")
    except prawcore.exceptions.RequestException as e:
        if hasattr(e, 'response') and e.response is not None and e.response.status_code == 429:
            raise RateLimitExceededError("Reddit API rate limit exceeded.") from e
        logger.error(f"Reddit request failed for u/{username}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching Reddit data for u/{username}: {e}", exc_info=True)
        return None