"""11/16 - the blog editor's AI buttons, after the owner pressed on them. His words, in order:

  "Draft with AI is there after making the entire article with AI? That's dumb." ... "Maybe we shouldn't have a draft
   with AI button, cause if someone wanted to do that I would've just made it with AI and used the AI flow."
  "Refine is 5 cents - isn't that a few lines of text? That seems extremely high."
  "It just makes sure its grammar is correct, kind of like if you go into a model and say fix this sentence ...
   Definitely don't be adding sections with these buttons ... it shows a before and after and you click which one you
   want, just like ChatGPT does and Apple Intelligence does."
  "It should show usage: how much it will use and how much it did use."

So: no Draft with AI in the editor. Refine is ONE button per section that fixes the spelling, grammar and wording of
the agent's own words - never the AI writer's paragraphs - on the small model, and shows Yours beside the New version.
Code checks every reply: a number, a link or a name that changed, a line added or lost, or a passage that grew or shrank
a lot is thrown away.

Measured the same day: the old Refine (a whole section rewritten on Sonnet 5) $0.0249 a press; the writing fixer on
Haiku 5.5 $0.0003 for six passages.

No AI and no database here: the service's own functions, a stand-in for the model, and the website's files."""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.ai import ai_blog_writer_service as w

FRONT = Path(__file__).resolve().parents[2] / "frontend"
ED = FRONT / "components" / "studio" / "blogs" / "editor"
OPS = (ED / "blockOps.ts").read_text(encoding="utf-8")
EDITOR = (ED / "BlogCanvasEditor.tsx").read_text(encoding="utf-8")
COMPARE = (ED / "RefineCompare.tsx").read_text(encoding="utf-8")
PAGE = (FRONT / "app" / "studio" / "blogs" / "[id]" / "page.tsx").read_text(encoding="utf-8")
COVER = (FRONT / "components" / "studio" / "blogs" / "AiCoverMaker.tsx").read_text(encoding="utf-8")
SHARE = (FRONT / "lib" / "studio" / "aiShare.ts").read_text(encoding="utf-8")
TOPICS = (FRONT / "lib" / "studio" / "blogTopics.ts").read_text(encoding="utf-8")
GUIDE = (Path(__file__).resolve().parents[1] / "app" / "services" / "ai" / "helper_guide.md").read_text(encoding="utf-8")
USER = SimpleNamespace(id="u1", plan_tier="IDX", first_name="Cora", last_name="Maddox")
ROUGH = "buyers in media notice the little things first so fix them. dont spend alot, the painted one went for $412,500 which was 4% over asking"
FIXED = "Buyers in Media notice the little things first, so fix them. Don't spend a lot. The painted one went for $412,500, which was 4% over asking."


