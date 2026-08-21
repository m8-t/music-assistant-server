"""Tests for Pocket Casts playback status handling."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import ProviderUnavailableError, RetriesExhausted

from music_assistant.providers.pocketcasts import PocketCastsProvider
from tests.common import use_real_create_task


@pytest.fixture
def client() -> AsyncMock:
    """Return a mocked Pocket Casts API client with no transcripts on offer."""
    client = AsyncMock()
    client.get_episode_transcripts.return_value = {}
    return client


@pytest.fixture
def provider(client: AsyncMock) -> PocketCastsProvider:
    """Return a PocketCastsProvider backed by the mocked API client and a cold cache."""
    mass = AsyncMock()
    # force a cache miss so the wrapped fetches always run
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    use_real_create_task(mass)
    manifest = MagicMock()
    manifest.domain = "pocketcasts"
    config = MagicMock()
    config.instance_id = "pocketcasts"
    config.get_value.return_value = None
    prov = PocketCastsProvider(mass, manifest, config)
    prov._client = client
    return prov


def _feed_episode(**overrides: Any) -> dict[str, Any]:
    """Build a full-podcast feed episode (snake_case schema, no playback status)."""
    return {
        "uuid": "episode-1",
        "title": "Episode 1",
        "url": "https://example.com/ep1.mp3",
        "file_type": "audio/mpeg",
        "duration": 1800,
        **overrides,
    }


async def test_sync_survives_episode_without_duration(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """A feed episode with a null duration must not abort the episode listing."""
    client.get_podcast_episodes.return_value = [
        _feed_episode(uuid="episode-1", duration=None),
        _feed_episode(uuid="episode-2", duration=1800),
    ]
    client.get_in_progress_episodes.return_value = []
    client.get_history.return_value = []

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert [episode.item_id for episode in episodes] == [
        "podcast-1:episode-1",
        "podcast-1:episode-2",
    ]
    assert episodes[0].duration == 0
    assert episodes[0].fully_played is False
    assert episodes[0].resume_position_ms == 0


async def test_sync_survives_null_status_fields(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """Null playedUpTo/duration on an in-progress entry must not abort the listing."""
    client.get_podcast_episodes.return_value = [_feed_episode(uuid="episode-1", duration=None)]
    client.get_in_progress_episodes.return_value = [
        {"uuid": "episode-1", "playedUpTo": None, "duration": None}
    ]
    client.get_history.return_value = []

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert len(episodes) == 1
    assert episodes[0].fully_played is False
    assert episodes[0].resume_position_ms == 0


async def test_sync_still_marks_played_episodes(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """An episode played past the threshold is still reported as fully played."""
    client.get_podcast_episodes.return_value = [_feed_episode(duration=1000)]
    client.get_in_progress_episodes.return_value = [
        {"uuid": "episode-1", "playedUpTo": 950, "duration": 1000}
    ]
    client.get_history.return_value = []

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert episodes[0].fully_played is True
    assert episodes[0].resume_position_ms == 0


async def test_sync_reports_resume_position(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """A partially played episode keeps its resume position."""
    client.get_podcast_episodes.return_value = [_feed_episode(duration=1000)]
    client.get_in_progress_episodes.return_value = [
        {"uuid": "episode-1", "playedUpTo": 300, "duration": 1000}
    ]
    client.get_history.return_value = []

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert episodes[0].fully_played is False
    assert episodes[0].resume_position_ms == 300000


async def test_get_podcast_episode_handles_null_fields(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """The single-episode endpoint must tolerate null duration/playedUpTo."""
    client.get_episode_details.return_value = {
        "uuid": "episode-1",
        "title": "Episode 1",
        "url": "https://example.com/ep1.mp3",
        "fileType": "audio/mpeg",
        "duration": None,
        "playedUpTo": None,
        "playingStatus": 1,
    }

    # call the undecorated function so the @use_cache wrapper stays out of the test
    get_podcast_episode = cast("Any", PocketCastsProvider.get_podcast_episode).__wrapped__
    episode = await get_podcast_episode(provider, "podcast-1:episode-1")

    assert episode.fully_played is False
    assert episode.resume_position_ms == 0


async def test_get_resume_position_handles_null_fields(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """A null playedUpTo/duration on an in-progress entry yields a zero resume point."""
    client.get_in_progress_episodes.return_value = [
        {"uuid": "episode-1", "playedUpTo": None, "duration": None}
    ]

    assert await provider.get_resume_position("podcast-1:episode-1", MediaType.PODCAST_EPISODE) == (
        False,
        0,
        None,
    )


async def test_sync_flags_episodes_that_have_a_transcript(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """The episode listing reports transcript availability without fetching any of them."""
    client.get_podcast_episodes.return_value = [
        _feed_episode(uuid="episode-1"),
        _feed_episode(uuid="episode-2"),
    ]
    client.get_in_progress_episodes.return_value = []
    client.get_history.return_value = []
    client.get_episode_transcripts.return_value = {
        "episode-1": [{"url": "https://example.com/ep1.vtt", "type": "text/vtt"}]
    }

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert [episode.metadata.has_transcript for episode in episodes] == [True, False]
    cast("MagicMock", provider.mass.http_session).get.assert_not_called()


async def test_sync_survives_unavailable_transcripts(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """A failing transcript lookup must not abort the episode listing."""
    client.get_podcast_episodes.return_value = [_feed_episode(uuid="episode-1")]
    client.get_in_progress_episodes.return_value = []
    client.get_history.return_value = []
    client.get_episode_transcripts.side_effect = ProviderUnavailableError("boom")

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert [episode.item_id for episode in episodes] == ["podcast-1:episode-1"]
    assert episodes[0].metadata.has_transcript is False


async def test_sync_survives_exhausted_transcript_retries(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """A throttled-out transcript lookup must not abort the episode listing either."""
    client.get_podcast_episodes.return_value = [_feed_episode(uuid="episode-1")]
    client.get_in_progress_episodes.return_value = []
    client.get_history.return_value = []
    client.get_episode_transcripts.side_effect = RetriesExhausted("gave up")

    episodes = [episode async for episode in provider.get_podcast_episodes("podcast-1")]

    assert [episode.item_id for episode in episodes] == ["podcast-1:episode-1"]


async def test_episode_transcript_is_fetched_on_demand(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """The transcript is retrieved through its own call, not with the episode."""
    client.get_episode_transcripts.return_value = {
        "episode-1": [{"url": "https://example.com/ep1.vtt", "type": "text/vtt"}]
    }
    with patch(
        "music_assistant.providers.pocketcasts.get_episode_transcript",
        AsyncMock(return_value=("Some words.", [])),
    ) as fetch:
        text, _ = await provider.get_podcast_episode_transcript("podcast-1:episode-1")

    assert text == "Some words."
    assert fetch.await_args is not None
    assert fetch.await_args.kwargs["transcripts"] == [
        {"url": "https://example.com/ep1.vtt", "type": "text/vtt"}
    ]


async def test_episode_transcript_absent_yields_nothing(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """An episode without a transcript returns nothing rather than raising."""
    client.get_episode_transcripts.return_value = {}
    assert await provider.get_podcast_episode_transcript("podcast-1:episode-1") == (None, None)


async def test_episode_no_longer_carries_the_transcript(
    provider: PocketCastsProvider, client: AsyncMock
) -> None:
    """Fetching an episode reports transcript availability without the transcript itself."""
    client.get_episode_details.return_value = {
        "uuid": "episode-1",
        "title": "Episode 1",
        "url": "https://example.com/ep1.mp3",
        "duration": 1800,
    }
    client.get_episode_transcripts.return_value = {
        "episode-1": [{"url": "https://example.com/ep1.vtt", "type": "text/vtt"}]
    }

    episode = await provider.get_podcast_episode("podcast-1:episode-1")

    assert episode.metadata.has_transcript is True
    assert episode.metadata.transcript is None
    assert episode.metadata.transcript_cues is None
