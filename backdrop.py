"""Decides what the window stands in front of, apart from the window.

re/app draws a blurred picture of the desktop behind a transparent web view, so that the page looks
like glass. On X11 that picture is a photograph of the screen. Wayland does not let a window help
itself to the screen, so there the wallpaper stands in for the photograph: it is what most of a
desktop is, it needs no permission, and unlike a capture it cannot turn out to be a picture of
whatever window happened to be open when the application started.

Nothing here touches Qt or a display. What the environment says, and what gsettings answers, are
handed in, so that every desktop's rule can be exercised without that desktop. Like the rest of
re/app it is forgiving: a setting that is missing, unreadable or points at nothing means no
wallpaper, never an error, because the window is perfectly usable without one.
"""

import os
import re
import subprocess
from urllib.parse import unquote, urlparse

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

COSMIC_BACKGROUND = os.path.join("com.system76.CosmicBackground", "v1")

# RON, as COSMIC writes it: `source: Path("/some/where.png"),`. Only a Path source is a picture; a
# Color source is a flat colour the window has no use for.
COSMIC_PATH_SOURCE = re.compile(r'\bsource\s*:\s*Path\(\s*"((?:[^"\\]|\\.)*)"\s*\)')


def can_photograph(platform, environ=None):
    """Whether a grab of the screen will return the screen.

    Asked of the session rather than read off the result, because the result lies: under XWayland a
    grab of the root window succeeds and hands back a black rectangle, which is not empty and would
    have been drawn, so an application Qt happened to start on xcb inside a Wayland session opened in
    front of nothing at all.
    """
    environ = os.environ if environ is None else environ
    if (platform or "").startswith("wayland"):
        return False
    return environ.get("XDG_SESSION_TYPE", "").lower() != "wayland"


def find_wallpaper(environ=None, gsettings=None, output=None):
    """The path of the picture the desktop is wearing, or None.

    Only desktops whose settings are known are asked. Guessing at the others would mean reading
    another desktop's leftover configuration and drawing a wallpaper that is not on the screen.
    """
    environ = os.environ if environ is None else environ
    desktops = [name.strip().lower() for name in environ.get("XDG_CURRENT_DESKTOP", "").split(":")]

    if "cosmic" in desktops:
        return cosmic_wallpaper(cosmic_config_directories(environ), output)
    if "gnome" in desktops:
        return gnome_wallpaper(gsettings or run_gsettings)
    return None


def gnome_wallpaper(gsettings):
    """GNOME keeps a second picture for dark mode, and shows it whenever dark mode is preferred."""
    background = "org.gnome.desktop.background"

    # 'none' is GNOME's way of saying the desktop is a flat colour, whatever picture-uri still holds.
    if gsettings(background, "picture-options") == "none":
        return None

    keys = ["picture-uri"]
    if gsettings("org.gnome.desktop.interface", "color-scheme") == "prefer-dark":
        keys.insert(0, "picture-uri-dark")

    for key in keys:
        image = image_at(path_from_uri(gsettings(background, key)))
        if image is not None:
            return image
    return None


def run_gsettings(schema, key):
    """A gsettings value with its quoting taken off, or None if it cannot be had."""
    try:
        result = subprocess.run(["gsettings", "get", schema, key],
                                capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return unquote_gvariant(result.stdout.strip())


def unquote_gvariant(value):
    """gsettings prints strings as GVariant text: in single quotes, or double quotes when the string
    itself holds a single quote, with backslash escapes either way."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    return value


def path_from_uri(value):
    if not value:
        return None
    if value.startswith("file://"):
        return unquote(urlparse(value).path)
    if value.startswith("/"):
        return value
    return None


def cosmic_config_directories(environ):
    """Where COSMIC looks for settings, the user's first.

    Each setting is a file of its own, and the user's copy of a file replaces the system's rather
    than adding to it, so the first directory holding a given file is the one that counts.
    """
    home = environ.get("HOME") or os.path.expanduser("~")
    config_home = environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    data_dirs = environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"

    directories = [os.path.join(config_home, "cosmic")]
    directories += [os.path.join(d, "cosmic") for d in data_dirs.split(":") if d]
    return directories


def cosmic_wallpaper(config_directories, output=None):
    """COSMIC keeps one background for every screen under `all`, or one per screen named after the
    output when `same-on-all` is switched off.

    The switch is read rather than the per-screen file simply preferred when it exists: turning
    same-on-all back on leaves the per-screen files where they were, and they would go on winning
    over the picture actually on the screen.
    """
    keys = []
    same_on_all = (read_first(config_directories, os.path.join(COSMIC_BACKGROUND, "same-on-all")) or "").strip()
    if same_on_all == "false" and output and os.sep not in output and output not in (".", ".."):
        keys.append(output)
    keys.append("all")

    for key in keys:
        entry = read_first(config_directories, os.path.join(COSMIC_BACKGROUND, key))
        if entry is None:
            continue
        match = COSMIC_PATH_SOURCE.search(entry)
        if match is None:
            # A setting that exists and is not a picture - a colour - is still the user's choice,
            # so the system's default picture underneath it is not shown instead.
            return None
        return image_at(re.sub(r"\\(.)", r"\1", match.group(1)))
    return None


def read_first(directories, relative):
    for directory in directories:
        try:
            with open(os.path.join(directory, relative), encoding="utf-8") as file:
                return file.read()
        except OSError:
            continue
    return None


def image_at(path):
    """A picture file, or the first picture in a folder of them.

    A folder is a slideshow. Which picture it is showing right now is not written down anywhere, so
    the first in name order is taken: blurred and darkened, a picture from the right set is a far
    better likeness than no picture at all. GNOME's own slideshows are XML files naming pictures,
    which there is no drawing, so they count as nothing.
    """
    if not path:
        return None
    if os.path.isfile(path):
        return None if path.lower().endswith(".xml") else path
    if os.path.isdir(path):
        try:
            names = sorted(os.listdir(path))
        except OSError:
            return None
        for name in names:
            candidate = os.path.join(path, name)
            if name.lower().endswith(IMAGE_EXTENSIONS) and os.path.isfile(candidate):
                return candidate
    return None
