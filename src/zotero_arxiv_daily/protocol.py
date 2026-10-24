from dataclasses import dataclass, field
from typing import Optional, TypeVar
from datetime import datetime
import re
import tiktoken
from openai import OpenAI
from loguru import logger
import json
RawPaperItem = TypeVar('RawPaperItem')


def _request_llm(openai_client: OpenAI, llm_params: dict, messages: list[dict]) -> str:
    api_mode = llm_params.get("api_mode", "chat_completion")
    generation_kwargs = dict(llm_params.get("generation_kwargs", {}))

    if api_mode == "chat_completion":
        response = openai_client.chat.completions.create(
            messages=messages,
            **generation_kwargs,
        )
        return response.choices[0].message.content

    if api_mode == "response":
        max_tokens = generation_kwargs.pop("max_tokens", None)
        if max_tokens is not None and "max_output_tokens" not in generation_kwargs:
            generation_kwargs["max_output_tokens"] = max_tokens
        response = openai_client.responses.create(
            input=messages,
            **generation_kwargs,
        )
        return response.output_text

    raise ValueError(
        f"Unsupported llm.api_mode: {api_mode}. "
        "Expected 'chat_completion' or 'response'."
    )


@dataclass
class Paper:
    source: str
    title: str
    authors: list[str]
    abstract: str
    url: str
    pdf_url: Optional[str] = None
    full_text: Optional[str] = None
    tldr: Optional[str] = None
    abstract_zh: Optional[str] = None
    affiliations: Optional[list[str]] = None
    score: Optional[float] = None
    primary_category: Optional[str] = None
    categories: list[str] = field(default_factory=list)

    def _generate_tldr_with_llm(self, openai_client:OpenAI,llm_params:dict) -> str:
        lang = llm_params.get('language', 'English')
        prompt = f"Given the following information of a paper, generate a one-sentence TLDR summary in {lang}:\n\n"
        if self.title:
            prompt += f"Title:\n {self.title}\n\n"

        if self.abstract:
            prompt += f"Abstract: {self.abstract}\n\n"

        if self.full_text:
            prompt += f"Preview of main content:\n {self.full_text}\n\n"

        if not self.full_text and not self.abstract:
            logger.warning(f"Neither full text nor abstract is provided for {self.url}")
            return "Failed to generate TLDR. Neither full text nor abstract is provided"
        
        # use gpt-4o tokenizer for estimation
        enc = tiktoken.encoding_for_model("gpt-4o")
        prompt_tokens = enc.encode(prompt)
        prompt_tokens = prompt_tokens[:4000]  # truncate to 4000 tokens
        prompt = enc.decode(prompt_tokens)
        
        tldr = _request_llm(
            openai_client,
            llm_params,
            [
                {
                    "role": "system",
                    "content": f"You are an assistant who perfectly summarizes scientific paper, and gives the core idea of the paper to the user. Your answer should be in {lang}.",
                },
                {"role": "user", "content": prompt},
            ],
        )
        return tldr
    
    def generate_tldr(self, openai_client:OpenAI,llm_params:dict) -> str:
        try:
            tldr = self._generate_tldr_with_llm(openai_client,llm_params)
            self.tldr = tldr
            return tldr
        except Exception as e:
            logger.warning(f"Failed to generate tldr of {self.url}: {e}")
            tldr = self.abstract
            self.tldr = tldr
            return tldr

    def _translate_abstract(self, openai_client:OpenAI, llm_params:dict) -> Optional[str]:
        """把英文摘要翻译成 llm.target_lang（默认中文）。失败返回 None，不抛异常。"""
        abstract = (self.abstract or "").strip()
        if not abstract:
            return None

        lang = llm_params.get('target_lang', 'Chinese')
        # 翻译任务的温度要压低，否则 LLM 会自己发挥；同时限制长度，避免把长摘要卷成论文复述
        params = dict(llm_params)
        params['generation_kwargs'] = dict(params.get('generation_kwargs', {}))
        # 摘要常常几百词，max_tokens 太大会让免费额度烧得飞快，这里给一个够用的上限
        params['generation_kwargs']['max_tokens'] = 1200

        prompt = (
            f"Translate the abstract of an academic paper into {lang}. "
            "Output ONLY the translated abstract, no preamble.\n\n"
            f"Abstract: {abstract}"
        )
        translated = _request_llm(
            openai_client,
            params,
            [
                {
                    "role": "system",
                    "content": (
                        "You translate academic paper abstracts faithfully and concisely. "
                        f"Preserve technical terms, numbers, citations and metric names. "
                        f"Output only the translation in {lang}."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        # 有的模型会在前面加说明：去掉空套路前缀，避免邮件里出现「以下是翻译：」
        if translated:
            translated = translated.strip()
            cleaned = re.sub(
                r'^(Here is the translation|Translation|译文|翻译)[:：]?\s*',
                '',
                translated,
                flags=re.IGNORECASE,
            )
            return cleaned.strip() or translated
        return None

    def generate_abstract_zh(self, openai_client:OpenAI, llm_params:dict) -> Optional[str]:
        try:
            translated = self._translate_abstract(openai_client, llm_params)
            self.abstract_zh = translated
            return translated
        except Exception as e:
            logger.warning(f"Failed to translate abstract of {self.url}: {e}")
            self.abstract_zh = None
            return None

    def _generate_affiliations_with_llm(self, openai_client:OpenAI,llm_params:dict) -> Optional[list[str]]:
        if self.full_text is not None:
            prompt = f"Given the beginning of a paper, extract the affiliations of the authors in a python list format, which is sorted by the author order. If there is no affiliation found, return an empty list '[]':\n\n{self.full_text}"
            # use gpt-4o tokenizer for estimation
            enc = tiktoken.encoding_for_model("gpt-4o")
            prompt_tokens = enc.encode(prompt)
            prompt_tokens = prompt_tokens[:2000]  # truncate to 2000 tokens
            prompt = enc.decode(prompt_tokens)
            affiliations = _request_llm(
                openai_client,
                llm_params,
                [
                    {
                        "role": "system",
                        "content": "You are an assistant who perfectly extracts affiliations of authors from a paper. You should return a python list of affiliations sorted by the author order, like [\"TsingHua University\",\"Peking University\"]. If an affiliation is consisted of multi-level affiliations, like 'Department of Computer Science, TsingHua University', you should return the top-level affiliation 'TsingHua University' only. Do not contain duplicated affiliations. If there is no affiliation found, you should return an empty list [ ]. You should only return the final list of affiliations, and do not return any intermediate results.",
                    },
                    {"role": "user", "content": prompt},
                ],
            )

            affiliations = re.search(r'\[.*?\]', affiliations, flags=re.DOTALL).group(0)
            affiliations = json.loads(affiliations)
            affiliations = list(set(affiliations))
            affiliations = [str(a) for a in affiliations]

            return affiliations
    
    def generate_affiliations(self, openai_client:OpenAI,llm_params:dict) -> Optional[list[str]]:
        try:
            affiliations = self._generate_affiliations_with_llm(openai_client,llm_params)
            self.affiliations = affiliations
            return affiliations
        except Exception as e:
            logger.warning(f"Failed to generate affiliations of {self.url}: {e}")
            self.affiliations = None
            return None
@dataclass
class CorpusPaper:
    title: str
    abstract: str
    added_date: datetime
    paths: list[str]