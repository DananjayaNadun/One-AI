const fs = require('fs');
const { JSDOM } = require('jsdom');

let html = fs.readFileSync(require('path').join(__dirname,'..','templates','index.html'), 'utf8');
// Strip CDN <script src> tags; we stub those libraries instead.
html = html.replace(/<script src="https:[^"]*"><\/script>/g, '');

const errors = [];
const logs = [];

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost/',
  beforeParse(win) {
    win.marked = { parse: (s) => String(s) };
    win.__sanitizeCalls = 0; win.DOMPurify = { sanitize: (s) => { win.__sanitizeCalls++; return String(s).replace(/<script[\s\S]*?<\/script>/gi,'').replace(/ on\w+="[^"]*"/gi,''); } };
    win.Prism = { highlightAllUnder: () => {} };
    win.matchMedia = (q) => ({ matches: q.includes('pointer: fine'), addListener(){}, removeListener(){} });
    win.speechSynthesis = { speaking: false, speak(){}, cancel(){} };
    const store = {};
    Object.defineProperty(win, 'localStorage', { value: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    }});
    win.__store = store;
    win.AbortController = class { constructor(){ this.signal = { aborted:false }; }
      abort(){ this.signal.aborted = true; if (this.signal.onabort) this.signal.onabort(); } };
    win.URL.createObjectURL = () => 'blob:fake';
    win.URL.revokeObjectURL = () => {};
    win.HTMLCanvasElement.prototype.getContext = () => ({
      clearRect(){}, save(){}, restore(){}, beginPath(){}, arc(){}, fill(){},
      set globalAlpha(v){}, set fillStyle(v){}
    });
    win.requestAnimationFrame = () => 0;

    win.FormData = class { constructor(){ this._p=[]; } append(k,v,n){ this._p.push([k,n||v]); } };
    win.__uploads = [];
    const routes = {
      'GET /api/chats': () => [
        { id: 1, title: '<img src=x onerror=alert(1)>', updated_at: new Date().toISOString().slice(0,19).replace('T',' ') },
        { id: 2, title: 'Older chat', updated_at: '2020-01-05 10:00:00' },
      ],
      'GET /api/chats/1': () => [
        { id: 11, role: 'user', content: '<script>window.PWNED=true<\/script>hello', attachments: [] },
        { id: 12, role: 'assistant', content: '# Heading\n```py\nprint(1)\n```', attachments: [] },
      ],
    };
    win.fetch = async (url, opts = {}) => {
      const key = `${opts.method || 'GET'} ${url.split('?')[0]}`;
      const handler = routes[key];
      if (key === 'POST /api/upload') {
        win.__uploads.push(opts.body._p.map(p => p[1]));
        return { ok: true, json: async () => ({
          attachments: [
            { id: 'a'.repeat(32), name: 'shot.png', kind: 'image', size: 4096,
              thumbnail: 'data:image/jpeg;base64,AAA', truncated: false },
            { id: 'b'.repeat(32), name: 'spec.pdf', kind: 'document', size: 20480,
              thumbnail: '', pages: 3, truncated: false },
          ],
          rejected: [{ name: 'bad.exe', error: '.exe files are not accepted.' }],
        }) };
      }
      if (key === 'POST /api/chat') {
        win.__lastChatBody = JSON.parse(opts.body);
        return { ok: true, json: async () => ({ master_answer: 'Answer **bold**', chat_id: 1, degraded: false, nodes: ['logic'] }) };
      }
      if (!handler) return { ok: false, status: 500, json: async () => ({ error: 'boom' }) };
      return { ok: true, json: async () => handler() };
    };
    win.addEventListener('error', (e) => errors.push('window error: ' + e.message));
    win.console.error = (...a) => errors.push('console.error: ' + a.join(' '));
  },
});

const win = dom.window, doc = win.document;

