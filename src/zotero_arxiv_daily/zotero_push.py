"""把当天论文写进 Zotero：建（或复用）分类，然后批量创建条目。

设计要点：
- 分类名用模板（默认 "{date}"），挂在父分类下，于是每天一个子分类；
- 父分类/子分类都按名字查找，已存在就复用，不会重复建；
- 单个条目失败只告警，不影响整批和发信；
- 客户端可注入，便于测试时用假 client。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from loguru import logger

TAG = "auto-arxiv-daily"


def _extract_key(resp) -> str | None:
    """兼容 pyzotero 不同返回形态：{'success': {'0': key}} / {'successful': {'0': {...}}} / 直接给 key。"""
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, dict):
        return None
    for field in ("success", "successful"):
        bucket = resp.get(field) or {}
        if isinstance(bucket, dict) and bucket:
            first = next(iter(bucket.values()))
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                return first.get("key") or (first.get("data") or {}).get("key")
    if isinstance(resp.get("key"), str):
        return resp["key"]
    return None


def split_name(full_name: str) -> dict:
    """Zotero 作者字段：多词拆 firstName/lastName，单词用单字段 name。"""
    parts = (full_name or "").split()
    if len(parts) <= 1:
        return {"creatorType": "author", "name": full_name or "Unknown"}
    return {"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]}


def paper_to_item(paper, collection_key: str) -> dict:
    archive_id = (paper.url or "").rstrip("/").rsplit("/", 1)[-1]
    item = {
        "itemType": "preprint",
        "title": paper.title,
        "creators": [split_name(a) for a in paper.authors][:100],
        "abstractNote": paper.abstract or "",
        "url": paper.url,
        "repository": "arXiv",
        "archiveID": archive_id,
        "collections": [collection_key],
        "tags": [{"tag": TAG}, {"tag": paper.source}] if paper.source else [{"tag": TAG}],
    }
    if paper.pdf_url:
        item["extra"] = f"PDF: {paper.pdf_url}"
    return item


class ZoteroPusher:
    def __init__(self, config, client=None):
        self.config = config
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from pyzotero import zotero

            self._client = zotero.Zotero(self.config.zotero.user_id, "user", self.config.zotero.api_key)
        return self._client

    def _find_collection(self, name: str, parent_key: str | None) -> str | None:
        for collection in self.client.everything(self.client.collections()):
            data = collection.get("data", {})
            if data.get("name") != name:
                continue
            if (data.get("parentCollection") or None) == (parent_key or None):
                return collection.get("key")
        return None

    def ensure_collection(self, name: str, parent_key: str | None = None) -> str:
        existing = self._find_collection(name, parent_key)
        if existing:
            logger.info(f"Reusing zotero collection {name!r} ({existing})")
            return existing
        payload = {"name": name}
        if parent_key:
            payload["parentCollection"] = parent_key
        key = _extract_key(self.client.create_collections([payload]))
        if not key:
            raise RuntimeError(f"Failed to create zotero collection {name!r}")
        logger.info(f"Created zotero collection {name!r} ({key})")
        return key

    def push(self, papers: Iterable) -> int:
        papers = list(papers)
        if not papers:
            return 0

        settings = self.config.zotero.get("push") or {}
        offset = int(self.config.executor.get("archive_timezone_offset", 8))
        day = datetime.now(timezone(timedelta(hours=offset))).strftime("%Y-%m-%d")

        parent_name = str(settings.get("parent_collection") or "").strip()
        parent_key = self.ensure_collection(parent_name) if parent_name else None

        template = str(settings.get("collection_name") or "{date}")
        collection_name = template.format(date=day, source="arxiv")
        collection_key = self.ensure_collection(collection_name, parent_key)

        # pyzotero 的 create_items 会自动按 50 条分块
        resp = self.client.create_items([paper_to_item(p, collection_key) for p in papers]) or {}
        bucket = resp.get("success") or resp.get("successful") or {}
        failed = resp.get("failed") or {}
        created = len(bucket) if bucket else len(papers) - len(failed)
        if failed:
            logger.warning(f"Zotero rejected {len(failed)} item(s): {list(failed.values())[:3]}")
        logger.info(f"Pushed {created} papers to zotero collection {collection_name!r} ({collection_key})")
        return created
