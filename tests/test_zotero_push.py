"""Tests for zotero_arxiv_daily.zotero_push: 分类复用与条目构造（不触网）。"""

from datetime import datetime, timedelta, timezone

from omegaconf import OmegaConf

from tests.canned_responses import make_sample_paper
from zotero_arxiv_daily.zotero_push import ZoteroPusher, paper_to_item, split_name


class FakeZotero:
    """够用的假 client：记录创建了什么，并复刻「单次最多 50 条」的限制。"""

    def __init__(self, existing_collections=None):
        self.collections_data = list(existing_collections or [])
        self.created_collections = []
        self.created_items = []
        self.items_by_collection = {}
        self._seq = 0

    def everything(self, generator):
        return generator

    def collections(self):
        return self.collections_data

    def collection_items(self, key):
        return self.items_by_collection.get(key, [])

    def create_collections(self, payloads):
        success = {}
        for i, payload in enumerate(payloads):
            self._seq += 1
            key = f"COL{self._seq}"
            self.created_collections.append({**payload, "key": key})
            self.collections_data.append({"key": key, "data": dict(payload)})
            success[str(i)] = key
        return {"success": success}

    def create_items(self, items):
        if len(items) > 50:
            raise ValueError("You may only create up to 50 items per call")
        self.created_items.extend(items)
        for item in items:
            self.items_by_collection.setdefault(item["collections"][0], []).append({"data": item})
        return {"success": {str(i): f"ITEM{i}" for i in range(len(items))}, "failed": {}}


def _config(push=None):
    return OmegaConf.create(
        {
            "zotero": {
                "user_id": "1",
                "api_key": "k",
                "push": push or {"enabled": True, "parent_collection": "arXiv Daily", "collection_name": "{date}"},
            },
            "executor": {"archive_timezone_offset": 8},
        }
    )


def _today(offset=8):
    return datetime.now(timezone(timedelta(hours=offset))).strftime("%Y-%m-%d")


def test_push_creates_parent_and_dated_child():
    client = FakeZotero()
    pusher = ZoteroPusher(_config(), client=client)

    created = pusher.push([make_sample_paper(title="A")])

    assert created == 1
    names = [c["name"] for c in client.created_collections]
    assert names == ["arXiv Daily", _today()]
    assert client.created_collections[1]["parentCollection"] == "COL1"
    assert client.created_items[0]["collections"] == ["COL2"]


def test_push_reuses_existing_collections():
    client = FakeZotero(
        existing_collections=[
            {"key": "P1", "data": {"name": "arXiv Daily", "parentCollection": False}},
            {"key": "C1", "data": {"name": _today(), "parentCollection": "P1"}},
        ]
    )
    pusher = ZoteroPusher(_config(), client=client)

    pusher.push([make_sample_paper(title="A")])

    assert client.created_collections == []  # 一个都没新建
    assert client.created_items[0]["collections"] == ["C1"]


def test_push_skips_parent_when_name_empty():
    client = FakeZotero()
    pusher = ZoteroPusher(_config({"enabled": True, "parent_collection": "", "collection_name": "{date}"}), client=client)

    pusher.push([make_sample_paper(title="A")])

    assert [c["name"] for c in client.created_collections] == [_today()]


def test_push_batches_over_50_items():
    """Zotero 单次最多 50 条：120 篇必须分成 3 批，不能一次传。"""
    client = FakeZotero()
    papers = [
        make_sample_paper(title=f"P{i}", url=f"https://arxiv.org/abs/2410.{i:05d}")
        for i in range(120)
    ]

    created = ZoteroPusher(_config(), client=client).push(papers)

    assert created == 120
    assert len(client.created_items) == 120


def test_push_skips_papers_already_in_collection():
    """同一天重跑不能重复写入。"""
    client = FakeZotero()
    pusher = ZoteroPusher(_config(), client=client)
    papers = [make_sample_paper(title="A", url="https://arxiv.org/abs/2410.99999")]

    assert pusher.push(papers) == 1
    assert pusher.push(papers) == 0
    assert len(client.created_items) == 1


def test_push_retries_after_timeout_without_duplicates():
    """超时可能已在服务端写成功：重试前要重读去重，不能写两遍。"""

    class FlakyZotero(FakeZotero):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        def create_items(self, items):
            if self.fail_once:
                self.fail_once = False
                # 模拟：服务端写完，但响应超时
                self.created_items.extend(items)
                for item in items:
                    self.items_by_collection.setdefault(item["collections"][0], []).append({"data": item})
                raise TimeoutError("The read operation timed out")
            return super().create_items(items)

    client = FlakyZotero()
    papers = [
        make_sample_paper(title=f"P{i}", url=f"https://arxiv.org/abs/2410.{i:05d}")
        for i in range(30)
    ]

    ZoteroPusher(_config(), client=client).push(papers)

    stored = client.items_by_collection["COL2"]
    assert len(stored) == 30, "30 篇应该全都在，且不重复"
    assert len({item["data"]["archiveID"] for item in stored}) == 30


def test_paper_to_item_fields():
    paper = make_sample_paper(title="Great Paper", url="https://arxiv.org/abs/2410.12345")
    item = paper_to_item(paper, "COLL")

    assert item["itemType"] == "preprint"
    assert item["repository"] == "arXiv"
    assert item["archiveID"] == "2410.12345"
    assert item["collections"] == ["COLL"]
    assert {"tag": "auto-arxiv-daily"} in item["tags"]
    assert item["creators"][0] == {"creatorType": "author", "firstName": "Author", "lastName": "A"}


def test_split_name():
    assert split_name("Ada Lovelace") == {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
    assert split_name("Yann Le Cun") == {"creatorType": "author", "firstName": "Yann Le", "lastName": "Cun"}
    assert split_name("OpenAI") == {"creatorType": "author", "name": "OpenAI"}
