"""Tests for the lifecycle, with no Qt, no display and no real server.

Written against the standard library so that they run wherever Python does, and so that testing
re/app never becomes a reason to install anything.

Everything the application would talk to is stood in for, so these say what the logic does rather
than what a machine happened to be doing at the time.
"""

import json
import os
import signal
import tempfile
import unittest
from unittest import mock

import server
from server import Endpoint, Server, ServerError


class FakeProcess:
    """A started process whose fate the test decides."""

    def __init__(self, exits_with=None):
        self._exits_with = exits_with
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._exits_with

    def terminate(self):
        self.terminated = True
        self._exits_with = 0

    def wait(self, timeout=None):
        return self._exits_with

    def kill(self):
        self.killed = True


def answering(reply):
    """Stands in for asking a server what it is."""
    return lambda url, timeout=1.0: reply


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.directory = self._directory.name
        self.record_path = os.path.join(self.directory, "server.json")
        self.executable = os.path.join(self.directory, "app")
        with open(self.executable, "w", encoding="utf-8"):
            pass

    def tearDown(self):
        self._directory.cleanup()

    def write_record(self, url="http://127.0.0.1:41234", version="1.0.0", process_id=1234):
        with open(self.record_path, "w", encoding="utf-8") as file:
            json.dump({"url": url, "version": version, "processId": process_id}, file)

    def a_server(self, executable=None, **kwargs):
        kwargs.setdefault("launcher", lambda command: FakeProcess())
        kwargs.setdefault("sleep", lambda seconds: None)
        return Server(self.record_path, executable or self.executable, **kwargs)


class ReadingTheRecord(LifecycleTest):
    def test_reads_a_record(self):
        self.write_record()
        endpoint = Endpoint.read(self.record_path)
        self.assertEqual("http://127.0.0.1:41234", endpoint.url)
        self.assertEqual("1.0.0", endpoint.version)

    def test_no_record_at_all(self):
        self.assertIsNone(Endpoint.read(self.record_path))

    def test_unreadable_record(self):
        with open(self.record_path, "w", encoding="utf-8") as file:
            file.write("{ this is not json")
        self.assertIsNone(Endpoint.read(self.record_path))

    def test_record_with_no_address(self):
        with open(self.record_path, "w", encoding="utf-8") as file:
            json.dump({"version": "1.0.0", "processId": 1234}, file)
        self.assertIsNone(Endpoint.read(self.record_path))

    def test_record_that_is_not_an_object(self):
        with open(self.record_path, "w", encoding="utf-8") as file:
            file.write('"just a string"')
        self.assertIsNone(Endpoint.read(self.record_path))


class ReusingWhatIsAlreadyRunning(LifecycleTest):
    def test_reuses_a_server_that_is_already_running(self):
        self.write_record()
        with mock.patch.object(server, "status", answering({"application": "re/log", "version": "1.0.0"})):
            running = self.a_server(application="re/log", version="1.0.0")
            self.assertEqual("http://127.0.0.1:41234", running.ensure_running())
            self.assertIsNone(running.process, "nothing should have been started")

    def test_ignores_a_record_when_nothing_answers(self):
        self.write_record()
        with mock.patch.object(server, "status", answering(None)):
            self.assertIsNone(self.a_server(application="re/log").running())

    def test_ignores_something_else_answering_on_that_port(self):
        self.write_record()
        with mock.patch.object(server, "status", answering({"application": "something else"})):
            self.assertIsNone(self.a_server(application="re/log").running())

    def test_does_not_reuse_a_server_of_another_version(self):
        self.write_record(version="0.9.0")
        with mock.patch.object(server, "status", answering({"application": "re/log", "version": "0.9.0"})):
            self.assertIsNone(self.a_server(application="re/log", version="1.0.0").running())

    def test_reuses_regardless_of_version_when_none_is_expected(self):
        self.write_record()
        with mock.patch.object(server, "status", answering({"application": "re/log", "version": "0.9.0"})):
            self.assertEqual("http://127.0.0.1:41234", self.a_server(application="re/log").running())


