"""Tests for hermes_cli.skin_engine — the data-driven skin/theme system."""

import pytest


@pytest.fixture(autouse=True)
def reset_skin_state():
    """Reset skin engine state between tests."""
    from hermes_cli import skin_engine
    skin_engine._active_skin = None
    skin_engine._active_skin_name = "default"
    yield
    skin_engine._active_skin = None
    skin_engine._active_skin_name = "default"


class TestSkinConfig:
    def test_default_skin_has_required_fields(self):
        from hermes_cli.skin_engine import load_skin
        skin = load_skin("default")
        assert skin.name == "default"
        assert skin.tool_prefix == "┊"
        assert "banner_title" in skin.colors
        assert "banner_border" in skin.colors
        assert "agent_name" in skin.branding


    def test_get_spinner_wings_empty_for_default(self):
        from hermes_cli.skin_engine import load_skin
        skin = load_skin("default")
        assert skin.get_spinner_wings() == []


class TestBuiltinSkins:
    def test_ares_skin_loads(self):
        from hermes_cli.skin_engine import load_skin
        skin = load_skin("ares")
        assert skin.name == "ares"
        assert skin.tool_prefix == "╎"
        # Crimson identity: border stays red-dominant (exact values are owned
        # by the palette audit in test_skin_palettes.py, which enforces
        # contrast floors — don't pin literals here).
        border = skin.get_color("banner_border")
        r, g, b = (int(border[i:i + 2], 16) for i in (1, 3, 5))
        assert r > g and r > b, f"ares border lost its crimson: {border}"
        assert skin.get_color("response_border") == "#C7A96B"
        assert skin.get_color("session_label") == "#C7A96B"
        assert skin.get_color("session_border") == "#6E584B"
        assert skin.get_branding("agent_name") == "Ares Agent"

    def test_ares_has_spinner_customization(self):
        from hermes_cli.skin_engine import load_skin
        skin = load_skin("ares")
        wings = skin.get_spinner_wings()
        assert len(wings) > 0
        assert isinstance(wings[0], tuple)
        assert len(wings[0]) == 2








class TestSkinManagement:
    def test_set_active_skin(self):
        from hermes_cli.skin_engine import set_active_skin, get_active_skin, get_active_skin_name
        skin = set_active_skin("ares")
        assert skin.name == "ares"
        assert get_active_skin_name() == "ares"
        assert get_active_skin().name == "ares"


    def test_list_skins_includes_builtins(self):
        from hermes_cli.skin_engine import list_skins
        skins = list_skins()
        names = [s["name"] for s in skins]
        assert "default" in names
        assert "ares" in names
        assert "mono" in names
        assert "slate" in names
        assert "daylight" in names
        assert "warm-lightmode" in names
        for s in skins:
            assert "source" in s
            assert s["source"] == "builtin"




