"""把当天抓到的论文按日期 + 方向落盘，方便下载/归档。

产物（每天一个目录，下面再按 arXiv 主分类分子目录）：
    papers/2026-10-22/index.md              当天总览：各方向篇数
    papers/2026-10-22/cs.AI/papers.md       该方向论文（标题/作者/链接/相关度/摘要）
    papers/2026-10-22/cs.AI/papers.json     机器可读

目录不进仓库（.gitignore 已忽略），由 workflow 用 artifact 发布出去。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .protocol import Paper

FALLBACK_CATEGORY = "other"


def current_day(offset_hours: int = 8) -> str:
    """按指定时区给出「今天」。默认 +8（北京时间），避免 UTC 22 点跑出来的目录比实际早一天。"""
    return datetime.now(timezone(timedelta(hours=offset_hours))).strftime("%Y-%m-%d")


def category_of(paper: "Paper") -> str:
    """论文落到哪个方向目录：优先主分类，其次第一个分类，兜底 other。"""
    category = getattr(paper, "primary_category", None)
    if not category:
        categories = getattr(paper, "categories", None) or []
        category = categories[0] if categories else FALLBACK_CATEGORY
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(category)).strip("._-")
    return safe or FALLBACK_CATEGORY


def group_by_category(papers: Iterable["Paper"]) -> dict[str, list["Paper"]]:
    groups: dict[str, list["Paper"]] = {}
    for paper in papers:
        groups.setdefault(category_of(paper), []).append(paper)
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])))


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
        "primary_category": getattr(paper, "primary_category", None),
        "categories": list(getattr(paper, "categories", []) or []),
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


def _write_category_dir(out_dir: Path, day: str, category: str, papers: list["Paper"]) -> None:
    cat_dir = out_dir / category
    cat_dir.mkdir(parents=True, exist_ok=True)

    items = [_paper_to_dict(i, p) for i, p in enumerate(papers, 1)]
    (cat_dir / "papers.json").write_text(
        json.dumps(
            {"date": day, "category": category, "count": len(items), "papers": items},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    header = [f"# {category} · {day}", "", f"共 {len(items)} 篇", ""]
    body = "\n".join(_paper_to_markdown(i, p) for i, p in enumerate(papers, 1))
    (cat_dir / "papers.md").write_text("\n".join(header) + body, encoding="utf-8")


def _write_index(out_dir: Path, day: str, groups: dict[str, list["Paper"]]) -> None:
    total = sum(len(v) for v in groups.values())
    lines = [f"# arXiv Daily · {day}", "", f"共 {total} 篇，按 arXiv 主分类分目录：", ""]
    lines += ["| 方向 | 篇数 | 目录 |", "| --- | --- | --- |"]
    for category, papers in groups.items():
        lines.append(f"| {category} | {len(papers)} | [{category}/]({category}/papers.md) |")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_daily_papers(papers: Iterable["Paper"], archive_root: Path | str, offset_hours: int = 8) -> Path:
    """写到 <archive_root>/<YYYY-MM-DD>/<category>/，返回当天目录。"""
    papers = list(papers)
    day = current_day(offset_hours)
    out_dir = Path(archive_root) / day
    out_dir.mkdir(parents=True, exist_ok=True)

    groups = group_by_category(papers)
    for category, category_papers in groups.items():
        _write_category_dir(out_dir, day, category, category_papers)
    _write_index(out_dir, day, groups)

    return out_dir
