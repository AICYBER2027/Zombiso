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
        return YouTubePublisher(client_id=os.environ.get("YOUTUBE_CLIENT_ID", ""),
                                client_secret=os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
                                refresh_token=platform_secret("youtube", channel_key, "refresh_token"),
                                audited=audited)
    if platform == "instagram":
        return InstagramPublisher(user_id=platform_secret("instagram", channel_key, "user_id"),
                                  access_token=platform_secret("instagram", channel_key, "access_token"))
    if platform == "tiktok":
        return TikTokPublisher(access_token=platform_secret("tiktok", channel_key, "access_token"), audited=audited)
    raise PlatformError(f"unknown platform {platform}")


__all__ = ["PlatformError", "Publisher", "PublishRequest", "PublishResult", "make_publisher",
           "YouTubePublisher", "InstagramPublisher", "TikTokPublisher"]
