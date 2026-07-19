import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import praw
import tweepy
from atproto import Client
from atproto import exceptions as atproto_exceptions
from mastodon import Mastodon
from socialosintagent.platforms.twitter import uses_xquik_backend

logger = logging.getLogger("SocialOSINTAgent.ClientManager")

class ClientManager:
    """Handles the creation and management of API clients for social media platforms."""
    def __init__(self, is_offline: bool):
        self.is_offline = is_offline
        self._twitter: Optional[tweepy.Client] = None
        self._reddit: Optional[praw.Reddit] = None
        self._bluesky: Optional[Client] = None
        self._mastodon_clients: Dict[str, Mastodon] = {}
        self._default_mastodon_lookup_client: Optional[Mastodon] = None
        self._mastodon_clients_initialized: bool = False

    @property
    def twitter_client(self) -> Optional[tweepy.Client]:
        if uses_xquik_backend():
            if not os.getenv("XQUIK_API_KEY"):
                raise RuntimeError("XQUIK_API_KEY not set.")
            return None

        if self._twitter is None:
            token = os.environ.get("TWITTER_BEARER_TOKEN")
            if not token: raise RuntimeError("TWITTER_BEARER_TOKEN not set.")
            self._twitter = tweepy.Client(bearer_token=token, wait_on_rate_limit=False)
            if not self.is_offline:
                try:
                    self._twitter.get_user(username="twitterdev", user_fields=["id"])
                except tweepy.errors.TweepyException as e:
                    logger.warning(f"Could not verify Twitter client credentials: {e}")
        return self._twitter

    @property
    def reddit_client(self) -> praw.Reddit:
        if self._reddit is None:
            if not all(os.getenv(k) for k in ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"]):
                raise RuntimeError("Reddit credentials not fully set.")
            self._reddit = praw.Reddit(
                client_id=os.environ["REDDIT_CLIENT_ID"],
                client_secret=os.environ["REDDIT_CLIENT_SECRET"],
                user_agent=os.environ["REDDIT_USER_AGENT"],
                read_only=True,
            )
        return self._reddit

    @property
    def bluesky_client(self) -> Client:
        if self._bluesky is None:
            if not all(os.getenv(k) for k in ["BLUESKY_IDENTIFIER", "BLUESKY_APP_SECRET"]):
                raise RuntimeError("Bluesky credentials not set.")
            client = Client()
            if not self.is_offline:
                try:
                    client.login(os.environ["BLUESKY_IDENTIFIER"], os.environ["BLUESKY_APP_SECRET"])
                except atproto_exceptions.AtProtocolError as e:
                    logger.warning(f"Could not verify Bluesky client credentials: {e}")
            self._bluesky = client
        return self._bluesky

    def get_mastodon_clients(self) -> Tuple[Dict[str, Mastodon], Optional[Mastodon]]:
        if not self._mastodon_clients_initialized:
            logger.info("Initializing Mastodon clients from environment variables...")
            i = 1
            while True:
                base_url_var = f"MASTODON_INSTANCE_{i}_URL"
                token_var = f"MASTODON_INSTANCE_{i}_TOKEN"
                default_var = f"MASTODON_INSTANCE_{i}_DEFAULT"
                url = os.getenv(base_url_var)
                token = os.getenv(token_var)
                if not url: break
                if not token:
                    logger.warning(f"Found {base_url_var} but missing {token_var}. Skipping instance {i}.")
                    i += 1
                    continue
                try:
                    client = Mastodon(access_token=token, api_base_url=url)
                    if not self.is_offline: client.instance()
                    self._mastodon_clients[url.rstrip('/')] = client
                    logger.info(f"Successfully initialized Mastodon client for {url}")
                    if os.getenv(default_var, 'false').lower() == 'true':
                        self._default_mastodon_lookup_client = client
                        logger.info(f"Set {url} as the default Mastodon lookup instance.")
                except Exception as e:
                    logger.error(f"Failed to initialize Mastodon instance {url}: {e}")
                i += 1
            if not self._default_mastodon_lookup_client and self._mastodon_clients:
                self._default_mastodon_lookup_client = next(iter(self._mastodon_clients.values()))
                logger.info("No default Mastodon instance specified, using first available.")
            self._mastodon_clients_initialized = True
        return self._mastodon_clients, self._default_mastodon_lookup_client

    def get_platform_client(self, platform: str) -> Any:
        try:
            if platform == "twitter": return self.twitter_client
            if platform == "reddit": return self.reddit_client
            if platform == "bluesky": return self.bluesky_client
            if platform == "mastodon": return self.get_mastodon_clients()
            if platform == "github": return None 
        except (RuntimeError, tweepy.errors.TweepyException, praw.exceptions.PRAWException, atproto_exceptions.AtProtocolError) as e:
            raise RuntimeError(f"Failed to initialize client for {platform}: {e}")
        return None

    def get_available_platforms(self, check_creds=True) -> List[str]:
        available = []
        uses_xquik = uses_xquik_backend()
        twitter_available = (
            not check_creds
            or (uses_xquik and os.getenv("XQUIK_API_KEY"))
            or (not uses_xquik and os.getenv("TWITTER_BEARER_TOKEN"))
        )
        if twitter_available:
            available.append("twitter")
        if not check_creds or all(os.getenv(k) for k in ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"]): available.append("reddit")
        if not check_creds or all(os.getenv(k) for k in ["BLUESKY_IDENTIFIER", "BLUESKY_APP_SECRET"]): available.append("bluesky")
        if not check_creds or os.getenv("MASTODON_INSTANCE_1_URL"): available.append("mastodon")
        
        available.append("github")
        available.append("hackernews")
        return sorted(list(set(available)))
