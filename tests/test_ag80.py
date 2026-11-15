"""AG80 (sweep 22 agentic wave): dashboard accessibility.

The tab bar is a real tablist, every select has a name, the capture input shows focus, detail
panes take focus and give it back on Escape, the command palette focuses at once, matches words in
any order and keeps Tab inside, the Tickets tab has the project picker its cap sentence asks for,
and the page has a main landmark. HTML-level tests run through the TestClient; the page's own
JavaScript functions run under node against small stubs when node is on PATH.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

KEY = "ag80-test-key-" + "0123456789abcdef"


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_WEB_API_KEY", KEY)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_BRAIN_URL", "http://127.0.0.1:9")
    from fastapi.testclient import TestClient
    from atlas_plus.tickets import store as tstore
    from atlas_plus.web import app as webapp
    root = tmp_path / "mem" / "tickets"
    monkeypatch.setattr(tstore, "TICKETS_ROOT", root)
    monkeypatch.setattr(webapp, "API_KEY", KEY, raising=False)
    for proj, pre in (("alpha", "ALP"), ("beta", "BET")):
        d = root / proj
        d.mkdir(parents=True)
        for i in (1, 2):
            (d / f"{pre}-00{i}.md").write_text(
                f"---\nid: {pre}-00{i}\nproject: {proj}\nstatus: todo\ntitle: {proj} t{i}\ntype: ticket\n---\nbody\n",
                encoding="utf-8")
    (root / "_archive" / "old").mkdir(parents=True)          # archived: not a project
    (root / "_archive" / "old" / "OLD-001.md").write_text("---\nid: OLD-001\n---\n", encoding="utf-8")
    (root / "empty").mkdir()                                  # no live ticket: not offered
    (root / "stray.md").write_text("not a directory", encoding="utf-8")
    c = TestClient(webapp.app, raise_server_exceptions=False)
    c.headers["Authorization"] = "Bearer " + KEY
    return c


class _Tree(HTMLParser):
    """Collects every start tag with its attributes and the stack of its ancestors' tags/ids."""

    VOID = {"input", "br", "img", "meta", "link", "hr", "path", "source", "col", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict]] = []
        self.elems: list[tuple[str, dict, list[tuple[str, dict]]]] = []
        self.in_script = False

    def handle_starttag(self, tag, attrs):
        if self.in_script:
            return
        a = {k: (v or "") for k, v in attrs}
        self.elems.append((tag, a, list(self.stack)))
        if tag == "script":
            self.in_script = True
        if tag not in self.VOID:
            self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        self.elems.append((tag, a, list(self.stack)))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        if self.in_script:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _page(web):
    r = web.get("/")
    assert r.status_code == 200
    t = _Tree()
    t.feed(r.text)
    return r.text, t


# ------------------------------------------------------------------ tablist
def test_tab_bar_is_a_tablist_with_one_selected_tab_and_linked_panels(web):
    _, t = _page(web)
    lists = [a for tag, a, _ in t.elems if a.get("role") == "tablist"]
    assert len(lists) == 1 and lists[0].get("id") == "tabbar" and lists[0].get("aria-label")
    tabs = [(a, anc) for tag, a, anc in t.elems if "tab" in a.get("class", "").split() and a.get("data-tab")]
    assert len(tabs) >= 10
    panels = {a["id"]: a for tag, a, _ in t.elems if "tab-content" in a.get("class", "").split()}
    selected = []
    for a, anc in tabs:
        assert any(x[1].get("id") == "tabbar" for x in anc), a  # inside the tablist
        assert a.get("role") == "tab"
        assert a.get("aria-selected") in ("true", "false")
        if a["aria-selected"] == "true":
            selected.append(a["data-tab"])
            assert a.get("tabindex") == "0"
        else:
            assert a.get("tabindex") == "-1"  # one tab stop for the whole bar
        panel = panels.get(a.get("aria-controls"))
        assert panel is not None, a
        assert panel.get("role") == "tabpanel" and panel.get("aria-labelledby") == a.get("id")
    assert selected == ["dash"]
    # every panel has its tab (an optional panel removed with its tab leaves no dangling link)
    assert len(panels) == len(tabs)


