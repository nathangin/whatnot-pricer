import json
import os
import subprocess
import sys
from pathlib import Path

import main

REPO = Path(__file__).resolve().parent.parent


def test_env_file_is_loaded(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\nWHATNOT_MODEL=claude-haiku-4-5-20251001\n")
    main.load_env(env_file)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-file"
    assert os.environ["WHATNOT_MODEL"] == "claude-haiku-4-5-20251001"


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n")
    main.load_env(env_file)
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-from-shell"


def test_env_file_lives_next_to_main():
    assert main.ENV_FILE == REPO / ".env"
    assert main.REGION_FILE == REPO / "region.json"


def test_region_round_trip(tmp_path):
    path = tmp_path / "region.json"
    region = {"left": -1920, "top": 0, "width": 800, "height": 600}
    main.save_region(region, path)
    assert main.load_region(path) == region


def test_invalid_region_files_are_ignored(tmp_path):
    path = tmp_path / "region.json"
    assert main.load_region(path) is None                      # missing
    for content in ["not json", "[]", json.dumps({"left": 0}),
                    json.dumps({"left": 0, "top": 0, "width": 10, "height": 10})]:
        path.write_text(content)
        assert main.load_region(path) is None, content


def test_modules_import_without_a_display():
    """Importing the app must not need a screen: tkinter and mss load lazily."""
    code = ("import sys, main, monitor, prices; "
            "print(sorted(m for m in ('tkinter', 'mss') if m in sys.modules))")
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
