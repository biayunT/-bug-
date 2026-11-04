"""使用本机 Chrome 验证离线报告交互，不启动本地服务。"""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from zentao_report import render_pending_html
from zentao_review import record_revision, validate_review


CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")

# 复用仓库已有 ws 进行 Chrome 调试，不新增依赖，不启动前端或反馈服务。
DRIVER = r"""
const fs = require('fs'), path = require('path'), {spawn} = require('child_process'), WebSocket = require('ws');
const [chrome, url, profile] = process.argv.slice(1);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const processChrome = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--no-default-browser-check', '--disable-background-networking', '--remote-debugging-port=0',
  '--user-data-dir=' + profile, 'about:blank'], {windowsHide:true, stdio:'ignore'});
let socket, sequence=0; const requests = new Map();
function call(method, params={}, sessionId) {
  return new Promise((resolve,reject) => {const id=++sequence; requests.set(id,{resolve,reject});
    socket.send(JSON.stringify({id,method,params,sessionId}));});
}
(async () => {
  try {
    const portFile=path.join(profile,'DevToolsActivePort');
    for(let i=0;!fs.existsSync(portFile)&&i<100;i++) await delay(50);
    const [port,endpoint]=fs.readFileSync(portFile,'utf8').trim().split(/\r?\n/);
    socket=new WebSocket('ws://127.0.0.1:'+port+endpoint);
    socket.on('message',data=>{const message=JSON.parse(data);const pending=requests.get(message.id);
      if(pending){requests.delete(message.id);message.error?pending.reject(new Error(message.error.message)):pending.resolve(message.result);}});
    await new Promise((resolve,reject)=>{socket.once('open',resolve);socket.once('error',reject);});
    const {targetId}=await call('Target.createTarget',{url:'about:blank'});
    const {sessionId}=await call('Target.attachToTarget',{targetId,flatten:true});
    await call('Page.enable',{},sessionId);
    await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false},sessionId);
    await call('Page.navigate',{url},sessionId);
    let result;
    for(let i=0;i<150;i++) {
      await delay(100);
      try{const data=await call('Runtime.evaluate',{expression:'document.getElementById("browser-test-result")?.textContent',returnByValue:true},sessionId);
        if(data.result.value){result=JSON.parse(data.result.value);break;}}catch{}
    }
    if(!result) throw new Error('浏览器交互检查超时');
    process.stdout.write(JSON.stringify(result));
  } catch(error) {process.stdout.write(JSON.stringify({ok:false,error:error.message}));process.exitCode=1;}
  finally {if(socket?.readyState===1){try{await call('Browser.close');}catch{}socket.close();}processChrome.kill();}
})();
"""


