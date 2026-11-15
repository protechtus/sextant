"""MN76 (sweep 22 manual wave, the dashboard in a real browser, keyboard only).

Found with Playwright Chromium against a running dashboard:
- Ctrl+K while the dispatch dialog was open: the two focus traps chased each other ("Maximum call
  stack size exceeded" twice), the palette was drawn on top and every key went into the hidden
  instruction box;
- a polled list (tasks 8 s, tickets and inbox 30 s) re-rendered under the focused row and dropped
  focus on the page body;
- the capture-scope select had no focus indicator at all (outline none, border none);
- Enter in the palette with no match, or a command that only switches tab, left focus on the body;
  the palette offered Watchtower when no Watchtower tab is installed;
- with a project picked the board's cap sentence still said "pick a project".

The browser tests run the real page on a real server and skip when Playwright or its Chromium is
missing, or when the page could not load htmx from its CDN (documented: the dashboard needs it).
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

KEY = "mn76-test-key-" + "0123456789abcdef"


def _seed(root, per_project):
    for proj, pre, n in per_project:
        d = root / proj
        d.mkdir(parents=True, exist_ok=True)
        for i in range(1, n + 1):
            (d / f"{pre}-{i:03d}.md").write_text(
                f"---\nid: {pre}-{i:03d}\nproject: {proj}\nstatus: todo\npriority: normal\ntitle: {proj} t{i}\n"
                f"type: ticket\nupdated: '2026-11-15'\n---\nbody\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_WEB_API_KEY", KEY)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_BRAIN_URL", "http://127.0.0.1:9")
    from atlas_plus.tickets import store as tstore
    from atlas_plus.web import app as webapp
    root = tmp_path / "mem" / "tickets"
    monkeypatch.setattr(tstore, "TICKETS_ROOT", root)
    monkeypatch.setattr(webapp, "API_KEY", KEY, raising=False)
    return webapp, root


def test_cap_sentence_does_not_ask_for_a_project_when_one_is_picked(env):
    webapp, root = env
    _seed(root, [("alpha", "ALP", 503), ("beta", "BET", 2)])
    from fastapi.testclient import TestClient
    c = TestClient(webapp.app, raise_server_exceptions=False)
    c.headers["Authorization"] = "Bearer " + KEY
    picked = c.get("/api/tickets/board?project=alpha&status_filter=all").text
    assert "3 more ticket(s) not shown" in picked
    assert "pick a project" not in picked and "pick a status filter" in picked
    whole = c.get("/api/tickets/board?status_filter=all").text
    assert "5 more ticket(s) not shown: pick a project or a status filter" in whole



def test_board_cap_keeps_the_newest_ids_of_one_day(env):
    """MN76 F8: 520 tickets all updated on one date; the 500-card board must keep the newest (ALP-520), not cut them."""
    webapp, root = env
    _seed(root, [("alpha", "ALP", 520)])
    from fastapi.testclient import TestClient
    c = TestClient(webapp.app, raise_server_exceptions=False)
    c.headers["Authorization"] = "Bearer " + KEY
    html = c.get("/api/tickets/board?project=alpha&status_filter=all").text
    assert "20 more ticket(s) not shown" in html
    assert "ALP-520" in html and "ALP-501" in html
    assert "ALP-020" not in html and "ALP-021" in html

@pytest.fixture
def browser_page(env):
    sync_api = pytest.importorskip("playwright.sync_api")
    import uvicorn
    webapp, root = env
    _seed(root, [("alpha", "ALP", 6)])
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(webapp.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True); th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch()
    except Exception as exc:  # no browser downloaded
        pw.stop(); server.should_exit = True
        pytest.skip(f"Chromium for Playwright is not installed: {exc}")
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"http://127.0.0.1:{port}/?key={KEY}")
    pg.wait_for_timeout(1500)
    if not pg.evaluate("typeof htmx !== 'undefined'"):
        b.close(); pw.stop(); server.should_exit = True
        pytest.skip("the page could not load htmx from its CDN (offline)")
    yield pg, errors
    b.close(); pw.stop(); server.should_exit = True; th.join(5)


def test_ctrl_k_inside_the_dispatch_dialog_does_not_fight_its_focus_trap(browser_page):
    pg, errors = browser_page
    pg.focus("#theme-btn")
    pg.evaluate("quickDispatch('fast')")
    assert pg.evaluate("document.activeElement.id") == "dm-inst"
    pg.keyboard.press("Control+k"); pg.wait_for_timeout(200)
    pg.keyboard.type("tickets")
    assert not [e for e in errors if "call stack" in e], errors
    assert pg.eval_on_selector("#palette", "e=>e.style.display") == "none"
    assert pg.input_value("#dm-inst") == "tickets"   # the dialog the person sees is the one that types
    pg.keyboard.press("Escape"); pg.wait_for_timeout(100)
    assert pg.evaluate("document.activeElement.id") == "theme-btn"


def test_palette_enter_without_a_match_gives_focus_back_and_hides_missing_tabs(browser_page):
    pg, errors = browser_page
    pg.focus("#theme-btn")
    pg.keyboard.press("Control+k"); pg.keyboard.type("watchtower"); pg.wait_for_timeout(100)
    assert pg.eval_on_selector_all("#palette-list .palette-item", "e=>e.length") == 0
    pg.keyboard.press("Enter"); pg.wait_for_timeout(100)
    assert pg.eval_on_selector("#palette", "e=>e.style.display") == "none"
    assert pg.evaluate("document.activeElement.id") == "theme-btn"
    pg.keyboard.press("Control+k"); pg.keyboard.type("tickets board"); pg.keyboard.press("Enter"); pg.wait_for_timeout(600)
    assert pg.evaluate("document.activeElement.id") == "tabbtn-tickets"


def test_capture_scope_shows_focus_when_tabbed_to(browser_page):
    pg, errors = browser_page
    pg.focus("#capture-input"); pg.keyboard.press("Tab")
    assert pg.evaluate("document.activeElement.id") == "capture-scope"
    style = pg.evaluate("(()=>{const c=getComputedStyle(document.activeElement);return [c.outlineStyle,c.outlineWidth]})()")
    assert style[0] != "none" and float(style[1].rstrip("px")) >= 1, style


def test_focused_ticket_card_survives_the_board_poll(browser_page):
    pg, errors = browser_page
    pg.click("#tabbtn-tickets"); pg.wait_for_timeout(1500)
    pg.focus("#tabbtn-tickets")
    for _ in range(10):
        pg.keyboard.press("Tab")
        if pg.evaluate("document.activeElement.getAttribute('role')==='button'"):
            break
    pg.keyboard.press("Tab")
    before = pg.evaluate("document.activeElement.getAttribute('onclick')")
    assert before and "showTicketDetail" in before
    pg.evaluate("htmx.trigger(document.getElementById('tboard'),'tab-show')")   # what the 30 s poll fires
    pg.wait_for_timeout(1500)
    assert pg.evaluate("document.activeElement.getAttribute('onclick')") == before
