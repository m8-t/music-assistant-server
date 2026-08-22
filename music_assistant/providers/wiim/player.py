"""Wiim Player implementation."""

from __future__ import annotations

import time
import typing
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import IdentifierType, PlaybackState, PlayerFeature, PlayerType
from music_assistant_models.player import DeviceInfo
from wiim import PlayingStatus, WiimDevice
from wiim.exceptions import WiimDeviceException, WiimRequestException
from wiim.models import WiimGroupRole

from music_assistant.constants import create_sample_rates_config_entry
from music_assistant.helpers.upnp import create_didl_metadata
from music_assistant.models.player import Player, PlayerMedia

from .constants import (
    INPUT_MODE_SOURCES,
    PASSIVE_SOURCES,
    PLAYER_ID_PREFIX,
    SOURCE_AIRPLAY,
    SOURCE_ID_TO_INPUT_MODE,
    SOURCE_NETWORK,
    SOURCE_SPOTIFY,
    SOURCE_UNKNOWN,
)

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpService, UpnpStateVariable
    from music_assistant_models.config_entries import ConfigEntry, ConfigValueType

    from .provider import WiimProvider

SDK_TO_MA_STATE: dict[PlayingStatus, PlaybackState] = {
    PlayingStatus.PLAYING: PlaybackState.PLAYING,
    PlayingStatus.PAUSED: PlaybackState.PAUSED,
    PlayingStatus.STOPPED: PlaybackState.IDLE,
    PlayingStatus.LOADING: PlaybackState.PLAYING,
}

COMMAND_IN_FLIGHT_WINDOW = 0.8  # seconds
COMMAND_IN_FLIGHT_WINDOW_PLAY_MEDIA = 2.0  # seconds
PREVIOUS_PRESS_WINDOW = 10.0  # seconds - generous window for natural user pacing
STREAM_REQUEST_WAIT = 0.5  # seconds - device stream re-request observation window


