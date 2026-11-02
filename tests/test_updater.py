"""The updater's decisions: versions, GitHub's release format, signing details, and once a day."""

from datetime import date

import pytest

from launch import in_applications_folder
from updater import (UpdateError, check_is_due, is_newer, parse_release, signing_requirement,
                     team_identifier)


def test_versions_compare_number_by_number():
    assert is_newer("1.10.0", "1.9.0")
    assert is_newer("1.1", "1.0.9")
    assert is_newer("v2.0", "1.99")
    assert not is_newer("1.1.0", "1.1")  # same version, different spelling
    assert not is_newer("1.0.0", "1.1.0")


def test_release_gives_version_and_disk_image():
    release = {"tag_name": "v1.2.0", "draft": False, "assets": [
        {"name": "notes.txt", "browser_download_url": "https://example.invalid/notes.txt"},
        {"name": "Mac-Engine-Room-1.2.0.dmg", "browser_download_url": "https://example.invalid/Mac-Engine-Room-1.2.0.dmg"},
    ]}
    assert parse_release(release) == ("1.2.0", "https://example.invalid/Mac-Engine-Room-1.2.0.dmg")


def test_release_without_disk_image_or_tag_is_refused():
    with pytest.raises(UpdateError, match="no disk image"):
        parse_release({"tag_name": "v1.2.0", "assets": [{"name": "Mac-Engine-Room.zip", "browser_download_url": "x"}]})
    with pytest.raises(UpdateError):
        parse_release({"message": "Not Found"})


def test_team_comes_from_codesign_details():
    details = "Executable=/Applications/Mac Engine Room.app/Contents/MacOS/Mac Engine Room\nIdentifier=com.pythonflow.monitor\nTeamIdentifier=XT4LT7MS6Y\n"
    assert team_identifier(details) == "XT4LT7MS6Y"
    assert team_identifier("Identifier=com.pythonflow.monitor\nTeamIdentifier=not set\n") is None
    assert team_identifier("code object is not signed at all") is None


def test_signing_requirement_pins_identifier_and_team():
    requirement = signing_requirement("com.pythonflow.monitor", "XT4LT7MS6Y")
    assert requirement.startswith("=identifier \"com.pythonflow.monitor\" and anchor apple generic")
    assert requirement.endswith("certificate leaf[subject.OU] = \"XT4LT7MS6Y\"")


def test_checks_once_a_day():
    today = date(2026, 10, 7)
    assert check_is_due(None, today)
    assert check_is_due("2026-10-06", today)
    assert not check_is_due("2026-10-07", today)


def test_applications_folder(tmp_path):
    assert in_applications_folder("/Applications/Mac Engine Room.app")
    assert not in_applications_folder("/Applications Old/Mac Engine Room.app")
    assert not in_applications_folder(tmp_path / "Mac Engine Room.app")
    assert not in_applications_folder("/private/var/folders/xy/T/AppTranslocation/1234/d/Mac Engine Room.app")
