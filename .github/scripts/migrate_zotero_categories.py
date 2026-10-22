"""一次性迁移：把某个日期分类里的存量条目，按 arXiv 主分类放进方向子分类。

背景：zotero_push 早先只建「父分类/日期」两层，条目没有方向归属。改成三层之后，
存量条目不会自动补上方向（去重会跳过它们），所以用这个脚本补一次。

方向来源：条目里只存了 archiveID（arXiv 编号），没有分类字段，因此按 id 回查
arXiv API（一次最多 50 个 id），拿 <arxiv:primary_category>。查不到的统一进 other。

用法（在仓库根目录）：
    ZOTERO_ID=... ZOTERO_KEY=... TARGET_DATE=2026-10-22 \
        uv run python .github/scripts/migrate_zotero_categories.py
加 DRY_RUN=1 只打印计划，不写任何东西。
"""
from __future__ import annotations

import os
import sys
import time
import xml.etree.ElementTree as ET

import httpx
from loguru import logger
from pyzotero import zotero

PARENT_NAME = "arXiv Daily"
FALLBACK = "other"
ARXIV_API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
BATCH = 50


def arxiv_id(item: dict) -> str:
    """从条目里取 arXiv 编号：优先 archiveID，退化为 url 最后一段。"""
    data = item.get("data", {})
    raw = str(data.get("archiveID") or "").strip()
    if not raw:
        raw = str(data.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    if raw.startswith("arXiv:"):
        raw = raw.split(":", 1)[1]
    # 去掉版本号后缀 v1/v2
    if "v" in raw and raw.rsplit("v", 1)[-1].isdigit():
        raw = raw.rsplit("v", 1)[0]
    return raw


def fetch_categories(ids: list[str]) -> dict[str, str]:
    """按 id 批量查 arXiv 主分类，返回 {arxiv_id: category}。"""
    result: dict[str, str] = {}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            try:
                resp = client.get(ARXIV_API, params={"id_list": ",".join(chunk), "max_results": len(chunk)})
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
            except Exception as exc:
                logger.warning(f"arXiv query failed for {len(chunk)} ids: {exc}")
                continue
            for entry in root.findall("a:entry", NS):
                entry_id = (entry.findtext("a:id", default="", namespaces=NS) or "").rstrip("/").rsplit("/", 1)[-1]
                primary = entry.find("arxiv:primary_category", NS)
                if not entry_id or primary is None:
                    continue
                if "v" in entry_id and entry_id.rsplit("v", 1)[-1].isdigit():
                    entry_id = entry_id.rsplit("v", 1)[0]
                result[entry_id] = primary.get("term") or FALLBACK
            logger.info(f"Resolved {len(result)}/{min(start + BATCH, len(ids))} ids so far")
            time.sleep(3)  # arXiv 友好间隔
    return result


def find_collection(client, name: str, parent_key: str | None) -> str | None:
    for collection in client.everything(client.collections()):
        data = collection.get("data", {})
        if data.get("name") == name and (data.get("parentCollection") or None) == (parent_key or None):
            return collection.get("key")
    return None


def ensure_collection(client, name: str, parent_key: str) -> str:
    existing = find_collection(client, name, parent_key)
    if existing:
        return existing
    key = client.create_collections([{"name": name, "parentCollection": parent_key}])["success"]["0"]
    logger.info(f"Created sub-collection {name!r} ({key})")
    return key


def main() -> int:
    day = os.environ.get("TARGET_DATE", "").strip()
    if not day:
        logger.error("TARGET_DATE 必须给出，例如 2026-10-22")
        return 1
    dry_run = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false")

    client = zotero.Zotero(os.environ["ZOTERO_ID"], "user", os.environ["ZOTERO_KEY"])
    parent_key = find_collection(client, PARENT_NAME, None)
    if not parent_key:
        logger.error(f"找不到父分类 {PARENT_NAME!r}")
        return 1
    date_key = find_collection(client, day, parent_key)
    if not date_key:
        logger.error(f"找不到日期分类 {day!r}（父分类 {PARENT_NAME!r}）")
        return 1

    items = [i for i in client.everything(client.collection_items(date_key)) if i.get("data", {}).get("itemType")]
    logger.info(f"{day}: {len(items)} item(s) in collection {date_key}")

    ids = [arxiv_id(i) for i in items]
    categories = fetch_categories([i for i in ids if i])
    logger.info(f"arXiv returned categories for {len(categories)}/{len(ids)} ids")

    plan: dict[str, list[dict]] = {}
    for item, aid in zip(items, ids):
        plan.setdefault(categories.get(aid, FALLBACK), []).append(item)

    logger.info("计划：" + ", ".join(f"{name}={len(v)}" for name, v in sorted(plan.items(), key=lambda kv: -len(kv[1]))))
    if dry_run:
        logger.info("DRY_RUN=1，不写入")
        return 0

    updated = 0
    for category, group in plan.items():
        sub_key = ensure_collection(client, category, date_key)
        payload = []
        for item in group:
            data = item["data"]
            keys = list(data.get("collections") or [])
            if sub_key not in keys:
                keys.append(sub_key)
            if date_key not in keys:
                keys.append(date_key)
            data["collections"] = keys
            # 原样交回（links/library/version/meta/key/data 六个键）：
            # pyzotero 的 check_items 只对这种完整形态自动解包，平铺或 {"data":...} 都会报 Invalid keys
            payload.append(item)
        for start in range(0, len(payload), BATCH):
            chunk = payload[start : start + BATCH]
            try:
                client.update_items(chunk)
            except Exception as exc:
                logger.warning(f"update failed for {len(chunk)} item(s) in {category!r}: {exc}")
                continue
            updated += len(chunk)
            logger.info(f"{category!r}: updated {updated} so far")
            time.sleep(1)

    logger.info(f"Done. {updated}/{len(items)} item(s) now carry a category sub-collection")
    return 0


if __name__ == "__main__":
    sys.exit(main())
