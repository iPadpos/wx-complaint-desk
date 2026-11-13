// 仅预填表单：证书归属、有效期和私钥配套仍由保存时的后端校验。
function certificateSerial(pem) {
  const match = pem.trim().match(/^-----BEGIN CERTIFICATE-----\s+([A-Za-z0-9+/=\s]+)-----END CERTIFICATE-----$/);
  if (!match) throw new Error('请上传有效的 PEM 证书文件');
  const bytes = Uint8Array.from(atob(match[1].replace(/\s/g, '')), char => char.charCodeAt(0));
  const read = (offset, limit) => {
    if (offset + 2 > limit) throw new Error('证书内容不完整');
    const tag = bytes[offset++];
    let size = bytes[offset++];
    if (size & 0x80) {
      const count = size & 0x7f;
      if (!count || count > 4 || offset + count > limit) throw new Error('证书格式无法读取');
      size = 0;
      for (let i = 0; i < count; i++) size = size * 256 + bytes[offset++];
    }
    if (offset + size > limit) throw new Error('证书内容不完整');
    return { tag, start: offset, end: offset + size };
  };
  const certificate = read(0, bytes.length);
  if (certificate.tag !== 0x30 || certificate.end !== bytes.length) throw new Error('证书格式无法读取');
  const body = read(certificate.start, certificate.end);
  if (body.tag !== 0x30) throw new Error('证书格式无法读取');
  const algorithm = read(body.end, certificate.end);
  const signature = read(algorithm.end, certificate.end);
  if (algorithm.tag !== 0x30 || signature.tag !== 0x03 || signature.end !== certificate.end) {
    throw new Error('证书格式无法读取');
  }
  let serial = read(body.start, body.end);
  if (serial.tag === 0xa0) serial = read(serial.end, body.end);
  if (serial.tag !== 0x02 || serial.start === serial.end || (bytes[serial.start] & 0x80)) {
    throw new Error('无法读取证书序列号');
  }
  return Array.from(bytes.slice(serial.start, serial.end), byte => byte.toString(16).padStart(2, '0'))
    .join('').replace(/^0+/, '').toUpperCase() || '0';
}

document.querySelectorAll('[data-credential-upload]').forEach(field => {
  const input = field.querySelector('input[type="file"]');
  const name = field.querySelector('[data-file-name]');
  const caption = field.querySelector('[data-file-caption]');
  const clear = field.querySelector('[data-file-clear]');
  const status = field.querySelector('[data-file-status]');
  const select = field.querySelector('[data-file-select]');
  const savedName = field.dataset.savedName || '';
  const savedCaption = field.dataset.savedCaption || '';
  const target = input.form.elements.namedItem(input.dataset.certificateTarget || '');
  const preview = field.querySelector('[data-certificate-preview]');
  let readVersion = 0;
  let autoValue = null;
  let previousValue = target ? target.value : '';
  const readCertificate = async file => {
    const version = ++readVersion;
    if (!target) return;
    if (autoValue !== null && target.value === autoValue) target.value = previousValue;
    autoValue = null;
    preview.hidden = true;
    preview.classList.remove('error-text');
    if (!file) return;
    const valueBeforeRead = target.value;
    const show = (text, error = false) => {
      preview.textContent = text;
      preview.classList.toggle('error-text', error);
      preview.hidden = false;
    };
    try {
      if (!file.size || file.size > 64 * 1024) throw new Error('请选择非空 PEM 文件，最大 64 KB');
      show('正在读取证书…');
      const pem = await file.text();
      if (version !== readVersion || input.files[0] !== file) return;
      if (input.dataset.allowPublicKey && /^\s*-----BEGIN (RSA )?PUBLIC KEY-----/.test(pem)) {
        show('公钥文件不含公钥 ID，请填写对应的 PUB_KEY_ID。');
        return;
      }
      const serial = certificateSerial(pem);
      if (target.value !== valueBeforeRead) {
        show('已读取序列号；已保留你在读取期间手动修改的内容。');
        return;
      }
      previousValue = valueBeforeRead;
      target.value = serial;
      autoValue = serial;
      show('已读取并填入证书序列号，保存后生效。');
    } catch (error) {
      if (version === readVersion && input.files[0] === file) {
        show('未能读取证书序列号，请检查文件；已保留原填写值。', true);
      }
    }
  };
  const update = () => {
    const file = input.files[0];
    name.textContent = file ? file.name : savedName || '未选择文件';
    name.title = file ? file.name : savedName;
    caption.textContent = file ? `${Math.max(1, Math.ceil(file.size / 1024))} KB · ${savedName ? '保存后替换现有配置' : '保存时上传'}` : savedCaption || 'PEM 格式，最大 64 KB';
    status.textContent = file ? '待保存' : savedName ? '已配置' : '未配置';
    status.classList.toggle('amber', Boolean(file));
    status.classList.toggle('processed', !file && Boolean(savedName));
    status.classList.toggle('neutral', !file && !savedName);
    select.textContent = file ? '重新选择' : savedName ? '更换文件' : '选择文件';
    clear.hidden = !file;
    field.classList.toggle('has-selected-file', Boolean(file));
    field.classList.toggle('has-saved-file', !file && Boolean(savedName));
    readCertificate(file);
  };
  input.addEventListener('change', update);
  clear.addEventListener('click', () => {
    input.value = '';
    update();
    input.focus();
  });
  update();
});

