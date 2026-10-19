/* AgentWebView dashboard. Plain JavaScript, no build step, no external resources.
   Everything shown that came from a provider, a web page or an agent is inserted as text, never as HTML. */
'use strict';

// ---------------------------------------------------------------- small helpers

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style') el.style.cssText = v; // CSSOM, allowed by the page's Content-Security-Policy
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else if (k === 'selected') el.selected = !!v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, kids);
  return el;
}

function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

async function api(method, path, body, form) {
  const headers = { 'X-AgentWebView': '1' };
  const opts = { method, headers };
  if (form) opts.body = form;
  else if (body !== undefined) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const err = (data && data.error) || { message: (data && data.detail && JSON.stringify(data.detail)) || res.statusText };
    throw err;
  }
  return data;
}

function toast(message, kind) {
  const t = h('div', { class: 'toast' + (kind === 'bad' ? ' bad' : '') }, message);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.remove(), kind === 'bad' ? 7000 : 3500);
}

function errorText(e) {
  if (!e) return 'Something went wrong';
  let msg = e.message || String(e);
  if (e.details && e.details.errors) msg += ' — ' + e.details.errors.join('; ');
  return msg;
}

async function act(fn, okMessage) {
  try {
    const out = await fn();
    if (okMessage) toast(okMessage);
    return out;
  } catch (e) {
    toast(errorText(e), 'bad');
    throw e;
  }
}

function errorBox(e) {
  return h('div', { class: 'notice bad' },
    h('strong', null, e.code ? e.code.replaceAll('_', ' ').toLowerCase() + ': ' : ''), errorText(e),
    e.remediation ? h('div', { class: 'small' }, 'What to do: ', e.remediation) : null);
}

function when(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60 && diff > -60) return 'just now';
  if (diff < 3600 && diff > 0) return Math.round(diff / 60) + ' min ago';
  if (diff < 86400 && diff > 0) return Math.round(diff / 3600) + ' h ago';
  return d.toLocaleString();
}

function dateOnly(iso) { return iso ? new Date(iso).toLocaleDateString() : '—'; }

const UNIT_WORDS = {
  task_calls: 'requests', search_calls: 'searches', fetched_pages: 'pages', browser_ms: 'browser time',
  tavily_credits: 'credits', firecrawl_credits: 'credits', serpapi_searches: 'searches', jina_tokens: 'tokens',
};

const SINGULAR = { requests: 'request', searches: 'search', pages: 'page', credits: 'credit', tokens: 'token' };

function isUsd(unit) { return /_usd_micro$/.test(unit || ''); }

function fmtUnits(unit, n) {
  if (n === null || n === undefined || n === '') return '—';
  n = Number(n);
  if (isUsd(unit)) return '$' + (n / 1e6).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  if (unit === 'browser_ms') return (n / 60000).toLocaleString(undefined, { maximumFractionDigits: 1 }) + ' min';
  const word = UNIT_WORDS[unit] || unit.replace(/_/g, ' ');
  return n.toLocaleString() + ' ' + (n === 1 ? (SINGULAR[word] || word) : word);
}

function usageText(usage) {
  const parts = Object.entries(usage || {}).filter(([k]) => k !== 'task_calls').map(([k, v]) => fmtUnits(k, v));
  return parts.length ? parts.join(', ') : '1 request';
}

function statusBadge(status) {
  const cls = { succeeded: 'good', ok: 'good', connected: 'good', active: 'good', running: 'accent', starting: 'accent',
    failed: 'bad', invalid: 'bad', revoked: 'bad', denied: 'bad', uncertain: 'bad', canceled: 'warn', expired: 'warn',
    unverified: 'warn', waiting: 'warn', awaiting_decision: 'warn', closed: '', queued: 'accent', approved: 'accent' }[status];
  return h('span', { class: 'badge ' + (cls || '') }, String(status || '').replaceAll('_', ' '));
}

function meter(used, total) {
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  return h('div', { class: 'meter' + (pct >= 90 ? ' bad' : pct >= 70 ? ' warn' : '') }, h('span', { style: `width:${pct}%` }));
}

function copyButton(text, label) {
  return h('button', {
    class: 'small copy', type: 'button',
    onclick: async (ev) => {
      try { await navigator.clipboard.writeText(text); ev.target.textContent = 'Copied'; setTimeout(() => (ev.target.textContent = label || 'Copy'), 1500); }
      catch (_) { toast('Copy failed: select the text and copy it yourself', 'bad'); }
    },
  }, label || 'Copy');
}

function codeBlock(text) {
  return h('div', { class: 'code-wrap' }, h('pre', { class: 'code' }, text), copyButton(text));
}

function pageHead(title, sub, ...actions) {
  return h('div', { class: 'page-head' }, h('div', null, h('h1', null, title), sub ? h('p', { class: 'muted' }, sub) : null),
    actions.length ? h('div', { class: 'row' }, actions) : null);
}

function field(label, input, hint) {
  return h('label', null, label, input, hint ? h('span', { class: 'hint' }, hint) : null);
}

function formData(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === 'checkbox') {
      if (el.dataset.group) { (out[el.dataset.group] = out[el.dataset.group] || []); if (el.checked) out[el.dataset.group].push(el.name); }
      else out[el.name] = el.checked;
    } else out[el.name] = el.value;
  }
  return out;
}

let CONNECT = null;
async function connectInfo() {
  if (!CONNECT) CONNECT = await api('GET', '/api/connect');
  return CONNECT;
}

// ---------------------------------------------------------------- receipt rendering

function renderEvidence(evidence) {
  if (!evidence || !evidence.length) return h('p', { class: 'muted' }, 'No sources.');
  return h('div', { class: 'results' }, evidence.map((e) => h('div', { class: 'result' },
    h('div', { class: 'title' }, e.original_url
      ? h('a', { href: e.original_url, target: '_blank', rel: 'noopener noreferrer' }, e.title || e.original_url)
      : (e.title || e.local_identifier || e.source_id)),
    e.original_url ? h('div', { class: 'url' }, e.original_url) : (e.local_identifier ? h('div', { class: 'url' }, e.local_identifier) : null),
    h('div', { class: 'excerpt' }, e.excerpt || ''),
    h('div', { class: 'small muted' }, [e.extraction_method, e.published_at ? 'published ' + dateOnly(e.published_at) : null, e.fetched_at ? 'retrieved ' + when(e.fetched_at) : null].filter(Boolean).join(' · ')))));
}

function renderResultSummary(r) {
  const box = h('div', { class: 'stack' });
  const line = h('div', { class: 'row' }, statusBadge(r.status),
    r.engine ? h('span', null, 'answered by ', h('strong', null, r.engine)) : null,
    r.cache && r.cache.state && r.cache.state.includes('hit') ? h('span', { class: 'badge accent' }, 'from cache') : null,
    r.resource_usage ? h('span', { class: 'muted' }, 'cost: ', usageText(r.resource_usage)) : null,
    r.duration_ms !== undefined ? h('span', { class: 'muted' }, (r.duration_ms / 1000).toFixed(2) + ' s') : null,
    r.task_id ? h('a', { href: '#/task/' + r.task_id }, 'View receipt') : null);
  box.append(line);
  if (r.error) box.append(errorBox(r.error));
  if (r.coverage && r.coverage.statement) box.append(h('p', { class: 'small muted' }, r.coverage.statement));
  if (r.evidence) box.append(renderEvidence(r.evidence));
  if (r.content) box.append(h('details', null, h('summary', null, `Page text (${r.content.length.toLocaleString()} characters)`), h('pre', { class: 'content' }, r.content)));
  return box;
}

