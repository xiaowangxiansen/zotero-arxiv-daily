#!/usr/bin/env bash
# 把 CUSTOM_CONFIG 解析成 Hydra 真正读取的 config/custom.yaml。
#
# 支持三种情形（按顺序判断）：
#   1) CUSTOM_CONFIG 是仓库内相对路径（推荐，例：config/custom.yaml）
#   2) CUSTOM_CONFIG 是整段 YAML 正文（仓库 README 的历史用法，保持兼容）
#   3) CUSTOM_CONFIG 为空 —— 直接使用仓库里已提交的 config/custom.yaml
#
# 关键点：内容为空时绝不落盘，避免把已有配置清成 0 字节（旧 workflow 的 printf 就会这么干）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET="$REPO_ROOT/config/custom.yaml"
cd "$REPO_ROOT"

raw="${CUSTOM_CONFIG-}"
# 去掉 Windows 换行残留的 \r
value="$(printf '%s' "$raw" | tr -d '\r')"
trimmed="$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

echo "::group::CUSTOM_CONFIG 诊断"
echo "运行目录      : $REPO_ROOT"
echo "工作区 config/ :"
ls -la "$REPO_ROOT/config" || true
echo "CUSTOM_CONFIG 长度: ${#value}"
if [ "${#value}" -eq 0 ]; then
  echo "CUSTOM_CONFIG 取值: <空>"
  echo "→ 将直接使用仓库里已提交的 config/custom.yaml"
elif [[ "$value" == *$'\n'* ]]; then
  echo "CUSTOM_CONFIG 取值: <多行 YAML 正文，$(printf '%s\n' "$value" | awk 'END{print NR}') 行，正文不回显>"
else
  echo "CUSTOM_CONFIG 取值: [$trimmed]"
fi
echo "::endgroup::"

if [ -z "$trimmed" ]; then
  if [ ! -s "$TARGET" ]; then
    echo "::error::CUSTOM_CONFIG 为空，且仓库里的 config/custom.yaml 不存在或是空文件。"
    echo "二选一：① 把自定义配置提交为 config/custom.yaml；② 设置仓库变量 CUSTOM_CONFIG 指向它的路径。"
    exit 1
  fi
  MODE="仓库中已提交的 config/custom.yaml"
else
  # 先落到仓库内临时文件，确认非空再原子替换，绝不出现「写一半 / 清成 0 字节」
  tmp="$REPO_ROOT/config/.custom.yaml.tmp"
  if [[ "$value" == *$'\n'* ]]; then
    printf '%s\n' "$value" >"$tmp"
    MODE="CUSTOM_CONFIG 内联 YAML 正文"
  elif [ -f "$REPO_ROOT/$trimmed" ]; then
    cp -f "$REPO_ROOT/$trimmed" "$tmp"
    MODE="仓库内文件 $trimmed"
  elif printf '%s' "$trimmed" | grep -q ':'; then
    printf '%s\n' "$value" >"$tmp"
    MODE="CUSTOM_CONFIG 单行 YAML 正文"
  else
    echo "::error::CUSTOM_CONFIG=[$trimmed] 既不是仓库里存在的文件，也不像 YAML 正文。"
    echo "排查提示："
    echo "  - Linux runner 区分大小写，路径必须与 git 里的文件名完全一致，且用正斜杠 /"
    echo "  - 路径相对仓库根目录，例如 config/custom.yaml（不要写 D:\\... 之类的本机绝对路径）"
    echo "  - 文件必须已 commit 并 push 到本次运行的分支"
    if [ -n "$(basename "$trimmed")" ]; then
      similar="$(find "$REPO_ROOT" -name .git -prune -o -type f -iname "$(basename "$trimmed")" -print 2>/dev/null || true)"
      if [ -n "$similar" ]; then
        echo "  - 下面是全仓库中同名（忽略大小写）的文件，便于定位："
        printf '%s\n' "$similar" | sed 's/^/      /'
      fi
    fi
    exit 1
  fi

  if [ ! -s "$tmp" ]; then
    echo "::error::CUSTOM_CONFIG 解析结果为空，已放弃写入，仓库里的 config/custom.yaml 保持不变。"
    rm -f "$tmp"
    exit 1
  fi
  mv -f "$tmp" "$TARGET"
fi

echo "配置来源: $MODE"
echo "写入结果: $TARGET ($(wc -c <"$TARGET") 字节, sha256: $(sha256sum "$TARGET" | cut -d' ' -f1))"
echo "----- config/custom.yaml 开始 -----"
cat "$TARGET"
echo "----- config/custom.yaml 结束 -----"
