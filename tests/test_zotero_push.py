"""Tests for zotero_arxiv_daily.zotero_push: 分类复用与条目构造（不触网）。"""

from datetime import datetime, timedelta, timezone

from omegaconf import OmegaConf

from tests.canned_responses import make_sample_paper
from zotero_arxiv_daily.zotero_push import ZoteroPusher, paper_to_item, split_name


class FakeZotero:
    """够用的假 client：记录创建了什么。"""

    def __init__(self, existing_collections=None):
        self.collections_data = list(existing_collections or [])
        self.created_collections = []
        self.created_items = []
        self._seq = 0

    def everything(self, generator):
        return generator

    def collections(self):
        return self.collections_data

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
        self.created_items.extend(items)
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