class StartingOne(LifecycleTest):
    def test_starts_a_server_when_none_is_running(self):
        started = []

        def launch(command):
            started.append(command)
            self.write_record()          # the server publishes where it is listening
            return FakeProcess()

        with mock.patch.object(server, "status", answering({"application": "re/log", "version": "1.0.0"})):
            running = self.a_server(launcher=launch, application="re/log", version="1.0.0")
            self.assertEqual("http://127.0.0.1:41234", running.ensure_running())
            self.assertEqual(1, len(started))

    def test_gives_up_when_the_server_never_answers(self):
        ticks = iter(range(0, 100))
        with mock.patch.object(server, "status", answering(None)):
            running = self.a_server(timeout=5, clock=lambda: next(ticks), application="re/log")
            with self.assertRaisesRegex(ServerError, "did not start"):
                running.start()

    def test_gives_up_when_the_server_exits_while_starting(self):
        with mock.patch.object(server, "status", answering(None)):
            running = self.a_server(launcher=lambda command: FakeProcess(exits_with=1), application="re/log")
            with self.assertRaisesRegex(ServerError, "stopped while starting"):
                running.start()

    def test_says_so_when_the_application_is_not_where_it_should_be(self):
        running = self.a_server(executable=os.path.join(self.directory, "not-here"))
        with self.assertRaisesRegex(ServerError, "cannot find"):
            running.start()


class Stopping(LifecycleTest):
    def test_stops_a_server_it_started(self):
        process = FakeProcess()

        def launch(command):
            self.write_record()
            return process

        with mock.patch.object(server, "status", answering({"application": "re/log"})):
            running = self.a_server(launcher=launch, application="re/log")
            running.ensure_running()
            running.stop()

        self.assertTrue(process.terminated)

    def test_does_not_stop_a_server_it_only_attached_to(self):
        self.write_record()
        with mock.patch.object(server, "status", answering({"application": "re/log"})):
            running = self.a_server(application="re/log")
            running.ensure_running()
            running.stop()          # must not raise; there is nothing of ours to stop

        self.assertIsNone(running.process)


class ReplacingAnOlderOne(LifecycleTest):
    """A server left running by a previous version has to go before ours can have the port."""

    def a_replacing_server(self, answer, **kwargs):
        self.signalled = []
        self.write_record(process_id=os.getpid())          # a process that is really alive
        kwargs.setdefault("application", "re/log")
        kwargs.setdefault("version", "2.0.0")
        kwargs.setdefault("signaller", lambda pid, sig: self.signalled.append((pid, sig)))
        kwargs.setdefault("stop_timeout", 0)               # do not linger over a fake process
        return self.a_server(**kwargs), answer

    def test_recognises_a_server_of_an_older_version(self):
        running, answer = self.a_replacing_server({"application": "re/log", "version": "1.0.0"})
        with mock.patch.object(server, "status", answering(answer)):
            self.assertIsNotNone(running.outdated())

    def test_does_not_call_a_matching_server_outdated(self):
        running, answer = self.a_replacing_server({"application": "re/log", "version": "2.0.0"})
        with mock.patch.object(server, "status", answering(answer)):
            self.assertIsNone(running.outdated())

    def test_will_not_touch_a_different_application_on_that_port(self):
        # Somebody else's program is not ours to stop, whatever it is holding.
        running, answer = self.a_replacing_server({"application": "something else", "version": "1.0.0"})
        with mock.patch.object(server, "status", answering(answer)):
            self.assertIsNone(running.outdated())

    def test_will_not_touch_anything_when_no_version_is_expected(self):
        running, answer = self.a_replacing_server({"application": "re/log", "version": "1.0.0"}, version=None)
        with mock.patch.object(server, "status", answering(answer)):
            self.assertIsNone(running.outdated())

    def test_ignores_a_record_whose_process_is_gone(self):
        running, answer = self.a_replacing_server({"application": "re/log", "version": "1.0.0"})
        self.write_record(process_id=2147483647)           # a process that cannot exist
        with mock.patch.object(server, "status", answering(answer)):
            self.assertIsNone(running.outdated())

    def test_stops_the_older_server_then_starts_its_own(self):
        started = []

        def launch(command):
            started.append(command)
            return FakeProcess()

        running, answer = self.a_replacing_server(
            {"application": "re/log", "version": "1.0.0"}, launcher=launch)

        # The old server answers as version 1; ours is version 2, so it is replaced and then, once
        # the record is ours, attached to.
        replies = [answer, answer, {"application": "re/log", "version": "2.0.0"}]
        with mock.patch.object(server, "status", lambda url, timeout=1.0: replies.pop(0) if len(replies) > 1 else replies[0]):
            running.ensure_running()

        self.assertEqual([(os.getpid(), signal.SIGTERM)], [s for s in self.signalled if s[1] == signal.SIGTERM])
        self.assertEqual(1, len(started), "ours should have been started after the old one stopped")

    def test_does_not_stop_anything_when_nothing_is_running(self):
        self.signalled = []          # setUp leaves no record, which is the case being tested

        def launch(command):
            self.write_record(version="2.0.0")
            return FakeProcess()

        running = self.a_server(application="re/log", version="2.0.0", launcher=launch,
                                signaller=lambda pid, sig: self.signalled.append((pid, sig)))
        with mock.patch.object(server, "status", answering({"application": "re/log", "version": "2.0.0"})):
            running.ensure_running()

        self.assertEqual([], self.signalled)


