/**
 * Enkiball Teacher — frontend application
 */

(function () {
  'use strict';

  var LEVEL_NAMES = {
    1: 'L1 — 精准记忆',
    2: 'L2 — 深层理解',
    3: 'L3 — 工程应用与分析',
    4: 'L4 — 毕业课题',
  };

  var COMMANDS = [
    { cmd: '/learn',   desc: '设定学习目标',   args: ' <主题>' },
    { cmd: '/yield',   desc: '认输求解答',     args: '' },
    { cmd: '/progress',desc: '查看学习进度',   args: '' },
    { cmd: '/assess',  desc: '请求等级评估',   args: '' },
    { cmd: '/code',    desc: '代码评审模式',   args: '' },
    { cmd: '/undo',    desc: '撤回上一条消息', args: '' },
    { cmd: '/reset',   desc: '重置对话',       args: '' },
    { cmd: '/help',    desc: '显示帮助',       args: '' },
  ];

  var globals = {
    sending: false,
    rawBuffer: '',
    streamingEl: null,
    thinkingDiv: null,
    thinkingToggle: null,
    thinkingVisible: false,
    projects: [],
    editMsgEl: null,
    editOriginal: '',
    cmdIdx: -1,
  };

  var $messages = document.getElementById('messages');
  var $input = document.getElementById('input');
  var $sendBtn = document.getElementById('send-btn');
  var $topicName = document.getElementById('topic-name');
  var $levelName = document.getElementById('level-name');
  var $modulesList = document.getElementById('modules-list');
  var $statAssess = document.getElementById('stat-assessments');
  var $statYields = document.getElementById('stat-yields');
  var $projectSelect = document.getElementById('project-select');
  var $newProjectBtn = document.getElementById('new-project-btn');

  // Autocomplete popup
  var $ac = document.createElement('div');
  $ac.id = 'cmd-autocomplete';
  $ac.style.display = 'none';
  document.body.appendChild($ac);

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  loadProjects().then(function () {
    loadState();
    loadMessages();
  });
  $input.addEventListener('keydown', onKeyDown);
  $input.addEventListener('input', onInput);
  $sendBtn.addEventListener('click', sendMessage);
  $projectSelect.addEventListener('change', onProjectSwitch);
  $newProjectBtn.addEventListener('click', createProject);
  setInterval(loadState, 5000);

  // ------------------------------------------------------------------
  // Autocomplete
  // ------------------------------------------------------------------

  function onInput() {
    autoResize();
    showAutocomplete();
  }

  function showAutocomplete() {
    var val = $input.value;
    if (!val.startsWith('/') || val.includes(' ')) {
      $ac.style.display = 'none';
      globals.cmdIdx = -1;
      return;
    }
    var matches = COMMANDS.filter(function (c) {
      return c.cmd.indexOf(val) === 0;
    });
    if (matches.length === 0) {
      $ac.style.display = 'none';
      globals.cmdIdx = -1;
      return;
    }
    var html = '';
    for (var i = 0; i < matches.length; i++) {
      var cls = i === globals.cmdIdx ? ' class="active"' : '';
      html += '<div' + cls + ' data-cmd="' + escAttr(matches[i].cmd) + '">' +
        '<strong>' + escHtml(matches[i].cmd) + '</strong>' +
        '<span>' + escHtml(matches[i].desc) + '</span></div>';
    }
    $ac.innerHTML = html;

    // Position below input
    var rect = $input.getBoundingClientRect();
    $ac.style.top = (rect.bottom + 4) + 'px';
    $ac.style.left = rect.left + 'px';
    $ac.style.minWidth = Math.max(rect.width, 220) + 'px';
    $ac.style.display = 'block';

    // Click handler
    var items = $ac.querySelectorAll('div');
    for (var j = 0; j < items.length; j++) {
      items[j].onclick = (function (cmd) {
        return function () { applyAutocomplete(cmd); };
      })(matches[j].cmd);
    }
  }

  function applyAutocomplete(cmd) {
    $input.value = cmd + ' ';
    $ac.style.display = 'none';
    globals.cmdIdx = -1;
    $input.focus();
    autoResize();
  }

  function hideAutocomplete() {
    $ac.style.display = 'none';
    globals.cmdIdx = -1;
  }

  // ------------------------------------------------------------------
  // Textarea auto-resize
  // ------------------------------------------------------------------

  function autoResize() {
    $input.style.height = 'auto';
    $input.style.height = Math.min($input.scrollHeight, 200) + 'px';
  }

  // ------------------------------------------------------------------
  // Projects
  // ------------------------------------------------------------------

  function loadProjects() {
    return fetch('/api/projects')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        globals.projects = data.projects || [];
        var html = '';
        for (var i = 0; i < globals.projects.length; i++) {
          var p = globals.projects[i];
          var label = p.name;
          if (p.topic) label += ' (' + p.topic + ')';
          html += '<option value="' + escAttr(p.id) + '">' + escHtml(label) + '</option>';
        }
        $projectSelect.innerHTML = html;
        return globals.projects;
      });
  }

  function onProjectSwitch() {
    var id = $projectSelect.value;
    if (!id) return;
    fetch('/api/projects/' + encodeURIComponent(id) + '/switch', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          $messages.innerHTML = '';
          appendMessage('system', '已切换到课题: ' + escHtml(data.project.name));
          loadState();
          loadMessages();
        }
      });
  }

  function createProject() {
    var name = prompt('输入新课题名称:');
    if (!name || !name.trim()) return;
    fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name.trim() }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          return fetch('/api/projects/' + encodeURIComponent(data.project.id) + '/switch',
            { method: 'POST' }).then(function (r) { return r.json(); });
        }
        throw new Error(data.error);
      })
      .then(function (data) {
        if (data.ok) {
          $messages.innerHTML = '';
          appendMessage('system', '新课题已创建: ' + escHtml(data.project.name));
          loadProjects();
          loadState();
        }
      });
  }

  // ------------------------------------------------------------------
  // Sidebar state
  // ------------------------------------------------------------------

  function loadState() {
    fetch('/api/state')
      .then(function (r) { return r.json(); })
      .then(renderSidebar)
      .catch(function () {});
  }

  function renderSidebar(data) {
    $topicName.textContent = data.topic || '未设定';
    $levelName.textContent = LEVEL_NAMES[data.level] || 'L1 — 精准记忆';
    var modules = data.modules || [];
    if (modules.length === 0) {
      $modulesList.innerHTML = '<p class="empty">尚未设置课程<br>输入 /learn &lt;主题&gt; 开始</p>';
    } else {
      var html = '';
      var curLevel = data.level;
      for (var i = 0; i < modules.length; i++) {
        var m = modules[i];
        var dotClass = 'pending';
        if (m.completed) dotClass = 'completed';
        else if (m.level === curLevel) dotClass = 'current';
        html +=
          '<div class="module-item">' +
          '<span class="module-dot ' + dotClass + '"></span>' +
          '<div class="module-info">' +
          '<div class="module-name">' + escHtml(m.name) + '</div>' +
          '<div class="module-level">L' + m.level + '</div>' +
          '</div></div>';
      }
      $modulesList.innerHTML = html;
    }
    $statAssess.textContent = (data.assessments || []).length;
    $statYields.textContent = (data.yields || []).length;
    if (data.capstone_completed) {
      $levelName.textContent = '已毕业';
      $levelName.style.color = 'var(--green)';
    }
  }

  function loadMessages() {
    fetch('/api/messages')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var msgs = data.messages || [];
        if (msgs.length > 0 && $messages.children.length <= 1) {
          $messages.innerHTML = '';
          var systemShown = false;
          for (var i = 0; i < msgs.length; i++) {
            var m = msgs[i];
            if (m.role === 'system') {
              if (!systemShown) { systemShown = true; }
              continue;
            }
            var type = m.role === 'user' ? 'student' : 'teacher';
            appendMessage(type, m.content || '');
          }
          if (systemShown) {
            appendMessage('system', '对话历史已恢复');
          }
        }
      })
      .catch(function () {});
  }

  // ------------------------------------------------------------------
  // Send message + SSE streaming
  // ------------------------------------------------------------------

  function sendMessage() {
    if (globals.sending) return;

    var message = $input.value;
    // Don't trim — preserve newlines for code
    if (!message.trim()) return;

    globals.sending = true;
    $input.value = '';
    autoResize();
    $input.disabled = true;
    $sendBtn.disabled = true;
    hideAutocomplete();

    // Client-side commands
    if (message.trim() === '/undo') {
      handleUndo();
      return;
    }
    if (message.trim() === '/reset') {
      handleReset();
      return;
    }

    appendMessage('student', message);

    globals.rawBuffer = '';
    globals.streamingEl = createStreamingMessage();

    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message }),
    })
      .then(function (res) {
        if (!res.ok) throw new Error('Server error: ' + res.status);
        return res.body.getReader();
      })
      .then(function (reader) {
        var decoder = new TextDecoder();
        var buffer = '';
        function pump() {
          return reader.read().then(function (_a) {
            var done = _a.done, value = _a.value;
            if (done) { _finalizeStream(); return; }
            buffer += decoder.decode(value, { stream: true });
            var parts = buffer.split('\n\n');
            buffer = parts.pop() || '';
            for (var i = 0; i < parts.length; i++) {
              var lines = parts[i].split('\n');
              for (var j = 0; j < lines.length; j++) {
                if (lines[j].startsWith('data: ')) {
                  try { handleEvent(JSON.parse(lines[j].slice(6))); }
                  catch (_e) {}
                }
              }
            }
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        _finalizeStream();
        appendMessage('error', 'Error: ' + escHtml(String(err.message || err)));
        _finishSend();
      });
  }

  function _finishSend() {
    globals.sending = false;
    $input.disabled = false;
    $sendBtn.disabled = false;
    $input.focus();
    loadState();
  }

  function handleEvent(event) {
    switch (event.type) {
      case 'token':    handleToken(event.data); break;
      case 'thinking': handleThinking(event.data); break;
      case 'tool':     handleTool(event.data); break;
      case 'done':     break;
      case 'error':    handleError(event.data); break;
    }
  }

  // ------------------------------------------------------------------
  // Streaming handlers
  // ------------------------------------------------------------------

  function handleToken(text) {
    globals.rawBuffer += text;
    var el = globals.streamingEl;
    if (!el) return;
    var content = el.querySelector('.msg-content');
    content.classList.remove('cursor-blink');
    content.innerHTML = renderMarkdown(globals.rawBuffer) +
      '<span class="cursor-blink"></span>';
    scrollToBottom();
  }

  function handleThinking(text) {
    if (!globals.thinkingDiv) {
      var el = globals.streamingEl;
      if (!el) return;
      var content = el.querySelector('.msg-content');
      var toggle = document.createElement('span');
      toggle.className = 'thinking-toggle';
      toggle.textContent = '[思考过程]';
      content.appendChild(toggle);
      var div = document.createElement('div');
      div.className = 'thinking-content';
      content.appendChild(div);
      globals.thinkingDiv = div;
      globals.thinkingToggle = toggle;
      toggle.addEventListener('click', function () {
        if (globals.thinkingVisible) {
          div.classList.remove('visible');
          toggle.textContent = '[思考过程]';
        } else {
          div.classList.add('visible');
          toggle.textContent = '[隐藏思考]';
        }
        globals.thinkingVisible = !globals.thinkingVisible;
      });
    }
    globals.thinkingDiv.textContent += text;
    scrollToBottom();
  }

  function handleTool(data) {
    appendMessage('tool', '🔧 ' + escHtml(String(data)));
  }

  function handleError(data) {
    appendMessage('error', 'Error: ' + escHtml(String(data)));
  }

  function _finalizeStream() {
    var el = globals.streamingEl;
    if (!el) return;
    var content = el.querySelector('.msg-content');
    if (content) {
      content.classList.remove('cursor-blink');
      content.innerHTML = renderMarkdown(globals.rawBuffer);
    }
    globals.streamingEl = null;
    globals.thinkingDiv = null;
    globals.thinkingToggle = null;
    globals.thinkingVisible = false;
    globals.rawBuffer = '';
    scrollToBottom();
    _finishSend();
  }

  // ------------------------------------------------------------------
  // Message builders
  // ------------------------------------------------------------------

  function createStreamingMessage() {
    var div = document.createElement('div');
    div.className = 'message msg-teacher';
    var content = document.createElement('div');
    content.className = 'msg-content cursor-blink';
    div.appendChild(content);
    $messages.appendChild(div);
    scrollToBottom();
    return div;
  }

  function appendMessage(type, text) {
    var div = document.createElement('div');
    div.className = 'message msg-' + type;
    var content = document.createElement('div');
    content.className = 'msg-content';
    if (type === 'teacher' || type === 'system') {
      content.innerHTML = renderMarkdown(text);
    } else {
      content.textContent = text;
    }
    div.appendChild(content);
    $messages.appendChild(div);

    // Student messages: click to edit
    if (type === 'student') {
      div.style.cursor = 'pointer';
      div.title = '点击编辑重发';
      div.addEventListener('click', function () {
        startEditMessage(div, text);
      });
    }
    scrollToBottom();
    return div;
  }

  function startEditMessage(el, original) {
    if (globals.sending) return;
    if (globals.editMsgEl) cancelEdit();
    globals.editMsgEl = el;
    globals.editOriginal = original;
    el.classList.add('editing');
    // Insert edit hint
    var hints = document.createElement('div');
    hints.className = 'edit-hints';
    hints.innerHTML = '<button class="eh-send">重发</button>' +
      '<button class="eh-cancel">取消</button>';
    el.appendChild(hints);
    hints.querySelector('.eh-send').onclick = function (e) { e.stopPropagation(); confirmEdit(); };
    hints.querySelector('.eh-cancel').onclick = function (e) { e.stopPropagation(); cancelEdit(); };
    // Fill input with original text
    $input.value = original;
    autoResize();
    $input.focus();
  }

  function confirmEdit() {
    if (!globals.editMsgEl) return;
    cancelEdit();
    // The edited message is already in the input, send it
    sendMessage();
  }

  function cancelEdit() {
    if (globals.editMsgEl) {
      globals.editMsgEl.classList.remove('editing');
      var h = globals.editMsgEl.querySelector('.edit-hints');
      if (h) h.remove();
      globals.editMsgEl = null;
      globals.editOriginal = '';
    }
  }

  function scrollToBottom() {
    $messages.scrollTop = $messages.scrollHeight;
  }

  // ------------------------------------------------------------------
  // Commands
  // ------------------------------------------------------------------

  function handleUndo() {
    cancelEdit();
    fetch('/api/revert', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok && data.reverted > 0) {
          // Remove last user message and its response from DOM
          removeLastTurn();
          loadState();
        } else {
          appendMessage('system', '没有可撤回的消息');
          _finishSend();
        }
      })
      .catch(function () {
        appendMessage('error', '撤回失败');
        _finishSend();
      });
  }

  function removeLastTurn() {
    var children = $messages.children;
    // Remove backwards until we hit a user message (inclusive),
    // then remove any assistant/tool/system messages that followed it
    var foundUser = false;
    var toRemove = [];
    for (var i = children.length - 1; i >= 0; i--) {
      var c = children[i];
      if (c.classList.contains('msg-student')) {
        toRemove.push(c);
        foundUser = true;
        break;
      }
      toRemove.push(c);
    }
    for (var j = 0; j < toRemove.length; j++) {
      toRemove[j].remove();
    }
    if (foundUser) {
      appendMessage('system', '已撤回上一条消息');
    }
    _finishSend();
  }

  function handleReset() {
    fetch('/api/reset', { method: 'POST' })
      .then(function () {
        $messages.innerHTML = '';
        appendMessage('system', '对话已重置。');
        loadState();
        _finishSend();
      });
  }

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function escAttr(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function onKeyDown(e) {
    // Autocomplete navigation
    if ($ac.style.display === 'block') {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        globals.cmdIdx++;
        var items = $ac.querySelectorAll('div');
        if (globals.cmdIdx >= items.length) globals.cmdIdx = 0;
        for (var i = 0; i < items.length; i++) {
          items[i].className = i === globals.cmdIdx ? 'active' : '';
        }
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        globals.cmdIdx--;
        var items2 = $ac.querySelectorAll('div');
        if (globals.cmdIdx < 0) globals.cmdIdx = items2.length - 1;
        for (var j = 0; j < items2.length; j++) {
          items2[j].className = j === globals.cmdIdx ? 'active' : '';
        }
        return;
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault();
        if (globals.cmdIdx >= 0) {
          var sel = $ac.querySelector('div.active');
          if (sel) {
            applyAutocomplete(sel.getAttribute('data-cmd'));
          }
        } else {
          var first = $ac.querySelector('div');
          if (first) applyAutocomplete(first.getAttribute('data-cmd'));
        }
        return;
      }
      if (e.key === 'Escape') {
        hideAutocomplete();
        return;
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    } else if (e.key === 'Enter' && e.shiftKey) {
      setTimeout(autoResize, 0);
    }
  }

  function renderMarkdown(text) {
    var html = escHtml(text);
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function (_, lang, code) {
      return '<pre><code class="language-' + escHtml(lang) + '">' +
        escHtml(code.trimEnd()) + '</code></pre>';
    });
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/^###### (.+)$/gm, '<h6>$1</h6>');
    html = html.replace(/^##### (.+)$/gm, '<h5>$1</h5>');
    html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
    html = html.replace(/^(---|\*\*\*|___)\s*$/gm, '<hr>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    html = html.replace(/^&gt; (.*)$/gm, '<blockquote>$1</blockquote>');
    html = html.replace(/((?:^\|.+\|\n?)+)/gm, function (match) {
      var lines = match.trim().split('\n');
      var si = -1;
      for (var i = 0; i < lines.length; i++) {
        if (/^[\|\s\-:]+$/.test(lines[i])) { si = i; break; }
      }
      if (si < 0) return match;
      var aligns = [];
      var sc = lines[si].split('|').filter(function(c){return c.trim();});
      for (var a = 0; a < sc.length; a++) {
        var s = sc[a].trim();
        if (s.startsWith(':') && s.endsWith(':')) aligns.push('center');
        else if (s.endsWith(':')) aligns.push('right');
        else aligns.push('left');
      }
      var tbl = '<table>';
      for (var r = 0; r < lines.length; r++) {
        if (r === si) continue;
        var cells = lines[r].split('|').filter(function(c){return c.trim();});
        var tag = (r === 0 || (si > 0 && r < si)) ? 'th' : 'td';
        tbl += '<tr>';
        for (var c = 0; c < cells.length; c++) {
          var al = aligns[c] ? ' style="text-align:' + aligns[c] + '"' : '';
          tbl += '<' + tag + al + '>' + cells[c].trim() + '</' + tag + '>';
        }
        tbl += '</tr>';
      }
      tbl += '</table>';
      return tbl;
    });
    html = html.replace(/((?:^[-*] .*(?:\n|$))+)/gm, function (match) {
      var items = match.trim().split('\n').map(function (line) {
        return '<li>' + line.replace(/^[-*] /, '') + '</li>';
      }).join('');
      return '<ul>' + items + '</ul>';
    });
    html = html.replace(/((?:^\d+\. .*(?:\n|$))+)/gm, function (match) {
      var items = match.trim().split('\n').map(function (line) {
        return '<li>' + line.replace(/^\d+\. /, '') + '</li>';
      }).join('');
      return '<ol>' + items + '</ol>';
    });
    var blocks = html.split('\n\n');
    html = blocks.map(function (b) {
      var trimmed = b.trim();
      if (!trimmed) return '';
      if (/^<(pre|ul|ol|blockquote|h[1-6]|div|table|hr)/.test(trimmed)) return trimmed;
      return '<p>' + trimmed.replace(/\n/g, '<br>') + '</p>';
    }).join('');
    return html;
  }
})();
