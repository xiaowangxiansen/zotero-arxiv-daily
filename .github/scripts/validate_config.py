#!/usr/bin/env python3
"""跑主流程之前，先按 Hydra 的真实合成结果校验配置。

为什么需要它：旧 workflow 用 `printf ... > config/custom.yaml` 写配置，一旦
CUSTOM_CONFIG 为空就会把文件清成 0 字节，主流程要跑到 `Executor.__init__`
才抛 `MissingMandatoryValue: executor.source`，日志里完全看不出是"配置没加载"。

校验四件事：
1. 配置里引用的 `${oc.env:XXX}` 变量是否都已注入且非空（secret 漏配会在这里报）；
2. 必填字段是否真的被覆盖（还是残留 base.yaml 的 `???`）；
3. executor.source / executor.reranker 是否是程序已注册的实现名；
4. 打印最终生效的摘要（敏感值只打长度或掩码），便于事后对账。

任何一项不通过 → 退出码 1。
"""
from __future__ import annotations

import os
import re
import sys

from hydra import compose, initialize_config_dir
from omegaconf import OmegaConf

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "config"))
ENV_REF_RE = re.compile(r"\$\{oc\.env:\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:,([^}]*))?\}")

cfg = None  # 由 main() 在 Hydra 合成后填充，供 read()/require() 使用
problems: list[str] = []
notes: list[str] = []


def scan_env_refs() -> dict[str, list[str]]:
    """收集配置文件中所有 oc.env 引用以及出现位置（file:line）。"""
    refs: dict[str, list[str]] = {}
    for name in ("base.yaml", "custom.yaml"):
        path = os.path.join(CONFIG_DIR, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for match in ENV_REF_RE.finditer(line):
                    var, default = match.group(1), match.group(2)
                    if default is not None:  # 有默认值，不强制要求
                        continue
                    refs.setdefault(var, []).append(f"{name}:{lineno}")
    return refs


def check_env_refs(ignored: set[str] | None = None) -> None:
    ignored = ignored or set()
    refs = scan_env_refs()
    if not refs:
        notes.append("配置里没有强制要求的 ${oc.env:...} 引用")
        return
    for var, where in sorted(refs.items()):
        if var in ignored:
            continue
        value = os.environ.get(var)
        if value is None or value.strip() == "":
            problems.append(
                f"环境变量 {var} 未注入或为空，但配置在 {', '.join(where)} 里引用了它"
                "（检查仓库 Settings → Secrets and variables → Actions 是否配了同名的 secret）"
            )


def read(path: str):
    """返回 (状态, 值)：missing=键不存在, unset=仍是 ???, ok=正常。"""
    sentinel = object()
    try:
        node = OmegaConf.select(cfg, path, default=sentinel)
    except Exception as exc:  # 插值解析失败等
        return "error", f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    if node is sentinel:
        return "missing", None
    try:
        if OmegaConf.is_missing(cfg, path):
            return "unset", None
    except Exception:
        pass
    if OmegaConf.is_dict(node) or OmegaConf.is_list(node):
        node = OmegaConf.to_container(node, resolve=True)
    return "ok", node


def require(path: str):
    state, value = read(path)
    if state == "missing":
        problems.append(f"{path} 不存在（配置没有被合并进来？）")
        return None
    if state == "unset":
        problems.append(f"{path} 仍是 base.yaml 里的 `???`，没有被任何自定义配置覆盖")
        return None
    if state == "error":
        # 环境变量缺失已经在 check_env_refs 里精确报过（含 file:line），这里不重复刷屏
        if "Environment variable" not in str(value):
            problems.append(f"{path} 解析失败: {value}")
        return None
    if value is None or value == "" or value == [] or value == {}:
        problems.append(f"{path} 为空值（{value!r}）")
        return None
    return value


def registry_names(module: str, attr: str) -> set[str] | None:
    try:
        import importlib

        mod = importlib.import_module(module)
        return set(getattr(mod, attr).keys())
    except Exception as exc:  # 依赖没装好时不让校验本身崩掉
        notes.append(f"跳过实现名校验（导入 {module} 失败: {type(exc).__name__}）")
        return None


def mask(value: str) -> str:
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * max(len(value) - 4, 1)}{value[-2:]} (长度 {len(value)})"


def mask_email(value: str) -> str:
    value = str(value)
    if "@" not in value:
        return mask(value)
    local, domain = value.split("@", 1)
    return f"{local[:1]}{'*' * max(len(local) - 1, 1)}@{domain}"


