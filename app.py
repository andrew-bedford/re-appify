#!/usr/bin/python3
import os
import signal
import sys
import configparser

import server as lifecycle
from server import Server, ServerError

from PyQt6 import QtWidgets, QtWebEngineWidgets, QtWebEngineCore, QtCore
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QSplashScreen, QLabel, QSizePolicy,
                             QStyle, QSystemTrayIcon, QMenu)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings, QWebEngineProfile
from PyQt6.QtCore import Qt, QUrl, QTimer
from PyQt6.QtGui import QDesktopServices, QPixmap, QIcon, QGuiApplication

# Where re/app itself lives. Everything it ships with is found relative to this rather than to
# wherever it happened to be launched from, because an installed application is started from
# anywhere and cannot assume it can even write to the current directory.
HERE = os.path.dirname(os.path.realpath(__file__))

def cache_directory():
    """Somewhere writable for things re/app makes rather than ships."""
    root = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    directory = os.path.join(root, "re", "app")
    os.makedirs(directory, exist_ok=True)
    return directory

def screenshot_path():
    return os.path.join(cache_directory(), "screenshot.png")

def take_screenshot():
    """Photographs the desktop for the window to sit blurred in front of.

    Says whether it got one. Wayland does not let a window help itself to the screen, and the grab
    comes back empty rather than refusing, so a launch that cannot have a picture has to throw the
    previous one away: saving nothing over it would leave the last capture that worked on disk, and
    the window would open in front of a picture of some earlier day's desktop for good.
    """
    screen = QGuiApplication.primaryScreen()
    screenshot = QPixmap()
    if screen is not None:
        screen_geometry = screen.geometry()
        # TODO: Check if this will work with multiple screens, probably not.
        screenshot = screen.grabWindow(0, screen_geometry.left(), screen_geometry.top(), screen_geometry.width(), screen_geometry.height())

    if screenshot.isNull():
        try:
            os.remove(screenshot_path())
        except OSError:
            pass
        return False

    return screenshot.save(screenshot_path())

def desktop_screenshot(screen):
    """The last photograph of the desktop, at the size it is meant to be drawn.

    A pixmap does not carry its device pixel ratio through a PNG, so on a scaled screen it comes
    back saying it is twice the size it should be drawn at and only its middle would show. What the
    ratio was is worked out again from the screen rather than remembered.
    """
    screenshot = QPixmap(screenshot_path())
    width = screen.geometry().width() if screen is not None else 0
    if not screenshot.isNull() and width > 0:
        screenshot.setDevicePixelRatio(max(1.0, screenshot.width() / width))
    return screenshot

# Named after the application rather than shared, so that two applications built on re/app do not
# end up reading each other's local storage, cookies and cache.
PROFILE_NAME = "re-app"

class OpenLinksInDesktopBrowserWebEnginePage(QWebEnginePage):
    def __init__(self, webengine_view):
        persistent_profile = QWebEngineProfile(PROFILE_NAME, webengine_view)
        super().__init__(persistent_profile, webengine_view)

    def acceptNavigationRequest(self, url, navType, isMainFrame):
        if (navType == QWebEnginePage.NavigationType.NavigationTypeLinkClicked):
            # Use the system's default URL handler.
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, navType, isMainFrame)

