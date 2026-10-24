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


def _key_of(value) -> str | None:
    """Zotero 写入返回值里一个「条目 key」可能是字符串，也可能包成 {'key': ...} / {'data': {'key': ...}}。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("key") or (value.get("data") or {}).get("key")
    return None


def _extract_key(resp) -> str | None:
    """兼容 pyzotero 不同返回形态：{'success': {'0': key}} / {'successful': {'0': {...}}} / 直接给 key。"""
    if isinstance(resp, str):
        return resp
    if not isinstance(resp, dict):
        return None
    for field in ("success", "successful"):
        bucket = resp.get(field) or {}
        if isinstance(bucket, dict) and bucket:
            return _key_of(next(iter(bucket.values())))
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


def paper_to_attachment(paper, parent_key: str) -> dict | None:
    """给条目挂一个 PDF 附件。

    linkMode 用 linked_url（只存链接，不占 Zotero 存储配额）：点开直接跳 arXiv PDF。
    改成 imported_url + upload_attachments 就是把 PDF 文件本体存进 Zotero，
    但每天 30 篇约 60MB，免费 300MB 配额几天就满，所以默认用链接。
    """
    if not paper.pdf_url:
        return None
    return {
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "linked_url",
        "title": "arXiv PDF",
        "url": paper.pdf_url,
        "contentType": "application/pdf",
        "tags": [{"tag": TAG}],
    }


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
        """全库已有条目（用 archiveID / url 判定），跨日期分类去重。

        原来的实现只读「当天」那个分类，于是第二天跑的时候当天分类是空的，
        昨天已经推过的论文会被当成新论文再推一遍 —— 这是「连着两天收到同样文章」的根因。
        现在改为扫描整个文库的顶层条目（preprint/journalArticle 等），
        不论它挂在哪个日期分类下，只要 archiveID 或 url 出现过就跳过。
        """
        seen = set()

        def absorb(items):
            for item in items:
                data = item.get("data", {})
                # 只收顶层文献条目；附件/笔记的 url 会把 arXiv PDF 直链带进来，
                # 但同一篇论文的条目本身也带 url，所以按 url 判定仍然成立。
                if data.get("itemType") in ("attachment", "note", "annotation"):
                    continue
                if data.get("archiveID"):
                    seen.add(str(data["archiveID"]))
                if data.get("url"):
                    seen.add(str(data["url"]).rstrip("/"))

        # 1) 先扫全库顶层条目：这是跨日期去重的关键
        try:
            absorb(self.client.everything(self.client.items()))
        except Exception as exc:
            logger.warning(f"Failed to read whole library for dedupe: {exc}")
        # 2) 再补当天分类（万一上一步被限流返回不全）
        if collection_key:
            try:
                absorb(self.client.everything(self.client.collection_items(collection_key)))
            except Exception as exc:
                logger.warning(f"Failed to read existing items of collection {collection_key}: {exc}")
        return seen

    def known_archive_ids(self) -> set[str]:
        """对外暴露「库里已有」的 ID 集合，供执行器在截断到 max_paper_num 之前先剔除旧论文。

        必须「先剔除再截断」：否则每天取前 30 篇里有一半是旧的，剔除后只剩十几篇新的，
        日志看着推送成功，用户却觉得当天收获变少。
        """
        return self._existing_keys("")

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
        attached = 0
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
            attached += self._attach_pdfs(batch, resp)
            queue = queue[BATCH_SIZE:]

        if queue:
            logger.warning(f"{len(queue)} paper(s) were not pushed after {MAX_ATTEMPTS} attempts")

        if category_keys:
            summary = ", ".join(f"{name}({len([p for p in papers if category_of(p) == name])})" for name in category_keys)
            logger.info(f"Zotero category sub-collections under {collection_name!r}: {summary}")
        logger.info(f"Pushed {created} papers to zotero collection {collection_name!r} ({collection_key})")
        if attached:
            logger.info(f"Attached {attached} PDF link(s) to the new items")
        return created

    def _attach_pdfs(self, batch: list, resp: dict) -> int:
        """按 create_items 返回的 key 给每条新条目挂 PDF 附件；失败只告警，不影响主流程。"""
        success = resp.get("success") or resp.get("successful") or {}
        attachments = []
        for index, value in success.items():
            key = _key_of(value)
            if not key:
                continue
            try:
                paper = batch[int(index)]
            except (TypeError, ValueError, IndexError):
                continue
            attachment = paper_to_attachment(paper, key)
            if attachment:
                attachments.append(attachment)
        if not attachments:
            return 0
        try:
            self.client.create_items(attachments)
        except Exception as exc:
            logger.warning(f"Failed to attach {len(attachments)} PDF link(s): {exc}")
            return 0
        return len(attachments)
