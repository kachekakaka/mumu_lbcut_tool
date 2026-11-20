"""冻结素材引用并原子写入独立的作品 YAML。"""

from hashlib import blake2b
import os
from pathlib import Path
import stat
import tempfile

import yaml

from .upload_platform import BridgeError


def file_snapshot(path, root):
    try:
        original = Path(path)
        resolved = original.resolve(strict=True)
        if (not original.is_absolute() or resolved.drive.startswith("\\\\")
            or resolved != Path(os.path.normpath(path)) or not resolved.is_relative_to(root.resolve())
            or resolved.suffix.lower() != ".mp4"):
            raise ValueError
        with resolved.open("rb") as handle:
            info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError
        return {"path": str(resolved), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
    except (OSError, ValueError, RuntimeError, TypeError):
        raise BridgeError("segment_file_unavailable", "正式录像不存在、不可读或不在录像输出目录内") from None


def version(content):
    return blake2b(content, digest_size=32).hexdigest()


def manifest_for(root, work_id, frozen):
    params = frozen["parameters"]
    videos = [
        {"id": f"p{position}", "path": Path(clip["file"]["path"]).relative_to(root).as_posix()}
        for position, clip in enumerate(frozen["clips"], 1)
    ]
    return {
        "version": 1,
        "work": {"id": work_id, "ready": True, **{k: params[k] for k in ("title", "description", "tags")}},
        "assets": {"videos": videos},
        "upload_units": [{
            "id": "recording", "mode": "multipart" if len(videos) > 1 else "independent",
            "videos": [video["id"] for video in videos],
            "targets": [{
                "platform": "bilibili", "account": frozen["account"]["id"], "enabled": True,
                "options": {k: params[k] for k in ("category_id", "copyright", "source", "publish_mode")},
            }],
        }],
    }


def manifest_path(root, work_id):
    path = root / "sessions" / work_id / "upload.yaml"
    if root.drive.startswith("\\\\") or path.resolve() != path or path.is_symlink():
        raise BridgeError("invalid_path", "作品目录越界或包含重解析路径")
    return path


def read_yaml(path):
    if path.resolve() != path or path.is_symlink():
        raise BridgeError("invalid_path", "作品 YAML 路径已变化")
    with path.open("rb") as handle:
        content = handle.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise BridgeError("invalid_manifest", "作品 YAML 超过 1 MiB")
    return content


def save_manifest(root, work_id, manifest, previous=None):
    path = manifest_path(root, work_id)
    content = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False).encode("utf-8")
    revision = version(content)
    if len(content) > 1024 * 1024:
        raise BridgeError("invalid_manifest", "作品 YAML 超过 1 MiB")
    try:
        original = read_yaml(path)
    except FileNotFoundError:
        original = None
    if original == content:
        return str(path), revision
    if original is not None and version(original) != previous:
        raise BridgeError("version_conflict", "现有作品 YAML 与冻结内容不同，已保留文件供核对")
    path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path(root, work_id) != path:
        raise BridgeError("invalid_path", "作品目录已变化")
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
        temp = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            if original is None:
                # Windows 下 rename 不覆盖目标，保留中途出现的文件。
                temp.rename(path)
            else:
                if read_yaml(path) != original:
                    raise BridgeError("version_conflict", "作品 YAML 在保存期间已变化")
                temp.replace(path)
        finally:
            handle.close()
            temp.unlink(missing_ok=True)
    return str(path), revision
