"""在仓外独立目录构建 Windows x64 录播 ZIP，每个候选号只写一次。"""
import argparse
from email.parser import BytesParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


TEMPLATES = Path(__file__).resolve().parent
ROOT = TEMPLATES.parents[1]
DEV = ROOT.parent / "mumu_lbcut_tool_dev"
PYTHON = "3.11.9"
FFMPEG = "ffmpeg-9.0.2-full_build"
SCRCPY = """scrcpy.exe scrcpy-server adb.exe AdbWinApi.dll AdbWinUsbApi.dll SDL3.dll
avcodec-63.dll avformat-63.dll avutil-61.dll swresample-7.dll libusb-1.0.dll
disconnected.png scrcpy.png LICENSE.txt scrcpy-noconsole.vbs open_a_terminal_here.bat""".split()
MVP = {"__init__", "command", "config", "dep_manager", "mumu_client", "room_resolver", "ui_dump", "windows_job"}
TEMPLATE_FILES = ("launcher.py", "启动录播控制台.cmd", "环境自检.cmd", "使用说明.txt",
                  "第三方来源.txt", "requirements-win311.lock")


def git(*args):
    return subprocess.check_output(["git", "-c", "core.quotepath=false", *args], cwd=ROOT,
                                   encoding="utf-8").rstrip("\n")


def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def unpack(archive, dest):
    for member in archive.infolist():
        rel = PurePosixPath(member.filename)
        if rel.is_absolute() or ".." in rel.parts or "\\" in member.filename or ":" in member.filename:
            raise ValueError(f"输入存档含非法路径：{member.filename}")
        if not member.is_dir():
            file = dest / rel
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open("xb") as output, archive.open(member) as source:
                shutil.copyfileobj(source, output)


def runtime(stage, work, embed, wheelhouse):
    if embed is None:
        filename = f"python-{PYTHON}-embed-amd64.zip"
        embed = work / filename
        urllib.request.urlretrieve(f"https://www.python.org/ftp/python/{PYTHON}/{filename}", embed)
    py_dir = stage / "python"
    with zipfile.ZipFile(embed) as archive:
        unpack(archive, py_dir)
    version = subprocess.check_output([py_dir / "python.exe", "-c",
        "import sys,struct;print(sys.version.split()[0],struct.calcsize('P'))"], encoding="ascii").strip()
    if version != f"{PYTHON} 8":
        raise ValueError(f"需要 Python {PYTHON} x64 的嵌入式发行包，输入为 {version}")
    (py_dir / "python311._pth").write_text("python311.zip\n.\n../app\nLib/site-packages\n", encoding="ascii")
    lock = TEMPLATES / "requirements-win311.lock"
    wheels = work / "wheels"
    cmd = [sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
           "-d", str(wheels), "-r", str(ROOT / "requirements.txt"), "-c", str(lock)]
    if wheelhouse:
        cmd += ["--no-index", "--find-links", str(wheelhouse)]
    subprocess.run(cmd, check=True)
    expected = {line.lower().replace("_", "-") for line in lock.read_text().splitlines() if line}
    actual = set()
    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            metadata, = (n for n in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n))
            msg = BytesParser().parsebytes(archive.read(metadata))
            actual.add(str(msg["Name"]).lower().replace("_", "-") + "==" + msg["Version"])
            # 当前锁定 wheels 的全部运行文件均直接位于 site-packages。
            if any(n.split("/")[0].endswith(".data") for n in archive.namelist()):
                raise ValueError(f"需先核对新 wheel 的安装布局：{wheel.name}")
            unpack(archive, py_dir / "Lib/site-packages")
    if expected != actual:
        raise ValueError(f"解析出的依赖与锁定清单不同：{sorted(actual ^ expected)}")
    return sorted(actual)


def application(stage, work):
    for name in git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "src", "web").split("\0"):
        if not name:
            continue
        file = Path(name)
        directory = file.parent.as_posix()
        if directory.startswith("web"):
            # 只从受版本控制的源码构建，不带开发机的 .env 或原 dist。
            if not any(p in {"dist", "node_modules"} or p.startswith(".env") for p in file.parts):
                copy(ROOT / file, work / file)
        elif ((directory in {"src/orchestrator", "src/worker"} and file.suffix == ".py"
               and file.name not in {"upload_core_host.py", "benchmark_tool.py"})
              or (directory == "src/mvp" and file.suffix == ".py" and file.stem in MVP)
              or (directory == "src/mvp/vendor" and file.name in {"__init__.py", "abogus.py", "NOTICE.md", "LICENSE"})):
            copy(ROOT / file, stage / "app" / file)
    npm = shutil.which("npm.cmd")
    if not npm:
        raise FileNotFoundError("构建机 PATH 中缺少 npm.cmd")
    subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=work / "web", check=True)
    subprocess.run([npm, "run", "build"], cwd=work / "web", check=True)
    web = work / "web/dist"
    if not (web / ".vite/license.md").is_file() or not (web / "index.html").is_file():
        raise ValueError("生产网页或网页第三方许可未生成")
    shutil.copytree(web, stage / "app/web/dist")


