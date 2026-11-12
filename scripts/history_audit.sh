#!/usr/bin/env bash
# git 历史安全审计:密钥模式 / 内网地址 / 历史大对象
cd "$(dirname "$0")/.." || exit 1
# 本地内部词表(不入库):每行一个 ERE,不能有空行(空模式会匹配所有行)
VOCAB_FILE="scripts/.internal_vocab"

echo "=== 1. 密钥/凭证模式扫描(全部历史) ==="
git log --all -p | grep -inE "BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20}|ghp_[A-Za-z0-9]{30}|hf_[A-Za-z0-9]{30}|xox[baprs]-[A-Za-z0-9-]{10,}" | head -15
echo "--- 疑似 key=value 凭证(人工复核) ---"
git log --all -p | grep -inE "(api_key|apikey|secret|password|passwd|token)['\"]?\s*[=:]\s*['\"][^'\"\s]{12,}" | grep -viE "test|fake|mock|example|placeholder|EMPTY|your[_-]|<|\$\{" | head -15
echo "(空输出 = 无命中)"

echo
echo "=== 2. 内网地址/内部主机名(全部历史,本地词表) ==="
if [ -f "$VOCAB_FILE" ]; then
  git log --all -p | grep -cE -f "$VOCAB_FILE" || true
  echo "(0 = 无命中)"
else
  echo "(跳过：本地词表 $VOCAB_FILE 不存在)"
fi

echo
echo "=== 3. 历史大对象(>1MB, TOP10) ==="
git rev-list --objects --all \
  | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' \
  | awk '$1=="blob" && $3 > 1000000 {printf "%.1fMB  %s\n", $3/1048576, $4}' \
  | sort -rn | head -10
echo "(空输出 = 无大对象)"

echo
echo "=== 4. 当前 HEAD 中被跟踪的 >5MB 文件 ==="
git ls-files -z | xargs -0 -I{} du -k "{}" 2>/dev/null | awk '$1 > 5120 {printf "%.1fMB  %s\n", $1/1024, $2}' | sort -rn | head -8
echo "AUDIT DONE"