class ReadingTheSettings(LifecycleTest):
    """How the window should behave, as the application writes it down."""

    def write_settings(self, contents):
        path = os.path.join(self.directory, "desktop.json")
        with open(path, "w", encoding="utf-8") as file:
            file.write(contents)
        return path

    def test_reads_what_the_application_wrote(self):
        path = self.write_settings(json.dumps({"runInBackground": False, "showTrayIcon": False}))
        found = server.settings(path)
        self.assertFalse(found["runInBackground"])
        self.assertFalse(found["showTrayIcon"])

    def test_keeps_the_two_apart(self):
        path = self.write_settings(json.dumps({"runInBackground": False, "showTrayIcon": True}))
        found = server.settings(path)
        self.assertFalse(found["runInBackground"])
        self.assertTrue(found["showTrayIcon"])

    def test_keeps_running_and_shows_the_tray_when_nothing_was_written(self):
        found = server.settings(os.path.join(self.directory, "desktop.json"))
        self.assertTrue(found["runInBackground"])
        self.assertTrue(found["showTrayIcon"])

    def test_falls_back_when_no_path_is_configured(self):
        self.assertEqual(server.DEFAULT_SETTINGS, server.settings(None))

    def test_falls_back_when_the_file_cannot_be_read(self):
        path = self.write_settings("{ this is not json")
        self.assertEqual(server.DEFAULT_SETTINGS, server.settings(path))

    def test_falls_back_when_it_is_not_an_object(self):
        path = self.write_settings('"just a string"')
        self.assertEqual(server.DEFAULT_SETTINGS, server.settings(path))

    def test_fills_in_whatever_is_missing(self):
        path = self.write_settings(json.dumps({"runInBackground": False}))
        found = server.settings(path)
        self.assertFalse(found["runInBackground"])
        self.assertTrue(found["showTrayIcon"], "a setting nobody wrote keeps its default")

    def test_ignores_anything_it_does_not_know_about(self):
        # Asserted this way rather than against the whole set of defaults, so that adding a setting
        # does not break a test about something else.
        found = self.write_settings(json.dumps({"runInBackground": False, "somethingElse": 42}))
        settings = server.settings(found)

        self.assertNotIn("somethingElse", settings)
        self.assertFalse(settings["runInBackground"])

    def test_leaves_the_title_bar_alone_unless_asked(self):
        self.assertFalse(server.settings(None)["hideTitleBar"])

    def test_hides_the_title_bar_when_asked(self):
        path = self.write_settings(json.dumps({"hideTitleBar": True}))
        self.assertTrue(server.settings(path)["hideTitleBar"])


if __name__ == "__main__":
    unittest.main()