def main() -> int:
    global cfg

    with initialize_config_dir(version_base=None, config_dir=CONFIG_DIR):
        cfg = compose(config_name="default")

    # llm.enabled 缺省为 true，保持与旧配置的兼容
    llm_enabled = read("llm.enabled")[1]
    llm_enabled = True if llm_enabled is None else bool(llm_enabled)
    # 关掉 LLM 时，OPENAI_* 就不再是必需项，别拿它卡住整个流程
    check_env_refs(ignored=set() if llm_enabled else {"OPENAI_API_KEY", "OPENAI_API_BASE"})

    sources = require("executor.source")
    reranker = require("executor.reranker")
    require("zotero.user_id")
    require("zotero.api_key")
    require("email.sender")
    require("email.receiver")
    require("email.smtp_server")
    require("email.smtp_port")
    require("email.sender_password")
    if llm_enabled:
        require("llm.api.key")
        require("llm.api.base_url")
        require("llm.generation_kwargs.model")

    if isinstance(sources, str):  # 写成字符串是常见错误
        problems.append(f"executor.source 必须是列表，当前是字符串 {sources!r}，应写成 ['{sources}']")
        sources = [sources]

    categories = read("source.arxiv.category")[1]
    if sources:
        valid = registry_names("zotero_arxiv_daily.retriever.base", "registered_retrievers")
        if valid is not None:
            unknown = [s for s in sources if s not in valid]
            if unknown:
                problems.append(f"executor.source 含未实现的来源 {unknown}，可用: {sorted(valid)}")
        if "arxiv" in sources:
            categories = require("source.arxiv.category")
            if isinstance(categories, str):
                problems.append(
                    f"source.arxiv.category 必须是列表，当前是字符串 {categories!r}，应写成 ['{categories}']"
                )

    if isinstance(reranker, str):
        valid_r = registry_names("zotero_arxiv_daily.reranker.base", "registered_rerankers")
        if valid_r is not None and reranker not in valid_r:
            problems.append(f"executor.reranker={reranker!r} 未实现，可用: {sorted(valid_r)}")
        if reranker == "local":
            require("reranker.local.model")
        elif reranker == "api":
            require("reranker.api.key")
            require("reranker.api.base_url")
            require("reranker.api.model")

    for key in ("zotero.include_path", "zotero.ignore_path"):
        state, value = read(key)
        if state == "ok" and value is not None and not isinstance(value, list):
            problems.append(f"{key} 必须是列表或 null，当前是 {type(value).__name__}: {value!r}")

    port = read("email.smtp_port")[1]
    if port is not None:
        try:
            int(port)
        except (TypeError, ValueError):
            problems.append(f"email.smtp_port 必须是整数，当前 {port!r}")

    if problems:
        print("配置校验未通过：\n")
        for item in problems:
            print(f"::error::{item}")
        hints = []
        if any("未注入或为空" in p for p in problems):
            hints.append(
                "· secret 缺失：Settings → Secrets and variables → Actions → Secrets 里补上同名 secret（大小写完全一致）"
            )
        if any("executor.source" in p or "source.arxiv.category" in p for p in problems):
            hints.append("· 配置没加载：确认自定义 yaml 里写了 executor.source（如 ['arxiv']）与 source.arxiv.category")
            hints.append(
                "· 让 workflow 找到它：提交为 config/custom.yaml（此时 CUSTOM_CONFIG 可留空），"
                "或把 CUSTOM_CONFIG 设为该文件的仓库内相对路径"
            )
        if any("必须是列表" in p for p in problems):
            hints.append("· 列表字段要写成 [a, b] 形式，不能写成单个字符串")
        if hints:
            print("\n如何修：")
            for hint in hints:
                print(f"  {hint}")
        return 1

    print("配置校验通过，最终生效的配置摘要：")
    print(f"  来源 executor.source      : {list(sources)}")
    print(f"  arXiv 分类                : {list(categories) if categories else None}")
    print(f"  cross-list                : {read('source.arxiv.include_cross_list')[1]}")
    print(f"  重排器 executor.reranker  : {reranker}")
    if reranker == "local":
        print(f"  本地模型                  : {read('reranker.local.model')[1]}")
    print(f"  Zotero user_id            : {mask(read('zotero.user_id')[1])}")
    print(f"  Zotero include_path       : {read('zotero.include_path')[1]}")
    print(f"  发件人                    : {mask_email(read('email.sender')[1])}")
    print(f"  收件人                    : {mask_email(read('email.receiver')[1])}")
    print(f"  SMTP                      : {read('email.smtp_server')[1]}:{read('email.smtp_port')[1]}")
    if llm_enabled:
        print(f"  LLM base_url              : {read('llm.api.base_url')[1]}")
        print(f"  LLM model                 : {read('llm.generation_kwargs.model')[1]}")
        print(f"  TL;DR 语言                : {read('llm.language')[1]}")
    else:
        print("  大模型                    : 已关闭（llm.enabled=false，不生成 TLDR/机构，不下载 PDF 全文）")
    print(f"  单封邮件最多论文数        : {read('executor.max_paper_num')[1]}")
    for note in notes:
        print(f"  · {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