# ------------------------------------------------------------------ names
def test_every_select_has_an_accessible_name(web):
    _, t = _page(web)
    label_for = {a.get("for") for tag, a, _ in t.elems if tag == "label"}
    selects = {a.get("id"): a for tag, a, _ in t.elems if tag == "select"}
    for sid in ("task-agent", "tkt-filter", "inbox-filter", "task-filter", "time-filter", "tg-mode", "tkt-project"):
        assert sid in selects, sid
    unnamed = [sid for sid, a in selects.items()
               if not (a.get("aria-label", "").strip() or a.get("aria-labelledby") or sid in label_for)]
    assert unnamed == []


def test_capture_input_is_labelled_and_shows_a_focus_ring(web):
    html, t = _page(web)
    assert any(tag == "label" and a.get("for") == "capture-input" for tag, a, _ in t.elems)
    import re
    rules = re.findall(r"\.capture-bar input:focus(?:-visible)?\{([^}]*)\}", html)
    assert any(re.search(r"outline:\s*\d+px\s+solid", r) for r in rules), rules


def test_page_has_one_main_landmark_holding_every_panel(web):
    _, t = _page(web)
    mains = [a for tag, a, _ in t.elems if tag == "main" or a.get("role") == "main"]
    assert len(mains) == 1
    for tag, a, anc in t.elems:
        if "tab-content" in a.get("class", "").split():
            assert any(x[0] == "main" for x in anc), a.get("id")
        if a.get("role") == "tablist":
            assert any(x[0] in ("header", "nav") for x in anc)


def test_palette_is_a_modal_dialog_and_its_backdrop_is_not_a_button(web):
    _, t = _page(web)
    pal = next(a for tag, a, _ in t.elems if a.get("id") == "palette")
    # _kbdify gives every [onclick] without a tabindex a tab stop and role=button: the backdrop must opt out
    assert pal.get("tabindex") == "-1"
    box = next(a for tag, a, anc in t.elems if any(x[1].get("id") == "palette" for x in anc) and a.get("role") == "dialog")
    assert box.get("aria-modal") == "true" and box.get("aria-label")
    inp = next(a for tag, a, _ in t.elems if a.get("id") == "palette-input")
    assert inp.get("aria-label")


# ------------------------------------------------------------------ project picker
def test_ticket_projects_lists_the_board_filter_values(web):
    r = web.get("/api/tickets/projects")
    assert r.status_code == 200
    assert r.json() == ["alpha", "beta"]
    for name in r.json():
        board = web.get("/api/tickets/board", params={"project": name, "status_filter": "all"}).text
        assert board.count("showTicketDetail(") == 2
        other = "beta" if name == "alpha" else "alpha"
        assert f">{other}<" not in board


def test_ticket_projects_needs_auth(web):
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as webapp
    assert TestClient(webapp.app, raise_server_exceptions=False).get("/api/tickets/projects").status_code == 401


def test_tickets_tab_has_a_project_picker(web):
    _, t = _page(web)
    sel = [(a, anc) for tag, a, anc in t.elems if tag == "select" and a.get("id") == "tkt-project"]
    assert len(sel) == 1
    a, anc = sel[0]
    assert any(x[1].get("id") == "tab-tickets" for x in anc)
    assert a.get("onchange", "").startswith("reloadTickets")


