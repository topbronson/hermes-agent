from __future__ import annotations

import json
import sqlite3
import threading
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    kb._INITIALIZED_PATHS.clear()
    return home


def test_recovery_preserves_timestamped_archive(fresh_home):
    kb.create_board("recoverable")
    with kb.connect(board="recoverable") as conn:
        kb.create_task(conn, title="keep me", assignee="dev")
    archived = kb.remove_board("recoverable")
    restored = kb.recover_board("recoverable", Path(archived["new_path"]))
    assert Path(restored["path"]).is_dir()
    assert Path(archived["new_path"]).is_dir()
    assert not kb._board_tombstone_path("recoverable").exists()
    with kb.connect(board="recoverable") as conn:
        assert [task.title for task in kb.list_tasks(conn)] == ["keep me"]


def test_stale_connect_and_init_fail_closed(fresh_home):
    kb.create_board("archived-init")
    db_path = kb.kanban_db_path(board="archived-init")
    kb.remove_board("archived-init")
    kb._INITIALIZED_PATHS.clear()
    with pytest.raises(kb.KanbanBoardArchivedError):
        kb.connect(board="archived-init")
    with pytest.raises(kb.KanbanBoardArchivedError):
        kb.init_db(board="archived-init")
    assert not kb.board_dir("archived-init").exists()
    assert not db_path.exists()


def test_remove_is_serialized_and_only_one_wins(fresh_home):
    kb.create_board("two-removers")
    barrier = threading.Barrier(2)
    outcomes = []

    def remove():
        barrier.wait()
        try:
            outcomes.append(("ok", kb.remove_board("two-removers")))
        except ValueError as exc:
            outcomes.append(("error", str(exc)))

    threads = [threading.Thread(target=remove) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(kind for kind, _ in outcomes) == ["error", "ok"]
    successful = next(result for kind, result in outcomes if kind == "ok")
    marker = kb._board_tombstone_path("two-removers")
    assert json.loads(marker.read_text(encoding="utf-8"))["archive_path"] == successful["new_path"]


def test_connect_remove_serializes_without_resurrection(fresh_home):
    kb.create_board("connect-remove")
    entered = threading.Event()
    release = threading.Event()
    removed = {}

    def connected_worker():
        with kb._board_lifecycle_lock("connect-remove"):
            with kb._connect_unlocked(board="connect-remove"):
                entered.set()
                assert release.wait(timeout=5)

    def remove_worker():
        entered.wait(timeout=5)
        removed["result"] = kb.remove_board("connect-remove")

    connector = threading.Thread(target=connected_worker)
    remover = threading.Thread(target=remove_worker)
    connector.start()
    remover.start()
    assert entered.wait(timeout=5)
    assert not removed
    release.set()
    connector.join(timeout=5)
    remover.join(timeout=5)
    assert "result" in removed
    assert not kb.board_dir("connect-remove").exists()
    assert kb._board_tombstone_path("connect-remove").exists()


def test_create_remove_serializes_without_resurrection(fresh_home, monkeypatch):
    kb.create_board("create-remove")
    kb.remove_board("create-remove")
    entered = threading.Event()
    release = threading.Event()
    original = kb.write_board_metadata

    def delayed_metadata(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(kb, "write_board_metadata", delayed_metadata)
    recreated = {}
    removed = {}

    def creator():
        recreated["result"] = kb.create_board("create-remove")

    def remover():
        entered.wait(timeout=5)
        removed["result"] = kb.remove_board("create-remove")

    creator_thread = threading.Thread(target=creator)
    remover_thread = threading.Thread(target=remover)
    creator_thread.start()
    remover_thread.start()
    assert entered.wait(timeout=5)
    assert not removed
    release.set()
    creator_thread.join(timeout=5)
    remover_thread.join(timeout=5)
    assert recreated and removed
    assert not kb.board_dir("create-remove").exists()
    assert removed["result"]["action"] == "archived"


def test_cli_counts_include_open_wal_and_do_not_create_sidecars(fresh_home):
    kb.create_board("readonly")
    path = kb.kanban_db_path(board="readonly")
    writer = kb.connect(board="readonly")
    try:
        kb.create_task(writer, title="wal-task", assignee="dev")
        before = {suffix: Path(f"{path}{suffix}").read_bytes() for suffix in ("-wal", "-shm") if Path(f"{path}{suffix}").exists()}
        assert kanban_cli._board_task_counts("readonly") == {"ready": 1}
        assert kanban_cli._cmd_boards_list(Namespace(all=False, json=True)) == 0
        after = {suffix: Path(f"{path}{suffix}").read_bytes() for suffix in before}
        assert after == before
        assert not (set(before) ^ {suffix for suffix in ("-wal", "-shm") if Path(f"{path}{suffix}").exists()})
    finally:
        writer.close()


def test_cli_counts_survive_checkpoint_between_snapshot_parts(fresh_home, monkeypatch):
    kb.create_board("checkpoint-race")
    path = kb.kanban_db_path(board="checkpoint-race")
    writer = kb.connect(board="checkpoint-race")
    try:
        kb.create_task(writer, title="checkpoint-task", assignee="dev")
        original_copyfile = kb.shutil.copyfile
        copied_main = False

        def copyfile_with_checkpoint(source, destination, *args, **kwargs):
            nonlocal copied_main
            result = original_copyfile(source, destination, *args, **kwargs)
            if Path(source) == path and not copied_main:
                copied_main = True
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return result

        monkeypatch.setattr(kb.shutil, "copyfile", copyfile_with_checkpoint)
        assert kanban_cli._board_task_counts("checkpoint-race") == {"ready": 1}
        assert copied_main
    finally:
        writer.close()


def test_cli_counts_retry_first_snapshot_query_database_error(fresh_home, monkeypatch):
    kb.create_board("query-retry")
    writer = kb.connect(board="query-retry")
    try:
        kb.create_task(writer, title="retry-task", assignee="dev")
        real_connect = kb.sqlite3.connect
        failed = False

        class FlakyConnection:
            def __init__(self, connection):
                self._connection = connection

            @property
            def row_factory(self):
                return self._connection.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self._connection.row_factory = value

            def execute(self, *args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise sqlite3.DatabaseError("transient snapshot query")
                return self._connection.execute(*args, **kwargs)

            def close(self):
                self._connection.close()

        def flaky_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            return FlakyConnection(connection) if kwargs.get("uri") else connection

        monkeypatch.setattr(kb.sqlite3, "connect", flaky_connect)
        assert kanban_cli._board_task_counts("query-retry") == {"ready": 1}
        assert failed
    finally:
        writer.close()


def test_hard_delete_and_default_removal_are_forbidden(fresh_home):
    kb.create_board("protected")
    with pytest.raises(ValueError, match="hard deletion"):
        kb.remove_board("protected", archive=False)
    with pytest.raises(ValueError, match="default"):
        kb.remove_board("default")
