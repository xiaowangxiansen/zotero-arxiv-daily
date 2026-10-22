"""把当天论文写进 Zotero：建（或复用）分类，然后批量创建条目。

设计要点：
- 分类层级：<父分类>/<日期>/<方向>，例如 arXiv Daily/2026-10-22/cs.AI；
- 条目同时属于「日期」和「方向」两个分类，日期目录仍能看到当天全部论文；
- 父分类/日期/方向都按名字查找，已存在就复用，不会重复建；
- 单个条目失败只告警，不影响整批和发信；
- 客户端可注入，便于测试时用假 client。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from loguru import logger

from .archive import category_of

TAG = "auto-arxiv-daily"
# Zotero 单次最多创建 50 条；但一次 50 条容易把响应拖到超时，25 条更稳
BATCH_SIZE = 25
MAX_ATTEMPTS = 3


def _archive_id(paper) -> str:
    return (getattr(paper, "url", "") or "").rstrip("/").rsplit("/", 1)[-1]


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


def paper_to_item(paper, collection_keys: str | list[str]) -> dict:
    """collection_keys 传单个 key 或一组 key：一组时条目同时属于日期分类和方向分类。"""
    if isinstance(collection_keys, str):
        collection_keys = [collection_keys]
    archive_id = _archive_id(paper)
    item = {
        "itemType": "preprint",
        "title": paper.title,
        "creators": [split_name(a) for a in paper.authors][:100],
        "abstractNote": paper.abstract or "",
        "url": paper.url,
        "repository": "arXiv",
        "archiveID": archive_id,
        "collections": list(collection_keys),
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

    def _existing_keys(self, collection_key: str) -> set[str]:
        """当天分类里已有的条目（用 archiveID / url 判定），避免同一天重跑时重复写入。"""
        try:
            items = self.client.everything(self.client.collection_items(collection_key))
        except Exception as exc:
            logger.warning(f"Failed to read existing items of collection {collection_key}: {exc}")
            return set()
        seen = set()
        for item in items:
            data = item.get("data", {})
            if data.get("archiveID"):
                seen.add(str(data["archiveID"]))
            if data.get("url"):
                seen.add(str(data["url"]).rstrip("/"))
        return seen

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

        seen = self._existing_keys(collection_key)
        queue = [p for p in papers if _archive_id(p) not in seen and (p.url or "").rstrip("/") not in seen]
        skipped = len(papers) - len(queue)
        if skipped:
            logger.info(f"Skipped {skipped} paper(s) already present in collection {collection_name!r}")

        # 日期分类下再按方向建一层：条目同时属于「日期」和「方向」，日期目录仍能看到全部。
        # 只给真要写入的论文建分类，全被跳过时不产生空分类。
        category_keys: dict[str, str] = {}

        def build_items(batch):
            items = []
            for paper in batch:
                category = category_of(paper)
                if category not in category_keys:
                    category_keys[category] = self.ensure_collection(category, collection_key)
                items.append(paper_to_item(paper, [collection_key, category_keys[category]]))
            return items

        created = 0
        attempts = 0
        while queue and attempts < MAX_ATTEMPTS:
            batch = queue[:BATCH_SIZE]
            try:
                resp = self.client.create_items(build_items(batch)) or {}
            except Exception as exc:
                # 超时可能已经在服务端写成功：重读分类去重后再续传，避免重复条目
                attempts += 1
                logger.warning(f"Batch of {len(batch)} failed ({exc}); re-reading collection and retrying ({attempts}/{MAX_ATTEMPTS})")
                seen |= self._existing_keys(collection_key)
                queue = [p for p in queue if _archive_id(p) not in seen and (p.url or "").rstrip("/") not in seen]
                if attempts < MAX_ATTEMPTS:
                    time.sleep(3 * attempts)
                continue
            attempts = 0
            failed = resp.get("failed") or {}
            if failed:
                logger.warning(f"Zotero rejected {len(failed)} item(s): {list(failed.values())[:3]}")
            created += len(batch) - len(failed)
            queue = queue[BATCH_SIZE:]

        if queue:
            logger.warning(f"{len(queue)} paper(s) were not pushed after {MAX_ATTEMPTS} attempts")

        if category_keys:
            summary = ", ".join(f"{name}({len([p for p in papers if category_of(p) == name])})" for name in category_keys)
            logger.info(f"Zotero category sub-collections under {collection_name!r}: {summary}")
        logger.info(f"Pushed {created} papers to zotero collection {collection_name!r} ({collection_key})")
        return created