// ---------------------------------------------------------------- Home

async function Home() {
  const [ov, conn] = await Promise.all([api('GET', '/api/overview'), connectInfo()]);
  const s = ov.setup;
  const root = h('div', null, pageHead('Home', 'Give every AI agent a budget, a set of rules and a receipt for everything it does.'));
  if (ov.pending_approvals) {
    root.append(h('div', { class: 'notice warn' }, h('strong', null, `${ov.pending_approvals} browser action(s) waiting for you. `), h('a', { href: '#/approvals' }, 'Review them')));
  }
  root.append(h('div', { class: 'card' }, h('h2', null, 'Set up in three steps'), h('div', { class: 'steps' },
    h('div', { class: 'step' + (s.provider_keys ? ' done' : '') }, h('h3', null, 'Add your API keys'),
      h('p', { class: 'muted small' }, s.provider_keys ? `${s.provider_keys} key(s) connected.` : 'Exa, Tavily, Firecrawl, SerpApi and others. Most have a free tier.'),
      h('a', { class: 'button small', href: '#/providers' }, s.provider_keys ? 'Manage keys' : 'Add a key')),
    h('div', { class: 'step' + (s.agents_connected ? ' done' : '') }, h('h3', null, 'Connect an agent'),
      h('p', { class: 'muted small' }, s.agents ? `${s.agents} agent(s), ${s.agents_connected} connected.` : 'Claude Code, Cursor, VS Code, Claude Desktop or anything that speaks MCP or HTTP.'),
      h('a', { class: 'button small', href: '#/agents' }, s.agents ? 'Manage agents' : 'Connect an agent')),
    h('div', { class: 'step' + (ov.this_month.total ? ' done' : '') }, h('h3', null, 'Try it'),
      h('p', { class: 'muted small' }, 'Run a search below, or ask your agent to search the web.')))));

  // Try it
  const out = h('div');
  const mode = h('select', { name: 'mode' },
    h('option', { value: 'web' }, 'Search the web'), h('option', { value: 'docs' }, 'Search my documents'), h('option', { value: 'read' }, 'Read a page (URL)'));
  const q = h('input', { name: 'q', placeholder: 'What do you want to find?', required: true, style: 'flex:1;min-width:240px' });
  const btn = h('button', { class: 'primary', type: 'submit' }, 'Run');
  const tryForm = h('form', { class: 'row', onsubmit: async (ev) => {
    ev.preventDefault();
    const m = mode.value;
    const body = m === 'read'
      ? { operation: 'read_page', input: { url: q.value.trim() } }
      : { operation: 'search', input: { query: q.value }, requirements: { coverage: m === 'web' ? 'web_index' : 'owned_corpus' } };
    btn.disabled = true; out.replaceChildren(h('p', { class: 'muted' }, 'Running…'));
    try { out.replaceChildren(renderResultSummary(await api('POST', '/api/try', body))); }
    catch (e) { out.replaceChildren(errorBox(e)); }
    finally { btn.disabled = false; }
  } }, mode, q, btn);
  mode.addEventListener('change', () => { q.placeholder = mode.value === 'read' ? 'https://…' : 'What do you want to find?'; });
  root.append(h('div', { class: 'card' }, h('h2', null, 'Try it'), h('p', { class: 'muted small' }, 'Calls from here use your keys and count against the global limits, like an agent would (but no agent limit).'), tryForm, out));

  // This month
  const by = ov.this_month.by_status;
  root.append(h('div', { class: 'grid three' },
    h('div', { class: 'card stat' }, h('span', { class: 'muted' }, 'Calls this month'), h('span', { class: 'value' }, ov.this_month.total.toLocaleString())),
    h('div', { class: 'card stat' }, h('span', { class: 'muted' }, 'Succeeded'), h('span', { class: 'value' }, (by.succeeded || 0).toLocaleString())),
    h('div', { class: 'card stat' }, h('span', { class: 'muted' }, 'Failed or stopped'), h('span', { class: 'value' }, ((by.failed || 0) + (by.canceled || 0)).toLocaleString()))));

  const grid = h('div', { class: 'grid two' });
  grid.append(h('div', { class: 'card' }, h('h2', null, 'Who answered this month'),
    ov.this_month.by_engine.length ? h('table', null, h('tbody', null, ov.this_month.by_engine.map((e) => h('tr', null, h('td', null, e.engine), h('td', { class: 'nowrap' }, e.calls.toLocaleString() + ' calls')))))
      : h('p', { class: 'muted' }, 'Nothing yet.')));
  grid.append(h('div', { class: 'card' }, h('h2', null, 'Global limits'),
    ov.global_limits.length ? ov.global_limits.map((l) => h('div', { class: 'stack', style: 'margin-bottom:10px' },
      h('div', { class: 'row spread small' }, h('span', null, l.name), h('span', { class: 'muted' }, `${fmtUnits(l.denomination, l.used + l.held)} of ${fmtUnits(l.denomination, l.amount)}`)),
      meter(l.used + l.held, l.amount)))
      : h('p', { class: 'muted' }, 'No global limits: only agent limits and key allowances apply.'),
    h('a', { href: '#/settings', class: 'small' }, 'Change limits')));
  root.append(grid);
  root.append(h('div', { class: 'card' }, h('div', { class: 'row spread' }, h('h2', null, 'Recent activity'), h('a', { href: '#/activity', class: 'small' }, 'All activity')),
    activityTable(ov.recent)));
  root.append(h('p', { class: 'muted small' }, 'MCP endpoint: ', h('code', null, conn.mcp_url), ' · REST: ', h('code', null, conn.rest_url), ' · API docs: ', h('a', { href: '/docs', target: '_blank' }, '/docs')));
  return root;
}

// ---------------------------------------------------------------- Providers

function allowanceInputs(spec, current) {
  const usd = isUsd(spec.unit);
  const def = current ? { amount: current.amount, period: current.period, expires_at: current.expires_at } : (spec.free_allowance || { amount: '', period: 'monthly' });
  const amount = def.amount === '' || def.amount === undefined || def.amount === null ? '' : (usd ? Number(def.amount) / 1e6 : def.amount);
  const amountInput = h('input', { name: 'allowance', type: 'number', min: '0', step: usd ? '0.01' : '1', value: amount, placeholder: usd ? 'dollars' : 'amount' });
  const period = h('select', { name: 'period' },
    ['monthly', 'daily', 'lifetime'].map((p) => h('option', { value: p, selected: def.period === p }, { monthly: 'renews monthly', daily: 'renews daily', lifetime: 'never renews (one-time)' }[p])));
  const expires = h('input', { name: 'expires_at', type: 'date', value: def.expires_at ? def.expires_at.slice(0, 10) : '' });
  return {
    nodes: [
      field(usd ? 'Credit on this key ($)' : `Credit on this key (${UNIT_WORDS[spec.unit] || spec.unit_label})`, amountInput,
        spec.free_tier_text ? 'Free tier: ' + spec.free_tier_text : spec.unit_label),
      field('Renews', period), field('Expires (optional)', expires, 'Credit that expires soonest is spent first'),
    ],
    read: () => {
      const raw = amountInput.value.trim();
      if (raw === '') return { amount: null, period: period.value, expires_at: expires.value || null };
      const n = Number(raw);
      return { amount: Math.round(usd ? n * 1e6 : n), period: period.value, expires_at: expires.value ? expires.value + 'T23:59:59Z' : null };
    },
  };
}

