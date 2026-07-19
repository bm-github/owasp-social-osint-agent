from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import tweepy

from socialosintagent.client_manager import ClientManager
from socialosintagent.exceptions import UserNotFoundError
from socialosintagent.platforms import twitter as twitter_fetcher


@pytest.fixture
def mock_tweepy_client(mocker):
    client = mocker.MagicMock(spec=tweepy.Client)
    client.bearer_token = "fake_bearer_token"
    
    mock_user = MagicMock(
        id=12345, name="Test User", username="testuser",
        created_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
        public_metrics={"followers_count": 100, "following_count": 50, "tweet_count": 10},
        description="A test user"
    )
    mock_tweet = MagicMock(spec=tweepy.Tweet)
    mock_tweet.id = 54321
    mock_tweet.author_id = 12345
    mock_tweet.text = "Hello world!"
    mock_tweet.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    mock_tweet.public_metrics = {}
    mock_tweet.attachments = None
    mock_tweet.in_reply_to_user_id = None
    
    client.get_user.return_value = MagicMock(data=mock_user)
    client.get_users_tweets.return_value = MagicMock(
        data=[mock_tweet], includes={}, meta={"next_token": None}
    )
    return client

@pytest.fixture
def mock_cache(mocker):
    return mocker.MagicMock()

def test_fetch_data_cache_miss(mock_tweepy_client, mock_cache):
    """Test fetch_data when no cache exists."""
    # Arrange
    mock_cache.load.return_value = None
    mock_cache.is_offline = False
    username = "testuser"

    # Act
    result = twitter_fetcher.fetch_data(
        client=mock_tweepy_client,
        username=username,
        cache=mock_cache,
        force_refresh=False,
        fetch_limit=50,
    )

    # Assert
    mock_cache.load.assert_called_once_with("twitter", username)
    mock_tweepy_client.get_user.assert_called_once()
    mock_tweepy_client.get_users_tweets.assert_called()
    mock_cache.save.assert_called_once()
    assert result is not None
    assert len(result["posts"]) == 1
    assert result["posts"][0]["text"] == "Hello world!"
    assert result["profile"]["username"] == "testuser"

def test_fetch_data_cache_hit_sufficient_items(mock_tweepy_client, mock_cache):
    """Test that API is not called when cache has enough items."""
    # Arrange
    cached_data = {
        "profile": {"id": "123"},
        "posts": [{"id": f"t{i}"} for i in range(50)],
    }
    mock_cache.load.return_value = cached_data
    username = "testuser"

    # Act
    twitter_fetcher.fetch_data(
        client=mock_tweepy_client, username=username, cache=mock_cache, fetch_limit=50
    )

    # Assert
    mock_tweepy_client.get_user.assert_not_called()
    mock_tweepy_client.get_users_tweets.assert_not_called()

def test_user_not_found(mock_tweepy_client, mock_cache):
    """Test that UserNotFoundError is raised for a non-existent user."""
    # Arrange
    mock_cache.load.return_value = None
    mock_cache.is_offline = False
    mock_tweepy_client.get_user.return_value = MagicMock(data=None)
    username = "nonexistent"

    # Act & Assert
    with pytest.raises(UserNotFoundError):
        twitter_fetcher.fetch_data(
            client=mock_tweepy_client,
            username=username,
            cache=mock_cache
        )

