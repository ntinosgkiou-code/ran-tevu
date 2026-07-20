/* ═══════════════════════════════════════════
   CHAT ENGINE — AI Γραμματεία
═══════════════════════════════════════════ */

let history = [];
let isScrolledUp = false;

// ── Init ─────────────────────────────────────────────────────
fetch('/api/business')
  .then(r => r.json())
  .then(b => {
    document.getElementById('businessName').textContent    = b.name;
    document.getElementById('sidebarName').textContent     = b.name;
    document.getElementById('sidebarTagline').textContent  = b.tagline || 'AI Γραμματεία';
    document.title = b.name + ' — AI Γραμματεία';

    const chips = document.getElementById('serviceChips');
    if (chips && b.services) {
      chips.innerHTML = b.services.slice(0, 8).map(s =>
        `<button class="sb-chip" onclick="quickAsk('Θέλω ραντεβού για ${s}')">${s}</button>`
      ).join('');
    }
  })
  .catch(() => {});

// ── DOM refs ─────────────────────────────────────────────────
const input   = document.getElementById('userInput');
const sendBtn = document.getElementById('sendBtn');
const msgs    = document.getElementById('messages');

// Scroll-to-bottom button
const scrollBtn = document.createElement('button');
scrollBtn.className = 'scroll-btn';
scrollBtn.innerHTML = '↓';
scrollBtn.title = 'Scroll to bottom';
scrollBtn.onclick = () => scrollToBottom(true);
document.querySelector('.chat-main').appendChild(scrollBtn);

// ── Events ───────────────────────────────────────────────────
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

msgs.addEventListener('scroll', () => {
  const atBottom = msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80;
  isScrolledUp = !atBottom;
  scrollBtn.classList.toggle('visible', isScrolledUp);
});

// ── Quick ask ────────────────────────────────────────────────
function quickAsk(text) {
  // Close sidebar on mobile when a quick-ask is triggered
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('overlay');
  if (sidebar && sidebar.classList.contains('open')) {
    sidebar.classList.remove('open');
    overlay.classList.remove('show');
  }
  input.value = text;
  sendMessage();
}

