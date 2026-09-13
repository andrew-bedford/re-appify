"""Tests for choosing the window's backdrop, with no Qt, no display and no desktop.

Each desktop's settings are written out as that desktop writes them, in a temporary directory, and
gsettings is a dictionary: these say what the rules are rather than what this machine's wallpaper
happens to be.
"""

import os
import tempfile
import unittest

import backdrop


class CanPhotographTests(unittest.TestCase):
    def test_an_x11_session_can_be_photographed(self):
        self.assertTrue(backdrop.can_photograph("xcb", {"XDG_SESSION_TYPE": "x11"}))

    def test_a_wayland_platform_cannot_be_photographed(self):
        self.assertFalse(backdrop.can_photograph("wayland", {"XDG_SESSION_TYPE": "wayland"}))

    def test_the_wayland_egl_platform_counts_as_wayland(self):
        self.assertFalse(backdrop.can_photograph("wayland-egl", {}))

    def test_xwayland_inside_a_wayland_session_cannot_be_photographed(self):
        # The grab succeeds there and returns black, so the session has to be asked.
        self.assertFalse(backdrop.can_photograph("xcb", {"XDG_SESSION_TYPE": "wayland"}))

    def test_a_session_that_does_not_say_is_assumed_photographable(self):
        # Windows and macOS set no XDG_SESSION_TYPE, and grabbing the screen works on both.
        self.assertTrue(backdrop.can_photograph("windows", {}))


