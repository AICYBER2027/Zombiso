"""Platform clients and the factory that builds them from environment variables."""

from __future__ import annotations

import os

from ..config import platform_secret
from .base import PlatformError, Publisher, PublishRequest, PublishResult
from .instagram import InstagramPublisher
from .tiktok import TikTokPublisher
from .youtube import YouTubePublisher


def make_publisher(platform: str, channel_key: str, *, audited: bool = False) -> Publisher:
    """Build the real client for one channel's account. Raises PlatformError if it is not configured."""
    if platform == "youtube":
        # .env values win; otherwise the account connected from the review page (stored in the data volume).
        from .. import accounts
        from ..config import Settings

        data_dir = Settings().data_dir
        client_id, client_secret = accounts.youtube_client(data_dir)
        return YouTubePublisher(client_id=client_id, client_secret=client_secret,
                                refresh_token=platform_secret("youtube", channel_key, "refresh_token")
                                or accounts.youtube_refresh_token(data_dir, channel_key),
                                audited=audited)
    if platform == "instagram":
        user_id = platform_secret("instagram", channel_key, "user_id")
        token = platform_secret("instagram", channel_key, "access_token")
        if not (user_id and token):
            from .. import accounts
            from ..config import Settings

            user_id, token = accounts.instagram_credentials(Settings().data_dir, channel_key)
        return InstagramPublisher(user_id=user_id, access_token=token)
    if platform == "tiktok":
        return TikTokPublisher(access_token=platform_secret("tiktok", channel_key, "access_token"), audited=audited)
    raise PlatformError(f"unknown platform {platform}")


__all__ = ["PlatformError", "Publisher", "PublishRequest", "PublishResult", "make_publisher",
           "YouTubePublisher", "InstagramPublisher", "TikTokPublisher"]
