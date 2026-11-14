#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 技能转换器 —— 把外部 AI 技能包（zip / 本地目录 / GitHub 仓库）转成 DSH 能直接用的技能。

设计红线：
  1. 不执行待转换包里的任何脚本；只做静态解析与复制。
  2. 不删技能正文；只校验 frontmatter。
  3. 动手前先备份到 _backup_<时间戳>/，--undo 可回滚。
  4. 默认只输出到 output/，加 --install 才写技能目录。

用法：python convert.py <来源> [选项]
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- 常量

if getattr(sys, "frozen", False):
    # PyInstaller 打包后 __file__ 在临时解包目录，不能用来存 output/_backup_
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

# 工作目录：output/_backup_/报告默认放在程序旁边；被限制只能写工作区的 Agent
# 可以用 DSH_SKILL_HOME 指到它有权限的目录（程序自己可能装在只读位置）。
APP_HOME = Path(os.environ.get("DSH_SKILL_HOME", "")).expanduser().resolve() if os.environ.get("DSH_SKILL_HOME") else SCRIPT_DIR
OUTPUT_DIR = APP_HOME / "output"

MAX_ARCHIVE_BYTES = 5 * 1024 * 1024          # 压缩包 ≤ 5 MB
MAX_EXTRACT_BYTES = 20 * 1024 * 1024         # 解压后 ≤ 20 MB
MAX_FILES = 500
MAX_BATCH_SKILLS = 200                          # 一次批量转换的技能个数上限

# 永远拒绝的二进制（技能目录里不应该出现）
BINARY_EXTS = {
    ".exe", ".com", ".scr", ".msi", ".msix", ".dll", ".sys", ".cpl", ".jar",
    ".pyc", ".pyd", ".so", ".dylib",
}
# 脚本：默认拒绝，--allow-scripts 才复制（复制但绝不执行）
SCRIPT_EXTS = {
    ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".jse", ".wsf", ".wsh",
    ".sh", ".bash", ".zsh", ".fish", ".command",
}
DANGEROUS_EXTS = BINARY_EXTS | SCRIPT_EXTS

# 技能自带的辅助代码（Python/JS 等）：原样复制、绝不执行，但要在报告里点名
CODE_EXTS = {
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".lua", ".php",
}

# 已知安全（只读文本 / 常见素材）扩展名，其余扩展名只警告不拦
KNOWN_EXTS = {
    ".md", ".markdown", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".csv", ".tsv", ".xml", ".html", ".htm",
    ".css", ".scss", ".less", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx",
    ".jsx", ".vue", ".svelte", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".rb", ".php", ".lua", ".sql", ".graphql", ".proto", ".lock",
    ".gitignore", ".gitattributes", ".editorconfig", ".env", ".png", ".jpg",
    ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".pdf", ".zip",
}

EXIT_OK = 0
EXIT_ARGS = 2
EXIT_REJECTED = 3
EXIT_ROLLBACK = 4


def eprint(*a):
    print(*a, file=sys.stderr)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def new_batch_id() -> str:
    """批次 ID：同一秒内多次运行也不会撞（微秒 + 4 位随机）。"""
    import secrets
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{secrets.token_hex(2)}"


def wp(p) -> str:
    """Windows 长路径前缀。Python 的 open() 本身能处理长路径，但 >=260 字符时
    某些 API 仍会失败；统一加 \\\\?\\ 前缀最稳。非 Windows 原样返回。"""
    s = os.fspath(p)
    if os.name != "nt":
        return s
    try:
        ap = os.path.abspath(s)
    except OSError:
        return s
    if ap.startswith("\\\\?\\"):
        return ap
    if len(ap) >= 240:
        if ap.startswith("\\\\"):
            return "\\\\?\\UNC\\" + ap[2:]
        return "\\\\?\\" + ap
    return ap


def open_w(p):
    return open(wp(p), "wb")


def read_text(p) -> str:
    with open(wp(p), "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_text(p, text: str):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    with open(wp(p), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# Windows 保留名：不能当目录/文件名
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}
PORTABLE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def check_skill_name(name: str):
    """技能名要能被 DSH 和 Windows 同时接受；返回 (ok, 原因)。"""
    if not name:
        return False, "名字是空的"
    if len(name) > 64:
        return False, "名字超过 64 个字符"
    if name.lower() in _WIN_RESERVED:
        return False, f"{name} 是 Windows 保留名，不能当目录名"
    if name != name.lower():
        return False, "只能用小写（DSH 的规范是 小写-连字符）"
    if not PORTABLE_NAME_RE.match(name):
        return False, "只能用小写字母、数字、连字符，且不能以连字符开头/结尾（例：code-review）"
    return True, ""


def tree_digest(root, ignore=("__pycache__", ".DS_Store")) -> str:
    """目录内容指纹：相对路径 + 每个文件内容的 sha256。用于撤销前判断有没有被改过。"""
    import hashlib
    root = Path(root)
    h = hashlib.sha256()
    if not root.exists():
        return "missing"
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(part in ignore for part in rel.split("/")):
            continue
        if p.is_symlink():
            h.update(b"L" + rel.encode("utf-8"))
            continue
        if p.is_dir():
            h.update(b"D" + rel.encode("utf-8"))
            continue
        h.update(b"F" + rel.encode("utf-8") + b"\0")
        with open(wp(p), "rb") as f:
            while True:
                chunk = f.read(1024 * 256)
                if not chunk:
                    break
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def dir_writable(p) -> bool:
    """目标目录能不能写（不存在就往上找第一个存在的祖先）。"""
    q = Path(p)
    while True:
        try:
            if q.exists():
                test = q / f".dsh-write-test-{os.getpid()}"
                test.write_text("x", encoding="utf-8")
                test.unlink()
                return True
            q = q.parent
            if q == q.parent:
                return False
        except OSError:
            return False

# ---------------------------------------------------------------- 拒绝异常

class Rejected(Exception):
    """安全检查失败：不落盘，退出码 3。"""

    def __init__(self, reason: str, detail: str = "", checks=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.checks = checks or []


# ---------------------------------------------------------------- 来源

def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s, re.I))


def parse_github(url: str):
    """github.com/<owner>/<repo>[/tree|blob/<ref...>[/<子目录...>]] -> (owner, repo, parts)

    ref 里可能带斜杠（feature/new），所以这里只返回「tree 后面剩下的全部片段」，
    由 fetch_github 逐个长度去试，而不是在这里硬切。
    """
    u = re.sub(r"[?#].*$", "", url).rstrip("/")
    m = re.match(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)(?:/(.*))?$", u, re.I)
    if not m:
        raise Rejected("这不是可识别的 GitHub 仓库地址",
                       f"{url}\n只接受 github.com/<owner>/<repo>（可带 /tree/<分支>/<子目录>）")
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    rest = [p for p in (m.group(3) or "").split("/") if p]
    parts: list[str] = []
    if rest:
        if rest[0] in ("tree", "blob"):
            parts = rest[1:]
        elif len(rest) >= 3 and rest[0] == "releases" and rest[1] == "tag":
            parts = [rest[2]]
        elif rest[0] in ("commit",) and len(rest) >= 2:
            parts = [rest[1]]
        # 其它（issues/pulls/...）当作没写 ref
    return owner, repo, parts


def _github_candidates(parts):
    """把 /tree/<a>/<b>/<sub> 展开成若干 (ref, subdir) 候选，长 ref 优先。"""
    if not parts:
        return [("main", ""), ("master", "")]
    cands = []
    for k in range(len(parts), 0, -1):
        cands.append(("/".join(parts[:k]), "/".join(parts[k:])))
    return cands


def fetch_github(url: str, workdir: Path):
    owner, repo, parts = parse_github(url)
    dest = workdir / f"{repo}.tar.gz"
    tried = []
    for ref, subdir in _github_candidates(parts):
        for spec in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
            tar_url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{spec}"
            try:
                n = download(tar_url, dest)
            except urllib.error.HTTPError as e:
                tried.append(f"{spec} → HTTP {e.code}")
                if e.code in (401, 403, 500, 502, 503):
                    break
                continue
            except urllib.error.URLError as e:
                raise Rejected("下载 GitHub 仓库失败",
                               f"{e.reason}\n网络被墙/DNS 有问题时，可先手动下载 zip 再传本地路径")
            if spec.startswith("refs/heads/"):
                what = f"分支 {ref}"
            elif spec.startswith("refs/tags/"):
                what = f"标签 {ref}"
            else:
                what = f"提交 {ref}"
            desc = f"github.com/{owner}/{repo} → {what}" + (f"/{subdir}" if subdir else "")
            return ("tarball", dest, desc, subdir, n)
    raise Rejected(
        "下载 GitHub 仓库失败",
        "试过这些引用都没成功：\n  " + "\n  ".join(tried[-6:])
        + "\n检查分支/标签名，或先手动下载 zip 再传本地路径")


