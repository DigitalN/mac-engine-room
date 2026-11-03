"""Moving an installed Pythonflow over to Mac Engine Room: folders, the bundle, old copies."""

import fcntl
import plistlib
import shutil
import threading
import time

import launch
import rename
from rename import LOGIN_ITEM_ARGUMENT, move_old_folders, rename_bundle, trash_old_copies

BUNDLE_ID = "com.pythonflow.monitor"


def make_app(path, bundle_id=BUNDLE_ID):
    (path / "Contents").mkdir(parents=True)
    with open(path / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, f)
    return path


def old_folders(tmp_path):
    data, logs = tmp_path / "Support" / "Pythonflow", tmp_path / "Logs" / "Pythonflow"
    data.mkdir(parents=True)
    logs.mkdir(parents=True)
    (data / "history.db").write_text("history")
    (logs / "app.log").write_text("log")
    return [(data, tmp_path / "Support" / "Mac Engine Room"), (logs, tmp_path / "Logs" / "Mac Engine Room")]


class FakeLoginItem:
    def __init__(self, enabled):
        self.enabled = enabled
        self.calls = []

    def set_enabled(self, on):
        self.calls.append(on)
        self.enabled = on


def test_folders_move_once(tmp_path):
    moves = old_folders(tmp_path)
    notes = move_old_folders(moves, None)
    assert len(notes) == 2 and all(note.startswith("Moved") for note in notes)
    assert (moves[0][1] / "history.db").read_text() == "history"
    assert (moves[1][1] / "app.log").read_text() == "log"
    assert not moves[0][0].exists()
    assert move_old_folders(moves, None) == []


def test_folders_that_already_exist_under_the_new_name_are_left_alone(tmp_path):
    moves = old_folders(tmp_path)
    moves[0][1].mkdir()
    (moves[0][1] / "history.db").write_text("newer")
    move_old_folders(moves, None)
    assert (moves[0][1] / "history.db").read_text() == "newer"
    assert (moves[0][0] / "history.db").exists()
    assert (moves[1][1] / "app.log").exists()  # the logs still moved


def test_nothing_moves_while_an_old_copy_holds_its_lock(tmp_path):
    moves = old_folders(tmp_path)
    with open(moves[0][0] / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert move_old_folders(moves, None, timeout=0.3) is None
    assert moves[0][0].exists() and moves[1][0].exists()


def test_folders_move_once_the_old_copy_has_quit(tmp_path):
    moves = old_folders(tmp_path)
    held = open(moves[0][0] / ".lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    threading.Timer(0.3, held.close).start()
    started = time.monotonic()
    notes = move_old_folders(moves, None, timeout=5)
    assert 0.2 < time.monotonic() - started < 5
    assert len(notes) == 2 and (moves[0][1] / "history.db").exists()


def test_old_bundle_is_renamed_and_relaunched(tmp_path, monkeypatch):
    relaunched = []
    monkeypatch.setattr(rename, "relaunch", lambda app, **kwargs: relaunched.append((app, kwargs)))
    app = make_app(tmp_path / "Pythonflow.app")
    login = FakeLoginItem(enabled=True)
    assert rename_bundle(app, login)
    target = tmp_path / "Mac Engine Room.app"
    assert target.exists() and not app.exists()
    # The login item is turned off before the rename and back on by the renamed copy.
    assert login.calls == [False]
    assert relaunched == [(target, {"background": False, "arguments": [LOGIN_ITEM_ARGUMENT]})]


def test_renaming_keeps_open_at_login_off(tmp_path, monkeypatch):
    relaunched = []
    monkeypatch.setattr(rename, "relaunch", lambda app, **kwargs: relaunched.append(kwargs))
    assert rename_bundle(make_app(tmp_path / "Pythonflow.app"), FakeLoginItem(enabled=False))
    assert relaunched[0]["arguments"] == []


def test_a_copy_already_under_the_new_name_goes_to_the_trash(tmp_path, monkeypatch):
    trashed = []

    def trash(path):
        trashed.append(path)
        shutil.rmtree(path)
        return True

    monkeypatch.setattr(rename, "relaunch", lambda app, **kwargs: None)
    monkeypatch.setattr(rename, "_trash", trash)
    app = make_app(tmp_path / "Pythonflow.app")
    make_app(tmp_path / "Mac Engine Room.app")
    assert rename_bundle(app, FakeLoginItem(enabled=False))
    assert trashed == [tmp_path / "Mac Engine Room.app"]
    assert (tmp_path / "Mac Engine Room.app").exists() and not app.exists()


def test_another_app_with_the_new_name_stops_the_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(rename, "relaunch", lambda app, **kwargs: None)
    app = make_app(tmp_path / "Pythonflow.app")
    make_app(tmp_path / "Mac Engine Room.app", bundle_id="com.example.other")
    assert not rename_bundle(app, FakeLoginItem(enabled=True))
    assert app.exists()


def test_bundles_under_any_other_name_stay_put(tmp_path, monkeypatch):
    monkeypatch.setattr(rename, "relaunch", lambda app, **kwargs: None)
    assert not rename_bundle(make_app(tmp_path / "Mac Engine Room.app"), FakeLoginItem(enabled=True))
    assert not rename_bundle(make_app(tmp_path / "My Pythonflow.app"), FakeLoginItem(enabled=True))


def test_old_copies_of_this_app_go_to_the_trash(tmp_path, monkeypatch):
    trashed = []
    monkeypatch.setattr(rename, "_trash", lambda path: trashed.append(path) or True)
    app = make_app(tmp_path / "Applications" / "Mac Engine Room.app")
    make_app(tmp_path / "Applications" / "Pythonflow.app")
    make_app(tmp_path / "Home" / "Pythonflow.app", bundle_id="com.example.other")
    (tmp_path / "Empty").mkdir()
    folders = [tmp_path / "Applications", tmp_path / "Home", tmp_path / "Empty"]
    assert trash_old_copies(app, folders)
    assert trashed == [tmp_path / "Applications" / "Pythonflow.app"]
    assert not trash_old_copies(app, [tmp_path / "Home", tmp_path / "Empty"])


def test_relaunch_passes_its_arguments(monkeypatch):
    launched = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda command, **kwargs: launched.append(command))
    monkeypatch.delenv("PYTHONFLOW_DATA_DIR", raising=False)
    launch.relaunch("/Applications/Mac Engine Room.app", background=True, arguments=[LOGIN_ITEM_ARGUMENT])
    launch.relaunch("/Applications/Mac Engine Room.app", arguments=[LOGIN_ITEM_ARGUMENT])
    launch.relaunch("/Applications/Mac Engine Room.app")
    # After the script: "sh", this process's id, and the executable it checks for.
    assert {command[command.index("sh", 3) + 2] for command in launched} == {
        "/Applications/Mac Engine Room.app/Contents/MacOS/Mac Engine Room"}
    opened = [command[command.index("sh", 3) + 3:] for command in launched]
    assert opened == [
        ["-g", "/Applications/Mac Engine Room.app", "--args", "--background", LOGIN_ITEM_ARGUMENT],
        ["/Applications/Mac Engine Room.app", "--args", LOGIN_ITEM_ARGUMENT],
        ["/Applications/Mac Engine Room.app"],
    ]