function keyRow(spec, k, reload) {
  const a = k.allowance;
  const row = h('div', { class: 'key-row' });
  const edit = h('div', { class: 'hidden' });
  const status = h('div', { class: 'row spread' },
    h('div', { class: 'row' }, h('strong', null, k.label), h('span', { class: 'muted mono' }, k.key_hint), statusBadge(k.status),
      !k.enabled ? h('span', { class: 'badge' }, 'turned off') : null,
      k.paused_until ? h('span', { class: 'badge warn' }, 'paused until ' + new Date(k.paused_until).toLocaleTimeString()) : null),
    h('div', { class: 'row' },
      spec.free_key_check ? h('button', { class: 'small', onclick: async () => { await act(() => api('POST', `/api/keys/${k.id}/check`), 'Key checked'); reload(); } }, 'Check') : null,
      h('button', { class: 'small', onclick: () => edit.classList.toggle('hidden') }, 'Edit'),
      h('button', { class: 'small', onclick: async () => { await act(() => api('PATCH', `/api/keys/${k.id}`, { enabled: !k.enabled })); reload(); } }, k.enabled ? 'Turn off' : 'Turn on'),
      h('button', { class: 'small danger', onclick: async () => { if (!confirm(`Delete the key "${k.label}"? Its allowance and history of use are removed.`)) return; await act(() => api('DELETE', `/api/keys/${k.id}`), 'Key deleted'); reload(); } }, 'Delete')));
  row.append(status);
  if (a) {
    const used = a.used + a.held + a.written_off;
    row.append(h('div', { class: 'stack', style: 'margin-top:8px' },
      h('div', { class: 'row spread small' },
        h('span', null, `${fmtUnits(spec.unit, a.remaining)} left of ${fmtUnits(spec.unit, a.amount)}`, a.period === 'lifetime' ? ' (one-time)' : ` (${a.period})`),
        h('span', { class: 'muted' }, a.resets_at ? (a.period === 'lifetime' ? 'expires ' : 'resets ') + dateOnly(a.resets_at) : '')),
      meter(used, a.amount),
      a.written_off ? h('span', { class: 'small muted' }, 'The provider said this key ran out; the rest of this period was written off.') : null));
  } else {
    row.append(h('div', { class: 'notice warn small', style: 'margin:8px 0 0' }, 'No allowance recorded, so this key is never used. Click Edit and enter the credit it has.'));
  }
  row.append(h('div', { class: 'small muted', style: 'margin-top:6px' }, [k.status_detail, k.last_used_at ? 'last used ' + when(k.last_used_at) : 'not used yet'].filter(Boolean).join(' · ')));
  // edit form
  const al = allowanceInputs(spec, a);
  const label = h('input', { name: 'label', value: k.label });
  const secret = h('input', { name: 'key', type: 'password', placeholder: 'leave empty to keep the current key', autocomplete: 'off' });
  edit.append(h('div', { class: 'divider' }), h('form', { class: 'stack', onsubmit: async (ev) => {
    ev.preventDefault();
    await act(async () => {
      await api('PATCH', `/api/keys/${k.id}`, { label: label.value, allowance: al.read() });
      if (secret.value.trim()) await api('PUT', `/api/keys/${k.id}/secret`, { key: secret.value.trim() });
    }, 'Saved');
    reload();
  } }, h('div', { class: 'form-grid' }, field('Label', label), al.nodes, field('Replace the key', secret)),
  h('div', { class: 'row' }, h('button', { class: 'primary', type: 'submit' }, 'Save'), h('span', { class: 'small muted' }, 'Leave the credit empty to remove the allowance (the key will not be used).'))));
  row.append(edit);
  return row;
}

async function Providers() {
  const root = h('div');
  const body = h('div');
  const reload = async () => {
    const { providers } = await api('GET', '/api/providers');
    body.replaceChildren(...providers.map((p) => providerCard(p, reload)));
  };
  root.append(pageHead('Providers', 'Bring your own keys. Web searches spend the credit that expires soonest first, and switch to another provider when one runs out. Keys are encrypted on this computer and never shown again.'));
  root.append(body);
  await reload();
  return root;
}

function providerCard(p, reload) {
  const card = h('div', { class: 'card' });
  const ops = p.operations.map((o) => (o === 'search' ? 'web search' : 'read pages')).join(' + ');
  const addForm = h('form', { class: 'hidden stack' });
  const key = h('input', { name: 'key', type: 'password', placeholder: 'paste the API key', required: true, autocomplete: 'off', style: 'min-width:260px' });
  const label = h('input', { name: 'label', placeholder: `${p.name} key` });
  const al = allowanceInputs(p, null);
  const save = h('button', { class: 'primary', type: 'submit' }, 'Add key');
  addForm.append(h('div', { class: 'divider' }), h('div', { class: 'form-grid' }, field('API key', key), field('Label', label, 'Optional, e.g. "personal"'), al.nodes),
    h('div', { class: 'row' }, save, h('span', { class: 'small muted' }, p.free_key_check ? 'The key is checked with a free call when you add it.' : 'This provider has no free key check; the key is verified by its first call.')));
  addForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const a = al.read();
    save.disabled = true;
    try {
      const k = await act(() => api('POST', `/api/providers/${p.provider_id}/keys`, { key: key.value.trim(), label: label.value, allowance: a.amount, period: a.period, expires_at: a.expires_at }));
      toast(k.status === 'invalid' ? `${p.name} rejected that key` : `${p.name} key added`, k.status === 'invalid' ? 'bad' : undefined);
      reload();
    } catch (_) { save.disabled = false; }
  });
  card.append(h('div', { class: 'provider' },
    h('div', null, h('div', { class: 'row' }, h('h2', { style: 'margin:0' }, p.name), h('span', { class: 'badge accent' }, ops),
      p.keys.length ? h('span', { class: 'badge good' }, `${p.keys.length} key(s)`) : null),
    h('p', { class: 'muted small', style: 'margin-top:6px' }, p.free_tier_text ? 'Free tier: ' + p.free_tier_text + '. ' : '', p.notes)),
    h('div', { class: 'row', style: 'align-items:flex-start' },
      h('a', { class: 'button small', href: p.signup_url, target: '_blank', rel: 'noopener noreferrer' }, 'Get a key ↗'),
      h('a', { class: 'button small', href: p.docs_url, target: '_blank', rel: 'noopener noreferrer' }, 'Docs ↗'),
      h('button', { class: 'small primary', onclick: () => { addForm.classList.toggle('hidden'); key.focus(); } }, 'Add key'))));
  card.append(addForm);
  for (const k of p.keys) card.append(keyRow(p, k, reload));
  return card;
}

// ---------------------------------------------------------------- Agents