def download(url: str, dest: Path, limit: int = MAX_ARCHIVE_BYTES, attempts: int = 3) -> int:
    """下载到 dest。校验长度，短读就重试——网络上截断的包会让解压阶段报 EOFError。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "dsh-skill-converter/0.1 (+https://github.com)",
        "Accept": "application/octet-stream, application/x-gzip, */*",
    })
    last_err = None
    for attempt in range(1, attempts + 1):
        total = 0
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
                length = resp.headers.get("Content-Length")
                expect = int(length) if length else 0
                if expect and expect > limit:
                    raise Rejected(f"下载体积超过上限（{human_size(expect)} > {human_size(limit)}）",
                                   "为避免拖爆磁盘，超过 5 MB 的包需先自行下载再传本地路径")
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise Rejected(f"下载体积超过上限（> {human_size(limit)}）",
                                       "为避免拖爆磁盘，超过 5 MB 的包需先自行下载再传本地路径")
                    f.write(chunk)
            if expect and total != expect:
                last_err = f"只收到 {human_size(total)}，应为 {human_size(expect)}"
                if attempt < attempts:
                    continue
                raise Rejected(f"下载不完整（{last_err}）",
                               "网络被中断了；重试一次，或先手动下载 zip 再传本地路径")
            return total
        except Rejected:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, EOFError, OSError) as e:
            last_err = str(e)
            if attempt < attempts:
                continue
            raise Rejected(f"下载失败：{last_err}",
                           "检查网络/代理；也可以先手动下载 zip，再用本地路径转换")
    raise Rejected(f"下载失败：{last_err}", "检查网络/代理，或改用本地 zip")





# ---------------------------------------------------------------- 安全解包

def _norm_parts(name: str):
    """返回规范化路径分段；越界或绝对路径返回 None。"""
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return parts


def _extract_zip(zf: zipfile.ZipFile, dest: Path) -> dict:
    stats = {"files": 0, "bytes": 0, "skipped_dirs": 0, "links": []}
    infos = zf.infolist()
    if len(infos) > MAX_FILES * 4:
        raise Rejected("压缩包条目过多", f"{len(infos)} 个条目（上限 {MAX_FILES * 4}）")
    total = 0
    for info in infos:
        parts = _norm_parts(info.filename)
        if parts is None:
            raise Rejected("压缩包含越界路径", f"拒绝：{info.filename!r}（绝对路径或 ../ 穿越）")
        if info.is_dir():
            stats["skipped_dirs"] += 1
            continue
        if info.flag_bits & 0x1:
            raise Rejected("压缩包是加密的", "本工具不解密，请先解压后传本地目录")
        mode = (info.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            # 符号链接一律不落地（也不跟随），只记录；比硬拒绝更贴近真实仓库
            stats["links"].append(info.filename)
            continue
        total += info.file_size
        if total > MAX_EXTRACT_BYTES:
            raise Rejected("解压后体积超过上限", f"> {human_size(MAX_EXTRACT_BYTES)}（疑似压缩炸弹）")
        stats["files"] += 1
        if stats["files"] > MAX_FILES:
            raise Rejected("文件数超过上限", f"> {MAX_FILES} 个")
        out = dest.joinpath(*parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open_w(out) as dst:
            shutil.copyfileobj(src, dst, 1024 * 256)
        stats["bytes"] += info.file_size
    return stats


def _extract_tar(tf: tarfile.TarFile, dest: Path) -> dict:
    stats = {"files": 0, "bytes": 0, "skipped_dirs": 0, "links": []}
    members = tf.getmembers()
    if len(members) > MAX_FILES * 4:
        raise Rejected("压缩包条目过多", f"{len(members)} 个条目")
    total = 0
    for m in members:
        parts = _norm_parts(m.name)
        if parts is None:
            raise Rejected("压缩包含越界路径", f"拒绝：{m.name!r}（绝对路径或 ../ 穿越）")
        if m.isdir():
            stats["skipped_dirs"] += 1
            continue
        if m.issym() or m.islnk():
            # 不落地、不跟随，只记录（GitHub tarball 里常有 AGENTS.md -> ... 这类链接）
            stats["links"].append(m.name)
            continue
        if not m.isfile():
            raise Rejected("压缩包含特殊文件", f"拒绝：{m.name!r}")
        total += m.size
        if total > MAX_EXTRACT_BYTES:
            raise Rejected("解压后体积超过上限", f"> {human_size(MAX_EXTRACT_BYTES)}（疑似压缩炸弹）")
        stats["files"] += 1
        if stats["files"] > MAX_FILES:
            raise Rejected("文件数超过上限", f"> {MAX_FILES} 个")
        out = dest.joinpath(*parts)
        out.parent.mkdir(parents=True, exist_ok=True)
        src = tf.extractfile(m)
        if src is None:
            raise Rejected("压缩包条目无法读取", f"拒绝：{m.name!r}")
        with src, open_w(out) as dst:
            shutil.copyfileobj(src, dst, 1024 * 256)
        stats["bytes"] += m.size
    return stats


def safe_extract(archive: Path, dest: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zf:
                return _extract_zip(zf, dest)
        with tarfile.open(archive, "r:*") as tf:
            return _extract_tar(tf, dest)
    except tarfile.TarError as e:
        raise Rejected("无法识别的压缩包格式", f"只支持 zip / tar.gz：{e}")
    except (zipfile.BadZipFile, EOFError) as e:
        raise Rejected("压缩包不完整（下载被中断或文件损坏）",
                       f"{e}\n重跑一次；还不行就手动下载 zip 再传本地路径")
    except OSError as e:
        # gzip.BadGzipFile 也是 OSError：既可能是坏包，也可能是磁盘写不进去
        raise Rejected("解压失败（包损坏，或磁盘空间/权限不足）",
                       f"{e}\n重跑一次；或先手动下载 zip 再传本地路径")


def copy_dir(src: Path, dest: Path) -> dict:
    """本地目录：不复制，先做遍历安检；返回统计。真正复制在落地阶段。"""
    stats = {"files": 0, "bytes": 0, "skipped_dirs": 0}
    total = 0
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if p.is_symlink():
            raise Rejected("目录里包含符号链接", f"拒绝：{rel.as_posix()}")
        if p.is_dir():
            stats["skipped_dirs"] += 1
            continue
        stats["files"] += 1
        if stats["files"] > MAX_FILES:
            raise Rejected("文件数超过上限", f"> {MAX_FILES} 个")
        total += p.stat().st_size
        if total > MAX_EXTRACT_BYTES:
            raise Rejected("目录体积超过上限", f"> {human_size(MAX_EXTRACT_BYTES)}")
        stats["bytes"] = total
    return stats


# ---------------------------------------------------------------- 结构分析

FRONTMATTER_RE = re.compile(r"^---\s*(?:\r?\n)(.*?)(?:\r?\n)---\s*(?:\r?\n|$)", re.S)


def _strip_yaml_comment(s: str) -> str:
    """去掉 YAML 行尾注释，但引号里的 # 不算。"""
    out, quote = [], None
    i = 0
    while i < len(s):
        c = s[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < len(s):
                out.append(c); out.append(s[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            out.append(c)
        else:
            if c in "\"'":
                quote = c
                out.append(c)
            elif c == "#" and (i == 0 or s[i - 1] in " \t"):
                break
            else:
                out.append(c)
        i += 1
    return "".join(out).rstrip()


_YAML_BOOL = {"true": True, "yes": True, "on": True, "false": False, "no": False, "off": False}


def _yaml_scalar(s: str):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner
    low = s.lower()
    if low in _YAML_BOOL:
        return _YAML_BOOL[low]
    if low in ("null", "~"):
        return None
    if re.match(r"^-?\d+$", s):
        try:
            return int(s)
        except ValueError:
            return s
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_yaml_scalar(x) for x in inner.split(",")] if inner else []
    return s


def yaml_load(block: str):
    """够用的 YAML 子集解析：标量 / 布尔 / 数字 / 引号 / 行尾注释 / 嵌套 map / 列表 / | 块。

    不追求完整 YAML（不解析锚点、tag、复杂 flow），但保证 frontmatter 里常见的写法
    不会被当成字符串 —— 这是 review 里指出的「disable-model-invocation: true # 注释」问题。
    """
    entries = []
    for raw in block.splitlines():
        if not raw.strip():
            continue
        line = raw.replace("\t", "    ")
        line = _strip_yaml_comment(line)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        entries.append((indent, line.strip()))

    root: dict = {}
    stack = [(-1, root)]
    i = 0
    while i < len(entries):
        indent, body = entries[i]
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if body.startswith("- "):
            if isinstance(parent, list):
                parent.append(_yaml_scalar(body[2:].strip()))
            i += 1
            continue
        m = re.match(r"^([^:#]+):\s*(.*)$", body)
        if not m:
            i += 1
            continue
        key = m.group(1).strip()
        rest = m.group(2).strip()
        if rest in ("|", ">"):
            buf, j = [], i + 1
            while j < len(entries) and entries[j][0] > indent:
                buf.append(entries[j][1])
                j += 1
            parent[key] = ("\n" if rest == "|" else " ").join(buf).strip()
            i = j
            continue
        if rest:
            parent[key] = _yaml_scalar(rest)
            i += 1
            continue
        if i + 1 < len(entries) and entries[i + 1][0] > indent:
            child = [] if entries[i + 1][1].startswith("- ") else {}
            parent[key] = child
            stack.append((indent, child))
            i += 1
            continue
        parent[key] = ""
        i += 1
    return root


def parse_frontmatter(text: str):
    """返回 (frontmatter dict, 错误信息)。dict 里额外带 __raw__ = 原始 frontmatter 文本。"""
    m = FRONTMATTER_RE.match(text.lstrip("\ufeff"))
    if not m:
        return None, "文件开头没有 --- 包裹的 YAML frontmatter"
    block = m.group(1)
    try:
        data = yaml_load(block)
    except Exception as e:  # 解析器自身出问题也不要崩，按「不合法」处理
        return None, f"frontmatter 解析失败：{e}"
    if not isinstance(data, dict):
        return None, "frontmatter 不是 key: value 结构"
    for k in ("name", "description"):
        if not data.get(k):
            return None, f"frontmatter 里缺少 {k}"
    data["__raw__"] = block
    return data, ""


def truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "yes", "1", "on")


def find_skill_roots(root: Path):
    """找出所有 <任意层级>/<名字>/SKILL.md（拍平后目标只留一级）。"""
    found = []
    for p in sorted(root.rglob("SKILL.md")):
        if not p.is_file():
            continue
        skill_dir = p.parent
        rel = skill_dir.relative_to(root)
        found.append({
            "dir": skill_dir,
            "rel": rel.as_posix() if str(rel) != "." else ".",
            "name": skill_dir.name if str(rel) != "." else root.name,
        })
    return found


def build_tree(paths):
    tree = {}
    for p in paths:
        node = tree
        parts = p.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append(parts[-1])
    return tree


def render_tree(tree, prefix="", out=None):
    if out is None:
        out = []
    keys = sorted(k for k in tree if k != "__files__")
    files = sorted(tree.get("__files__", []))
    for k in keys:
        out.append(f"{prefix}├─ {k}/")
        render_tree(tree[k], prefix + "│  ", out)
    for f in files:
        out.append(f"{prefix}├─ {f}")
    return out


# ---------------------------------------------------------------- 落地

def resolve_skills_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    home = os.environ.get("DSH_HOME")
    if home:
        return (Path(home).expanduser() / "skills").resolve()
    return (Path.home() / ".dsh" / "skills").resolve()


def _batch_dir(backup_root: Path, ts: str) -> Path:
    """批次备份目录：排他创建；名字撞了就加后缀，绝不覆盖别人的 manifest。"""
    d = backup_root / f"_backup_{ts}"
    n = 1
    while True:
        try:
            d.mkdir(parents=True, exist_ok=False)
            return d
        except FileExistsError:
            n += 1
            d = backup_root / f"_backup_{ts}-{n}"
            if n > 50:
                raise Rejected("备份目录创建失败", f"{backup_root} 下同名批次过多")


def install_skill(skill: dict, dest_root: Path, batch_dir: Path, mode: str, ts: str):
    """把技能复制进 dest_root/<name>。返回变更记录（含目录指纹）。"""
    name = skill["name"]
    dest = dest_root / name
    change = {"name": name, "action": "added", "dest": str(dest), "backup": "",
              "digest_before": "", "digest_after": ""}

    dest_root.mkdir(parents=True, exist_ok=True)
    # staging 放在目标同一文件系统里：最后的 move 才是「改个名」，不会变成跨盘复制
    staging = Path(tempfile.mkdtemp(prefix=".dsh-stage-", dir=str(dest_root)))
    stage_dir = staging / name
    try:
        shutil.copytree(skill["dir"], stage_dir, copy_function=shutil.copyfile,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if dest.exists():
            bdir = batch_dir / name
            bdir.parent.mkdir(parents=True, exist_ok=True)
            if bdir.exists():
                bdir = bdir.parent / f"{name}-{new_batch_id()}"
            change["digest_before"] = tree_digest(dest)
            shutil.move(str(dest), str(bdir))
            change.update({"action": "replaced", "backup": str(bdir),
                           "backup_digest": tree_digest(bdir)})
        shutil.move(str(stage_dir), str(dest))
        change["digest_after"] = tree_digest(dest)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return change, ts


def write_backup_manifest(batch_dir: Path, record: dict):
    batch_dir.mkdir(parents=True, exist_ok=True)
    write_text(batch_dir / "manifest.json", json.dumps(record, ensure_ascii=False, indent=2))
    return batch_dir


def list_backups(backup_root: Path):
    items = sorted(backup_root.glob("_backup_*"), reverse=True)
    return [p for p in items if p.is_dir()]


def _undo_plan(rec: dict):
    """把 manifest 变成「要做什么」的清单，同时收集冲突；不动任何文件。"""
    plan, problems = [], []
    for ch in rec.get("changes", []):
        dest = Path(ch.get("dest", ""))
        act = ch.get("action")
        item = {"change": ch, "dest": dest, "act": act, "backup": Path(ch["backup"]) if ch.get("backup") else None}
        if act == "replaced":
            if not item["backup"] or not item["backup"].exists():
                problems.append(f"{ch.get('name')}：旧版本的备份不见了（{item['backup']}），"
                                "现在还原会把当前版本换成空的")
            else:
                dg = ch.get("backup_digest")
                if dg and tree_digest(item["backup"]) != dg:
                    problems.append(f"{ch.get('name')}：备份内容被改动过，不能当作原版还原")
        if dest.exists():
            expect = ch.get("digest_after")
            if expect and tree_digest(dest) != expect:
                problems.append(f"{ch.get('name')}：装好之后这个技能被改过，"
                                "撤销会覆盖你的修改。先自己备份，或加 --force（会把当前版本另存一份）")
        plan.append(item)
    return plan, problems


def cmd_undo(backup_root: Path, skills_dir: Path, out_dir: Path, force: bool = False) -> int:
    backups = list_backups(backup_root)
    if not backups:
        print("没有可回滚的备份。")
        return EXIT_OK
    latest = backups[0]
    manifest = latest / "manifest.json"
    if not manifest.exists():
        eprint(f"最新备份 {latest.name} 没有 manifest.json，无法判断该删什么。")
        eprint("请手动检查该目录，没动你的任何文件。")
        return EXIT_ROLLBACK
    rec = json.loads(read_text(manifest))
    plan, problems = _undo_plan(rec)
    print(f"回滚 {latest.name}（{len(plan)} 项变更）")
    if rec.get("status") == "pending":
        problems.append("这批安装的 manifest 是「未完成」状态（上次可能中途失败），"
                        "撤销前请确认目标目录内容")

    if problems:
        print()
        for p in problems:
            eprint("  ✖ " + p)
        if not force:
            print()
            print("没有改动任何文件。处理办法：")
            print("  · 想保留自己的修改：先手动备份那个技能目录，再决定要不要撤")
            print("  · 确定要撤：加 --force（会先把当前版本另存到 _undo_keep_<时间戳>/）")
            return EXIT_ROLLBACK
        keep = out_dir / f"_undo_keep_{new_batch_id()}"
        for item in plan:
            dest = item["dest"]
            if dest.exists():
                keep.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copytree(dest, keep / dest.name, copy_function=shutil.copyfile)
                    print(f"  · 当前版本已另存：{keep / dest.name}")
                except OSError as e:
                    eprint(f"  ✖ 另存当前版本失败：{e}")
                    return EXIT_ROLLBACK
        print("  （--force：已把当前版本另存，继续撤销）")

    # 预检通过才动文件；顺序：先确认备份，再删目标，再放回备份
    for item in plan:
        dest, act, bdir = item["dest"], item["act"], item["backup"]
        try:
            if act == "replaced":
                if not bdir or not bdir.exists():
                    eprint(f"  ✖ {dest.name}：备份在检查之后消失了，已停止，其余未动")
                    return EXIT_ROLLBACK
                if dest.exists():
                    shutil.rmtree(wp(dest), ignore_errors=False)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(bdir), str(dest))
                print(f"  ✔ 已还原：{dest.name}")
            elif act == "added":
                if dest.exists():
                    shutil.rmtree(wp(dest))
                    print(f"  ✔ 已删除新增：{dest.name}")
                else:
                    print(f"  · 已不存在：{dest.name}")
        except PermissionError as e:
            eprint(f"  ✖ 没有权限动 {dest}：{e}")
            eprint("     技能目录被别的程序占用，或当前账号没有写权限。")
            eprint("     关掉占用者再试；被限制在工作区时用 --skills-dir 指到工作区目录。")
            return EXIT_ROLLBACK
        except OSError as e:
            eprint(f"  ✖ 撤销 {dest.name} 时出错：{e}")
            return EXIT_ROLLBACK

    bad = [item["dest"].name for item in plan
           if (item["act"] == "replaced" and not item["dest"].exists())]
    if bad:
        eprint("✖ 回滚不干净：" + "、".join(bad) + "（其余已处理，备份目录保留）")
        return EXIT_ROLLBACK
    shutil.rmtree(latest, ignore_errors=True)
    print("\n✔ 回滚完成，备份目录已清理。")
    return EXIT_OK

# ---------------------------------------------------------------- 报告（HTML）

HTML_HEAD = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>技能转换报告 · __TITLE__</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--line:#2a2f3a;--fg:#e6e8ee;--dim:#9aa3b2;
--ok:#3ddc97;--bad:#ff6b6b;--warn:#ffb454;--accent:#6aa9ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:32px 0 10px;color:var(--dim);
letter-spacing:.04em}
.sub{color:var(--dim);font-size:13px;margin:0 0 24px}
.banner{display:flex;gap:14px;align-items:flex-start;padding:18px 20px;border-radius:12px;
background:var(--panel);border:1px solid var(--line);margin-bottom:8px}
.banner .icon{font-size:26px;line-height:1.2}
.banner.ok{border-color:#1f5a45}.banner.bad{border-color:#5a2222}.banner.dry{border-color:#2b3f66}
.banner .t{font-size:17px;font-weight:600;margin-bottom:2px}
.banner .d{color:var(--dim);font-size:13.5px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin:10px 0}
.kv{display:grid;grid-template-columns:110px 1fr;gap:6px 12px;font-size:14px}
.kv .k{color:var(--dim)}
.mono,code,pre{font-family:ui-monospace,Consolas,"Cascadia Mono",monospace;font-size:13px}
pre{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:14px 16px;
overflow:auto;max-height:420px;color:#cfd6e3;margin:0}
ul.check{list-style:none;padding:0;margin:0}
ul.check li{display:flex;gap:10px;padding:7px 0;border-bottom:1px dashed var(--line);font-size:14px}
ul.check li:last-child{border-bottom:0}
.mark{width:18px;flex:0 0 18px;text-align:center}
.ok .mark,.mark.ok{color:var(--ok)}.bad .mark,.mark.bad{color:var(--bad)}.mark.warn{color:var(--warn)}
.note{color:var(--dim);font-size:13px}
.tree{font-family:ui-monospace,Consolas,monospace;font-size:13px;white-space:pre;color:#c6cede}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;border:1px solid var(--line);color:var(--dim)}
.badge.on{color:var(--warn);border-color:#5a4520}
.toolbar{position:sticky;top:0;z-index:5;display:flex;gap:10px;flex-wrap:wrap;align-items:center;
background:rgba(15,17,21,.93);backdrop-filter:blur(8px);padding:10px 0;margin:0 0 8px;
border-bottom:1px solid var(--line)}
.toolbar input{flex:1 1 220px;min-width:170px;background:var(--panel2);border:1px solid var(--line);
border-radius:9px;padding:8px 12px;color:var(--fg);font-size:14px;outline:none}
.toolbar input:focus{border-color:#3d5a86}
.chip{border:1px solid var(--line);background:transparent;color:var(--dim);border-radius:999px;
padding:5px 12px;font-size:13px;cursor:pointer}
.chip[aria-pressed="true"]{background:#1c2534;color:var(--fg);border-color:#3d5a86}
.count{color:var(--dim);font-size:13px;margin-left:auto;white-space:nowrap}
details.skill-item>summary{cursor:pointer;list-style:none;display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
details.skill-item>summary::-webkit-details-marker{display:none}
summary .name{font-size:16px;font-weight:600}
summary .caret{color:var(--dim);transition:transform .15s;display:inline-block}
details[open] summary .caret{transform:rotate(90deg)}
summary .num{color:var(--dim);font-size:13px}
.skill-body{margin-top:10px}
#no-match{display:none;color:var(--dim);font-size:14px;padding:14px 2px}
#toTop{position:fixed;right:22px;bottom:22px;width:40px;height:40px;border-radius:50%;
background:var(--panel2);border:1px solid var(--line);color:var(--dim);cursor:pointer;
font-size:16px;display:none;z-index:9}
#toTop:hover{color:var(--fg)}
footer{margin-top:40px;color:var(--dim);font-size:12.5px;border-top:1px solid var(--line);padding-top:16px}
a{color:var(--accent)}
</style></head><body><div class="wrap">
"""

TOOLBAR_HTML = """<div class="toolbar" id="toolbar">
  <input id="q" type="search" placeholder="搜技能名、描述、路径…（按 / 聚焦）" autocomplete="off">
  __CHIPS__
  <span class="count" id="count"></span>
</div>"""


def build_toolbar(skills: list, comp_map: dict) -> str:
    chips = [('<button class="chip" data-mode="all" aria-pressed="true">全部</button>')]
    if any(s.get("manual_only") for s in skills):
        chips.append('<button class="chip" data-mode="manual" aria-pressed="false">仅手动</button>')
    if any(not s.get("manual_only") for s in skills):
        chips.append('<button class="chip" data-mode="auto" aria-pressed="false">可自动调用</button>')
    if any(comp_map.get(s["name"], {}).get("saved_pct", 0) >= 5 for s in skills):
        chips.append('<button class="chip" data-mode="compact" aria-pressed="false">省 ≥5%</button>')
    return TOOLBAR_HTML.replace("__CHIPS__", "\n  ".join(chips))

REPORT_JS = """
<script>
(function(){
  var q=document.getElementById('q');
  var list=document.getElementById('skill-list');
  if(!list) return;
  var items=[].slice.call(document.querySelectorAll('.skill-item'));
  var chips=[].slice.call(document.querySelectorAll('.chip'));
  var count=document.getElementById('count');
  var nomatch=document.getElementById('no-match');
  var mode='all';
  function apply(){
    var t=(q&&q.value||'').trim().toLowerCase();
    var n=0;
    items.forEach(function(el){
      var hay=(el.getAttribute('data-search')||'').toLowerCase();
      var okText=!t||hay.indexOf(t)>=0;
      var a=el.getAttribute('data-auto');
      var c=el.getAttribute('data-compact');
      var okMode=(mode==='all')||(mode==='auto'&&a==='1')||(mode==='manual'&&a==='0')||(mode==='compact'&&c==='1');
      var ok=okText&&okMode;
      el.style.display=ok?'':'none';
      if(ok) n++;
    });
    if(count) count.textContent=n+' / '+items.length+' 个';
    if(nomatch) nomatch.style.display=n?'none':'block';
  }
  chips.forEach(function(b){
    b.addEventListener('click',function(){
      mode=b.getAttribute('data-mode');
      chips.forEach(function(x){x.setAttribute('aria-pressed', x===b?'true':'false')});
      apply();
    });
  });
  if(q) q.addEventListener('input',apply);
  document.addEventListener('keydown',function(e){
    if(e.key==='/'&&q&&document.activeElement!==q){e.preventDefault();q.focus();}
    if(e.key==='Escape'&&q&&document.activeElement===q){q.value='';apply();q.blur();}
  });
  apply();

  var top=document.getElementById('toTop');
  if(top){
    window.addEventListener('scroll',function(){
      top.style.display=window.scrollY>600?'block':'none';
    });
    top.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'})});
  }
})();
</script>
"""

HTML_TAIL = """
<footer>
  这份报告只用于展示，不会执行任何操作、不联网。<br>
  由 dsh-skill-converter 生成 · __TIME__
</footer></div></body></html>
"""


def esc(s):
    return html.escape(str(s))


def _compact_stats(raw: str, packed: str) -> dict:
    return {
        "before_lines": raw.count("\n"), "after_lines": packed.count("\n"),
        "before_bytes": len(raw.encode("utf-8")), "after_bytes": len(packed.encode("utf-8")),
        "before_tokens": estimate_tokens(raw), "after_tokens": estimate_tokens(packed),
        "saved_pct": (round(100 * (1 - estimate_tokens(packed) / estimate_tokens(raw)), 1)
                      if estimate_tokens(raw) else 0.0),
    }


HARD_WORDS = ("MUST", "NEVER", "ALWAYS", "IMPORTANT", "REQUIRED", "DO NOT",
              "必须", "不要", "禁止", "一定", "绝不")


def _count_hard(text: str, word: str) -> int:
    return text.count(word)


def verify_compact(original: str, compact: str):
    """改写版的红线校验：命令参数、硬性要求、步骤、frontmatter 都不能少。"""
    checks = []

    fm1, _ = parse_frontmatter(original)
    fm2, _ = parse_frontmatter(compact)
    if fm2 is None:
        checks.append({"label": "frontmatter", "ok": False, "detail": "开头缺 --- ... --- 或缺 name/description"})
    else:
        same = (fm1 and fm1.get("name") == fm2.get("name")
                and fm1.get("description") == fm2.get("description"))
        checks.append({"label": "frontmatter", "ok": bool(same),
                       "detail": "name、description 原样保留" if same else "name 或 description 被改了（会破坏技能识别和自动调用）"})

    flags_in = set(re.findall(r"(?<![\w-])--[a-z][\w-]*", original))
    flags_lost = sorted(f for f in flags_in if f not in compact)
    checks.append({"label": "命令参数", "ok": not flags_lost,
                   "detail": f"{len(flags_in)} 个参数都在" if not flags_lost
                   else "丢了：" + "、".join(flags_lost)})

    hard_lost = [w for w in HARD_WORDS if _count_hard(compact, w) < _count_hard(original, w)]
    checks.append({"label": "硬性要求", "ok": not hard_lost,
                   "detail": "MUST/NEVER/必须/不要 等一条没少" if not hard_lost
                   else "变少了：" + "、".join(hard_lost)})

    step_re = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
    n1, n2 = len(step_re.findall(original)), len(step_re.findall(compact))
    checks.append({"label": "编号步骤", "ok": n2 >= n1,
                   "detail": f"原文 {n1} 步，改写后 {n2} 步" + ("（少了就是漏内容）" if n2 < n1 else "")})

    fences1, fences2 = original.count("```"), compact.count("```")
    checks.append({"label": "示例代码", "ok": fences2 >= fences1,
                   "detail": f"代码块 {fences1 // 2} → {fences2 // 2} 个"})

    t1, t2 = estimate_tokens(original), estimate_tokens(compact)
    checks.append({"label": "确实更省", "ok": t2 < t1,
                   "detail": f"{t1} → {t2} Token" + ("（没省，没必要换）" if t2 >= t1 else "")})
    return all(c["ok"] for c in checks), checks


def verify_compact_attachment(original: str, compact: str):
    """附件 .md 的红线校验：没有 frontmatter 就不要求 frontmatter，其余照旧。"""
    checks = []

    fm1, _ = parse_frontmatter(original)
    if fm1 is not None:
        fm2, _ = parse_frontmatter(compact)
        same = bool(fm2 and fm1.get("name") == fm2.get("name")
                    and fm1.get("description") == fm2.get("description"))
        checks.append({"label": "frontmatter", "ok": same,
                       "detail": "name、description 原样保留" if same else "frontmatter 被改了"})

    flags_in = set(re.findall(r"(?<![\w-])--[a-z][\w-]*", original))
    flags_lost = sorted(f for f in flags_in if f not in compact)
    checks.append({"label": "命令参数", "ok": not flags_lost,
                   "detail": f"{len(flags_in)} 个参数都在" if not flags_lost
                   else "丢了：" + "、".join(flags_lost)})

    hard_lost = [w for w in HARD_WORDS if _count_hard(compact, w) < _count_hard(original, w)]
    checks.append({"label": "硬性要求", "ok": not hard_lost,
                   "detail": "MUST/NEVER/必须/不要 等一条没少" if not hard_lost
                   else "变少了：" + "、".join(hard_lost)})

    step_re = re.compile(r"^\s*\d+[.)]\s+\S", re.M)
    n1, n2 = len(step_re.findall(original)), len(step_re.findall(compact))
    checks.append({"label": "编号步骤", "ok": n2 >= n1,
                   "detail": f"原文 {n1} 步，压缩后 {n2} 步"})

    fences1, fences2 = original.count("```"), compact.count("```")
    checks.append({"label": "示例代码", "ok": fences2 >= fences1,
                   "detail": f"代码块 {fences1 // 2} → {fences2 // 2} 个"})

    t1, t2 = estimate_tokens(original), estimate_tokens(compact)
    checks.append({"label": "确实更省", "ok": t2 < t1, "detail": f"{t1} → {t2} Token"})
    return all(c["ok"] for c in checks), checks


ATTACH_MAX_BYTES = 256 * 1024   # 单个附件超过 256 KB 不碰（多半是数据而不是说明文字）
ATTACH_MIN_SAVED = 3.0          # 省不到 3% 就不生成，免得满地都是没用的 .compact.md


def compress_attachments(sdir: Path):
    """把技能目录里除 SKILL.md 之外的 .md 附件（refs/ 等）各压一份，只算文本、不碰图片。"""
    out = []
    for p in sorted(sdir.rglob("*.md")):
        name = p.name.lower()
        if name == "skill.md" or name.endswith(".compact.md"):
            continue
        try:
            if p.stat().st_size > ATTACH_MAX_BYTES:
                continue
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        packed, stats = compress_text(raw)
        if stats["saved_pct"] < ATTACH_MIN_SAVED:
            continue
        ok, vchecks = verify_compact_attachment(raw, packed)
        entry = {"rel": str(p.relative_to(sdir)).replace("\\", "/"),
                 "text": packed if ok else "", "verified": ok,
                 "blocked": not ok, **stats}
        if not ok:
            entry["vdetail"] = "；".join(f"{c['label']}{'✔' if c['ok'] else '✖'}"
                                        for c in vchecks if not c["ok"])
        out.append(entry)
    return out


def _compress_detail(c: dict) -> str:
    if c.get("verified"):
        return "由你（或 AI）改写；命令、参数、硬性要求、步骤已逐项校验通过"
    bits = []
    for key, label in (("removed_dup_para", "重复段落"), ("removed_dup_line", "重复行"),
                       ("removed_filler", "废话起句"), ("removed_section", "无用章节"),
                       ("removed_badge", "徽章图"), ("removed_toc", "目录项"),
                       ("removed_comment", "注释"), ("removed_blank", "多余空行"),
                       ("trimmed_example", "超长示例")):
        n = c.get(key, 0)
        if n:
            bits.append(f"{label} {n}")
    if c.get("examples"):
        bits.append("示例共省略 " + "、".join(c["examples"][:3]))
    return "、".join(bits) if bits else "几乎没动（原文已经很紧凑）"



# ---------------------------------------------------------------- 压缩（省 Token）

# 出现这些词的行/段落一律不动：多半是命令、参数、硬性要求
PROTECT_RE = re.compile(r"`|https?://|\b(MUST|NEVER|ALWAYS|IMPORTANT|REQUIRED|WARNING|NOTE)\b"
                        r"|^\s*(\d+[.)]|[-*+]\s|\|)")

# 英文里纯属占地方的起句（只在没有反引号、不含代码的行里删）
FILLER_PREFIXES = (
    "please note", "note that", "it's important to", "it is important to", "keep in mind",
    "you should note", "remember that", "as you know", "in order to", "as mentioned above",
    "this is because", "for your information", "basically", "essentially",
)

# 整节删除：这些章节对"AI 干活"没用
DROP_SECTION_RE = re.compile(
    r"^#{1,6}\s*(致谢|感谢|鸣谢|贡献者|贡献指南|作者|关于我|关于本|联系|支持我|赞助|许可|协议|"
    r"acknowledg\w*|contributors?|contact|about the author|support me|"
    r"star this|licen[cs]e|changelog|更新日志)\b", re.I)

TOC_LINE_RE = re.compile(r"^\s*[-*+]\s*\[[^\]]*\]\(#[^)]*\)\s*$")
IMAGE_LINE_RE = re.compile(r"^\s*(\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\))\s*$")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def estimate_tokens(text: str) -> int:
    """粗略估 token：中文按 1 字≈0.63 token，其余按 4 字符≈1 token。"""
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return int(round(cjk / 1.6 + (len(text) - cjk) / 3.8))


def _norm_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def compress_text(text: str, aggressive: bool = False) -> tuple[str, dict]:
    """保守压缩（默认）：只做不会改变语义的排版清理，绝不删句子、绝不截代码。

    aggressive=True 时启用原来的有损规则（删废话起句/整节/截超长示例），
    但它产出的东西必须先通过 verify_compact；过不了就不写进技能目录。
    """
    stats = {"removed_blank": 0, "removed_comment": 0, "removed_badge": 0, "removed_toc": 0,
             "removed_section": 0, "removed_dup_line": 0, "removed_dup_para": 0,
             "removed_filler": 0, "trimmed_example": 0, "examples": [], "mode": "aggressive" if aggressive else "conservative"}
    if not aggressive:
        # 只做三件事：统一行尾空白、连续空行压成一个、去掉文件末尾多余空行
        lines = []
        blank = 0
        for ln in text.split("\n"):
            ln = ln.rstrip()
            if ln == "":
                blank += 1
                if blank > 1:
                    stats["removed_blank"] += 1
                    continue
            else:
                blank = 0
            lines.append(ln)
        text2 = "\n".join(lines).strip("\n") + "\n"
        return _finish_stats(text, text2, stats)

    n_comments = len(HTML_COMMENT_RE.findall(text))
    stats["removed_comment"] = n_comments
    text = HTML_COMMENT_RE.sub("", text)

    lines = text.split("\n")
    kept: list[str] = []
    in_fence = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            kept.append(ln)
            continue
        if in_fence:
            kept.append(ln)
            continue
        if IMAGE_LINE_RE.match(ln):
            stats["removed_badge"] += 1
            continue
        if TOC_LINE_RE.match(ln):
            stats["removed_toc"] += 1
            continue
        kept.append(ln)

    # 按标题层级删整节（致谢/许可/更新日志等）
    out_lines: list[str] = []
    skipping_lvl = None
    in_fence = False
    for ln in kept:
        stripped = ln.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out_lines.append(ln)
            continue
        if in_fence:
            out_lines.append(ln)
            continue
        if stripped.startswith("#"):
            lvl = len(stripped) - len(stripped.lstrip("#"))
            if skipping_lvl is not None and lvl <= skipping_lvl:
                skipping_lvl = None
            if DROP_SECTION_RE.match(stripped):
                skipping_lvl = lvl
                continue
        if skipping_lvl is not None:
            continue
        out_lines.append(ln)
    kept = out_lines

    # 只剩标题、底下没内容的空章节
    pruned: list[str] = []
    in_fence = False
    for i, ln in enumerate(kept):
        st = ln.strip()
        if st.startswith("```"):
            in_fence = not in_fence
            pruned.append(ln)
            continue
        if in_fence or not st.startswith("#"):
            pruned.append(ln)
            continue
        j = i + 1
        while j < len(kept) and kept[j].strip() == "":
            j += 1
        if j >= len(kept) or kept[j].strip().startswith("#"):
            stats["removed_section"] += 1
            continue
        pruned.append(ln)
    kept = pruned

    # 去重行（长行、非列表、非保护行）
    seen = set()
    deduped = []
    for ln in kept:
        n = _norm_line(ln)
        if (len(n) > 40 and not PROTECT_RE.search(ln)
                and not n.startswith(("#", "-", "*", "|", ">"))):
            if n in seen:
                stats["removed_dup_line"] += 1
                continue
            seen.add(n)
        deduped.append(ln)
    kept = deduped

    collapsed, blank = [], 0
    for ln in kept:
        if ln.strip() == "":
            blank += 1
            if blank > 1:
                stats["removed_blank"] += 1
                continue
        else:
            blank = 0
        collapsed.append(ln)
    kept = collapsed

    blocks = "\n".join(kept).split("\n\n")
    seen_para = set()
    new_blocks = []
    for b in blocks:
        if not b.strip():
            continue
        st = b.strip()
        if st.startswith("---") or st.startswith("```") or st.startswith("#"):
            new_blocks.append(b)
            continue
        n = _norm_line(b)
        if len(n) > 60 and not PROTECT_RE.search(b) and "\n" not in st:
            if n in seen_para:
                stats["removed_dup_para"] += 1
                continue
            seen_para.add(n)
        kept_b = []
        for ln in b.split("\n"):
            s = ln.strip()
            low = s.lower()
            if (s and len(s) < 300 and "`" not in ln and not PROTECT_RE.search(ln)
                    and any(low.startswith(p) for p in FILLER_PREFIXES)):
                idx = s.find(",")
                rest = s[idx + 1:].strip() if 0 <= idx < len(s) - 2 else ""
                stats["removed_filler"] += 1
                if rest:
                    kept_b.append(rest[0].upper() + rest[1:])
                continue
            kept_b.append(ln)
        if not any(x.strip() for x in kept_b):
            continue
        new_blocks.append("\n".join(kept_b))
    text2 = "\n\n".join(new_blocks) + "\n"

    # 截超长示例（有损；会被 verify_compact 拦下来说明不该用）
    out = []
    in_fence = False
    block: list[str] = []
    for ln in text2.split("\n"):
        if ln.strip().startswith("```") and not in_fence:
            in_fence = True
            block = [ln]
            continue
        if ln.strip().startswith("```") and in_fence:
            in_fence = False
            block.append(ln)
            body = block[1:-1]
            if len(body) > 15:
                keep_n = 8
                dropped = len(body) - keep_n
                block = [block[0]] + body[:keep_n] + [f"…（示例省略 {dropped} 行）"] + [block[-1]]
                stats["trimmed_example"] += 1
                stats["examples"].append(f"{dropped} 行")
            out.extend(block)
            block = []
            continue
        if in_fence:
            block.append(ln)
        else:
            out.append(ln)
    if in_fence:
        out.extend(block)
    text2 = "\n".join(out)
    text2 = re.sub(r"\n{3,}", "\n\n", text2).strip() + "\n"
    return _finish_stats(text, text2, stats)


def _finish_stats(before: str, after: str, stats: dict):
    stats["before_lines"] = before.count("\n")
    stats["after_lines"] = after.count("\n")
    stats["before_bytes"] = len(before.encode("utf-8"))
    stats["after_bytes"] = len(after.encode("utf-8"))
    stats["before_tokens"] = estimate_tokens(before)
    stats["after_tokens"] = estimate_tokens(after)
    stats["saved_pct"] = (round(100 * (1 - stats["after_tokens"] / stats["before_tokens"]), 1)
                          if stats["before_tokens"] else 0.0)
    return after, stats


def _compact_stats(raw: str, packed: str) -> dict:
    return {
        "before_lines": raw.count("\n"), "after_lines": packed.count("\n"),
        "before_bytes": len(raw.encode("utf-8")), "after_bytes": len(packed.encode("utf-8")),
        "before_tokens": estimate_tokens(raw), "after_tokens": estimate_tokens(packed),
        "saved_pct": (round(100 * (1 - estimate_tokens(packed) / estimate_tokens(raw)), 1)
                      if estimate_tokens(raw) else 0.0),
    }


HARD_WORDS = ("MUST", "NEVER", "ALWAYS", "IMPORTANT", "REQUIRED", "DO NOT",
              "必须", "不要", "禁止", "一定", "绝不")

FENCE_RE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.S | re.M)


def _count_hard(text: str, word: str) -> int:
    return text.count(word)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _code_blocks(text: str):
    """取出所有 fenced code block 的正文（忽略围栏语言标记），用于逐字节比对。"""
    blocks = []
    for b in FENCE_RE.findall(text):
        blocks.append("\n".join(line.rstrip() for line in b.strip("\n").splitlines()))
    return blocks


def _hard_sentences(text: str):
    """含 MUST/必须 等硬性要求的行，去掉空白差异后作为「必须逐字保留」的单位。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("```"):
            continue
        if any(w in s for w in HARD_WORDS):
            n = _norm_ws(s)
            if len(n) >= 8:
                out.append(n)
    return out


def _step_lines(text: str):
    return [_norm_ws(line) for line in text.splitlines()
            if re.match(r"^\s*\d+[.)]\s+\S", line)]


def _fm_values(fm: dict):
    return {k: str(v).strip() for k, v in (fm or {}).items() if k != "__raw__"}


def verify_compact(original: str, compact: str, require_frontmatter: bool = True):
    """结构初筛（不是语义等价证明）：frontmatter/参数/硬性句子/步骤/代码块都要原样在。

    - frontmatter：所有字段（含 disable-model-invocation 这类控制字段）值必须一致
    - 命令参数：按完整 token 比对（--name 不会因为 --namespace 存在就算「还在」）
    - 硬性要求：原文每条含 MUST/必须 的句子，必须在改写版里逐字找到
    - 编号步骤：每条原步骤也要能逐字找到（不许只数条数）
    - 代码块：逐字节相同（只容忍行尾空白差异）
    """
    checks = []
    fm1, _ = parse_frontmatter(original)
    fm2, _ = parse_frontmatter(compact)
    if require_frontmatter or fm1 is not None:
        if fm2 is None:
            checks.append({"label": "frontmatter", "ok": False,
                           "detail": "开头缺 --- ... --- 或缺 name/description"})
        else:
            v1, v2 = _fm_values(fm1), _fm_values(fm2)
            diffs = sorted(k for k in set(v1) | set(v2) if v1.get(k) != v2.get(k))
            checks.append({
                "label": "frontmatter", "ok": not diffs,
                "detail": "所有字段（含控制字段）原样保留" if not diffs
                else "被改了：" + "、".join(diffs)})

    flags1 = set(re.findall(r"(?<![\w-])--[a-z][\w-]*", original))
    flags2 = set(re.findall(r"(?<![\w-])--[a-z][\w-]*", compact))
    lost = sorted(flags1 - flags2)
    checks.append({"label": "命令参数", "ok": not lost,
                   "detail": f"{len(flags1)} 个参数按完整名字比对，都在" if not lost
                   else "丢了：" + "、".join(lost)})

    hard = _hard_sentences(original)
    ncompact = _norm_ws(compact)
    missing_hard = [s for s in hard if s not in ncompact]
    checks.append({"label": "硬性要求", "ok": not missing_hard,
                   "detail": f"{len(hard)} 条硬性要求原句都在" if not missing_hard
                   else f"{len(missing_hard)} 条被改了/删了（初筛只看字面，改写过就不能用）"})

    steps = _step_lines(original)
    missing_steps = [s for s in steps if s not in ncompact]
    checks.append({"label": "编号步骤", "ok": not missing_steps,
                   "detail": f"{len(steps)} 条步骤都在" if not missing_steps
                   else f"{len(missing_steps)} 条步骤找不到（不许只数条数）"})

    cb1 = _code_blocks(original)
    cb2 = _code_blocks(compact)
    missing_cb = [b for b in cb1 if b not in cb2]
    checks.append({"label": "示例代码", "ok": not missing_cb,
                   "detail": f"{len(cb1)} 个代码块逐字节一致" if not missing_cb
                   else f"{len(missing_cb)} 个代码块被改过（命令/代码必须原样）"})

    t1, t2 = estimate_tokens(original), estimate_tokens(compact)
    checks.append({"label": "确实更省", "ok": t2 < t1,
                   "detail": f"{t1} → {t2} Token" + ("（没省，没必要换）" if t2 >= t1 else "")})
    return all(c["ok"] for c in checks), checks


def _blocking(checks):
    """哪些初筛项真的会阻止落地。「确实更省」只算提示：不省就别用，但不算改坏。"""
    return [c for c in checks if not c["ok"] and c["label"] != "确实更省"]


def verify_compact_attachment(original: str, compact: str):
    """附件 .md：没有 frontmatter 就不查 frontmatter，其余规则与 SKILL.md 相同。"""
    return verify_compact(original, compact, require_frontmatter=False)


ATTACH_MAX_BYTES = 256 * 1024   # 单个附件超过 256 KB 不碰（多半是数据而不是说明文字）
ATTACH_MIN_SAVED = 3.0          # 省不到 3% 就不生成，免得满地都是没用的 .compact.md


def compress_attachments(sdir: Path):
    """把技能目录里除 SKILL.md 之外的 .md 附件（refs/ 等）各压一份，只算文本、不碰图片。"""
    out = []
    for p in sorted(sdir.rglob("*.md")):
        name = p.name.lower()
        if name == "skill.md" or name.endswith(".compact.md"):
            continue
        try:
            if p.stat().st_size > ATTACH_MAX_BYTES:
                continue
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        packed, stats = compress_text(raw)
        if stats["saved_pct"] < ATTACH_MIN_SAVED:
            continue
        ok, vchecks = verify_compact_attachment(raw, packed)
        entry = {"rel": str(p.relative_to(sdir)).replace("\\", "/"),
                 "text": packed if ok else "", "verified": ok,
                 "blocked": not ok, **stats}
        if not ok:
            entry["vdetail"] = "；".join(f"{c['label']}{'✔' if c['ok'] else '✖'}"
                                        for c in vchecks if not c["ok"])
        out.append(entry)
    return out


def _compress_detail(c: dict) -> str:
    if c.get("verified"):
        return "由你（或 AI）改写；命令、参数、硬性要求、步骤已逐项校验通过"
    bits = []
    for key, label in (("removed_dup_para", "重复段落"), ("removed_dup_line", "重复行"),
                       ("removed_filler", "废话起句"), ("removed_section", "无用章节"),
                       ("removed_badge", "徽章图"), ("removed_toc", "目录项"),
                       ("removed_comment", "注释"), ("removed_blank", "多余空行"),
                       ("trimmed_example", "超长示例")):
        n = c.get(key, 0)
        if n:
            bits.append(f"{label} {n}")
    if c.get("examples"):
        bits.append("示例共省略 " + "、".join(c["examples"][:3]))
    return "、".join(bits) if bits else "几乎没动（原文已经很紧凑）"


def build_report_html(data: dict) -> str:
    title = {"ok": "安装成功", "dry-run": "预演（未写盘）", "rejected": "已拒绝", "output": "已输出"}.get(data["status"], "报告")
    cls = {"ok": "ok", "dry-run": "dry", "rejected": "bad", "output": "ok"}.get(data["status"], "dry")
    icon = {"ok": "✔", "dry-run": "◐", "rejected": "✖", "output": "✔"}.get(data["status"], "•")

    parts = [HTML_HEAD.replace("__TITLE__", esc(data.get("skill_name") or "转换"))]
    parts.append(f'<h1>技能转换报告</h1><p class="sub">{esc(data.get("generated_at", ""))} · '
                 f'来源 <span class="mono">{esc(data.get("source", ""))}</span></p>')
    parts.append(f'<div class="banner {cls}"><div class="icon">{icon}</div><div>'
                 f'<div class="t">{esc(data.get("headline", title))}</div>'
                 f'<div class="d">{esc(data.get("reason", ""))}</div></div></div>')

    comp_map = {c["name"]: c for c in data.get("compression", [])}
    attach_map: dict[str, list] = {}
    for c in data.get("attach_compression", []):
        attach_map.setdefault(c.get("skill", ""), []).append(c)

    if data.get("skills"):
        many = len(data["skills"]) > 5
        parts.append(f"<h2 id='skills'>技能（{len(data['skills'])} 个）</h2>")
        if len(data["skills"]) > 1:
            parts.append(build_toolbar(data["skills"], comp_map))
        parts.append("<div id='skill-list'>")
        for s in data["skills"]:
            c = comp_map.get(s["name"])
            a = attach_map.get(s["name"], [])
            badges = []
            badges.append("<span class='badge on'>仅手动</span>" if s.get("manual_only")
                          else "<span class='badge'>AI 可自动调用</span>")
            if c:
                sp = c.get("saved_pct", 0)
                if sp >= 5:
                    badges.append(f"<span class='badge' style='background:#1f2b1f;color:#7fe0a8'>压缩省 {sp}%</span>")
                elif sp > 0:
                    badges.append(f"<span class='badge'>压缩 {sp}%</span>")
            if a:
                saved = sum(x["before_tokens"] - x["after_tokens"] for x in a)
                badges.append(f"<span class='badge' style='background:#1f2b1f;color:#7fe0a8'>附件省 {saved} Token</span>")
            if s.get("scripts"):
                badges.append(f"<span class='badge on'>含 {len(s['scripts'])} 个脚本</span>")

            body = [f"<div class='skill-body'>"]
            body.append(f"<div style='color:#cfd6e3;margin-bottom:10px'>{esc(s['description'])}</div>")
            body.append("<div class='kv'>"
                        f"<div class='k'>正文</div><div>{s['lines']} 行 / {human_size(s['bytes'])}</div>"
                        f"<div class='k'>原位置</div><div class='mono'>{esc(s['source_path'])}</div>"
                        f"<div class='k'>安装到</div><div class='mono'>{esc(s.get('dest', '（未安装）'))}</div>"
                        "</div>")
            if c:
                body.append("<div class='note' style='margin-top:8px'>"
                            f"压缩：{c['before_tokens']} → {c['after_tokens']} Token（省 {c['saved_pct']}%）"
                            + ("　已校验红线" if c.get("verified") else "")
                            + f"<br>{esc(_compress_detail(c))}</div>")
            if a:
                rows = "".join(
                    f"<div class='mono' style='font-size:12.5px'>{esc(x['rel'])}　"
                    f"{x['before_tokens']} → {x['after_tokens']}（省 {x['saved_pct']}%）"
                    + ("　✖ 未通过校验，没落地" if x.get("blocked") else "")
                    + "</div>" for x in a)
                body.append(f"<div class='note' style='margin-top:8px'>附件压缩：</div>{rows}")
            if s.get("scripts"):
                body.append("<div class='note' style='margin-top:8px'>随包复制的脚本（不会执行）："
                            + "、".join(esc(x) for x in s["scripts"]) + "</div>")
            body.append("</div>")

            search = " ".join([s["name"], s.get("description", ""), s["source_path"],
                               s.get("dest", "")]).lower()
            parts.append(
                f"<details class='card skill-item' data-search=\"{esc(search)}\" "
                f"data-auto='{0 if s.get('manual_only') else 1}' "
                f"data-compact='{1 if (c and c.get('saved_pct', 0) >= 5) else 0}'"
                + ("" if many else " open") + ">"
                f"<summary><span class='caret'>▸</span><span class='name'>{esc(s['name'])}</span>"
                + "".join(badges)
                + f"<span class='num' style='margin-left:auto'>{s['lines']} 行 · {human_size(s['bytes'])}</span>"
                "</summary>" + "".join(body) + "</details>")
        parts.append("</div>")
        if len(data["skills"]) > 1:
            parts.append("<div id='no-match'>没有匹配的技能，换个词试试（Esc 清空）。</div>")

    if data.get("scripts"):
        parts.append("<h2>脚本（已复制，绝不执行）</h2><div class='card'>"
                     "<p class='note' style='margin:0 0 10px'>你用了 <span class='mono'>--allow-scripts</span>，"
                     "下面这些脚本被原样复制进技能目录。本工具不运行它们，但 AI 之后可能自己调用——"
                     "装之前请确认来源可信。</p><ul class='check'>")
        for x in data["scripts"]:
            for f in x["files"]:
                parts.append(f'<li><span class="mark warn">!</span><span class="mono">'
                             f'{esc(x["name"])}/{esc(f)}</span></li>')
        parts.append("</ul></div>")

    if data.get("code_files"):
        parts.append("<h2>辅助代码（原样复制，绝不执行）</h2><div class='card'>"
                     "<p class='note' style='margin:0 0 10px'>很多技能自带 Python / JS 小脚本当"
                     "「工具」用，少了它们技能就干不了活，所以原样复制了。本工具不运行、不联网验证它们——"
                     "装之前请确认来源可信。</p><ul class='check'>")
        for x in data["code_files"]:
            for f in x["files"]:
                parts.append(f'<li><span class="mark warn">!</span><span class="mono">'
                             f'{esc(x["name"])}/{esc(f)}</span></li>')
        parts.append("</ul></div>")

    if data.get("compression"):
        parts.append("<h2>压缩对比</h2>")
        for c in data["compression"]:
            saved = c.get("saved_pct", 0)
            verified = (" <span class='badge'>已校验红线</span>" if c.get("verified") else "")
            parts.append(
                f'<div class="card"><div style="font-size:16px;font-weight:600;margin-bottom:8px">'
                f'{esc(c["name"])} <span class="badge" style="background:#1f2b1f;color:#7fe0a8">省 {saved}% Token</span>'
                f'{verified}</div>'
                f'<div class="kv">'
                f'<div class="k">行数</div><div>{c["before_lines"]} → {c["after_lines"]}</div>'
                f'<div class="k">体积</div><div>{human_size(c["before_bytes"])} → {human_size(c["after_bytes"])}</div>'
                f'<div class="k">估算 Token</div><div>{c["before_tokens"]} → <b>{c["after_tokens"]}</b>'
                f'（省 {c["before_tokens"] - c["after_tokens"]}）</div>'
                f'<div class="k">删了什么</div><div class="note">{esc(_compress_detail(c))}</div>'
                f'</div></div>')
        parts.append("<div class='card'><p class='note' style='margin:0'>"
                     "压缩只动重复内容、空行、徽章图、目录和超长示例；命令名、参数、前提条件、"
                     "失败怎么办、步骤顺序一个字都没改。原文保留在 SKILL.md，压缩版另存为 SKILL.compact.md。</p></div>")

    if data.get("attach_compression"):
        parts.append("<h2>附件压缩（refs/ 等 .md）</h2><div class='card'>"
                     "<p class='note' style='margin:0 0 10px'>附件只压 <b>.md 文本</b>，"
                     "图片、csv、pdf 一律不碰；压缩版另存为 <span class='mono'>原名.compact.md</span>，"
                     "原文件一个字节都没改。</p>"
                     "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
                     "<tr style='color:#8b95a7;text-align:left'>"
                     "<th style='padding:6px 8px'>文件</th><th style='padding:6px 8px'>Token</th>"
                     "<th style='padding:6px 8px'>省</th><th style='padding:6px 8px'>校验</th></tr>")
        for c in data["attach_compression"]:
            mark = ("<span class='badge' style='background:#2b1f1f;color:#ff9c9c'>未通过，没落地</span>"
                    if c.get("blocked") else "<span class='badge'>已校验红线</span>")
            parts.append(
                f"<tr style='border-top:1px solid #1d2330'>"
                f"<td style='padding:6px 8px' class='mono'>{esc(c['skill'])}/{esc(c['rel'])}</td>"
                f"<td style='padding:6px 8px'>{c['before_tokens']} → {c['after_tokens']}</td>"
                f"<td style='padding:6px 8px'>{c['saved_pct']}%</td>"
                f"<td style='padding:6px 8px'>{mark}"
                + (f"<div class='note'>{esc(c.get('vdetail',''))}</div>" if c.get("vdetail") else "")
                + "</td></tr>")
        parts.append("</table></div>")

    if data.get("skipped"):
        parts.append("<h2>跳过</h2><div class='card'><p class='note' style='margin:0 0 10px'>"
                     "下面这些技能没通过检查，没有复制、没有安装。其余技能照常转换。</p>"
                     "<ul class='check'>")
        for x in data["skipped"]:
            parts.append(f'<li><span class="mark bad">✖</span><span><b>{esc(x["name"])}</b>'
                         f'<br><span class="note">{esc(x["reason"])}'
                         + (f'｜{esc(x["detail"])}' if x.get("detail") else "")
                         + f'</span><span class="mono"> </span></span></li>')
        parts.append("</ul></div>")

    parts.append("<h2>检查清单</h2><div class='card'><ul class='check'>")
    for c in data.get("checks", []):
        mark_cls = "ok" if c["ok"] else "bad"
        mark = "✔" if c["ok"] else "✖"
        parts.append(f'<li><span class="mark {mark_cls}">{mark}</span><span><b>{esc(c["label"])}</b>'
                     f'<br><span class="note">{esc(c.get("detail", ""))}</span></span></li>')
    parts.append("</ul></div>")

    if data.get("warnings"):
        parts.append("<h2>提示</h2><div class='card'><ul class='check'>")
        for w in data["warnings"]:
            parts.append(f'<li><span class="mark warn">!</span><span>{esc(w)}</span></li>')
        parts.append("</ul></div>")

    if data.get("tree"):
        parts.append("<h2>包结构</h2><div class='card'><div class='tree'>")
        parts.append(esc("\n".join(data["tree"])))
        parts.append("</div></div>")

    if data.get("changes"):
        parts.append("<h2>变更</h2><div class='card'><div class='kv'>")
        for ch in data["changes"]:
            act = {"added": "新增", "replaced": "覆盖（已备份）", "would-add": "将新增",
                   "would-replace": "将覆盖"}.get(ch["action"], ch["action"])
            parts.append(f'<div class="k">{esc(act)}</div><div class="mono">{esc(ch["dest"])}</div>')
            if ch.get("backup"):
                parts.append(f'<div class="k">备份</div><div class="mono">{esc(ch["backup"])}</div>')
        parts.append("</div></div>")

    if data.get("undo"):
        parts.append("<h2>怎么撤销</h2><div class='card'><p class='note' style='margin:0 0 10px'>"
                     "复制下面这行命令执行，即可还原到转换之前：</p>"
                     f'<pre>{esc(data["undo"])}</pre></div>')

    parts.append(HTML_TAIL.replace("__TIME__", esc(data.get("generated_at", ""))))
    parts.append(REPORT_JS)
    parts.append("<button id='toTop' title='回到顶部'>↑</button>")
    return "".join(parts)


def write_report(data: dict, out: Path):
    out.write_text(build_report_html(data), encoding="utf-8")


# ---------------------------------------------------------------- 主流程

def analyze(src_arg: str, workdir: Path):
    """返回 (根目录, 来源描述, 统计, 是否本地目录)"""
    p = Path(src_arg).expanduser()
    if p.exists():
        if p.is_dir():
            return p, f"本地目录 {p}", copy_dir(p, p), True
        if p.is_file():
            size = p.stat().st_size
            if size > MAX_ARCHIVE_BYTES:
                raise Rejected("压缩包超过 5 MB", f"{human_size(size)}；请先精简再转")
            dest = workdir / "unpacked"
            stats = safe_extract(p, dest)
            return dest, f"本地压缩包 {p.name}", stats, False
        raise Rejected("来源既不是文件也不是目录", str(p))
    if is_url(src_arg):
        kind, archive, desc, subdir, n = fetch_github(src_arg, workdir)
        dest = workdir / "unpacked"
        stats = safe_extract(archive, dest)
        if subdir:
            sub = dest / subdir
            if not sub.is_dir():
                # tarball 顶层通常有 <repo>-<ref>/ 前缀
                cands = [d for d in dest.iterdir() if d.is_dir()]
                if len(cands) == 1 and (cands[0] / subdir).is_dir():
                    sub = cands[0] / subdir
                else:
                    raise Rejected("GitHub 仓库里找不到指定子目录", subdir)
            return sub, desc, stats, False
        return dest, desc, stats, False
    raise Rejected("找不到这个来源", f"{src_arg}\n可以是：本地 zip、本地目录、github.com/<owner>/<repo>")


def file_ext(p: Path) -> str:
    n = p.name.lower()
    if n.startswith(".") and "." not in n[1:]:
        return n  # .gitignore 这类
    return p.suffix.lower()


def scan_files(root: Path):
    """返回 (文件列表, 危险文件列表, 未知类型列表)"""
    files, dangerous, unknown = [], [], []
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        files.append(p)
        ext = file_ext(p)
        if ext in DANGEROUS_EXTS:
            dangerous.append(p)
        elif ext and ext not in KNOWN_EXTS:
            unknown.append(p)
    return files, dangerous, unknown


def convert(args) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dsh-skill-conv-"))
    report_dir = APP_HOME
    skills_dir = resolve_skills_dir(args.skills_dir)
    backup_root = APP_HOME
    data = {
        "status": "ok",
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": args.source,
        "skills": [],
        "checks": [],
        "warnings": [],
        "changes": [],
        "tree": [],
        "undo": "",
        "compression": [],
        "attach_compression": [],
        "skipped": [],
        "scripts": [],
        "code_files": [],
    }
    try:
        root, desc, stats, is_dir = analyze(args.source, workdir)
        data["source"] = desc

        files, dangerous, unknown = scan_files(root)
        rel_paths = [p.relative_to(root).as_posix() for p in files]
        data["tree"] = render_tree(build_tree(rel_paths)) if len(rel_paths) <= 400 else rel_paths[:400]

        checks = [
            {"label": "体积", "ok": stats["bytes"] <= MAX_EXTRACT_BYTES,
             "detail": f"{human_size(stats['bytes'])}（上限 {human_size(MAX_EXTRACT_BYTES)}）"},
            {"label": "文件数", "ok": stats["files"] <= MAX_FILES,
             "detail": f"{stats['files']} 个（上限 {MAX_FILES}）"},
            {"label": "路径安全", "ok": True, "detail": "没有 ../ 穿越、绝对路径、加密条目"},
        ]
        if stats.get("links"):
            links = stats["links"]
            data["warnings"].append(
                f"已跳过 {len(links)} 个符号链接（不会复制、也不会跟随）：" + "、".join(links[:5])
                + ("…" if len(links) > 5 else ""))

        skill_roots = find_skill_roots(root)
        if not skill_roots:
            raise Rejected("没有找到 SKILL.md", "这不是一个技能包（或结构不对）。技能包需要 <名字>/SKILL.md", checks)

        checks.append({"label": "技能识别", "ok": True,
                       "detail": f"找到 {len(skill_roots)} 个：{', '.join(s['name'] for s in skill_roots[:12])}"})

        picked = skill_roots
        if args.name:
            picked = [s for s in skill_roots if s["name"] == args.name]
            if not picked:
                raise Rejected(f"没有名为 {args.name} 的技能", "可选：" + ", ".join(s["name"] for s in skill_roots), checks)
        elif len(skill_roots) > 1 and not args.all:
            raise Rejected(
                "这个包里包含多个技能，需要指定一个或全部转换",
                f"共 {len(skill_roots)} 个：" + ", ".join(s["name"] for s in skill_roots[:20]) +
                "\n用 --name <名字> 挑一个，或加 --all 一次转全部", checks)
        if len(picked) > MAX_BATCH_SKILLS:
            raise Rejected(f"一次最多转 {MAX_BATCH_SKILLS} 个技能",
                           f"这个包有 {len(picked)} 个，请拆包或分批", checks)

        # 只检查「被选中的技能目录」，仓库别处的脚本不会复制、也无所谓
        # 批量模式下，单个技能有问题就跳过它、继续转其余的
        batch = len(picked) > 1
        for s in picked:
            try:
                s_files, s_danger, s_unknown = scan_files(s["dir"])
                s_bin = [p for p in s_danger if p.suffix.lower() in BINARY_EXTS]
                s_scr = [p for p in s_danger if p.suffix.lower() in SCRIPT_EXTS]
                if s_bin:
                    raise Rejected(
                        "含可执行文件（二进制永远不复制、不执行）",
                        "、".join(p.relative_to(s["dir"]).as_posix() for p in s_bin[:8]), [])
                if s_scr and not args.allow_scripts:
                    raise Rejected(
                        "含脚本文件（默认不复制）",
                        "、".join(p.relative_to(s["dir"]).as_posix() for p in s_scr[:8])
                        + "\n要装这类技能：加 --allow-scripts（脚本会原样复制，但本工具绝不执行它们）", [])
                for p in s_unknown[:10]:
                    data["warnings"].append(f"{s['name']}：未知扩展名，已保留但请自行确认："
                                            f"{p.relative_to(s['dir']).as_posix()}")
                s["unknown_types"] = bool(s_unknown)
                s["scripts"] = [p.relative_to(s["dir"]).as_posix() for p in s_scr]
                s_code = [p for p in s_files if p.suffix.lower() in CODE_EXTS]
                s["code_files"] = [p.relative_to(s["dir"]).as_posix() for p in s_code]
                if s["code_files"]:
                    data["code_files"].append({"name": s["name"], "files": s["code_files"]})
                md = s["dir"] / "SKILL.md"
                text = md.read_text(encoding="utf-8", errors="replace")
                fm, err = parse_frontmatter(text)
                if fm is None:
                    raise Rejected("SKILL.md 不合法", err, [])
                if fm["name"] != s["name"]:
                    okn, why = check_skill_name(str(fm["name"]))
                    if not okn:
                        raise Rejected(
                            f"frontmatter 里的 name（{fm['name']}）不能用",
                            why, [])
                    if len(picked) == 1:
                        data["warnings"].append(
                            f"包里的目录名（{s['name']}）和 frontmatter 的 name（{fm['name']}）不一致，"
                            f"已按 frontmatter 归一化成 {fm['name']}（DSH 只认这一层）")
                        s["name"] = str(fm["name"])
                    else:
                        raise Rejected(
                            f"frontmatter 里的 name（{fm['name']}）与目录名（{s['name']}）不一致",
                            "批量转换时无法判断该用哪个；让作者改名，或用 --name 单独装这一个", [])
                else:
                    okn, why = check_skill_name(str(fm["name"]))
                    if not okn:
                        raise Rejected(f"技能名不能用：{fm['name']}", why, [])
            except Rejected as r:
                if not batch:
                    raise Rejected(f"技能 {s['name']}：{r.reason}", r.detail, checks)
                data["skipped"].append({"name": s["name"], "reason": r.reason,
                                        "detail": r.detail, "source_path": s["rel"]})
                continue
            with_scripts = s["scripts"]
            if with_scripts:
                data["scripts"].append({"name": s["name"], "files": with_scripts})
            lines = text.count("\n") + 1
            data["skills"].append({
                "name": fm["name"],
                "description": fm.get("description", ""),
                "manual_only": truthy(fm.get("disable-model-invocation", "")),
                "lines": lines,
                "bytes": len(text.encode("utf-8")),
                "source_path": s["rel"] + "/SKILL.md",
                "dir": s["dir"],
                "unknown_types": s.get("unknown_types", False),
                "scripts": with_scripts,
                "code_files": s.get("code_files", []),
            })

        if not data["skills"]:
            raise Rejected("所有技能都没通过检查，一个都没转",
                           f"共 {len(picked)} 个技能全部有问题，详见下面的清单", checks)
        # 平铺后重名会互相覆盖：装之前就拒绝，而不是让第二个悄悄盖掉第一个
        seen_names: dict[str, str] = {}
        dups = []
        for s in data["skills"]:
            if s["name"] in seen_names:
                dups.append(f"{s['name']}（{seen_names[s['name']]} 与 {s['source_path']}）")
            seen_names[s["name"]] = s["source_path"]
        if dups:
            raise Rejected(
                "有同名技能，装进去会互相覆盖",
                "重名：" + "、".join(dups) +
                "\n可以：① 先只装其中一个（--name <名字>）② 让作者把 frontmatter 的 name 改成不同值"
                " ③ 分开装到不同 --skills-dir", checks)
        if data["skipped"]:
            data["warnings"].append(
                f"有 {len(data['skipped'])} 个技能没通过检查、已跳过（其余照常转换）："
                + "、".join(x["name"] for x in data["skipped"][:10]))

        data["checks"].append({"label": "可执行文件", "ok": not data["skipped"],
                               "detail": f"所选 {len(picked)} 个技能里没有二进制文件（.exe/.dll 等永远拒绝）"
                                         + (f"；{len(data['skipped'])} 个技能有问题已跳过，见「跳过」" if data["skipped"] else "")
                                         + (f"；包内别处另有 {len(dangerous)} 个脚本，不在所选技能内，不会复制" if dangerous else "")})
        if data["scripts"]:
            total_scripts = sum(len(x["files"]) for x in data["scripts"])
            data["checks"].append({
                "label": "脚本（--allow-scripts）", "ok": True,
                "detail": f"{total_scripts} 个脚本随技能原样复制，绝不执行：" +
                          "、".join(f"{x['name']}/{f}" for x in data["scripts"] for f in x["files"][:3])})
            data["warnings"].append(
                f"这批技能带了 {total_scripts} 个脚本（.sh/.ps1 等），已按原样复制但**不会执行**；"
                "如果 AI 之后要跑它们，那是另一回事，需要你自己确认。")
        if data["code_files"]:
            total_code = sum(len(x["files"]) for x in data["code_files"])
            names = sorted({f.rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
                            for x in data["code_files"] for f in x["files"]})
            data["checks"].append({
                "label": "辅助代码", "ok": True,
                "detail": f"{total_code} 个 .{'/.'.join(names[:4])} 文件随技能原样复制，"
                          "本工具不会执行它们：" +
                          "、".join(f"{x['name']}/{f}" for x in data["code_files"] for f in x["files"][:3])})
            data["warnings"].append(
                f"这批技能带了 {total_code} 个 Python/JS 辅助代码文件，已原样复制但**没有运行**；"
                "技能靠它们干活时，是 AI 之后按说明去调用（用不用、怎么用由你决定）。")
        data["checks"].append({"label": "文件类型", "ok": True,
                               "detail": "全部是文本 / 常见素材" if not any(
                                   s.get("unknown_types") for s in data["skills"]) else "有未知类型见「提示」"})

        if args.compact_file and len(data["skills"]) > 1:
            raise Rejected("压缩版文件只能配一个技能用",
                           f"这次要转 {len(data['skills'])} 个：用 --name 挑一个再配 --compact-file，"
                           "或改用 --compress（自动给每个技能生成压缩版）", checks)
        if args.compress or args.compact_file:
            cbase = workdir / "_compact"
            for s in data["skills"]:
                raw = read_text(s["dir"] / "SKILL.md")
                if args.compact_file:
                    cp = Path(args.compact_file).expanduser()
                    if not cp.is_file():
                        raise Rejected("找不到你指定的压缩版文件", str(cp), checks)
                    packed = read_text(cp)
                    ok_all, vchecks = verify_compact(raw, packed)
                    ok = not _blocking(vchecks)
                    checks.append({
                        "label": "结构初筛（改写版）", "ok": ok,
                        "detail": "；".join(f"{c['label']}{'✔' if c['ok'] else '✖'}" for c in vchecks)})
                    data["checks"] = checks
                    if not ok:
                        raise Rejected(
                            "压缩版改坏了关键内容，拒绝使用",
                            "\n".join(f"✖ {c['label']}：{c['detail']}"
                                      for c in vchecks if not c["ok"]), checks)
                    cstats = _compact_stats(raw, packed)
                    cstats["verified"] = True
                    cstats["vchecks"] = vchecks
                else:
                    packed, cstats = compress_text(raw, aggressive=bool(getattr(args, "aggressive", False)))
                    ok_all, vchecks = verify_compact(raw, packed)
                    ok = not _blocking(vchecks)
                    cstats["verified"] = ok
                    cstats["vchecks"] = vchecks
                    checks.append({
                        "label": "结构初筛（规则压缩）", "ok": ok,
                        "detail": "；".join(f"{c['label']}{'✔' if c['ok'] else '✖'}" for c in vchecks)})
                    data["checks"] = checks
                    if not ok:
                        msg = ("规则压缩没通过结构初筛（保守模式也会拦），没有生成压缩版；原文照装。"
                               "要在省 Token 上再进一步，用 AI 改一版再用 --compact-file 交回来校验。")
                        if args.use_compact:
                            raise Rejected("规则压缩没通过结构初筛，不敢用它替换 SKILL.md", msg, checks)
                        data["warnings"].append(msg)
                        s["compact"] = cstats
                        data["compression"].append({"name": s["name"], **cstats})
                        continue
                    if cstats["saved_pct"] < 1.0 and not args.compact_file:
                        # 一个字都没省下来：不必生成一份内容一样的副本
                        data["warnings"].append(
                            "规则压缩没省下 Token（这个技能本来就紧凑），所以没有生成 SKILL.compact.md；原文照装。")
                        s["compact"] = cstats
                        data["compression"].append({"name": s["name"], **cstats})
                        continue
                s["compact_text"] = packed
                s["compact"] = cstats
                data["compression"].append({"name": s["name"], **cstats})
                if args.use_compact:
                    stage = cbase / s["name"]
                    shutil.copytree(s["dir"], stage)
                    (stage / "SKILL.md").write_text(packed, encoding="utf-8")
                    s["dir"] = stage
                    s["lines"] = packed.count("\n")
                    s["bytes"] = len(packed.encode("utf-8"))
                    s["compact_used"] = True
            if args.use_compact:
                data["warnings"].append("已按「压缩版」安装：SKILL.md 是精简后的正文，原文可在报告里对比。")
            else:
                data["warnings"].append("已生成 SKILL.compact.md（和原 SKILL.md 并排），对比满意后可用 --use-compact 换成它。")

            # 附件（refs/ 等）：只对 .md 文本做压缩，图片/数据一律不碰；永不替换原文件
            if args.compress and not getattr(args, "no_attach", False):
                data["attach_compression"] = []
                for s in data["skills"]:
                    entries = compress_attachments(s["dir"])
                    s["attach_compacts"] = entries
                    for e in entries:
                        data["attach_compression"].append({"skill": s["name"], **e})
                if data["attach_compression"]:
                    ok_n = sum(1 for x in data["attach_compression"] if not x["blocked"])
                    bad_n = len(data["attach_compression"]) - ok_n
                    tokens_b = sum(x["before_tokens"] for x in data["attach_compression"])
                    tokens_a = sum(x["after_tokens"] for x in data["attach_compression"])
                    saved = round(100 * (1 - tokens_a / tokens_b), 1) if tokens_b else 0.0
                    checks.append({"label": "附件压缩", "ok": bad_n == 0,
                                   "detail": f"{ok_n} 个 .md 附件生成了压缩版"
                                             + (f"，{bad_n} 个没通过红线校验（不落地）" if bad_n else "")
                                             + f"；合计 {tokens_b} → {tokens_a} Token（省 {saved}%）"})
                    data["checks"] = checks
                    data["warnings"].append(
                        f"附件的压缩版叫 <原名>.compact.md，和原文件并排放在一起；"
                        f"原文件一个都没动。SKILL.md 里如果写死了引用的文件名，用不用压缩版由你决定。")
                else:
                    checks.append({"label": "附件压缩", "ok": True,
                                   "detail": "没有值得压的 .md 附件（没有附件，或压缩省不到 3%）"})
                    data["checks"] = checks
            if (args.compress and not args.compact_file
                    and all(c.get("saved_pct", 0) < 5 for c in data["compression"])):
                data["warnings"].append(
                    "这个包本身写得很紧凑，规则压缩几乎没省下 Token。"
                    "想再省就得改写：让 AI（或你自己）改一版存成 SKILL.compact.md，"
                    "再用 --compact-file 指给本工具，它会先校验红线再收。")

        checks.append({"label": "frontmatter", "ok": True,
                       "detail": "name、description 都在；正文完整保留"})
        data["checks"] = checks
        if any(s["manual_only"] for s in data["skills"]):
            data["warnings"].append("这个技能设了 disable-model-invocation: true —— 装好后 AI 不会自动调，"
                                    "只能你手动 /名字 使用（本工具不擅自改这个字段）。")

        mode = "install" if args.install else "output"
        out_override = getattr(args, "output_dir", None)
        dest_root = (Path(out_override).expanduser().resolve() if out_override
                     else (skills_dir if mode == "install" else OUTPUT_DIR))
        dest_root = Path(dest_root)
        dest_note = str(dest_root)
        ts = getattr(args, "_ts", "") or new_batch_id()

        if mode == "install" and not args.dry_run and not dir_writable(dest_root):
            raise Rejected(
                "写不进 DSH 技能目录",
                f"{dest_root}\n"
                "常见原因：① Agent 被限制只能写工作区 ② 目录被占用 ③ 当前账号没有写权限\n"
                "办法：\n"
                "  · 装到工作区内：--skills-dir <你的工作区>/.dsh/skills\n"
                "  · 只输出到本地：去掉 --install（产物在程序旁边的 output/）\n"
                "  · 让程序把临时文件也放在工作区：设环境变量 DSH_SKILL_HOME=<工作区目录>", checks)

        batch_dir = None
        if not args.dry_run:
            batch_dir = _batch_dir(backup_root, ts)
            write_backup_manifest(batch_dir, {
                "status": "pending",
                "createdAt": data["generated_at"],
                "source": desc,
                "mode": mode,
                "destRoot": str(dest_root),
                "changes": [{"name": s["name"], "action": "added", "dest": str(dest_root / s["name"]),
                             "backup": ""} for s in data["skills"]],
                "note": "这批还没装完；如果程序中途出错，--undo 会按这里记录的清单修复",
            })

        for s in data["skills"]:
            target = dest_root / s["name"]
            s["dest"] = str(target)
            ch = {"name": s["name"],
                  "action": ("would-replace" if target.exists() else "would-add") if args.dry_run
                  else ("replaced" if target.exists() else "added"),
                  "dest": str(target), "backup": ""}
            if args.dry_run:
                data["changes"].append(ch)
                continue
            try:
                real, ts_used = install_skill(s, dest_root, batch_dir, mode, ts)
            except PermissionError as e:
                eprint(f"\n✖ 没有权限写 {dest_root}：{e}")
                eprint("   已经装进去的部分可以 --undo 回滚；被限制在工作区时请改用 --skills-dir。")
                raise Rejected("写不进目标目录（权限）",
                               f"{dest_root}\n{e}\n已写入的部分可用 --undo 回滚；"
                               "被限制在工作区时可加 --skills-dir <工作区>/.dsh/skills", checks)
            except OSError as e:
                raise Rejected("写目标目录失败", f"{dest_root}\n{e}", checks)
            data["changes"].append(real)
            if args.compress and not args.use_compact:
                if not s.get("compact_text"):
                    continue
                try:
                    write_text(Path(real["dest"]) / "SKILL.compact.md", s["compact_text"])
                except OSError as e:
                    data["warnings"].append(f"压缩版没写进去：{e}")
            for e in s.get("attach_compacts", []):
                if e.get("blocked") or not e.get("text"):
                    continue
                rel = e["rel"]
                dst = Path(real["dest"]) / (rel[:-3] + ".compact.md")
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_text(e["text"], encoding="utf-8")
                except OSError as ex:
                    data["warnings"].append(f"附件压缩版没写进去（{rel}）：{ex}")

        if args.dry_run:
            data["status"] = "dry-run"
            data["headline"] = "预演通过：没有写任何文件"
            data["reason"] = f"会新增/覆盖到 {dest_note}；确认无误后去掉 --dry-run 再跑一次"
            data["undo"] = ""
        else:
            write_backup_manifest(batch_dir, {
                "status": "done",
                "createdAt": data["generated_at"],
                "source": desc,
                "mode": mode,
                "destRoot": str(dest_root),
                "changes": data["changes"],
                "note": "本文件用于 --undo：核对备份与目标指纹后，删除新增 / 还原被覆盖的",
            })
            data["status"] = "ok"
            n = len(data["skills"])
            base = "安装完成" if mode == "install" else "已输出到 output/"
            data["headline"] = (base + f"：{n} 个技能" if n > 1 else base)
            if n > 1:
                data["reason"] = (f"共 {n} 个技能已复制进 DSH 技能目录，稍等一两秒会自动刷新"
                                  if mode == "install" else
                                  f"共 {n} 个技能已放进 output/，确认后用 --install 再装进技能目录")
            else:
                data["reason"] = ("已复制进 DSH 技能目录，稍等一两秒会自动刷新" if mode == "install"
                                  else "产物在 output/，确认后用 --install 再装进技能目录")
            data["undo"] = (f'"{sys.executable}" --undo' if getattr(sys, "frozen", False)
                            else f'"{sys.executable}" "{Path(__file__).resolve()}" --undo')
        code = EXIT_OK

    except Rejected as r:
        data["status"] = "rejected"
        data["headline"] = "拒绝转换：" + r.reason
        data["reason"] = r.detail
        data["checks"] = r.checks or [{"label": "拒绝原因", "ok": False, "detail": r.reason}]
        code = EXIT_REJECTED
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not args.no_report:
        sfx = getattr(args, "_suffix", "")
        if data["status"] == "rejected":
            out = report_dir / f"report-rejected{sfx}.html"
        elif args.dry_run:
            out = report_dir / f"report-dry-run{sfx}.html"
        else:
            out = report_dir / f"report{sfx}.html"
        try:
            write_report(data, out)
        except Exception as e:  # 报告失败不影响主结果
            eprint(f"（报告生成失败：{e}）")
            out = None
    else:
        out = None
    print_result(data)
    if out is not None:
        print(f"\n   报告：{out}")
        if args.open:
            try:
                os.startfile(str(out))  # type: ignore[attr-defined]
            except Exception as e:
                eprint(f"（打开报告失败：{e}）")
    return code


def print_result(data: dict):
    st = data["status"]
    print()
    if st == "rejected":
        print("✖ " + data["headline"])
        if data["reason"]:
            for line in str(data["reason"]).splitlines():
                print("   " + line)
        return
    print("✔ " + data["headline"])
    many = len(data["skills"]) > 1
    for s in data["skills"]:
        if many:
            auto = "（仅手动）" if s["manual_only"] else ""
            comp = ""
            if s.get("compact"):
                comp = f"，压缩省 {s['compact']['saved_pct']}%"
            print(f"   · {s['name']}{auto}  {s['lines']} 行 / {human_size(s['bytes'])}{comp}")
            continue
        print(f"   技能：{s['name']}")
        print(f"   描述：{s['description']}")
        print(f"   正文：{s['lines']} 行 / {human_size(s['bytes'])}")
        print(f"   自动调用：{'禁止（disable-model-invocation）' if s['manual_only'] else '允许'}")
        print(f"   原位置：{s['source_path']}")
        if s.get("dest") and st != "dry-run":
            print(f"   装到：{s['dest']}")
        if s.get("compact"):
            c = s["compact"]
            tail = ("，已按压缩版安装" if s.get("compact_used")
                    else ("，压缩版另存为 SKILL.compact.md" if s.get("compact_text")
                          else "，没生成压缩版（原文照装）"))
            print(f"   压缩：{c['before_tokens']} → {c['after_tokens']} Token（省 {c['saved_pct']}%）" + tail)
    for ch in data["changes"]:
        if ch.get("backup"):
            print(f"   备份：{ch['backup']}")
    if data["reason"]:
        print("   " + data["reason"])
    for w in data["warnings"]:
        print("   ! " + w)
    for x in data.get("skipped", []):
        print(f"   ✖ 跳过 {x['name']}：{x['reason']}")
    if data.get("scripts"):
        n = sum(len(x["files"]) for x in data["scripts"])
        print(f"   ! 随技能复制了 {n} 个脚本（--allow-scripts），不会执行")
    if data.get("code_files"):
        n = sum(len(x["files"]) for x in data["code_files"])
        print(f"   ! 随技能复制了 {n} 个辅助代码文件（.py 等），不会执行")
    if data.get("attach_compression"):
        ok = [x for x in data["attach_compression"] if not x.get("blocked")]
        bad = len(data["attach_compression"]) - len(ok)
        tb = sum(x["before_tokens"] for x in ok)
        ta = sum(x["after_tokens"] for x in ok)
        pct = round(100 * (1 - ta / tb), 1) if tb else 0.0
        print(f"   附件：{len(ok)} 个 .md 生成了压缩版（{tb} → {ta} Token，省 {pct}%）"
              + (f"；{bad} 个没通过校验" if bad else ""))
    if data.get("undo"):
        print(f"\n   撤销：{data['undo']}")


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="convert.py",
        description="把外部 AI 技能包（zip / 本地目录 / GitHub 仓库）转换成 DSH 技能。",
        epilog="退出码：0 成功 / 2 参数错误 / 3 安全检查失败 / 4 回滚失败")
    ap.add_argument("source", nargs="*",
                    help="本地 zip、本地目录、或 github.com/<owner>/<repo> 链接（可以给多个）")
    ap.add_argument("--dry-run", action="store_true", help="只检查并出报告，不写任何文件")
    ap.add_argument("--install", action="store_true", help="复制进 DSH 技能目录（默认只输出到 output/）")
    ap.add_argument("--all", action="store_true", help="包里多个技能时，一次全部转换")
    ap.add_argument("--allow-scripts", action="store_true",
                    help="允许复制技能里的脚本（.sh/.ps1 等；只复制，绝不执行）。二进制永远拒绝")
    ap.add_argument("--name", help="从包里挑哪个技能，或覆盖推断的名字")
    ap.add_argument("--no-report", action="store_true", help="不生成 report.html")
    ap.add_argument("--open", action="store_true", help="生成后用默认浏览器打开报告")
    ap.add_argument("--undo", action="store_true", help="还原最近一次备份")
    ap.add_argument("--force", action="store_true",
                    help="配合 --undo：检测到冲突也继续（会先把当前版本另存到 _undo_keep_*/）")
    ap.add_argument("--list-backups", action="store_true", help="列出备份")
    ap.add_argument("--skills-dir", help="技能目录（默认 $DSH_HOME/skills 或 ~/.dsh/skills）")
    ap.add_argument("--local", action="store_true",
                    help="装到程序旁边的 local-skills/（Agent 被限制只能写工作区时用）")
    ap.add_argument("--output-dir", metavar="PATH",
                    help="不装技能目录，只把产物放到这个目录（相当于 --output 的自定义路径）")
    ap.add_argument("--self-install", action="store_true",
                    help="把本工具自己装成一个 DSH 技能（引擎跟着技能目录走，不再依赖作者电脑的路径）")
    ap.add_argument("--compress", action="store_true",
                    help="生成压缩版（删重复/空行/徽章图/目录/废话起句、截超长示例）：SKILL.md 另存为 SKILL.compact.md，"
                         "refs/ 等目录里的 .md 附件也各生成一份 <原名>.compact.md")
    ap.add_argument("--no-attach", action="store_true",
                    help="配合 --compress：只压 SKILL.md，refs/ 等目录里的 .md 附件不压")
    ap.add_argument("--use-compact", action="store_true",
                    help="直接把压缩版当 SKILL.md 安装（需配合 --compress 或 --compact-file）")
    ap.add_argument("--compact-file", metavar="PATH",
                    help="用你（或 AI）改写好的 SKILL.md 当压缩版；本工具会先校验红线，改坏了直接拒绝")
    ap.add_argument("--aggressive", action="store_true",
                    help="规则压缩改成有损模式（删废话起句/致谢节/截超长示例）；"
                         "过不了结构初筛就不会写进技能目录")
    return ap


def _parse_args(argv):
    return _build_parser().parse_args(argv)


def _ask(prompt: str, default: str = "") -> str:
    try:
        s = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return s or default


def interactive_wizard() -> int:
    """双击 exe 时用：问一句、跑一次、停住别关窗。"""
    print("=" * 58)
    print("  DSH 外部技能极速转换器")
    print("=" * 58)
    print("把下面任意一个拖进这个窗口，然后按回车（拖多个也行）：")
    print("  · GitHub 链接（如 https://github.com/用户名/仓库名）")
    print("  · 一个 .zip 压缩包")
    print("  · 一个文件夹")
    print()
    src = _ask("来源：").strip()
    if not src:
        print("没有输入，退出。")
        _ask("按回车键关闭窗口……")
        return EXIT_ARGS
    sources = _split_sources(src)

    print()
    print("要做什么？")
    print("  1) 先演习一遍：只检查、什么都不写（最安全，推荐先用这个）")
    print("  2) 装进 DSH 技能目录")
    print("  3) 只放到本工具旁边的 output 文件夹")
    print("  4) 全部技能一次转（包里技能很多时用，等同 --all + 演习）")
    choice = _ask("选 1 / 2 / 3 / 4（直接回车 = 1）：", "1")

    argv = list(sources)
    if choice == "2":
        argv.append("--install")
    elif choice == "4":
        argv.append("--all")
        argv.append("--dry-run")
    elif choice != "3":
        argv.append("--dry-run")

    print()
    ans = _ask("要不要同时生成一份「省 Token 的压缩版」？(y/N)：", "n").lower()
    if ans in ("y", "yes", "是", "要"):
        argv.append("--compress")

    print()
    print("-" * 58)
    status = run_sources(_parse_args(argv))

    if status == EXIT_REJECTED:
        print()
        print("如果上面提示「包含多个技能」，可以：")
        print("  1) 全部转（--all）")
        print("  2) 只转其中一个（填名字）")
        pick = _ask("选 1 / 2（直接回车 = 算了）：", "")
        retry = None
        if pick == "1":
            retry = argv + ["--all"]
        elif pick == "2":
            name = _ask("技能名：")
            if name:
                retry = argv + ["--name", name]
        if retry:
            print()
            print("-" * 58)
            status = run_sources(_parse_args(retry))

    print()
    _ask("按回车键关闭窗口……")
    return status


def _split_sources(s: str):
    """把「拖进来的多个路径」拆开：支持带引号、空格分隔。"""
    import shlex
    try:
        parts = shlex.split(s, posix=False)
    except ValueError:
        parts = s.split()
    out = []
    for p in parts:
        p = p.strip().strip('"').strip("'")
        if p:
            out.append(p)
    return out or [s.strip()]


def run_sources(args) -> int:
    """逐个来源转换；每个来源各自出报告、各自写一批 _backup_。"""
    sources = list(args.source)
    status = EXIT_OK
    total = len(sources)
    base_ts = now_stamp()
    try:
        for i, src in enumerate(sources, 1):
            if total > 1:
                print()
                print("=" * 62)
                print(f"  [{i}/{total}] {src}")
                print("=" * 62)
            one = argparse.Namespace(**vars(args))
            one.source = src
            one._suffix = "" if i == 1 else f"-{i}"
            # 多个来源在同一秒跑完时，别让 _backup_ 目录撞名（否则后一批会盖掉前一批的清单）
            one._ts = base_ts + ("" if i == 1 else f"-{i}")
            st = convert(one)
            if st != EXIT_OK and status == EXIT_OK:
                status = st
        if total > 1:
            print()
            print(f"全部处理完：{total} 个来源。每批都有自己的 _backup_，--undo 一次撤一批。")
    except KeyboardInterrupt:
        eprint("\n已中断。")
        status = 1
    return status


def cmd_self_install(skills_dir: Path, dry_run: bool = False) -> int:
    """把本工具自己装成一个技能：引擎 convert.py 跟着技能目录走，不再指向作者电脑的路径。"""
    name = "skill-converter"
    engine = SCRIPT_DIR / "convert.py"
    if not engine.exists():
        eprint(f"找不到引擎文件：{engine}")
        eprint("自安装需要 convert.py 和 SKILL.md 在同一个目录（Release 的 exe 版本不支持自安装，")
        eprint("请用源码版，或直接把 exe 放到 PATH 里用）。")
        return EXIT_ROLLBACK
    dest = skills_dir / name
    print(f"把本工具装成技能：{dest}")
    if dry_run:
        print("（--dry-run：什么都没写）")
        for f in ("convert.py", "SKILL.md", "README.md"):
            p = SCRIPT_DIR / f
            print(f"  · {f}：{'有' if p.exists() else '缺（跳过）'}")
        return EXIT_OK
    skills_dir.mkdir(parents=True, exist_ok=True)
    ts = new_batch_id()
    tmp = Path(tempfile.mkdtemp(prefix=".dsh-self-", dir=str(skills_dir)))
    pkg = tmp / name
    try:
        pkg.mkdir(parents=True)
        shutil.copyfile(wp(engine), wp(pkg / "convert.py"))
        skill_md = SCRIPT_DIR / "SKILL.md"
        if skill_md.exists():
            text = read_text(skill_md)
            text = text.replace(str(engine), "<本技能目录>/convert.py")
            text = text.replace(str(SCRIPT_DIR), "<本技能目录>")
            header = ("本技能是「自安装」形态：引擎 `convert.py` 就在本技能目录里。\n"
                      "所有命令里的 `convert.py` 都优先用 <本技能目录>/convert.py；"
                      "本机安装时它是 "
                      f"`{dest / 'convert.py'}`。\n\n")
            write_text(pkg / "SKILL.md", header + text)
        else:
            eprint("警告：源目录没有 SKILL.md，装出来的技能不可用")
        if (SCRIPT_DIR / "README.md").exists():
            shutil.copyfile(wp(SCRIPT_DIR / "README.md"), wp(pkg / "README.md"))
        batch_dir = _batch_dir(APP_HOME, ts)
        write_backup_manifest(batch_dir, {
            "status": "pending", "createdAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "self-install", "mode": "install", "destRoot": str(skills_dir),
            "changes": [{"name": name, "action": "added", "dest": str(dest), "backup": ""}],
        })
        ch, _ = install_skill({"dir": pkg, "name": name}, skills_dir, batch_dir, "install", ts)
        write_backup_manifest(batch_dir, {
            "status": "done", "createdAt": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": "self-install", "mode": "install", "destRoot": str(skills_dir),
            "changes": [ch], "note": "自安装：引擎随技能目录",
        })
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"✔ 已装好：{dest}")
    print(f"   引擎：{dest / 'convert.py'}")
    print("   撤销：--undo")
    return EXIT_OK


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    argv = list(sys.argv[1:] if argv is None else argv)

    ap = _build_parser()
    args = ap.parse_args(argv)

    if args.use_compact and not (args.compress or args.compact_file):
        ap.error("--use-compact 要和 --compress 或 --compact-file 一起用")
    if args.compress and args.compact_file:
        ap.error("--compress 和 --compact-file 只能选一个")

    sources = list(args.source)
    if len(sources) > 1 and (args.name or args.compact_file):
        ap.error("给了多个来源时，不能用 --name 或 --compact-file（它们只针对单个技能）")

    if args.local:
        args.install = True
        if not args.skills_dir:
            args.skills_dir = str(APP_HOME / "local-skills")

    if args.undo:
        return cmd_undo(APP_HOME, resolve_skills_dir(args.skills_dir), OUTPUT_DIR, force=args.force)
    if args.self_install:
        return cmd_self_install(resolve_skills_dir(args.skills_dir), dry_run=args.dry_run)
    if args.list_backups:
        bs = list_backups(APP_HOME)
        if not bs:
            print("没有备份。")
        for b in bs:
            man = b / "manifest.json"
            label = ""
            extra = ""
            if man.exists():
                try:
                    rec = json.loads(read_text(man))
                    chs = rec.get("changes", [])
                    label = f"（{len(chs)} 项变更）"
                    if rec.get("status") == "pending":
                        label += "  ⚠ 未完成"
                    added = [c["name"] for c in chs if c.get("action") == "added"]
                    if added:
                        extra = "  --undo 会删掉：" + "、".join(added)
                except Exception:
                    label = "（manifest 读不了）"
            print(f"{b.name}  {label}{extra}")
        return EXIT_OK
    if not sources:
        # 双击 exe（没带参数、有终端）→ 走问答模式，别让人看到一串英文帮助然后闪退
        if not argv and (sys.stdin is not None and sys.stdin.isatty()
                         or os.environ.get("DSH_SKILL_WIZARD") == "1"):
            return interactive_wizard()
        ap.print_help()
        return EXIT_ARGS

    if len(sources) > 1 and (args.name or args.compact_file):
        ap.error("给了多个来源时，不能用 --name 或 --compact-file（它们只针对单个技能）")

    return run_sources(args)


if __name__ == "__main__":
    sys.exit(main())