# ------------------------------------------------------------------ the page's own JS, under node
def _js_function(html: str, name: str) -> str:
    i = html.find("function " + name + "(")
    assert i >= 0, name
    j = html.index("{", i)
    depth = 0
    for k in range(j, len(html)):
        ch = html[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[i:k + 1]
    raise AssertionError("unbalanced " + name)


def _node(script: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_palette_filter_matches_words_in_any_order_and_ignores_extra_spaces(web):
    html, _ = _page(web)
    fns = _js_function(html, "filterPalette")
    pal = html[html.index("const _PALETTE_CMDS="):html.index("let _palActive")]
    extra = _js_function(html, "_palMatch") if "function _palMatch(" in html else ""
    script = (
        "const switchTab=()=>{},toggleTicketForm=()=>{},closePalette=()=>{},loadMemoryIndex=()=>{},"
        "toggleConfig=()=>{},retryAllFailed=()=>{},stopServer=()=>{},htmx={};\n"
        "let _palActive=0;const _list={};\n"
        "const document={getElementById:()=>_list};\n"
        "function renderPalette(c){}\n" + pal + "\n" + extra + "\n" + fns + "\n"
        "const res={};for(const q of ['board tickets','tickets  board','ticket board','  memory   search ',"
        "'TICKETS','zzz nope','']){filterPalette(q);res[q]=_list._filtered.map(c=>c.label);}\n"
        "console.log(JSON.stringify(res));"
    )
    res = _node(script)
    for q in ("board tickets", "tickets  board", "ticket board"):
        assert "Tickets board" in res[q], (q, res[q])
    assert "Search memory" in res["  memory   search "]
    assert "Tickets board" in res["TICKETS"]
    assert res["zzz nope"] == []
    assert len(res[""]) > 20  # an empty query lists every command


def test_tab_keys_move_and_show_the_next_tab(web):
    html, _ = _page(web)
    fn = _js_function(html, "_tabKey")
    script = (
        "const shown=[];const focused=[];\n"
        "function mk(n){const t={dataset:{tab:n},focus(){focused.push(n);},closest(){return t;}};return t;}\n"
        "const tabs=['dash','map','tickets','sessions'].map(mk);\n"
        "const document={querySelectorAll:()=>tabs};\n"
        "function switchTab(n){shown.push(n);}\n" + fn + "\n"
        "const out=[];\n"
        "for(const [i,k] of [[0,'ArrowRight'],[3,'ArrowRight'],[0,'ArrowLeft'],[1,'End'],[2,'Home'],[2,'Enter'],[1,'x']]){\n"
        "  let pd=false;shown.length=0;focused.length=0;\n"
        "  _tabKey({key:k,target:tabs[i],preventDefault(){pd=true;}});\n"
        "  out.push([k,shown.slice(),focused.slice(),pd]);}\n"
        "console.log(JSON.stringify(out));"
    )
    out = _node(script)
    assert out[0] == ["ArrowRight", ["map"], ["map"], True]
    assert out[1] == ["ArrowRight", ["dash"], ["dash"], True]          # wraps
    assert out[2] == ["ArrowLeft", ["sessions"], ["sessions"], True]   # wraps backwards
    assert out[3] == ["End", ["sessions"], ["sessions"], True]
    assert out[4] == ["Home", ["dash"], ["dash"], True]
    assert out[5] == ["Enter", ["tickets"], [], True]
    assert out[6] == ["x", [], [], False]                              # other keys untouched


def test_detail_pane_takes_focus_and_escape_returns_it_to_the_opener(web):
    html, _ = _page(web)
    fns = "\n".join(_js_function(html, n) for n in ("_noteOpener", "_focusPane", "_returnFromPane"))
    script = (
        "const log=[];\n"
        "function el(name,parent){const e={name,parent,isConnected:true,offsetParent:{},attrs:{},"
        "getAttribute(k){return this.attrs[k]||null;},setAttribute(k,v){this.attrs[k]=v;},hasAttribute(k){return k in this.attrs;},"
        "focus(){document.activeElement=this;log.push(name);},contains(x){while(x){if(x===this)return true;x=x.parent;}return false;},"
        "closest(sel){let x=this;while(x){if(x.pane)return x;x=x.parent;}return null;}};return e;}\n"
        "const pane=el('ticket-detail');pane.pane=true;\n"
        "const btn=el('status-button',pane);\n"
        "const row=el('row');row.attrs.onclick=\"showTicketDetail('ALP-001')\";\n"
        "const row2=el('row-rerendered');row2.attrs.onclick=row.attrs.onclick;\n"
        "const window={innerWidth:1280};\n"
        "const document={body:{},activeElement:row,getElementById:id=>id==='ticket-detail'?pane:null,"
        "querySelectorAll:()=>[row2]};\n"
        "let _detailReturn=null;\n" + fns + "\n"
        "_noteOpener(['ticket-detail']);_focusPane('ticket-detail');\n"
        "const r1=[document.activeElement.name,pane.attrs.tabindex];\n"
        "document.activeElement=btn;_noteOpener(['ticket-detail']);\n"     # a re-render from inside keeps the opener
        "const back=_returnFromPane();const r2=[back,document.activeElement.name];\n"
        "document.activeElement=btn;row.isConnected=false;\n"               # the list re-rendered under the pane
        "const r3=[_returnFromPane(),document.activeElement.name];\n"
        "document.activeElement=row2;const r4=_returnFromPane();\n"         # focus outside a pane: Escape does nothing
        "console.log(JSON.stringify([r1,r2,r3,r4]));"
    )
    r1, r2, r3, r4 = _node(script)
    assert r1 == ["ticket-detail", "-1"]
    assert r2 == [True, "row"]
    assert r3 == [True, "row-rerendered"]
    assert r4 is False


def test_every_detail_opener_moves_focus_into_its_pane(web):
    """Runs showTicketDetail / showTaskDetail / showInboxDetail with a stub fetch: the row that
    opened the pane is remembered and the pane holds focus once its HTML arrives."""
    html, _ = _page(web)
    nl = chr(10)
    helpers = nl.join(_js_function(html, n) for n in ("_noteOpener", "_focusPane"))
    for fn, pane in (("showTicketDetail", "ticket-detail"), ("showTaskDetail", "task-detail"),
                     ("showInboxDetail", "inbox-detail")):
        script = nl.join([
            "function el(name){return {name,isConnected:true,offsetParent:{},attrs:{},innerHTML:'',"
            "getAttribute(k){return this.attrs[k]||null;},setAttribute(k,v){this.attrs[k]=v;},"
            "hasAttribute(k){return k in this.attrs;},focus(){document.activeElement=this;},"
            "contains(x){return x===this;},scrollIntoView(){}};}",
            "const panes={};for(const n of ['ticket-detail','task-detail','dash-detail','inbox-detail'])panes[n]=el(n);",
            "panes['dash-detail'].offsetParent=null;",
            "const row=el('row');row.attrs.onclick='x';",
            "const window={innerWidth:1280};",
            "const document={body:{},activeElement:row,getElementById:id=>panes[id]||null};",
            "function showToast(){}",
            "function fetch(){return Promise.resolve({ok:true,status:200,text:()=>Promise.resolve('<b>detail</b>'),"
            "json:()=>Promise.resolve({})});}",
            "let _detailReturn=null;",
            helpers,
            _js_function(html, fn),
            fn + "('ALP-001');",
            "setTimeout(()=>console.log(JSON.stringify([document.activeElement.name,"
            "_detailReturn&&_detailReturn.el.name,panes['" + pane + "'].innerHTML])),50);",
        ])
        assert _node(script) == [pane, "row", "<b>detail</b>"], fn


def test_dispatch_modal_is_a_dialog_focused_at_once_and_gives_focus_back(web):
    """The palette's sibling dialog: role=dialog, focus lands in the instruction box
    synchronously (no timer), and closing returns focus to the button that opened it."""
    html, t = _page(web)
    box = next(a for tag, a, anc in t.elems if a.get("role") == "dialog"
               and any(x[1].get("id") == "dispatch-modal" for x in anc))
    assert box.get("aria-modal") == "true" and box.get("aria-labelledby") == "dm-title"
    nl = chr(10)
    script = nl.join([
        "function el(name){return {name,isConnected:true,style:{},textContent:'',className:'',value:'x',"
        "focus(){document.activeElement=this;}};}",
        "const els={};for(const n of ['dispatch-modal','dm-agent','dm-inst','dm-result'])els[n]=el(n);",
        "const opener=el('agent-button');",
        "const document={body:{},activeElement:opener,getElementById:id=>els[id]};",
        "let _dmAgent='';let _dmReturn=null;",
        _js_function(html, "quickDispatch"),
        _js_function(html, "closeDispatchModal"),
        "quickDispatch('coder');",
        "const a=[document.activeElement.name,els['dispatch-modal'].style.display];",
        "closeDispatchModal();",
        "console.log(JSON.stringify([a,[document.activeElement.name,els['dispatch-modal'].style.display]]));",
    ])
    opened, closed = _node(script)
    assert opened == ["dm-inst", "flex"]
    assert closed == ["agent-button", "none"]