HARNESS = r"""
<script>
(async () => {
  const output = document.createElement('pre'); output.id = 'browser-test-result'; document.body.append(output);
  const checks = [];
  function check(value, label) {if (!value) throw new Error(label); checks.push(label);}
  function choose(card, status) {const input = card.querySelector('.review-status'); input.value = status; input.dispatchEvent(new Event('change'));}
  function remark(card, value) {const input = card.querySelector('textarea'); input.value = value; input.dispatchEvent(new Event('input'));}
  let captured;
  Object.defineProperty(window, 'showSaveFilePicker', {configurable:true, value: async () => ({
    createWritable: async () => ({write: async text => {captured = JSON.parse(text);}, close: async () => {}})
  })});
  try {
    await reviewsReady;
    const [first, second, third] = cards;
    if (location.hash === '#reload') {
      check(first.querySelector('.review-status').value === 'resolved', '刷新保留已解决标记');
      check(second.querySelector('textarea').value.includes('复现说明'), '刷新保留备注');
      check(second.querySelectorAll('.review-image').length === 1, '刷新保留反馈截图');
      await saveFeedback();
      check(captured.items[2].status === 'pending', '待验证仍跳过');
      check(!window.不应执行, '备注只按文本显示');
      output.textContent = JSON.stringify({ok:true, checks, feedback:captured}); return;
    }
    check(cards.length === 3 && reviews.size === 3, '每条 Bug 均有验收表');
    check(third.querySelector('.review-status').value === 'pending', '上一轮已解决标记不用于本轮');
    check(third.querySelector('textarea').value === '上一轮备注', '新一轮保留旧备注');
    choose(first, 'resolved'); choose(second, 'unresolved');
    await saveFeedback();
    check(!captured && feedbackMessage.textContent.includes('请填写说明'), '缺少备注和截图时阻止保存');
    remark(second, '复现说明 </textarea><script>window.不应执行=true<\/script>');
    filter.value = 'review-unresolved'; filter.dispatchEvent(new Event('change'));
    check(first.hidden && !second.hidden && third.hidden, '按处理标记筛选');
    filter.value = 'all'; search.value = '复现说明'; search.dispatchEvent(new Event('input'));
    check(first.hidden && !second.hidden, '可以搜索反馈说明');
    search.value = ''; search.dispatchEvent(new Event('input'));
    const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    const transfer = new DataTransfer(); transfer.items.add(new File([bytes], '反馈截图.png', {type:'image/png'}));
    const upload = second.querySelector('input[type=file]');
    const invalid = new DataTransfer(); invalid.items.add(new File(['bad'], '错误.svg', {type:'image/svg+xml'}));
    upload.files = invalid.files; upload.dispatchEvent(new Event('change'));
    while (imageJobs) await new Promise(resolve => setTimeout(resolve, 20));
    check(second.querySelector('.review-error').textContent.includes('仅支持'), '错误附件有提示');
    upload.files = transfer.files; upload.dispatchEvent(new Event('change'));
    while (imageJobs) await new Promise(resolve => setTimeout(resolve, 20));
    check(second.querySelectorAll('.review-image').length === 1, '添加真实反馈图片');
    second.querySelector('.review-image').click(); check(dialog.open, '反馈图片可以放大'); dialog.close();
    choose(second, 'continue');
    const validPicker = window.showSaveFilePicker;
    window.showSaveFilePicker = async () => {throw new DOMException('取消', 'AbortError');};
    await saveFeedback();
    check(!captured && feedbackMessage.textContent.includes('取消'), '取消保存保留本页反馈');
    window.showSaveFilePicker = async () => ({createWritable:async () => {throw new Error('无法写入');}});
    await saveFeedback();
    check(!captured && feedbackMessage.textContent.includes('保存失败'), '写入失败有提示且允许重试');
    let downloadName, downloadedBlob;
    const originalURL = URL.createObjectURL, originalClick = HTMLAnchorElement.prototype.click;
    URL.createObjectURL = blob => {downloadedBlob = blob; return 'blob:测试';};
    HTMLAnchorElement.prototype.click = function(){downloadName = this.download;};
    window.showSaveFilePicker = async () => {throw new DOMException('不可用', 'SecurityError');};
    await saveFeedback();
    check(downloadName === 'zentao-review.json' && JSON.parse(await downloadedBlob.text()).items.length === 3, '不支持文件选择时下载完整反馈');
    URL.createObjectURL = originalURL; HTMLAnchorElement.prototype.click = originalClick;
    window.showSaveFilePicker = validPicker;
    await saveFeedback();
    check(captured.items[0].status === 'resolved' && captured.items[1].status === 'continue', '反馈文件包含各项标记');
    check(captured.items[1].images[0].data_url.startsWith('data:image/png;base64,'), '反馈文件包含图片本体');
    check(!window.不应执行, '备注不能注入脚本');
    check(document.documentElement.scrollWidth <= innerWidth, '窄屏无横向溢出');
    await draftWrites;
    location.hash = 'reload'; location.reload();
  } catch (error) {output.textContent = JSON.stringify({ok:false, checks, error:error.message, stack:error.stack});}
})();
</script>
"""


@unittest.skipUnless(CHROME.exists(), "未安装 Chrome，无法运行浏览器检查")
class 离线报告浏览器检查(unittest.TestCase):
    def test_标记备注图片保存刷新与窄屏(self):
        store = {str(i): {"title": "测试问题 " + str(i), "solution": "这是修改方案"} for i in range(12, 15)}
        reviews = {"items": [{"bug_id": 14, "revision": "旧轮次", "status": "resolved", "remark": "上一轮备注", "images": []}]}
        document = render_pending_html(store, {}, "https://example.test/pms", reviews=reviews)
        document = document.replace('</body>', HARNESS + '</body>')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "browser.html"
            fixture.write_text(document, encoding="utf-8")
            result = subprocess.run(['node', '-e', DRIVER, str(CHROME), fixture.as_uri(), str(root / 'profile')],
                                    cwd='C:/数字教材/digital-textbook-web', capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            text = result.stdout.decode('utf-8', errors='replace')
            self.assertTrue(text, '浏览器未返回结果：' + result.stderr.decode('utf-8', errors='replace'))
            output = json.loads(text)
            self.assertTrue(output["ok"], str(output))
            # 浏览器实际保存的 JSON 再交给 Python 导入契约验证，覆盖两端字段与附件的一致性。
            items = validate_review(output["feedback"], store, "https://example.test/pms")
            self.assertEqual([item["status"] for item in items], ["resolved", "continue", "pending"])
            self.assertEqual(items[0]["revision"], record_revision(store["12"]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
