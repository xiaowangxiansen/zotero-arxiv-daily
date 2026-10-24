"""Tests for zotero_arxiv_daily.construct_email: render_email, get_stars, get_block_html."""

from zotero_arxiv_daily.construct_email import render_email, get_stars, get_block_html, get_empty_html
from tests.canned_responses import make_sample_paper


def test_render_email_with_papers():
    papers = [make_sample_paper(score=7.5, tldr="A great paper.", affiliations=["MIT"])]
    html = render_email(papers)
    assert "Sample Paper Title" in html
    assert "A great paper." in html
    assert "MIT" in html


def test_render_email_empty_list():
    html = render_email([])
    assert "No Papers Today" in html


def test_render_email_author_truncation():
    authors = [f"Author {i}" for i in range(10)]
    paper = make_sample_paper(authors=authors, score=7.0, tldr="ok")
    html = render_email([paper])
    assert "Author 0" in html
    assert "Author 1" in html
    assert "Author 2" in html
    assert "..." in html
    assert "Author 8" in html
    assert "Author 9" in html
    # Middle authors should be truncated
    assert "Author 5" not in html


def test_render_email_affiliation_truncation():
    affiliations = [f"Uni {i}" for i in range(8)]
    paper = make_sample_paper(affiliations=affiliations, score=7.0, tldr="ok")
    html = render_email([paper])
    assert "Uni 0" in html
    assert "Uni 4" in html
    assert "..." in html
    assert "Uni 7" not in html


def test_render_email_no_affiliations():
    paper = make_sample_paper(affiliations=None, score=7.0, tldr="ok")
    html = render_email([paper])
    assert "Unknown Affiliation" in html


def test_render_email_uses_abstract_when_tldr_missing():
    # 关掉 LLM 后没有 TLDR，用摘要兜底，且不能出现字面量 None
    paper = make_sample_paper(tldr=None, abstract="A plain abstract about widgets.", score=7.0)
    html = render_email([paper])
    assert "Abstract:" in html
    assert "A plain abstract about widgets." in html
    assert "TLDR:" not in html
    assert ">None<" not in html


def test_get_stars_low_score():
    assert get_stars(5.0) == ""
    assert get_stars(6.0) == ""


def test_get_stars_high_score():
    stars = get_stars(8.0)
    assert stars.count("full-star") == 5


def test_get_stars_mid_score():
    stars = get_stars(7.0)
    assert "star" in stars
    assert stars.count("full-star") + stars.count("half-star") > 0


def test_render_email_with_abstract_zh():
    """有中文摘要翻译时，必须单独渲染一行，且标为「中文摘要」。"""
    paper = make_sample_paper(score=7.5, tldr="A great paper.", abstract_zh="这篇论文提出了新方法。")
    html = render_email([paper])
    assert "中文摘要" in html
    assert "这篇论文提出了新方法。" in html
    # 英文 TLDR 仍应在，两者不能互相覆盖
    assert "A great paper." in html


def test_render_email_without_abstract_zh():
    """翻译失败（None）时不能出现字面量 None，也不能留下空行标签。"""
    paper = make_sample_paper(score=7.0, tldr="ok", abstract_zh=None)
    html = render_email([paper])
    assert "中文摘要" not in html
    assert ">None<" not in html


def test_render_email_abstract_zh_survives_missing_field():
    """老 Paper（没有 abstract_zh 字段）不能让渲染崩掉。"""
    paper = make_sample_paper(score=7.0, tldr="ok")
    del paper.abstract_zh
    html = render_email([paper])
    assert "ok" in html
    assert "中文摘要" not in html


def test_get_block_html_contains_all_fields():
    html = get_block_html("Title", "Auth", "3.5", "Summary", "http://pdf.url", "MIT")
    assert "Title" in html
    assert "Auth" in html
    assert "3.5" in html
    assert "Summary" in html
    assert "http://pdf.url" in html
    assert "MIT" in html


def test_get_block_html_with_abstract_zh():
    html = get_block_html("Title", "Auth", "3.5", "Summary", "http://pdf.url", "MIT", "TLDR", "中文译文")
    assert "摘要翻译" in html
    assert "中文译文" in html


def test_get_empty_html():
    html = get_empty_html()
    assert "No Papers Today" in html
