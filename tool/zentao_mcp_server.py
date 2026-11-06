#!/usr/bin/env python3
"""
ZenTao (禅道开源版 22.6) Bug Management MCP Server — 专为 OpenAI Codex (VSCode 插件 / CLI) 定制
【两阶段人工验证模式：改完代码只出待验证列表，绝不擅自回写禅道状态；人工验证通过下令后再解决】

工作流设计:
- 阶段一（读取与改代码，零禅道写操作）：
  1. Codex 调用 `list_product_bugs` 或 `list_execution_bugs` 获取待修 Bug。
  2. Codex 调用 `get_bug_detail`（及 `download_bug_images`）分析重现步骤与截图，在本地修改代码。
  3. 代码修改完成后，Codex 调用 `record_pending_verification` 将修改结果登记到本地【待人工验证列表】并展示给用户，
     此时 **严禁回写禅道状态**。
- 阶段二（人工验证通过后，授权解决）：
  4. 用户根据列表在本地自行验证；验证通过后，用户明确下达指令（如“Bug #123 验证过了，可以解决”）。
  5. Codex 收到明确指令后，调用 `resolve_verified_bug` 自动提取本地暂存的根因与修改文件说明，正式向禅道提交“已解决”。

环境变量配置 (在 ~/.codex/config.toml 或 .codex/config.toml 中设置):
  ZENTAO_BASE_URL    : 禅道基础地址，如 https://zentao.example.com 或 http://127.0.0.1/zentao
  ZENTAO_ACCOUNT     : 禅道登录账号
  ZENTAO_PASSWORD    : 禅道登录密码
  ZENTAO_VERIFY_FILE : (可选) 本地待验证列表 JSON 存储路径，默认保存在 ~/.zentao_pending_verify.json
  ZENTAO_REPORT_FILE : (可选) HTML 报告路径，默认 C:/-bug-/tool/reports/zentao-pending.html（脚本旁 reports 目录）
"""

from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from zentao_report import write_pending_report
from zentao_review import import_review, load_review, STATUSES

# Codex 初始化全局指令（严格限制两阶段工作流，禁止改完代码直接回写状态）
CODEX_INSTRUCTIONS = (
    "禅道(ZenTao 22.6)两阶段改Bug服务【严禁改完代码后自动回写禅道状态】。"
    "阶段1(改代码)：调 list_product_bugs/list_execution_bugs 和 get_bug_detail 读取Bug，"
    "修改本地代码后必须调 record_pending_verification 登记并向用户输出【待验证Bug列表】，绝不可调用 resolve_verified_bug。"
    "登记时单独填写 solution（实际修改方案）、test_results（实际检查结果）和 notes（待核实项）；"
    "完成一批后调用 export_pending_report 生成含原始截图的离线 HTML 报告并提供本地路径。"
    "阶段2(人工验证后解决)：仅当用户在新对话消息中明确表示“已验证通过、可以解决”时，才允许调用 resolve_verified_bug 回写禅道。"
    "用户在后续对话说‘回归bug’、‘回归一下bug’或要求按HTML验收反馈处理时，"
    "第一步必须调用 read_review_feedback()，默认读取 C:/-bug- 目录下修改时间最新的 JSON，"
    "不要先索要Bug ID、JSON路径或读取浏览器暂存；仅在文件缺失或校验失败时说明具体问题。"
    "报告的已解决标记表示用户验证通过并授权解决；只对用户本次要求处理的已解决条目调用 resolve_verified_bug。"
    "未解决按备注重新复现和回归；继续处理参考说明与图片修改；待验证跳过。"
    "读取反馈本身绝不回写禅道；不得将模型自检结果当作用户已解决标记。"
)

mcp = FastMCP(
    "zentao-bug-mcp",
    instructions=CODEX_INSTRUCTIONS,
    dependencies=["httpx>=0.27.0", "mcp>=1.2.0"],
)

DEFAULT_VERIFY_FILE = Path(
    os.environ.get(
        "ZENTAO_VERIFY_FILE",
        str(Path.home() / ".zentao_pending_verify.json"),
    )
).expanduser()
DEFAULT_REPORT_FILE = Path(os.environ.get(
    "ZENTAO_REPORT_FILE", str(Path(__file__).parent / "reports" / "zentao-pending.html")
)).expanduser()
DEFAULT_FEEDBACK_DIR = Path(__file__).resolve().parent.parent
BUG_CACHE_FILE = DEFAULT_VERIFY_FILE.with_name(f"{DEFAULT_VERIFY_FILE.stem}_bugs.json")