function connectionGuide(conn, token) {
  const t = token || 'awv_YOUR_TOKEN';
  const mcp = conn.mcp_url;
  const guides = [
    ['Claude Code', 'Run this in a terminal:', `claude mcp add --transport http agentwebview ${mcp} --header "Authorization: Bearer ${t}"`],
    ['Cursor', 'Add to ~/.cursor/mcp.json (or .cursor/mcp.json in a project):', JSON.stringify({ mcpServers: { agentwebview: { url: mcp, headers: { Authorization: `Bearer ${t}` } } } }, null, 2)],
    ['VS Code', 'Add to .vscode/mcp.json in your project:', JSON.stringify({ servers: { agentwebview: { type: 'http', url: mcp, headers: { Authorization: `Bearer ${t}` } } } }, null, 2)],
    ['Claude Desktop', 'Add to claude_desktop_config.json (needs the agentwebview command installed; it shares this data folder):', JSON.stringify({ mcpServers: { agentwebview: { command: 'agentwebview', args: ['mcp'], env: { AGENTWEBVIEW_TOKEN: t, AGENTWEBVIEW_DATA: conn.data_dir } } } }, null, 2)],
    ['Other MCP clients', 'Streamable HTTP MCP endpoint and header:', `URL:     ${mcp}\nHeader:  Authorization: Bearer ${t}`],
    ['HTTP / curl', 'Call the REST API directly:', `curl -s ${conn.rest_url}/tasks \\\n  -H "Authorization: Bearer ${t}" -H "Content-Type: application/json" \\\n  -d '{"operation":"search","input":{"query":"latest MCP spec"},"requirements":{"coverage":"web_index"}}'`],
  ];
  const tabs = h('div', { class: 'tabs' });
  const pane = h('div');
  const show = (i) => {
    [...tabs.children].forEach((b, j) => b.classList.toggle('active', i === j));
    pane.replaceChildren(h('p', { class: 'small muted' }, guides[i][1]), codeBlock(guides[i][2]));
  };
  guides.forEach((g, i) => tabs.append(h('button', { type: 'button', onclick: () => show(i) }, g[0])));
  show(0);
  return h('div', null, tabs, pane,
    h('p', { class: 'small muted', style: 'margin-top:10px' }, 'Then ask your agent something like: "Use agentwebview to search the web for …". Tools: search, read_page, crawl_site, add_document, browser_action, task_status, routing_info, rate_result.'));
}

function abilityBoxes(selected) {
  const opts = [
    ['add_documents', 'Add documents', 'save text into your collections'],
    ['crawl', 'Crawl websites', 'into a collection, within your limits'],
    ['browser', 'Use the browser', 'risky clicks still need your approval'],
  ];
  return h('div', { class: 'stack' },
    h('label', { class: 'inline' }, h('input', { type: 'checkbox', checked: true, disabled: true }), 'Search and read pages ', h('span', { class: 'hint' }, '(always on)')),
    opts.map(([name, title, hint]) => h('label', { class: 'inline' }, h('input', { type: 'checkbox', name, 'data-group': 'abilities', checked: selected.includes(name) }), title, h('span', { class: 'hint' }, hint))));
}

async function Agents() {
  const conn = await connectInfo();
  const root = h('div');
  const list = h('div');
  const created = h('div');
  const reload = async () => {
    const { agents } = await api('GET', '/api/agents');
    list.replaceChildren(...(agents.length ? agents.map((a) => agentCard(a, conn, reload, created)) : [h('div', { class: 'card empty' }, 'No agents yet. Create one above.')]));
  };
  const form = h('form', { class: 'stack', onsubmit: async (ev) => {
    ev.preventDefault();
    const d = formData(form);
    const res = await act(() => api('POST', '/api/agents', { name: d.name, abilities: d.abilities || [], limits: { monthly: d.monthly, daily: d.daily } }));
    form.reset();
    showToken(created, res.agent, res.token, conn);
    reload();
  } },
  h('div', { class: 'form-grid' },
    field('Name', h('input', { name: 'name', required: true, placeholder: 'e.g. Claude Code on my laptop' })),
    field('Requests per month', h('input', { name: 'monthly', type: 'number', min: '0', placeholder: 'no limit' })),
    field('Requests per day', h('input', { name: 'daily', type: 'number', min: '0', placeholder: 'no limit' }))),
  abilityBoxes([]),
  h('div', null, h('button', { class: 'primary', type: 'submit' }, 'Create agent and show its token')));
  root.append(pageHead('Agents', 'Each agent gets its own token, abilities and limits, and every call it makes is recorded on the Activity screen.'));
  root.append(h('div', { class: 'card' }, h('h2', null, 'New agent'), form));
  root.append(created, list);
  await reload();
  return root;
}

function showToken(target, agent, token, conn) {
  target.replaceChildren(h('div', { class: 'card' },
    h('div', { class: 'row spread' }, h('h2', null, `Connect “${agent.name}”`), h('button', { class: 'small', onclick: () => target.replaceChildren() }, 'Done')),
    h('div', { class: 'notice warn' }, h('strong', null, 'Copy this token now. '), 'It is shown only once; if you lose it, make a new one with "New token".'),
    h('div', { class: 'row' }, h('div', { class: 'secret', style: 'flex:1' }, token), copyButton(token, 'Copy token')),
    h('div', { class: 'divider' }), connectionGuide(conn, token)));
  target.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function agentCard(a, conn, reload, created) {
  const card = h('div', { class: 'card' });
  const lim = (l, word) => (l ? h('div', { class: 'stack', style: 'min-width:180px' },
    h('div', { class: 'row spread small' }, h('span', null, word), h('span', { class: 'muted' }, `${l.used + l.held} / ${l.amount}`)), meter(l.used + l.held, l.amount))
    : h('span', { class: 'small muted' }, word + ': no limit'));
  const edit = h('form', { class: 'hidden stack' });
  card.append(
    h('div', { class: 'row spread' },
      h('div', { class: 'row' }, h('h2', { style: 'margin:0' }, a.name), statusBadge(a.status), h('span', { class: 'mono muted small' }, a.token_hint)),
      h('div', { class: 'row' },
        h('button', { class: 'small', onclick: () => edit.classList.toggle('hidden') }, 'Edit'),
        h('button', { class: 'small', onclick: () => {
          created.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'row spread' }, h('h2', null, `Connect “${a.name}”`), h('button', { class: 'small', onclick: () => created.replaceChildren() }, 'Done')),
            h('p', { class: 'small muted' }, 'Tokens are not stored in readable form. Use the token you copied when you created the agent, or make a new one.'), connectionGuide(conn, null)));
          created.scrollIntoView({ behavior: 'smooth' });
        } }, 'Setup'),
        h('button', { class: 'small', onclick: async () => {
          if (!confirm(`Make a new token for "${a.name}"? The old one stops working immediately.`)) return;
          const res = await act(() => api('POST', `/api/agents/${a.id}/rotate`));
          showToken(created, res.agent, res.token, conn); reload();
        } }, 'New token'),
        a.status !== 'revoked'
          ? h('button', { class: 'small danger', onclick: async () => { if (!confirm(`Revoke "${a.name}"? Its token stops working.`)) return; await act(() => api('POST', `/api/agents/${a.id}/revoke`), 'Revoked'); reload(); } }, 'Revoke')
          : h('button', { class: 'small danger', onclick: async () => { if (!confirm(`Delete "${a.name}"? Its activity stays on record.`)) return; await act(() => api('DELETE', `/api/agents/${a.id}`), 'Deleted'); reload(); } }, 'Delete'))),
    h('div', { class: 'row', style: 'margin-top:10px;gap:24px;align-items:flex-start' },
      h('div', { class: 'small' }, h('div', { class: 'muted' }, 'Can use'), a.abilities.map((x) => x.replace('_', ' ')).join(', ')),
      h('div', { class: 'small' }, h('div', { class: 'muted' }, 'This month'), `${a.this_month.calls} calls, ${a.this_month.succeeded} succeeded`),
      h('div', { class: 'small' }, h('div', { class: 'muted' }, 'Last call'), a.last_used_at ? when(a.last_used_at) : 'never'),
      lim(a.limits.monthly, 'This month'), lim(a.limits.daily, 'Today'),
      h('a', { class: 'small', href: '#/activity?agent=' + a.id }, 'Activity')));
  edit.append(h('div', { class: 'divider' }), h('div', { class: 'form-grid' },
    field('Name', h('input', { name: 'name', value: a.name })),
    field('Requests per month', h('input', { name: 'monthly', type: 'number', min: '0', value: a.limits.monthly ? a.limits.monthly.amount : '', placeholder: 'no limit' })),
    field('Requests per day', h('input', { name: 'daily', type: 'number', min: '0', value: a.limits.daily ? a.limits.daily.amount : '', placeholder: 'no limit' }))),
  abilityBoxes(a.abilities), h('div', null, h('button', { class: 'primary', type: 'submit' }, 'Save')));
  edit.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const d = formData(edit);
    await act(() => api('PATCH', `/api/agents/${a.id}`, { name: d.name, abilities: d.abilities || [], limits: { monthly: d.monthly, daily: d.daily } }), 'Saved');
    reload();
  });
  card.append(edit);
  return card;
}

