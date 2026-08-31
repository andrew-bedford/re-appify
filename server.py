"""Finding, starting and stopping the application's server.

Kept apart from the window on purpose. This is the part that can be wrong in ways nobody notices -
attaching to the wrong server, waiting forever for one that will never answer, starting a second
copy of something already running - and none of it needs a screen to exercise.
"""

import json
import os
import signal
import subprocess
import time

import requests


class ServerError(Exception):
    """The server could not be started, or never became ready."""


class Endpoint:
    """Where a server said it was listening, as it wrote it down."""

    def __init__(self, url, version, process_id):
        self.url = url
        self.version = version
        self.process_id = process_id

    @staticmethod
    def read(path):
        """The record at `path`, or None for every way of not having one.

        Missing, unreadable, half written and incomplete all mean the same thing to a caller - there
        is nothing to attach to - so they are not distinguished here.
        """
        try:
            with open(path, "r", encoding="utf-8") as file:
                record = json.load(file)
        except (OSError, ValueError):
            return None

        if not isinstance(record, dict):
            return None

        url = record.get("url")
        if not url:
            return None

        return Endpoint(url, record.get("version"), record.get("processId"))


#: How the window behaves when the application has not said otherwise: keep the server running once
#: the window closes, show a tray icon so that it is visible while it does, and leave the title bar
#: alone. The title bar stays by default because a window without one cannot be dragged on desktops
#: that expect it to be, and only the person using it knows whether theirs is such a desktop.
DEFAULT_SETTINGS = {"runInBackground": True, "showTrayIcon": True, "hideTitleBar": False}


def settings(path):
    """How the application says its window should behave.

    Every way of not knowing gives the same answer as never having been asked. A window that would
    not open because a settings file was truncated would be a worse failure than any of these
    settings being wrong.
    """
    if not path:
        return dict(DEFAULT_SETTINGS)

    try:
        with open(path, "r", encoding="utf-8") as file:
            found = json.load(file)
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)

    if not isinstance(found, dict):
        return dict(DEFAULT_SETTINGS)

    return {key: found.get(key, default) for key, default in DEFAULT_SETTINGS.items()}


def alive(process_id):
    """Whether a process is still there.

    Signal zero asks the question without sending anything. Not being allowed to signal it is still
    an answer: it exists.
    """
    if not process_id:
        return False

    try:
        os.kill(process_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError):
        return False


def status(url, timeout=1.0):
    """What the thing answering at `url` says it is, or None if it is not answering or not ours.

    Answering on a port is not evidence of being the application: anything at all could be there,
    including a stale copy of something else. This asks.
    """
    try:
        response = requests.get(url.rstrip("/") + "/api/status", timeout=timeout)
    except requests.exceptions.RequestException:
        return None

    if response.status_code != 200:
        return None

    try:
        answer = response.json()
    except ValueError:
        return None

    return answer if isinstance(answer, dict) and answer.get("application") else None


class Server:
    """The application's server: found if it is already running, started if it is not."""

    def __init__(self, endpoint_path, executable, application=None, version=None,
                 timeout=30.0, poll=0.1, stop_timeout=10.0, launcher=subprocess.Popen,
                 clock=time.monotonic, sleep=time.sleep, signaller=os.kill):
        self.endpoint_path = endpoint_path
        self.executable = executable
        self.application = application
        self.version = version
        self.timeout = timeout
        self.poll = poll
        self.stop_timeout = stop_timeout
        self.url = None
        self.process = None

        # Injected so the waiting can be tested without actually waiting, and starting without
        # actually starting anything.
        self._launch = launcher
        self._now = clock
        self._sleep = sleep
        self._signal = signaller

    def running(self):
        """The URL of a server already running and fit to use, or None.

        Fit to use means it answers, says it is the application we want, and is the version we
        expect. A server from an older version is not reused: attaching to it would show the
        previous version's interface with nothing on screen to explain why.
        """
        endpoint = Endpoint.read(self.endpoint_path)
        if endpoint is None:
            return None

        answer = status(endpoint.url)
        if answer is None:
            return None

        if self.application is not None and answer.get("application") != self.application:
            return None

        if self.version is not None and answer.get("version") != self.version:
            return None

        return endpoint.url

    def outdated(self):
        """A running server that is ours but the wrong version, or None.

        Deliberately narrow. Not answering could be anything; answering as a different application
        means the port belongs to somebody else's program, and stopping that would be inexcusable.
        Only a live server that says it is this application, at a version this window does not
        match, has both a reason to be replaced and the standing to be.
        """
        if self.version is None:
            return None

        endpoint = Endpoint.read(self.endpoint_path)
        if endpoint is None or not alive(endpoint.process_id):
            return None

        answer = status(endpoint.url)
        if answer is None:
            return None

        if self.application is not None and answer.get("application") != self.application:
            return None

        return None if answer.get("version") == self.version else endpoint

    def replace(self, endpoint):
        """Stops a server so that ours can have the port."""
        try:
            self._signal(endpoint.process_id, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            return

        deadline = self._now() + self.stop_timeout
        while self._now() < deadline:
            if not alive(endpoint.process_id):
                return
            self._sleep(self.poll)

        try:
            self._signal(endpoint.process_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def start(self):
        """Starts the server and waits for it to answer.

        Raises rather than returning a failure, because there is nothing sensible for the caller to
        do with a server that is not there, and the alternative - a splashscreen that never goes
        away - is what this replaces.
        """
        if not self.executable or not os.path.exists(self.executable):
            raise ServerError("re/app cannot find the application to start: %s" % self.executable)

        self.process = self._launch([self.executable])

        deadline = self._now() + self.timeout
        while self._now() < deadline:
            endpoint = Endpoint.read(self.endpoint_path)
            if endpoint is not None and status(endpoint.url) is not None:
                self.url = endpoint.url
                return self.url

            if self.process.poll() is not None:
                raise ServerError("the application stopped while starting up")

            self._sleep(self.poll)

        raise ServerError("the application did not start within %g seconds" % self.timeout)

    def ensure_running(self):
        """The URL to load: whatever is already there if it will do, otherwise a server we start.

        A server left running by an older version is stopped first rather than reused. Attaching to
        it would show the previous version's interface after an update, with nothing on screen to
        say why, and it is holding the port ours needs anyway.
        """
        self.url = self.running()
        if self.url is not None:
            return self.url

        stale = self.outdated()
        if stale is not None:
            self.replace(stale)

        return self.start()

    def stop(self):
        """Stops the server, if this is the one that started it."""
        if self.process is None or self.process.poll() is not None:
            return

        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
