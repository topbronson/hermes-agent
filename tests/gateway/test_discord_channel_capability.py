"""Fail-closed tests for the gateway-owned Discord channel-create capability."""

from types import SimpleNamespace

from gateway.discord_channel_capability import (
    ChannelCreateCapability,
    ChannelCreateCapabilityConfig,
)
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from tools.discord_tool import DiscordAPIError

GUILD = "1526597087947653150"
PARENT = "1526597087947653153"
CAPABILITY = "bronson-validation"


def _event(*, permission_bits: str = "16", **overrides: object) -> MessageEvent:
    source_values = {
        "platform": Platform.RELAY,
        "scope_id": GUILD,
        "guild_id": GUILD,
        "chat_type": "channel",
        "chat_id": "channel-1",
        "message_id": "message-1",
        "user_id": "member-1",
        "delivered_via_upstream_relay": True,
    }
    metadata = {
        "trusted_discord_interaction": {
            "guild_id": GUILD,
            "member_id": "member-1",
            "role_ids": ["role-1"],
            "permission_bits": permission_bits,
        }
    }
    source_values.update(overrides.pop("source", {}))
    metadata.update(overrides.pop("metadata", {}))
    return MessageEvent(
        text="/approve",
        message_id="message-1",
        source=SessionSource(**source_values),
        metadata=metadata,
    )


def test_configured_capability_is_fail_closed_for_malformed_or_missing_profile_config():
    from gateway.discord_channel_capability import configured_channel_create_capability

    assert configured_channel_create_capability({}) is None
    assert configured_channel_create_capability({"channel_create_capabilities": []}) is None
    assert configured_channel_create_capability({"channel_create_capabilities": {CAPABILITY: {"guild_id": GUILD}}}) is None
    capability = configured_channel_create_capability({
        "channel_create_capabilities": {CAPABILITY: {"guild_id": GUILD, "parent_id": PARENT}}
    })
    assert capability is not None
    assert capability._capabilities[CAPABILITY] == ChannelCreateCapabilityConfig(guild_id=GUILD, parent_id=PARENT)


def _capability(request):
    return ChannelCreateCapability(
        capabilities={CAPABILITY: ChannelCreateCapabilityConfig(guild_id=GUILD, parent_id=PARENT)},
        token_provider=lambda: "gateway-owned-token",
        request=request,
    )


def test_creates_only_the_configured_parent_from_a_trusted_relay_interaction():
    calls = []

    def request(method, path, token, *, body):
        calls.append((method, path, token, body))
        return {"id": "987654321", "name": "phase2-channel-create-validation", "type": 0, "guild_id": GUILD, "parent_id": PARENT}

    result = _capability(request).create_from_interaction(
        _event(), capability_id=CAPABILITY, name="phase2-channel-create-validation"
    )

    assert result.ok is True
    assert result.channel_id == "987654321"
    assert calls == [(
        "POST", f"/guilds/{GUILD}/channels", "gateway-owned-token",
        {"name": "phase2-channel-create-validation", "type": 0, "parent_id": PARENT},
    )]


def test_rejects_forged_or_incomplete_ingress_without_a_request():
    calls = []
    capability = _capability(lambda *args, **kwargs: calls.append((args, kwargs)))
    cases = [
        _event(source={"delivered_via_upstream_relay": False}),
        _event(source={"platform": Platform.DISCORD}),
        _event(source={"chat_type": "dm"}),
        _event(source={"scope_id": "other-guild"}),
        _event(metadata={"trusted_discord_interaction": {"guild_id": GUILD, "member_id": "other-member", "role_ids": [], "permission_bits": "16"}}),
        _event(metadata={"trusted_discord_interaction": {"guild_id": GUILD, "member_id": "member-1", "role_ids": [], "permission_bits": "not-a-bitset"}}),
    ]

    for event in cases:
        result = capability.create_from_interaction(event, capability_id=CAPABILITY, name="phase2-channel-create-validation")
        assert result.ok is False
        assert result.reason == "untrusted_interaction"
    assert calls == []


def test_rejects_unknown_capability_permissions_and_invalid_name_without_a_request():
    calls = []
    capability = _capability(lambda *args, **kwargs: calls.append((args, kwargs)))

    assert capability.create_from_interaction(_event(), capability_id="other", name="phase2-channel-create-validation").reason == "unknown_capability"
    assert capability.create_from_interaction(_event(permission_bits="0"), capability_id=CAPABILITY, name="phase2-channel-create-validation").reason == "permission_denied"
    for name in ("", " bad", "bad\nname", "x" * 101):
        assert capability.create_from_interaction(_event(), capability_id=CAPABILITY, name=name).reason == "invalid_name"
    assert calls == []


def test_never_returns_token_or_raw_discord_error_body():
    def request(*args, **kwargs):
        raise DiscordAPIError(403, "Authorization: gateway-owned-token; sensitive Discord body")

    result = _capability(request).create_from_interaction(_event(), capability_id=CAPABILITY, name="phase2-channel-create-validation")

    assert result.ok is False
    assert result.reason == "discord_refused"
    assert "gateway-owned-token" not in repr(result)
    assert "sensitive" not in repr(result)


def test_requires_a_gateway_owned_token_without_a_request():
    calls = []
    capability = ChannelCreateCapability(
        capabilities={CAPABILITY: ChannelCreateCapabilityConfig(guild_id=GUILD, parent_id=PARENT)},
        token_provider=lambda: None,
        request=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = capability.create_from_interaction(_event(), capability_id=CAPABILITY, name="phase2-channel-create-validation")

    assert result.reason == "gateway_credential_unavailable"
    assert calls == []