// ---------------------------------------------------------------- Knowledge

async function Knowledge(selectedId) {
  const root = h('div', null, pageHead('Knowledge', 'Your own documents. Agents search them with the search tool (the default scope); nothing here leaves this computer.'));
  const { collections } = await api('GET', '/api/collections');
  const current = collections.find((c) => c.id === selectedId) || collections[0];
  const nameInput = h('input', { name: 'name', placeholder: 'e.g. product docs', required: true });
  root.append(h('div', { class: 'card' }, h('form', { class: 'row', onsubmit: async (ev) => {
    ev.preventDefault();
    const c = await act(() => api('POST', '/api/collections', { name: nameInput.value }), 'Collection created');
    location.hash = '#/knowledge/' + c.id;
  } }, h('strong', null, 'New collection'), nameInput, h('button', { class: 'primary', type: 'submit' }, 'Create'))));
  if (!collections.length) {
    root.append(h('div', { class: 'card empty' }, 'No collections yet. Create one, then add files or crawl a website into it.'));
    return root;
  }
  root.append(h('div', { class: 'tabs' }, collections.map((c) => h('button', { class: c.id === current.id ? 'active' : '', onclick: () => (location.hash = '#/knowledge/' + c.id) }, `${c.name} (${c.documents})`))));
  root.append(await collectionPanel(current));
  return root;
}

async function collectionPanel(c) {
  const { documents } = await api('GET', `/api/collections/${c.id}/documents`);
  const panel = h('div');
  const refresh = () => route();

  const fileInput = h('input', { type: 'file', multiple: true, accept: '.txt,.md,.markdown,.html,.htm' });
  const drop = h('div', { class: 'drop' }, 'Drop .txt, .md or .html files here, or ', fileInput);
  const upload = async (files) => {
    if (!files.length) return;
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    const res = await act(() => api('POST', `/api/collections/${c.id}/upload`, undefined, fd));
    const bad = res.results.filter((r) => !r.ok);
    toast(`${res.results.length - bad.length} file(s) added` + (bad.length ? `, ${bad.length} refused: ${bad.map((b) => b.file + ' (' + b.error.message + ')').join('; ')}` : ''), bad.length ? 'bad' : undefined);
    refresh();
  };
  fileInput.addEventListener('change', () => upload([...fileInput.files]));
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); upload([...e.dataTransfer.files]); });

  const noteTitle = h('input', { name: 'title', placeholder: 'Title' });
  const noteText = h('textarea', { name: 'text', placeholder: 'Paste text or Markdown', required: true });
  const noteForm = h('form', { class: 'stack', onsubmit: async (ev) => {
    ev.preventDefault();
    const fd = new FormData(); fd.append('title', noteTitle.value); fd.append('text', noteText.value);
    await act(() => api('POST', `/api/collections/${c.id}/text`, undefined, fd), 'Saved');
    refresh();
  } }, noteTitle, noteText, h('div', null, h('button', { class: 'primary', type: 'submit' }, 'Save text')));

  const seed = h('input', { name: 'seed', type: 'url', placeholder: 'https://docs.example.com/', required: true, style: 'min-width:280px' });
  const pages = h('input', { name: 'pages', type: 'number', min: '1', value: '50' });
  const depth = h('input', { name: 'depth', type: 'number', min: '0', max: '10', value: '2' });
  const crawlForm = h('form', { class: 'stack', onsubmit: async (ev) => {
    ev.preventDefault();
    const res = await act(() => api('POST', '/api/crawls', { collection_id: c.id, seeds: [seed.value.trim()], max_pages: Number(pages.value), max_depth: Number(depth.value) }), 'Crawl started');
    location.hash = '#/task/' + res.task_id;
  } }, h('div', { class: 'form-grid' }, field('Start URL', seed), field('Max pages', pages), field('Link depth', depth)),
  h('p', { class: 'small muted', style: 'margin:0' }, 'Stays on the start site, respects robots.txt, waits between requests and stops at your page limits.'),
  h('div', null, h('button', { class: 'primary', type: 'submit' }, 'Start crawl')));

  panel.append(h('div', { class: 'grid two' },
    h('div', { class: 'card' }, h('h2', null, 'Add files'), drop, h('div', { class: 'divider' }), h('h3', null, 'Or paste text'), noteForm),
    h('div', { class: 'card' }, h('h2', null, 'Crawl a website into this collection'), crawlForm)));
  panel.append(h('div', { class: 'card' },
    h('div', { class: 'row spread' }, h('h2', null, `${c.name}: ${documents.length} document(s)`),
      h('button', { class: 'small danger', onclick: async () => { if (!confirm(`Delete the collection "${c.name}" and all its documents?`)) return; await act(() => api('DELETE', `/api/collections/${c.id}`), 'Deleted'); location.hash = '#/knowledge'; } }, 'Delete collection')),
    documents.length ? h('table', null, h('thead', null, h('tr', null, h('th', null, 'Title'), h('th', null, 'Source'), h('th', null, 'Size'), h('th', null, 'Updated'), h('th'))),
      h('tbody', null, documents.map((d) => h('tr', null,
        h('td', null, d.title || d.local_identifier),
        h('td', { class: 'small' }, d.original_url ? h('a', { href: d.original_url, target: '_blank', rel: 'noopener noreferrer' }, d.original_url) : h('span', { class: 'muted' }, d.source_type)),
        h('td', { class: 'nowrap small' }, d.chars.toLocaleString() + ' chars'),
        h('td', { class: 'nowrap small' }, when(d.updated_at)),
        h('td', null, h('button', { class: 'small danger', onclick: async () => { await act(() => api('DELETE', `/api/documents/${d.id}`), 'Deleted'); refresh(); } }, 'Delete'))))))
      : h('p', { class: 'muted' }, 'Empty. Add files, paste text, or crawl a site.')));
  return panel;
}

// ---------------------------------------------------------------- Approvals and browser

