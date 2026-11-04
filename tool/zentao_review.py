"""离线验收反馈的版本校验、图片解包与本地存储；不调用禅道接口。"""

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


STATUSES = {"pending": "待验证", "resolved": "已解决", "unresolved": "未解决", "continue": "继续处理"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def record_revision(record: dict[str, Any]) -> str:
    """绑定当前修改记录，避免上一轮的验收结果被用于新一轮代码。"""
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def load_review(path: Path) -> dict[str, Any]:
    """不存在反馈时返回空档案，损坏文件明确报错而非静默丢弃。"""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"items": []}


def decode_image(image: dict[str, Any]) -> tuple[bytes, str]:
    """仅接收可离线展示的位图；名称只作说明，绝不作为写入路径。"""
    data_url = image.get("data_url", "")
    if not isinstance(data_url, str) or len(data_url) > MAX_IMAGE_BYTES * 4 // 3 + 100:
        raise ValueError("单张反馈图片不能超过 5 MB")
    match = re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\r\n]+)", data_url)
    if not match:
        raise ValueError("反馈图片仅支持 PNG、JPEG、GIF、WebP")
    try:
        data = base64.b64decode(match[2], validate=True)
    except ValueError as exc:
        raise ValueError("反馈图片编码无效") from exc
    checks = {
        "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": data.startswith(b"\xff\xd8\xff"),
        "gif": data.startswith((b"GIF87a", b"GIF89a")),
        "webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    }
    if len(data) > MAX_IMAGE_BYTES or not checks[match[1]]:
        raise ValueError("反馈图片内容与格式不符或超过 5 MB")
    return data, "jpg" if match[1] == "jpeg" else match[1]


def validate_review(document: Any, store: dict[str, dict[str, Any]], site_root: str) -> list[dict[str, Any]]:
    """整批验证后才能落盘；待验证项只用于显示，不生成处理动作。"""
    if not isinstance(document, dict) or document.get("format") != "zentao-review" or document.get("version") != 1:
        raise ValueError("不是受支持的禅道验收反馈文件")
    if not isinstance(document.get("site_root"), str) or document["site_root"].rstrip("/") != site_root.rstrip("/"):
        raise ValueError("反馈文件的禅道地址与当前配置不一致")
    items = document.get("items")
    if not isinstance(items, list):
        raise ValueError("反馈条目格式错误")
    validated, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("反馈条目格式错误")
        bug_id = str(item.get("bug_id", ""))
        if not bug_id.isdigit() or bug_id in seen:
            raise ValueError("反馈包含无效或重复的 Bug ID")
        seen.add(bug_id)
        status = item.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError(f"Bug #{bug_id} 的标记无效")
        # 已在禅道解决并从待验证列表移除的旧条目不再处理，允许重复读取同一批反馈。
        if bug_id not in store:
            continue
        if item.get("revision") != record_revision(store[bug_id]):
            if status == "pending":
                continue
            raise ValueError(f"Bug #{bug_id} 的修改记录已更新，请打开最新报告重新标记")
        remark, images = item.get("remark", ""), item.get("images", [])
        if not isinstance(remark, str) or len(remark) > 20000 or not isinstance(images, list) or len(images) > 10:
            raise ValueError(f"Bug #{bug_id} 的备注或图片格式错误（备注最多 20000 字，每项最多 10 张图）")
        if status in ("unresolved", "continue") and not remark.strip() and not images:
            raise ValueError(f"Bug #{bug_id} 请填写说明或添加图片")
        for image in images:
            if not isinstance(image, dict) or not isinstance(image.get("name"), str):
                raise ValueError(f"Bug #{bug_id} 的图片格式错误")
            decode_image(image)
        validated.append({"bug_id": int(bug_id), "revision": item["revision"], "status": status,
                          "remark": remark, "images": images, "updated_at": str(item.get("updated_at", ""))})
    return validated


def import_review(path: Path, store: dict[str, dict[str, Any]], site_root: str,
                  archive_path: Path, image_dir: Path) -> dict[str, Any]:
    """保存用户反馈并提取截图供代理查看；保留历史，绝不自动解决或重改 Bug。"""
    if path.stat().st_size > 75 * 1024 * 1024:
        raise ValueError("反馈文件超过 75 MB，请减少图片后重新保存")
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    items = validate_review(document, store, site_root)
    previous = load_review(archive_path)
    by_id = {str(item["bug_id"]): item for item in previous.get("items", [])}
    history = list(previous.get("history", []))
    for item in items:
        bug_id = str(item["bug_id"])
        paths = []
        for image in item["images"]:
            data, suffix = decode_image(image)
            target = image_dir / bug_id / f"{hashlib.sha256(data).hexdigest()[:24]}.{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            paths.append(str(target.resolve()))
        item["image_paths"] = paths
        old = by_id.get(bug_id)
        if old and old != item:
            history.append(old)
        by_id[bug_id] = item
    saved = {"format": "zentao-review", "version": 1, "site_root": site_root,
             "imported_at": datetime.now(timezone.utc).isoformat(), "items": list(by_id.values()), "history": history}
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(archive_path)
    return {"items": items, "archive_path": str(archive_path.resolve())}
