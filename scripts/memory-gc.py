#!/usr/bin/env python3
"""memory-gc.py — 记忆库周巡检（2026-08-07 P6 建立）

扫全部项目 memory，输出四类信号，不自动改动任何文件：
  ① 索引超标（逼近 200 行 / 25KB 加载线，或有超长行）
  ② 过期候选（90 天未 modified 且 status: active；forget_after 已过期）
  ③ 孤儿文件（磁盘上有、索引里没链接到）
  ④ 幻影链接（索引里链了、磁盘上没有）

⚠ 路径以 `-` 开头（CC 项目目录命名），一律用 Python 的 open/glob，别用 shell 循环
   —— 见 research memory `dash-prefixed-path-shell-trap`。

用法：python3 memory-gc.py [--days 90] [--out 报告路径]
"""
import os, re, sys, glob, argparse, datetime, json

LINE_LIMIT, BYTE_LIMIT = 200, 25600
SKIP = {'MEMORY.md', 'MEMORY.local.md', 'STALE.md'}


def _read(path):
    """读文件，失败返回 None 而不是让整轮巡检崩掉。

    实测会崩的输入：权限 000 的 .md（PermissionError）、名字叫 x.md 的**目录**
    （IsADirectoryError）、不可读的 MEMORY.md。39 个项目扫到第 3 个就死、
    前面的结果全丢，用户看到 traceback 而不是报告。
    """
    try:
        if not os.path.isfile(path):
            return None
        return open(path, encoding='utf-8', errors='replace').read()
    except OSError:
        return None