async function Approvals() {
  const [{ pending, browser }, { sessions }] = await Promise.all([api('GET', '/api/approvals'), api('GET', '/api/browser/sessions')]);
  const root = h('div', null, pageHead('Approvals', 'Browser clicks that could submit, send, buy, delete or publish wait here until you decide. Nothing runs without your yes.'));
  if (!browser.enabled) root.append(h('div', { class: 'notice' }, 'The browser is turned off. ', h('a', { href: '#/settings' }, 'Turn it on in Settings'), ' to let agents use it.'));
  else if (!browser.installed) root.append(h('div', { class: 'notice warn' }, browser.detail));
  root.append(h('div', { class: 'card' }, h('h2', null, `Waiting for you (${pending.length})`),
    pending.length ? pending.map((a) => approvalCard(a)) : h('p', { class: 'muted' }, 'Nothing is waiting.')));
  root.append(h('div', { class: 'card' }, h('h2', null, 'Browser sessions'),
    sessions.length ? h('table', null, h('thead', null, h('tr', null, h('th', null, 'Started'), h('th', null, 'Page'), h('th', null, 'State'), h('th', null, 'Time used'))),
      h('tbody', null, sessions.map((s) => h('tr', { class: 'clickable', onclick: () => (location.hash = '#/session/' + s.id) },
        h('td', { class: 'nowrap small' }, when(s.created_at)),
        h('td', { class: 'small' }, s.title || s.current_url || s.start_url || s.allowed_hosts.join(', ')),
        h('td', null, statusBadge(s.state)),
        h('td', { class: 'small nowrap' }, (s.browser_ms / 1000).toFixed(1) + ' s')))))
      : h('p', { class: 'muted' }, 'No browser sessions yet.')));
  return root;
}

function approvalCard(a) {
  const note = h('input', { placeholder: 'Note (optional)', style: 'flex:1' });
  const el = a.element || {};
  const decide = async (approve) => {
    await act(() => api('POST', `/api/approvals/${a.id}`, { approve, note: note.value }), approve ? 'Approved: the action runs now' : 'Denied');
    route();
  };
  return h('div', { class: 'key-row' },
    h('div', { class: 'row' }, h('span', { class: 'badge warn' }, a.write_category), h('strong', null, `${a.agent_id ? 'An agent' : 'The dashboard'} wants to click “${(el.text || el.aria || el.value || el.title || 'an element').trim().slice(0, 80)}”`)),
    h('dl', { class: 'kv small', style: 'margin:8px 0' },
      h('dt', null, 'Page'), h('dd', null, a.page_title ? a.page_title + ' — ' : '', a.page_url || 'unknown'),
      h('dt', null, 'Element'), h('dd', { class: 'mono' }, `<${(el.tag || '?').toLowerCase()}${el.type ? ' type=' + el.type : ''}${el.form ? ' in a form (' + (el.method || 'get') + ')' : ''}>`),
      h('dt', null, 'Selector'), h('dd', { class: 'mono' }, JSON.stringify((a.params || {}).selector || {})),
      h('dt', null, 'Asked'), h('dd', null, when(a.created_at))),
    h('div', { class: 'row' }, note, h('button', { class: 'primary', onclick: () => decide(true) }, 'Approve'), h('button', { class: 'danger', onclick: () => decide(false) }, 'Deny'),
      h('a', { class: 'small', href: '#/session/' + a.session_id }, 'Session')));
}

async function Session(id) {
  const s = await api('GET', '/api/browser/sessions/' + id);
  const root = h('div', null, pageHead('Browser session', s.title || s.current_url || s.start_url || '',
    h('a', { class: 'button small', href: '#/approvals' }, 'Back'),
    ['starting', 'active'].includes(s.state) ? h('button', { class: 'small danger', onclick: async () => { await act(() => api('POST', `/api/browser/sessions/${id}/close`), 'Closed'); route(); } }, 'Close session') : null));
  root.append(h('div', { class: 'card' }, h('dl', { class: 'kv' },
    h('dt', null, 'State'), h('dd', null, statusBadge(s.state), s.close_reason ? ' ' + s.close_reason : ''),
    h('dt', null, 'Allowed hosts'), h('dd', null, s.allowed_hosts.join(', ')),
    h('dt', null, 'Write capabilities'), h('dd', null, s.write_capabilities.length ? s.write_capabilities.join(', ') : 'none (risky clicks need approval)'),
    h('dt', null, 'Time used'), h('dd', null, (s.browser_ms / 1000).toFixed(1) + ' s'),
    h('dt', null, 'Receipt'), h('dd', null, h('a', { href: '#/task/' + s.task_id }, s.task_id)))));
  root.append(h('div', { class: 'card' }, h('h2', null, 'Actions'), s.actions.length ? s.actions.map((a) => h('div', { class: 'key-row' },
    h('div', { class: 'row spread' }, h('div', { class: 'row' }, h('strong', null, `${a.seq}. ${a.type}`), statusBadge(a.state), a.write_category ? h('span', { class: 'badge warn' }, a.write_category) : null),
      h('span', { class: 'small muted' }, when(a.created_at))),
    h('div', { class: 'mono small muted', style: 'margin-top:4px' }, JSON.stringify(a.params)),
    a.error ? h('div', { class: 'small', style: 'color:var(--bad)' }, a.error.message) : null,
    a.result && a.result.url ? h('div', { class: 'small' }, a.result.title || '', ' ', h('span', { class: 'muted' }, a.result.url)) : null,
    a.result && a.result.text ? h('details', null, h('summary', null, 'Extracted text'), h('pre', { class: 'content' }, a.result.text)) : null,
    a.screenshot_url ? h('img', { class: 'screenshot', src: a.screenshot_url, alt: 'Screenshot', loading: 'lazy' }) : null))
    : h('p', { class: 'muted' }, 'No actions.')));
  return root;
}

// ---------------------------------------------------------------- Activity and receipts

function activityTable(items) {
  if (!items.length) return h('p', { class: 'muted' }, 'Nothing yet.');
  return h('table', null, h('thead', null, h('tr', null, h('th', null, 'When'), h('th', null, 'Who'), h('th', null, 'What'), h('th', null, 'Answered by'), h('th', null, 'Status'), h('th', null, 'Cost'))),
    h('tbody', null, items.map((t) => h('tr', { class: 'clickable', onclick: () => (location.hash = '#/task/' + t.id) },
      h('td', { class: 'nowrap small' }, when(t.created_at)),
      h('td', { class: 'small' }, t.agent_name || '—'),
      h('td', null, h('span', { class: 'badge' }, t.operation.replace('_', ' ')), ' ', h('span', { class: 'small' }, (t.summary || '').slice(0, 120))),
      h('td', { class: 'small' }, t.engine || '—', t.key_label ? h('div', { class: 'muted' }, t.key_label) : null),
      h('td', null, statusBadge(t.status), t.error_code && t.status !== 'succeeded' ? h('div', { class: 'small muted' }, t.error_code.replaceAll('_', ' ').toLowerCase()) : null),
      h('td', { class: 'small' }, t.status === 'running' ? '…' : usageText(t.usage))))));
}

async function Activity() {
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  const { agents } = await api('GET', '/api/agents');
  const filters = { agent: params.get('agent') || '', operation: params.get('operation') || '', status: params.get('status') || '' };
  const sel = (name, options) => h('select', { name, onchange: (ev) => { filters[name] = ev.target.value; load(true); } },
    options.map(([v, t]) => h('option', { value: v, selected: filters[name] === v }, t)));
  const table = h('div');
  let items = [];
  const more = h('button', { class: 'small', onclick: () => load(false) }, 'Load more');
  const load = async (reset) => {
    const q = new URLSearchParams({ limit: '100' });
    for (const [k, v] of Object.entries(filters)) if (v) q.set(k, v);
    if (!reset && items.length) q.set('before', items[items.length - 1].created_at);
    const res = await api('GET', '/api/activity?' + q.toString());
    items = reset ? res.items : items.concat(res.items);
    table.replaceChildren(activityTable(items));
    more.classList.toggle('hidden', res.items.length < 100);
  };
  const root = h('div', null, pageHead('Activity', 'Every call, with who made it, which provider answered, what it cost and its sources. Click a row for the full receipt.'),
    h('div', { class: 'card' }, h('div', { class: 'row', style: 'margin-bottom:10px' },
      sel('agent', [['', 'All agents'], ...agents.map((a) => [a.id, a.name])]),
      sel('operation', [['', 'Everything'], ['search', 'Searches'], ['read_page', 'Page reads'], ['crawl', 'Crawls'], ['browser_session', 'Browser']]),
      sel('status', [['', 'Any status'], ['succeeded', 'Succeeded'], ['failed', 'Failed'], ['running', 'Running'], ['canceled', 'Cancelled']]),
      h('button', { class: 'small', onclick: () => load(true) }, 'Refresh')), table, more));
  await load(true);
  return root;
}

