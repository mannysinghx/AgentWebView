"""Browser host process: Playwright + Chromium, separate from the AgentWebView process.

The host is started with ``multiprocessing`` (spawn) and immediately calls ``os.setsid()``, so it and every
Chromium process it launches share one new process group. The supervisor can therefore terminate the whole
tree at the hard deadline, whatever state Playwright or the page is in. The host also exits on its own if it
is orphaned or if its own hard deadline passes.

Chromium runs headless with all traffic forced through the policy proxy (``--proxy-bypass-list=<-loopback>``
removes the implicit localhost bypass), and with UDP side channels (QUIC, non-proxied WebRTC) and background
services disabled.
"""

from __future__ import annotations

import base64
import contextlib
import os
import signal
import threading
import time
from typing import Any

CHROMIUM_ARGS = [
    "--proxy-bypass-list=<-loopback>",
    "--disable-quic",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--dns-prefetch-disable",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-client-side-phishing-detection",
    "--safebrowsing-disable-auto-update",
    "--no-pings",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--use-mock-keychain",
    "--mute-audio",
    "--disable-features=OptimizationHints,MediaRouter,DialMediaRouteProvider,Translate,AutofillServerCommunication,"
    "CertificateTransparencyComponentUpdater,InterestFeedContentSuggestions,PrivacySandboxSettings4",
]
VIEWPORT = {"width": 1280, "height": 800}

INSPECT_JS = """e => ({tag: e.tagName, type: e.getAttribute('type') || e.type || '', form: !!e.form,
  method: e.form ? (e.form.getAttribute('method') || 'get') : null, text: (e.innerText || '').slice(0, 300),
  aria: e.getAttribute('aria-label'), value: (e.tagName === 'INPUT' ? e.value : '') || '',
  title: e.getAttribute('title'), name: e.getAttribute('name'), href: e.getAttribute('href'),
  role: e.getAttribute('role')})"""


def _watchdog(parent_pid: int, hard_deadline: float) -> None:
    while True:
        time.sleep(0.25)
        if os.getppid() != parent_pid or time.time() > hard_deadline:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(0), signal.SIGKILL)
            os._exit(3)


def _locator(page: Any, sel: dict[str, Any]) -> Any:
    if sel.get("css"):
        loc = page.locator(f"css={sel['css']}")
    elif sel.get("role"):
        loc = page.get_by_role(sel["role"], name=sel.get("name")) if sel.get("name") else page.get_by_role(sel["role"])
    elif sel.get("text"):
        loc = page.get_by_text(sel["text"])
    elif sel.get("label"):
        loc = page.get_by_label(sel["label"])
    elif sel.get("placeholder"):
        loc = page.get_by_placeholder(sel["placeholder"])
    else:
        loc = page.get_by_test_id(sel["test_id"])
    return loc.first


def host_main(
    conn: Any, proxy_port: int, parent_pid: int, hard_deadline: float, sandbox: bool, executable: str | None
) -> None:
    if hasattr(os, "setsid"):
        os.setsid()  # new process group: the supervisor kills the whole tree with killpg
    threading.Thread(target=_watchdog, args=(parent_pid, hard_deadline), daemon=True).start()
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    args = [f"--proxy-server=http://127.0.0.1:{proxy_port}", *CHROMIUM_ARGS]
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=True, args=args, chromium_sandbox=sandbox, executable_path=executable or None
            )
            context = browser.new_context(accept_downloads=False, viewport=VIEWPORT, service_workers="block")  # type: ignore[arg-type]
            page = context.new_page()
            conn.send({"ok": True, "op": "ready", "browser_version": browser.version})
        except Exception as exc:  # report launch failure to the supervisor and exit
            conn.send({"ok": False, "op": "ready", "error": f"{type(exc).__name__}: {str(exc)[:400]}"})
            return
        while True:
            if not conn.poll(0.5):
                continue
            cmd = conn.recv()
            op = cmd.get("op")
            timeout = int(cmd.get("timeout_ms", 5_000))
            try:
                if op == "close":
                    with contextlib.suppress(Exception):
                        browser.close()
                    conn.send({"ok": True})
                    break
                conn.send(_dispatch(page, op, cmd, timeout))
            except PlaywrightTimeout as exc:
                conn.send({"ok": False, "timeout": True, "error": str(exc).splitlines()[0][:300]})
            except PlaywrightError as exc:
                conn.send({"ok": False, "error": str(exc).splitlines()[0][:300]})
            except Exception as exc:
                conn.send({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})


def _state(page: Any) -> dict[str, Any]:
    try:
        title = page.title()
    except Exception:
        title = None
    return {"url": page.url, "title": title}


def _dispatch(page: Any, op: str, cmd: dict[str, Any], timeout: int) -> dict[str, Any]:
    if op == "navigate":
        resp = page.goto(cmd["url"], timeout=timeout, wait_until="domcontentloaded")
        return {"ok": True, "status": resp.status if resp else None, **_state(page)}
    if op == "inspect":
        loc = _locator(page, cmd["selector"])
        loc.wait_for(state="attached", timeout=timeout)
        return {"ok": True, "element": loc.evaluate(INSPECT_JS)}
    if op == "click":
        loc = _locator(page, cmd["selector"])
        loc.wait_for(state="visible", timeout=timeout)  # failure here means nothing was clicked
        before = page.url
        try:
            loc.click(timeout=timeout)
            with contextlib.suppress(Exception):
                page.wait_for_load_state("domcontentloaded", timeout=min(timeout, 3_000))
        except Exception as exc:
            # The click may have been dispatched: outcome unknown.
            return {"ok": False, "uncertain": True, "error": str(exc).splitlines()[0][:300], **_state(page)}
        return {"ok": True, "navigated": page.url != before, **_state(page)}
    if op == "fill":
        _locator(page, cmd["selector"]).fill(cmd["value"], timeout=timeout)
        return {"ok": True, **_state(page)}
    if op == "select":
        chosen = _locator(page, cmd["selector"]).select_option(cmd["values"], timeout=timeout)
        return {"ok": True, "selected": chosen, **_state(page)}
    if op == "scroll":
        page.mouse.wheel(0, cmd["pixels"] if cmd.get("direction", "down") == "down" else -cmd["pixels"])
        return {"ok": True, **_state(page)}
    if op == "extract":
        max_chars = int(cmd.get("max_chars", 20_000))
        if cmd.get("mode") == "links":
            root = _locator(page, cmd["selector"]) if cmd.get("selector") else page.locator("body")
            links = root.locator("a[href]").evaluate_all(
                "els => els.slice(0, 500).map(a => ({text: (a.innerText || '').slice(0, 200), href: a.href}))"
            )
            return {"ok": True, "links": links, **_state(page)}
        if cmd.get("mode") == "text" or cmd.get("selector"):
            root = _locator(page, cmd["selector"]) if cmd.get("selector") else page.locator("body")
            text = root.inner_text(timeout=timeout)
            return {"ok": True, "text": text[:max_chars], "truncated": len(text) > max_chars, **_state(page)}
        html = page.content()
        return {"ok": True, "html": html[: max_chars * 20], **_state(page)}
    if op == "screenshot":
        png = page.screenshot(type="png", full_page=bool(cmd.get("full_page")), timeout=timeout)
        return {"ok": True, "png_b64": base64.b64encode(png).decode(), **_state(page)}
    if op == "state":
        return {"ok": True, **_state(page)}
    return {"ok": False, "error": f"unknown operation {op!r}"}
