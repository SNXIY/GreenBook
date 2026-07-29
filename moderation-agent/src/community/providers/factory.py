from community.providers.base import CommunityDataProvider
from community.providers.java import JavaCommunityDataProvider


def create_community_provider(
    *,
    java_base_url: str | None = None,
    java_auth_token: str | None = None,
    timeout: float = 10.0,
) -> CommunityDataProvider:
    if not java_base_url:
        raise ValueError("JAVA_COMMUNITY_BASE_URL is required")
    return JavaCommunityDataProvider(
        base_url=java_base_url,
        auth_token=java_auth_token,
        timeout=timeout,
    )