function routeView(trace) {
  const planned = (trace || []).find((e) => e.step === 'route_planned');
  if (!planned) return null;
  return h('div', null,
    h('h3', null, `Route (${planned.mode}${planned.rotation === 'round_robin' ? ', taking turns' : ''})`),
    planned.candidates.length ? h('ol', { class: 'small' }, planned.candidates.map((c) => h('li', null, h('strong', null, c.engine_id), c.key ? ` — key “${c.key.label}”` + (c.key.credit_remaining !== null ? `, ${c.key.credit_remaining} left` : '') : '', h('span', { class: 'muted' }, ` score ${c.score}`)))) : h('p', { class: 'small muted' }, 'No engine was eligible.'),
    planned.exclusions.length ? h('details', null, h('summary', null, `Not used (${planned.exclusions.length})`), h('ul', { class: 'small' }, planned.exclusions.map((e) => h('li', null, h('strong', null, e.engine_id), e.key ? ` (${e.key.label})` : '', ': ', e.reasons.join('; '))))) : null);
}

async function Task(id) {
  const t = await api('GET', '/api/tasks/' + id);
  const root = h('div', null, pageHead('Receipt', `${(t.operation || '').replace('_', ' ')} · ${t.agent_name || (t.origin === 'dashboard' ? 'Dashboard' : t.origin || '')} · ${when(t.created_at)}`,
    h('a', { class: 'button small', href: '#/activity' }, 'Back'),
    t.status === 'running' ? h('button', { class: 'small danger', onclick: async () => { await act(() => api('POST', `/api/tasks/${id}/cancel`), 'Cancel requested'); route(); } }, 'Cancel') : null,
    t.status === 'running' ? h('button', { class: 'small', onclick: () => route() }, 'Refresh') : null));
  const req = t.request || {};
  const what = req.input ? (req.input.query || req.input.url || req.input.source_id || (req.input.seeds || []).join(', ')) : (req.start_url || '');
  root.append(h('div', { class: 'card' }, h('dl', { class: 'kv' },
    h('dt', null, 'Asked'), h('dd', null, what || '—'),
    h('dt', null, 'Status'), h('dd', null, statusBadge(t.status)),
    t.engine ? [h('dt', null, 'Answered by'), h('dd', null, t.engine)] : null,
    t.resource_usage ? [h('dt', null, 'Charged'), h('dd', null, Object.entries(t.resource_usage).map(([k, v]) => fmtUnits(k, v)).join(', ') || '—')] : null,
    t.duration_ms !== undefined ? [h('dt', null, 'Took'), h('dd', null, (t.duration_ms / 1000).toFixed(2) + ' s')] : null,
    t.cache ? [h('dt', null, 'Cache'), h('dd', null, t.cache.state)] : null,
    t.coverage && t.coverage.statement ? [h('dt', null, 'Coverage'), h('dd', null, t.coverage.statement)] : null)));
  if (t.error) root.append(errorBox(t.error));
  if (t.progress) root.append(h('div', { class: 'card' }, h('h2', null, 'Progress'), h('dl', { class: 'kv small' }, Object.entries(t.progress).map(([k, v]) => [h('dt', null, k.replaceAll('_', ' ')), h('dd', null, String(v))]))));
  if (t.session) root.append(h('div', { class: 'card' }, h('a', { href: '#/session/' + t.session.id }, 'Open the browser session')));
  if (t.session_id) root.append(h('div', { class: 'card' }, h('a', { href: '#/session/' + t.session_id }, 'Open the browser session')));
  if (t.evidence) root.append(h('div', { class: 'card' }, h('h2', null, `Sources (${t.evidence.length})`), renderEvidence(t.evidence)));
  if (t.content) root.append(h('div', { class: 'card' }, h('h2', null, 'Page text'), h('pre', { class: 'content' }, t.content)));
  if (t.attempts && t.attempts.length) root.append(h('div', { class: 'card' }, h('h2', null, 'Attempts'),
    h('table', null, h('thead', null, h('tr', null, h('th', null, '#'), h('th', null, 'Engine'), h('th', null, 'Key'), h('th', null, 'Result'), h('th', null, 'Used'), h('th', null, 'Time'))),
      h('tbody', null, t.attempts.map((a) => h('tr', null, h('td', null, a.attempt), h('td', null, a.engine_id), h('td', { class: 'small' }, a.key ? a.key.label : '—'),
        h('td', null, statusBadge(a.state), a.error_code ? h('div', { class: 'small muted' }, a.error_code) : null),
        h('td', { class: 'small' }, Object.entries(a.usage || {}).map(([k, v]) => fmtUnits(k, v)).join(', ')),
        h('td', { class: 'small nowrap' }, a.latency_ms + ' ms')))))));
  const rv = routeView(t.trace);
  if (rv) root.append(h('div', { class: 'card' }, rv));
  if (t.status === 'succeeded' && t.operation !== 'browser_session') {
    const rate = (relevance) => async () => { await act(() => api('POST', `/api/tasks/${id}/rating`, { relevance }), 'Thanks: routing learns from this'); };
    root.append(h('div', { class: 'card row' }, h('span', null, 'Was this result useful?'),
      h('button', { class: 'small', onclick: rate('relevant') }, 'Relevant'), h('button', { class: 'small', onclick: rate('partial') }, 'Partly'), h('button', { class: 'small', onclick: rate('irrelevant') }, 'Not relevant')));
  }
  root.append(h('details', { class: 'card' }, h('summary', null, 'Raw receipt (JSON)'), h('pre', { class: 'content mono' }, JSON.stringify(t, null, 2))));
  if (t.status === 'running') setTimeout(() => { if (location.hash === '#/task/' + id) route(); }, 3000);
  return root;
}

// ---------------------------------------------------------------- Settings

