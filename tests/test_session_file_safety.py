"""Session names come from user input and must never escape the project dir."""
import os

import pytest

from src.utils import get_safe_session_file_path, sanitize_session_name

TRAVERSAL_ATTEMPTS = [
    '../../etc/passwd',
    '..%2f..%2fetc%2fpasswd',
    '/etc/shadow',
    'foo/../../bar',
    r'..\..\windows\system32',
    'a/b/c',
]


@pytest.mark.parametrize('name', TRAVERSAL_ATTEMPTS)
def test_traversal_never_escapes_project_dir(name, temp_dir):
    try:
        path = get_safe_session_file_path(name, project_dir=temp_dir)
    except ValueError:
        return  # rejecting outright is a valid outcome
    assert os.path.commonpath([os.path.abspath(temp_dir), path]) == os.path.abspath(temp_dir)
    assert os.sep not in os.path.relpath(path, temp_dir)


@pytest.mark.parametrize('name', TRAVERSAL_ATTEMPTS)
def test_sanitize_strips_path_separators(name):
    try:
        sanitized = sanitize_session_name(name)
    except ValueError:
        return
    assert '/' not in sanitized
    assert '\\' not in sanitized
    assert '..' not in sanitized


@pytest.mark.parametrize('bad', ['', None, '...', '///', 123, '---'])
def test_empty_or_non_string_names_are_rejected(bad):
    with pytest.raises(ValueError):
        sanitize_session_name(bad)


def test_phone_number_survives_sanitization():
    """Session keys are phone numbers; they must stay usable as filenames."""
    assert sanitize_session_name('+15550000006') == '15550000006'


def test_session_path_ends_with_session_extension(temp_dir):
    path = get_safe_session_file_path('15550000006', project_dir=temp_dir)
    assert path.endswith('.session')
    assert os.path.dirname(path) == os.path.abspath(temp_dir)


def test_default_session_path_uses_persistent_data_directory(tmp_path, monkeypatch):
    from src import config

    data_dir = tmp_path / "persistent-data"
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    path = get_safe_session_file_path("15550000006")
    assert os.path.dirname(path) == str(data_dir / "sessions")
    assert os.path.isdir(data_dir / "sessions")


def test_legacy_session_is_migrated_into_persistent_directory(tmp_path, monkeypatch):
    from src import config

    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "persistent-data"
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    legacy = tmp_path / "15550000006.session"
    legacy.write_bytes(b"legacy session fixture")

    path = get_safe_session_file_path("15550000006")

    assert os.path.exists(path)
    with open(path, "rb") as session:
        assert session.read() == b"legacy session fixture"
    assert not legacy.exists()


def test_long_names_are_truncated():
    assert len(sanitize_session_name('a' * 500)) == 255
