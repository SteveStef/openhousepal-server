"""Blog editor test 11/14: an agent's own edits must look the same published as they do in the editor.

Pressing Enter in the middle of a paragraph made the browser wrap the new line in a <div>; the published
article's text cleaner dropped the <div> with nothing in its place, so "...a median of $418" and
"A new line..." were published as "$418A new line" - glued, no break and no space. Text pasted from Word,
Google Docs or a web page (<p>...</p><p>...</p>) was glued the same way.

- lib/studio/sanitizeHtml.ts: a block boundary is a line break (fixes articles already saved that way).
- the editor: Enter splits the block into two paragraphs, Shift+Enter is a line break, paste is plain text
  with each pasted paragraph becoming its own paragraph block.
The lib test bundles the real frontend module with esbuild and runs it in Node (skipped if either is missing)."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
PAGE = FRONTEND / "app" / "studio" / "blogs" / "[id]" / "page.tsx"


EDITOR = FRONTEND / "components" / "studio" / "blogs" / "editor"


def _node(script: str):
    """Bundles a TypeScript snippet against the real frontend modules and returns the JSON it prints."""
    esbuild = FRONTEND / "node_modules" / ".bin" / ("esbuild.cmd" if shutil.which("cmd") else "esbuild")
    if not esbuild.exists() or not shutil.which("node"):
        pytest.skip("esbuild/node not available")
    with tempfile.TemporaryDirectory() as tmp:
        entry, out = Path(tmp) / "entry.ts", Path(tmp) / "out.js"
        entry.write_text(script, encoding="utf-8")
        subprocess.run([str(esbuild), str(entry), "--bundle", "--platform=node", "--format=cjs",
                        f"--tsconfig={FRONTEND / 'tsconfig.json'}", "--log-level=error", f"--outfile={out}"],
                       cwd=FRONTEND, check=True, capture_output=True, timeout=120)
        return json.loads(subprocess.run(["node", str(out)], check=True, capture_output=True, encoding="utf-8", timeout=60).stdout)


def _clean(samples: list[str]) -> list[str]:
    return _node("import { sanitizeInlineHtml } from '@/lib/studio/sanitizeHtml'\n"
                 f"const samples: string[] = {json.dumps(samples)}\n"
                 "console.log(JSON.stringify(samples.map(sanitizeInlineHtml)))\n")


def test_a_line_added_with_enter_is_published_as_a_line_not_glued():
    got = _clean([
        "sold for a median of $418<div>A new line added mid-paragraph. ,000 more text.</div>",          # Enter mid-paragraph
        "first<div>second</div><div>third</div>",                                                         # two Enters
        "<p style=\"font-family:Comic Sans MS;color:red;font-size:22px\">Pasted one.</p><p>Pasted two.</p>",   # a Word / web paste
        "first<div><br></div><div>after a blank line</div>",                                              # an empty line between
        "kept <b>bold</b> and a <a href=\"https://www.phila.gov/x\">link</a><div>next</div>",
    ])
    assert got[0] == "sold for a median of $418<br>A new line added mid-paragraph. ,000 more text."
    assert got[1] == "first<br>second<br>third"
    assert got[2] == "Pasted one.<br>Pasted two."                              # foreign fonts and colours never publish
    assert got[3] == "first<br><br>after a blank line"
    assert got[4] == 'kept <b>bold</b> and a <a href="https://www.phila.gov/x" target="_blank" rel="noreferrer">link</a><br>next'


def test_ordinary_text_is_untouched_and_nothing_unsafe_gets_through():
    got = _clean([
        "<strong>At a glance</strong><br>• One.<br>• Two.",          # the AI writer's own short list
        "plain text with 2 < 3 and a &amp; sign",
        "typed then Enter at the end<br><br>",                        # the editor's own trailing placeholder
        "<div onclick=\"x()\">a</div><script>alert(1)</script><img src=x onerror=y>b",
        "",
    ])
    assert got[0] == "<strong>At a glance</strong><br>• One.<br>• Two."
    assert got[1] == "plain text with 2 &lt; 3 and a &amp; sign"
    assert got[2] == "typed then Enter at the end"
    assert got[3] == "a<br>b" and "script" not in got[3] and "onerror" not in got[3]
    assert got[4] == ""


def test_the_editor_never_leaves_enter_or_paste_to_the_browser():
    """11/15: the article is edited on the published-looking page itself (components/studio/blogs/editor). One Enter
    handler and one paste handler on the article's root cover every text spot - paragraphs, headings, quotes, boxes,
    list points, captions - so no spot can fall back to the browser's own Enter (<div>) or its styled paste."""
    src = (EDITOR / "BlogCanvasEditor.tsx").read_text(encoding="utf-8")
    keys = src.split("const onKeyDown = (e: React.KeyboardEvent) => {")[1].split("const onPaste = (e: React.ClipboardEvent) => {")[0]
    enter = keys.split("if (e.key === 'Enter') {")[1].split("if (e.key === 'Backspace'")[0]
    # Enter is stopped first (only a half-typed word from a phone keyboard / IME is let finish), then routed
    assert enter.lstrip().startswith("if (e.nativeEvent.isComposing) return") and "e.preventDefault()" in enter[:120]
    # a list point makes the next point, a paragraph or heading splits in two, a box keeps the line inside or is left
    assert "enterInList(" in enter and "splitRich(" in enter and "exitBox(" in enter and "insertLineBreak" in enter
    paste = src.split("const onPaste = (e: React.ClipboardEvent) => {")[1].split("// --- picking sections")[0]
    assert -1 < paste.index("e.preventDefault()") < paste.index("getData('text/plain')")
    assert "pasteParagraphs(" in paste and "pasteItems(" in paste
    assert "text/html" not in src                                        # a copied page's own markup is never read
    # both are wired on the article's root, which every typing spot sits inside
    assert "onKeyDown: on('onKeyDown'), onPaste: on('onPaste')" in src
    # the title and subtitle sit outside the article body and do the same themselves
    assert src.count("onPaste={e => { e.preventDefault(); document.execCommand('insertText'") == 2
    assert src.count("if (e.key === 'Enter') { e.preventDefault(); e.currentTarget.blur()") == 2
    # nothing else makes article text editable: the published-page component only takes the editor's hooks
    body = (FRONTEND / "components" / "studio" / "blogs" / "BlogArticleBody.tsx").read_text(encoding="utf-8")
    assert "contentEditable" not in body and "contentEditable: true" in src
    page = PAGE.read_text(encoding="utf-8")
    assert "<BlogCanvasEditor" in page and "suppressContentEditableWarning" not in page and "function RichTextBlock(" not in page


