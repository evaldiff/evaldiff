"""Tests for the evaldiff CLI stub."""

from __future__ import annotations

import json
import pathlib

from typer.testing import CliRunner

from evaldiff_cli.main import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "evaldiff" in result.output


def test_init_writes_valid_json(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--name", "my-dataset"])
    assert result.exit_code == 0
    path = tmp_path / "my-dataset.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert isinstance(data, list) and len(data) == 2
    assert "input" in data[0] and "expected" in data[0] and "rubric" in data[0]


def test_init_is_idempotent(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["init", "--name", "my-dataset"])
    second = runner.invoke(app, ["init", "--name", "my-dataset"])
    assert first.exit_code == 0
    assert second.exit_code == 0
    data = json.loads((tmp_path / "my-dataset.json").read_text())
    assert len(data) == 2
