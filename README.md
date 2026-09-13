# re/app
A simple python script that creates a desktop window using Qt, automatically starts your web app's server locally, displays a splashscreen with your application's logo while it's starting and then load the page in a webview once the server is reachable. Supports an acrylic-like effect through [pyqt-hackrylic](https://github.com/andrew-bedford/pyqt-hackrylic/).

![](https://github.com/andrew-bedford/pyqt-hackrylic/raw/main/Screenshots/QWebEngine.jpg)

**Note**: Work in progress.

## Development
### Running
```
pip3 install PyQt6 PyQt6-WebEngine
python app.py
```

### Configuration
To configure your re/app, edit the `config.ini` file that is located in the `_internal`. It allows you to specify:
 - `icon`: Path to the image that is to be used as the taskbar's icon and as the splashscreen.
 - `title`: The window title to display. At the moment, this window title is static, so it cannot be changed at runtime once the application starts.
 - `executable`: The application to run. This is a built application, not a source tree - re/app is meant to start something that is installed, which has no SDK and no sources to build from.
 - `endpoint`: The file your application writes the address it is listening on into. re/app reads this rather than assuming a port, so your application can take a free one and two copies never collide.
 - `application`: Optional. The name your application answers with at `/api/status`. When set, re/app refuses to attach to anything that gives a different answer, so whatever else happens to be listening is never mistaken for yours.
 - `startup_timeout`: Optional, 30 seconds by default. How long to wait for your application to answer before giving up and saying so.
 - `ready_attribute`: Optional. An attribute your page sets on its `<html>` element once it has drawn itself. The splashscreen stays up until it appears, so a page that builds itself from script after loading is not seen doing it. Without it the page is shown as soon as it has loaded.
 - `ready_timeout`: Optional, 5 seconds by default. The longest to wait for `ready_attribute`, so a page that never sets it is shown late rather than not at all.

### What your application has to do
re/app no longer polls a fixed url, because something answering on a port is not evidence of being your application. Instead your application should:
 - Write a small JSON file at the `endpoint` path once it is listening, holding `url`, `version` and `processId`. Write it to one side and move it into place, so a reader never sees half of it, and remove it when you shut down cleanly.
 - Answer `GET /api/status` with `{"application": "...", "version": "..."}`.

### Running the tests
The lifecycle - finding a server, reusing it, starting one, giving up on one that never answers - is in `server.py`, apart from the window, and is tested without Qt or a display:
```
python3 -m unittest test_server
```

### Publishing
To generate an installer for your application, you can use PyInstaller:
```
pip3 install PyInstaller
pyinstaller app.spec
```
Note that it has to be run on the platform that you are targeting (e.g., on Windows for a Windows installer).

## FAQ
### Why?
I was developing .NET web applications that I wanted to run in desktop windows. Existing options didn't quite meet my needs:
 - [MAUI](https://github.com/dotnet/maui) is not available on Linux, my main operating system.
 - [Electron.NET](https://github.com/ElectronNET/) and [SpiderEye](https://github.com/JBildstein/SpiderEye) were a few .NET versions behind, which prevented me from using either.
 - [Photino](https://github.com/tryphotino/photino.NET) seemed promising, but I encountered issues on Windows (application would not load) and on Linux (WebKit quirks).

All I wanted was a cross-platform way to automatically start the application's server and display the page in a window. How hard could it be? This lead me to create re/app.