document.querySelectorAll('[data-merchant-config]').forEach(form => {
  const mode = form.querySelector('[name="access_mode"]');
  const channelFields = form.querySelector('[data-channel-fields]');
  const credentials = form.querySelector('[data-direct-credentials]');
  const update = () => {
    const usesChannel = mode.value === 'channel';
    channelFields.hidden = !usesChannel;
    credentials.hidden = usesChannel;
    channelFields.querySelectorAll('input,select,textarea').forEach(input => { input.disabled = !usesChannel; });
    credentials.querySelectorAll('input,select,textarea').forEach(input => { input.disabled = usesChannel; });
    form.querySelector('[name="channel"]').required = usesChannel;
    form.querySelector('label[for="id_mch_id"]').textContent = usesChannel ? '微信子商户号:' : '微信商户号:';
  };
  mode.addEventListener('change', update);
  update();
});

document.querySelectorAll('[data-toggle]').forEach(button => {
  button.addEventListener('click', () => {
    const target = document.getElementById(button.dataset.toggle);
    target.hidden = !target.hidden;
    button.setAttribute('aria-expanded', String(!target.hidden));
  });
});
document.querySelectorAll('form').forEach(form => {
  const content = form.querySelector('textarea[name="content"]');
  const count = form.querySelector('.character-count');
  if (content && count) content.addEventListener('input', () => { count.textContent = `${[...content.value].length} / 500`; });
  const input = form.querySelector('[data-evidence]');
  if (input) {
    const output = form.querySelector('.upload-results');
    const media = form.querySelector('[name="media"]');
    const ids = media ? JSON.parse(media.value || '[]') : [];
    const render = (message = '') => {
      output.replaceChildren();
      ids.forEach((id, index) => {
        const row = document.createElement('p'); row.textContent = `图片 ${index + 1} · ${id}`;
        const remove = document.createElement('button'); remove.type='button'; remove.textContent='移除';
        remove.addEventListener('click', () => { ids.splice(index,1); render(); });
        row.append(remove); output.append(row);
      });
      if (message) {
        const status = document.createElement('p'); status.textContent = message; output.append(status);
      }
      if (media) media.value = JSON.stringify(ids);
    };
    render();
    let uploading = false;
    const uploadFiles = async files => {
      if (uploading) return;
      if (files.length + ids.length > 4) { render('最多上传4张图片'); input.value = ''; return; }
      uploading = true;
      input.disabled = true;
      form.querySelectorAll('button[data-send]').forEach(button => { button.disabled = true; });
      try {
        for (const file of files) {
          if (file.size > 2 * 1024 * 1024) throw new Error('图片大小不能超过2MB');
          const data = new FormData(); data.append('file', file);
          render(`正在上传 ${file.name}…`);
          const response = await fetch(form.dataset.uploadUrl, { method:'POST', body:data,
            headers:{'X-CSRFToken':form.querySelector('[name="csrfmiddlewaretoken"]').value}, credentials:'same-origin' });
          if (response.redirected) throw new Error('登录已过期，请刷新后重新登录');
          const result = await response.json();
          if (!response.ok) throw new Error(result.error || '图片上传失败');
          ids.push(result.media_id);
          render();
        }
      } catch (error) { render(error.message); }
      finally { uploading=false; input.disabled=false; input.value=''; form.querySelectorAll('button[data-send]').forEach(button => { button.disabled=false; }); }
    };
    input.addEventListener('change', () => uploadFiles([...input.files]));
    if (content && form.hasAttribute('data-paste-images')) {
      content.placeholder = '输入回复，可直接粘贴图片…';
      content.addEventListener('paste', event => {
        const files = [...(event.clipboardData?.items || [])]
          .filter(item => item.kind === 'file' && item.type.startsWith('image/'))
          .map(item => item.getAsFile()).filter(Boolean);
        if (!files.length) return;
        event.preventDefault();
        if (uploading) {
          render('图片正在上传，请稍候再粘贴');
          return;
        }
        uploadFiles(files);
      });
    }
  }
  form.addEventListener('submit', event => {
    if (!form.checkValidity()) return;
    const button = event.submitter;
    if (button) { setTimeout(() => { button.disabled=true; button.textContent='提交中…'; },0); }
  });
});

