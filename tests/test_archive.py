"""Tests for zotero_arxiv_daily.archive: 按日期落盘。"""

import json
from datetime import datetime, timedelta, timezone

from tests.canned_responses import make_sample_paper
from zotero_arxiv_daily.archive import current_day, save_daily_papers


def test_current_day_uses_requested_offset():
    # +8 与 UTC 同一天时，两个偏移应该给出同一个日期；差 8 小时的时刻则应不同
    now_utc = datetime.now(timezone.utc)
    assert current_day(0) == now_utc.strftime("%Y-%m-%d")
    assert current_day(8) == (now_utc + timedelta(hours=8)).strftime("%Y-%m-%d")


def test_save_daily_papers_creates_dated_dir(tmp_path):
    papers = [
        make_sample_paper(title="Paper A", score=7.5, tldr="TLDR A", affiliations=["MIT"]),
        make_sample_paper(title="Paper B", score=None, tldr=None, abstract="Abstract B"),
    ]

    out_dir = save_daily_papers(papers, tmp_path, offset_hours=8)

    assert out_dir == tmp_path / current_day(8)
    assert (out_dir / "papers.md").is_file()
    assert (out_dir / "papers.json").is_file()

    md = (out_dir / "papers.md").read_text(encoding="utf-8")
    assert "Paper A" in md and "Paper B" in md
    assert "TLDR A" in md
    assert "Abstract B" in md
    assert "N/A" in md  # 没有 score 的论文

    data = json.loads((out_dir / "papers.json").read_text(encoding="utf-8"))
    assert data["date"] == current_day(8)
    assert data["count"] == 2
    assert [p["title"] for p in data["papers"]] == ["Paper A", "Paper B"]
    assert data["papers"][0]["tldr"] == "TLDR A"


def test_save_daily_papers_handles_empty_list(tmp_path):
    out_dir = save_daily_papers([], tmp_path)
    assert (out_dir / "papers.md").is_file()
    assert json.loads((out_dir / "papers.json").read_text(encoding="utf-8"))["count"] == 0
