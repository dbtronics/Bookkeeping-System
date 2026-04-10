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

  // Session cost tracking
  var session = { inputTokens: 0, outputTokens: 0, costUsd: 0, costCad: 0 };

  // Find the card-header that contains this chat's controls and inject a cost display
  var chatCard   = win.closest('.card');
  var cardHeader = chatCard ? chatCard.querySelector('.card-header') : null;
  var costEl = null;
  if (cardHeader) {
    costEl = document.createElement('span');
    costEl.className = 'session-cost';
    costEl.title = 'Total token cost for this chat session';
    costEl.textContent = 'Session: $0.00 CAD';
    cardHeader.appendChild(costEl);
  }

  function updateSessionCost() {
    if (!costEl) return;
    costEl.innerHTML =
      '<span class="cost-detail">' +
        session.inputTokens.toLocaleString() + ' in / ' +
        session.outputTokens.toLocaleString() + ' out' +
      '</span>' +
      ' &nbsp;Session: <strong>$' + session.costCad.toFixed(4) + ' CAD</strong>' +
      ' <span class="cost-usd">($' + session.costUsd.toFixed(4) + ' USD)</span>';
  }

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

  function appendAiMsg(text, meta, cost) {
    var div = document.createElement('div');
    div.className = 'chat-row ai';
    var metaHtml = '';
    if (meta || cost) {
      metaHtml = '<div class="chat-meta">';
      if (cost) metaHtml += '<span class="chat-cost">' + cost + '</span>';
      if (meta) metaHtml += '<span>' + meta + '</span>';
      metaHtml += '</div>';
    }
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

        // Model + scope meta
        var modelLabel = (data.model || '').includes('haiku') ? 'Haiku' : 'Sonnet';
        var scopeLabel = data.scope || '';
        var meta = modelLabel + (scopeLabel ? ' · ' + scopeLabel : '');

        // Per-message cost
        var costStr = '';
        if (data.tokens && data.cost_cad !== undefined) {
          var t = data.tokens;
          session.inputTokens  += t.input  || 0;
          session.outputTokens += t.output || 0;
          session.costUsd      += data.cost_usd || 0;
          session.costCad      += data.cost_cad || 0;
          updateSessionCost();
          costStr =
            t.input.toLocaleString() + ' in / ' +
            t.output.toLocaleString() + ' out · ' +
            '$' + data.cost_cad.toFixed(4) + ' CAD';
        }

        appendAiMsg(answer, meta, costStr);
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