class WiimPlayer(Player):
    """Wiim Player in Music Assistant."""

    def __init__(
        self,
        provider: WiimProvider,
        player_id: str,
        device: WiimDevice,
        mac_address: str | None = None,
    ) -> None:
        """Initialize the Player."""
        super().__init__(provider, player_id)

        self._attr_name = device.name
        self._attr_type = PlayerType.PLAYER
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.ENQUEUE,
            PlayerFeature.GAPLESS_PLAYBACK,
            PlayerFeature.SEEK,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
            PlayerFeature.SET_MEMBERS,
            PlayerFeature.SELECT_SOURCE,
        }
        self._attr_can_group_with = {provider.instance_id}
        self.device = device
        self._wiim_controller = provider.wiim_controller

        self._attr_device_info = DeviceInfo(
            model=device.model_name,
            manufacturer=device.manufacturer or "WiiM",
            software_version=device.firmware_version,
        )
        self._attr_device_info.add_identifier(IdentifierType.UUID, device.udn.removeprefix("uuid:"))
        if device.ip_address:
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, device.ip_address)
        if mac_address:
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, mac_address)

        device.general_event_callback = self._handle_sdk_general_device_update
        device.rendering_control_event_callback = self._handle_sdk_rendering_control_event
        device.av_transport_event_callback = self._handle_sdk_av_transport_event
        device.play_queue_event_callback = self._handle_sdk_play_queue_event

        self._last_logged_sdk_uri: str | None = None
        self._last_logged_sdk_status: PlayingStatus | None = None
        self._last_seen_device_uri: str | None = None
        self._command_in_flight_deadline: float = 0.0
        self._previous_press_deadline: float = 0.0
        self._pending_press_at: float = 0.0
        self._pending_press_loadings: int = 0
        self._pending_press_requests: int = 0

    # --- Lifecycle ---

    async def setup(self) -> None:
        """Handle logic when the player is set up in the Player controller."""
        for mode_name in self.device.supported_input_modes:
            if mode_name in INPUT_MODE_SOURCES and mode_name != SOURCE_NETWORK:
                self._attr_source_list.append(INPUT_MODE_SOURCES[mode_name])
        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_AIRPLAY])
        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_SPOTIFY])
        self._attr_source_list.append(PASSIVE_SOURCES[SOURCE_UNKNOWN])
        self._attr_needs_poll = True
        self._attr_poll_interval = 5

    async def poll(self) -> None:
        """Poll player for state updates to prevent position drift."""
        await self._sync_position()
        self._attr_poll_interval = 5 if self._attr_playback_state == PlaybackState.PLAYING else 30

    async def on_unload(self) -> None:
        """Handle logic when the player is unloaded from the Player controller."""
        self.device.general_event_callback = None
        self.device.av_transport_event_callback = None
        self.device.rendering_control_event_callback = None
        self.device.play_queue_event_callback = None
        try:
            await self._wiim_controller.remove_device(self.device.udn)
            await self.device.disconnect()
        except Exception:
            self.logger.exception("Error tearing down WiiM device %s", self.name)
        self.logger.debug("Player %s unloaded, SDK resources released", self.name)

    async def get_config_entries(
        self,
        action: str | None = None,
        values: dict[str, ConfigValueType] | None = None,
    ) -> list[ConfigEntry]:
        """Return player-specific config entries."""
        return [
            create_sample_rates_config_entry(
                max_sample_rate=192000,
                safe_max_sample_rate=192000,
                max_bit_depth=24,
                safe_max_bit_depth=24,
            ),
        ]

    # --- Player commands ---

    async def play_media(self, media: PlayerMedia) -> None:
        """Play media command."""
        stream_url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url=stream_url)
        self.set_current_media(
            uri=stream_url,
            title=media.title,
            artist=media.artist,
            album=media.album,
            image_url=media.image_url,
            duration=media.duration,
            source_id=media.source_id,
            clear_all=True,
        )
        self._command_in_flight_deadline = time.monotonic() + COMMAND_IN_FLIGHT_WINDOW_PLAY_MEDIA
        try:
            await self.device.async_play(uri=stream_url, metadata=didl_metadata)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("play_media", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def enqueue_next_media(self, media: PlayerMedia) -> None:
        """Handle enqueuing of the next queue item on the player."""
        stream_url = await self.mass.streams.resolve_stream_url(self.player_id, media)
        didl_metadata = create_didl_metadata(media, url=stream_url)
        self.logger.debug(
            "enqueue_next_media on %s: queue_item_id=%s, uri=%s",
            self._attr_name,
            media.queue_item_id,
            stream_url,
        )
        try:
            await self.device._invoke_upnp_action(
                "AVTransport",
                "SetNextAVTransportURI",
                NextURI=stream_url,
                NextURIMetaData=didl_metadata,
            )
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.warning("Enqueue failed on %s: %s", self._attr_name, err)

    async def play(self) -> None:
        """Play command."""
        self._command_in_flight_deadline = time.monotonic() + COMMAND_IN_FLIGHT_WINDOW
        try:
            await self.device.async_play()
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("play", err)
            return
        await self._sync_position()

    async def pause(self) -> None:
        """Pause command."""
        self._command_in_flight_deadline = time.monotonic() + COMMAND_IN_FLIGHT_WINDOW
        try:
            await self.device.async_pause()
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("pause", err)
            return
        await self._sync_position()

    async def stop(self) -> None:
        """Stop command."""
        self._attr_active_source = None
        self._attr_current_media = None
        self._command_in_flight_deadline = time.monotonic() + COMMAND_IN_FLIGHT_WINDOW
        try:
            await self.device.async_stop()
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("stop", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def seek(self, position: int) -> None:
        """Seek to position in seconds."""
        self._command_in_flight_deadline = time.monotonic() + COMMAND_IN_FLIGHT_WINDOW
        try:
            await self.device.async_seek(position)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("seek", err)

    async def volume_set(self, volume_level: int) -> None:
        """Handle VOLUME_SET command on the player."""
        try:
            # Remove and uncomment when wiim-sdk 0.1.5+ has been released
            # await self.device.async_set_volume(volume_level)
            # Inlined fix from https://github.com/Linkplay2020/wiim/pull/18:
            # async_set_volume uses a UPnP action that is unreliable; the HTTP
            # command works. Replicates the SDK method body until 0.1.5 ships.
            volume_level = max(0, min(100, volume_level))
            await self.device._http_command_ok("setPlayerCmd:vol:{}", str(volume_level))
            self.device.volume = volume_level
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("volume_set", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def volume_mute(self, muted: bool) -> None:
        """Handle VOLUME MUTE command on the player."""
        try:
            await self.device.async_set_mute(muted)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("volume_mute", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def select_source(self, source: str) -> None:
        """Handle SELECT SOURCE command on the player.

        :param source: The source(id) to select, as defined in the source_list.
        """
        sdk_mode = SOURCE_ID_TO_INPUT_MODE.get(source)
        if not sdk_mode:
            self.logger.warning("Unknown source '%s' for %s", source, self.display_name)
            return
        self._command_in_flight_deadline = time.monotonic() + COMMAND_IN_FLIGHT_WINDOW
        try:
            await self.device.async_set_play_mode(sdk_mode)
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("select_source", err)
            return
        self._update_ma_state_from_sdk_cache()

    async def set_members(
        self,
        player_ids_to_add: list[str] | None = None,
        player_ids_to_remove: list[str] | None = None,
    ) -> None:
        """Handle SET_MEMBERS command on the player."""
        queue = self.mass.player_queues.get(self.player_id)
        entry_sdk_uri = self.device.current_media.uri if self.device.current_media else None
        self.logger.debug(
            "set_members entry on %s: add=%s, remove=%s | "
            "queue.current_item_id=%s, queue.next_item_id=%s, "
            "queue.next_item_id_enqueued=%s | "
            "sdk.current_uri=%s, sdk.playing_status=%s",
            self._attr_name,
            player_ids_to_add,
            player_ids_to_remove,
            queue.current_item.queue_item_id if queue and queue.current_item else None,
            queue.next_item.queue_item_id if queue and queue.next_item else None,
            queue.next_item_id_enqueued if queue else None,
            entry_sdk_uri,
            self.device.playing_status,
        )
        try:
            if player_ids_to_add:
                follower_udns = [
                    cast("WiimPlayer", member).device.udn
                    for member_id in player_ids_to_add
                    if (member := self.mass.players.get_player(member_id))
                ]
                if follower_udns:
                    await self._wiim_controller.async_join_group(self.device.udn, follower_udns)

            if player_ids_to_remove:
                for member_id in player_ids_to_remove:
                    if member := self.mass.players.get_player(member_id):
                        await self._wiim_controller.async_ungroup_device(
                            cast("WiimPlayer", member).device.udn
                        )
        except (WiimDeviceException, WiimRequestException) as err:
            self._handle_command_error("set_members", err)
        finally:
            exit_sdk_uri = self.device.current_media.uri if self.device.current_media else None
            self.logger.debug(
                "set_members exit on %s: sdk.current_uri=%s, sdk.playing_status=%s, "
                "queue.current_item_id=%s",
                self._attr_name,
                exit_sdk_uri,
                self.device.playing_status,
                queue.current_item.queue_item_id if queue and queue.current_item else None,
            )

    # --- SDK event handlers ---

    def _handle_sdk_general_device_update(self, device: WiimDevice) -> None:
        """Handle general updates from the SDK (availability changes)."""
        if not device.available:
            self.logger.debug("Device %s became unavailable", self._attr_name)
            self._update_ma_state_from_sdk_cache()
            return
        if device.supports_http_api:
            self.logger.debug("Device %s available, ensuring subscriptions", self._attr_name)
            self.mass.create_task(self._ensure_subscriptions_and_update())
        else:
            self._update_ma_state_from_sdk_cache()

    def _handle_sdk_av_transport_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle AVTransport events from the SDK."""
        event_data = self.device.event_data
        if transport_state := event_data.get("TransportState"):
            try:
                sdk_status = PlayingStatus(transport_state)
            except ValueError:
                pass
            else:
                if sdk_status in (
                    PlayingStatus.PLAYING,
                    PlayingStatus.PAUSED,
                    PlayingStatus.LOADING,
                ):
                    self.mass.create_task(self._sync_position())
                    if sdk_status == PlayingStatus.LOADING:
                        self._detect_remote_button_press()
        self._update_ma_state_from_sdk_cache()

    def on_stream_requested(self, queue_item_id: str) -> None:
        """
        Handle stream re-request signal for remote button detection.

        When the device sends a GET request for the stream, check if we have
        an open press window and if the requested item is the current item.
        A re-request for the current item indicates a PREVIOUS button press.

        :param queue_item_id: Unique identifier of the queue item being requested.
        """
        # Check if we have an open press window
        if time.monotonic() >= self._pending_press_at:
            return

        # Check if the requested item is the current item (stream re-request)
        ma_queue = self.mass.player_queues.get(self.player_id)
        if not ma_queue or not ma_queue.current_item:
            return

        if ma_queue.current_item.queue_item_id == queue_item_id:
            # Stream re-request for current item detected — increment counter
            self._pending_press_requests += 1
            self.logger.debug(
                "Stream re-request for current item on %s (request #%d)",
                self._attr_name,
                self._pending_press_requests,
            )

    def _handle_sdk_rendering_control_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle RenderingControl events from the SDK."""
        self._update_ma_state_from_sdk_cache()
        for sv in state_variables:
            if sv.name == "LastChange" and sv.value and "Slave" in str(sv.value):
                self.mass.create_task(self._refresh_multiroom())
                break

    def _handle_sdk_play_queue_event(
        self, service: UpnpService, state_variables: list[UpnpStateVariable[typing.Any]]
    ) -> None:
        """Handle PlayQueue events from the SDK."""
        self._update_ma_state_from_sdk_cache()

    # --- Private helpers ---

    def _update_ma_state_from_sdk_cache(self) -> None:
        """Update MA state from SDK's cache/HTTP poll attributes."""
        self._attr_available = self.device.available
        if self.device.name != self._attr_name:
            self._attr_name = self.device.name

        if not self._attr_available:
            self._attr_current_media = None
            self._attr_active_source = None
            self.update_state()
            return

        self._attr_volume_level = self.device.volume
        self._attr_volume_muted = self.device.is_muted

        # Followers have their state derived by MA from the leader.
        snapshot = self._wiim_controller.get_group_snapshot(self.device.udn)
        if snapshot.role == WiimGroupRole.FOLLOWER:
            self.update_state()
            return

        # Playback state
        if self.device.playing_status is not None:
            self._attr_playback_state = SDK_TO_MA_STATE.get(
                self.device.playing_status, PlaybackState.IDLE
            )

        # Group members
        group_members = self._wiim_controller.get_group_members(self.device.udn)
        self._attr_group_members = [
            f"{PLAYER_ID_PREFIX}{m.udn}" for m in group_members if m.udn != self.device.udn
        ]

        media = self.device.current_media
        device_uri = media.uri if media and media.uri else ""
        play_mode = self.device.play_mode

        if play_mode and play_mode != SOURCE_NETWORK and play_mode in INPUT_MODE_SOURCES:
            self._attr_active_source = INPUT_MODE_SOURCES[play_mode].id
        elif play_mode == SOURCE_NETWORK:
            ma_queue = self.mass.player_queues.get(self.player_id)
            assert ma_queue is not None
            if device_uri == "wiimu_airplay":
                self._attr_active_source = SOURCE_AIRPLAY
            elif device_uri.startswith("spotify:"):
                self._attr_active_source = SOURCE_SPOTIFY
            elif ma_queue.current_item:
                self._attr_active_source = self.player_id
            else:
                self._attr_active_source = SOURCE_UNKNOWN
        else:
            self._attr_active_source = None

        source_display_name: str | None = None
        if play_mode and play_mode in INPUT_MODE_SOURCES:
            source_display_name = INPUT_MODE_SOURCES[play_mode].name
        elif self._attr_active_source in PASSIVE_SOURCES:
            source_display_name = PASSIVE_SOURCES[self._attr_active_source].name

        is_ma_source = self._attr_active_source == self.player_id
        sdk_has_metadata = bool(media and (media.title or media.artist or media.album))

        if self._attr_active_source is None:
            self._attr_current_media = None
        elif is_ma_source and not sdk_has_metadata:
            # Keep the metadata play_media set until the SDK catches up.
            pass
        else:
            self._attr_current_media = PlayerMedia(
                uri=(media.uri if media else None) or "",
                title=(media.title if media else None) or source_display_name,
                artist=media.artist if media else None,
                album=media.album if media else None,
                image_url=media.image_url if media else None,
                duration=media.duration if media else None,
                source_id=self._attr_active_source,
            )

        self._log_sdk_state_change()
        self._last_seen_device_uri = device_uri
        self.update_state()

    def _log_sdk_state_change(self) -> None:
        """Log a debug line whenever the SDK-reported URI or playing_status changes."""
        new_sdk_uri = self.device.current_media.uri if self.device.current_media else None
        new_sdk_status = self.device.playing_status
        if (
            new_sdk_uri == self._last_logged_sdk_uri
            and new_sdk_status == self._last_logged_sdk_status
        ):
            return
        ma_queue = self.mass.player_queues.get(self.player_id)
        self.logger.debug(
            "WiiM state changed on %s: uri=%r->%r, status=%s->%s | queue.current_item_id=%s",
            self._attr_name,
            self._last_logged_sdk_uri,
            new_sdk_uri,
            self._last_logged_sdk_status,
            new_sdk_status,
            ma_queue.current_item.queue_item_id if ma_queue and ma_queue.current_item else None,
        )
        self._last_logged_sdk_uri = new_sdk_uri
        self._last_logged_sdk_status = new_sdk_status

    async def _ensure_subscriptions_and_update(self) -> None:
        """Re-subscribe to UPnP events and update state."""
        try:
            await self.device.ensure_subscriptions()
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.warning("Failed to re-subscribe for %s: %s", self._attr_name, err)
        self._update_ma_state_from_sdk_cache()

    async def _sync_position(self) -> None:
        """Fetch fresh position from the device and update state."""
        try:
            await self.device.sync_device_duration_and_position()
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.debug("Failed to sync position for %s: %s", self._attr_name, err)
            return
        if (media := self.device.current_media) is not None and media.position is not None:
            self._attr_elapsed_time = media.position
            self._attr_elapsed_time_last_updated = time.time()
        self._update_ma_state_from_sdk_cache()

    async def _refresh_multiroom(self) -> None:
        """Refresh multiroom status from devices, then update all WiiM players."""
        try:
            await self._wiim_controller.async_update_all_multiroom_status()
        except (WiimDeviceException, WiimRequestException) as err:
            self.logger.debug("Failed to refresh multiroom status: %s", err)
        for player in self.provider.players:
            if isinstance(player, WiimPlayer):
                player._update_ma_state_from_sdk_cache()

    def _handle_command_error(
        self, action: str, err: WiimDeviceException | WiimRequestException
    ) -> None:
        """Handle a command error by logging and refreshing state."""
        self.logger.warning("Command '%s' failed on %s: %s", action, self._attr_name, err)
        self._update_ma_state_from_sdk_cache()

    def _detect_remote_button_press(self) -> None:
        """
        Detect remote button press from LOADING state and open press window.

        A LOADING event with unchanged device URI indicates a local remote button
        press, not a natural queue advance. This opens a press window to observe
        stream request signals and schedules the resolution handler.
        """
        snapshot = self._wiim_controller.get_group_snapshot(self.device.udn)
        if snapshot.role == WiimGroupRole.FOLLOWER:
            return

        is_ma_source = self._attr_active_source == self.player_id
        if not is_ma_source:
            return

        if time.monotonic() < self._command_in_flight_deadline:
            return

        media = self.device.current_media
        device_uri = media.uri if media and media.uri else ""
        if device_uri == self._last_seen_device_uri:
            now = time.monotonic()
            # Check if this is a new burst (window expired) or continuation
            if now >= self._pending_press_at:
                # New burst — reset counters
                self._pending_press_loadings = 0
                self._pending_press_requests = 0
            # Always: increment loading count, extend window, reschedule resolver
            self._pending_press_loadings += 1
            self._pending_press_at = now + STREAM_REQUEST_WAIT
            self.logger.debug(
                "Remote transport press on %s, waiting %.1fs for stream request",
                self._attr_name,
                STREAM_REQUEST_WAIT,
            )
            # Schedule the resolution handler to fire after STREAM_REQUEST_WAIT
            # task_id dedup re-arms the timer on each press
            self.mass.call_later(
                STREAM_REQUEST_WAIT,
                self._resolve_remote_button_press,
                task_id=f"wiim_remote_button_check_{self.player_id}",
            )

    def _resolve_remote_button_press(self) -> None:
        """
        Resolve remote button press direction based on stream request signal.

        Called after STREAM_REQUEST_WAIT seconds to determine if the device
        re-requested the stream (PREVIOUS) or not (NEXT). Counts presses within
        a burst window and dispatches accordingly.
        """
        # Capture burst state and reset it
        requests = self._pending_press_requests
        loadings = self._pending_press_loadings

        if requests == 0:
            # No stream re-requests — classify as NEXT presses
            # Cap at 3 to bound a runaway burst
            n = min(loadings, 3)
            self.logger.debug(
                "No stream request within %.1fs on %s, pending press is NEXT (x%d)",
                STREAM_REQUEST_WAIT,
                self._attr_name,
                n,
            )
            # Dispatch next() n times; each call advances index and debounces playback
            for _ in range(n):
                self.mass.create_task(self.mass.players.cmd_next_track(self.player_id))
        elif requests >= 2:
            # Multiple stream re-requests — a complete double-press gesture
            self.logger.debug(
                "Double PREVIOUS press in burst on %s, jumping back",
                self._attr_name,
            )
            self._dispatch_previous_in_queue()
            self._previous_press_deadline = time.monotonic() + PREVIOUS_PRESS_WINDOW
        else:  # requests == 1
            # Single stream re-request — apply press-count window logic
            if time.monotonic() < self._previous_press_deadline:
                self.logger.debug(
                    "Second PREVIOUS press on %s within window, jumping back",
                    self._attr_name,
                )
                self._dispatch_previous_in_queue()
            else:
                self.logger.debug(
                    "First PREVIOUS press on %s, opening window",
                    self._attr_name,
                )
                # Device already restarted track; nothing to dispatch here
            self._previous_press_deadline = time.monotonic() + PREVIOUS_PRESS_WINDOW

    def _dispatch_previous_in_queue(self) -> None:
        """
        Dispatch a jump to the previous queue item.

        Called when the second PREVIOUS press is detected within the press window.
        Uses play_index to bypass the elapsed_time rule and get exact semantics.
        """
        ma_queue = self.mass.player_queues.get(self.player_id)
        if ma_queue is None or ma_queue.current_index is None:
            self.logger.debug("No queue or current index for PREVIOUS on %s", self._attr_name)
            return

        prev_index = max(ma_queue.current_index - 1, 0)
        self.logger.debug(
            "play_index to previous item: index %d on %s",
            prev_index,
            self._attr_name,
        )
        self.mass.create_task(self.mass.player_queues.play_index(ma_queue.queue_id, prev_index))