// 原生弹窗承载现有详情/表单，内部跳转不改变外层列表。
const complaintDialog = document.getElementById('complaint-dialog');
if (complaintDialog) {
  const frame = complaintDialog.querySelector('iframe');
  const loading = complaintDialog.querySelector('.dialog-loading');
  const listPane = document.querySelector('.shell > .main');
  let opener;
  let scrollPosition = 0;
  const openComplaint = (url, element) => {
    opener = element;
    scrollPosition = listPane ? listPane.scrollTop : window.scrollY;
    loading.hidden = false;
    loading.textContent = '正在加载投诉…';
    frame.hidden = true;
    frame.src = url;
    complaintDialog.showModal();
    document.documentElement.classList.add('complaint-dialog-open');
  };
  document.querySelectorAll('.complaint-row').forEach(row => {
    row.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
      if (event.target.closest('a') && !event.target.closest('[data-open-complaint]')) return;
      event.preventDefault();
      openComplaint(row.dataset.detailUrl, row.querySelector('[data-open-complaint]'));
    });
  });
  frame.addEventListener('load', () => {
    if (!complaintDialog.open || !frame.getAttribute('src')) return;
    try {
      if (!frame.contentDocument?.body?.childElementCount) throw new Error('empty');
    } catch {
      loading.textContent = '详情加载失败，请关闭后重试。';
      return;
    }
    loading.hidden = true;
    frame.hidden = false;
  });
  complaintDialog.querySelector('.dialog-close').addEventListener('click', () => complaintDialog.close());
  complaintDialog.addEventListener('click', event => {
    if (event.target !== complaintDialog) return;
    const box = complaintDialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) complaintDialog.close();
  });
  complaintDialog.addEventListener('close', () => {
    document.documentElement.classList.remove('complaint-dialog-open');
    frame.removeAttribute('src');
    opener?.focus({preventScroll: true});
    if (listPane) listPane.scrollTop = scrollPosition;
    else window.scrollTo(0, scrollPosition);
  });
  window.addEventListener('message', event => {
    if (event.origin !== location.origin || event.source !== frame.contentWindow) return;
    if (event.data?.type === 'close-complaint') {
      complaintDialog.close();
      return;
    }
    if (event.data?.type !== 'complaint-updated') return;
    const data = event.data;
    const row = document.querySelector(`.complaint-row[data-complaint-id="${data.id}"]`);
    if (row) {
      const badge = row.querySelector('[data-complaint-state]');
      badge.className = `badge ${data.state.toLowerCase()}`;
      badge.textContent = data.label;
      row.querySelector('[data-incoming]').hidden = !data.incoming;
    }
    Object.entries(data.stats).forEach(([key, value]) => {
      const count = document.querySelector(`[data-stat="${key}"]`);
      if (count) count.textContent = value;
    });
  });
  if (complaintDialog.dataset.initialUrl) openComplaint(complaintDialog.dataset.initialUrl);
}
if (window.parent !== window && document.documentElement.classList.contains('embedded-document')) {
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !event.defaultPrevented) {
      event.preventDefault();
      window.parent.postMessage({type: 'close-complaint'}, location.origin);
    }
  });
}
const chatDetail = document.querySelector('.chat-detail');
if (chatDetail) {
  const info = chatDetail.querySelector('.complaint-info');
  const narrowScreen = window.matchMedia('(max-width: 720px)');
  const adaptInfo = () => { if (info) info.open = !narrowScreen.matches; };
  adaptInfo();
  narrowScreen.addEventListener('change', adaptInfo);
  const conversation = chatDetail.querySelector('.chat-conversation');
  conversation.scrollTop = conversation.scrollHeight;
  if (window.parent !== window) {
    window.parent.postMessage({type: 'complaint-updated', id: chatDetail.dataset.complaintId,
      state: chatDetail.dataset.state, label: chatDetail.dataset.stateLabel,
      incoming: chatDetail.dataset.incoming === 'true',
      stats: JSON.parse(document.getElementById('complaint-stats').textContent)}, location.origin);
  }
}

