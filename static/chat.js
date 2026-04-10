/* chat.js — reusable WhatsApp-style chat widget.
 *
 * Usage:
 *   initChat({
 *     windowId:      'chatWindow',    // scrollable message area
 *     inputId:       'chatInput',     // text input
 *     sendBtnId:     'chatSend',      // send button
 *     modelSelectId: 'nlModel',       // model <select> (optional)
 *     scopeSelectId: 'nlScope',       // scope <select> (optional)
 *     defaultScope:  'all',           // fallback scope
 *   });
 */

function initChat(cfg) {
  var win     = document.getElementById(cfg.windowId);
  var input   = document.getElementById(cfg.inputId);
  var btn     = document.getElementById(cfg.sendBtnId);
  var history = [];  // [{role:'user'|'assistant', content:'...'}]

  function modelVal() {
    var el = cfg.modelSelectId && document.getElementById(cfg.modelSelectId);
    return el ? el.value : 'claude-haiku-4-5-20251001';
  }

  function scopeVal() {
    var el = cfg.scopeSelectId && document.getElementById(cfg.scopeSelectId);
    return el ? el.value : (cfg.defaultScope || 'all');
  }

  // ---- Markdown → HTML (basic subset) --------------------------------
  function renderMd(text) {
    // Escape HTML first
    var s = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Headings → bold line
    s = s.replace(/^#{1,3} (.+)$/gm, '<strong>$1</strong>');

    // Bold
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

    // Italic
    s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');

    // Bullet lists — collect consecutive li lines into a <ul>
    s = s.replace(/((?:^[ \t]*[-*] .+\n?)+)/gm, function(block) {
      var items = block.trim().split('\n').map(function(line) {
        return '<li>' + line.replace(/^[ \t]*[-*] /, '') + '</li>';
      });
      return '<ul>' + items.join('') + '</ul>';
    });

    // Numbered lists
    s = s.replace(/((?:^\d+\. .+\n?)+)/gm, function(block) {
      var items = block.trim().split('\n').map(function(line) {
        return '<li>' + line.replace(/^\d+\. /, '') + '</li>';
      });
      return '<ol>' + items.join('') + '</ol>';
    });

    // Double newline → paragraph break
    s = s.replace(/\n\n+/g, '</p><p>');

    // Single newline → <br>
    s = s.replace(/\n/g, '<br>');

    return '<p>' + s + '</p>';
  }

  // ---- DOM helpers ---------------------------------------------------
  function scrollBottom() {
    win.scrollTop = win.scrollHeight;
  }

  function removeEmpty() {
    var e = win.querySelector('.chat-empty');
    if (e) e.remove();
  }

  function appendUserMsg(text) {
    removeEmpty();
    var div = document.createElement('div');
    div.className = 'chat-row user';
    div.innerHTML = '<div class="chat-bubble user">' +
      text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') +
      '</div>';
    win.appendChild(div);
    scrollBottom();
  }

  function appendThinking() {
    removeEmpty();
    var div = document.createElement('div');
    div.className = 'chat-row ai';
    div.id = 'thinking-indicator';
    div.innerHTML =
      '<div class="chat-avatar">AI</div>' +
      '<div class="chat-bubble ai thinking">' +
        '<span class="dot-pulse"></span>' +
        '<span class="dot-pulse"></span>' +
        '<span class="dot-pulse"></span>' +
      '</div>';
    win.appendChild(div);
    scrollBottom();
    return div;
  }

  function removeThinking() {
    var el = document.getElementById('thinking-indicator');
    if (el) el.remove();
  }

  function appendAiMsg(text, meta) {
    var div = document.createElement('div');
    div.className = 'chat-row ai';
    var metaHtml = meta
      ? '<div class="chat-meta">' + meta + '</div>'
      : '';
    div.innerHTML =
      '<div class="chat-avatar">AI</div>' +
      '<div class="chat-bubble-wrap">' +
        '<div class="chat-bubble ai">' + renderMd(text) + '</div>' +
        metaHtml +
      '</div>';
    win.appendChild(div);
    scrollBottom();
  }

  function appendErrorMsg(text) {
    var div = document.createElement('div');
    div.className = 'chat-row ai';
    div.innerHTML =
      '<div class="chat-avatar">AI</div>' +
      '<div class="chat-bubble ai error">' +
        text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') +
      '</div>';
    win.appendChild(div);
    scrollBottom();
  }

  // ---- Send ----------------------------------------------------------
  async function send() {
    var q = input.value.trim();
    if (!q) return;

    input.value = '';
    input.focus();
    btn.disabled = true;

    appendUserMsg(q);
    appendThinking();

    history.push({ role: 'user', content: q });

    try {
      var res = await fetch('/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: q,
          model: modelVal(),
          scope: scopeVal(),
          history: history.slice(0, -1),  // exclude current message — backend appends it
        }),
      });
      var data = await res.json();
      removeThinking();

      if (data.error) {
        history.pop();  // remove failed message from history
        appendErrorMsg('Error: ' + data.error);
      } else {
        var answer = data.answer || 'No answer returned.';
        history.push({ role: 'assistant', content: answer });
        var modelLabel = (data.model || '').includes('haiku') ? 'Haiku' : 'Sonnet';
        var scopeLabel = data.scope || '';
        var meta = modelLabel + (scopeLabel ? ' · ' + scopeLabel : '');
        appendAiMsg(answer, meta);
      }
    } catch (e) {
      history.pop();  // remove failed message from history
      removeThinking();
      appendErrorMsg('Network error: ' + e.message);
    }

    btn.disabled = false;
  }

  // ---- Wire up events ------------------------------------------------
  btn.addEventListener('click', send);
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
}
