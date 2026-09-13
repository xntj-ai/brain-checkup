#!/usr/bin/env bash
# weekly-report.sh — 每周一次的只读记忆体检快照（方案 v1 第 3 步的数据来源）
#
# 为什么要它：第 3 步「常驻判据按条撤」的验收标准是
# **「连续 30 天『又踩且当次未读档』计数趋零」** —— 那是个时间序列指标，
# 不跑够 30 天就没有任何数据可判。这个脚本让数据从今天开始积累。
#
# 三份只读快照，全部带日期落盘，永不覆盖历史：
#   · index-reconcile  —— 索引对账：死链 / 孤儿 / 日志化存量
#   · again-ledger     —— 「又踩」台账：哪些判据反复失手、现有触达是什么
#   · reach-rate       —— 触达率：该读那个档的时候到底读没读
#
# 只读，不改任何记忆文件。跑一次约 1–3 分钟（reach-rate 要扫 jsonl）。
#
# 用法：
#   bash scripts/weekly-report.sh                    # 默认落 memory-curator/_audits/weekly/
#   bash scripts/weekly-report.sh /some/other/dir
#
# 想让它自动跑：挂 cron（每周一早八点）——
#   0 8 * * 1 cd ~/projects/brain-checkup && bash scripts/weekly-report.sh >/dev/null 2>&1
# 🔴 本脚本不替你挂 cron：新增常驻任务该由人决定。

set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT_BASE="${1:-$HOME/projects/memory-curator/_audits/weekly}"
DAY="$(date +%Y-%m-%d)"
OUT="$OUT_BASE/$DAY"
mkdir -p "$OUT" || exit 1

BUCKETS="-home-ccuser-projects-cruna-ai -home-ccuser-projects-cc-service"

echo "══ 记忆周报 $DAY ══"

# 1) 索引对账（两个主桶）
DIRS=""
for b in $BUCKETS; do DIRS="$DIRS $HOME/.claude/projects/$b/memory"; done
# shellcheck disable=SC2086
python3 "$HERE/scripts/index-reconcile.py" $DIRS > "$OUT/index-reconcile.md" 2>&1 \
  && echo "  ✅ 索引对账 → $OUT/index-reconcile.md" \
  || echo "  🔴 索引对账失败"

# 2) 「又踩」台账（全库；它的头条数字就是主指标的分母）
python3 "$HERE/scripts/again-ledger.py" --json "$OUT/again-ledger.json" \
        > "$OUT/again-ledger.md" 2>&1 \
  && echo "  ✅ 又踩台账 → $OUT/again-ledger.md" \
  || echo "  🔴 又踩台账失败"

# 3) 触达率（近 30 天，与基线同口径 —— 口径变了就不能跟历史比）
python3 "$HERE/scripts/reach-rate.py" --days 30 --json "$OUT/reach-rate.json" \
        > "$OUT/reach-rate.md" 2>&1 \
  && echo "  ✅ 触达率 → $OUT/reach-rate.md" \
  || echo "  🔴 触达率失败"

# 4) 趋势：把历次的关键数字并成一张表，人一眼看得出有没有在变好
python3 - "$OUT_BASE" > "$OUT_BASE/trend.md" 2>/dev/null <<'PY'
import glob, json, os, sys
base = sys.argv[1]
rows = []
for d in sorted(glob.glob(os.path.join(base, "20*-*-*"))):
    day = os.path.basename(d)
    again = reach = hit = "—"
    try:
        a = json.load(open(os.path.join(d, "again-ledger.json"), encoding="utf-8"))
        linked = sum(1 for r in a if r.get("in_index"))
        again = f"{len(a)}（{linked} 有链）"
    except Exception:
        pass
    try:
        r = json.load(open(os.path.join(d, "reach-rate.json"), encoding="utf-8"))
        rr = r.get("rows", [])
        ok = sum(1 for x in rr if x.get("reached"))
        reach = f"{ok}/{len(rr)}" if rr else "无样本"
    except Exception:
        pass
    rows.append((day, again, reach))

print("# 记忆体检趋势\n")
print("> 主指标是**触达率**：prompt 命中「又踩」触发词的会话里，那个档当次被打开的比例。")
print("> 方案第 3 步的验收标准是「连续 30 天『又踩且当次未读档』趋零」——"
      "所以这张表至少要攒满 4–5 行才有意义，别拿两行就下结论。\n")
print("| 日期 | 又踩处数 | 触达率 |")
print("|---|---|---|")
for day, a, r in rows:
    print(f"| {day} | {a} | {r} |")
PY
echo "  ✅ 趋势表 → $OUT_BASE/trend.md"
echo
echo "快照目录：$OUT"
