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
const [chrome, url, profile, secureFixture] = process.argv.slice(1);
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
    socket.on('message',data=>{const message=JSON.parse(data);
      if(message.method==='Fetch.requestPaused') {
        call('Fetch.fulfillRequest',{requestId:message.params.requestId,responseCode:200,
          responseHeaders:[{name:'Content-Type',value:'text/html; charset=utf-8'}],
          body:fs.readFileSync(secureFixture).toString('base64')},message.sessionId).catch(()=>{});
      }
      const pending=requests.get(message.id);
      if(pending){requests.delete(message.id);message.error?pending.reject(new Error(message.error.message)):pending.resolve(message.result);}});
    await new Promise((resolve,reject)=>{socket.once('open',resolve);socket.once('error',reject);});
    const {targetId}=await call('Target.createTarget',{url:'about:blank'});
    const {sessionId}=await call('Target.attachToTarget',{targetId,flatten:true});
    await call('Page.enable',{},sessionId);
    // 在浏览器内拦截测试域名并返回本地 HTML，无网络请求，也不启动服务。
    // 安全来源允许用真实私有文件句柄验证落盘和 IndexedDB 恢复。
    if(secureFixture) await call('Fetch.enable',{patterns:[{urlPattern:'https://zentao-review.test/*'}]},sessionId);
    await call('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false},sessionId);
    await call('Page.navigate',{url},sessionId);
    let result;
    for(let i=0;i<250;i++) {
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
    check(first.querySelector('textarea').value === '旧版浏览器暂存', '数据库升级保留原有人工反馈：' + first.querySelector('textarea').value + '；' + feedbackMessage.textContent);
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
    feedbackHandle = undefined;
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


# 使用浏览器真实文件句柄和 IndexedDB，验证自动保存与刷新后的授权恢复，不假冒落盘成功。
AUTO_SAVE_HARNESS = r"""
<script>
(async () => {
  const output = document.createElement('pre'); output.id = 'browser-test-result'; document.body.append(output);
  const checks = [];
  function check(value, label) {if (!value) throw new Error(label); checks.push(label);}
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  async function until(test, label) {
    for (let i=0; i<100; i++) {
      // 文件替换期间先前取得的 Blob 可能失效，下一次轮询重新取得文件即可。
      try {if (await test()) {checks.push(label); return;}}
      catch(error) {if (!['NotFoundError', 'NotReadableError'].includes(error.name)) throw error;}
      await delay(30);
    }
    throw new Error(label + '：超时');
  }
  function choose(card, status) {const select=card.querySelector('select'); select.value=status; select.dispatchEvent(new Event('change'));}
  function remark(card, value) {const textarea=card.querySelector('textarea'); textarea.value=value; textarea.dispatchEvent(new Event('input'));}
  try {
    await reviewsReady;
    const root = await navigator.storage.getDirectory();
    const file = await root.getFileHandle('zentao-review.json', {create:true});
    const read = async () => JSON.parse(await (await file.getFile()).text());
    if (location.hash === '#reload') {
      check(feedbackHandle?.name === 'zentao-review.json', '刷新恢复真实文件句柄');
      remark(cards[0], '刷新后自动保存的备注');
      await until(async () => (await read()).items[0].remark === '刷新后自动保存的备注', '刷新后修改自动写入文件');
      // 写权限失效时后台不得弹框或声称已保存；点击按钮才能恢复。
      let requested = 0;
      feedbackHandle = {name:file.name, queryPermission:async ()=>'prompt',
        requestPermission:async ()=>{requested++;return 'granted';}, createWritable:()=>file.createWritable()};
      remark(cards[0], '等待授权的备注');
      await until(() => feedbackMessage.textContent.includes('授权已失效'), '后台失权明确提示未保存');
      check(requested === 0 && (await read()).items[0].remark !== '等待授权的备注', '后台不请求授权且文件不被误写');
      await saveFeedback();
      check(requested === 1 && (await read()).items[0].remark === '等待授权的备注', '点击保存恢复授权并落盘');
      let aborted = false;
      feedbackHandle = {name:file.name, createWritable:async()=>{
        const writer = await file.createWritable();
        return {write:async()=>{throw new Error('磁盘写入失败');}, close:()=>writer.close(),
          abort:async()=>{await writer.abort();aborted=true;}};
      }};
      remark(cards[0], '失败后重试的备注');
      await until(() => feedbackMessage.textContent.includes('保存失败'), '自动写入失败有明确提示');
      check((await read()).items[0].remark !== '失败后重试的备注', '失败不伪报已保存');
      check(aborted, '写入失败释放文件锁');
      feedbackHandle = file; await saveFeedback();
      check((await read()).items[0].remark === '失败后重试的备注', '写入失败后可手动重试');
      check(!saving, '文件写入完成后恢复按钮');
      output.textContent = JSON.stringify({ok:true, checks, feedback:await read()}); return;
    }
    Object.defineProperty(window, 'showSaveFilePicker', {configurable:true, value:async()=>file});
    await saveFeedback();
    check((await read()).items.length === 3, '首次绑定保存完整反馈');
    choose(cards[0], 'resolved');
    check(saving, '切换已解决标记立即开始保存，无需等待输入防抖');
    await until(async () => (await read()).items[0].status === 'resolved', '标记已解决自动写入JSON');
    await until(() => !saving, '标记保存完成后可继续编辑');
    remark(cards[0], '失去焦点立即保存的备注');
    cards[0].querySelector('textarea').dispatchEvent(new Event('blur'));
    check(saving, '输入框失去焦点立即开始保存');
    await until(async () => (await read()).items[0].remark === '失去焦点立即保存的备注', '失焦备注实际写入JSON');
    await until(() => !saving, '失焦保存完成后可继续输入');
    remark(cards[0], '快速输入一'); remark(cards[0], '快速输入最终备注');
    await until(async () => (await read()).items[0].remark === '快速输入最终备注', '连续输入保留最终备注');
    const bytes=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='), c=>c.charCodeAt(0));
    const files=new DataTransfer(); files.items.add(new File([bytes], '自动保存截图.png', {type:'image/png'}));
    const upload=cards[1].querySelector('input[type=file]'); upload.files=files.files; upload.dispatchEvent(new Event('change'));
    await until(async () => (await read()).items[1].images.length === 1, '新增截图自动保存图片本体');
    cards[1].querySelector('.remove-image').click();
    await until(async () => (await read()).items[1].images.length === 0, '删除截图自动更新JSON');
    choose(cards[1], 'unresolved');
    await until(() => feedbackMessage.textContent.includes('请填写说明'), '缺少说明时自动保存提示补充');
    check((await read()).items[1].status === 'pending', '校验失败不覆盖上次有效文件');
    remark(cards[1], '用户提供的复现说明');
    await until(async () => (await read()).items[1].status === 'unresolved', '补全说明后自动保存');
    // 模拟慢磁盘，保存中的新修改也必须串行写入，不能被旧快照覆盖。
    let active=0, maximum=0;
    feedbackHandle={name:file.name, createWritable:async()=>{
      active++; maximum=Math.max(maximum,active); const writer=await file.createWritable();
      return {write:async text=>{await delay(850);await writer.write(text);}, close:async()=>{await writer.close();active--;}};
    }};
    remark(cards[0], '写入开始');
    await until(()=>active===1, '慢磁盘写入已开始');
    remark(cards[0], '写入期间新增的最终备注');
    await until(async ()=>(await read()).items[0].remark==='写入期间新增的最终备注', '写入期间变更会继续自动保存');
    check(maximum===1, '自动写入串行且不互相覆盖');
    await until(()=>!saving, '慢磁盘保存结束');
    feedbackHandle=file; await saveFeedback(); await draftWrites;
    location.hash='reload'; location.reload();
  } catch(error) {output.textContent=JSON.stringify({ok:false,checks,error:error.message,stack:error.stack});}
})();
</script>
"""


@unittest.skipUnless(CHROME.exists(), "未安装 Chrome，无法运行浏览器检查")
class 离线报告浏览器检查(unittest.TestCase):
    def test_真实文件自动保存刷新授权与失败重试(self):
        store = {str(i): {"title": "自动保存检查 " + str(i), "solution": "修改方案"} for i in range(12, 15)}
        document = render_pending_html(store, {}, "https://example.test/pms")
        document = document.replace('</body>', AUTO_SAVE_HARNESS + '</body>')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / 'auto-save.html'
            fixture.write_text(document, encoding='utf-8')
            result = subprocess.run(['node', '-e', DRIVER, str(CHROME), 'https://zentao-review.test/report',
                                     str(root / 'profile'), str(fixture)],
                                    cwd='C:/数字教材/digital-textbook-web', capture_output=True, timeout=35,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            text = result.stdout.decode('utf-8', errors='replace')
            self.assertTrue(text, result.stderr.decode('utf-8', errors='replace'))
            output = json.loads(text)
            self.assertTrue(output['ok'], str(output))
            items = validate_review(output['feedback'], store, 'https://example.test/pms')
            self.assertEqual([item['status'] for item in items], ['resolved', 'unresolved', 'pending'])

    def test_标记备注图片保存刷新与窄屏(self):
        store = {str(i): {"title": "测试问题 " + str(i), "solution": "这是修改方案"} for i in range(12, 15)}
        reviews = {"items": [{"bug_id": 14, "revision": "旧轮次", "status": "resolved", "remark": "上一轮备注", "images": []}]}
        document = render_pending_html(store, {}, "https://example.test/pms", reviews=reviews)
        draft = {"bug_id": 12, "revision": record_revision(store["12"]), "status": "pending",
                 "remark": "旧版浏览器暂存", "images": [], "updated_at": "2026-11-04T12:00:00Z"}
        # 先建立上一版数据库，再加载新版页面，检查升级时不删除既有草稿。
        bootstrap = '''<script>
const oldDB = indexedDB.open('zentao-review-drafts', 1);
oldDB.onupgradeneeded = () => oldDB.result.createObjectStore('reviews');
oldDB.onsuccess = () => {
  const db=oldDB.result, tx=db.transaction('reviews','readwrite');
  tx.objectStore('reviews').put(DRAFT, 'https://example.test/pms#12');
  tx.oncomplete=()=>db.close();
};
</script>'''.replace('DRAFT', json.dumps(draft, ensure_ascii=False))
        document = document.replace('<head>', '<head>' + bootstrap)
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
