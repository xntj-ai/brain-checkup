#!/usr/bin/env python3
"""verify-facts.py — 记忆事实核验：把记忆里写死的事实拿去实地验证。

解决的问题：记忆里写着「端口 8800」「路径 /opt/xxx」「版本 3.14」这类**可以被验证**的事实，
但从来没人去核过。它们会静默过期，然后让 AI 自信地做错事。

两种模式：
  ① 探针模式：文件里已写了 `<!-- vrfy: <命令> -->`，直接跑那条命令，看退出码
  ② 发现模式：文件里没有探针，但扫出了可验证的事实（IP/端口/路径/版本），
     **不猜命令**，只列出「这些事实值得核」交给人或模型去配探针

🔴 设计红线：
- **默认只读**。核验命令一律在只读白名单内（ls/test/curl -I/systemctl status 等），
  发现命令里有写操作关键词直接拒跑并报告 —— 记忆核验绝不该改变被核验的系统。
- **绝不自动改写记忆**。本脚本只输出「符合 / 不符 / 没法自动验」三类结论，
  改写由人或模型在看到结果后决定。理由：探针失败可能是网络抖动、机器关机、
  临时故障，据此自动改记忆会把正确的记忆改错。
- 远程核验要显式开启（--remote），因为它会真的 ssh 出去。

用法：
  python3 verify-facts.py --base ~/.claude/projects                # 只跑已有探针
  python3 verify-facts.py --base ... --discover                    # 额外列出「该配探针」的文件
  python3 verify-facts.py --base ... --remote                      # 允许探针里含 ssh
  python3 verify-facts.py --base ... --only <项目名片段>            # 限定范围
"""
import os, re, glob, argparse, subprocess, datetime, sys, json, shlex

SKIP = {'MEMORY.md', 'MEMORY.local.md', 'STALE.md'}
# 探针两种写法：
#   <!-- vrfy: <命令> -->          在**本机**执行
#   <!-- vrfy@<host>: <命令> -->   在 <host> 上执行（ssh），默认跳过，加 --remote 才真连
#
# 🔴 `@host` 这个形态是 2026-08-08 加的，起因：库里原有 7 条探针**全部失败，且 7/7 都是
#    环境错配** —— 写的是 `test -d /c/Users/…`、`ls "D:/…"`，却在 Linux 主控机上执行，
#    永远必失败。根因不是"写错了命令"，是**探针语法里根本没有"这条该在哪台机上验"这个位置**，
#    于是作者默认了"本机"。让归属在语法上成为必填项，比事后一条条改更可靠。
PROBE_RE = re.compile(r'<!--\s*vrfy(?:@([A-Za-z0-9_.-]+))?:\s*(.+?)\s*-->')
# 文档里讲解语法用的占位命令，不是真探针
PLACEHOLDER_CMD = re.compile(r'(cmd|command|命令|<[^>]*>|\.\.\.|…)', re.I)

# 只读白名单：命令的第一个词必须在这里
READONLY_CMDS = {
    'ls', 'test', 'stat', 'cat', 'head', 'tail', 'wc', 'grep', 'find', 'file',
    'curl', 'wget', 'ping', 'nc', 'ss', 'netstat', 'dig', 'host', 'nslookup',
    'systemctl', 'journalctl', 'ps', 'pgrep', 'df', 'du', 'uname', 'whoami',
    # openssl 只在**查看**形态下放行（s_client 探连接 / x509 读证书）；
    # 生成类（req/genrsa/genpkey 带 -out）由下面的 OPENSSL_WRITE 单独拦。
    'openssl', 'echo',
    'python3', 'node', 'git', 'jq', 'echo', 'printf', 'date', 'which', 'command',
    'ssh', 'docker', 'kubectl', 'psql', 'redis-cli', 'true', 'false', '[',
}
# 出现即拒（哪怕在只读命令的参数里）
WRITE_MARKERS = re.compile(
    r'\b(rm|mv|cp|dd|mkfs|chmod|chown|truncate|tee|kill|pkill|killall|reboot|shutdown)\b'
    r'|>\s*/|>>|\bDROP\b|\bDELETE\b|\bUPDATE\b|\bINSERT\b|--force|-f\b.*\brm\b',
    re.I)
# systemctl/docker/git 的写子命令
OPENSSL_WRITE = re.compile(r'\bopenssl\s+(req|genrsa|genpkey|rsa|pkcs12|enc)\b|\bopenssl\b.*\s-out\b', re.I)

