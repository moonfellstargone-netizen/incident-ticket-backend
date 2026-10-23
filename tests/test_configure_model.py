"""Local key configuration uses fake values and never calls a provider."""

from pathlib import Path
import warnings

import pytest

from scripts import configure_model


FAKE_KEY = "test-only-local-configuration-key"


@pytest.fixture
def local_files(tmp_path, monkeypatch):
    monkeypatch.setattr(configure_model, "REPOSITORY", tmp_path)
    source = tmp_path / "input.txt"
    destination = tmp_path / "instance" / "deepseek_api_key.txt"
    return source, destination


@pytest.mark.parametrize("content", [FAKE_KEY, FAKE_KEY + "\r\n", "\ufeff" + FAKE_KEY + "\n"])
def test_file_import_normalizes_without_displaying_key(local_files, capsys, content):
    source, destination = local_files
    source.write_bytes(content.encode("utf-8"))
    assert configure_model.main(["--from-file", str(source)]) == 0
    assert destination.read_text(encoding="utf-8") == FAKE_KEY + "\n"
    assert source.read_bytes() == content.encode("utf-8")
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err
    assert f"Received {len(FAKE_KEY)} characters" in captured.out
    assert "provider validity has not been checked" in captured.out


@pytest.mark.parametrize("content, reason", [
    (" \r\n", "No characters received"),
    (FAKE_KEY + "\nsecond-line", "Multiple lines received"),
    (FAKE_KEY + " inside-space", "Spaces or control characters"),
    (FAKE_KEY + "\x00", "Spaces or control characters"),
    (FAKE_KEY + "中文", "Non-ASCII characters"),
    ("a" * 513, "More than 512 characters"),
    ('"' + FAKE_KEY + '"', "Surrounding quotes"),
])
def test_invalid_input_gives_reason_and_does_not_save(local_files, capsys, content, reason):
    source, destination = local_files
    source.write_bytes(content.encode("utf-8"))
    assert configure_model.main(["--from-file", str(source)]) == 1
    assert not destination.exists()
    captured = capsys.readouterr()
    assert reason in captured.err
    assert FAKE_KEY not in captured.out + captured.err


@pytest.mark.parametrize("content", [b"a" * 1025, b"\xff\xfeinvalid"])
def test_oversize_or_non_utf8_file_is_rejected(local_files, capsys, content):
    source, destination = local_files
    source.write_bytes(content)
    assert configure_model.main(["--from-file", str(source)]) == 1
    assert not destination.exists()
    reason = "exceeds 1024 bytes" if len(content) > 1024 else "Could not read a UTF-8 key"
    assert reason in capsys.readouterr().err


def test_missing_input_file_is_reported_without_traceback(local_files, capsys):
    source, destination = local_files
    assert configure_model.main(["--from-file", str(source)]) == 1
    assert not destination.exists()
    captured = capsys.readouterr()
    assert "Could not read" in captured.err and "Traceback" not in captured.err


def test_existing_key_is_preserved_without_reading_input(local_files, monkeypatch, capsys):
    source, destination = local_files
    destination.parent.mkdir()
    destination.write_text(FAKE_KEY, encoding="utf-8")

    def forbidden_input(*args):
        pytest.fail("Existing keys must be preserved without requesting input.")

    monkeypatch.setattr(configure_model, "getpass", forbidden_input)
    assert configure_model.main([]) == 0
    assert configure_model.main(["--from-file", str(source)]) == 0
    assert destination.read_text(encoding="utf-8") == FAKE_KEY
    captured = capsys.readouterr()
    assert "Use --replace" in captured.out
    assert FAKE_KEY not in captured.out + captured.err


def test_explicit_replacement_and_invalid_replacement(local_files, capsys):
    source, destination = local_files
    destination.parent.mkdir()
    destination.write_text("old-fake-key", encoding="utf-8")
    source.write_text(FAKE_KEY, encoding="utf-8")
    assert configure_model.main(["--from-file", str(source), "--replace"]) == 0
    assert destination.read_text(encoding="utf-8") == FAKE_KEY + "\n"
    source.write_text(FAKE_KEY + " invalid", encoding="utf-8")
    assert configure_model.main(["--from-file", str(source), "--replace"]) == 1
    assert destination.read_text(encoding="utf-8") == FAKE_KEY + "\n"
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.out + captured.err


def test_failed_replacement_keeps_previous_key_and_removes_temporary_file(local_files, monkeypatch, capsys):
    source, destination = local_files
    destination.parent.mkdir()
    destination.write_text("old-fake-key", encoding="utf-8")
    source.write_text(FAKE_KEY, encoding="utf-8")

    def fail_replace(self, target):
        raise PermissionError(FAKE_KEY)

    monkeypatch.setattr(Path, "replace", fail_replace)
    assert configure_model.main(["--from-file", str(source), "--replace"]) == 1
    assert destination.read_text(encoding="utf-8") == "old-fake-key"
    assert list(destination.parent.glob(".model-key-*.tmp")) == []
    captured = capsys.readouterr()
    assert "Could not save" in captured.err
    assert FAKE_KEY not in captured.out + captured.err


def test_hidden_input_explains_empty_input(local_files, monkeypatch, capsys):
    _, destination = local_files
    monkeypatch.setattr(configure_model, "getpass", lambda prompt: "")
    assert configure_model.main([]) == 1
    assert not destination.exists()
    captured = capsys.readouterr()
    assert "Received 0 characters" in captured.out
    assert "--from-file" in captured.err


def test_unsupported_terminal_does_not_fall_back_to_visible_input(local_files, monkeypatch, capsys):
    _, destination = local_files

    def unsupported_terminal(prompt):
        warnings.warn("Hidden input is unavailable.", configure_model.GetPassWarning)
        pytest.fail("Visible input fallback must not be reached.")

    monkeypatch.setattr(configure_model, "getpass", unsupported_terminal)
    assert configure_model.main([]) == 1
    assert not destination.exists()
    assert "cannot hide input safely" in capsys.readouterr().err