class Desktop(unittest.TestCase):
    """A temporary home with somewhere to put pictures and COSMIC settings."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = self._directory.name
        self.config_home = os.path.join(self.root, "config")
        self.data_dir = os.path.join(self.root, "share")

    def tearDown(self):
        self._directory.cleanup()

    def picture(self, name="wallpaper.png"):
        path = os.path.join(self.root, "pictures", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as file:
            file.write(b"\x89PNG")
        return path

    def environ(self, desktop):
        return {
            "XDG_CURRENT_DESKTOP": desktop,
            "HOME": self.root,
            "XDG_CONFIG_HOME": self.config_home,
            "XDG_DATA_DIRS": self.data_dir,
        }


class GnomeTests(Desktop):
    def settings(self, **values):
        schema_of = {"color-scheme": "org.gnome.desktop.interface"}
        stored = {(schema_of.get(k.replace("_", "-"), "org.gnome.desktop.background"), k.replace("_", "-")): v
                  for k, v in values.items()}
        return lambda schema, key: stored.get((schema, key))

    def find(self, **values):
        return backdrop.find_wallpaper(self.environ("ubuntu:GNOME"), gsettings=self.settings(**values))

    def test_reads_the_picture_uri(self):
        path = self.picture()
        self.assertEqual(path, self.find(picture_uri="file://" + path))

    def test_prefers_the_dark_picture_when_dark_mode_is_preferred(self):
        light, dark = self.picture("light.png"), self.picture("dark.png")
        self.assertEqual(dark, self.find(picture_uri="file://" + light, picture_uri_dark="file://" + dark,
                                         color_scheme="prefer-dark"))

    def test_ignores_the_dark_picture_otherwise(self):
        light, dark = self.picture("light.png"), self.picture("dark.png")
        self.assertEqual(light, self.find(picture_uri="file://" + light, picture_uri_dark="file://" + dark,
                                          color_scheme="default"))

    def test_falls_back_to_the_light_picture_when_the_dark_one_is_gone(self):
        light = self.picture("light.png")
        self.assertEqual(light, self.find(picture_uri="file://" + light,
                                          picture_uri_dark="file:///nowhere/dark.png",
                                          color_scheme="prefer-dark"))

    def test_decodes_a_uri_with_spaces_in_it(self):
        path = self.picture("my wallpaper.png")
        self.assertEqual(path, self.find(picture_uri="file://" + path.replace(" ", "%20")))

    def test_has_no_picture_when_the_desktop_is_a_flat_colour(self):
        path = self.picture()
        self.assertIsNone(self.find(picture_uri="file://" + path, picture_options="none"))

    def test_has_no_picture_for_a_slideshow(self):
        path = self.picture("adwaita-timed.xml")
        self.assertIsNone(self.find(picture_uri="file://" + path))

    def test_has_no_picture_when_gsettings_cannot_be_asked(self):
        self.assertIsNone(self.find())


class GvariantTests(unittest.TestCase):
    def test_takes_off_single_quotes(self):
        self.assertEqual("file:///a.png", backdrop.unquote_gvariant("'file:///a.png'"))

    def test_takes_off_the_double_quotes_used_for_a_string_holding_a_single_quote(self):
        self.assertEqual("file:///andrew's.png", backdrop.unquote_gvariant('"file:///andrew\'s.png"'))

    def test_unescapes_backslashes(self):
        self.assertEqual("file:///it's.png", backdrop.unquote_gvariant("'file:///it\\'s.png'"))


class CosmicTests(Desktop):
    def setting(self, key, text, system=False):
        base = os.path.join(self.data_dir if system else self.config_home, "cosmic", backdrop.COSMIC_BACKGROUND)
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, key), "w", encoding="utf-8") as file:
            file.write(text)

    def entry(self, source, output="all"):
        # As COSMIC writes it, down to the trailing commas.
        return (f'(\n    output: "{output}",\n    source: {source},\n    filter_by_theme: true,\n'
                f'    rotation_frequency: 300,\n    filter_method: Lanczos,\n'
                f'    scaling_mode: Fit((0.0, 0.0, 0.0)),\n    sampling_method: Alphanumeric,\n)')

    def find(self, output=None):
        return backdrop.find_wallpaper(self.environ("COSMIC"), output=output)

    def test_reads_the_background_for_every_screen(self):
        path = self.picture()
        self.setting("all", self.entry(f'Path("{path}")'))
        self.assertEqual(path, self.find())

    def test_the_users_setting_replaces_the_systems(self):
        mine, default = self.picture("mine.png"), self.picture("default.jpg")
        self.setting("all", self.entry(f'Path("{default}")'), system=True)
        self.setting("all", self.entry(f'Path("{mine}")'))
        self.assertEqual(mine, self.find())

    def test_falls_back_to_the_systems_setting_when_the_user_has_none(self):
        default = self.picture("default.jpg")
        self.setting("all", self.entry(f'Path("{default}")'), system=True)
        self.assertEqual(default, self.find())

    def test_a_colour_the_user_chose_is_not_replaced_by_the_system_picture(self):
        default = self.picture("default.jpg")
        self.setting("all", self.entry(f'Path("{default}")'), system=True)
        self.setting("all", self.entry("Color(Single((0.1, 0.2, 0.3)))"))
        self.assertIsNone(self.find())

    def test_takes_the_first_picture_of_a_slideshow_folder(self):
        second, first = self.picture("b.jpg"), self.picture("a.jpg")
        self.setting("all", self.entry(f'Path("{os.path.dirname(first)}")'))
        self.assertEqual(first, self.find())

    def test_uses_the_screens_own_background_when_screens_differ(self):
        everywhere, laptop = self.picture("all.png"), self.picture("laptop.png")
        self.setting("same-on-all", "false")
        self.setting("all", self.entry(f'Path("{everywhere}")'))
        self.setting("eDP-1", self.entry(f'Path("{laptop}")', output="eDP-1"))
        self.assertEqual(laptop, self.find(output="eDP-1"))

    def test_ignores_a_screens_old_background_once_every_screen_is_the_same_again(self):
        everywhere, laptop = self.picture("all.png"), self.picture("laptop.png")
        self.setting("same-on-all", "true")
        self.setting("all", self.entry(f'Path("{everywhere}")'))
        self.setting("eDP-1", self.entry(f'Path("{laptop}")', output="eDP-1"))
        self.assertEqual(everywhere, self.find(output="eDP-1"))

    def test_an_output_name_cannot_reach_outside_the_settings(self):
        path = self.picture()
        self.setting("same-on-all", "false")
        self.setting("all", self.entry(f'Path("{path}")'))
        self.assertEqual(path, self.find(output="../../elsewhere"))

    def test_has_no_picture_when_the_picture_is_gone(self):
        self.setting("all", self.entry('Path("/nowhere/wallpaper.png")'))
        self.assertIsNone(self.find())

    def test_has_no_picture_without_any_settings(self):
        self.assertIsNone(self.find())


class DesktopTests(Desktop):
    def test_an_unknown_desktop_has_no_wallpaper(self):
        # Even with COSMIC settings lying around from a desktop that is not the one running.
        path = self.picture()
        base = os.path.join(self.config_home, "cosmic", backdrop.COSMIC_BACKGROUND)
        os.makedirs(base)
        with open(os.path.join(base, "all"), "w") as file:
            file.write(f'(source: Path("{path}"))')
        self.assertIsNone(backdrop.find_wallpaper(self.environ("KDE"), gsettings=lambda s, k: None))

    def test_no_desktop_at_all_has_no_wallpaper(self):
        self.assertIsNone(backdrop.find_wallpaper({}, gsettings=lambda s, k: None))


if __name__ == "__main__":
    unittest.main()