async function Settings() {
  const s = await api('GET', '/api/settings');
  const p = s.policy;
  const root = h('div', null, pageHead('Settings', 'The rules every call follows. Changes apply to the next call.'));
  const save = async (patch, msg) => { await act(() => api('PATCH', '/api/settings', patch), msg || 'Saved'); route(); };
  const toggle = (label, hint, checked, patchFn) => h('label', { class: 'inline' }, h('input', { type: 'checkbox', checked, onchange: (ev) => save(patchFn(ev.target.checked)) }), label, hint ? h('span', { class: 'hint' }, hint) : null);
  const select = (value, options, patchFn) => h('select', { onchange: (ev) => save(patchFn(ev.target.value)) }, options.map(([v, t]) => h('option', { value: v, selected: value === v }, t)));

  root.append(h('div', { class: 'card stack' }, h('h2', null, 'What agents may use'),
    toggle('Web search providers', 'Exa, Tavily, Firecrawl, SerpApi… through the keys you added', p.providers_enabled, (v) => ({ providers_enabled: v })),
    toggle('Read websites directly', 'the built-in page reader and the crawler; private networks are always refused', p.direct_web.enabled, (v) => ({ direct_web: { enabled: v } })),
    toggle('Respect robots.txt', null, p.direct_web.respect_robots, (v) => ({ direct_web: { respect_robots: v } })),
    toggle('Any public website', 'off: only the hosts listed below', p.direct_web.allow_any_public_host, (v) => ({ direct_web: { allow_any_public_host: v } })),
    p.direct_web.allow_any_public_host ? null : (() => {
      const hosts = h('input', { value: p.direct_web.allowed_hosts.join(', '), placeholder: 'docs.python.org, example.com', style: 'min-width:320px' });
      return h('div', { class: 'row' }, field('Allowed hosts', hosts, 'comma-separated, exact host names'),
        h('button', { class: 'small', onclick: () => save({ direct_web: { allowed_hosts: hosts.value.split(',').map((x) => x.trim().toLowerCase()).filter(Boolean) } }) }, 'Save hosts'));
    })()));

  const b = s.browser;
  root.append(h('div', { class: 'card stack' }, h('h2', null, 'Browser'),
    b.installed ? null : h('div', { class: 'notice warn small' }, b.detail),
    toggle('Agents may use a sandboxed browser', 'Chromium in its own process; every request goes through AgentWebView\'s proxy', p.browser.enabled, (v) => ({ browser: { enabled: v } })),
    h('div', { class: 'form-grid' },
      field('Risky clicks (submit, send, buy, delete, publish)', select(p.browser.write_actions, [['require_decision', 'Ask me first (Approvals)'], ['deny', 'Never allow'], ['allow_with_capability', 'Allow when the agent asked for that capability']], (v) => ({ browser: { write_actions: v } }))),
      field('Sessions at once', h('input', { type: 'number', min: '0', max: '16', value: p.browser.max_sessions, onchange: (ev) => save({ browser: { max_sessions: Number(ev.target.value) } }) })),
      field('Longest session (minutes)', h('input', { type: 'number', min: '1', max: '60', value: Math.round(p.browser.max_session_ms / 60000), onchange: (ev) => save({ browser: { max_session_ms: Number(ev.target.value) * 60000 } }) }))),
    toggle('Keep screenshots', null, p.browser.screenshots, (v) => ({ browser: { screenshots: v } }))));

  root.append(h('div', { class: 'card stack' }, h('h2', null, 'Routing'),
    h('div', { class: 'form-grid' },
      field('Prefer', select(p.routing_mode, [['economical', 'Cheapest (free engines and credit first)'], ['balanced', 'Balanced'], ['quality', 'Best measured quality']], (v) => ({ routing_mode: v }))),
      field('Web searches', select(p.search_rotation, [['score', 'Use the best key, others as fallback'], ['round_robin', 'Take turns across all keys with credit']], (v) => ({ search_rotation: v }))),
      field('Attempts per call', h('input', { type: 'number', min: '1', max: '5', value: p.max_attempts, onchange: (ev) => save({ max_attempts: Number(ev.target.value) }) }), 'tries the next engine when one fails'),
      field('Time zone for monthly and daily limits', h('input', { value: p.timezone, onchange: (ev) => save({ timezone: ev.target.value.trim() || 'UTC' }) }), 'e.g. UTC, Europe/London, America/New_York')),
    toggle('Reuse recent results (cache)', 'a cached answer costs one request and no provider credit', p.cache.enabled, (v) => ({ cache: { enabled: v } })),
    field('Cache results for (minutes)', h('input', { type: 'number', min: '0', value: Math.round(p.cache.ttl_seconds / 60), onchange: (ev) => save({ cache: { ttl_seconds: Number(ev.target.value) * 60 } }), style: 'max-width:160px' }))));

  const units = ['task_calls', 'search_calls', 'fetched_pages', 'browser_ms'];
  const limitFor = (u, period) => s.global_limits.find((l) => l.denomination === u && l.period === period);
  const limitInput = (u, period) => {
    const l = limitFor(u, period);
    const toDisplay = (n) => (u === 'browser_ms' ? Math.round(n / 60000) : n);
    return h('input', { type: 'number', min: '0', value: l ? toDisplay(l.amount) : '', placeholder: 'no limit', style: 'max-width:150px',
      onchange: async (ev) => {
        const raw = ev.target.value.trim();
        const amount = raw === '' ? null : (u === 'browser_ms' ? Number(raw) * 60000 : Number(raw));
        await act(() => api('PUT', '/api/limits', { unit: u, period, amount }), 'Limit saved');
        route();
      } });
  };
  root.append(h('div', { class: 'card' }, h('h2', null, 'Global limits'), h('p', { class: 'small muted' }, 'Apply to every agent together (and to the dashboard). Empty means no limit. Each agent can have its own limits too, on the Agents screen; each provider key has its own credit, on the Providers screen.'),
    h('table', null, h('thead', null, h('tr', null, h('th', null, 'What'), h('th', null, 'Per month'), h('th', null, 'Per day'), h('th', null, 'Used this month'))),
      h('tbody', null, units.map((u) => {
        const m = limitFor(u, 'monthly');
        return h('tr', null, h('td', null, u === 'browser_ms' ? 'Browser time (minutes)' : UNIT_WORDS[u][0].toUpperCase() + UNIT_WORDS[u].slice(1)),
          h('td', null, limitInput(u, 'monthly')), h('td', null, limitInput(u, 'daily')),
          h('td', { class: 'small' }, m ? fmtUnits(u, m.used + m.held) : '—'));
      })))));

  root.append(h('div', { class: 'card' }, h('h2', null, 'This installation'), h('dl', { class: 'kv small' },
    h('dt', null, 'Data folder'), h('dd', { class: 'mono' }, s.data_dir),
    h('dt', null, 'Listening on'), h('dd', { class: 'mono' }, s.listen),
    h('dt', null, 'Sign-in'), h('dd', null, 'None. Anyone who can open this page can change it, so keep it on this computer (the default) or behind your own login.'),
    h('dt', null, 'Backup'), h('dd', null, 'Stop AgentWebView and copy the data folder. secret.key decrypts your provider keys: keep it with the database.'))));
  return root;
}

// ---------------------------------------------------------------- router

const ROUTES = { home: Home, providers: Providers, agents: Agents, knowledge: Knowledge, approvals: Approvals, activity: Activity, settings: Settings, task: Task, session: Session };
const main = document.getElementById('main');

async function route() {
  const path = location.hash.replace(/^#\/?/, '').split('?')[0];
  const [name, ...rest] = path.split('/');
  const view = ROUTES[name] || Home;
  const navName = { task: 'activity', session: 'approvals' }[name] || (ROUTES[name] ? name : 'home');
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.route === navName));
  try {
    const node = await view(...rest.map(decodeURIComponent));
    main.replaceChildren(node);
  } catch (e) {
    main.replaceChildren(pageHead('Something went wrong'), errorBox(e));
  }
}

async function pollApprovals() {
  try {
    const { pending } = await api('GET', '/api/approvals');
    const pill = document.getElementById('approval-count');
    pill.textContent = String(pending.length);
    pill.classList.toggle('hidden', pending.length === 0);
  } catch (_) { /* server restarting */ }
}

window.addEventListener('hashchange', route);
connectInfo().then((c) => { document.getElementById('version').textContent = 'AgentWebView ' + c.version; }).catch(() => {});
route();
pollApprovals();
setInterval(pollApprovals, 5000);
