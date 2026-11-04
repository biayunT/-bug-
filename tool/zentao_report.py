"""生成可离线阅读的禅道待验证报告，不包含任何禅道写操作。"""

import base64
from datetime import datetime
import html
import json
from pathlib import Path
import re
from typing import Any

from zentao_review import record_revision


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _image_data(path: str) -> str:
    """只嵌入已下载的位图，避免将鉴权链接或可执行 SVG 写入报告。"""
    data = Path(path).read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("截图不是支持的 PNG、JPEG、GIF 或 WebP 图片")
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def render_pending_html(
    store: dict[str, dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    site_root: str = "",
    include_images: bool = True,
    reviews: dict[str, Any] | None = None,
) -> str:
    """将旧记录与新增方案字段一起渲染；缺失方案时明确标记，绝不从根因臆造方案。"""
    cards, navigation = [], []
    image_count = 0
    note_count = 0
    for index, (bug_id, item) in enumerate(store.items(), 1):
        # 记录键必须为禅道数字 ID，不能成为 HTML 属性或脚本注入入口。
        if not str(bug_id).isdigit():
            continue
        detail = metadata.get(bug_id, {})
        title = detail.get("title") or item.get("title") or "未填写标题"
        notes = str(item.get("notes", "")).strip()
        note_count += bool(notes)
        shots = []
        bug_image_count = 0
        for shot_index, path in enumerate(detail.get("screenshot_paths", []) if include_images else [], 1):
            try:
                src = _image_data(path)
                shots.append(
                    f'<button class="screenshot" type="button" data-zoom '
                    f'aria-label="放大 Bug #{bug_id} 原始截图 {shot_index}">'
                    f'<img loading="lazy" src="{src}" alt="Bug #{bug_id} 原始截图 {shot_index}">'
                    f'<span>原始截图 {shot_index}<b>点击放大 ↗</b></span></button>'
                )
                image_count += 1
                bug_image_count += 1
            except (OSError, ValueError):
                shots.append('<p class="muted">该截图无法读取，请重新导出。</p>')
        has_images = bug_image_count > 0
        steps = re.sub(r"\[内嵌截图:[^\]]*\]", "", str(detail.get("steps", "")))
        # 空的编辑器段落和模板标签不提供问题信息；保留原有实际文字，不推测步骤。
        step_lines = [line.strip() for line in steps.splitlines() if line.strip()]
        step_lines = [line for line in step_lines if not re.fullmatch(
            r"[\[【]?(?:步骤|结果|期望|期望结果)[\]】]?[：:]?|（无重现步骤描述）", line
        )]
        description = "\n".join(step_lines) or "禅道未填写文字步骤，请结合原始截图核对问题现象。"
        files = [f.strip() for f in str(item.get("modified_files", "")).split(",") if f.strip()]
        file_list = "".join(f'<li><code>{_escape(f)}</code></li>' for f in files)
        bug_url = f'{site_root.rstrip("/")}/index.php?m=bug&f=view&bugID={bug_id}' if site_root else ""
        link = f'<a class="source-link" href="{_escape(bug_url)}" target="_blank" rel="noreferrer">查看禅道 ↗</a>' if bug_url else ""
        meta_parts = []
        for label, field in [("严重程度", "severity"), ("优先级", "pri"), ("指派给", "assigned_name")]:
            if detail.get(field):
                meta_parts.append(f'<span>{label} {_escape(detail[field])}</span>')
        screenshot_html = "".join(shots) or '<div class="no-shot"><span>▧</span>暂无可用截图</div>'
        snapshot_error = f'<p class="image-note">{_escape(detail.get("screenshot_error"))}</p>' if detail.get("screenshot_error") else ""
        notes_html = f'<div class="attention"><strong>仍需核实</strong><p>{_escape(notes)}</p></div>' if notes else ""
        search = " ".join(str(v) for v in [bug_id, title, item.get("root_cause", ""), item.get("solution", ""), *files])
        cards.append(f'''
<article class="bug-card" id="bug-{bug_id}" data-revision="{record_revision(item)}" data-search="{_escape(search.lower())}" data-notes="{int(bool(notes))}" data-images="{int(has_images)}">
  <header class="card-header"><div class="card-heading"><div class="eyebrow">BUG <span>#{bug_id}</span><i>·</i> {index:02d} / {len(store):02d}</div><h2>{_escape(title)}</h2><div class="bug-meta">{''.join(meta_parts)}</div></div><div class="card-actions"><span class="status"><i></i>待人工验证</span>{link}</div></header>
  <div class="card-body"><div class="solution-column">
    <section class="problem"><h3><span>01</span>问题现象</h3><p>{_escape(description)}</p></section>
    <section><h3><span>02</span>定位根因</h3><p>{_escape(item.get('root_cause')) or '尚未填写'}</p></section>
    <section class="solution"><h3><span>03</span>修改方案</h3><p>{_escape(item.get('solution')) or '旧记录未单独填写修改方案，需补充；请勿仅凭根因认定已修复。'}</p></section>
    <section><h3><span>04</span>如何验证</h3><p>{_escape(item.get('verify_steps')) or '尚未填写'}</p></section>
    <div class="test-result"><strong>检查记录</strong><p>{_escape(item.get('test_results')) or '未单独登记自动检查结果；仍需人工验证。'}</p></div>
    {notes_html}<details class="files"><summary>修改文件 <span>{len(files)}</span></summary><ul>{file_list or '<li>未登记文件</li>'}</ul></details>
    <p class="record-time">本地登记 {_escape(item.get('updated_at'))}</p>
  </div><aside class="evidence"><h3>禅道原始截图 <span>修复前的问题记录</span></h3><div class="screenshots">{screenshot_html}</div>{snapshot_error}</aside></div>
</article>''')
        navigation.append(f'<a href="#bug-{bug_id}" data-nav="{bug_id}"><span>#{bug_id}</span><b>{_escape(title)}</b></a>')
    template = Path(__file__).with_name("zentao_report_template.html").read_text(encoding="utf-8")
    values = {
        "NAVIGATION": "".join(navigation), "CARDS": "".join(cards),
        "BUG_COUNT": str(len(cards)), "IMAGE_COUNT": str(image_count),
        "NOTE_COUNT": str(note_count), "GENERATED_AT": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        # JSON 必须转义尖括号，备注中的 </script> 不能提前结束数据脚本。
        "REVIEW_DATA": json.dumps({"site_root": site_root, "items": (reviews or {}).get("items", [])}, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"),
        "REVIEW_SCRIPT": Path(__file__).with_name("zentao_review_ui.js").read_text(encoding="utf-8"),
    }
    # 只替换模板内的占位符，不对用户内容做第二轮替换。
    return re.sub(r"@@([A-Z_]+)@@", lambda match: values[match[1]], template)


def write_pending_report(
    path: Path,
    store: dict[str, dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    site_root: str = "",
    include_images: bool = True,
    reviews: dict[str, Any] | None = None,
) -> Path:
    """输出单文件 HTML；图片使用 data URL，无需本地服务或额外图片目录。"""
    path = path.expanduser().resolve()
    if path.suffix.lower() != ".html":
        raise ValueError("报告输出路径必须以 .html 结尾")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = render_pending_html(store, metadata, site_root, include_images, reviews)
    temporary = path.with_suffix(".html.tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(path)
    return path