def test_the_editing_rules_keep_the_articles_own_shapes():
    """What Enter, Backspace and paste turn into (components/studio/blogs/editor/blockOps.ts) - run for real in Node."""
    got = _node("""
import * as ops from '@/components/studio/blogs/editor/blockOps'
const P = (id: string, html: string): any => ({ id, type: 'paragraph', data: { html } })
const H = (id: string, html: string): any => ({ id, type: 'heading', data: { html } })
const L = (id: string, items: string[]): any => ({ id, type: 'bulleted_list', data: { items } })
const shape = (bs: any[]) => bs.map(b => [b.type, b.data.items || b.data.html || ''])
const out: any = {}
let e = ops.splitRich([P('a', 'one two')], 'a', 'one', 'two')
out.split = shape(e.blocks); out.splitFocus = e.focus
out.headingEnter = shape(ops.splitRich([H('h', 'Title')], 'h', 'Title', '').blocks)
e = ops.enterInList([L('l', ['one two', 'three'])], 'l', 0, 'one', 'two')
out.listEnter = shape(e.blocks); out.listEnterFocus = e.focus
out.listExit = shape(ops.enterInList([L('l', ['a', '', 'c'])], 'l', 1, '', '').blocks)
e = ops.backspaceInList([L('l', ['ab', 'cd'])], 'l', 1)
out.listJoin = shape(e.blocks); out.listJoinFocus = e.focus
out.listFirst = shape(ops.backspaceInList([L('l', ['ab', 'cd'])], 'l', 0).blocks)
e = ops.backspaceRich([P('a', 'one'), P('b', '<b>two</b>')], 'b')
out.merge = shape(e.blocks); out.mergeFocus = e.focus
out.pasted = ops.pastedParagraphs('First.\\n\\nSecond line.\\n- a\\n- b')
out.paste = shape(ops.pasteParagraphs([P('a', 'x')], 'a', 'x First.', ['Second.'], 'tail').blocks)
out.typed = ops.applyHostValue([P('a', '')], 'a', 'html', 'one<div>two</div><script>x()</script>')[0].data.html
out.toList = shape(ops.convertBlock([P('a', '• one<br>• two')], 'a', 'bulleted_list').blocks)
const doc: any[] = [P('i', 'intro'), H('h1', 'A'), P('p1', 'a'), H('h2', 'B'), P('p2', 'b'),
  { id: 's', type: 'sources', data: {} }, { id: 'c', type: 'cta', data: {} }, { id: 'ap', type: 'agent_profile', data: {} }]
const ids = (bs: any[]) => bs.map(b => b.id).join(' ')
out.ending = ops.tailStart(doc)
out.sectionDown = ids(ops.stepUnit(doc, 'h1', 1))
out.lastSectionStays = ops.stepUnit(doc, 'h2', 1) === doc && ops.stepUnit(doc, 'h1', -1) === doc
out.dropAtEnd = ids(ops.insertAt(doc, doc.length, [P('n', 'new')]))
out.copySection = ops.duplicateUnit(doc, 'h1')!.blocks.map(b => b.type).join(' ')
out.noSecondClosing = ops.duplicateUnit(doc, 'c')
console.log(JSON.stringify(out))
""")
    # Enter: a paragraph splits into two paragraphs; a heading is followed by a paragraph, never a second heading
    assert got["split"] == [["paragraph", "one"], ["paragraph", "two"]] and got["splitFocus"]["at"] == "start"
    assert got["headingEnter"] == [["heading", "Title"], ["paragraph", ""]]
    # Enter in a list adds a point; Enter on an empty point leaves the list (and keeps the points after it a list)
    assert got["listEnter"] == [["bulleted_list", ["one", "two", "three"]]] and got["listEnterFocus"] == {"host": "l:i1", "at": "start"}
    assert got["listExit"] == [["bulleted_list", ["a"]], ["paragraph", ""], ["bulleted_list", ["c"]]]
    # Backspace at the start joins the point above; the first point becomes a paragraph above the list
    assert got["listJoin"] == [["bulleted_list", ["abcd"]]] and got["listJoinFocus"] == {"host": "l:i0", "at": 2}
    assert got["listFirst"] == [["paragraph", "ab"], ["bulleted_list", ["cd"]]]
    assert got["merge"] == [["paragraph", "one<b>two</b>"]] and got["mergeFocus"] == {"host": "a:html", "at": 3}
    # paste: one paragraph per pasted paragraph, a run of bullet lines kept together; never the browser's own markup
    assert got["pasted"] == ["First.", "Second line.", "• a<br>• b"]
    assert got["paste"] == [["paragraph", "x First."], ["paragraph", "Second. tail"]]
    assert got["typed"] == "one<br>two"
    assert got["toList"] == [["bulleted_list", ["one", "two"]]]
    # a heading moves with its section; the ending (sources, closing button, contact card) belongs to no section
    assert got["ending"] == 5
    assert got["sectionDown"] == "i h2 p2 h1 p1 s c ap" and got["lastSectionStays"] is True
    assert got["dropAtEnd"] == "i h1 p1 h2 p2 s n c ap"           # nothing lands below the closing button
    assert got["copySection"] == "paragraph heading paragraph heading paragraph heading paragraph sources cta agent_profile"
    assert got["noSecondClosing"] is None