// 拖动调整商户号顺序：松手后提交完整顺序，失败则恢复原顺序。
document.querySelectorAll('[data-sortable]').forEach(list => {
  let dragging = null;
  let original = [];
  const items = container => [...container.children].filter(item => item.dataset.id);
  list.addEventListener('dragstart', event => {
    const item = event.target.closest && event.target.closest('[data-id]');
    if (!item || item.parentElement !== list) return;
    dragging = item;
    original = items(list);
    item.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', item.dataset.id);
  });
  list.addEventListener('dragover', event => {
    if (!dragging) return;
    event.preventDefault();
    const target = event.target.closest && event.target.closest('[data-id]');
    if (!target || target === dragging || target.parentElement !== list) return;
    const rect = target.getBoundingClientRect();
    const horizontal = getComputedStyle(list).flexDirection === 'row';
    const after = horizontal ? event.clientX > rect.left + rect.width / 2 : event.clientY > rect.top + rect.height / 2;
    list.insertBefore(dragging, after ? target.nextSibling : target);
  });
  list.addEventListener('drop', event => { if (dragging) event.preventDefault(); });
  list.addEventListener('dragend', async () => {
    if (!dragging) return;
    dragging.classList.remove('dragging');
    dragging = null;
    const before = original;
    const ids = items(list).map(item => item.dataset.id);
    if (ids.join() === before.map(item => item.dataset.id).join()) return;
    const body = new URLSearchParams();
    ids.forEach(id => body.append('ids', id));
    list.classList.add('sort-saving');
    try {
      const response = await fetch(list.dataset.sortable, {method: 'POST', body, credentials: 'same-origin',
        headers: {'X-CSRFToken': list.dataset.csrf}});
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || '排序保存失败，请刷新后重试');
      }
      // 同页的另一个列表（侧栏 / 管理表格）跟着调整。
      document.querySelectorAll('[data-sortable]').forEach(other => {
        if (other === list) return;
        const byId = new Map(items(other).map(item => [item.dataset.id, item]));
        ids.forEach(id => { if (byId.has(id)) other.appendChild(byId.get(id)); });
      });
    } catch (error) {
      before.forEach(item => list.appendChild(item));
      alert(error.message);
    } finally {
      list.classList.remove('sort-saving');
    }
  });
});
