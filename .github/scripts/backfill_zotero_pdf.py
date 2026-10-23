"""一次性回填：给某个日期分类下已有的条目补 PDF 附件（linked_url，不占存储配额）。

背景：zotero_push 早先只写元数据，PDF 只放在 extra 字段里，Zotero 里点开没有附件。
新代码会给新条目自动挂附件，这个脚本负责把存量补齐。

幂等：已经挂过附件的条目会跳过（先读出全库附件的 parentItem 集合）。
用法：ZOTERO_ID=... ZOTERO_KEY=... TARGET_DATE=2026-10-22 uv run python <本文件>
DRY_RUN=1 只打印计划。
"""
from __future__ import annotations

import os
import sys
import time

from loguru import logger
from pyzotero import zotero

PARENT_NAME = "arXiv Daily"
TAG = "auto-arxiv-daily"
BATCH = 50


def pdf_url_of(item: dict) -> str | None:
    """优先用 extra 里的 PDF 行，退化为把摘要页 /abs/ 换成 /pdf/。"""
    data = item.get("data", {})
    for line in (data.get("extra") or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("pdf:"):
            value = stripped.split(":", 1)[1].strip()
            if value:
                return value
    url = str(data.get("url") or "").strip()
    if "/abs/" in url:
        return url.replace("/abs/", "/pdf/")
    return None


def find_collection(client, name: str, parent_key: str | None) -> str | None:
    for collection in client.everything(client.collections()):
        data = collection.get("data", {})
        if data.get("name") == name and (data.get("parentCollection") or None) == (parent_key or None):
            return collection.get("key")
    return None


def main() -> int:
    day = os.environ.get("TARGET_DATE", "").strip()
    if not day:
        logger.error("TARGET_DATE 必须给出，例如 2026-10-22")
        return 1
    dry_run = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false")

    client = zotero.Zotero(os.environ["ZOTERO_ID"], "user", os.environ["ZOTERO_KEY"])
    parent_key = find_collection(client, PARENT_NAME, None)
    date_key = find_collection(client, day, parent_key) if parent_key else None
    if not date_key:
        logger.error(f"找不到分类 {PARENT_NAME!r}/{day!r}")
        return 1

    items = [
        i for i in client.everything(client.collection_items(date_key))
        if (i.get("data") or {}).get("itemType") not in ("attachment", "note")
    ]
    have_attachment = {
        (i.get("data") or {}).get("parentItem")
        for i in client.everything(client.items(itemType="attachment"))
    }
    logger.info(f"{day}: {len(items)} item(s), library has {len(have_attachment)} item(s) with attachments")

    payloads = []
    skipped_has = 0
    skipped_no_pdf = 0
    for item in items:
        if item["key"] in have_attachment:
            skipped_has += 1
            continue
        pdf_url = pdf_url_of(item)
        if not pdf_url:
            skipped_no_pdf += 1
            continue
        payloads.append(
            {
                "itemType": "attachment",
                "parentItem": item["key"],
                "linkMode": "linked_url",
                "title": "arXiv PDF",
                "url": pdf_url,
                "contentType": "application/pdf",
                "tags": [{"tag": TAG}],
            }
        )

    logger.info(f"计划新增 {len(payloads)} 个附件（已有附件跳过 {skipped_has}，无 PDF 链接跳过 {skipped_no_pdf}）")
    if dry_run:
        logger.info("DRY_RUN=1，不写入")
        return 0

    created = 0
    for start in range(0, len(payloads), BATCH):
        chunk = payloads[start : start + BATCH]
        try:
            client.create_items(chunk)
        except Exception as exc:
            logger.warning(f"batch of {len(chunk)} failed: {exc}")
            continue
        created += len(chunk)
        logger.info(f"created {created}/{len(payloads)} attachment(s)")
        time.sleep(1)

    logger.info(f"Done. {created} PDF link(s) attached under {PARENT_NAME}/{day}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