class ZenTaoClient:
    """禅道 22.6 RESTful API (v1 & v2) 客户端，支持自动获取与刷新 Token"""

    def __init__(self) -> None:
        raw_url = os.environ.get("ZENTAO_BASE_URL", "").strip().rstrip("/")
        raw_url = re.sub(r"/api\.php/v[12]$", "", raw_url)
        self.site_root = raw_url
        self.api_v1 = f"{raw_url}/api.php/v1" if raw_url else ""
        self.api_v2 = f"{raw_url}/api.php/v2" if raw_url else ""

        self.account = os.environ.get("ZENTAO_ACCOUNT", "").strip()
        self.password = os.environ.get("ZENTAO_PASSWORD", "").strip()
        self._token: Optional[str] = None

    def _validate_config(self) -> None:
        missing = []
        if not self.site_root:
            missing.append("ZENTAO_BASE_URL")
        if not self.account:
            missing.append("ZENTAO_ACCOUNT")
        if not self.password:
            missing.append("ZENTAO_PASSWORD")
        if missing:
            raise ValueError(
                f"缺少必要的环境变量配置: {', '.join(missing)}。"
                "请在 ~/.codex/config.toml 的 [mcp_servers.zentao.env] 中配置禅道地址与账号密码。"
            )

    async def get_token(self, force_refresh: bool = False) -> str:
        """优先通过 POST /api.php/v1/tokens 获取 Token，兼容 v2 /api.php/v2/users/login"""
        self._validate_config()
        if self._token and not force_refresh:
            return self._token

        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            resp = await client.post(
                f"{self.api_v1}/tokens",
                json={"account": self.account, "password": self.password},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code >= 400:
                resp = await client.post(
                    f"{self.api_v2}/users/login",
                    json={"account": self.account, "password": self.password},
                    headers={"Content-Type": "application/json"},
                )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token")
            if not token:
                raise RuntimeError(f"禅道 22.6 未返回有效 token，响应内容: {data}")
            self._token = token
            return token

    async def request(
        self,
        method: str,
        path: str,
        api_version: str = "v1",
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """发送带 Token 的 API 请求，兼容 v1 和 v2 路径，遇到 401 自动刷新 Token"""
        token = await self.get_token()
        base = self.api_v2 if api_version == "v2" else self.api_v1
        url = f"{base}/{path.lstrip('/')}"

        async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
            # HTTP 请求头不区分大小写；同时发送 Token 和 token 会导致禅道鉴权失败。
            headers = {
                "Token": token,
                "Content-Type": "application/json",
            }
            resp = await client.request(
                method, url, params=params, json=json_body, headers=headers
            )

            if resp.status_code == 401:
                token = await self.get_token(force_refresh=True)
                headers["Token"] = token
                resp = await client.request(
                    method, url, params=params, json=json_body, headers=headers
                )

            resp.raise_for_status()
            return resp.json()

    async def download_file(self, relative_or_full_url: str, dest_path: Path) -> Path:
        """通过文件 REST 端点读取截图，避免网页附件地址跳登录及重复部署前缀。"""
        token = await self.get_token()
        source = html.unescape(relative_or_full_url)
        parsed = urlsplit(source)
        site = urlsplit(self.site_root)
        if parsed.netloc and (parsed.scheme, parsed.netloc) != (site.scheme, site.netloc):
            raise ValueError("截图地址不属于当前禅道站点")
        file_id = parse_qs(parsed.query).get("fileID", [""])[0]
        if not file_id:
            match = re.search(r"(?:/files/|file-read-)(\d+)", parsed.path)
            file_id = match[1] if match else ""
        full_url = (
            f"{self.api_v1}/files/{file_id}"
            if file_id.isdigit()
            else urljoin(f"{self.site_root}/", source)
        )
        async with httpx.AsyncClient(
            timeout=30.0, verify=False, follow_redirects=True
        ) as client:
            headers = {"Token": token}
            resp = await client.get(full_url, headers=headers)
            if resp.status_code == 401:
                headers["Token"] = await self.get_token(force_refresh=True)
                resp = await client.get(full_url, headers=headers)
            resp.raise_for_status()
            # 网页登录页也可能返回 200，不能将其静默保存成截图。
            data = resp.content
            is_image = data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
                data.startswith(b"RIFF") and data[8:12] == b"WEBP"
            )
            if not is_image:
                raise ValueError("禅道返回的内容不是有效截图，可能需要重新登录或检查文件权限")
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(resp.content)
            return dest_path


zentao_client = ZenTaoClient()


# ==============================================================================
# 本地【待人工验证列表】存储辅助函数（确保不回写禅道状态，先交给用户自己验证）
# ==============================================================================
def load_pending_store() -> dict[str, dict[str, Any]]:
    if not DEFAULT_VERIFY_FILE.exists():
        return {}
    try:
        data = json.loads(DEFAULT_VERIFY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pending_store(store: dict[str, dict[str, Any]]) -> None:
    DEFAULT_VERIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_VERIFY_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_bug_cache() -> dict[str, dict[str, Any]]:
    """缓存只读 Bug 详情及截图路径，支持离线生成报告。"""
    if not BUG_CACHE_FILE.exists():
        return {}
    data = json.loads(BUG_CACHE_FILE.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def cache_bug_detail(bug_id: int, detail: dict[str, Any]) -> None:
    cache = load_bug_cache()
    cache[str(bug_id)] = {
        **cache.get(str(bug_id), {}),
        "title": detail.get("title", ""),
        "steps": clean_html_steps(detail.get("steps", "")),
        "image_urls": extract_image_urls(detail.get("steps", "")),
        "severity": detail.get("severity", ""),
        "pri": detail.get("pri", ""),
        "assigned_name": extract_user_name(detail.get("assignedTo")),
    }
    save_bug_cache(cache)


def save_bug_cache(cache: dict[str, dict[str, Any]]) -> None:
    BUG_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BUG_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_report_message(store: dict[str, dict[str, Any]]) -> str:
    """本地登记成功后即生成 HTML；生成失败也保留已登记记录并明确提示。"""
    try:
        report = write_pending_report(DEFAULT_REPORT_FILE, store, load_bug_cache(), zentao_client.site_root,
                                      reviews=load_review(review_archive_path()))
        return (
            f"【待验证Bug列表 · HTML】[{report.name}]({report.as_posix()})\n"
            f"共 {len(store)} 项，验收标记见报告，尚未回写禅道状态。\n"
            "可调用 export_pending_report 刷新禅道原始详情和截图。"
        )
    except (OSError, ValueError) as exc:
        return f"记录已保存，但 HTML 报告生成失败：{exc}\n\n" + render_pending_markdown_table(store)


def render_pending_markdown_table(store: dict[str, dict[str, Any]]) -> str:
    if not store:
        return "当前【待人工验证列表】为空（没有已改完代码等待验证的 Bug）。"

    lines = [
        "### 待你本地验证的 Bug 列表（尚未回写禅道状态）",
        "",
        "| Bug ID | 标题 | 定位根因 | 修改文件 | 本地验证建议 | 登记时间 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for bug_id_str, item in store.items():
        title = str(item.get("title", "")).replace("|", "｜")
        root_cause = str(item.get("root_cause", "")).replace("|", "｜")
        files = str(item.get("modified_files", "")).replace("|", "｜")
        steps = str(item.get("verify_steps", "")).replace("|", "｜")
        updated_at = str(item.get("updated_at", ""))
        lines.append(
            f"| **#{bug_id_str}** | {title} | {root_cause} | `{files}` | {steps} | {updated_at} |"
        )

    lines.append("")
    lines.append(
        "> **下一步操作**：请你根据上表在本地验证。验证通过后，直接对我说："
        "“**Bug #ID 验证通过，可以解决了**”（或“**把列表里的 Bug 全部解决**”），我再去禅道更新状态。"
    )
    return "\n".join(lines)


def extract_image_urls(raw_html: str) -> list[str]:
    if not raw_html:
        return []
    return re.findall(r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>', raw_html, flags=re.I)


def clean_html_steps(raw_html: str) -> str:
    if not raw_html:
        return "（无重现步骤描述）"

    text = raw_html
    text = re.sub(
        r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>',
        r"\n[内嵌截图: \1]\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"</?(p|div|br|li|tr|h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE
    )
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned or "（无重现步骤描述）"


def extract_user_name(user_field: Any) -> str:
    if isinstance(user_field, dict):
        return (
            user_field.get("realname")
            or user_field.get("account")
            or str(user_field.get("id", ""))
        )
    return str(user_field or "未指定")


def format_bug_list_items(
    bugs: list[dict[str, Any]], status_filter: str = "", assigned_to: str = ""
) -> list[str]:
    filtered = []
    store = load_pending_store()
    for b in bugs:
        raw_status = b.get("status")
        status_code = (
            raw_status.get("code", "")
            if isinstance(raw_status, dict)
            else str(raw_status or "")
        )
        if status_filter and status_code.lower() != status_filter.lower():
            continue

        raw_assigned = b.get("assignedTo")
        assigned_account = (
            raw_assigned.get("account", "")
            if isinstance(raw_assigned, dict)
            else str(raw_assigned or "")
        )
        if assigned_to and assigned_account.lower() != assigned_to.lower():
            continue

        bug_id_str = str(b.get("id"))
        local_tag = " [本地已改完待你验证]" if bug_id_str in store else ""

        filtered.append(
            f"- [Bug #{bug_id_str}]{local_tag} (P{b.get('pri')} / S{b.get('severity')} | "
            f"禅道状态: {status_code} | 指派给: {extract_user_name(raw_assigned)}) "
            f"{b.get('title')}"
        )
    return filtered


# ==============================================================================
# 第一阶段工具：只读获取禅道 Bug 与图片下载
# ==============================================================================
@mcp.tool()
async def list_product_bugs(
    product_id: int,
    page: int = 1,
    limit: int = 20,
    status_filter: str = "active",
    assigned_to: str = "",
) -> str:
    """
    【只读】获取禅道指定【产品】下的 Bug 列表 (RESTful API v1: GET /products/{id}/bugs)。

    Args:
        product_id: 禅道产品 ID (必填)
        page: 页码，默认第 1 页
        limit: 每页条数，默认 20 条
        status_filter: 状态过滤（'active' 仅看激活待修 Bug，'resolved' 已解决，'closed' 已关闭，传 '' 查看全部）
        assigned_to: 指派给某账号过滤（如 'zhangsan'，传 '' 不过滤）
    """
    data = await zentao_client.request(
        "GET",
        f"/products/{product_id}/bugs",
        api_version="v1",
        params={"page": page, "limit": limit},
    )
    bugs = data.get("bugs", [])
    items = format_bug_list_items(
        bugs, status_filter=status_filter, assigned_to=assigned_to
    )

    if not items:
        return (
            f"产品 #{product_id} 在当前筛选条件 (page={page}, status='{status_filter}', "
            f"assigned_to='{assigned_to}') 下没有匹配的 Bug。"
        )

    header = (
        f"产品 #{product_id} Bug 列表 (共 {data.get('total', len(bugs))} 条，"
        f"当前页匹配 {len(items)} 条)：\n"
    )
    return header + "\n".join(items)


@mcp.tool()
async def list_execution_bugs(
    execution_id: int,
    browse_type: str = "unresolved",
    assigned_to: str = "",
    page: int = 1,
    limit: int = 50,
) -> str:
    """
    【只读】获取禅道 22.6 指定【执行/迭代】下的 Bug 列表 (RESTful API v2: GET /executions/{id}/bugs)。

    Args:
        execution_id: 禅道执行/迭代 ID (必填)
        browse_type: 状态范围，默认 'unresolved' (未关闭)，可选 'all' (全部)
        assigned_to: 按指派给某账号过滤（如 'zhangsan'，传 '' 不过滤）
        page: 页码，默认 1
        limit: 每页数量，默认 50 (最大 1000)
    """
    data = await zentao_client.request(
        "GET",
        f"/executions/{execution_id}/bugs",
        api_version="v2",
        params={
            "browseType": browse_type,
            "pageID": page,
            "recPerPage": limit,
            "orderBy": "pri_asc,id_desc",
        },
    )
    bugs = data.get("bugs", [])
    items = format_bug_list_items(bugs, status_filter="", assigned_to=assigned_to)

    if not items:
        return (
            f"执行/迭代 #{execution_id} 下没有匹配的 Bug "
            f"(browseType={browse_type}, assigned_to='{assigned_to}')。"
        )

    return f"执行/迭代 #{execution_id} Bug 列表 (匹配 {len(items)} 条)：\n" + "\n".join(
        items
    )


@mcp.tool()
async def get_bug_detail(bug_id: int) -> str:
    """
    【只读】根据 Bug ID 获取禅道 Bug 的完整详情（包含标题、严重程度、清洗后的重现步骤、代码路径、关联需求与内嵌截图链接），
    供 Codex 分析根因并定位代码修复。

    Args:
        bug_id: 禅道 Bug ID
    """
    b = await zentao_client.request("GET", f"/bugs/{bug_id}", api_version="v1")
    cache_bug_detail(bug_id, b)

    raw_status = b.get("status")
    status_str = (
        f"{raw_status.get('name')} ({raw_status.get('code')})"
        if isinstance(raw_status, dict)
        else str(raw_status or "未知")
    )

    raw_steps = b.get("steps", "")
    steps_clean = clean_html_steps(raw_steps)
    img_urls = extract_image_urls(raw_steps)

    files_info = ""
    files = b.get("files", [])
    file_lines = []
    if isinstance(files, list) and files:
        for f in files:
            if isinstance(f, dict):
                file_lines.append(
                    f"  - 附件: {f.get('title', '未命名')} (路径: {f.get('webPath') or f.get('realPath') or '无'})"
                )
    if img_urls:
        for idx, img_url in enumerate(img_urls, 1):
            file_lines.append(f"  - 步骤内嵌截图 #{idx}: {img_url}")

    if file_lines:
        files_info = (
            "\n## 截图与附件列表 (可调用 `download_bug_images` 下载到本地供 Codex 查看)\n"
            + "\n".join(file_lines)
        )

    code_context = ""
    if b.get("entry") or b.get("lines"):
        code_context = (
            f"- **禅道标记代码位置**: 文件 `{b.get('entry', '')}` 行号 `{b.get('lines', '')}`\n"
        )

    return (
        f"# 禅道 Bug #{b.get('id')}: {b.get('title')}\n\n"
        f"- **状态**: {status_str} (是否确认: {'已确认' if b.get('confirmed') else '未确认'})\n"
        f"- **类型**: {b.get('type', 'codeerror')} | **关键词**: {b.get('keywords') or '无'}\n"
        f"- **严重程度 (Severity)**: {b.get('severity')} | **优先级 (Priority)**: {b.get('pri')}\n"
        f"- **所属产品 ID**: {b.get('product')} | **所属项目**: {b.get('projectName') or b.get('project')} | "
        f"**所属执行**: {b.get('executionName') or b.get('execution')}\n"
        f"- **关联需求**: {b.get('storyTitle') or b.get('story') or '无'}\n"
        f"- **环境信息**: OS={b.get('os') or '未填'}, Browser={b.get('browser') or '未填'}\n"
        f"{code_context}"
        f"- **创建人**: {extract_user_name(b.get('openedBy'))} ({b.get('openedDate', '')})\n"
        f"- **当前指派给**: {extract_user_name(b.get('assignedTo'))}\n\n"
        f"## 重现步骤与现象描述\n"
        f"{steps_clean}"
        f"{files_info}\n\n"
        f"---\n"
        f"【重要规则提醒】：完成本地代码修改后，**严禁直接回写禅道状态**！"
        f"请调用 `record_pending_verification` 将修改记录加入【待人工验证列表】并展示给用户验证。"
    )


@mcp.tool()
async def download_bug_images(bug_id: int, save_dir: str = "") -> str:
    """
    【只读】下载指定 Bug 重现步骤中的内嵌报错截图到本地目录，返回本地文件绝对路径，方便 VSCode Codex 直接查看图片内容。

    Args:
        bug_id: 禅道 Bug ID
        save_dir: 保存目录路径（留空则默认保存在系统临时目录 /tmp/zentao_bug_<id> 下）
    """
    b = await zentao_client.request("GET", f"/bugs/{bug_id}", api_version="v1")
    cache_bug_detail(bug_id, b)
    raw_steps = b.get("steps", "")
    img_urls = extract_image_urls(raw_steps)

    if not img_urls:
        return f"Bug #{bug_id} 的重现步骤中没有检测到内嵌图片 (<img>)。"

    target_dir = (
        Path(save_dir).expanduser().resolve()
        if save_dir
        else Path(tempfile.gettempdir()) / f"zentao_bug_{bug_id}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = []
    image_paths = []
    errors = []
    for idx, url in enumerate(img_urls, 1):
        ext = Path(urlsplit(url).path).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            ext = ".png"
        dest = target_dir / f"bug_{bug_id}_screenshot_{idx}{ext}"
        try:
            await zentao_client.download_file(url, dest)
            saved_paths.append(str(dest))
            image_paths.append(str(dest))
        except Exception as exc:
            saved_paths.append(f"下载失败 ({url}): {exc}")
            errors.append(f"截图 {idx} 下载失败：{type(exc).__name__}")

    cache = load_bug_cache()
    cache[str(bug_id)]["screenshot_paths"] = image_paths
    cache[str(bug_id)]["screenshot_error"] = "；".join(errors)
    save_bug_cache(cache)

    return f"Bug #{bug_id} 截图下载结果：\n" + "\n".join(f"- {p}" for p in saved_paths)


# ==============================================================================
# 本地【待人工验证列表】工具：改完代码后登记并输出列表给用户验证（绝不修改禅道状态）
# ==============================================================================
@mcp.tool()
async def record_pending_verification(
    bug_id: int,
    title: str,
    root_cause: str,
    modified_files: str,
    verify_steps: str,
    solution: str = "",
    test_results: str = "",
    notes: str = "",
) -> str:
    """
    【改完代码后必调 · 不回写禅道】当 Codex 在本地修改完某个 Bug 的代码后，调用此工具将修复信息登记到本地【待人工验证列表】，
    并生成离线 HTML 报告交由用户自行验证。此操作不会向禅道发送任何修改请求。

    Args:
        bug_id: 禅道 Bug ID
        title: Bug 标题
        root_cause: 简明根因分析（说明为什么会出现该 Bug）
        modified_files: 修改的源码文件列表（多个文件用逗号分隔）
        verify_steps: 给用户的本地验证步骤/回归测试建议
        solution: 实际修改方案，单独说明改了什么、为何能消除问题；不能把根因直接冒充方案
        test_results: 实际已运行的检查结果及未完成验证，不得虚构通过
        notes: 仍待确认的业务问题、风险或人工验证重点
    """
    store = load_pending_store()
    previous = store.get(str(bug_id), {})
    store[str(bug_id)] = {
        **previous,
        "bug_id": bug_id,
        "title": title,
        "root_cause": root_cause,
        "modified_files": modified_files,
        "verify_steps": verify_steps,
        "solution": solution or previous.get("solution", ""),
        "test_results": test_results or previous.get("test_results", ""),
        "notes": notes or previous.get("notes", ""),
        # 每次重新登记都是新一轮验收，即使同一分钟内方案文字未变也不能沿用已解决标记。
        "verification_round": datetime.now().astimezone().isoformat(timespec="microseconds"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_pending_store(store)
    return (
        f"已将 Bug #{bug_id} 登记到本地【待人工验证列表】（未回写禅道状态）。\n\n"
        + pending_report_message(store)
    )


@mcp.tool()
async def list_pending_verifications() -> str:
    """
    【不回写禅道】查看待验证 Bug，并重新生成包含缓存截图的本地 HTML 报告。
    """
    store = load_pending_store()
    return pending_report_message(store)


@mcp.tool()
async def export_pending_report(
    output_path: str = "",
    include_images: bool = True,
    refresh_details: bool = True,
) -> str:
    """导出逐项对应问题、根因、修改方案和截图的离线 HTML；仅 GET 禅道，不更新状态。

    Args:
        output_path: HTML 输出路径，留空使用 ZENTAO_REPORT_FILE 或脚本旁 reports/zentao-pending.html
        include_images: 是否下载并嵌入原始截图，默认开启
        refresh_details: 是否从禅道刷新详情；关闭时只用已有缓存，可离线生成
    """
    store = load_pending_store()
    warnings = []
    for bug_id in store:
        if not bug_id.isdigit():
            continue
        if refresh_details:
            try:
                detail = await zentao_client.request("GET", f"/bugs/{bug_id}", api_version="v1")
                cache_bug_detail(int(bug_id), detail)
            except Exception as exc:
                warnings.append(f"#{bug_id} 详情刷新失败：{type(exc).__name__}，沿用缓存")
        cache = load_bug_cache()
        detail = cache.get(bug_id, {})
        if include_images and refresh_details and detail.get("image_urls"):
            paths, errors = [], []
            target = DEFAULT_REPORT_FILE.parent / "screenshots" / bug_id
            for index, url in enumerate(detail["image_urls"], 1):
                dest = target / f"{index}.png"
                try:
                    await zentao_client.download_file(url, dest)
                    paths.append(str(dest.resolve()))
                except Exception as exc:
                    errors.append(f"截图 {index} 下载失败：{type(exc).__name__}")
            # 网络失败时保留之前可用的截图，同时在报告显示失败提示。
            detail["screenshot_paths"] = paths or detail.get("screenshot_paths", [])
            detail["screenshot_error"] = "；".join(errors)
            cache[bug_id] = detail
            save_bug_cache(cache)
            warnings.extend(f"#{bug_id} {error}" for error in errors)
    path = Path(output_path) if output_path else DEFAULT_REPORT_FILE
    report = write_pending_report(path, store, load_bug_cache(), zentao_client.site_root, include_images,
                                  reviews=load_review(review_archive_path()))
    warning_text = "\n\n部分内容未刷新：\n" + "\n".join(warnings) if warnings else ""
    return (
        f"【待验证Bug列表 · HTML】[{report.name}]({report.as_posix()})\n"
        f"共 {len(store)} 项。包含问题现象、根因、独立修改方案、文件及验证说明；"
        "截图为禅道原始问题截图，已嵌入报告，可离线查看。未回写禅道状态。"
        + warning_text
    )


def review_archive_path() -> Path:
    """人工反馈与修复记录分开存储，重生成报告不覆盖用户备注和图片。"""
    return DEFAULT_VERIFY_FILE.with_name(f"{DEFAULT_VERIFY_FILE.stem}_review.json")


def latest_feedback_file() -> Path:
    """从用户约定的反馈目录选取最新 JSON，仍由导入流程校验格式和验收轮次。"""
    candidates = (path for path in DEFAULT_FEEDBACK_DIR.glob("*.json") if path.is_file())
    # 只选择最新文件；若它损坏或轮次过期，不能退回旧反馈代替本轮人工验收。
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name),
               default=DEFAULT_FEEDBACK_DIR / "zentao-review.json")


@mcp.tool()
async def read_review_feedback(feedback_path: str = "") -> str:
    """读取用户从 HTML 保存的标记、说明和图片，返回逐条处理清单；不请求禅道、不修改源码。

    用户说“回归 Bug”“回归一下 bug”或“按反馈处理”时，第一步直接调用本工具，
    不必再索要 Bug ID 或默认文件路径。已解决表示用户已验证通过并授权解决，
    代理收到本次处理指令后逐条调用 resolve_verified_bug；本工具不会代为解决。待验证不得继续修改。

    Args:
        feedback_path: 用户保存的 .json 反馈路径，留空读取 C:/-bug- 下修改时间最新的 JSON
    """
    source = Path(feedback_path).expanduser() if feedback_path else latest_feedback_file()
    if not source.is_file():
        return (f"尚未找到反馈文件：{source}。请在 HTML 点击“绑定 / 保存反馈 JSON”，"
                "选择此路径并等待‘JSON 已保存’；若使用下载模式，请将下载文件放到此路径。"
                "仅在你保存到其他位置时才需要提供路径。未回写禅道或修改源码。")
    try:
        result = import_review(source, load_pending_store(), zentao_client.site_root,
                               review_archive_path(), DEFAULT_REPORT_FILE.parent / "review-images")
    except (OSError, ValueError, TypeError) as exc:
        return f"反馈未导入：{exc}。未修改禅道或源码。"
    actions = {"resolved": "用户已验证通过；收到本次处理指令后在禅道解决",
               "unresolved": "按用户备注和图片重新复现、排查并回归",
               "continue": "参考用户说明和图片继续处理，修改后重新登记待验证",
               "pending": "跳过，先不动"}
    items = [{"bug_id": item["bug_id"], "status": STATUSES[item["status"]],
              "action": actions[item["status"]], "remark": item["remark"],
              "image_paths": item["image_paths"]} for item in result["items"]]
    return json.dumps({"source": str(source.resolve()), "archive_path": result["archive_path"],
                       "items": items, "message": "仅导入本地反馈，未回写禅道或修改源码。",
                       "report": pending_report_message(load_pending_store())}, ensure_ascii=False, indent=2)


# ==============================================================================
# 第二阶段工具：仅在用户验证通过并明确下令“可以解决”后，才向禅道提交解决状态
# ==============================================================================
@mcp.tool()
async def resolve_verified_bug(
    bug_id: int,
    user_confirmed: bool = False,
    resolution: str = "fixed",
    resolved_build: str = "trunk",
    extra_comment: str = "",
    assigned_to: str = "",
) -> str:
    """
    【仅限用户验证通过并明确下令后调用！】将用户已验证通过的 Bug 在禅道中正式标记为“已解决”
    (POST /api.php/v1/bugs/{id}/resolve)。

    严禁在刚改完代码时自动调用此工具！必须先调用 `record_pending_verification` 输出列表给用户验证，
    只有当用户在后续对话中明确发出“验证过了，可以解决”等指令时，才可传入 `user_confirmed=True` 调用。

    Args:
        bug_id: 禅道 Bug ID
        user_confirmed: 必须为 True（代表用户已在对话中亲自确认验证通过并要求解决）
        resolution: 解决方案代码，默认 'fixed' (已解决)，可选 'bydesign'、'duplicate'、'external'、'notrepro'、'postponed'、'willnotfix'、'tostory'
        resolved_build: 解决版本（默认 'trunk' 主干，或传具体版本 ID）
        extra_comment: 附加备注说明（工具会自动合并本地待验证列表中记录的根因与修改文件）
        assigned_to: 解决后指派给谁验收（留空则自动指派回该 Bug 的创建人）
    """
    if not user_confirmed:
        return (
            f"已拦截：Bug #{bug_id} 尚未获得用户人工验证确认 (`user_confirmed=False`)。"
            "请先展示待验证列表由用户自行验证，待用户明确下令“可以解决”后再调用。"
        )

    store = load_pending_store()
    pending_record = store.get(str(bug_id))
    if not pending_record:
        return (
            f"已拦截：Bug #{bug_id} 不在本地【待人工验证列表】中。"
            "请先调用 `record_pending_verification` 输出待验证列表给用户验证，待用户确认后再解决。"
        )

    # 自动将之前暂存的根因分析与修改文件组装为禅道解决备注
    auto_comment_parts = [
        f"【根因分析】{pending_record.get('root_cause', '')}",
        f"【修改文件】{pending_record.get('modified_files', '')}",
    ]
    if extra_comment:
        auto_comment_parts.append(f"【补充说明】{extra_comment}")
    final_comment = "\n".join(auto_comment_parts)

    payload: dict[str, Any] = {
        "resolution": resolution,
        "resolvedBuild": resolved_build,
        "comment": final_comment,
    }

    if not assigned_to:
        current = await zentao_client.request("GET", f"/bugs/{bug_id}", api_version="v1")
        opened_by = current.get("openedBy")
        if isinstance(opened_by, dict) and opened_by.get("account"):
            assigned_to = opened_by["account"]
        elif isinstance(opened_by, str) and opened_by:
            assigned_to = opened_by

    if assigned_to:
        payload["assignedTo"] = assigned_to

    res = await zentao_client.request(
        "POST", f"/bugs/{bug_id}/resolve", api_version="v1", json_body=payload
    )

    # 从本地待验证列表中移除已解决的 Bug
    store.pop(str(bug_id), None)
    save_pending_store(store)

    remaining_msg = (
        f"\n\n剩余待验证 Bug 数量: {len(store)} 条。"
        if store
        else "\n\n当前所有待验证 Bug 均已处理完毕！"
    )
    return (
        f"Bug #{bug_id} 已在禅道 22.6 中正式标记为已解决 ("
        f"resolution={resolution}, 指派给={assigned_to or '默认'}, "
        f"status={res.get('status', 'resolved')})，并已写入根因与修改文件备注。"
        f"{remaining_msg}"
    )


@mcp.prompt()
def fix_zentao_bug(bug_id: int) -> str:
    """阶段一 Prompt：读取禅道 Bug 并修改代码，完成后只输出待验证列表，不回写禅道"""
    return (
        f"请帮我排查并修改禅道 22.6 上的 Bug #{bug_id}，严格按以下规则执行：\n"
        f"1. 调用 `get_bug_detail(bug_id={bug_id})` 获取重现步骤；若含内嵌截图，调用 `download_bug_images(bug_id={bug_id})` 下载到本地查看。\n"
        f"2. 在当前代码库定位根因并完成最小化代码修改，运行相关测试或语法检查。\n"
        f"3. **严禁调用 `resolve_verified_bug` 回写禅道状态！** 代码改完后，调用 `record_pending_verification` 登记该 Bug 的根因、修改文件与本地验证步骤，并将生成的【待你本地验证的 Bug 列表】展示给我，等待我自行验证。"
        f"登记时单独填写 solution、test_results 和 notes；完成后调用 export_pending_report 生成带禅道原始截图的 HTML 报告。"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
