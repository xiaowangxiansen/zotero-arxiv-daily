"""把当天抓到的论文按日期落盘，方便下载/归档。

产物（每天一个目录）：
    papers/2026-10-22/papers.md    人类可读：标题/作者/链接/相关度/摘要
    papers/2026-10-22/papers.json  机器可读，便于后续再加工

目录不进仓库（它只是当天产物的临时存放），由 workflow 用 artifact 发布出去。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .protocol import Paper


def current_day(offset_hours: int = 8) -> str:
    """按指定时区给出「今天」。默认 +8（北京时间），避免 UTC 22 点跑出来的目录比实际早一天。"""
    return datetime.now(timezone(timedelta(hours=offset_hours))).strftime("%Y-%m-%d")


def _paper_to_dict(index: int, paper: "Paper") -> dict:
    return {
        "index": index,
        "title": paper.title,
        "authors": list(paper.authors),
        "abstract": paper.abstract,
        "tldr": paper.tldr,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "score": paper.score,
        "source": paper.source,
    }


def _paper_to_markdown(index: int, paper: "Paper") -> str:
    score = f"{paper.score:.1f}" if paper.score is not None else "N/A"
    lines = [
        f"## {index}. {paper.title}",
        "",
        f"- 作者：{', '.join(paper.authors)}",
        f"- 来源：{paper.source}　相关度：{score}",
        f"- 摘要页：{paper.url}",
    ]
    if paper.pdf_url:
        lines.append(f"- PDF：{paper.pdf_url}")
    lines.append("")
    if paper.tldr:
        lines += [f"**TLDR**：{paper.tldr}", ""]
    if paper.abstract:
        lines += ["**摘要**：", "", paper.abstract.strip(), ""]
    return "\n".join(lines)


def save_daily_papers(papers: Iterable["Paper"], archive_root: Path | str, offset_hours: int = 8) -> Path:
    """把 papers 写到 <archive_root>/<YYYY-MM-DD>/ 下，返回当天目录。"""
    papers = list(papers)
    day = current_day(offset_hours)
    out_dir = Path(archive_root) / day
    out_dir.mkdir(parents=True, exist_ok=True)

    items = [_paper_to_dict(i, p) for i, p in enumerate(papers, 1)]
    (out_dir / "papers.json").write_text(
        json.dumps({"date": day, "count": len(items), "papers": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    header = [f"# arXiv Daily · {day}", "", f"共 {len(items)} 篇", ""]
    body = "\n".join(_paper_to_markdown(i, p) for i, p in enumerate(papers, 1))
    (out_dir / "papers.md").write_text("\n".join(header) + body, encoding="utf-8")

    return out_dir
