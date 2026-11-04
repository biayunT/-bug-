# 禅道 MCP 的 HTML 待验证报告

报告默认输出到 `C:/tool/reports/zentao-pending.html`。双击即可在浏览器打开，不用启动服务。图片嵌入 HTML，复制单个 HTML 文件也能离线查看。

每项展示禅道原始标题、问题文字、根因、独立修改方案、验证步骤、实际检查记录、待核实事项及修改文件；原始截图可点击放大。支持搜索、筛选、目录跳转及打印为 PDF。

## 在 HTML 中验收

每项下方都有“我的验收反馈”，支持以下标记：

| 标记 | 收到“按反馈处理”后的动作 |
| --- | --- |
| 已解决 | 表示你已验证通过并授权在禅道解决，我会逐条调用原有解决工具 |
| 未解决 | 根据备注、复现步骤和图片重新排查，再回归验证 |
| 继续处理 | 根据你补充的说明与图片继续修改，改后重新登记待验证 |
| 待验证 | 跳过，保留当前代码，不继续处理 |

“未解决”和“继续处理”至少填写一段说明或添加一张图片。每项最多 10 张，单张不超过 5 MB，支持 PNG、JPEG、GIF 和 WebP。所有反馈图片都嵌入反馈 JSON，我读取后会提取到本地目录查看。

标记、说明和图片会暂存在当前浏览器的 IndexedDB 中，刷新可恢复；清理浏览器数据或换浏览器不会自动携带暂存。点击顶部“保存反馈文件”，保存到 `C:/tool/reports/zentao-review.json`。支持文件保存选择器的浏览器会弹出路径选择；不支持时会下载 JSON。保存后在对话中说“按反馈处理”；如果放在其他位置，一并提供路径即可。离线 HTML 不能自行通知代理，也不能直接修改禅道。

反馈导入后会保留在待验证记录旁的 `_review.json` 档案中，旧备注不会覆盖掉修复方案；反馈变更会留历史。若同一 Bug 的修改记录更新，上一轮说明和图片仍保留，本轮标记重置为待验证，旧的已解决反馈不能用于新一轮修改。报告中的“已解决 · 本地标记”表示用户验收结论，实际禅道状态以解决工具的结果为准。

## 工具用法

- `record_pending_verification`：保留原参数，新增可选 `solution`、`test_results`、`notes`。分别填写实际修改、实际检查结果和待核实事项。登记后立即生成 HTML；旧记录不会被删除，重复登记未提供新字段时保留已有内容。
- `list_pending_verifications`：使用本地待验证记录和缓存截图重新生成报告，返回路径。
- `export_pending_report`：默认通过只读 GET 刷新禅道原始详情、下载截图并导出报告。参数 `refresh_details=false` 可离线导出；`include_images=false` 可导出不含截图的轻量报告；`output_path` 可指定其他 `.html` 路径。
- `download_bug_images`：从禅道 REST 文件端点下载截图，修复部署前缀重复拼接及网页附件跳转登录的问题；不是有效图片的响应会明确报错。
- `read_review_feedback(feedback_path="")`：导入用户保存的反馈 JSON，返回 Bug ID、标记、说明和解包后的本地图片路径；默认读取报告目录的 `zentao-review.json`。只登记本地反馈并刷新报告，不调用禅道、不修改源码。用户在后续对话要求“按反馈处理”后，代理依上述四类执行；待验证项跳过。

所有导出和登记操作均不更新禅道状态。只有用户后续明确表示人工验证通过且可以解决时，才允许使用原有的解决工具。

## 配置与文件

沿用 `C:/Users/zving/.codex/config.toml` 的 `[mcp_servers.zentao.env]`。现有禅道地址、账号和密码不需要重新配置。

可选设置 `ZENTAO_REPORT_FILE` 改变默认报告路径。`ZENTAO_VERIFY_FILE` 仍控制原 JSON 记录位置；只读详情及截图路径缓存在该 JSON 旁的 `_bugs.json` 文件。截图下载在默认报告目录的 `screenshots` 子目录。

`zentao_report.py`、`zentao_report_template.html`、`zentao_review.py` 和 `zentao_review_ui.js` 应与 `zentao_mcp_server.py` 放在同一目录。生成的 HTML 仍是单文件，复制 HTML 即可查看，不依赖这些源文件。未增加第三方依赖。需要重启当前 MCP 连接（或 Codex）后，新增工具及参数才会载入；已生成的 HTML 现在即可打开。

## 检查

在 `C:/tool` 执行 `C:/tool/.zentao-venv/Scripts/python.exe -m unittest -v test_zentao_report`，覆盖旧记录、方案缺失、HTML 转义、离线图片、截图失败、路径修正及不回写禅道等行为。

`test_zentao_review` 覆盖四种反馈、版本校验、备注历史、图片校验解包、读取反馈不回写禅道。`test_zentao_review_browser` 使用本机 Chrome 和仓库已安装的 ws，通过浏览器真实检查标记、备注、图片添加与放大、保存后的 JSON、刷新恢复及窄屏布局；不启动前端或反馈服务。
