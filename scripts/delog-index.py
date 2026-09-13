#!/usr/bin/env python3
"""delog-index.py — 主索引去日志化（方案 v1 第 2 步）。默认干跑，--apply 才写。

只做**保守版**：删三类日志 token，其余一个字不动。
  ① 状态标 ✅ 🆕
  ② 反引号包裹的 commit hash
  ③ 日期戳（YYYY-MM-DD / MM-DD）
删完清理遗留的空括号与悬挂标点。

🔴 为什么不整段删论证（那才是大头，实测三类 token 只占 3.0%）：
  索引行的实际形态是判据与日志**混在同一个括号里**，例如
    （`183b2881`；🔴 商标仍未检索，用户知情）
  hash 是日志，分号后是判据。按括号整删会把判据一起删掉，
  而删钩子是**不可逆的信息损失**（「删钩子词＝那个症状再也勾不出来」，spec 里用户 08-13 定的）。

  正确做法是让数据说话：先做保守版 → 用 `one-hop-eval.py` 同一批题复测 →
  · 命中率不退步 ⇒ 这些日志确实不承担检索职责，可以再往前走一步；
  · 命中率退步   ⇒ 日志有检索价值，停手。
  这正是方案第 5 节设计 A/B 的本意：**不靠推演决定改多狠**。

守恒：被删的每个 hash / 日期，检查它在该行所链的专题档里是否还找得到。
找不到的会单独列出 —— 那才是真正会丢信息的，需要先补进专题档再删。
（索引本身在 git 里，历史永远可追溯，所以守恒的底线不是「信息消失」而是「可达性下降」。）
"""

import argparse
import os
import re
import shutil
import sys

# 🔴 判据**从闸里导入**，不自己写第二份。
#    原来这里的 HASH_RE 要求反引号包裹，而闸四的不要求 ⇒ `HEAD 009ee98` 这种裸 hash
#    delog 不删也不给豁免，闸四却拦，整桶写不进去。两份正则必然漂移。
def _load_gate():
    import importlib.machinery
    import importlib.util
    hp = os.path.expanduser("~/.claude/hooks/guard-memory-landing.py")
    spec = importlib.util.spec_from_file_location(
        "gate", hp, loader=importlib.machinery.SourceFileLoader("gate", hp))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HASH_RE, mod.DATE_RE, mod.FLAG_RE


HASH_RE, DATE_RE, FLAG_RE = _load_gate()
LINK_RE = re.compile(r"\]\(([^)]+\.md)\)")
# 链接目标里的日期是文件名的一部分，不是日志 ⇒ 判之前先挡住
LINKTARGET_RE = re.compile(r"\]\([^)]*\)")

CLEANUP = [
    (re.compile(r"（\s*[；;，,、]\s*"), "（"),      # 删掉开头 token 后留下的悬挂标点
    (re.compile(r"[；;，,、]\s*）"), "）"),
    (re.compile(r"（\s*）"), ""),                   # 整个括号空了
    (re.compile(r"\(\s*\)"), ""),
    (re.compile(r"（\s+"), "（"),
    (re.compile(r"\s+）"), "）"),
    (re.compile(r" {2,}"), " "),
    (re.compile(r"·\s*·"), "·"),
    # ⚠ 曾有两条 `**\s+`→`**` 的清理规则，把原有的 `🔴🔴 **[标题]` 压成 `🔴🔴**[标题]`，
    #    破坏了 markdown 排版 —— 而删 ✅🆕 后面紧跟的是 🔴🔴，本来就不会产生多余空格。
    #    清理规则只该清**自己删出来的**空缺，不该动原有结构。已移除。
]


