from . import bluesky, hackernews, mastodon, reddit, twitter

FETCHERS = {
    "twitter": twitter.fetch_data,
    "reddit": reddit.fetch_data,
    "bluesky": bluesky.fetch_data,
    "mastodon": mastodon.fetch_data,
    "hackernews": hackernews.fetch_data,
}