class TestUserSkins:
    def test_load_user_skin_from_yaml(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import load_skin
        # Create a user skin YAML
        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        skin_file = skins_dir / "custom.yaml"
        skin_data = {
            "name": "custom",
            "description": "A custom test skin",
            "colors": {"banner_title": "#FF0000"},
            "branding": {"agent_name": "Custom Agent"},
            "tool_prefix": "▸",
        }
        import yaml
        skin_file.write_text(yaml.dump(skin_data))

        # Patch skins dir
        monkeypatch.setattr("hermes_cli.skin_engine._skins_dir", lambda: skins_dir)

        skin = load_skin("custom")
        assert skin.name == "custom"
        assert skin.get_color("banner_title") == "#FF0000"
        assert skin.get_branding("agent_name") == "Custom Agent"
        assert skin.tool_prefix == "▸"
        # Should inherit defaults for unspecified colors
        assert skin.get_color("banner_border") == "#CD7F32"  # from default

    def test_load_user_skin_invalid_section_types_fall_back_to_defaults(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import load_skin

        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        import yaml

        (skins_dir / "broken.yaml").write_text(
            yaml.dump(
                {
                    "name": "broken",
                    "colors": ["not", "a", "mapping"],
                    "spinner": "invalid",
                    "branding": ["also", "invalid"],
                    "tool_emojis": ["invalid"],
                    "tool_prefix": "!",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("hermes_cli.skin_engine._skins_dir", lambda: skins_dir)

        skin = load_skin("broken")

        assert skin.name == "broken"
        assert skin.get_color("banner_title") == "#FFD700"
        assert skin.get_branding("agent_name") == "Hermes Agent"
        assert skin.spinner.get("waiting_faces", []) == []
        assert skin.tool_emojis == {}
        assert skin.tool_prefix == "!"

    def test_list_skins_includes_user_skins(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import list_skins
        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        import yaml
        (skins_dir / "pirate.yaml").write_text(yaml.dump({
            "name": "pirate",
            "description": "Arr matey",
        }))
        monkeypatch.setattr("hermes_cli.skin_engine._skins_dir", lambda: skins_dir)

        skins = list_skins()
        names = [s["name"] for s in skins]
        assert "pirate" in names
        pirate = [s for s in skins if s["name"] == "pirate"][0]
        assert pirate["source"] == "user"


class TestNullSkinValues:
    """A bare ``key:`` in a skin YAML parses as null. In YAML an unquoted ``#`` starts a
    comment, so the natural way to type a hex color (``banner_border: #CD7F32``) leaves the
    key null -- which used to override the default palette and crash the CLI at startup
    (Rich AttributeError / prompt_toolkit ``Wrong color format 'None'``). Null must mean
    unset-and-inherit, exactly like a missing key. See #123777."""

    @staticmethod
    def _skins_dir(tmp_path, monkeypatch, filename, body):
        skins_dir = tmp_path / "skins"
        skins_dir.mkdir()
        (skins_dir / filename).write_text(body, encoding="utf-8")
        monkeypatch.setattr("hermes_cli.skin_engine._skins_dir", lambda: skins_dir)
        return skins_dir

    def test_unquoted_hex_color_inherits_default(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import load_skin

        self._skins_dir(tmp_path, monkeypatch, "bronze.yaml", (
            "name: bronze\n"
            "colors:\n"
            "  banner_border: #CD7F32\n"
            "  banner_title: \"#00FF00\"\n"
        ))

        skin = load_skin("bronze")

        # The nulled key inherits the default palette instead of crashing the renderers.
        assert skin.get_color("banner_border") == "#CD7F32"
        # The genuinely quoted sibling still wins.
        assert skin.get_color("banner_title") == "#00FF00"

    def test_null_values_in_every_section_are_dropped(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import load_skin

        self._skins_dir(tmp_path, monkeypatch, "holes.yaml", (
            "name: holes\n"
            "colors:\n"
            "  banner_border:\n"
            "spinner:\n"
            "  waiting_faces:\n"
            "branding:\n"
            "  agent_name:\n"
            "light_colors:\n"
            "  banner_border:\n"
            "dark_colors:\n"
            "  banner_border:\n"
            "tool_emojis:\n"
            "  terminal:\n"
        ))

        skin = load_skin("holes")

        assert skin.get_color("banner_border") == "#CD7F32"  # default
        assert skin.get_branding("agent_name") == "Hermes Agent"  # default
        assert skin.light_colors == {}
        assert skin.dark_colors == {}
        assert skin.tool_emojis == {}
        assert skin.spinner.get("waiting_faces", []) == []

    def test_null_top_level_fields_fall_back_to_defaults(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import load_skin

        self._skins_dir(tmp_path, monkeypatch, "bare.yaml", (
            "name: bare\n"
            "description:\n"
            "tool_prefix:\n"
            "banner_logo:\n"
            "banner_hero:\n"
        ))

        skin = load_skin("bare")

        assert skin.description == ""
        assert skin.tool_prefix == "┊"
        assert skin.banner_logo == ""
        assert skin.banner_hero == ""

    def test_null_value_warns_and_points_at_quoting(self, tmp_path, monkeypatch, caplog):
        self._skins_dir(tmp_path, monkeypatch, "warn.yaml", (
            "name: warn\n"
            "colors:\n"
            "  banner_border: #CD7F32\n"
        ))

        with caplog.at_level("WARNING", logger="hermes_cli.skin_engine"):
            from hermes_cli.skin_engine import load_skin
            load_skin("warn")

        assert any("quote" in r.message.lower() for r in caplog.records)

    def test_list_skins_shows_empty_description_not_none(self, tmp_path, monkeypatch):
        from hermes_cli.skin_engine import list_skins

        self._skins_dir(tmp_path, monkeypatch, "nodesc.yaml", "name: nodesc\ndescription:\n")

        nodesc = [s for s in list_skins() if s["name"] == "nodesc"][0]
        assert nodesc["description"] == ""


class TestDisplayIntegration:


    def test_tool_message_uses_skin_prefix(self):
        from hermes_cli.skin_engine import set_active_skin
        from agent.display import get_cute_tool_message
        set_active_skin("ares")
        msg = get_cute_tool_message("terminal", {"command": "ls"}, 0.5)
        assert msg.startswith("╎")
        assert "┊" not in msg


class TestCliBrandingHelpers:


    def test_active_goodbye_ares(self):
        from hermes_cli.skin_engine import set_active_skin, get_active_goodbye

        set_active_skin("ares")
        assert get_active_goodbye() == "Farewell, warrior! ⚔"

    def test_prompt_toolkit_style_overrides_cover_tui_classes(self):
        from hermes_cli.skin_engine import set_active_skin, get_prompt_toolkit_style_overrides
        set_active_skin("ares")
        overrides = get_prompt_toolkit_style_overrides()
        required = {
            "input-area",
            "placeholder",
            "prompt",
            "prompt-working",
            "hint",
            "status-bar",
            "status-bar-strong",
            "status-bar-dim",
            "status-bar-good",
            "status-bar-warn",
            "status-bar-bad",
            "status-bar-critical",
            "input-rule",
            "image-badge",
            "completion-menu",
            "completion-menu.completion",
            "completion-menu.completion.current",
            "completion-menu.meta.completion",
            "completion-menu.meta.completion.current",
            "status-bar",
            "status-bar-strong",
            "status-bar-dim",
            "status-bar-good",
            "status-bar-warn",
            "status-bar-bad",
            "status-bar-critical",
            "voice-status",
            "voice-status-recording",
            "clarify-border",
            "clarify-title",
            "clarify-question",
            "clarify-choice",
            "clarify-selected",
            "clarify-active-other",
            "clarify-countdown",
            "sudo-prompt",
            "sudo-border",
            "sudo-title",
            "sudo-text",
            "approval-border",
            "approval-title",
            "approval-desc",
            "approval-cmd",
            "approval-choice",
            "approval-selected",
        }
        assert required.issubset(overrides.keys())

    def test_prompt_toolkit_style_overrides_use_skin_colors(self):
        from hermes_cli.skin_engine import (
            set_active_skin,
            get_active_skin,
            get_prompt_toolkit_style_overrides,
        )

        set_active_skin("ares")
        skin = get_active_skin()
        overrides = get_prompt_toolkit_style_overrides()
        assert overrides["prompt"] == skin.get_color("prompt")
        assert overrides["input-rule"] == skin.get_color("input_rule")
        assert overrides["status-bar"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_text')}"
        )
        assert overrides["status-bar-strong"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_strong')} bold"
        )
        assert overrides["status-bar-session-title"] == (
            f"bg:{skin.get_color('status_bar_strong')} {skin.get_color('status_bar_bg')} bold"
        )
        assert overrides["status-bar-critical"] == (
            f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('status_bar_critical')} bold"
        )
        assert overrides["clarify-title"] == f"{skin.get_color('banner_title')} bold"
        assert overrides["sudo-prompt"] == f"{skin.get_color('ui_error')} bold"
        assert overrides["approval-title"] == f"{skin.get_color('ui_warn')} bold"

        set_active_skin("daylight")
        skin = get_active_skin()
        overrides = get_prompt_toolkit_style_overrides()
        assert overrides["status-bar"] == f"bg:{skin.get_color('status_bar_bg')} {skin.get_color('banner_text')}"
        assert overrides["voice-status"] == f"bg:{skin.get_color('voice_status_bg')} {skin.get_color('ui_label')}"