def test_fetch_data_xquik_backend(mocker, monkeypatch, mock_cache):
    """Test fetch_data with Xquik as the selected Twitter backend."""
    # Arrange
    monkeypatch.setenv("TWITTER_BACKEND", "xquik")
    monkeypatch.setenv("XQUIK_API_KEY", "test-key")
    monkeypatch.setenv("XQUIK_API_BASE_URL", "https://xquik.example/api/v1")
    mock_cache.load.return_value = None
    mock_cache.is_offline = False
    mock_cache.base_dir = mocker.MagicMock()

    profile_response = mocker.MagicMock()
    profile_response.json.return_value = {
        "id": "12345",
        "username": "testuser",
        "name": "Test User",
        "description": "A test user",
        "createdAt": "2022-01-01T00:00:00Z",
        "followers": 100,
        "statusesCount": 10,
        "location": "N/A",
    }

    tweets_response = mocker.MagicMock()
    tweets_response.json.return_value = {
        "tweets": [
            {
                "id": "54321",
                "text": "Hello world!",
                "createdAt": "2026-06-06T12:00:00Z",
                "likeCount": 2,
                "retweetCount": 1,
                "url": "https://x.com/testuser/status/54321",
                "author": {"username": "testuser"},
                "media": [
                    {"mediaUrl": "https://pbs.twimg.com/media/test.jpg", "type": "photo"}
                ],
            }
        ],
        "has_next_page": False,
        "next_cursor": None,
    }

    http_get = mocker.patch.object(
        twitter_fetcher.httpx,
        "get",
        side_effect=[profile_response, tweets_response],
    )
    mocker.patch.object(twitter_fetcher, "download_media", return_value=None)

    # Act
    result = twitter_fetcher.fetch_data(
        username="testuser",
        cache=mock_cache,
        force_refresh=False,
        fetch_limit=50,
        allow_external_media=False,
    )

    # Assert
    assert result is not None
    assert result["profile"]["username"] == "testuser"
    assert result["profile"]["metrics"]["followers"] == 100
    assert result["posts"][0]["id"] == "54321"
    assert result["posts"][0]["text"] == "Hello world!"
    assert result["posts"][0]["media"][0]["type"] == "image"
    assert http_get.call_args_list[0].kwargs["headers"] == {"x-api-key": "test-key"}
    assert (
        http_get.call_args_list[0].args[0]
        == "https://xquik.example/api/v1/x/users/testuser"
    )
    assert (
        http_get.call_args_list[1].args[0]
        == "https://xquik.example/api/v1/x/users/testuser/tweets"
    )
    assert http_get.call_args_list[1].kwargs["params"] == {
        "includeReplies": True,
        "pageSize": 50,
    }


def test_client_manager_accepts_xquik_twitter_backend(monkeypatch):
    """Test that Xquik credentials enable the Twitter platform."""
    # Arrange
    monkeypatch.delenv("TWITTER_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("TWITTER_BACKEND", "xquik")
    monkeypatch.setenv("XQUIK_API_KEY", "test-key")

    # Act
    manager = ClientManager(is_offline=True)

    # Assert
    assert "twitter" in manager.get_available_platforms()
    assert manager.get_platform_client("twitter") is None


def test_client_manager_rejects_incomplete_xquik_backend(monkeypatch):
    """Test that a native bearer token cannot mask a missing Xquik key."""
    # Arrange
    monkeypatch.setenv("TWITTER_BACKEND", "xquik")
    monkeypatch.delenv("XQUIK_API_KEY", raising=False)
    monkeypatch.setenv("TWITTER_BEARER_TOKEN", "native-token")
    manager = ClientManager(is_offline=True)

    # Act & Assert
    assert "twitter" not in manager.get_available_platforms()
    with pytest.raises(RuntimeError, match="XQUIK_API_KEY not set"):
        manager.get_platform_client("twitter")


def test_native_media_types_are_normalized(mock_tweepy_client, mock_cache, mocker):
    """Test that native Twitter media uses the normalized media vocabulary."""
    # Arrange
    mock_cache.load.return_value = None
    mock_cache.is_offline = False
    mock_cache.base_dir = mocker.MagicMock()
    tweet = mock_tweepy_client.get_users_tweets.return_value.data[0]
    tweet.attachments = {"media_keys": ["photo-key", "gif-key"]}
    photo = MagicMock(
        media_key="photo-key",
        type="photo",
        url="https://pbs.twimg.com/media/photo.jpg",
        preview_image_url=None,
    )
    animated_gif = MagicMock(
        media_key="gif-key",
        type="animated_gif",
        url=None,
        preview_image_url="https://pbs.twimg.com/media/animation.jpg",
    )
    mock_tweepy_client.get_users_tweets.return_value.includes = {
        "media": [photo, animated_gif]
    }
    mocker.patch.object(
        twitter_fetcher,
        "download_media",
        side_effect=["photo.jpg", "animation.jpg"],
    )

    # Act
    result = twitter_fetcher.fetch_data(
        client=mock_tweepy_client,
        username="testuser",
        cache=mock_cache,
        force_refresh=True,
        fetch_limit=5,
    )

    # Assert
    assert result is not None
    assert [media["type"] for media in result["posts"][0]["media"]] == [
        "image",
        "gif",
    ]