class MainWindow(QtWidgets.QMainWindow):
    def loadConfig(self):
        config = configparser.ConfigParser()
        config.read(os.path.join(HERE, '_internal', 'config.ini'))
        self.iconPath = config.get('App', 'icon')
        self.title = config.get('App', 'title')
        self.close_confirmation = config.get('App', 'close_confirmation', fallback=None)

        # The name of the .desktop file this application is installed with, without the extension.
        # Telling Qt lets the desktop associate the window with its entry, so it is named and iconed
        # in a window switcher rather than showing up as a stray python process.
        self.desktop_file = config.get('App', 'desktop_file', fallback=None)

        # What to run, and where it writes down the address it is listening on. Between them these
        # replace the old pairing of a source directory to run `dotnet run` in and a fixed url to
        # poll: an installed application has neither a source tree nor a port it can count on.
        self.executable = os.path.expanduser(config.get('App', 'executable'))
        self.endpoint = os.path.expanduser(config.get('App', 'endpoint'))
        self.application = config.get('App', 'application', fallback=None)

        # The version this copy of the application expects its server to be. When it is set and a
        # server of another version is running, that server is stopped and replaced rather than
        # attached to - otherwise an update would show you the previous version's interface.
        self.expected_version = config.get('App', 'version', fallback=None)

        # Settings the application itself writes, about how its window should behave. Read fresh
        # each time they are needed rather than kept, so that changing one takes effect at once
        # instead of at the next launch.
        settings = config.get('App', 'settings', fallback=None)
        self.settings_path = os.path.expanduser(settings) if settings else None
        self.startup_timeout = config.getfloat('App', 'startup_timeout', fallback=30.0)

    def desktopSettings(self):
        return lifecycle.settings(self.settings_path)

    def showTrayIcon(self):
        """Somewhere to see that the application is still running, and to stop it.

        Only worth having when something is going to be left running: with the server stopping along
        with the window there is nothing for it to represent.
        """
        settings = self.desktopSettings()
        if not (settings["runInBackground"] and settings["showTrayIcon"]):
            return

        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray = QSystemTrayIcon(QIcon(self.icon), self)
        self.tray.setToolTip(self.title)

        menu = QMenu()
        menu.addAction("Open", self.showNormal)
        menu.addSeparator()
        menu.addAction("Quit", self.quitCompletely)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def quitCompletely(self):
        """Stops the server as well as the window, whatever the background setting says.

        Quit from the tray is the one place someone has asked for exactly that, so it is the one
        place that ignores the setting.
        """
        if self.server is not None:
            self.server.stop()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

    def startServer(self):
        """Finds a server already running, or starts one, and loads it.

        Failure ends here rather than in a splashscreen nobody can get past: if the application
        cannot be started there is nothing to wait for, and saying so is the only useful thing left.
        """
        self.server = Server(
            self.endpoint,
            self.executable,
            application=self.application,
            version=self.expected_version,
            timeout=self.startup_timeout,
        )

        try:
            self.url = self.server.ensure_running()
        except ServerError as failure:
            self.showFailure(str(failure))
            return

        self.showTrayIcon()

        self.browser.setUrl(QUrl(self.url))
        self.browser.loadFinished.connect(self.delayedShowBrowser)

    def showFailure(self, message):
        self.splash.setPixmap(QPixmap())
        self.splash.setText(message)
        self.splash.setWordWrap(True)
        self.splash.setStyleSheet("color: white; font-size: 16px; padding: 48px;")
        self.splash.show()

    # We introduce a small delay to give the pages a bit more time to render
    # after loading.
    def delayedShowBrowser(self):
        self.timer=QTimer()
        self.timer.timeout.connect(self.showBrowser)
        self.timer.start(100)

    def showBrowser(self):
        self.timer.stop()
        self.splash.hide()
        self.browser.show()

    def showMaximized(self):
        """Asks to be maximised, and expects to have to ask again.

        QtWebEngine gives the window a native surface of its own only once the window is already up,
        and the desktop takes that new surface for a window that never asked to be maximised: a
        state change back to normal follows a moment later and the window settles at whatever size
        a floating one is given - two thirds of the screen, here. So the request is remembered
        rather than assumed to have been granted.
        """
        self.awaitingMaximized = True
        super().showMaximized()

    def showSplashscreen(self):
        self.splash = QLabel(self.main_widget)
        self.splash.setGeometry(self.rect())
        self.splash.setPixmap(self.icon)
        self.splash.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.show()

    # FIXME: Unsuccessful tentative to get (optional) spell checking working.
    def enableSpellCheck(self):
        profile = self.browser.page().profile()
        profile.setSpellCheckEnabled = True
        profile.setSpellCheckLanguages = ["en-US"]

    def __init__(self, *args, **kwargs):
        super(MainWindow, self).__init__(*args, **kwargs)
        # self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint) # For a frameless window

        self.tray = None
        self.server = None

        # Both are needed before the things they describe exist, because a resize and a state change
        # can arrive while the window is still being built.
        self.splash = None
        self.awaitingMaximized = False

        self.main_widget = QWidget(self)
        self.setGeometry(0, 0, 1280, 720)
        self.setCentralWidget(self.main_widget)

        self.background_widget = QWidget(self.main_widget)
        self.background = QLabel(self.background_widget)
        self.screenshot = desktop_screenshot(self.screen())
        self.background.setPixmap(self.screenshot)
        self.background.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layOutBackground()

        # Add a blur effect to the screenshot used as background for the window
        blur = QtWidgets.QGraphicsBlurEffect()
        blur.setBlurRadius(15.0)
        self.background.setGraphicsEffect(blur)

        # Add a black semi-transparent layer over the background to darken it
        self.color_layer = QWidget(self.main_widget)
        self.color_layer.setStyleSheet("background-color: rgba(0, 0, 0, 200);")
        self.color_layer.setGeometry(self.rect())

        self.previousWindowState = self.windowState()

        # self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed) # Prevents the window from resizing when changing the central widget
        self.loadConfig()

        # Some desktops hide title bars - a tiling one has no use for them - and there is no way to
        # ask which, so the application says. Applied here because a window's frame is decided when
        # it is built, which is why changing this takes effect the next time re/log is opened.
        if self.desktopSettings()["hideTitleBar"]:
            self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)

        if self.desktop_file:
            QApplication.setDesktopFileName(self.desktop_file)
        self.setWindowTitle(self.title)
        self.resize(self.screen().geometry().width(), self.screen().geometry().height()) # Use screen dimensions as default window size
        self.icon = QPixmap(self.iconPath)
        self.setWindowIcon(QIcon(self.icon))

        self.browser = QWebEngineView(self.main_widget)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, True)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
        self.browser.setGeometry(self.rect())
        self.browser.setPage(OpenLinksInDesktopBrowserWebEnginePage(self.browser))
        self.browser.page().setBackgroundColor(QtCore.Qt.GlobalColor.transparent)
        self.browser.page().quotaRequested.connect(lambda request: request.accept())

        self.showSplashscreen()

        # After the splashscreen is up, so that there is something to look at while the server
        # starts, and after a single pass of the event loop so that it is actually painted.
        QTimer.singleShot(50, self.startServer)

    def layOutBackground(self):
        """Lines the blurred desktop up with the desktop it is a picture of.

        The widget is offset by where the window is, so that its own origin sits on the screen's and
        what shows through the window is the part of the desktop the window is standing on. The
        picture inside it is then the size of the screen, said explicitly: a QLabel left to work it
        out takes the size of its pixmap instead, so a capture smaller than the screen - or the one
        left behind by a desktop that cannot be captured at all - was drawn as a rectangle ending
        partway across the window, with a visible edge where it stopped.
        """
        self.background_widget.setGeometry(-self.x(), -self.y(), self.width()+self.x(), self.height()+self.y())
        screen_geometry = self.screen().geometry()
        self.background.setGeometry(0, 0, screen_geometry.width(), screen_geometry.height())

    def resizeEvent(self, event):
        self.layOutBackground()
        self.color_layer.setGeometry(self.rect())
        self.browser.setGeometry(self.rect())
        # The splashscreen has to follow the window like everything else on it. It used to be sized
        # once, before the desktop had said how big the window would actually be, so the logo was
        # centred on a rectangle the size of the whole screen and came out low and to the right of
        # the middle - or off the window altogether when the window ended up smaller still.
        if self.splash is not None:
            self.splash.setGeometry(self.rect())
        super().resizeEvent(event)

    def moveEvent(self, event):
        self.layOutBackground()
        super().moveEvent(event)

    def update_background(self):
        take_screenshot()
        self.screenshot = desktop_screenshot(self.screen())
        self.background.setPixmap(self.screenshot)

    def changeEvent(self, event):
        if event.type() == QtCore.QEvent.Type.WindowStateChange:
            # Losing the maximised state before anyone has touched the window is the web engine's
            # doing rather than a choice, so ask again. Only ever once: the next window that stops
            # being maximised is someone un-maximising it, and that has to be allowed to work.
            if self.awaitingMaximized and not self.isMaximized():
                self.awaitingMaximized = False
                super().showMaximized()

            # HACK: Update background when un-minimizing
            if ((self.previousWindowState & QtCore.Qt.WindowState.WindowMinimized) and
                (not self.windowState() or (self.windowState() & QtCore.Qt.WindowState.WindowMaximized))):
                self.update_background()

            self.previousWindowState = self.windowState()

        super().changeEvent(event)

    def closeEvent(self, event):
        # Read now rather than at startup, so that turning the setting off and closing the window
        # does what it says in the same session.
        if self.server is not None and not self.desktopSettings()["runInBackground"]:
            self.server.stop()

        if self.close_confirmation:
            reply = QtWidgets.QMessageBox.question(self, 'Confirm Exit', self.close_confirmation,
                                                   QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                                                   QtWidgets.QMessageBox.StandardButton.No)
            if reply == QtWidgets.QMessageBox.StandardButton.Yes:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

if __name__ == '__main__':
    # QtWebEngine dictionaries are required for spell checking.
    os.environ["QTWEBENGINE_DICTIONARIES_PATH"] = os.path.join(
        HERE, "_internal", "qtwebengine_dictionaries"
    )

    app = QtWidgets.QApplication(sys.argv)
    take_screenshot() # Take initial screenshot before showing the main window
    window = MainWindow()
    window.showMaximized()

    # Being asked to stop - at logout, or by anything that sends a term signal - goes through the
    # same closing as the window being closed, so that whatever the application asked to happen when
    # its window closes happens here too rather than being skipped.
    signal.signal(signal.SIGTERM, lambda number, frame: window.close())

    # Qt's event loop does not run Python code while it waits, so a signal that arrives during the
    # wait is not delivered until something else happens. Waking briefly and regularly gives Python
    # the chance.
    signals = QTimer()
    signals.start(200)
    signals.timeout.connect(lambda: None)

    app.exec()
