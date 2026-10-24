from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from pathlib import Path
from .utils import glob_match
from .retriever import get_retriever_cls
from .protocol import CorpusPaper
from .archive import save_daily_papers
from .zotero_push import ZoteroPusher
import random
import time
from datetime import datetime
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from openai import OpenAI
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[2]


def timed(label: str, fn):
    """跑 fn 并把耗时打进日志，用于定位整条链路卡在哪一段。"""
    started = time.perf_counter()
    try:
        return fn()
    finally:
        logger.info(f"[timing] {label}: {time.perf_counter() - started:.1f}s")


def normalize_path_patterns(patterns: list[str] | ListConfig | None, config_key: str) -> list[str] | None:
    if patterns is None:
        return None

    if not isinstance(patterns, (list, ListConfig)):
        raise TypeError(
            f"config.zotero.{config_key} must be a list of glob patterns or null, "
            'for example ["2026/survey/**"]. Single strings are not supported.'
        )

    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError(f"config.zotero.{config_key} must contain only glob pattern strings.")

    return list(patterns)


class Executor:
    def __init__(self, config:DictConfig):
        self.config = config
        self.include_path_patterns = normalize_path_patterns(config.zotero.include_path, "include_path")
        self.ignore_path_patterns = normalize_path_patterns(config.zotero.ignore_path, "ignore_path")
        self.retrievers = {
            source: get_retriever_cls(source)(config) for source in config.executor.source
        }
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        # llm.enabled=false 时完全不碰 LLM：不建客户端（因此也不要求 api.key），不生成 TLDR/机构
        self.llm_enabled = bool(config.llm.get("enabled", True))
        self.openai_client = (
            OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)
            if self.llm_enabled
            else None
        )
    def fetch_zotero_corpus(self) -> list[CorpusPaper]:
        logger.info("Fetching zotero corpus")
        zot = zotero.Zotero(self.config.zotero.user_id, 'user', self.config.zotero.api_key)
        collections = zot.everything(zot.collections())
        collections = {c['key']:c for c in collections}
        corpus = zot.everything(zot.items(itemType='conferencePaper || journalArticle || preprint'))
        corpus = [c for c in corpus if c['data']['abstractNote'] != '']
        def get_collection_path(col_key:str) -> str:
            if p := collections[col_key]['data']['parentCollection']:
                return get_collection_path(p) + '/' + collections[col_key]['data']['name']
            else:
                return collections[col_key]['data']['name']
        for c in corpus:
            paths = [get_collection_path(col) for col in c['data']['collections']]
            c['paths'] = paths
        logger.info(f"Fetched {len(corpus)} zotero papers")
        return [CorpusPaper(
            title=c['data']['title'],
            abstract=c['data']['abstractNote'],
            added_date=datetime.strptime(c['data']['dateAdded'], '%Y-%m-%dT%H:%M:%SZ'),
            paths=c['paths']
        ) for c in corpus]
    
    def filter_corpus(self, corpus:list[CorpusPaper]) -> list[CorpusPaper]:
        if self.include_path_patterns:
            logger.info(f"Selecting zotero papers matching include_path: {self.include_path_patterns}")
            corpus = [
                c for c in corpus
                if any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.include_path_patterns
                )
            ]
        if self.ignore_path_patterns:
            logger.info(f"Excluding zotero papers matching ignore_path: {self.ignore_path_patterns}")
            corpus = [
                c for c in corpus
                if not any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.ignore_path_patterns
                )
            ]
        if self.include_path_patterns or self.ignore_path_patterns:
            samples = random.sample(corpus, min(5, len(corpus)))
            samples = '\n'.join([c.title + ' - ' + '\n'.join(c.paths) for c in samples])
            logger.info(f"Selected {len(corpus)} zotero papers:\n{samples}\n...")
        return corpus

    
    def drop_already_pushed(self, papers: list) -> list:
        """剔除库里已存在的论文（按 arXiv ID / url），避免跨天重复推送。

        只在 push.enabled 为真时生效：否则读取不到 key，无法判断「已有」。
        读取失败时保守地原样返回 —— 宁可多推一次，也不要因为读库失败把当天空掉。
        """
        push_conf = self.config.zotero.get("push") or {}
        if not bool(push_conf.get("enabled", False)):
            return papers
        try:
            known = ZoteroPusher(self.config).known_archive_ids()
        except Exception as exc:
            logger.warning(f"Skip cross-day dedupe (cannot read library): {exc}")
            return papers
        if not known:
            logger.warning("Cross-day dedupe got an empty known-set; paper list left unchanged")
            return papers
        kept = [
            p for p in papers
            if (getattr(p, "url", "") or "").rstrip("/").rsplit("/", 1)[-1] not in known
            and (getattr(p, "url", "") or "").rstrip("/") not in known
        ]
        dropped = len(papers) - len(kept)
        if dropped:
            logger.info(f"Dropped {dropped} paper(s) already in library (cross-day dedupe), {len(kept)} left")
        return kept

    def run(self):
        corpus = timed("fetch zotero corpus", self.fetch_zotero_corpus)
        corpus = timed("filter corpus", lambda: self.filter_corpus(corpus))
        if len(corpus) == 0:
            if not self.config.executor.get("allow_empty_corpus", False):
                logger.error(f"No zotero papers found. Please check your zotero settings:\n{self.config.zotero}")
                return
            logger.warning(
                "Zotero corpus is empty: personalized ranking is skipped and papers are sent in retrieval order. "
                "Set executor.allow_empty_corpus=false once your library has papers."
            )
        all_papers = []
        for source, retriever in self.retrievers.items():
            logger.info(f"Retrieving {source} papers...")
            papers = timed(f"retrieve {source}", retriever.retrieve_papers)
            if len(papers) == 0:
                logger.info(f"No {source} papers found")
                continue
            logger.info(f"Retrieved {len(papers)} {source} papers")
            all_papers.extend(papers)
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")
        # 先剔除「库里已经有」的论文，再排序和截断。
        # 顺序很重要：若先截断到 max_paper_num=30 再交给 push 去重，
        # 名额会被旧论文占满，实际入新的只有几篇，用户看到的就是「翻来覆去同样文章」。
        all_papers = timed("drop already-pushed", lambda: self.drop_already_pushed(all_papers))
        reranked_papers = []
        if len(all_papers) > 0:
            if len(corpus) > 0:
                logger.info("Reranking papers...")
                reranked_papers = timed(
                    f"rerank {len(all_papers)}x{len(corpus)}",
                    lambda: self.reranker.rerank(all_papers, corpus),
                )
            else:
                reranked_papers = list(all_papers)
            reranked_papers = reranked_papers[:self.config.executor.max_paper_num]
            if self.llm_enabled:
                logger.info("Generating TLDR, abstract translation and affiliations...")
                for p in tqdm(reranked_papers):
                    p.generate_tldr(self.openai_client, self.config.llm)
                    if bool(self.config.llm.get("translate_abstract", True)):
                        p.generate_abstract_zh(self.openai_client, self.config.llm)
                    p.generate_affiliations(self.openai_client, self.config.llm)
            else:
                logger.info("llm.enabled=false: skipping TLDR and affiliations")
        elif not self.config.executor.send_empty:
            logger.info("No new papers found. No email will be sent.")
            return
        if reranked_papers:
            timed("archive", lambda: self.archive_papers(reranked_papers))
            timed("zotero push", lambda: self.push_to_zotero(reranked_papers))
        logger.info("Sending email...")
        email_content = timed("render email", lambda: render_email(reranked_papers))
        timed("send email", lambda: send_email(self.config, email_content))
        logger.info("Email sent successfully")

    def archive_papers(self, papers: list) -> None:
        """把当天论文按日期落到 executor.archive_dir（默认 papers/），失败不影响发信。"""
        archive_dir = str(self.config.executor.get("archive_dir", "") or "").strip()
        if not archive_dir:
            return
        root = Path(archive_dir)
        if not root.is_absolute():
            root = REPO_ROOT / root
        offset = int(self.config.executor.get("archive_timezone_offset", 8))
        try:
            out_dir = save_daily_papers(papers, root, offset)
            logger.info(f"Archived {len(papers)} papers to {out_dir}")
        except Exception as exc:
            logger.warning(f"Failed to archive papers to {root}: {exc}")

    def push_to_zotero(self, papers: list) -> None:
        """把当天论文写进 Zotero 分类（需要 key 有写权限），失败不影响发信。"""
        push_conf = self.config.zotero.get("push") or {}
        if not bool(push_conf.get("enabled", False)):
            return
        try:
            ZoteroPusher(self.config).push(papers)
        except Exception as exc:
            logger.warning(f"Failed to push papers to zotero: {exc}")
