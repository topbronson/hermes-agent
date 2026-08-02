"""Narrow gateway-owned Discord capability for creating one configured channel.

This module is deliberately not a plugin API or a general Discord REST client.
It accepts a relay-authenticated interaction event plus a preconfigured capability
name, derives all source identity from that event, and makes only the one fixed
Discord request needed to create a text channel under the configured parent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from tools.discord_tool import DiscordAPIError, _discord_request, _get_bot_token

_MANAGE_CHANNELS = 1 << 4
_ADMINISTRATOR = 1 << 3
_TEXT_CHANNEL = 0


@dataclass(frozen=True)
class ChannelCreateCapabilityConfig:
    """A gateway-owned exact Discord target, configured outside plugin arguments."""

    guild_id: str
    parent_id: str

    def __post_init__(self) -> None:
        if not self.guild_id.isdecimal() or not self.parent_id.isdecimal():
            raise ValueError("Channel-create capability requires numeric Discord guild and parent IDs.")


@dataclass(frozen=True)
class ChannelCreateCapabilityResult:
    """Sanitized result for an allowlisted channel-create operation."""

    ok: bool
    reason: str = ""
    channel_id: str | None = None
    channel_name: str | None = None


class ChannelCreateCapability:
    """Gateway-only narrow write boundary; it never exposes Discord credentials."""

    def __init__(
        self,
        *,
        capabilities: Mapping[str, ChannelCreateCapabilityConfig],
        token_provider: Callable[[], str | None] = _get_bot_token,
        request: Callable[..., Any] = _discord_request,
    ) -> None:
        self._capabilities = dict(capabilities)
        self._token_provider = token_provider
        self._request = request

    def create_from_interaction(
        self,
        event: MessageEvent,
        *,
        capability_id: str,
        name: str,
    ) -> ChannelCreateCapabilityResult:
        """Perform the configured POST only for a verified relay interaction."""
        capability = self._capabilities.get(capability_id)
        if capability is None:
            return ChannelCreateCapabilityResult(False, "unknown_capability")
        if not _valid_name(name):
            return ChannelCreateCapabilityResult(False, "invalid_name")
        if not _is_trusted_interaction(event, expected_guild_id=capability.guild_id):
            return ChannelCreateCapabilityResult(False, "untrusted_interaction")
        if not _can_manage_channels(event):
            return ChannelCreateCapabilityResult(False, "permission_denied")
        token = self._token_provider()
        if not isinstance(token, str) or not token:
            return ChannelCreateCapabilityResult(False, "gateway_credential_unavailable")
        try:
            response = self._request(
                "POST",
                f"/guilds/{capability.guild_id}/channels",
                token,
                body={"name": name, "type": _TEXT_CHANNEL, "parent_id": capability.parent_id},
            )
        except DiscordAPIError as error:
            return ChannelCreateCapabilityResult(False, "discord_refused" if 400 <= error.status < 500 else "discord_uncertain")
        except Exception:
            return ChannelCreateCapabilityResult(False, "discord_uncertain")
        if not isinstance(response, dict):
            return ChannelCreateCapabilityResult(False, "discord_uncertain")
        channel_id = response.get("id")
        if (
            not isinstance(channel_id, str)
            or not channel_id.isdecimal()
            or response.get("name") != name
            or response.get("type") != _TEXT_CHANNEL
            or response.get("guild_id") != capability.guild_id
            or response.get("parent_id") != capability.parent_id
        ):
            return ChannelCreateCapabilityResult(False, "discord_uncertain")
        return ChannelCreateCapabilityResult(True, channel_id=channel_id, channel_name=name)


def configured_channel_create_capability(platform_extra: object) -> ChannelCreateCapability | None:
    """Build configured exact targets from gateway-owned profile settings only.

    Expected profile configuration beneath the Discord platform's ``extra`` map::

        channel_create_capabilities:
          a-stable-capability-id:
            guild_id: "123"
            parent_id: "456"

    Bad configuration disables the complete capability set rather than granting a
    partially parsed target. The plugin receives only its stable capability ID.
    """
    if not isinstance(platform_extra, dict):
        return None
    raw_capabilities = platform_extra.get("channel_create_capabilities")
    if not isinstance(raw_capabilities, dict) or not raw_capabilities:
        return None
    capabilities: dict[str, ChannelCreateCapabilityConfig] = {}
    try:
        for capability_id, raw_target in raw_capabilities.items():
            if (
                not isinstance(capability_id, str)
                or not capability_id
                or not isinstance(raw_target, dict)
                or set(raw_target) != {"guild_id", "parent_id"}
            ):
                return None
            guild_id = _decimal_config_id(raw_target["guild_id"])
            parent_id = _decimal_config_id(raw_target["parent_id"])
            if guild_id is None or parent_id is None:
                return None
            capabilities[capability_id] = ChannelCreateCapabilityConfig(guild_id=guild_id, parent_id=parent_id)
    except ValueError:
        return None
    return ChannelCreateCapability(capabilities=capabilities)


def _decimal_config_id(value: object) -> str | None:
    """Normalize YAML's safe integer IDs without accepting ambiguous values."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and value.isdecimal() else None


def _valid_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and name == name.strip()
        and len(name) <= 100
        and all(ord(character) >= 32 and ord(character) != 127 for character in name)
    )


def _is_trusted_interaction(event: MessageEvent, *, expected_guild_id: str) -> bool:
    source = event.source
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    interaction = metadata.get("trusted_discord_interaction")
    if not isinstance(interaction, dict):
        return False
    permission_bits = interaction.get("permission_bits")
    role_ids = interaction.get("role_ids")
    return bool(
        source.platform is Platform.RELAY
        and source.delivered_via_upstream_relay is True
        and source.chat_type == "channel"
        and source.scope_id == expected_guild_id
        and source.guild_id == expected_guild_id
        and source.user_id
        and source.message_id
        and source.chat_id
        and interaction.get("guild_id") == expected_guild_id
        and interaction.get("member_id") == source.user_id
        and isinstance(role_ids, list)
        and all(isinstance(role_id, str) and role_id for role_id in role_ids)
        and isinstance(permission_bits, str)
        and permission_bits.isdecimal()
    )


def _can_manage_channels(event: MessageEvent) -> bool:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    interaction = metadata.get("trusted_discord_interaction")
    if not isinstance(interaction, dict):
        return False
    permission_bits = interaction.get("permission_bits")
    if not isinstance(permission_bits, str) or not permission_bits.isdecimal():
        return False
    permissions = int(permission_bits)
    return bool(permissions & (_MANAGE_CHANNELS | _ADMINISTRATOR))