def _stand_in(monkeypatch, reply):
    seen = {}

    async def call(db, user, **kw):
        seen.update(kw)
        return reply, {"model": kw.get("model"), "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.0003, "stop_reason": "tool_use"}

    async def cfg(db):
        return {"blog_refine_max_output_tokens": 2500, "blog_writer_model": "claude-sonnet-5"}

    async def hub(db, uid):
        return {}
    monkeypatch.setattr(w, "_metered_call", call)
    monkeypatch.setattr(w.ai_config_service, "get_config", cfg)
    monkeypatch.setattr(w, "_hub_config", hub)
    monkeypatch.setattr(w, "_agent_profile", lambda user, config: {"name": "Cora Maddox", "brokerage": "Zq Test Realty", "market": "Media"})
    return seen


def _polish(monkeypatch, blocks, passages):
    seen = _stand_in(monkeypatch, {"passages": passages})
    return asyncio.run(w.polish_blocks(None, USER, title="Getting a home ready", blocks=blocks)), seen


# ------------------------------------------------------------------ the model and the price
def test_refine_runs_on_the_small_model_not_the_article_writers(monkeypatch):
    assert w.REFINE_MODEL == "claude-haiku-5-5"
    assert w.refine_model({}) == "claude-haiku-5-5" and w.refine_model({"blog_refine_model": "claude-sonnet-5-5"}) == "claude-sonnet-5-5"
    assert w.REFINE_TYPICAL_USD <= 0.005                     # what the editor quotes before a press
    got, seen = _polish(monkeypatch, [{"id": "p1", "type": "paragraph", "data": {"html": ROUGH}}], [{"id": "p1", "text": FIXED}])
    assert seen["model"] == "claude-haiku-5-5" and seen["refine"] is True
    assert got["usage"]["cost_usd"] == 0.0003                # the page shows this as a share of the monthly AI
    # the article around the passage is never sent: only the title and the passages themselves
    assert "PASSAGES TO FIX" in seen["user_msg"] and "FULL ARTICLE" not in seen["user_msg"]


# ------------------------------------------------------------------ a writing fixer, nothing more
def test_it_fixes_the_writing_and_gives_back_both_versions(monkeypatch):
    blocks = [{"id": "h1", "type": "heading", "data": {"html": "fix the small stuff first"}},
              {"id": "p1", "type": "paragraph", "data": {"html": ROUGH}},
              {"id": "l1", "type": "bulleted_list", "data": {"items": ["paint front door", "new mulch, its cheap"]}},
              {"id": "ok", "type": "paragraph", "data": {"html": "This one already reads well."}}]
    got, _ = _polish(monkeypatch, blocks, [{"id": "h1", "text": "Fix the small stuff first"}, {"id": "p1", "text": FIXED},
                                           {"id": "l1", "text": "Paint front door\nNew mulch, it's cheap"}, {"id": "ok", "text": "This one already reads well."}])
    by = {c["id"]: c for c in got["changes"]}
    assert set(by) == {"h1", "p1", "l1"}                                         # the one that was already fine is not offered
    assert by["p1"]["before"] == ROUGH and by["p1"]["after"] == FIXED and by["p1"]["data"] == {"html": FIXED}
    assert by["l1"]["data"] == {"items": ["Paint front door", "New mulch, it's cheap"]}      # a list stays a list, line for line


def test_bold_words_and_links_come_back_where_they_were(monkeypatch):
    html_in = 'i sold 3 homes on <strong>orange street</strong>, see <a href="https://www.mediaborough.com/permits" target="_blank" rel="noopener">the permit page</a> first'
    marks = w.html_to_marks(html_in)
    assert marks == "i sold 3 homes on **orange street**, see [the permit page](https://www.mediaborough.com/permits) first"
    got, _ = _polish(monkeypatch, [{"id": "p", "type": "paragraph", "data": {"html": html_in}}],
                     [{"id": "p", "text": "I sold 3 homes on **orange street**. See [the permit page](https://www.mediaborough.com/permits) first."}])
    out = got["changes"][0]["data"]["html"]
    assert "<strong>orange street</strong>" in out and 'href="https://www.mediaborough.com/permits"' in out and "**" not in out
    # the side-by-side text is plain words, no marks
    assert "**" not in got["changes"][0]["after"] and "](" not in got["changes"][0]["after"]
    # formatting it does not know (underline, strike-through) : that passage is left alone rather than lose it
    assert w.html_to_marks("this is <u>underlined</u>") is None


@pytest.mark.parametrize("after, why", [
    (FIXED.replace("$412,500", "$421,500"), "a number changed"),
    (FIXED.replace("4%", "5%"), "a number changed"),
    (FIXED + " Staging helps too.\nAlso wash the windows.", "a line was added or lost"),
    (FIXED + " A fresh coat of paint is one of the most visible updates you can make, and it costs far less than most sellers expect it to.", "it grew or shrank too much"),
    ("Fix it.", "a number changed"),
    (FIXED.replace("in Media", "in Swarthmore"), "a name the agent did not write (Swarthmore)"),
])
def test_a_reply_that_changes_a_fact_or_adds_anything_is_thrown_away(monkeypatch, after, why):
    assert w.polish_problem(ROUGH, after) == why
    got, _ = _polish(monkeypatch, [{"id": "p1", "type": "paragraph", "data": {"html": ROUGH}}], [{"id": "p1", "text": after}])
    assert got["changes"] == []                                                   # the agent's own words stay


def test_a_clean_fix_passes_every_check():
    assert w.polish_problem(ROUGH, FIXED) is None
    assert w.polish_problem("paint front door\nnew mulch, its cheap", "Paint front door\nNew mulch, it's cheap") is None
    assert w.polish_problem("see [the page](https://a.example/x) first", "See [the page](https://a.example/x) first.") is None
    assert w.polish_problem("see [the page](https://a.example/x) first", "See [the page](https://b.example/y) first.") == "a link changed"


def test_nothing_can_be_added_moved_or_made_up(monkeypatch):
    # a passage the model invents (an id that was never sent), a card, and an empty section are all ignored or refused
    got, _ = _polish(monkeypatch, [{"id": "p1", "type": "paragraph", "data": {"html": ROUGH}}, {"id": "c1", "type": "cta", "data": {"label": "Ask me"}}],
                     [{"id": "p1", "text": FIXED}, {"id": "new-section", "text": "A whole new section the model made up."}, {"id": "c1", "text": "Changed button"}])
    assert [c["id"] for c in got["changes"]] == ["p1"]
    with pytest.raises(HTTPException) as e:
        _polish(monkeypatch, [{"id": "h", "type": "heading", "data": {"html": ""}}, {"id": "p", "type": "paragraph", "data": {"html": "  "}}], [])
    assert e.value.status_code == 422 and "nothing written" in e.value.detail
    rules = w._POLISH_SYSTEM
    assert "Add nothing" in rules and "Keep every fact, number, price, date, name and place exactly" in rules and "never merge, split, add, drop or reorder" in rules


def test_the_routes_exist_and_are_reached_before_the_article_route():
    from app.api import blog_routes
    from app.schemas.blogs import BlogAiPolishResponse
    assert BlogAiPolishResponse(changes=[], used={"usd": 0.0003, "percent": 0.0006}).used == {"usd": 0.0003, "percent": 0.0006}
    paths = [r.path for r in blog_routes.router.routes]
    assert any(p.endswith("/ai/polish") for p in paths)
    gets = [r.path for r in blog_routes.router.routes if "GET" in getattr(r, "methods", ())]
    # declared before "/{blog_id}", or the word "ai" would be read as an article id
    assert next(i for i, p in enumerate(gets) if p.endswith("/ai/refine-estimate")) < gets.index(blog_routes.router.prefix + "/{blog_id}")


# ------------------------------------------------------------------ the old "rewrite the section" call (no button uses it now)
def test_the_old_section_rewrite_refuses_a_heading_with_nothing_under_it(monkeypatch):
    # pressed for real on a new article's starter section: given only a heading, the AI wrote a whole section itself
    _stand_in(monkeypatch, {})
    with pytest.raises(HTTPException) as e:
        asyncio.run(w.refine_section(None, USER, title="t", action="rewrite", instruction=None, article_text="x",
                                     section_blocks=[{"id": "h1", "type": "heading", "data": {"html": "Untitled Article"}}, {"id": "p1", "type": "paragraph", "data": {"html": ""}}]))
    assert e.value.status_code == 422 and "empty" in e.value.detail


# ------------------------------------------------------------------ the website
def test_the_editor_offers_refine_only_on_the_agents_own_words():
    # AI-written blocks carry "-ai-" in their id (the server's _bid); a block whose words the agent changed is marked
    assert "-ai-" in w._bid("paragraph") and "const AI_ID = /-ai-/" in OPS
    assert "export const byHand = (b: BlogBlock) => !AI_ID.test(b.id) || !!b.data?.edited" in OPS
    targets = OPS[OPS.index("export function refineTargets"):OPS.index("export function canRefine")]
    assert "(!ai || byHand(x))" in targets and "plainOf(x).trim()" in targets                 # only their words, only where there are words
    rule = OPS[OPS.index("export function canRefine"):OPS.index("export function unitRange")]
    assert "if (b.type === 'heading') return refineTargets(blocks, id).some(x => x.type !== 'heading')" in rule     # one button per section
    assert "if (aiWrote(blocks)) return byHand(b)" in rule                                     # the AI's own paragraphs never show it
    # every edit passes through one place that marks the changed block; the toolbar asks the rule; the page sends the targets
    assert "next = stampEdited(prev, next)" in EDITOR and "{canRefine(p.blocks, b.id) && (" in EDITOR
    assert "const targets = refineTargets(blog.blocks, id)" in PAGE and "api.blogs.aiPolish({ title: blog.title, blocks: targets })" in PAGE
    # what Refine may touch is words, the same kinds on both sides
    kinds = "".join(re.search(rf"export const {name}: BlockType\[\] = \[([^\]]*)\]", OPS).group(1) for name in ("RICH_TYPES", "LIST_TYPES"))
    assert sorted(re.findall(r"'(\w+)'", kinds)) == sorted(w.POLISH_TYPES)
    assert "export const POLISH_TYPES: BlockType[] = [...RICH_TYPES, ...LIST_TYPES]" in OPS


def test_one_button_no_menu_of_rewrites_and_a_before_and_after_to_pick_from():
    assert "REFINE_ACTIONS" not in EDITOR and "Expand with more depth" not in EDITOR and "Or tell it what to change" not in EDITOR
    assert 'data-testid="blog-refine-go"' in EDITOR and "Fix my writing" in EDITOR
    assert "Keep mine" in COMPARE and "Use the new version" in COMPARE and 'data-testid="blog-refine-before"' in COMPARE and 'data-testid="blog-refine-after"' in COMPARE
    # nothing changes until the agent picks the new version, and then only those passages' words
    assert "setRefineOffer({ changes: res.data.changes" in PAGE and "onUse={useRefined} onKeep={() => setRefineOffer(null)}" in PAGE
    use = PAGE[PAGE.index("const useRefined = () => {"):PAGE.index("const handleCoverUpload")]
    assert "current.blocks.map(b => {" in use and "if (!d) return b" in use


def test_there_is_no_draft_with_ai_button_in_the_editor():
    assert "Draft with AI" not in PAGE and "confirmRewrite" not in PAGE and "showAiDraft" not in PAGE
    assert 'no "Draft with AI" button in the editor' in GUIDE and "Fix my writing" in GUIDE          # the AI helper knows the new buttons
    assert "Expand with more depth" not in GUIDE


def test_every_ai_button_says_about_how_much_before_and_how_much_after():
    assert "aiUsesLine" in SHARE and "aiUsedLine" in SHARE and "'less than 0.01%'" in SHARE     # never a bare "0%"
    assert 'data-testid="blog-refine-cost"' in EDITOR and "aiUsesLine(p.refineEstimate)" in EDITOR and "api.blogs.aiRefineEstimate()" in PAGE
    assert 'data-testid="blog-refine-used"' in COMPARE and "aiUsedLine(used)" in COMPARE and 'data-testid="blog-refine-used"' in PAGE
    assert "Each picture uses about {pct}% of your monthly AI" in COVER and 'data-testid="blog-cover-used"' in COVER and "r.data?.cost_usd" in COVER


def test_a_cover_picture_already_paid_for_is_shown_again_when_the_agent_comes_back():
    # 11/16, making five real covers: the page reloaded while the first was being made - it was paid for and kept by
    # the server, but the screen came back empty and a second press would have paid again.
    assert "useState<string | null>(last?.url || null)" in COVER
    assert "last={(blog.ai_review as any)?.cover_ai ? { url: (blog.ai_review as any).cover_ai.last_url" in PAGE
    # the changes already used on that picture still count
    assert "r.data.max_revisions - (last?.url ? Number(last.revisions) || 0 : 0)" in COVER


def test_a_new_article_does_not_start_with_a_heading_readers_would_see_as_untitled_article():
    starter = TOPICS[TOPICS.index("if (!topic) {"):TOPICS.index("return [", TOPICS.index("if (!topic) {") + 30) + 400]
    assert re.search(r"type: 'heading', data: \{ text: '' \}", starter) and "data: { text: 'Untitled Article' }" not in TOPICS
    # ... and an empty heading's "type here" hint is not squeezed into the look's 4px gold bar (it stood one letter per line)
    assert '[data-editing] h2[data-host]:empty::after{content:attr(data-ph)' in EDITOR and '[data-editing] h2[data-host]:empty::before{content:""' in EDITOR
