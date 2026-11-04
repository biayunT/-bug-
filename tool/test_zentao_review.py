"""人工反馈闭环回归；使用临时记录，不修改真实 Bug 或源码。"""

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import zentao_mcp_server as server
from zentao_report import render_pending_html
from zentao_review import import_review, record_revision, validate_review
from test_zentao_report import PNG


class 反馈导入检查(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = {str(i): {"title": f"测试 {i}", "solution": "方案"} for i in range(12, 16)}
        self.document = {"format": "zentao-review", "version": 1, "site_root": "https://example.test/pms", "items": [
            {"bug_id": i, "revision": record_revision(self.store[str(i)]), "status": status,
             "remark": "验证现象" if status in ("unresolved", "continue") else "", "images": [], "updated_at": "2026-11-04T12:00:00Z"}
            for i, status in zip(range(12, 16), ["resolved", "unresolved", "continue", "pending"])]}
        self.source = self.root / "feedback.json"

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def write_document(self):
        self.source.write_text(json.dumps(self.document, ensure_ascii=False), encoding="utf-8")

    def test_四种状态保留且拒绝无说明的待处理项(self):
        items = validate_review(self.document, self.store, self.document["site_root"])
        self.assertEqual([item["status"] for item in items], ["resolved", "unresolved", "continue", "pending"])
        self.document["items"][1]["remark"] = ""
        with self.assertRaisesRegex(ValueError, "填写说明"):
            validate_review(self.document, self.store, self.document["site_root"])

    def test_旧轮次和其他禅道反馈不能用来解决新记录(self):
        changed = {**self.store, "12": {"solution": "新修改"}}
        with self.assertRaisesRegex(ValueError, "修改记录已更新"):
            validate_review(self.document, changed, self.document["site_root"])
        with self.assertRaisesRegex(ValueError, "地址.*不一致"):
            validate_review(self.document, self.store, "https://other.test")

    async def test_重复登记相同方案仍开启新一轮待验证(self):
        with patch.object(server, "DEFAULT_VERIFY_FILE", self.root / "pending.json"), patch.object(
            server, "BUG_CACHE_FILE", self.root / "cache.json"
        ), patch.object(server, "DEFAULT_REPORT_FILE", self.root / "report.html"):
            await server.record_pending_verification(12, "问题", "根因", "a.py", "验证")
            previous = record_revision(server.load_pending_store()["12"])
            await server.record_pending_verification(12, "问题", "根因", "a.py", "验证")
            self.assertNotEqual(previous, record_revision(server.load_pending_store()["12"]))

    def test_已移除项和旧待验证项跳过且重复条目被拒绝(self):
        changed = {**self.store, "15": {"solution": "新修改"}}
        changed.pop("12")
        self.assertEqual(len(validate_review(self.document, changed, self.document["site_root"])), 2)
        self.document["items"].append(self.document["items"][0])
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_review(self.document, self.store, self.document["site_root"])

    def test_反馈图片可查看且原始文件名不影响写入路径(self):
        self.document["items"][1]["images"] = [{"name": "../../逃逸.png", "data_url": "data:image/png;base64," + base64.b64encode(PNG).decode()}]
        self.write_document()
        result = import_review(self.source, self.store, self.document["site_root"], self.root / "archive.json", self.root / "images")
        image = Path(result["items"][1]["image_paths"][0])
        self.assertTrue(image.is_relative_to(self.root / "images" / "13"))
        self.assertEqual(image.read_bytes(), PNG)
        self.assertFalse((self.root / "逃逸.png").exists())

    def test_伪装图片整批拒绝且不覆盖已有档案(self):
        archive = self.root / "archive.json"
        archive.write_text('{"items": []}', encoding="utf-8")
        previous = archive.read_bytes()
        self.document["items"][1]["images"] = [{"name": "假图.png", "data_url": "data:image/png;base64," + base64.b64encode(b"<html>fake</html>").decode()}]
        self.write_document()
        with self.assertRaisesRegex(ValueError, "内容与格式不符"):
            import_review(self.source, self.store, self.document["site_root"], archive, self.root / "images")
        self.assertEqual(archive.read_bytes(), previous)

    async def test_MCP只导入反馈并生成报告绝不自动解决或改修复记录(self):
        self.write_document()
        with patch.object(server, "DEFAULT_VERIFY_FILE", self.root / "pending.json"), patch.object(
            server, "BUG_CACHE_FILE", self.root / "cache.json"
        ), patch.object(server, "DEFAULT_REPORT_FILE", self.root / "report.html"), patch.object(
            server.zentao_client, "site_root", self.document["site_root"]
        ), patch.object(server.zentao_client, "request", new_callable=AsyncMock) as request, patch.object(
            server, "resolve_verified_bug", new_callable=AsyncMock
        ) as resolve:
            server.save_pending_store(self.store)
            previous = server.DEFAULT_VERIFY_FILE.read_bytes()
            result = json.loads(await server.read_review_feedback(str(self.source)))
            self.assertEqual([item["status"] for item in result["items"]], ["已解决", "未解决", "继续处理", "待验证"])
            self.assertEqual(result["items"][-1]["action"], "跳过，先不动")
            self.assertTrue(server.DEFAULT_REPORT_FILE.exists())
            self.assertEqual(previous, server.DEFAULT_VERIFY_FILE.read_bytes())
            request.assert_not_called()
            resolve.assert_not_called()

    def test_重新导入保留旧备注历史并可重复读取(self):
        self.write_document()
        archive = self.root / "archive.json"
        import_review(self.source, self.store, self.document["site_root"], archive, self.root / "images")
        self.document["items"][1]["remark"] = "新的复现说明"
        self.write_document()
        import_review(self.source, self.store, self.document["site_root"], archive, self.root / "images")
        saved = json.loads(archive.read_text(encoding="utf-8"))
        self.assertEqual(saved["items"][1]["remark"], "新的复现说明")
        self.assertEqual(saved["history"][0]["remark"], "验证现象")

    def test_反馈备注中的脚本只作为文本进入报告(self):
        self.document["items"][0]["remark"] = '</script><script>window.不应执行=true</script>'
        report = render_pending_html(self.store, {}, reviews=self.document)
        self.assertNotIn(self.document["items"][0]["remark"], report)
        self.assertIn('\\u003c/script\\u003e', report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