def _date(s):
    """解析 YYYY-MM-DD，非法值返回 None。
    正则只管形状不管合法性，`forget_after: 2026-13-45` 会让 fromisoformat 抛 ValueError。
    """
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def scan_project(mdir):
    """返回单个项目 memory 目录的巡检结果"""
    idx_path = os.path.join(mdir, 'MEMORY.md')
    if not os.path.exists(idx_path):
        return None
    idx = _read(idx_path)
    if idx is None:
        return None
    # 按官方口径：frontmatter 与块级 HTML 注释在加载前剥离，不占限额
    measured = re.sub(r'\A---\n.*?\n---\n', '', idx, flags=re.S)
    measured = re.sub(r'(?ms)^<!--.*?-->\n?', '', measured)

    lines = measured.splitlines()
    r = {
        'project': os.path.basename(os.path.dirname(mdir)),
        'index_lines': len(lines),
        'index_bytes': len(measured.encode()),
        'long_lines': sum(1 for x in lines if len(x) > 200),
        'over': [],
        'stale': [],
        'orphans': [],
        'phantoms': [],
    }
    if r['index_lines'] > LINE_LIMIT * 0.85:
        r['over'].append(f"行数 {r['index_lines']}/{LINE_LIMIT}")
    if r['index_bytes'] > BYTE_LIMIT * 0.85:
        r['over'].append(f"字节 {r['index_bytes']}/{BYTE_LIMIT}")
    # 🔴 2026-08-08：**长行检查已整条移除**，不是调阈值。
    # 证据链：① 原判据 >200 字符把 19 项告警里的 18 项算成问题
    #        ② 盲测（4 评估者 × 8 道真实历史题）显示恰恰相反 —— 把结论直接写进索引行的
    #           密集写法命中率最高，评估者原话「索引行本身已把结论写出来，不用翻正文」
    #        ③ 改成 >1200「疑似粘贴事故」后仅剩 2 例，逐条看**全是有意的密集写法**
    # ⇒ 两个阈值下都是 100% 假阳性、0 真阳性。一个从没报对过的检查只会训练人无视整份报告。
    # 真正有效的仍在：行数 >85% 与字节 >85%（那两条是硬加载上限，超了会静默截断）。
    # 若将来要恢复，先拿出「它报对过一次」的证据。

    # 🔴 链接要连**二级索引**一起收：本库用 `_index-*.md` / `MEMORY-archive.md` 分流大专题，
    # 只扫 MEMORY.md 会把二级索引下挂的文件全报成孤儿（首跑 374 个，计入后 173 —— 误报过半）。
    linked, outside_refs, mentioned, idx_text = set(), set(), set(), []
    broken_outside = set()
    for src in [idx_path] + sorted(glob.glob(os.path.join(mdir, '_index-*.md'))
                                   + glob.glob(os.path.join(mdir, 'MEMORY-archive.md'))):
        s = _read(src)
        if s is None:
            continue
        # 链接先按「索引所在目录」解析成相对本目录的规范路径再比对；basename 只作兜底。
        # 旧版只比 basename ⇒ 索引里写 `](../elsewhere/notes.md)` 会让本目录里从没被链过的
        # notes.md 被误判为"已链接"，孤儿数变 0。
        for m in re.findall(r'\]\(([^)]+\.md)\)', s):
            if '/' not in m:
                linked.add(m)            # 纯文件名 ⇒ 指本目录，直接登记
                continue
            # 带路径的：解析成相对本目录的规范路径。指向目录**之外**的（`../elsewhere/x.md`）
            # 绝不能把本目录里的同名文件算成"已链接"—— 旧版就是这么把真孤儿吞掉的。
            rel = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(src)), m))
            inside = os.path.relpath(rel, os.path.abspath(mdir))
            if not inside.startswith('..'):
                linked.add(inside)
                linked.add(os.path.basename(m))
            else:
                # 库外引用：**不能一律放过**。整座库被归档搬走后，指过去的相对路径就是坏的。
                # 实测本库 5 条「跨库引用」全部落空（`../../C--Users-<旧用户名>/memory/*`
                # 那个桶已在 P2 移到 `~/projects/memory-curator/_archive/` 外部）。
                # 判据：落地探一次；目标不存在、且全库也找不到同名文件 ⇒ 记成坏引用而非「另计」。
                if os.path.exists(rel) or os.path.basename(m) in _all_memory_filenames():
                    outside_refs.add(m)
                else:
                    broken_outside.add(m)
        linked |= {m + '.md' for m in re.findall(r'\[\[([^\]]+)\]\]', s)}
        # 第三种链接形态：反引号里或裸文本直接写文件名（`memory/xxx.md` · xxx.md）。
        # 本库绝大多数索引就是这么写的，只认 md-link + wikilink ⇒ 全报成孤儿：
        # xntj-video 报 38 个孤儿，抽验 5 个**全部**在索引里（误报率 100%）。
        # 判据回到目的：名字在索引里出现过 ⇒ 检索得到 ⇒ 不是孤儿。
        #
        # ⚠ 只进 `mentioned`（压孤儿），**绝不进 `linked`**（不参与死链判定）：
        # 索引里同样会提到 `CLAUDE.md`、`docs/account-profile.md` 这类**项目仓库文件**，
        # 它们本来就不在记忆目录里。首版混进 linked ⇒ 幻影链接 0 → 54 全是误报。
        # 非对称：提到了 ⇒ 能被检索到（证有，一次命中即可）；提到但记忆库里没有 ⇒ 什么都证明不了。
        mentioned |= {os.path.basename(m)
                      for m in re.findall(r'(?<![\w/-])([\w][\w./-]*\.md)', s)}
        idx_text.append(s)
    # 递归：把记忆按主题分了子目录的库，旧版非递归 glob 一个都扫不到，孤儿数恒为 0
    on_disk = {os.path.relpath(f, mdir)
               for f in glob.glob(os.path.join(mdir, '**', '*.md'), recursive=True)
               if os.path.isfile(f)}
    on_disk -= {n for n in on_disk if os.path.basename(n) in SKIP}

    # 索引里用「各 ep00XX-progress.md」这类**泛指**覆盖一整批同构文件，是有意设计不是漏链。
    # 判据：索引正文出现该模式的泛指写法时，同名族文件不算孤儿。
    # ⚠ 家族前缀必须**连同后面的形状**一起匹配，不能只认前缀。
    # 旧版把 `maxx.md` 解析成家族 `^ma\d+`，于是 `ma2024-report.md` 被静默排除出孤儿名单。
    # 现在要求：前缀 + 数字 + 与模板相同的后缀形状（如 `ep00XX-progress.md` ⇒ `^ep\d+-progress\.md$`）。
    fam_pats = []
    for m in re.finditer(r'`?([a-z]+)\d*(?:XX|xx|####)([-\w]*)\.md`?', idx):
        prefix, suffix = re.escape(m.group(1)), re.escape(m.group(2))
        fam_pats.append(f'^{prefix}\\d+{suffix}\\.md$')
    fam_re = re.compile('|'.join(sorted(set(fam_pats)))) if fam_pats else None
    # 第四种形态：索引里写**裸词**、不带 `.md` 也不做链接
    # （cruna-studio：`**运维**: deploy, backup-system, db-separation, …`）。
    # 名字原样出现在索引里 ⇒ 人和模型都检索得到 ⇒ 不是孤儿。抽验 3/3 全是这种，误报率 100%。
    # 只认「整词命中」（前后不接字母数字/连字符/下划线），避免 `deploy` 顺带盖住 `deploy-race-condition`。
    idx_all = '\n'.join(idx_text)
    stem_hit = set()
    for n in on_disk:
        stem = os.path.basename(n)[:-3]
        if re.search(r'(?<![\w-])' + re.escape(stem) + r'(?![\w-])', idx_all):
            stem_hit.add(n)
    r['orphans'] = sorted(n for n in on_disk
                          if n not in linked and os.path.basename(n) not in linked
                          and os.path.basename(n) not in mentioned
                          and n not in stem_hit
                          and not (fam_re and fam_re.match(os.path.basename(n))))

    # 幻影＝索引链了但**全库都找不到**的文件。首版只在本目录找，6 条全是误报：
    #   ① 跨项目引用（某项目索引链到另一个项目的 memory 文件，4 条）—— 合法用法，不是死链
    #   ② 占位符/示例（`ep00XX-progress.md` 是模板写法、`链接.md` 是 markdown 语法示例被正则吃到）
    # 天天误报的检查等于没有检查（本库已记过「audit hook 误报 token 变量」，同病）。
    PLACEHOLDER = re.compile(r'(XX|xx|\{|\}|<|>|^链接\.md$|示例|example)')
    everywhere = _all_memory_filenames()
    disk_names = on_disk | {os.path.basename(n) for n in on_disk}
    dangling = {n for n in linked if n not in disk_names and os.path.basename(n) not in disk_names}
    dangling -= SKIP
    r['phantoms'] = sorted(n for n in dangling
                           if os.path.basename(n) not in everywhere and not PLACEHOLDER.search(n))
    r['cross_ref'] = sorted(set(n for n in dangling if os.path.basename(n) in everywhere)
                            | outside_refs)
    # 库外引用里**探不到落点**的那些：等价于死链，必须单列（否则整座库被归档搬走后无人发现）
    r['phantoms'] = sorted(set(r['phantoms']) | broken_outside)
    return r, mdir, on_disk


