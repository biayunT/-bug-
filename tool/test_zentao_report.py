"""HTML 报告与截图下载的回归检查；全部使用临时数据，不修改真实禅道。"""

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import zentao_mcp_server as server
from zentao_report import render_pending_html, write_pending_report


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class 报告渲染检查(unittest.TestCase):
    def test_方案与根因独立展示且旧记录不会伪造方案(self):
        data = {"12": {"title": "标题", "root_cause": "这是根因", "solution": "这是实际修改方案"}, "13": {"root_cause": "旧根因"}}
        report = render_pending_html(data, {})
        self.assertIn("这是根因", report)
        self.assertIn("这是实际修改方案", report)
        self.assertIn("旧记录未单独填写修改方案", report)
        self.assertEqual(report.count('class="bug-card"'), 2)

    def test_原始描述和文件内容不能注入脚本(self):
        payload = '<script>alert("秘密")</script>'
        data = {"12": {"title": payload, "solution": payload, "notes": payload, "modified_files": payload}}
        report = render_pending_html(data, {"12": {"steps": payload}})
        self.assertNotIn(payload, report)
        self.assertIn("&lt;script&gt;", report)
        self.assertNotIn("@@CARDS@@", report)

    def test_截图嵌入后报告不依赖原始文件(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "测试.png"
            image.write_bytes(PNG)
            report = write_pending_report(Path(directory) / "报告.html", {"12": {"title": "测试"}}, {"12": {"screenshot_paths": [str(image)]}})
            image.unlink()
            document = report.read_text(encoding="utf-8")
            self.assertIn("data:image/png;base64,", document)
            self.assertNotIn(str(image), document)
            self.assertIn("修复前的问题记录", document)

    def test_空列表和截图失效有明确展示(self):
        report = render_pending_html({}, {})
        self.assertIn("待验证 Bug", report)
        report = render_pending_html({"12": {}}, {"12": {"screenshot_paths": ["Z:/不存在/截图.png"]}})
        self.assertIn("该截图无法读取", report)


class 服务集成检查(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.patches = [
            patch.object(server, "DEFAULT_VERIFY_FILE", root / "pending.json"),
            patch.object(server, "BUG_CACHE_FILE", root / "cache.json"),
            patch.object(server, "DEFAULT_REPORT_FILE", root / "report.html"),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def test_重复登记保留已填写方案且不请求禅道(self):
        with patch.object(server.zentao_client, "request", new_callable=AsyncMock) as request:
            await server.record_pending_verification(12, "测试", "根因", "a.py", "步骤", solution="方案", notes="待核实")
            await server.record_pending_verification(12, "测试更新", "根因更新", "b.py", "步骤更新")
            record = server.load_pending_store()["12"]
            self.assertEqual(record["solution"], "方案")
            self.assertEqual(record["notes"], "待核实")
            self.assertTrue(server.DEFAULT_REPORT_FILE.exists())
            request.assert_not_called()

    async def test_离线导出不请求禅道且保留待验证记录(self):
        server.save_pending_store({"12": {"title": "测试", "solution": "修改方案"}})
        previous = server.DEFAULT_VERIFY_FILE.read_bytes()
        with patch.object(server.zentao_client, "request", new_callable=AsyncMock) as request:
            result = await server.export_pending_report(refresh_details=False)
            self.assertIn("HTML", result)
            self.assertEqual(server.DEFAULT_VERIFY_FILE.read_bytes(), previous)
            request.assert_not_called()

    async def test_详情刷新失败仍能用缓存导出(self):
        server.save_pending_store({"12": {"title": "测试"}})
        server.save_bug_cache({"12": {"title": "缓存标题"}})
        with patch.object(server.zentao_client, "request", new_callable=AsyncMock, side_effect=RuntimeError("不可用")):
            result = await server.export_pending_report()
            self.assertIn("详情刷新失败", result)
            self.assertIn("缓存标题", server.DEFAULT_REPORT_FILE.read_text(encoding="utf-8"))

    async def test_截图使用文件接口且不会重复拼接部署路径(self):
        client = server.ZenTaoClient()
        client.site_root = "https://example.test/pms"
        client.api_v1 = client.site_root + "/api.php/v1"
        client._token = "fixture-token"
        client.account = "测试账号"
        client.password = "测试密码"
        requested = []

        def respond(request):
            requested.append(request)
            return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

        real_async_client = httpx.AsyncClient
        with patch.object(server.httpx, "AsyncClient", side_effect=lambda **kw: real_async_client(transport=httpx.MockTransport(respond), **kw)):
            image = Path(self.directory.name) / "图片.png"
            await client.download_file("/pms/index.php?m=file&amp;f=read&amp;fileID=99", image)
            self.assertEqual(str(requested[0].url), "https://example.test/pms/api.php/v1/files/99")
            self.assertEqual(len(requested[0].headers.get_list("Token")), 1)
            self.assertEqual(image.read_bytes(), PNG)

    async def test_登录网页不能冒充下载成功的截图(self):
        client = server.ZenTaoClient()
        client.site_root = "https://example.test/pms"
        client.api_v1 = client.site_root + "/api.php/v1"
        real_async_client = httpx.AsyncClient
        with patch.object(client, "get_token", new_callable=AsyncMock, return_value="fixture-token"), patch.object(
            server.httpx, "AsyncClient", side_effect=lambda **kw: real_async_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html>登录</html>")), **kw)
        ):
            image = Path(self.directory.name) / "不应生成.png"
            with self.assertRaisesRegex(ValueError, "不是有效截图"):
                await client.download_file("/pms/index.php?fileID=99", image)
            self.assertFalse(image.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