// ── Send message ─────────────────────────────────────────────
async function sendMessage() {
  const text = input.value.trim();
  if (!text || sendBtn.disabled) return;

  addMessage('user', text);
  history.push({ role: 'user', content: text });
  input.value = '';
  sendBtn.disabled = true;

  const typingId = showTyping();

  try {
    // Αποστολή max 40 μηνυμάτων για αποφυγή abuse
    const cappedHistory = history.slice(-40);
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ history: cappedHistory })
    });

    removeTyping(typingId);

    const data = await res.json().catch(() => ({}));
    const reply = data.reply || (res.ok ? '' : 'Προσωρινό πρόβλημα. Δοκίμασε ξανά σε λίγο.');

    if (reply) {
      addMessage('bot', reply);
      if (res.ok) {
        history.push({ role: 'assistant', content: reply });
      } else {
        // Σφάλμα — το μήνυμα δεν επεξεργάστηκε, αφαίρεσέ το από ιστορικό
        history.pop();
      }
    } else {
      history.pop();
    }
  } catch {
    removeTyping(typingId);
    history.pop(); // Αφαίρεσε το μήνυμα που δεν στάλθηκε
    addMessage('bot', 'Πρόβλημα σύνδεσης. Δοκίμασε ξανά σε λίγο. 🔌');
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

// ── Render message ────────────────────────────────────────────
function addMessage(role, text) {
  const row = document.createElement('div');
  row.className = `msg ${role}`;

  const content = document.createElement('div');
  content.className = 'msg-content';

  const bbl = document.createElement('div');
  bbl.className = 'bubble';
  bbl.innerHTML = role === 'bot' ? renderMarkdown(text) : escapeHtml(text).replace(/\n/g, '<br>');

  const meta = document.createElement('div');
  meta.className = 'msg-meta';
  meta.textContent = timeNow();

  content.appendChild(bbl);
  content.appendChild(meta);

  if (role === 'bot') {
    const av = document.createElement('div');
    av.className = 'msg-av';
    av.textContent = '✦';
    row.appendChild(av);
  }

  row.appendChild(content);
  msgs.appendChild(row);

  if (!isScrolledUp) scrollToBottom();
}

// ── Typing indicator ──────────────────────────────────────────
function showTyping() {
  const id = 'typing-' + Date.now();
  const row = document.createElement('div');
  row.className = 'msg bot';
  row.id = id;

  const av = document.createElement('div');
  av.className = 'msg-av';
  av.textContent = '✦';

  const content = document.createElement('div');
  content.className = 'msg-content';

  const bbl = document.createElement('div');
  bbl.className = 'typing-bubble';
  bbl.innerHTML = `<span class="tdot"></span><span class="tdot"></span><span class="tdot"></span>`;

  content.appendChild(bbl);
  row.appendChild(av);
  row.appendChild(content);
  msgs.appendChild(row);

  if (!isScrolledUp) scrollToBottom();
  return id;
}

function removeTyping(id) {
  document.getElementById(id)?.remove();
}

// ── Scroll ────────────────────────────────────────────────────
function scrollToBottom(force = false) {
  if (force || !isScrolledUp) {
    msgs.scrollTop = msgs.scrollHeight;
  }
}

// ── Time ──────────────────────────────────────────────────────
function timeNow() {
  const now = new Date();
  return now.toLocaleTimeString('el-GR', { hour: '2-digit', minute: '2-digit' });
}

// ── Markdown renderer ─────────────────────────────────────────
function renderMarkdown(text) {
  // Split into lines for processing
  const rawLines = text.split('\n');
  const out = [];
  let i = 0;

  while (i < rawLines.length) {
    const line = rawLines[i];

    // Horizontal rule: --- (standalone line)
    if (/^\s*[-*_]{3,}\s*$/.test(line)) {
      out.push('<hr class="msg-hr">');
      i++;
      continue;
    }

    // Table: detect header row (contains |) followed by separator row (|---|)
    if (line.includes('|') && i + 1 < rawLines.length && /^\s*\|?[\s\-:|]+\|/.test(rawLines[i + 1])) {
      // Parse table
      const tableLines = [];
      tableLines.push(line); // header
      i++;
      // separator line
      i++;
      // data rows
      while (i < rawLines.length && rawLines[i].includes('|')) {
        tableLines.push(rawLines[i]);
        i++;
      }

      const parseRow = (r) => r.split('|').map(c => c.trim()).filter(c => c !== '');

      const headers = parseRow(tableLines[0]);
      const rows = tableLines.slice(1).map(parseRow);

      let thtml = '<div class="msg-table-wrap"><table class="msg-table"><thead><tr>';
      headers.forEach(h => { thtml += `<th>${applyInline(escapeHtml(h))}</th>`; });
      thtml += '</tr></thead><tbody>';
      rows.forEach(r => {
        thtml += '<tr>';
        r.forEach(cell => { thtml += `<td>${applyInline(escapeHtml(cell))}</td>`; });
        thtml += '</tr>';
      });
      thtml += '</tbody></table></div>';
      out.push(thtml);
      continue;
    }

    // Bullet list: - item or • item
    const bullet = line.match(/^([-•])\s+(.+)/);
    if (bullet) {
      out.push(`<div class="msg-li"><span class="msg-bullet">•</span><span>${applyInline(escapeHtml(bullet[2]))}</span></div>`);
      i++;
      continue;
    }

    // Numbered list: 1. item
    const numbered = line.match(/^(\d+)\.\s+(.+)/);
    if (numbered) {
      out.push(`<div class="msg-li"><span class="msg-bullet">${numbered[1]}.</span><span>${applyInline(escapeHtml(numbered[2]))}</span></div>`);
      i++;
      continue;
    }

    // Empty line → paragraph spacing (we add a <br> separator)
    if (line.trim() === '') {
      // Only add spacing if previous item wasn't already a block element
      const last = out[out.length - 1] || '';
      if (last && !last.endsWith('<br>')) {
        out.push('<br>');
      }
      i++;
      continue;
    }

    // Normal line — add inline-formatted text followed by <br>
    out.push(applyInline(escapeHtml(line)) + '<br>');
    i++;
  }

  // Join everything, clean up excess breaks
  let result = out.join('');
  result = result.replace(/(<br>){3,}/g, '<br><br>');
  result = result.replace(/<br>$/, ''); // trim trailing <br>
  return result;
}

// Apply inline formatting (bold, italic) to already-escaped HTML
function applyInline(html) {
  // Bold **text**
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic *text* (not preceded or followed by *)
  html = html.replace(/\*([^*\n]+?)\*/g, '<em>$1</em>');
  return html;
}

// ── HTML escape ───────────────────────────────────────────────
function escapeHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Mobile sidebar ────────────────────────────────────────────
function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('overlay');
  sidebar.classList.toggle('open');
  overlay.classList.toggle('show');
}