_ALL_NAMES = None
_SCAN_ROOTS = [os.path.expanduser("~/.claude/projects")]  # main() 会按 --base/--archive 覆盖


def _all_memory_filenames():
    """全库 .md 文件名集合（含归档区），用于把跨项目引用与真死链区分开。

    归档目录由 --archive 指定，可给多个；不给则只扫 memory 根。
    不硬编码任何个人路径——本脚本要能在任何人的机器上直接跑。
    """
    global _ALL_NAMES
    if _ALL_NAMES is None:
        _ALL_NAMES = set()
        for root in _SCAN_ROOTS:
            for f in glob.glob(os.path.join(root, '**', '*.md'), recursive=True):
                _ALL_NAMES.add(os.path.basename(f))
    return _ALL_NAMES


def check_staleness(mdir, names, days):
    """陈旧候选 + forget_after 过期。

    🔴 时间源优先级：frontmatter `last_verified` > 官方自动写的 `modified` > 文件 mtime。
    旧版只认 `last_verified`，而那是**某些库的私有约定**，不是 Claude Code 的产物
    （官方唯一自动字段是 `modified`，且明说 never adds frontmatter to a file that has none）
    ⇒ 在没跑过私有回填的库上，这条信号恒为 0，被读成"没有陈旧记忆"。
    同理 `status: active` 从准入门槛降级为过滤器：**缺该字段视为 active**，而不是跳过。
    """
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    stale, forget, unreadable, no_time = [], [], [], 0
    for n in sorted(names):
        path = os.path.join(mdir, n)
        txt = _read(path)
        if txt is None:
            unreadable.append(n)
            continue
        head = '\n'.join(txt.splitlines()[:20])
        st = re.search(r'^status:\s*(\w+)', head, re.M)
        if st and st.group(1).lower() != 'active':
            continue                      # 显式标了非 active 才跳过
        mt = None
        for pat in (r'^last_verified:\s*(\d{4}-\d{2}-\d{2})',
                    r'^modified:\s*(\d{4}-\d{2}-\d{2})'):
            m = re.search(pat, head, re.M)
            if m:
                mt = _date(m.group(1))
                if mt:
                    break
        if mt is None:
            no_time += 1
            try:
                mt = datetime.date.fromtimestamp(os.path.getmtime(path))
            except OSError:
                continue
        tier = (re.search(r'^tier:\s*(L[123])', head, re.M) or [None, '?'])[1]
        if mt < cutoff:
            stale.append(f'{n} [{tier}] {mt}')
        fa = re.search(r'^forget_after:\s*(\d{4}-\d{2}-\d{2})', head, re.M)
        if fa:
            d = _date(fa.group(1))
            if d is None:
                forget.append(f'{n} forget_after 值非法：{fa.group(1)}')
            elif d < today:
                forget.append(f'{n} forget_after={fa.group(1)} 已过期')
    return stale, forget, unreadable, no_time


