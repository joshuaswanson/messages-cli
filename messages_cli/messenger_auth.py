"""Messenger authentication: extract cookies via a browser login window.

Uses pywebview with WebKit on macOS, then reads cookies from the WebKit
cookie store (including httpOnly cookies like `xs`).
"""

import json
import sys
import threading
import time
from pathlib import Path

COOKIES_PATH = Path.home() / ".config/messages-cli/messenger_cookies.json"
REQUIRED_COOKIES = {"c_user", "xs", "datr", "sb"}


def _get_webkit_cookies(window) -> dict[str, str]:
    """Extract cookies from the WebKit cookie store via pyobjc."""
    try:
        from WebKit import WKWebsiteDataStore
        import objc

        cookies = {}
        done = threading.Event()

        data_store = WKWebsiteDataStore.defaultDataStore()
        cookie_store = data_store.httpCookieStore()

        def callback(all_cookies):
            for cookie in all_cookies:
                name = str(cookie.name())
                value = str(cookie.value())
                domain = str(cookie.domain())
                if "messenger.com" in domain or "facebook.com" in domain:
                    cookies[name] = value
            done.set()

        cookie_store.getAllCookies_(callback)
        done.wait(timeout=5)
        return cookies
    except Exception as e:
        print(f"Warning: Could not read WebKit cookies: {e}", file=sys.stderr)
        return {}


def run_auth() -> None:
    """Open a browser window for Messenger login and extract cookies."""
    try:
        import webview
    except ImportError:
        print(
            "pywebview is required for Messenger auth.\n"
            "Install it with: uv add pywebview",
            file=sys.stderr,
        )
        sys.exit(1)

    cookies_found = {}
    auth_done = False

    def poll_cookies(window):
        nonlocal cookies_found, auth_done
        while not auth_done:
            time.sleep(2)
            try:
                cookies = _get_webkit_cookies(window)
                cookies_found.update(cookies)
                if REQUIRED_COOKIES.issubset(cookies_found.keys()):
                    auth_done = True
                    _save_cookies(cookies_found)
                    window.destroy()
                    return
            except Exception:
                pass

    print("Opening Messenger login window...")
    print("Log in with your Facebook account. The window will close automatically.")

    window = webview.create_window(
        "Messenger Login",
        "https://www.messenger.com/login",
        width=500,
        height=700,
    )

    def on_shown():
        t = threading.Thread(target=poll_cookies, args=(window,), daemon=True)
        t.start()

    window.events.shown += on_shown
    webview.start()

    if REQUIRED_COOKIES.issubset(cookies_found.keys()):
        print("Authenticated successfully!")
        print(f"Cookies saved to {COOKIES_PATH}")
    else:
        print("Authentication cancelled or failed.", file=sys.stderr)
        sys.exit(1)


def _save_cookies(cookies: dict) -> None:
    """Save the required cookies to the config file."""
    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    to_save = {k: cookies[k] for k in REQUIRED_COOKIES if k in cookies}
    with open(COOKIES_PATH, "w") as f:
        json.dump(to_save, f, indent=2)
    COOKIES_PATH.chmod(0o600)