def delog_line(line, keep=()):
    """返回 (新行, 被删的 token 列表)。链接目标整段保护。

    `keep` 里的 token 原样留下 —— 守恒失败（专题档里找不到）的不删，
    否则那条信息的可达性就只剩 git 历史了。宁可少省几个字符。
    """
    shields = []

    def shield(m):
        shields.append(m.group(0))
        return f"\x00{len(shields)-1}\x00"

    work = LINKTARGET_RE.sub(shield, line)

    removed = []

    def maybe_drop(m):
        tok = m.group(0)
        if tok.strip("`") in keep:
            return tok
        removed.append(tok)
        return ""

    for rx in (HASH_RE, DATE_RE, FLAG_RE):
        work = rx.sub(maybe_drop, work)

    for rx, rep in CLEANUP:
        work = rx.sub(rep, work)

    work = re.sub(r"\x00(\d+)\x00", lambda m: shields[int(m.group(1))], work)
    return work.rstrip(), removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("index", help="MEMORY.md 路径")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, help="只处理前 N 个有改动的行（分批用）")
    ap.add_argument("--no-self-gate", action="store_true",
                    help="跳过写入前的闸自检（不建议：脚本直写 MEMORY.md 会绕过挂在 "
                         "Edit/Write 上的四道闸，那正是索引涨回去的原因）")
    ap.add_argument("--mem-dir", help="专题档所在目录。**在副本上试跑时必须给** —— "
                                      "否则副本目录里没有专题档，守恒会把每个 token 都判成失败、一个也不删，"
                                      "看起来像「脚本不工作」。")
    args = ap.parse_args()

    mem_dir = os.path.abspath(args.mem_dir) if args.mem_dir \
        else os.path.dirname(os.path.abspath(args.index))
    with open(args.index, encoding="utf-8") as f:
        lines = f.read().split("\n")

    out, changed, all_removed, orphan_tokens = [], 0, [], []
    skipped_long = []
    for line in lines:
        if args.limit and changed >= args.limit:
            out.append(line)
            continue
        # 第一遍：探这一行会删掉哪些 token
        _probe, candidates = delog_line(line)
        if not candidates:
            out.append(line)
            continue

        # 守恒检查：被删的 token 在该行所链的专题档里还找不找得到
        targets = LINK_RE.findall(line)
        blob = ""
        for t in targets:
            fp = os.path.join(mem_dir, os.path.basename(t))
            if os.path.exists(fp):
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    blob += fh.read()
        keep = set()
        for tok in candidates:
            bare = tok.strip("`")
            if FLAG_RE.fullmatch(tok):
                continue                      # 状态标不是信息，不参与守恒
            if bare not in blob:
                keep.add(bare)
                orphan_tokens.append((bare, targets[0] if targets else "(无链接)", line[:60]))

        # 第二遍：守恒失败的原样留下
        new, removed = delog_line(line, keep=keep)
        if keep and "闸四豁免" not in new:
            # 走闸四自己的豁免出口，把理由写下来 —— 不绕闸，也让下次审计看得见为什么留
            new += f"<!--闸四豁免:{'/'.join(sorted(keep))} 在专题档里查不到，删则可达性只剩 git-->"
        if not removed or new == line:
            out.append(line)
            continue
        if len(new) > 200:
            # 改后仍超 200 ⇒ 闸一会把它当「新增的超长行」拦下（它靠「不在旧文件里」判新增，
            # 区分不了「新加的长行」和「改短了但仍超长的存量行」）。整行跳过，不为它松闸。
            skipped_long.append((len(line), len(new), line[:60]))
            out.append(line)
            continue
        changed += 1
        all_removed.extend(removed)

        print(f"  L{lines.index(line)+1 if line in lines else '?'} 删 {len(removed)} 个 "
              f"[{' '.join(removed[:6])}]")
        print(f"    - {line[:110]}")
        print(f"    + {new[:110]}")
        out.append(new)

    before_chars = sum(len(l) for l in lines)
    after_chars = sum(len(l) for l in out)
    print(f"\n改动行数 {changed} · 删 token {len(all_removed)} 个 · "
          f"字符 {before_chars} → {after_chars}（-{before_chars-after_chars}，"
          f"{(before_chars-after_chars)/max(before_chars,1)*100:.1f}%）")

    if skipped_long:
        print(f"\n⚠ {len(skipped_long)} 行改后仍 >200 字符 ⇒ **整行跳过不改**"
              f"（闸一会当成新增超长行拦下，而它只是变短了的存量行）：")
        for a, b, ctx in skipped_long:
            print(f"  {a}→{b} 字符 ｜ {ctx}…")

    if orphan_tokens:
        print(f"\n⚠ {len(orphan_tokens)} 个 token 在对应专题档里找不到 ⇒ **已保留不删**"
              f"（删了可达性就只剩 git 历史）：")
        for tok, tgt, ctx in orphan_tokens[:15]:
            print(f"  `{tok}` → {tgt}  ｜ {ctx}…")
        if len(orphan_tokens) > 15:
            print(f"  …另有 {len(orphan_tokens)-15} 个")
    else:
        print("\n✅ 守恒：被删的 hash / 日期在对应专题档里都还找得到")

    if args.apply and not args.no_self_gate:
        import json as _json
        import subprocess as _sp
        hook = os.path.expanduser("~/.claude/hooks/guard-memory-landing.py")
        abspath = os.path.abspath(args.index)
        # 🔴 闸只认 `*/memory/MEMORY.md`。在副本上跑自检会**永远通过** ——
        #    那不是「闸放行了」，是「闸根本没看」。不许把它报成通过。
        gated = (os.path.basename(abspath) == "MEMORY.md"
                 and f"{os.sep}memory{os.sep}" in abspath)
        if not gated:
            print(f"\n⚠ 自检跳过：闸只管 */memory/MEMORY.md，而这次写的是 {abspath}"
                  "\n   （在副本上自检会永远通过 —— 那是闸没看，不是闸放行）")
        elif os.path.exists(hook):
            payload = {"tool_name": "Write",
                       "tool_input": {"file_path": os.path.abspath(args.index),
                                      "content": "\n".join(out)}}
            r = _sp.run(["python3", hook], input=_json.dumps(payload),
                        capture_output=True, text=True)
            if r.returncode != 0:
                print("\n🔴 闸自检没过，未写入。闸说：\n" + (r.stderr or r.stdout)[:1200])
                sys.exit(1)
            print("\n✅ 闸自检通过（把新内容当一次 Write 喂给 guard-memory-landing.py）")
            if r.stdout.strip():
                print("   闸的提示：" + r.stdout.strip()[:300])

    if args.apply:
        shutil.copy2(args.index, args.index + ".pre-delog")
        with open(args.index, "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        print(f"\n已写入（原件备份 {args.index}.pre-delog）")
    else:
        print("\n（干跑。确认后加 --apply）")


if __name__ == "__main__":
    main()