def main():
    global _SCAN_ROOTS
    ap = argparse.ArgumentParser(
        description='记忆库巡检：索引超标 / 降级候选 / 孤儿文件 / 幻影链接。只报告，不改动任何文件。')
    ap.add_argument('--base', default=os.path.expanduser('~/.claude/projects'),
                    help='auto memory 根目录（默认 ~/.claude/projects，会扫其下的 */memory/）')
    ap.add_argument('--archive', action='append', default=[],
                    help='归档目录，可重复给；用于把跨库引用与真死链区分开')
    ap.add_argument('--days', type=int, default=90, help='陈旧阈值天数（默认 90）')
    # ⚠ CC 项目目录名全部以 `-` 开头，`--only -home-xxx` 会被 argparse 当成选项 ⇒ 必须写 `--only=-home-xxx`
    ap.add_argument('--only', default=None,
                    help='只扫路径含该片段的项目。⚠ 值以 - 开头时必须写成 --only=-home-xxx')
    ap.add_argument('--brief', action='store_true',
                    help='只在**有可行动问题**时往 stdout 打几行（索引逼近上限/死链/孤儿），'
                         '否则完全静默、不写报告。给 hook 用：天天说话的检查会被无视')
    ap.add_argument('--force', action='store_true',
                    help='允许覆盖已存在的报告文件（默认改写成带时间戳的新文件，绝不静默覆盖）')
    ap.add_argument('--out', default=None,
                    help='报告输出路径（默认写到 <base>/../memory-gc-report.md）')
    a = ap.parse_args()

    base = os.path.expanduser(a.base)

    # 🔴 前置校验：路径写错时必须**出声报错**，不能静默输出「全 0」。
    # 自测发现：--base 指向不存在的目录时旧版照常输出 0 告警，用户会读成
    # 「我的记忆库很健康」，实际是根本没扫到东西 —— 正是本 skill 三条铁律要防的
    # 「脚本安静地给出错误数据」。
    if not os.path.isdir(base):
        print(f'错误：--base 指向的目录不存在：{base}', file=sys.stderr)
        print('提示：Claude Code 的默认位置是 ~/.claude/projects', file=sys.stderr)
        return 2
    # 两种已知结构：
    #   ① 项目 auto memory： <base>/<项目>/memory/MEMORY.md
    #   ② subagent memory ： <base>/<agent名>/MEMORY.md（扁平，没有中间的 memory/ 层）
    found_dirs = glob.glob(os.path.join(base, '*', 'memory'))
    if not found_dirs:
        flat = [d for d in glob.glob(os.path.join(base, '*'))
                if os.path.isdir(d) and os.path.exists(os.path.join(d, 'MEMORY.md'))]
        if flat:
            found_dirs = flat
            print(f'提示：未见 <base>/*/memory/ 结构，按扁平结构处理（如 subagent memory）：'
                  f'找到 {len(flat)} 个含 MEMORY.md 的目录', file=sys.stderr)
    if not found_dirs:
        print(f'错误：{base} 下没有找到任何 */memory/ 目录，没有可巡检的对象。', file=sys.stderr)
        print('提示：本脚本期望的结构是 <base>/<项目>/memory/MEMORY.md；'
              '若你的记忆库结构不同，请用 --base 指到正确的上层目录。', file=sys.stderr)
        return 2
    if a.only:
        found_dirs = [d for d in found_dirs if a.only in d]
        if not found_dirs:
            if a.brief:
                return 0            # brief 模式下「这个项目没记忆」是常态，静默
            print(f'错误：--only={a.only} 没匹配到任何项目。', file=sys.stderr)
            return 2
    with_index = [d for d in found_dirs if os.path.exists(os.path.join(d, 'MEMORY.md'))]
    if not with_index:
        print(f'警告：找到 {len(found_dirs)} 个 memory 目录，但没有一个含 MEMORY.md —— '
              '巡检的四类信号都依赖索引文件，结果会全为空。', file=sys.stderr)

    if not a.out:
        a.out = os.path.join(os.getcwd(), 'memory-gc-report.md')
    a.out = os.path.expanduser(a.out)
    # dirname 为空（--out 只给文件名）时 makedirs('') 会抛 FileNotFoundError
    out_dir = os.path.dirname(a.out) or '.'
    if not os.path.isdir(out_dir):
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            print(f'错误：无法创建报告目录 {out_dir}：{e}', file=sys.stderr)
            return 2

    # 报告落点若已存在同名文件，默认换成带时间戳的名字 —— 别人很可能把 --out 指到
    # 一个有内容的 md 上，静默覆盖就是数据丢失。
    if os.path.exists(a.out) and not a.force:
        stem, ext = os.path.splitext(a.out)
        a.out = f'{stem}-{datetime.datetime.now().strftime("%Y%m%d-%H%M%S")}{ext}'
        print(f'提示：目标已存在，报告改写到 {a.out}（要覆盖请加 --force）', file=sys.stderr)

    _SCAN_ROOTS = [base] + [os.path.expanduser(x) for x in a.archive]
    for r in _SCAN_ROOTS[1:]:
        if not os.path.isdir(r):
            print(f'警告：--archive 指向的目录不存在，已忽略：{r}', file=sys.stderr)

    out = [f'# 记忆库巡检报告 {datetime.date.today()}', '',
           f'扫描范围：`{base}/*/memory/`  ·  陈旧阈值：{a.days} 天', '']
    counts = {'over': 0, 'stale': 0, 'forget': 0, 'orphans': 0, 'phantoms': 0,
              'cross_ref': 0, 'unreadable': 0, 'no_time': 0}

    brief_hits = []
    for mdir in sorted(found_dirs):
        res = scan_project(mdir)
        if not res:
            continue
        r, mdir, on_disk = res
        if a.brief:
            # 只报**可行动**的三类，别的一律不出声。
            # 判据刻意收窄：长行、跨库引用、陈旧候选都不进 —— 它们要么不是问题（长行密集反而好用，
            # 盲测已证），要么需要人工裁决（陈旧），天天提示只会训练用户无视这个 hook。
            pct = r['index_bytes'] * 100 // BYTE_LIMIT
            if pct >= 85:
                brief_hits.append(f"索引已用 {pct}%（{r['index_bytes']}/{BYTE_LIMIT} 字节），"
                                  f"**超出部分会被静默截断**。按 brain-checkup skill 下沉。")
            if r['phantoms']:
                brief_hits.append(f"{len(r['phantoms'])} 条死链（索引指过去是空的）："
                                  + ' · '.join(r['phantoms'][:5]))
            if r['orphans']:
                brief_hits.append(f"{len(r['orphans'])} 个笔记没被索引收录（在硬盘上但想不起来用）："
                                  + ' · '.join(r['orphans'][:5]))
            continue
        stale, forget, unreadable, no_time = check_staleness(mdir, on_disk, a.days)
        counts['unreadable'] += len(unreadable)
        counts['no_time'] += no_time
        counts['cross_ref'] += len(r.get('cross_ref') or [])
        if not (r['over'] or r['orphans'] or r['phantoms'] or stale or forget or unreadable):
            continue
        out.append(f"## {r['project']}")
        out.append(f"索引 {r['index_lines']} 行 / {r['index_bytes']} 字节"
                   f"（限额 {LINE_LIMIT} 行 / {BYTE_LIMIT} 字节）")
        for k, label in [('over', '⚠ 索引超标'), ('phantoms', '🔴 幻影链接（索引链了但文件不存在）'),
                         ('orphans', 'ℹ 孤儿文件（磁盘有但索引没链）'),
                         ('cross_ref', 'ℹ 跨库引用（链到本目录之外，不是死链）')]:
            if r.get(k):
                if k != 'cross_ref':
                    counts[k] += len(r[k])
                out.append(f'- **{label}**：' + ' · '.join(r[k][:12])
                           + (f' …共 {len(r[k])} 项' if len(r[k]) > 12 else ''))
        if unreadable:
            out.append(f'- **🔴 读不了的文件（权限/是目录/IO 错）**：' + ' · '.join(unreadable[:8]))
        if forget:
            counts['forget'] += len(forget)
            out.append(f'- **🗑 forget_after 已过期**：' + ' · '.join(forget[:8]))
        if stale:
            counts['stale'] += len(stale)
            out.append(f'- **⏳ {a.days} 天未动的 active（降级候选）**：{len(stale)} 个'
                       + ('，例：' + ' · '.join(stale[:5]) if stale else ''))
        out.append('')

    if a.brief:
        if brief_hits:
            print('📋 记忆库体检发现：')
            for h in brief_hits:
                print(f'  - {h}')
        return 0                    # 无发现 ⇒ 完全静默，也不写报告

    out.insert(3, f"**汇总**：索引超标 {counts['over']} 项 · 幻影链接 {counts['phantoms']} 条 · "
                  f"孤儿文件 {counts['orphans']} 个 · 降级候选 {counts['stale']} 个 · "
                  f"forget_after 过期 {counts['forget']} 个\n")
    out.append('---\n处理原则：幻影链接必修（索引指向不存在的文件＝死链）；'
               '孤儿文件补链接或归档；降级候选人工过一遍后 active→archived 移库。'
               '**本脚本只报告不改动。**')

    open(a.out, 'w', encoding='utf-8').write('\n'.join(out).rstrip('\n') + '\n')
    print(f'报告 → {a.out}')
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == '__main__':
    sys.exit(main() or 0)
