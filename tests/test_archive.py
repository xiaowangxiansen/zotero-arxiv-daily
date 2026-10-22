"""Tests for zotero_arxiv_daily.archive: 按日期 + 方向落盘。"""

import json
from datetime import datetime, timedelta, timezone

from tests.canned_responses import make_sample_paper
from zotero_arxiv_daily.archive import category_of, current_day, save_daily_papers


def test_current_day_uses_requested_offset():
    now_utc = datetime.now(timezone.utc)
    assert current_day(0) == now_utc.strftime("%Y-%m-%d")
    assert current_day(8) == (now_utc + timedelta(hours=8)).strftime("%Y-%m-%d")


def test_category_of_prefers_primary_then_first_then_other():
    assert category_of(make_sample_paper(primary_category="cs.CV", categories=["cs.CV", "cs.AI"])) == "cs.CV"
    assert category_of(make_sample_paper(primary_category=None, categories=["cs.LG"])) == "cs.LG"
    assert category_of(make_sample_paper(primary_category=None, categories=[])) == "other"


def test_save_groups_papers_into_category_subdirs(tmp_path):
    papers = [
        make_sample_paper(title="AI Paper 1", primary_category="cs.AI", score=7.5, tldr="TLDR AI"),
        make_sample_paper(title="AI Paper 2", primary_category="cs.AI"),
        make_sample_paper(title="CV Paper", primary_category="cs.CV"),
    ]

    out_dir = save_daily_papers(papers, tmp_path, offset_hours=8)

    assert out_dir == tmp_path / current_day(8)
    assert (out_dir / "index.md").is_file()

    ai_md = (out_dir / "cs.AI" / "papers.md").read_text(encoding="utf-8")
    assert "AI Paper 1" in ai_md and "AI Paper 2" in ai_md
    assert "CV Paper" not in ai_md
    assert "TLDR AI" in ai_md

    cv_md = (out_dir / "cs.CV" / "papers.md").read_text(encoding="utf-8")
    assert "CV Paper" in cv_md

    ai_json = json.loads((out_dir / "cs.AI" / "papers.json").read_text(encoding="utf-8"))
    assert ai_json["category"] == "cs.AI"
    assert ai_json["count"] == 2
    assert [p["title"] for p in ai_json["papers"]] == ["AI Paper 1", "AI Paper 2"]

    index = (out_dir / "index.md").read_text(encoding="utf-8")
    assert "| cs.AI | 2 |" in index
    assert "| cs.CV | 1 |" in index


def test_save_handles_empty_list(tmp_path):
    out_dir = save_daily_papers([], tmp_path)
    index = (out_dir / "index.md").read_text(encoding="utf-8")
    assert "共 0 篇" in index
