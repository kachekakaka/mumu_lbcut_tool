"""将内部事件与存储原因投影为稳定代码和详情；不改变 Worker 协议。"""
import re


def reason_object(value, fallback="unspecified"):
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    match = re.fullmatch(r"([a-z][a-z0-9_]*)(?::\s*(.*))?", text, re.DOTALL)
    if match:
        return {"code": match[1], "detail": match[2] or None}
    return {"code": fallback, "detail": text}
