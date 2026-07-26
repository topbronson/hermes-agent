"""Process-level regression for profile-owned notifier delivery."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_real_queri_runner_delivers_owned_terminal_event(tmp_path, monkeypatch):
    """A real active non-default profile must route to its primary adapter."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profiles" / "queri"))
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "1")
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "queri")

    from hermes_cli import kanban_db as kb
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="real process notifier",
            assignee="worker",
            session_id="session-queri",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="discord",
            chat_id="1530770801543090237",
            thread_id="thread-1",
            notifier_profile="queri",
        )
        kb.complete_task(conn, task_id, result="completed")
    finally:
        conn.close()

    runner = GatewayRunner()
    runner._running = True
    sent = []
    adapter = MagicMock()

    async def send(chat_id, message, metadata=None):
        sent.append((chat_id, message, metadata))
        runner._running = False

    adapter.send = AsyncMock(side_effect=send)
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.DISCORD: adapter}

    original_sleep = asyncio.sleep

    sleep_calls = 0

    async def fast_sleep(_seconds):
        nonlocal sleep_calls
        await original_sleep(0)
        sleep_calls += 1
        if sleep_calls >= 4:
            runner._running = False

    with (
        patch("gateway.kanban_watchers.asyncio.sleep", side_effect=fast_sleep),
        patch.object(kb, "list_boards", wraps=kb.list_boards) as list_boards,
        patch.object(
            kb,
            "claim_unseen_events_for_sub",
            wraps=kb.claim_unseen_events_for_sub,
        ) as claim_events,
    ):
        await asyncio.wait_for(runner._kanban_notifier_watcher(interval=0), timeout=10)

    assert runner._kanban_notifier_profile == "queri"
    assert not runner._running
    assert list_boards.call_count >= 1
    assert claim_events.call_count >= 1
    assert len(sent) == 1
    assert adapter.handle_message.await_count == 1
    assert "done" in sent[0][1]
    assert sent[0][2]["thread_id"] == "thread-1"

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn, task_id) == []
    finally:
        conn.close()