def archive_package(stage, path):
    expected = set()
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(p for p in stage.rglob("*") if p.is_file()):
            relative = file.relative_to(stage.parent).as_posix()
            expected.add(relative)
            item = zipfile.ZipInfo(relative)  # 固定条目时间，解压路径不进入包。
            item.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(item, "w") as member, file.open("rb") as source:
                shutil.copyfileobj(source, member, 1024 * 1024)
    with zipfile.ZipFile(path) as archive:
        if set(archive.namelist()) != expected or archive.testzip():
            raise ValueError("ZIP 条目或完整性检查失败")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=int)
    parser.add_argument("--embed-zip", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--tools-dir", type=Path, default=DEV / "tools")
    parser.add_argument("--upload-zip", type=Path, help="同时制作含指定上传便携包的整合候选")
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 11) or struct.calcsize("P") != 8 or args.candidate < 1:
        parser.error("请使用 Windows x64 Python 3.11 和正数候选号")
    version = json.loads((ROOT / "web/package.json").read_text())["version"]
    name = f"mumu_lbcut_tool-{version}-windows-x64-portable-test.{args.candidate}"
    dist = DEV / "mumu_lbcut_tool_dists"
    output = dist / (name + ("-combined" if args.upload_zip else "") + ".zip")
    if output.exists():
        raise FileExistsError(f"候选已存在，请使用新的编号：{output}")
    temp = DEV / "mumu_lbcut_tool_tmp"
    temp.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=temp, prefix=f"T24-build-{args.candidate}-"))
    package = work / ("MuMu录播与上传" if args.upload_zip else name)
    stage = package / "录播" if args.upload_zip else package
    stage.mkdir(parents=True)
    print(f"构建目录 {work}", flush=True)
    dependencies = runtime(stage, work, args.embed_zip, args.wheelhouse)
    application(stage, work)
    tools = stage / "mumu_lbcut_tool_dev/tools"
    for file in [*("scrcpy/" + n for n in SCRCPY),
                 *(FFMPEG + "/" + n for n in ("LICENSE", "README.txt", "bin/ffmpeg.exe", "bin/ffprobe.exe"))]:
        copy(args.tools_dir / file, tools / file)
    for name_in in TEMPLATE_FILES:
        if name_in.endswith(".cmd"):
            text = (TEMPLATES / name_in).read_text(encoding="utf-8")
            (stage / name_in).write_bytes(text.replace("\n", "\r\n").encode("ascii"))
        else:
            copy(TEMPLATES / name_in, stage / name_in)
    copy(ROOT / "requirements.txt", stage / "app/requirements.txt")
    status = git("status", "--porcelain", "--untracked-files=all", "--",
                 "src", "web", "scripts/portable", "scripts/build_windows_portable.ps1", "requirements.txt")
    manifest = {"name": name, "version": version, "revision": git("rev-parse", "HEAD"),
                "working_tree_dirty": bool(git("status", "--porcelain")), "build_input_changes": status.splitlines(),
                "frontend": "web/package-lock.json / npm ci / npm run build", "python": PYTHON,
                "dependencies": dependencies, "scrcpy": "5.0.1", "ffmpeg": "9.0.2-full_build-www.gyan.dev",
                "upload_package": args.upload_zip.name if args.upload_zip else None}
    (stage / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.upload_zip:
        upload_stage = work / "upload-input"
        with zipfile.ZipFile(args.upload_zip) as archive:
            unpack(archive, upload_stage)
        (upload,) = upload_stage.iterdir()
        if not all((upload / path).is_file() for path in (
            "runtime/pythonw.exe", "release.json", "启动上传平台.cmd",
        )):
            raise ValueError(f"上传 ZIP 不是完整的 Windows 便携包：{args.upload_zip}")
        upload.rename(package / "上传")
        text = (TEMPLATES / "启动MuMu.cmd").read_text(encoding="ascii")
        (package / "启动MuMu.cmd").write_bytes(text.replace("\n", "\r\n").encode("ascii"))
    archive_package(package, work / output.name)
    dist.mkdir(parents=True, exist_ok=True)
    # 临时目录与产物目录位于同一仓外工作盘。Windows rename 拒绝覆盖并发构建的同名成品。
    (work / output.name).rename(output)
    print(f"候选成品 {output}", flush=True)


if __name__ == "__main__":
    main()
