#!/usr/bin/env python3
"""one-hop-eval.py — 「一跳高信心命中」盲测（方案 v1 第 5 节）。只读，不改任何记忆。

测什么：**只给主索引**的情况下，模型能不能一跳指对该读哪个专题档。
这正是索引的全部职责 —— 索引不负责回答问题，只负责把人领到正确的档。

题目与标准答案来自 `build-eval-set.py`：历史上用户真问过的话 +
那次会话 CC 实际打开的第一个专题档。不是我编题也不是我判卷。

🔴 隔离（这一条错了，整个测量就没意义）：
  · 子会话 cwd 指向一个**空的临时目录** —— 否则它会自动加载那个项目的记忆，
    「只给索引」的前提当场破掉，测出来的是「模型知不知道」而不是「索引管不管用」；
  · `--tools ""` 禁掉全部工具 ⇒ 它没法自己去 Read 专题档；
  · `--no-session-persistence` ⇒ 不写历史、题与题之间不串味。

用法（A/B 对照，两次都用同一批题）：
  python3 one-hop-eval.py --eval eval.json --index <改前的 MEMORY.md> --label before --out before.json
  python3 one-hop-eval.py --eval eval.json --index <改后的 MEMORY.md> --label after  --out after.json
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import subprocess
import tempfile
import time

CLAUDE = os.environ.get("ONEHOP_CLAUDE") or os.path.expanduser("~/.local/bin/claude")  # 可换成桩，给量具自己做测试
MD_RE = re.compile(r"([\w.\-]+\.md)")
# 🔴 2026-09-13 实测：额度耗尽时 50 题全部返回「You've hit your session limit」，
#    旧版把它们当「未命中」算，报了一个 0% —— 模型根本没答题，量具却给出了分数。
#    这类输出必须记为「无效」，不进分母；无效过多整轮判废。
LIMIT_RE = re.compile(r"session limit|usage limit|rate limit|hit your limit|credit balance|"
                      r"overloaded|api error|authentication", re.I)
INVALID_RATIO = 0.10

TEMPLATE = """下面是一个项目记忆库的**目录页**（MEMORY.md）。它的作用是：看到用户的问题后，
据此判断该打开哪一个专题笔记。

<目录页>
{index}
</目录页>

用户说：「{prompt}」

要处理这件事，你会先打开 memory 目录下的哪个专题档？
只输出文件名，最多 2 个，用空格分隔，不要解释、不要任何其它文字。
目录页里没有合适的就输出 NONE。"""


def ask(index_text, prompt, workdir, model, timeout):
    body = TEMPLATE.format(index=index_text, prompt=prompt)
    try:
        r = subprocess.run(
            [CLAUDE, "-p", "--model", model, "--tools", "",
             "--strict-mcp-config", "--no-session-persistence",
             "--permission-mode", "bypassPermissions", body],
            capture_output=True, text=True, timeout=timeout, cwd=workdir,
        )
        return (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return "<TIMEOUT>"
    except Exception as e:
        return f"<ERR {e}>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", required=True)
    ap.add_argument("--index", required=True, help="要测的那一版 MEMORY.md")
    ap.add_argument("--label", required=True, help="before / after —— 只是报告里的名字")
    ap.add_argument("--out")
    ap.add_argument("--n", type=int, help="只跑前 N 题（试流程用）")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    with open(args.eval, encoding="utf-8") as f:
        items = json.load(f)["items"]
    if args.n:
        items = items[:args.n]
    with open(args.index, encoding="utf-8") as f:
        index_text = f.read()

    # 空目录 ⇒ 子会话没有项目记忆可加载
    workdir = tempfile.mkdtemp(prefix="onehop-")

    t0 = time.time()
    results = [None] * len(items)
    stop = {"why": None}                 # 一撞额度就停，别把剩下的题白烧掉

    def classify(raw):
        """有效答案只有两种形态：含 .md 文件名，或明确的 NONE。其它一律无效。"""
        if not raw or raw.startswith("<") or LIMIT_RE.search(raw):
            return False
        return bool(MD_RE.search(raw)) or raw.strip().upper().startswith("NONE")

    def run(i):
        it = items[i]
        if stop["why"]:
            raw = "<SKIPPED 前面已撞额度>"
        else:
            raw = ask(index_text, it["prompt"], workdir, args.model, args.timeout)
            if LIMIT_RE.search(raw):
                stop["why"] = raw[:80]
        guessed = MD_RE.findall(raw)[:2]
        valid = classify(raw)
        return i, {
            "prompt": it["prompt"][:120],
            "answer": it["answer"],
            "has_anchor": it.get("has_anchor", False),
            "guessed": guessed,
            "valid": valid,
            "hit": valid and it["answer"] in guessed,
            "raw": raw[:200],
        }

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, r in ex.map(run, range(len(items))):
            results[i] = r
            print(f"  [{i+1}/{len(items)}] {'✅' if r['hit'] else ('⬜' if r['valid'] else '⚠')} "
                  f"{r['answer']:42} ← {' '.join(r['guessed']) or r['raw'][:40]}")

    valid = [r for r in results if r["valid"]]
    invalid = [r for r in results if not r["valid"]]
    ok = [r for r in valid if r["hit"]]
    anchored = [r for r in valid if r["has_anchor"]]
    anchored_ok = [r for r in anchored if r["hit"]]
    measurement_ok = len(invalid) <= len(results) * INVALID_RATIO

    print(f"\n══ {args.label} ══  索引 {len(index_text)} 字符 · 用时 {time.time()-t0:.0f}s")
    if not measurement_ok:
        print(f"  🔴🔴 **测量无效**：{len(invalid)}/{len(results)} 题没拿到有效回答"
              f"（上限 {INVALID_RATIO:.0%}）。这不是命中率，别拿它做任何比较。")
        if stop["why"]:
            print(f"     原因：{stop['why']}")
    print(f"  一跳命中：{len(ok)}/{len(valid)} = {len(ok)/max(len(valid),1)*100:.0f}%"
          f"（分母只算有效回答；无效 {len(invalid)} 题不计）")
    if anchored:
        print(f"  其中「prompt 带实体锚点」的：{len(anchored_ok)}/{len(anchored)} "
              f"= {len(anchored_ok)/len(anchored)*100:.0f}%")
        rest = len(valid) - len(anchored)
        rest_ok = len(ok) - len(anchored_ok)
        if rest:
            print(f"  「泛问题」的：{rest_ok}/{rest} = {rest_ok/rest*100:.0f}%"
                  "（这类题标准答案本就带偶然性，只看前后对比）")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"label": args.label, "index": os.path.abspath(args.index),
                       "index_chars": len(index_text), "model": args.model,
                       "measured_at": time.strftime("%Y-%m-%d %H:%M"),
                       "valid_measurement": measurement_ok,
                       "valid_n": len(valid), "invalid_n": len(invalid),
                       "hit_rate": len(ok) / max(len(valid), 1),
                       "results": results}, f, ensure_ascii=False, indent=2)
        print(f"\n逐题结果 → {os.path.abspath(args.out)}")

    if not measurement_ok:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
