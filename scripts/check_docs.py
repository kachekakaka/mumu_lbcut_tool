"""只读检查本仓库 Markdown 的行内相对链接与标题锚点。"""
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
INLINE = re.compile(
    r"(?P<ticks>`+).+?(?P=ticks)"
    r'|\[[^\]\n]*\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<plain>[^\s()]+))(?:\s+"[^"]*")?\s*\)'
)


def prose(path):
    fence = None
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
        else:
            yield number, line


def headings(path):
    anchors = set()
    for _, line in prose(path):
        title = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*$", line)
        if not title:
            continue
        name = re.sub(r"\s+#+$", "", title[1])
        base = re.sub(r"[^\w -]", "", name.lower()).replace(" ", "-")
        anchor = base
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{base}-{suffix}"
        anchors.add(anchor)
    return anchors


def check(root):
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
        cwd=root,
    )
    files = [root / os.fsdecode(n) for n in names.split(b"\0") if n]
    anchors = {}
    errors = external = checked = 0
    for doc in files:
        if not doc.is_file():  # 工作区可能正删除已跟踪文档；其反向引用仍需检查。
            continue
        for number, line in prose(doc):
            for link in INLINE.finditer(line):
                if link["ticks"]:
                    continue
                raw = link["angle"] or link["plain"]
                if raw.startswith(("/", "\\")) or re.match(r"^[a-zA-Z][\w+.-]*:", raw):
                    continue
                filename, _, fragment = raw.partition("#")
                filename = unquote(filename.partition("?")[0])
                target = (doc.parent / filename).resolve() if filename else doc
                where = f"{doc.relative_to(root)}:{number}"
                if not target.is_relative_to(root):
                    external += 1
                    print(f"仓外未核验 {where} → {raw}")
                    continue
                checked += 1
                problem = ""
                if not target.exists():
                    problem = "目标不存在"
                elif fragment and target.is_file() and target.suffix.lower() == ".md":
                    if target not in anchors:
                        anchors[target] = headings(target)
                    if unquote(fragment) not in anchors[target]:
                        problem = "标题锚点不存在"
                if problem:
                    errors += 1
                    print(f"{problem} {where} → {raw}")
    print(f"仓内链接 {checked}，错误 {errors}，仓外未核验 {external}")
    return int(errors != 0)


if __name__ == "__main__":
    raise SystemExit(check(ROOT))
