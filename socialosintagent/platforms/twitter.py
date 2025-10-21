import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import tweepy

from ..cache import CACHE_EXPIRY_HOURS, MAX_CACHE_ITEMS, CacheManager
from ..exceptions import (AccessForbiddenError, RateLimitExceededError,
                         UserNotFoundError)
from ..llm import LLMAnalyzer
from ..utils import SUPPORTED_IMAGE_EXTENSIONS, download_media, get_sort_key

logger = logging.getLogger("SociaAgent.platforms.twitter")

DEFAULT_FETCH_LIMIT = 50

def fetch_data(
    client: tweepy.Client,
    username: str,
    cache: CacheManager,
    llm: LLMAnalyzer,
    force_refresh: bool = False,
    fetch_limit: int = DEFAULT_FETCH_LIMIT,
) -> Optional[Dict[str, Any]]:
    """Fetches tweets and user info for a Twitter user."""
    
    cached_data = cache.load("twitter", username)

    if cache.is_offline:
        return cached_data or {"timestamp": datetime.now(timezone.utc).isoformat(), "user_info": {}, "tweets": [], "media_analysis": [], "media_paths": []}

    if not force_refresh and cached_data and (datetime.now(timezone.utc) - get_sort_key(cached_data, "timestamp")) < timedelta(hours=CACHE_EXPIRY_HOURS):
        if len(cached_data.get("tweets", [])) >= fetch_limit:
            logger.info(f"Twitter cache for @{username} is fresh and has enough items ({len(cached_data.get('tweets', []))}/{fetch_limit}). Skipping fetch.")
            return cached_data

    logger.info(f"Fetching Twitter data for @{username} (Force Refresh: {force_refresh}, Limit: {fetch_limit})")
    
    # --- START OF EFFICIENCY FIX ---
    
    since_id = None       # For fetching NEWER tweets than cache
    pagination_token = None # For fetching OLDER tweets (pagination)

    existing_tweets = cached_data.get("tweets", []) if not force_refresh and cached_data else []
    
    is_load_more_scenario = not force_refresh and cached_data and fetch_limit > len(existing_tweets)
    is_incremental_update_scenario = not force_refresh and cached_data and not is_load_more_scenario

    if is_incremental_update_scenario and existing_tweets:
        # We have a fresh cache and just want to get anything newer.
        since_id = existing_tweets[0].get("id")
        logger.debug(f"Incremental update for @{username}, using since_id: {since_id}")
    elif is_load_more_scenario and existing_tweets:
        # We want to fetch older tweets than what we have. We don't use since_id.
        # We will paginate from the beginning until we hit the new limit.
        # A more advanced pagination using the oldest ID is possible but complex with Twitter's v2 pagination tokens.
        # For now, we accept re-fetching the first page to get subsequent pages.
        logger.debug(f"Load more scenario for @{username}. Current: {len(existing_tweets)}, Target: {fetch_limit}. Will fetch pages from newest.")
    
    # --- END OF EFFICIENCY FIX ---

    existing_media_analysis = cached_data.get("media_analysis", []) if not force_refresh and cached_data else []
    existing_media_paths = cached_data.get("media_paths", []) if not force_refresh and cached_data else []
    user_info = cached_data.get("user_info") if not force_refresh and cached_data else None

    try:
        if not user_info or force_refresh:
            user_response = client.get_user(
                username=username,
                user_fields=["created_at", "public_metrics", "profile_image_url", "verified", "description", "location"]
            )
            if not user_response or not user_response.data:
                raise UserNotFoundError(f"Twitter user @{username} not found.")
            user = user_response.data
            user_info = {
                "id": str(user.id), "name": user.name, "username": user.username,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "public_metrics": user.public_metrics, "profile_image_url": user.profile_image_url,
                "verified": user.verified, "description": user.description, "location": user.location
            }

        user_id = user_info["id"]
        new_tweets_data = []
        new_media_includes = {}
        all_users_from_includes = {}
        all_tweets_from_includes = {}
        
        tweets_fetch_count = 0
        
        # In a "load more" scenario, our fetched tweets will overlap with existing ones.
        # We will collect all and then deduplicate.
        if is_load_more_scenario:
            tweets_fetch_count = len(existing_tweets)
            new_tweets_data.extend(existing_tweets) # Start with what we have

        while tweets_fetch_count < fetch_limit:
            current_page_limit = min(fetch_limit - tweets_fetch_count, 100)
            if current_page_limit <= 0: break
            
            expansions = [
                "attachments.media_keys", "author_id", "in_reply_to_user_id", 
                "referenced_tweets.id", "referenced_tweets.id.author_id"
            ]
            
            # --- START OF EFFICIENCY FIX 2 ---
            # When paginating backwards, the token handles fetching the next OLDER page.
            # When doing an incremental update, since_id handles fetching NEWER tweets.
            tweets_response = client.get_users_tweets(
                id=user_id, max_results=current_page_limit, 
                since_id=since_id, # Will be None unless it's an incremental update
                pagination_token=pagination_token, # Will be None on first page load
                tweet_fields=["created_at", "public_metrics", "attachments", "entities", "conversation_id", "in_reply_to_user_id", "referenced_tweets"],
                expansions=expansions,
                media_fields=["url", "preview_image_url", "type", "media_key", "width", "height", "alt_text"],
                user_fields=["username", "name", "id"]
            )
            # --- END OF EFFICIENCY FIX 2 ---
            
            if tweets_response.data:
                # Add only new tweets to avoid double-counting for the loop condition
                unique_new_tweets_on_page = [t for t in tweets_response.data if t.id not in {tw.get('id') for tw in new_tweets_data}]
                new_tweets_data.extend(unique_new_tweets_on_page)
                tweets_fetch_count += len(unique_new_tweets_on_page)
            
            if tweets_response.includes:
                if "media" in tweets_response.includes:
                    if "media" not in new_media_includes: new_media_includes["media"] = []
                    new_media_includes["media"].extend(tweets_response.includes["media"])
                if "users" in tweets_response.includes:
                    for user_obj in tweets_response.includes["users"]:
                        all_users_from_includes[str(user_obj.id)] = {"id": str(user_obj.id), "username": user_obj.username, "name": user_obj.name}
                if "tweets" in tweets_response.includes:
                    for included_tweet in tweets_response.includes["tweets"]:
                        all_tweets_from_includes[str(included_tweet.id)] = included_tweet
            
            pagination_token = tweets_response.meta.get("next_token")
            if not pagination_token:
                break
            
            # If we were only doing an incremental update, we only needed the first page.
            if is_incremental_update_scenario:
                break

        logger.info(f"Fetched/processed {tweets_fetch_count} total tweets for @{username}.")
        processed_new_tweets = []
        newly_added_media_analysis = []
        newly_added_media_paths = set()
        all_media_objects = {m.media_key: m for m in new_media_includes.get("media", [])}

        auth_details = {"bearer_token": client.bearer_token}

        # We process the new_tweets_data which now contains a mix of old and new tweets
        # The final de-duplication will happen at the end.
        for tweet_obj in new_tweets_data:
            # The list might contain dicts from cache or tweepy.Tweet objects from API
            tweet_id = str(tweet_obj.id) if isinstance(tweet_obj, tweepy.Tweet) else tweet_obj.get('id')
            
            # Convert tweepy.Tweet object to our dictionary format if necessary
            if isinstance(tweet_obj, tweepy.Tweet):
                tweet = tweet_obj
                media_items_for_tweet = []
                if tweet.attachments and "media_keys" in tweet.attachments:
                    for media_key in tweet.attachments["media_keys"]:
                        media = all_media_objects.get(media_key)
                        if media:
                            url = media.url if media.type in ["photo", "gif"] and media.url else media.preview_image_url
                            if url:
                                media_path = download_media(cache.base_dir, url, cache.is_offline, "twitter", auth_details)
                                if media_path:
                                    analysis = llm.analyze_image(media_path, source_url=url, context=f"Twitter user @{username}'s tweet (ID: {tweet.id})") if media_path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS else None
                                    media_items_for_tweet.append({"type": media.type, "analysis": analysis, "url": url, "alt_text": media.alt_text, "local_path": str(media_path)})
                                    if analysis: newly_added_media_analysis.append(analysis)
                                    newly_added_media_paths.add(str(media_path))
                
                replied_to_user_info = all_users_from_includes.get(str(tweet.in_reply_to_user_id)) if tweet.in_reply_to_user_id else None
                
                referenced_tweets_info = []
                quoted_tweet_info = None
                if tweet.referenced_tweets:
                    for ref in tweet.referenced_tweets:
                        referenced_tweets_info.append({"type": ref.type, "id": str(ref.id)})
                        if ref.type == 'quoted':
                            quoted_tweet_obj = all_tweets_from_includes.get(str(ref.id))
                            if quoted_tweet_obj and quoted_tweet_obj.author_id:
                                author_id_str = str(quoted_tweet_obj.author_id)
                                author_info = all_users_from_includes.get(author_id_str)
                                if author_info:
                                    quoted_tweet_info = {
                                        "tweet_id": str(quoted_tweet_obj.id),
                                        "author": author_info
                                    }

                tweet_data = {
                    "id": str(tweet.id), "text": tweet.text, "created_at": tweet.created_at.isoformat(),
                    "metrics": tweet.public_metrics, "entities_raw": tweet.entities,
                    "mentions": [{"username": m["username"], "id": str(m["id"])} for m in (tweet.entities or {}).get("mentions", [])],
                    "conversation_id": str(tweet.conversation_id),
                    "in_reply_to_user_id": str(tweet.in_reply_to_user_id) if tweet.in_reply_to_user_id else None,
                    "replied_to_user_info": replied_to_user_info,
                    "referenced_tweets": referenced_tweets_info,
                    "quoted_tweet_info": quoted_tweet_info,
                    "media": media_items_for_tweet
                }
                processed_new_tweets.append(tweet_data)
            else:
                # This is already a dict from the cache
                processed_new_tweets.append(tweet_obj)


        # Final combination and de-duplication logic
        unique_tweets = {t['id']: t for t in processed_new_tweets}
        final_tweets = sorted(list(unique_tweets.values()), key=lambda x: get_sort_key(x, "created_at"), reverse=True)[:max(fetch_limit, MAX_CACHE_ITEMS)]
        
        final_media_analysis = sorted(list(set(newly_added_media_analysis + existing_media_analysis)))
        final_media_paths = sorted(list(newly_added_media_paths.union(existing_media_paths)))

        final_data = {
            "user_info": user_info, 
            "tweets": final_tweets, 
            "media_analysis": final_media_analysis, 
            "media_paths": final_media_paths[:MAX_CACHE_ITEMS*2]
        }
        cache.save("twitter", username, final_data)
        logger.info(f"Successfully updated Twitter cache for @{username}. Total tweets cached: {len(final_data['tweets'])}")
        return final_data

    except tweepy.TooManyRequests as e:
        raise RateLimitExceededError("Twitter API rate limit exceeded.")
    except tweepy.errors.NotFound:
        raise UserNotFoundError(f"Twitter user @{username} not found.")
    except tweepy.errors.Forbidden as e:
        raise AccessForbiddenError(f"Access forbidden to @{username}'s tweets (protected/suspended). Reason: {e}")
    except Exception as e:
        logger.error(f"Unexpected error fetching Twitter data for @{username}: {e}", exc_info=True)
        return None