WRITE_SUBCMD = re.compile(
    r'\b(systemctl\s+(start|stop|restart|reload|enable|disable|mask)'
    r'|docker\s+(run|rm|rmi|stop|kill|exec|build|push)'
    r'|git\s+(push|reset|checkout|clean|commit|merge|rebase))\b', re.I)


def is_readonly(cmd):
    """判断一条探针命令是否安全到可以自动跑。返回 (ok, 原因)"""
    # `2>/dev/null` / `>/dev/null` 是**丢弃输出**，不是写入。
    # 原判据把它当成重定向拒跑，害 tencent-free-ssl-hosting 那条查证书到期的探针
    # 从装上那天起一次都没跑过 —— 拒跑不报错、静默不执行，比误报更难发现。
    cmd = re.sub(r'\d?>\s*/dev/null', ' ', cmd)
    if WRITE_MARKERS.search(cmd):
        return False, '含写操作/重定向标记'
    if WRITE_SUBCMD.search(cmd) or OPENSSL_WRITE.search(cmd):
        return False, '含写类子命令'
    # 逐段检查（|| && | ; 分隔）
    for seg in re.split(r'\|\||&&|[|;]', cmd):
        seg = seg.strip()
        if not seg:
            continue
        first = seg.split()[0].lstrip('(').strip()
        first = os.path.basename(first)
        if first.startswith('$') or first.startswith('`'):
            return False, f'命令名是变量/子命令替换：{first}'
        if first not in READONLY_CMDS:
            return False, f'命令不在只读白名单：{first}'
    return True, ''


