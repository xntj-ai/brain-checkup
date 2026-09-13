#!/usr/bin/env python3
"""memory-usage.py — 从会话记录（jsonl）统计一个记忆桶近 N 天的真实使用。只读，不改任何文件。

报什么：
  · 每个专题档被 Read / 被 Bash 命令提及（grep/cat/wc/git…）的次数，以及窗口内从未被碰过的档
  · 主索引每一条链接行：指向的档在窗口内有没有被碰过（常驻预算花在了谁身上）
  · 记忆写入（Edit/Write）次数，其中写主索引的次数与单日峰值；写得最多的专题档
  · 二级索引 _index-*.md 的体量结构（字符 / 行 / >200 字符行 / 最长行 / 被碰次数）

用法：
  python3 memory-usage.py --bucket=-home-ccuser-projects-cruna-ai [--days 30] [--top 15]
  ⚠ 桶名以 - 开头，必须写成 --bucket=-home-xxx（空格形式会被 argparse 当成选项）

口径（写进报告，别拿它做结构决策——方案 v1 M5「只出报告」）：
  · 「被碰」= Read 工具的 file_path 落在该桶 memory/ 下，或 Bash 命令文本里出现 /memory/<file>.md
    （含 grep/cat/wc/git 等读操作，也含少量写）。这是「触达」不是「阅读」。
  · 只统计本机 jsonl；别的机器上跑的 CC 会话看不到。
  · 「N 天未被碰」≠ 无价值：年检 / 灾备 / 收信链路这类低频现役档，正是用的那次要命的一类。
"""
import argparse
import collections
import glob
import json
import os
import re
import time

BASE = os.path.expanduser('~/.claude/projects')
MEM_RE = re.compile(r'/memory/([\w\-./]+\.md)')
LINK_RE = re.compile(r'\]\(([^)]+\.md)\)')


def scan_sessions(bucket, days):
    """遍历桶内窗口期的 jsonl，返回 (touched, writes, idx_writes, idx_days, sessions, tool_uses)。"""
    cut = time.time() - days * 86400
    touched = collections.Counter()
    writes = collections.Counter()
    idx_days = collections.Counter()
    sessions = 0
    tool_uses = 0
    for path in glob.glob(f'{BASE}/{bucket}/*.jsonl'):
        if os.path.getmtime(path) < cut:
            continue
        sessions += 1
        with open(path, encoding='utf-8', errors='ignore') as fh:
            for line in fh:
                if 'tool_use' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                day = (obj.get('timestamp') or '')[:10]
                msg = obj.get('message') or {}
                content = msg.get('content') if isinstance(msg.get('content'), list) else []
                for item in content:
                    if item.get('type') != 'tool_use':
                        continue
                    tool_uses += 1
                    name = item.get('name')
                    inp = item.get('input') or {}
                    fpath = inp.get('file_path') or ''
                    cmd = inp.get('command') or ''
                    if name == 'Read' and '/memory/' in fpath:
                        touched[fpath.split('/memory/')[-1]] += 1
                    elif name == 'Bash' and '/memory/' in cmd:
                        for m in MEM_RE.findall(cmd):
                            touched[m] += 1
                    elif name in ('Edit', 'Write') and f'{bucket}/memory/' in fpath:
                        fname = fpath.split('/memory/')[-1]
                        writes[fname] += 1
                        if fname == 'MEMORY.md':
                            idx_days[day] += 1
    return touched, writes, idx_days, sessions, tool_uses


def strip_frontmatter(text):
    text = re.sub(r'\A---\n.*?\n---\n', '', text, flags=re.S)
    return re.sub(r'(?ms)^<!--.*?-->\n?', '', text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bucket', required=True, help='桶目录名，以 - 开头时写成 --bucket=-home-xxx')
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--top', type=int, default=15)
    args = ap.parse_args()

    mem = f'{BASE}/{args.bucket}/memory'
    if not os.path.isdir(mem):
        raise SystemExit(f'没有这个桶的 memory 目录：{mem}')

    touched, writes, idx_days, sessions, tool_uses = scan_sessions(args.bucket, args.days)
    files = [f for f in os.listdir(mem) if f.endswith('.md') and f != 'MEMORY.md']
    hit = lambda f: touched[f] > 0 or touched[os.path.basename(f)] > 0
    never = sorted(f for f in files if not hit(f))

    print(f'== {args.bucket} · 近 {args.days} 天 · 会话 {sessions} 个 · 工具调用 {tool_uses} 次')
    print(f'   专题档 {len(files)} 个：被碰过 {len(files) - len(never)} 个，从未被碰 {len(never)} 个')
    print(f'   被碰最多的 {args.top} 个：')
    for name, n in touched.most_common(args.top):
        print(f'     {n:4d}  {name}')
    print('   从未被碰的：' + (' · '.join(never) if never else '（无）'))

    idx_path = f'{mem}/MEMORY.md'
    if os.path.exists(idx_path):
        live = dead = live_chars = dead_chars = nolink = 0
        dead_lines = []
        for line in open(idx_path, encoding='utf-8').read().split('\n'):
            if not line.strip() or line.startswith('#'):
                continue
            targets = [os.path.basename(t) for t in LINK_RE.findall(line)]
            if not targets:
                nolink += 1
                continue
            if any(hit(t) for t in targets):
                live += 1
                live_chars += len(line)
            else:
                dead += 1
                dead_chars += len(line)
                dead_lines.append(line)
        print(f'   主索引链接行 {live + dead}：指向被碰过的档 {live} 行（{live_chars} 字），'
              f'指向未被碰的档 {dead} 行（{dead_chars} 字）；无链接行 {nolink}')
        for line in dead_lines[:12]:
            print(f'     · {line[:110]}')

    total_w = sum(writes.values())
    print(f'   记忆写入 {total_w} 次，其中写主索引 {writes["MEMORY.md"]} 次，'
          f'分布在 {len(idx_days)} 天，单日最多 {max(idx_days.values()) if idx_days else 0} 次')
    print('   写得最多的专题档：' + ' · '.join(
        f'{k}({v})' for k, v in writes.most_common(8) if k != 'MEMORY.md'))

    subs = sorted(glob.glob(f'{mem}/_index-*.md'))
    if subs:
        print('   二级索引：')
        for sub in subs:
            text = strip_frontmatter(open(sub, encoding='utf-8').read())
            lines = [l for l in text.split('\n') if l.strip()]
            longest = max((len(l) for l in lines), default=0)
            over = sum(1 for l in lines if len(l) > 200)
            base = os.path.basename(sub)
            print(f'     {base:40s} {len(text):6d} 字 {len(lines):4d} 行  >200 字行 {over:3d}  '
                  f'最长 {longest:4d}  被碰 {touched[base]} 次')


if __name__ == '__main__':
    main()
