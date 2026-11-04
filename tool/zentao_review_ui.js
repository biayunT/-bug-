// 离线报告只收集人工验收结论，禅道写操作由对话中的 MCP 工具完成。
const reviewSeed = JSON.parse(document.getElementById('review-data').textContent);
const reviewLabels = {pending: '待验证', resolved: '已解决', unresolved: '未解决', continue: '继续处理'};
const reviews = new Map();
let reviewDB, feedbackHandle, imageJobs = 0, changeVersion = 0, draftWrites = Promise.resolve();
const feedbackMessage = document.getElementById('feedback-message');
function message(text, error = false) {
  feedbackMessage.textContent = text;
  feedbackMessage.classList.toggle('error', error);
}
function openDrafts() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('zentao-review-drafts', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('reviews');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
function draftRequest(mode, key, value) {
  return new Promise((resolve, reject) => {
    const transaction = reviewDB.transaction('reviews', mode);
    const request = mode === 'readonly' ? transaction.objectStore('reviews').get(key)
      : transaction.objectStore('reviews').put(value, key);
    transaction.oncomplete = () => resolve(request.result);
    transaction.onabort = () => reject(transaction.error);
    transaction.onerror = () => reject(transaction.error);
  });
}
function draftKey(id) { return reviewSeed.site_root + '#' + id; }
function currentItem(card, input) {
  const sameRound = input && input.revision === card.dataset.revision;
  return {bug_id: Number(card.id.slice(4)), revision: card.dataset.revision,
    status: sameRound && Object.hasOwn(reviewLabels, input.status) ? input.status : 'pending',
    remark: typeof input?.remark === 'string' ? input.remark : '',
    images: Array.isArray(input?.images) ? input.images : [],
    updated_at: input?.updated_at || '', previous_round: !!input && !sameRound};
}
function updateReview(card) {
  const id = card.id.slice(4), item = reviews.get(id);
  card.dataset.reviewStatus = item.status;
  const badge = card.querySelector('.status');
  badge.dataset.reviewStatus = item.status;
  badge.textContent = reviewLabels[item.status] + ' · 本地标记';
  const nav = document.querySelector('[data-nav="' + id + '"]');
  if (nav) nav.dataset.reviewStatus = item.status;
  card.querySelector('.review-note').textContent = item.previous_round
    ? '上一轮说明与图片已保留；本轮修改请重新验证。'
    : item.status === 'resolved' ? '表示你已验证通过，并授权我在禅道解决；处理结果以禅道为准。'
    : item.status === 'pending' ? '先不处理这项，保留当前修改。'
    : '请说明现象、复现步骤或预期，可附图；我会据此继续排查并回归。';
  const list = card.querySelector('.review-images');
  list.replaceChildren();
  item.images.forEach((image, index) => {
    const figure = document.createElement('figure');
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'review-image'; button.setAttribute('aria-label', '放大反馈图片 ' + (index + 1));
    const img = document.createElement('img'); img.src = image.data_url; img.alt = image.name;
    button.append(img); button.addEventListener('click', () => {
      preview.src = image.data_url; preview.alt = image.name;
      document.getElementById('lightbox-title').textContent = 'Bug #' + id + ' 反馈 · ' + image.name;
      dialog.showModal();
    });
    const caption = document.createElement('figcaption'); caption.textContent = image.name;
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '移除'; remove.className = 'remove-image';
    remove.addEventListener('click', () => {item.images.splice(index, 1); changed(card);});
    figure.append(button, caption, remove); list.append(figure);
  });
  for (const status of Object.keys(reviewLabels)) {
    document.querySelector('[data-review-count="' + status + '"]').textContent =
      [...reviews.values()].filter(value => value.status === status).length;
  }
  applyFilter();
}
function changed(card) {
  changeVersion++;
  const item = reviews.get(card.id.slice(4));
  item.updated_at = new Date().toISOString(); item.previous_round = false;
  updateReview(card);
  document.getElementById('save-feedback').textContent = '保存反馈文件 ●';
  message('有新标记尚未保存为反馈文件。');
  if (reviewDB) {
    // 串行提交快照，快速输入时较早的写入不能覆盖较晚的备注和附件。
    const snapshot = structuredClone(item);
    draftWrites = draftWrites.then(() => draftRequest('readwrite', draftKey(item.bug_id), snapshot))
      .catch(() => message('浏览器暂存失败，请点击“保存反馈文件”，避免丢失。', true));
  }
}
function readImage(file) {
  return new Promise((resolve, reject) => {
    if (!['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(file.type)) {
      reject(new Error('仅支持 PNG、JPEG、GIF、WebP 图片。')); return;
    }
    if (file.size > 5 * 1024 * 1024) {reject(new Error('单张图片不能超过 5 MB。')); return;}
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('图片读取失败，请重新选择。'));
    reader.onload = () => {
      const url = String(reader.result), raw = atob(url.split(',')[1] || '');
      const valid = file.type === 'image/png' ? raw.startsWith('\x89PNG\r\n\x1a\n')
        : file.type === 'image/jpeg' ? raw.startsWith('\xff\xd8\xff')
        : file.type === 'image/gif' ? /^(GIF87a|GIF89a)/.test(raw)
        : raw.startsWith('RIFF') && raw.slice(8, 12) === 'WEBP';
      valid ? resolve({name: file.name, data_url: url}) : reject(new Error('图片内容与格式不符，请使用有效截图。'));
    };
    reader.readAsDataURL(file);
  });
}
function reviewDocument() {
  return {format: 'zentao-review', version: 1, site_root: reviewSeed.site_root,
    exported_at: new Date().toISOString(), items: [...reviews.values()].map(({previous_round, ...item}) => structuredClone(item))};
}
async function saveFeedback() {
  if (imageJobs) {message('图片正在读取，请完成后再保存。', true); return;}
  for (const card of cards) {
    const item = reviews.get(card.id.slice(4));
    if (['unresolved', 'continue'].includes(item.status) && !item.remark.trim() && !item.images.length) {
      card.hidden = false; card.scrollIntoView({block: 'center'}); card.querySelector('textarea').focus();
      message('Bug #' + item.bug_id + ' 请填写说明或添加图片后再保存。', true); return;
    }
  }
  const text = JSON.stringify(reviewDocument(), null, 2);
  const savingVersion = changeVersion;
  if (new Blob([text]).size > 75 * 1024 * 1024) {message('反馈超过 75 MB，请减少图片后重新保存。', true); return;}
  const button = document.getElementById('save-feedback'); button.disabled = true;
  try {
    if ('showSaveFilePicker' in window) {
      try {
        feedbackHandle ||= await window.showSaveFilePicker({suggestedName: 'zentao-review.json',
          types: [{description: 'Bug 验收反馈', accept: {'application/json': ['.json']}}]});
      } catch (error) {
        if (error.name === 'AbortError') {message('已取消保存，标记仍保留在本浏览器。'); return;}
        if (error.name !== 'SecurityError' && error.name !== 'NotAllowedError') throw error;
      }
    }
    if (feedbackHandle) {
      const writer = await feedbackHandle.createWritable(); await writer.write(text); await writer.close();
      message('反馈文件已保存。告诉我“按反馈处理”，并提供保存位置即可。');
    } else {
      const url = URL.createObjectURL(new Blob([text], {type: 'application/json'}));
      const link = document.createElement('a'); link.href = url; link.download = 'zentao-review.json'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      message('已发起下载 zentao-review.json。请保存到 C:\\tool\\reports，再告诉我“按反馈处理”。');
    }
    if (savingVersion === changeVersion) button.textContent = '保存反馈文件';
    else message('保存期间又有新修改，请再次保存反馈文件。');
  } catch (error) {
    feedbackHandle = undefined;
    message('保存失败，标记仍在本页：' + error.message + '。请重试。', true);
  } finally {button.disabled = false;}
}
async function initializeReviews() {
  try {reviewDB = await openDrafts();}
  catch {message('浏览器无法暂存，请在关闭前保存反馈文件。', true);}
  for (const card of cards) {
    const id = card.id.slice(4);
    let input = reviewSeed.items.find(item => String(item.bug_id) === id);
    if (reviewDB) {
      try {
        const draft = await draftRequest('readonly', draftKey(id));
        if (draft && (!input || Date.parse(draft.updated_at) > (Date.parse(input.updated_at) || 0))) input = draft;
      } catch {message('部分暂存读取失败，请使用已保存的反馈文件。', true);}
    }
    const item = currentItem(card, input); reviews.set(id, item);
    const section = document.createElement('section'); section.className = 'review';
    section.innerHTML = '<h3>05 <span>我的验收反馈</span></h3><div class="review-fields"><label>处理标记<select class="review-status"><option value="pending">待验证</option><option value="resolved">已解决</option><option value="unresolved">未解决</option><option value="continue">继续处理</option></select></label></div><p class="review-note"></p><label class="remark-label">说明 / 复现步骤 / 预期<textarea rows="3" maxlength="20000" placeholder="未解决或继续处理时，请写明需要我检查的内容，也可以添加图片。"></textarea></label><div class="attachment-bar"><label class="attachment-button">＋ 添加反馈图片<input class="review-upload" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple></label><span>每项最多 10 张，单张不超过 5 MB</span></div><div class="review-error" role="alert"></div><div class="review-images"></div>';
    card.querySelector('.solution-column').append(section);
    const select = section.querySelector('select'), textarea = section.querySelector('textarea'), upload = section.querySelector('input');
    select.setAttribute('aria-label', 'Bug #' + id + ' 处理标记');
    textarea.setAttribute('aria-label', 'Bug #' + id + ' 反馈说明');
    upload.setAttribute('aria-label', 'Bug #' + id + ' 添加反馈图片');
    select.value = item.status; textarea.value = item.remark;
    select.addEventListener('change', () => {item.status = select.value; changed(card);});
    textarea.addEventListener('input', () => {item.remark = textarea.value; changed(card);});
    upload.addEventListener('change', async () => {
      const files = [...upload.files], errorBox = section.querySelector('.review-error');
      errorBox.textContent = '正在读取反馈图片…'; upload.disabled = true; imageJobs++;
      try {
        if (files.length + item.images.length > 10) throw new Error('每条 Bug 最多添加 10 张反馈图片。');
        const images = await Promise.all(files.map(readImage)); item.images.push(...images); errorBox.textContent = ''; changed(card);
      } catch (error) {errorBox.textContent = error.message;}
      finally {upload.value = ''; upload.disabled = false; imageJobs--;}
    });
    updateReview(card);
  }
  document.getElementById('save-feedback').disabled = false;
  document.getElementById('save-feedback').textContent = '保存反馈文件';
}
document.getElementById('save-feedback').addEventListener('click', saveFeedback);
const reviewsReady = initializeReviews();