# 可验证事实的形态（用于 --discover）
FACT_PATTERNS = [
    ('IP 地址', re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')),
    ('端口', re.compile(r'(?<![\w.])[:：]\s?(\d{4,5})\b')),
    ('绝对路径', re.compile(r'(?:^|[\s`(])(/(?:opt|var|etc|srv|home|usr)/[\w/.\-]+)')),
    ('Windows 路径', re.compile(r'[A-Za-z]:\\\\?[\w\\\\.\-]+')),
    ('版本号', re.compile(r'\bv?\d+\.\d+\.\d+\b')),
    ('服务名', re.compile(r'\b[\w-]+\.service\b')),
    ('域名', re.compile(r'\b(?:[a-z0-9-]+\.)+(?:com|cn|ai|tv|io|net|org)\b')),
]


def scan_facts(text):
    out = {}
    for name, pat in FACT_PATTERNS:
        hits = {m.group(0).strip() for m in pat.finditer(text)}
        if hits:
            out[name] = sorted(hits)[:6]
    return out


def run_probe(cmd, timeout, allow_remote, host=None):
    if host:
        if not allow_remote:
            return 'skipped', f'目标机 {host}，需 --remote 才连'
        # 只读白名单先对**原始命令**判，别让 ssh 外壳把内层命令藏过去
        ok, why = is_readonly(cmd)
        if not ok:
            return 'refused', why
        cmd = f"ssh -o ConnectTimeout=8 -o BatchMode=yes {host} {shlex.quote(cmd)}"
    if 'ssh' in cmd and not allow_remote:
        return 'skipped', '含 ssh，需 --remote 才跑'
    ok, why = is_readonly(cmd)
    if not ok:
        return 'refused', why
    try:
        r = subprocess.run(['bash', '-c', cmd], capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or r.stderr or '').strip().replace('\n', ' ')[:160]
        return ('pass' if r.returncode == 0 else 'fail'), f'exit={r.returncode} {out}'
    except subprocess.TimeoutExpired:
        return 'timeout', f'超过 {timeout}s'
    except Exception as e:
        return 'error', str(e)[:120]


def main():
    ap = argparse.ArgumentParser(description='记忆事实核验：跑探针 + 发现该配探针的文件。只读，绝不改写记忆。')
    ap.add_argument('--base', default=os.path.expanduser('~/.claude/projects'))
    # ⚠ CC 的项目目录名**全部**以 `-` 开头（`-home-ccuser-projects-xxx`），
    # 写成 `--only -home-ccuser` 会被 argparse 当成另一个选项而报错
    # ——即这个参数对它的主用途默认是坏的。必须用 `=` 形式。同族坑见 research memory
    # `dash-prefixed-path-shell-trap`（`-` 开头路径被 cd/head 当选项，不报错只给错数据）。
    ap.add_argument('--only', default=None,
                    help='只处理路径含该片段的项目。⚠ 值以 - 开头时必须写成 --only=-home-ccuser，'
                         '空格形式会被当成选项报错')
    ap.add_argument('--discover', action='store_true', help='额外列出「有可验证事实但没探针」的文件')
    ap.add_argument('--remote', action='store_true', help='允许跑含 ssh 的探针（会真的连出去）')
    ap.add_argument('--timeout', type=int, default=15)
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    base = os.path.expanduser(a.base)
    if not os.path.isdir(base):
        print(f'错误：--base 不存在：{base}', file=sys.stderr)
        return 2

    files = [f for f in glob.glob(os.path.join(base, '*', 'memory', '**', '*.md'), recursive=True)
             if os.path.basename(f) not in SKIP]
    if a.only:
        files = [f for f in files if a.only in f]
    if not files:
        print(f'错误：{base} 下没找到记忆文件（--only={a.only!r}）', file=sys.stderr)
        return 2

    results = {'pass': [], 'fail': [], 'refused': [], 'skipped': [], 'timeout': [], 'error': []}
    discover = []
    for f in sorted(files):
        try:
            txt = open(f, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        probes = PROBE_RE.findall(txt)
        if probes:
            for host, cmd in probes:          # host 为 '' 表示本机
                # 占位写法不是探针：讲解探针语法的文档（memory-methodology.md）里那句
                # `<!-- vrfy: cmd -->` 会被原样解析成一条真探针，然后永远"拒跑"。
                # 同族误报见 memory-gc.py 的 PLACEHOLDER —— 文档里的示例必须能被认出来。
                if PLACEHOLDER_CMD.fullmatch(cmd.strip()):
                    continue
                st, detail = run_probe(cmd, a.timeout, a.remote, host or None)
                label = f'@{host} ' if host else ''
                results[st].append((os.path.relpath(f, base), label + cmd, detail))
        elif a.discover:
            facts = scan_facts(txt)
            n = sum(len(v) for v in facts.values())
            if n >= 3:
                discover.append((os.path.relpath(f, base), n, facts))

    lines = [f'# 记忆事实核验 {datetime.date.today()}', '',
             f'扫描 {len(files)} 个记忆文件 · 远程探针：{"允许" if a.remote else "跳过（加 --remote 开启）"}', '']
    tot = sum(len(v) for v in results.values())
    lines.append(f'**探针结果**：通过 {len(results["pass"])} · '
                 f'🔴 不符 {len(results["fail"])} · 拒跑 {len(results["refused"])} · '
                 f'跳过 {len(results["skipped"])} · 超时 {len(results["timeout"])} · 出错 {len(results["error"])}'
                 f'（共 {tot} 条）')
    lines.append('')

    if results['fail']:
        lines += ['## 🔴 事实不符 —— 记忆与实际情况对不上', '',
                  '> 先别急着改记忆：探针失败也可能是机器关机 / 网络抖动 / 临时故障。',
                  '> 逐条确认根因，确认是「事实真变了」再按实际情况改写，并注明改动依据。', '']
        for f, c, d in results['fail']:
            lines.append(f'- `{f}`\n  - 探针：`{c}`\n  - 实际：{d}')
        lines.append('')
    if results['refused']:
        lines += ['## ⚠ 拒跑（探针含写操作，本工具只跑只读命令）', '']
        for f, c, d in results['refused']:
            lines.append(f'- `{f}` — {d}\n  - `{c}`')
        lines.append('')
    if results['skipped']:
        lines += ['## ⏭ 跳过的远程探针（加 `--remote` 才会真的连出去）', '']
        for f, c, _ in results['skipped'][:20]:
            lines.append(f'- `{f}` — `{c}`')
        lines.append('')
    if discover:
        lines += [f'## 📋 建议配探针的文件（{len(discover)} 个）', '',
                  '> 这些文件写着具体的 IP / 端口 / 路径 / 版本，但没有任何验证方式。',
                  '> 本工具**不猜命令**——请人或模型按文件内容补一行 `<!-- vrfy: <只读命令> -->`。', '']
        for f, n, facts in sorted(discover, key=lambda x: -x[1])[:30]:
            kinds = ' · '.join(f'{k}({len(v)})' for k, v in facts.items())
            lines.append(f'- `{f}` — {n} 处可验证事实：{kinds}')
            sample = next(iter(facts.values()))[:3]
            lines.append(f'  - 例：{", ".join(sample)}')
        lines.append('')

    lines.append('---')
    lines.append('本工具只读：不修改任何记忆文件，也不对被核验的系统做任何写操作。')
    report = '\n'.join(lines).rstrip('\n') + '\n'

    if a.out:
        out = os.path.expanduser(a.out)
        os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
        open(out, 'w', encoding='utf-8').write(report)
        print(f'报告 → {out}')
    else:
        print(report)
    print(json.dumps({k: len(v) for k, v in results.items()} | {'discover': len(discover)},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