setTimeout(async () => {
  const results = [];
  const check = (n, c, d='') => results.push(`${c ? 'PASS' : 'FAIL'}  ${n}${!c && d ? '  -> '+d : ''}`);

  check('page boots with no JS errors', errors.length === 0, errors.join(' | '));

  // Sidebar rendered a malicious title safely
  const btn = doc.querySelector('.sidebar-btn');
  check('sidebar rendered', !!btn, 'no sidebar button');
  check('malicious chat title not parsed as HTML',
        btn && btn.querySelector('img') === null && btn.textContent.includes('onerror'));

  // Send a message containing a script tag
  const input = doc.getElementById('user-input');
  input.value = '<img src=x onerror="window.PWNED=1">';
  doc.getElementById('submit-btn').click();
  await new Promise(r => setTimeout(r, 60));

  const bubble = doc.querySelector('.user-bubble');
  check('user message rendered', !!bubble);
  check('user HTML not executed', bubble && bubble.querySelector('img') === null && win.PWNED === undefined,
        'img injected: ' + (bubble && !!bubble.querySelector('img')));
  check('assistant reply rendered', doc.querySelector('.ai-content') && doc.querySelector('.ai-content').textContent.includes('Answer'),
        doc.querySelector('.ai-content') ? doc.querySelector('.ai-content').innerHTML.slice(0,80) : 'none');
  check('toolbar buttons attached', doc.querySelectorAll('.ai-block .action-btn-icon').length === 3,
        String(doc.querySelectorAll('.ai-block .action-btn-icon').length));
  check('answer carries a consensus strip', !!doc.querySelector('.ai-block .consensus'));
  check('consensus reports node count',
        /1 of 3 nodes/.test(doc.querySelector('.consensus-label').textContent),
        doc.querySelector('.consensus-label').textContent);
  check('partial panel is flagged', !!doc.querySelector('.consensus-flag'),
        'no flag for 1/3 nodes');
  check('no emoji left in the transcript',
        !/[\u{1F300}-\u{1FAFF}\u{2700}-\u{27BF}\u{2600}-\u{26FF}]/u.test(doc.getElementById('chat-box').textContent),
        doc.getElementById('chat-box').textContent.slice(0, 60));
  check('hero removed after send', !doc.getElementById('hero-container'));

  // Load history
  await win.loadChat ? null : null;
  doc.querySelector('.sidebar-btn').click();
  await new Promise(r => setTimeout(r, 60));
  check('history loaded', doc.querySelectorAll('.message-block').length === 2,
        String(doc.querySelectorAll('.message-block').length));
  check('history user content not executed', win.PWNED === undefined);

  // Error path: server 500
  win.fetch = async () => ({ ok: false, status: 503, json: async () => ({ error: 'Model service unavailable.' }) });
  input.value = 'test error';
  doc.getElementById('submit-btn').click();
  await new Promise(r => setTimeout(r, 60));
  check('error surfaced in message block', !!doc.querySelector('.error-text'),
        'no .error-text element');
  check('input re-enabled after error', doc.getElementById('submit-btn').disabled === false);

  // ---------- attachments ----------
  doc.getElementById('new-chat-btn').click();
  await new Promise(r => setTimeout(r, 30));
  win.fetch = (function(orig){ return async (url, o={}) => {
    if (url === '/api/upload') {
      win.__uploads.push('called');
      return { ok: true, json: async () => ({
        attachments: [
          { id: 'a'.repeat(32), name: 'shot.png', kind: 'image', size: 4096, thumbnail: 'data:image/jpeg;base64,AAA' },
          { id: 'b'.repeat(32), name: 'spec.pdf', kind: 'document', size: 20480, pages: 3 },
        ],
        rejected: [{ name: 'bad.exe', error: '.exe files are not accepted.' }],
      }) };
    }
    if (url === '/api/chat') {
      win.__lastChatBody = JSON.parse(o.body);
      return { ok: true, json: async () => ({ master_answer: 'I can see the diagram.', chat_id: 7, degraded: false, nodes: ['logic'] }) };
    }
    return { ok: true, json: async () => [] };
  }; })(win.fetch);

  const fakeFile = (name, type, size) => ({ name, type: type||'', size: size||100 });
  await win.uploadFiles([fakeFile('shot.png','image/png'), fakeFile('spec.pdf','application/pdf'), fakeFile('bad.exe','')]);
  await new Promise(r => setTimeout(r, 40));

  const cards = doc.querySelectorAll('#attach-tray .att-card');
  check('attachment tray shows accepted files', cards.length === 2, String(cards.length));
  check('image card renders a thumbnail', !!doc.querySelector('#attach-tray img.att-thumb'));
  check('document card shows page count',
        Array.from(doc.querySelectorAll('.att-sub')).some(e => e.textContent.includes('3 pages')),
        Array.from(doc.querySelectorAll('.att-sub')).map(e=>e.textContent).join('|'));
  check('rejected file surfaced to user', doc.getElementById('toast').textContent.includes('.exe'),
        doc.getElementById('toast').textContent);

  // remove one
  doc.querySelector('.att-remove').click();
  check('remove button drops one attachment',
        doc.querySelectorAll('#attach-tray .att-card').length === 1);

  // re-add and send
  await win.uploadFiles([fakeFile('shot.png','image/png')]);
  await new Promise(r => setTimeout(r, 40));
  input.value = 'what does this diagram show?';
  doc.getElementById('submit-btn').click();
  await new Promise(r => setTimeout(r, 60));

  check('attachment ids sent with message',
        win.__lastChatBody && win.__lastChatBody.attachment_ids.length > 0,
        JSON.stringify(win.__lastChatBody));
  check('tray cleared after send', doc.querySelectorAll('#attach-tray .att-card').length === 0);
  check('user message shows attachment strip', !!doc.querySelector('.user-block .msg-atts'));
  check('image thumbnail rendered in transcript', !!doc.querySelector('.msg-att img'));

  // send with ONLY a file, no text
  await win.uploadFiles([fakeFile('spec.pdf','application/pdf')]);
  await new Promise(r => setTimeout(r, 40));
  input.value = '';
  doc.getElementById('submit-btn').click();
  await new Promise(r => setTimeout(r, 60));
  check('file-only message is sent', win.__lastChatBody.message.toLowerCase().includes('review'),
        String(win.__lastChatBody.message));

  // lightbox
  const thumb = doc.querySelector('.msg-att img');
  if (thumb) thumb.click();
  check('lightbox opens on thumbnail click', doc.getElementById('lightbox').classList.contains('show'));

  // drag and drop veil
  const dragEvt = new win.Event('dragenter');
  dragEvt.dataTransfer = { types: ['Files'] };
  win.dispatchEvent(dragEvt);
  check('drop veil appears on file drag', doc.getElementById('drop-veil').classList.contains('show'));

  // New chat resets
  doc.getElementById('new-chat-btn').click();
  await new Promise(r => setTimeout(r, 30));
  check('new chat restores hero', !!doc.getElementById('hero-container'));
  check('DOMPurify invoked on assistant output', win.__sanitizeCalls > 0, 'sanitize never called');

  // ---------- theme ----------
  const startTheme = doc.documentElement.getAttribute('data-theme');
  doc.getElementById('theme-btn').click();
  check('theme toggles', doc.documentElement.getAttribute('data-theme') !== startTheme,
        String(doc.documentElement.getAttribute('data-theme')));
  check('theme persisted to localStorage', !!win.__store['oneai-theme'], JSON.stringify(win.__store));

  // ---------- suggestions ----------
  check('empty state offers suggestions', doc.querySelectorAll('.suggestion').length === 4,
        String(doc.querySelectorAll('.suggestion').length));
  doc.querySelector('.suggestion').click();
  check('suggestion fills the composer', input.value.length > 0, input.value);
  input.value = '';

  // ---------- sidebar date grouping ----------
  win.fetch = async (url, o={}) => {
    if (url === '/api/chats') return { ok: true, json: async () => [
      { id: 1, title: 'Recent one', updated_at: new Date().toISOString().slice(0,19).replace('T',' ') },
      { id: 2, title: 'Ancient one', updated_at: '2020-01-05 10:00:00' },
    ]};
    if (url.startsWith('/api/chats/1/regenerate') || url === '/api/chats/1/regenerate') {
      win.__regenBody = JSON.parse(o.body);
      return { ok: true, json: async () => ({ master_answer: 'Regenerated answer', message_id: 99, nodes: ['logic'], chat_id: 1 }) };
    }
    if (url === '/api/chats/1/edit') {
      win.__editBody = JSON.parse(o.body);
      return { ok: true, json: async () => ({ master_answer: 'Edited answer', message_id: 101, user_message_id: 100, nodes: ['logic'], chat_id: 1 }) };
    }
    if (url === '/api/chats/1') return { ok: true, json: async () => [
      { id: 11, role: 'user', content: 'original question', attachments: [] },
      { id: 12, role: 'assistant', content: 'original answer', attachments: [] },
    ]};
    if (url === '/api/chat') { win.__lastChatBody = JSON.parse(o.body);
      return { ok: true, json: async () => ({ master_answer: 'ok', chat_id: 1, message_id: 5, user_message_id: 4, nodes: [] }) }; }
    return { ok: true, json: async () => [] };
  };
  await win.loadSidebar();
  await new Promise(r => setTimeout(r, 40));
  const groups = Array.from(doc.querySelectorAll('.date-group')).map(e => e.textContent);
  check('sidebar groups chats by date', groups.includes('Today') && groups.includes('Older'),
        groups.join('|'));

  // ---------- load a chat, then regenerate ----------
  doc.querySelector('.sidebar-btn').click();
  await new Promise(r => setTimeout(r, 60));
  const aiBlock = doc.querySelector('.ai-block');
  check('loaded messages carry ids', aiBlock && aiBlock.dataset.messageId === '12',
        aiBlock ? String(aiBlock.dataset.messageId) : 'none');

  const regenBtn = Array.from(doc.querySelectorAll('.ai-block .action-btn-icon'))
                        .find(b => b.getAttribute('aria-label') === 'Regenerate');
  regenBtn.click();
  await new Promise(r => setTimeout(r, 60));
  check('regenerate hits the dedicated endpoint', !!win.__regenBody, JSON.stringify(win.__regenBody));
  check('regenerate sends the message id', win.__regenBody && win.__regenBody.message_id === 12,
        JSON.stringify(win.__regenBody));
  check('regenerated answer replaces in place',
        doc.querySelector('.ai-content').textContent.includes('Regenerated'),
        doc.querySelector('.ai-content').textContent.slice(0,50));
  check('only one ai block after regenerate', doc.querySelectorAll('.ai-block').length === 1,
        String(doc.querySelectorAll('.ai-block').length));

  doc.getElementById('lightbox').classList.remove('show');

  // ---------- inline edit ----------
  const editBtn = Array.from(doc.querySelectorAll('.user-block .action-btn-icon'))
                       .find(b => b.getAttribute('aria-label') === 'Edit');
  editBtn.click();
  const editArea = doc.querySelector('.editing-box textarea');
  check('edit opens an inline editor', !!editArea);
  check('editor prefilled with the message', editArea && editArea.value === 'original question',
        editArea ? editArea.value : 'none');

  editArea.value = 'revised question';
  Array.from(doc.querySelectorAll('.editing-box .btn')).find(b => b.textContent.includes('Save')).click();
  await new Promise(r => setTimeout(r, 60));
  check('edit posts to the edit endpoint', !!win.__editBody, JSON.stringify(win.__editBody));
  check('edit sends new text and id',
        win.__editBody && win.__editBody.message === 'revised question' && win.__editBody.message_id === 11,
        JSON.stringify(win.__editBody));
  check('bubble shows the edited text',
        doc.querySelector('.user-bubble').textContent === 'revised question',
        doc.querySelector('.user-bubble').textContent);
  check('user block id updated after edit',
        doc.querySelector('.user-block').dataset.messageId === '100',
        String(doc.querySelector('.user-block').dataset.messageId));
  check('editor closed after saving', !doc.querySelector('.editing-box'));

  // ---------- escape cancels an edit ----------
  Array.from(doc.querySelectorAll('.user-block .action-btn-icon'))
       .find(b => b.getAttribute('aria-label') === 'Edit').click();
  check('editor reopened', !!doc.querySelector('.editing-box'));
  const esc = new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true });
  doc.dispatchEvent(esc);
  check('escape cancels the edit', !doc.querySelector('.editing-box'));
  check('bubble restored after cancel',
        doc.querySelector('.user-bubble').style.display !== 'none');

  // ---------- delete uses a real dialog, not window.confirm ----------
  let confirmCalled = false;
  win.confirm = () => { confirmCalled = true; return true; };
  await win.loadSidebar();
  await new Promise(r => setTimeout(r, 30));
  doc.querySelector('.dots-btn').click();
  Array.from(doc.querySelectorAll('.action-item')).find(b => b.textContent.includes('Delete')).click();
  await new Promise(r => setTimeout(r, 30));
  check('delete opens the in-page dialog',
        doc.getElementById('dialog-backdrop').classList.contains('show'));
  check('window.confirm not used', confirmCalled === false);
  Array.from(doc.querySelectorAll('#dialog-actions .btn')).find(b => b.textContent === 'Cancel').click();
  await new Promise(r => setTimeout(r, 20));
  check('dialog closes on cancel',
        !doc.getElementById('dialog-backdrop').classList.contains('show'));

  // ---------- inline rename ----------
  doc.querySelector('.dots-btn').click();
  Array.from(doc.querySelectorAll('.action-item')).find(b => b.textContent.includes('Rename')).click();
  check('rename edits in place', !!doc.querySelector('.rename-input'));
  check('window.prompt not used for rename', !!doc.querySelector('.rename-input'));

  // ---------- shortcuts ----------
  // '?' must be ignored while typing, so move focus out of the rename field first.
  if (doc.activeElement && doc.activeElement.blur) doc.activeElement.blur();
  doc.body.focus();
  await new Promise(r => setTimeout(r, 20));
  doc.dispatchEvent(new win.KeyboardEvent('keydown', { key: '?', bubbles: true }));
  check('? opens the shortcuts dialog',
        doc.getElementById('dialog-backdrop').classList.contains('show'));
  doc.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await new Promise(r => setTimeout(r, 20));
  check('escape closes the dialog',
        !doc.getElementById('dialog-backdrop').classList.contains('show'));

  const themeBefore = doc.documentElement.getAttribute('data-theme');
  doc.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'j', ctrlKey: true, bubbles: true }));
  check('ctrl+J toggles theme',
        doc.documentElement.getAttribute('data-theme') !== themeBefore);

  // ---------- stop button ----------
  let resolveChat;
  win.fetch = async (url, opts = {}) => {
    if (url === '/api/chat') return new Promise((res, rej) => {
      resolveChat = res;
      if (opts.signal) opts.signal.onabort = () => {
        const e = new Error('aborted'); e.name = 'AbortError'; rej(e);
      };
    });
    return { ok: true, json: async () => [] };
  };
  input.value = 'long running';
  doc.getElementById('submit-btn').click();
  await new Promise(r => setTimeout(r, 30));
  check('stop button appears while generating',
        doc.getElementById('stop-btn').classList.contains('show'));
  check('progress timer rendered', !!doc.querySelector('.consensus-timer'));
  check('progress uses the consensus strip', !!doc.querySelector('.ai-block .consensus'));
  check('pips show a pending state', !!doc.querySelector('.node-pip.pending'));
  check('submit disabled while generating', doc.getElementById('submit-btn').disabled === true);

  // ---------- modes ----------
  // The stop-button test left a request in flight; clear it before sending again.
  win.stopGenerating();
  await new Promise(r => setTimeout(r, 30));
  check('stop clears the busy state', doc.getElementById('submit-btn').disabled === false);
  check('mode switch rendered', doc.querySelectorAll('.mode-opt').length === 3,
        String(doc.querySelectorAll('.mode-opt').length));
  check('panel is the default mode',
        doc.querySelector('.mode-opt[data-mode="panel"]').classList.contains('on'));

  doc.querySelector('.mode-opt[data-mode="code"]').click();
  check('clicking code switches mode',
        doc.querySelector('.mode-opt[data-mode="code"]').classList.contains('on')
        && !doc.querySelector('.mode-opt[data-mode="panel"]').classList.contains('on'));
  check('mode persisted', win.__store['oneai-mode'] === 'code', String(win.__store['oneai-mode']));

  win.fetch = async (u, o = {}) => {
    if (u === '/api/chat') { win.__modeBody = JSON.parse(o.body);
      return { ok: true, json: async () => ({ master_answer: 'x', chat_id: 1, message_id: 9,
        user_message_id: 8, nodes: [], mode: 'code', model: 'qwen/qwen3-coder:free', seconds: 12 }) }; }
    return { ok: true, json: async () => [] };
  };
  input.value = 'write a function';
  doc.getElementById('submit-btn').click();
  await new Promise(r => setTimeout(r, 80));
  check('mode sent with the message', win.__modeBody && win.__modeBody.mode === 'code',
        JSON.stringify(win.__modeBody));
  const strip = Array.from(doc.querySelectorAll('.consensus')).pop();
  check('single-model strip says so', /single model/i.test(strip.textContent), strip.textContent);
  check('single-model strip shows one pip',
        strip.querySelectorAll('.node-pip').length === 1,
        String(strip.querySelectorAll('.node-pip').length));
  check('strip names the model', /qwen3-coder/.test(strip.textContent), strip.textContent);
  check('strip omits :free suffix', !/:free/.test(strip.textContent), strip.textContent);
  // 'openrouter/free' would otherwise render as a meaningless bare "free".
  const auto = win.renderMarkdown ? null : null;
  const probe = doc.createElement('div');
  probe.className = 'message-block ai-block';
  probe.append(Object.assign(doc.createElement('div'), { className: 'ai-content' }));
  win.addConsensusStrip(probe, ['a','b','c'], 30, false, { mode: 'panel', model: 'openrouter/free' });
  check('auto-router labelled clearly',
        /auto/.test(probe.querySelector('.consensus').textContent)
        && !/\bfree\b/.test(probe.querySelector('.consensus').textContent),
        probe.querySelector('.consensus').textContent);

  // ---------- code block chrome ----------
  const realParse = win.marked.parse;
  win.marked.parse = () => '<pre><code class="language-python">' +
      '# db.py' + String.fromCharCode(10) + 'import os' + String.fromCharCode(10) + '</code></pre>';
  const rendered = win.renderMarkdown('x');
  win.marked.parse = realParse;
  const hdr = rendered.querySelector('.code-header');
  check('code block gets a header', !!hdr);
  check('filename lifted into the header',
        hdr && hdr.querySelector('.code-file') && hdr.querySelector('.code-file').textContent === 'db.py',
        hdr ? hdr.textContent : 'none');
  check('code block has copy and download',
        hdr && hdr.querySelectorAll('.copy-code-btn').length === 2,
        hdr ? String(hdr.querySelectorAll('.copy-code-btn').length) : 'none');

  // Every icon name must resolve. A missing name silently falls back to the
  // file glyph, which looks intentional and is easy to ship by accident.
  const srcText = fs.readFileSync(require('path').join(__dirname,'..','templates','index.html'),'utf8');
  const paths = JSON.parse(srcText.match(/const ICON_PATHS = (\{.*?\});/s)[1]);
  const names = [...new Set([
    ...[...srcText.matchAll(/icon\('([a-z_]+)'/g)].map(m => m[1]),
    ...[...srcText.matchAll(/data-icon="([a-z_]+)"/g)].map(m => m[1]),
  ])];
  const unknown = names.filter(n => !(n in paths));
  check('every icon name resolves', unknown.length === 0, unknown.join(','));
  check('no icon renders as a blank path',
        !doc.querySelector('svg.ic path:not([d])'));

  // ---------- free-tier budget meter ----------
  check('budget meter rendered', !!doc.getElementById('budget-text'));
  const readUsed = () => Number(doc.getElementById('budget-text').textContent.split('/')[0]);
  const readLimit = () => Number(doc.getElementById('budget-text').textContent.split('/')[1]);
  const before = readUsed();
  win.spendBudget(4);
  check('panel spend counts four requests', readUsed() === before + 4, String(readUsed()));
  check('budget limit comes from the server', readLimit() === 200, String(readLimit()));
  check('budget persisted for the day', !!win.__store['oneai-budget'],
        JSON.stringify(win.__store));
  check('budget bar reflects usage',
        parseFloat(doc.getElementById('budget-fill').style.width) > 0,
        doc.getElementById('budget-fill').style.width);

  win.spendBudget(Math.round(readLimit() * 0.85) - readUsed());
  check('budget warns near the cap',
        doc.getElementById('budget-meter').classList.contains('warn'));
  check('element id does not shadow state',
        doc.getElementById('budget-meter') !== null && !doc.getElementById('budget'));

  check('sparkle canvas removed', !doc.getElementById('sparkle-canvas'));
  check('no magnetic-target elements', doc.querySelectorAll('.magnetic-target').length === 0,
        String(doc.querySelectorAll('.magnetic-target').length));
  check('no emoji anywhere in the shell',
        !/[\u{1F300}-\u{1FAFF}]/u.test(doc.body.textContent),
        (doc.body.textContent.match(/[\u{1F300}-\u{1FAFF}]/u) || [''])[0]);
  check('mono metadata font declared',
        /IBM Plex Mono/.test(doc.documentElement.innerHTML));
  check('export enabled while a chat is open',
        doc.getElementById('export-btn').disabled === false);
  doc.getElementById('new-chat-btn').click();
  await new Promise(r => setTimeout(r, 20));
  check('export disabled on a new empty chat',
        doc.getElementById('export-btn').disabled === true);

  console.log(results.join('\n'));
  const failed = results.filter(r => r.startsWith('FAIL')).length;
  console.log(`\n${results.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}, 120);
