/* ─────────────────────────────────────────────────────────────────────────
   Corpus Intel — app.js
   Phase 0: navigation skeleton
   Phase 1: Import (upload, source detection, mapping editor, AI fallback)
           Settings (API-key persistence to process memory)
   ───────────────────────────────────────────────────────────────────────── */

/* ── 0. HELPERS ─────────────────────────────────────────────────────────── */
const $  = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

function escapeHtml(str) {
    return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatInt(n) {
    if (n === null || n === undefined) return '—';
    return Number(n).toLocaleString('en-US');
}

async function fetchJson(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) {
        let detail = res.statusText;
        try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
        throw new Error(`${res.status} ${detail}`);
    }
    return res.json();
}

/**
 * Track a background job by SSE. Calls `onProgress(pct, msg)` on each update.
 * Resolves with the final job object (status: done|error|cancelled).
 */
// Registry of live EventSources so navigation / session reset can close them.
// Each entry is tagged with the page it was opened on; we only close when the
// user navigates AWAY from that page (so an SSE bound to a background job
// isn't killed mid-stream by an incidental nav).
const LiveStreams = new Set();

function registerStream(es) {
    if (!es) return es;
    LiveStreams.add(es);
    const _close = es.close.bind(es);
    es.close = () => { LiveStreams.delete(es); _close(); };
    return es;
}

function closeAllStreams() {
    for (const es of [...LiveStreams]) {
        try { es.close(); } catch (_) {}
        LiveStreams.delete(es);
    }
}

function trackJob(jobId, onProgress) {
    return new Promise((resolve) => {
        const es = registerStream(new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`));
        let lastPayload = null;
        es.onmessage = (ev) => {
            try {
                const j = JSON.parse(ev.data);
                lastPayload = j;
                if (onProgress) onProgress(j.progress_pct || 0, j.message || '');
                updateJobsIndicator(j);
                if (j.status === 'done' || j.status === 'error' || j.status === 'cancelled') {
                    es.close();
                    fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`).then(resolve).catch(() => resolve(j));
                }
            } catch (_) {}
        };
        es.onerror = () => {
            // Network hiccup — fall back to polling.
            es.close();
            const poll = async () => {
                try {
                    const j = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`);
                    updateJobsIndicator(j);
                    if (onProgress) onProgress(j.progress_pct || 0, j.message || '');
                    if (j.status === 'done' || j.status === 'error' || j.status === 'cancelled') {
                        resolve(j);
                    } else {
                        setTimeout(poll, 1500);
                    }
                } catch (e) {
                    resolve(lastPayload || { status: 'error', error: String(e) });
                }
            };
            poll();
        };
    });
}

function updateJobsIndicator(job) {
    const bar = $('#jobs-indicator');
    if (!bar) return;
    if (!job) { bar.hidden = true; return; }
    bar.hidden = false;
    $('#jobs-indicator-label').textContent = `${job.kind}: ${job.message || job.status}`;
    $('#jobs-indicator-fill').style.width = `${Math.max(0, Math.min(100, job.progress_pct || 0))}%`;
    const cancelBtn = $('#jobs-indicator-cancel');
    if (cancelBtn) {
        cancelBtn.dataset.jobId = job.id;
        cancelBtn.style.display = (job.status === 'running' || job.status === 'queued') ? 'inline-flex' : 'none';
    }
    if (job.status === 'done' || job.status === 'error' || job.status === 'cancelled') {
        setTimeout(() => { const b = $('#jobs-indicator'); if (b) b.hidden = true; }, 2000);
    }
}

async function cancelCurrentJob() {
    const btn = $('#jobs-indicator-cancel');
    const id = btn?.dataset?.jobId;
    if (!id) return;
    try { await fetchJson(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }); } catch (_) {}
}

/* ── Decision cards + journey log (P15) ─────────────────────────────────── */
async function showDecisionCard(actionId) {
    let card;
    try {
        card = await fetchJson(`/api/didactics/decision_card/${encodeURIComponent(actionId)}`);
    } catch (e) {
        return true;  // fail-open if no card exists for this action
    }
    return new Promise(resolve => {
        const modal = $('#decision-modal');
        if (!modal) { resolve(true); return; }
        $('#decision-modal-title').textContent = card.title || 'Decision';
        $('#decision-modal-why').textContent = card.why || '';
        const tradeoffs = $('#decision-modal-tradeoffs');
        tradeoffs.innerHTML = (card.tradeoffs || []).map(t => `<li>${escapeHtml(t)}</li>`).join('');
        $('#decision-modal-meta').innerHTML =
            `<span class="tag ${card.reversible ? 'ok' : 'warn'}"><i class="fa-solid fa-${card.reversible ? 'rotate-left' : 'triangle-exclamation'}"></i> ${card.reversible ? 'Reversible' : 'Not easily reversible'}</span>` +
            `<span class="tag muted"><i class="fa-solid fa-dollar-sign"></i> ${escapeHtml(card.cost || '—')}</span>`;
        modal.hidden = false;
        document.body.classList.add('modal-open');
        const cleanup = (result) => {
            modal.hidden = true;
            document.body.classList.remove('modal-open');
            modal.removeEventListener('click', onClick);
            document.removeEventListener('keydown', onKey);
            resolve(result);
        };
        const onClick = (ev) => {
            if (ev.target?.dataset?.close === '1') cleanup(false);
            else if (ev.target?.id === 'decision-modal-ok') cleanup(true);
        };
        const onKey = (ev) => {
            if (ev.key === 'Escape') cleanup(false);
            else if (ev.key === 'Enter') cleanup(true);
        };
        modal.addEventListener('click', onClick);
        document.addEventListener('keydown', onKey);
        $('#decision-modal-ok').focus();
    });
}

/* ── AI health + budget envelope pills ──────────────────────────────────── */
const StatusDock = { aiTimer: null, budgetTimer: null, budget: null, aiHealth: null };

async function refreshAiHealth() {
    const pill = $('#ai-health-pill');
    if (!pill) return;
    try {
        const h = await fetchJson('/api/ai/health');
        StatusDock.aiHealth = h;
        pill.hidden = false;
        const reachable = !!h.reachable;
        const hasKey = h.has_api_key !== undefined ? !!h.has_api_key : reachable;
        const ready = h.ready !== undefined ? !!h.ready : (reachable && hasKey);
        pill.dataset.state = ready ? 'ok' : (reachable ? 'warn' : 'bad');
        const label = !reachable ? (h.reason || 'unreachable')
                    : !hasKey ? 'no API key'
                    : 'ready';
        $('#ai-health-text').textContent = label;
        const when = h.checked_at ? new Date(h.checked_at*1000).toLocaleTimeString() : 'now';
        pill.title = !reachable
            ? `Claude API unreachable — ${h.last_error || h.reason || 'no details'}. Click to re-check.`
            : !hasKey
                ? 'Claude API reachable but no API key set. Click to open Settings and add one.'
                : `Claude API ready · checked ${when}. Click to re-check now.`;
    } catch (e) {
        StatusDock.aiHealth = null;
        pill.hidden = false;
        pill.dataset.state = 'bad';
        $('#ai-health-text').textContent = 'offline';
        pill.title = 'Cannot reach the local server for AI status. Click to retry.';
    }
}

async function refreshBudget() {
    const pill = $('#budget-pill');
    if (!pill) return;
    try {
        const b = await fetchJson('/api/settings/budget');
        StatusDock.budget = b;
        const cap = Number(b.budget_usd || 0);
        const spent = Number(b.spent_usd || 0);
        if (cap <= 0) {
            pill.hidden = false;
            pill.dataset.state = 'off';
            $('#budget-pill-value').textContent = `$${spent.toFixed(2)}`;
            $('#budget-pill-fill').style.width = '0%';
            pill.title = `No monthly cap set · spent this month: $${spent.toFixed(2)}. Click to set in Settings.`;
            return;
        }
        const pct = Math.min(100, (spent / cap) * 100);
        pill.hidden = false;
        pill.dataset.state = pct >= 100 ? 'bad' : (pct >= 80 ? 'warn' : 'ok');
        $('#budget-pill-value').textContent = `$${spent.toFixed(2)} / $${cap.toFixed(0)}`;
        $('#budget-pill-fill').style.width = `${pct}%`;
        pill.title = `Month ${b.month || ''}: $${spent.toFixed(2)} of $${cap.toFixed(2)} (${pct.toFixed(0)}%). Click for Settings.`;
    } catch (e) {
        pill.hidden = true;
    }
}

function goToSettings() {
    const nav = [...document.querySelectorAll('nav a')].find(a => a.textContent.trim() === 'Settings');
    nav?.click();
}

function setupStatusDock() {
    const ai = $('#ai-health-pill');
    const bp = $('#budget-pill');
    if (ai) ai.addEventListener('click', () => {
        const h = StatusDock.aiHealth;
        const hasKey = h ? (h.has_api_key !== undefined ? !!h.has_api_key : !!h.reachable) : false;
        if (h && h.reachable && !hasKey) { goToSettings(); return; }
        refreshAiHealth();
    });
    if (bp) bp.addEventListener('click', goToSettings);
    refreshAiHealth();
    refreshBudget();
    // refresh AI health every 60s, budget every 45s
    StatusDock.aiTimer = setInterval(refreshAiHealth, 60_000);
    StatusDock.budgetTimer = setInterval(refreshBudget, 45_000);
}

async function logJourney(action, params, notes = '') {
    try {
        await fetchJson('/api/didactics/journey', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action, params: params || {}, notes }),
        });
    } catch (_) {}
}

/* ── Command palette (P14) ──────────────────────────────────────────────── */
const CommandPalette = { items: [], active: 0, open: false, loaded: false };

async function setupCommandPalette() {
    document.addEventListener('keydown', (ev) => {
        const isPaletteKey = (ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'k';
        if (isPaletteKey) {
            ev.preventDefault();
            togglePalette(true);
            return;
        }
        if (CommandPalette.open) {
            if (ev.key === 'Escape') { togglePalette(false); }
            else if (ev.key === 'ArrowDown') { ev.preventDefault(); movePalette(+1); }
            else if (ev.key === 'ArrowUp')   { ev.preventDefault(); movePalette(-1); }
            else if (ev.key === 'Enter')     { ev.preventDefault(); runPaletteActive(); }
        }
    });
    $('#command-palette-input')?.addEventListener('input', (ev) => renderPalette(ev.target.value));
    document.querySelector('#command-palette .command-palette-mask')?.addEventListener('click', () => togglePalette(false));
}

async function togglePalette(open) {
    const el = $('#command-palette');
    if (!el) return;
    if (open) {
        if (!CommandPalette.loaded) {
            try {
                const data = await fetchJson('/api/commands');
                CommandPalette.items = data.items || [];
                CommandPalette.loaded = true;
            } catch (_) { CommandPalette.items = []; }
        }
        CommandPalette.open = true;
        el.hidden = false;
        const input = $('#command-palette-input');
        if (input) { input.value = ''; input.focus(); }
        renderPalette('');
    } else {
        CommandPalette.open = false;
        el.hidden = true;
    }
}

function renderPalette(query) {
    const list = $('#command-palette-list');
    if (!list) return;
    const q = (query || '').toLowerCase().trim();
    const filtered = CommandPalette.items.filter(it => {
        if (!q) return true;
        return it.label.toLowerCase().includes(q) || it.id.toLowerCase().includes(q);
    });
    CommandPalette.active = 0;
    list.innerHTML = filtered.map((it, i) => `
        <li data-id="${escapeHtml(it.id)}" class="${i === 0 ? 'active' : ''}">
            <span class="cmd-kind">${escapeHtml(it.kind)}</span>
            <span>${escapeHtml(it.label)}</span>
        </li>`).join('');
    list.querySelectorAll('li').forEach((li, i) => {
        li.addEventListener('mouseenter', () => setPaletteActive(i));
        li.addEventListener('click', () => { setPaletteActive(i); runPaletteActive(); });
    });
    CommandPalette._filtered = filtered;
}

function setPaletteActive(i) {
    const list = $('#command-palette-list');
    if (!list) return;
    CommandPalette.active = i;
    list.querySelectorAll('li').forEach((li, j) => li.classList.toggle('active', j === i));
}

function movePalette(delta) {
    const list = $('#command-palette-list');
    if (!list) return;
    const items = list.querySelectorAll('li');
    if (!items.length) return;
    let next = CommandPalette.active + delta;
    if (next < 0) next = items.length - 1;
    if (next >= items.length) next = 0;
    setPaletteActive(next);
    items[next].scrollIntoView({ block: 'nearest' });
}

async function runPaletteActive() {
    const items = CommandPalette._filtered || [];
    const cmd = items[CommandPalette.active];
    if (!cmd) return;
    togglePalette(false);
    if (cmd.kind === 'navigate' && typeof navigateTo === 'function') {
        navigateTo(cmd.page);
        return;
    }
    if (cmd.kind === 'action') {
        switch (cmd.id) {
            case 'action.rebuild_corpus': navigateTo?.('corpus'); $('#btn-rebuild-corpus')?.click(); break;
            case 'action.undo': undoLast?.(); break;
            case 'action.export_report': window.open('/api/report/generate', '_blank'); break;
            case 'action.demo_csv': window.open('/api/onboarding/demo_csv', '_blank'); break;
            default: console.warn('No handler for command', cmd.id);
        }
    }
}

// Word-wrap a label for Chart.js tick rendering (used in Analytics, Phase 7).
function wrapLabel(text, maxChars = 32, maxLines = 3) {
    if (!text) return '';
    const words = String(text).split(/\s+/);
    const lines = [];
    let cur = '';
    for (const w of words) {
        const next = cur ? cur + ' ' + w : w;
        if (next.length <= maxChars) { cur = next; continue; }
        if (cur) lines.push(cur);
        if (lines.length >= maxLines) { cur = ''; break; }
        cur = w.length > maxChars ? w.slice(0, maxChars - 1) + '…' : w;
    }
    if (cur && lines.length < maxLines) lines.push(cur);
    const rendered = lines.join(' ');
    if (rendered.length < text.length) {
        const last = lines[lines.length - 1] || '';
        lines[lines.length - 1] = (last.length >= maxChars ? last.slice(0, maxChars - 1) : last) + '…';
    }
    return lines.length === 1 ? lines[0] : lines;
}

/* ── 1. STATE ───────────────────────────────────────────────────────────── */
const App = {
    state: null,
    currentPage: 'home',
    activeDatasetId: null,
    activeDataset: null,   // {meta, suggestions, preview}
    corpus: {
        built: false,
        rows: 0,
        facets: null,
        stats: null,
        builtAt: '',
        preview: [],
        previewColumns: [],
        totalFiltered: 0,
        page: 1,
        pageSize: 50,
        pickerSelection: null,     // Set<dataset_id> while picker is open
        pickerDedupeNear: true,
        filter: emptyFilter(),
        sortBy: '',
        sortDesc: true,
        loadedOnce: false,
        filterDebounce: null,
    },
    slicer: {
        query: '',
        lastRunQuery: '',          // the query that produced the current preview
        matched: 0,
        totalInCorpus: 0,
        preview: [],
        previewColumns: [],
        page: 1,
        pageSize: 25,
        running: false,
        errorMessage: '',
        diffA: '',
        diffB: '',
        diff: null,
        sample: {
            method: 'random',
            n: 200,
            seed: 42,
            byCol: '',
            ascending: false,
            step: 10,
            splitK: 2,
            splitOverlap: 0,
            baseQuery: '',
            dedupe: false,
            lastSpec: null,         // last spec that produced a preview
            rowsSampled: 0,
            rowsInCorpus: 0,
            description: '',
            preview: [],
            previewColumns: [],
            page: 1,
            pageSize: 25,
            categoricalCols: [],
            numericCols: [],
            columnsLoaded: false,
            splitLastResult: null,  // last /split response
        },
    },
    codebook: {
        focusRowIdx: null,
        focusRowData: null,
        scopeSliceId: '',
        scopeRows: [],
        scopePtr: 0,
        progress: null,
        rowTags: [],
        irr: null,
        bulkMode: 'query',
        keysBound: false,
    },
    ai: {
        preflight: null,           // last preflight result
        running: false,
        aborter: null,             // AbortController for the current SSE fetch
        log: [],                   // {type, ...} events for the run log
        progress: 0,               // 0–100 for the progress bar
        suggest: null,             // last suggest-codebook proposal + meta
        suggestLoading: false,
        cache: null,               // cache stats
        reviewRows: [],            // [{row_idx, row, tags}]
        reviewLoading: false,
        reviewLowConfOnly: false,
    },
    topics: {
        running: false,
        mode: '',                  // 'induce' | 'classify' | ''
        aborter: null,
        log: [],
        progress: 0,
        progressLabel: '',
        preflight: null,
        selected: new Set(),       // topic_ids selected for merge
        exampleRows: {},           // {topic_id: [{row_idx, text, ...}]}
    },
};

function emptyFilter() {
    return {
        text: '',
        regex: false,
        case_sensitive: false,
        platforms: [],
        languages: [],
        source_datasets: [],
        source_ids: [],
        countries: [],
        date_from: null,
        date_to: null,
        engagement: {},
    };
}

// Friendly column labels for Corpus preview + row modal.
const CORPUS_COL_LABELS = {
    _row_idx:       '#',
    created_at:     'Published',
    platform:       'Platform',
    source_dataset: 'Source file',
    source_id:      'Source type',
    author_handle:  'Author',
    author_name:    'Author name',
    author_id:      'Author ID',
    text:           'Text',
    language:       'Lang',
    country:        'Country',
    region:         'Region',
    like_count:     'Likes',
    share_count:    'Shares',
    comment_count:  'Comments',
    view_count:     'Views',
    url:            'Link',
    post_id:        'Post ID',
    source_type:    'Type',
    in_reply_to:    'Reply to',
    parent_id:      'Thread parent',
    media_urls:     'Media',
    hashtags:       'Hashtags',
    mentions:       'Mentions',
    sentiment_source: 'Sentiment (source)',
};
function corpusColLabel(name) { return CORPUS_COL_LABELS[name] || name; }
const CORPUS_NUM_COLS = new Set(['like_count', 'share_count', 'comment_count', 'view_count', '_row_idx']);
const CORPUS_DATE_COLS = new Set(['created_at']);
const CORPUS_LINK_COLS = new Set(['url']);

// Human-friendly names for the standard columns (canonical schema).
const FIELD_LABELS = {
    post_id:          'Post ID (unique row)',
    platform:         'Platform',
    source_type:      'Type (post / comment / reply)',
    author_id:        'Author — internal ID',
    author_handle:    'Author — @handle',
    author_name:      'Author — display name',
    text:             'Text content',
    language:         'Language',
    created_at:       'Published at',
    url:              'Link',
    like_count:       'Likes',
    share_count:      'Shares / retweets',
    comment_count:    'Comments / replies',
    view_count:       'Views / impressions',
    in_reply_to:      'Reply to (post ID)',
    parent_id:        'Thread parent ID',
    media_urls:       'Media URLs',
    hashtags:         'Hashtags',
    mentions:         '@-mentions',
    country:          'Country',
    region:           'Region / state',
    sentiment_source: 'Sentiment (already in file)',
};
function fieldLabel(name) { return FIELD_LABELS[name] || name; }

// How a column got its proposed match.
const METHOD_LABELS = {
    adapter:   'Source match',
    alias:     'Known alias',
    fuzzy:     'Similar name',
    ai:        'AI suggestion',
    unmatched: 'No match',
};

// Where the currently-saved mapping came from.
const MAPPING_SOURCE_LABELS = {
    auto:   'auto-detected',
    manual: 'manually edited',
    ai:     'AI-suggested',
};

// Human-friendly quality-flag text, shown both as a badge and as a tooltip.
function qualityFlagText(key, value) {
    switch (key) {
        case 'missing_text':      return 'No column is marked as the text content';
        case 'missing_post_id':   return 'No unique post ID — duplicates can\u2019t be removed automatically';
        case 'blank_text_rows':   return `${formatInt(value)} rows have blank text`;
        case 'duplicate_post_ids':return `${formatInt(value)} rows share the same post ID`;
        case 'unparseable_dates': return `${formatInt(value)} rows have dates we couldn\u2019t read`;
        default:                  return `${key}: ${value}`;
    }
}

/* ── 2. ROUTER ──────────────────────────────────────────────────────────── */
function switchBackgroundVideo(bgId) {
    if (!bgId) return;
    $$('.bg-video').forEach(v => {
        v.classList.remove('bg-video-active');
        if (v.id !== bgId) v.classList.add('bg-video-hidden');
    });
    const next = document.getElementById(bgId);
    if (next) {
        next.classList.remove('bg-video-hidden');
        next.classList.add('bg-video-active');
    }
}

function navigateTo(targetPage) {
    if (targetPage === App.currentPage) return;
    // Dispose any page-bound charts left behind and close any EventSources
    // that weren't tied to a long-running background job. This prevents the
    // "100 chart instances after 100 nav flips" memory blow-up.
    try { disposePageCharts(App.currentPage); } catch (_) {}
    App.currentPage = targetPage;

    $$('.page').forEach(p => p.classList.remove('active'));
    $$('.nav-links a').forEach(a => a.classList.toggle('active', a.dataset.page === targetPage));

    const navLink = $(`.nav-links a[data-page="${targetPage}"]`);
    if (navLink) switchBackgroundVideo(navLink.dataset.bg);

    const page = document.getElementById(`page-${targetPage}`);
    if (!page) return;
    page.classList.add('active');

    const main = $('#main-scroll');
    if (main) main.scrollTop = 0;

    const hook = PageHooks[targetPage];
    if (typeof hook === 'function') {
        try { hook(App.state); } catch (err) { console.error(`[${targetPage}] hook error`, err); }
    }
}

/* ── 3. PAGE HOOKS ──────────────────────────────────────────────────────── */
const PageHooks = {
    home:        renderHome,
    import:      renderImport,
    corpus:      renderCorpus,
    slicer:      renderSlicer,
    codebook:    renderCodebook,
    'ai-coding': renderAICoding,
    topics:      renderTopics,
    analytics:   renderAnalytics,
    export:      renderExport,
    settings:    renderSettings,
};

/* ── GLOSSARY ───────────────────────────────────────────────────────────── */
// Keys referenced by `data-hint="…"` attributes anywhere in the DOM.
// Keep each entry to 2-3 short sentences that a non-expert can scan.
const GLOSSARY = {
    'cohens-kappa': {
        term: "Cohen's κ",
        body: "A score between −1 and 1 measuring how much two coders agree beyond chance. Above 0.6 is usually considered acceptable; above 0.8 is strong. Only valid for two coders and one category at a time.",
    },
    'krippendorffs-alpha': {
        term: "Krippendorff's α",
        body: "Generalised agreement score for 2+ coders that also handles missing judgments. Same interpretation as κ: ≥ 0.667 is usable, ≥ 0.8 is strong. Pick this when more than two coders have worked on the same rows.",
    },
    'irr': {
        term: "Inter-coder reliability (IRR)",
        body: "How much your coders agree with each other on the same rows. Measured here with Cohen's κ (two coders) or Krippendorff's α (more than two). Overlap is created by splitting the corpus into chunks with a small shared slice.",
    },
    'near-duplicate': {
        term: "Near-duplicate",
        body: "Two rows whose normalised text is byte-identical after lowercasing, trimming and collapsing whitespace. We hash normalised text and drop repeats. Exact post-ID duplicates are caught by a separate pass first.",
    },
    'confidence': {
        term: "AI confidence",
        body: "How certain Haiku is about the category it picked for a row (0 = no idea, 1 = sure). The review table surfaces the lowest-confidence rows first — those are the ones to spot-check by hand.",
    },
    'prompt-caching': {
        term: "Prompt caching",
        body: "Anthropic caches the fixed part of each prompt (system instructions + codebook) for 5 minutes. Subsequent rows reuse the cache at 10% of the normal input cost. We cache automatically on every batch.",
    },
    'preflight': {
        term: "Preflight estimate",
        body: "Before a paid API run, we estimate rows × tokens × price and show you the number. Nothing hits the Anthropic API until you confirm the run.",
    },
    'target-k': {
        term: "Target topics (k)",
        body: "Roughly how many topics you want Sonnet to induce from the sample. This is a hint, not a hard ceiling — the model may return a few more or fewer if that's a better fit.",
    },
    'batch-size': {
        term: "Batch size",
        body: "How many rows go into one Haiku call. Smaller = more latency but finer progress; larger = fewer calls but each one is heavier. Default 20 works well for most cases.",
    },
    'sample-size': {
        term: "Sample size",
        body: "How many rows to run through the AI in sample mode. 200 is enough to spot-check a codebook or induce reasonable topics without burning budget on the full corpus.",
    },
    'base-query': {
        term: "Base filter query",
        body: "Apply a boolean query before sampling. Useful for drawing a sample from only rows that match, e.g. sample 100 rows from posts matching `hate speech OR harassment`.",
    },
    'stratified': {
        term: "Stratified sample",
        body: "Sample N rows, keeping the same distribution of a chosen column as the source. Use to preserve language, platform, or country balance when hand-coding a sample.",
    },
    'systematic': {
        term: "Systematic sample (every Nth)",
        body: "Pick every Nth row after an optional sort. Good for time-ordered data when you want evenly spaced coverage rather than random clumping.",
    },
    'irr-overlap': {
        term: "IRR overlap",
        body: "When splitting a corpus into K chunks for multiple coders, `overlap` sets the fraction of rows that appear in ALL chunks. That shared slice is how you'll later measure inter-coder reliability.",
    },
    'normalisation': {
        term: "Cross-tab normalisation",
        body: "`none` shows raw counts. `row` turns each row into percentages that sum to 100%. `column` does the same per column. `all` divides every cell by the grand total.",
    },
    'ngram': {
        term: "n-gram",
        body: "An ordered run of n words (1 = single words, 2 = pairs, 3 = triples). Larger n captures more context but needs more data to avoid noise. The chart shows top-K grams by frequency.",
    },
    'stopwords': {
        term: "Stopwords",
        body: "Common, low-information words (the, and, it, https …) filtered out of n-gram counts. Add domain-specific terms in the *Extra stopwords* field — e.g., your brand name if it dominates every row.",
    },
    'boolean-query': {
        term: "Boolean query",
        body: "Brandwatch-style syntax: `AND`, `OR`, `NOT`, parentheses, quoted phrases, `word*` wildcards. Unquoted neighbours act as AND. Example: `(\"hate speech\" OR harass*) AND NOT satire`.",
    },
    'source-confidence': {
        term: "Source detection confidence",
        body: "How sure the importer is that a file came from a given platform export format. Above 0.85 auto-applies the mapping; lower values surface a picker so you can confirm or change the match.",
    },
    'quality-flags': {
        term: "Quality flags",
        body: "Sanity checks run during import: missing text, missing post IDs, blank rows, duplicate post IDs, unparseable dates. A flag doesn't block import — it tells you what to clean up before merging.",
    },
    'exact-id-dupes': {
        term: "Exact ID duplicates",
        body: "Rows dropped because another row had the same post_id. Literal repeats of the same post — usually from overlapping exports or a re-pull, not near-duplicates. Only runs when a post_id column was mapped.",
    },
    'near-dupes-removed': {
        term: "Near-duplicates removed",
        body: "Rows dropped because their normalised text (lowercased, whitespace-collapsed, URLs stripped) matched another row. Catches copy-paste reposts, cross-posts and template messages. Turn this off if you're studying virality itself.",
    },
    'rows-total': {
        term: "Final rows",
        body: "How many rows survived deduplication and made it into the working corpus. Every downstream % and chart uses this as its denominator. Changing the active snapshot changes this number.",
    },
    'rows-in': {
        term: "Rows in (before dedup)",
        body: "Sum of rows across all source datasets before any duplicate removal. The gap to Final rows tells you how much the dedup step pruned.",
    },
    'regex-search': {
        term: "Regex search",
        body: "Treat the search box as a regular expression instead of a literal substring. Examples: `climate|weather` matches either word · `\\bhate\\s+speech\\b` matches the exact phrase · `(?i)MAGA` is case-insensitive. Uncheck for a plain find.",
    },
    'match-case': {
        term: "Match case",
        body: "When off (default), the search ignores upper/lower case — `climate` matches `Climate` and `CLIMATE`. Turn on when case distinguishes meaning (brand names, code tokens).",
    },
    'prompt-hint': {
        term: "Prompt hint (AI description)",
        body: "The short sentence the AI reads when it decides whether a row belongs in this category. Write it as an instruction to a careful research assistant — inclusion rules, edge cases, what to exclude. Example: \"Use when the post targets people for their religion, race or gender; do NOT use for criticism of ideas or institutions.\"",
    },
    'exclusion-group': {
        term: "Exclusion group",
        body: "Two categories in the same group cannot co-exist on one row for one coder — the newer tag replaces the older one. Use when categories are mutually exclusive (pro / anti / neutral). Leave blank if a row can carry the category alongside others.",
    },
    'coder-identity': {
        term: "Coder identity",
        body: "The display name attached to every tag you place. Required before tagging — every IRR calculation and provenance entry is keyed on it. Pick one short, stable identifier and keep it for the whole project.",
    },
    'ai-pill': {
        term: "AI status pill",
        body: "Header indicator of Claude reachability. Green = reachable and key on file. Yellow = reachable but no key — click to open Settings. Red = unreachable. Click to re-check now.",
    },
    'budget-pill': {
        term: "Budget status pill",
        body: "Header indicator of AI spend this month. Green = under 80 % of your cap. Yellow = 80–100 %. Red = cap hit (runs blocked unless you type the remaining amount). Click to change the cap in Settings.",
    },
    'language-stopwords': {
        term: "Stopword language",
        body: "Which language's common-words list to filter out. Pick the language that dominates your corpus — an English list won't help with German or Italian text. Set to Auto to use the combined EN+ES+PT default.",
    },
    'memo-pad': {
        term: "Row memos",
        body: "Free-text notes tied to one row. Use them for inductive observations — 'this is a recurring euphemism', 'this user posts the same template' — that don't fit a categorical tag. Memos are private to the coder who wrote them and never affect inter-coder agreement scores.",
    },
};

/* Setup the single shared popover and delegate hover/click handling. */
function setupGlossary() {
    const pop = $('#glossary-popover');
    if (!pop) return;
    const termEl = $('#glossary-term');
    const bodyEl = $('#glossary-body');
    let pinned = false;
    let lastTrigger = null;

    function positionPopover(anchor) {
        const r = anchor.getBoundingClientRect();
        // Place popover below the trigger; flip above if there isn't room.
        pop.style.visibility = 'hidden';
        pop.hidden = false;
        const pw = pop.offsetWidth;
        const ph = pop.offsetHeight;
        let top = r.bottom + 8;
        let left = r.left - 8;
        if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
        if (left < 8) left = 8;
        if (top + ph > window.innerHeight - 8) top = r.top - ph - 8;
        pop.style.top = `${top}px`;
        pop.style.left = `${left}px`;
        pop.style.visibility = '';
    }

    function show(trigger) {
        const key = trigger?.dataset?.hint;
        if (!key) return;
        const entry = GLOSSARY[key];
        if (!entry) return;
        termEl.textContent = entry.term;
        bodyEl.textContent = entry.body;
        positionPopover(trigger);
        lastTrigger = trigger;
    }

    function hide(force) {
        if (pinned && !force) return;
        pop.hidden = true;
        pinned = false;
        lastTrigger = null;
    }

    document.addEventListener('mouseover', (e) => {
        const t = e.target.closest('[data-hint]');
        if (!t) return;
        if (pinned) return;
        show(t);
    });
    document.addEventListener('mouseout', (e) => {
        const t = e.target.closest('[data-hint]');
        if (!t) return;
        // Only hide when the popover itself isn't being hovered.
        setTimeout(() => {
            if (pinned) return;
            if (pop.matches(':hover')) return;
            hide(false);
        }, 80);
    });
    document.addEventListener('click', (e) => {
        const t = e.target.closest('[data-hint]');
        if (t) {
            e.preventDefault();
            if (pinned && lastTrigger === t) {
                hide(true);
            } else {
                show(t);
                pinned = true;
            }
            return;
        }
        // Clicks outside dismiss a pinned popover.
        if (pinned && !pop.contains(e.target)) hide(true);
    });
    $('#glossary-dismiss')?.addEventListener('click', (e) => {
        e.preventDefault(); e.stopPropagation();
        hide(true);
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(true); });
}

/* Small helper to sprinkle hint triggers into static HTML after load.
   Used for headings that benefit from an ⓘ without cluttering index.html. */
function hintIcon(key, label) {
    return `<i class="hint-trigger" data-hint="${key}" tabindex="0" aria-label="${label || 'Definition'}" title="What's this?">i</i>`;
}

// Next-step target page per state — used by the "Take me there" button.
let _homeNextTarget = 'import';

function renderHome(state) {
    const line = $('#home-session-line');
    const tag  = $('#home-step-tag');
    const body = $('#home-step-body');
    if (!line || !tag || !body) return;
    if (!state || !state.ready) {
        tag.textContent = 'Getting started';
        body.textContent = 'Loading\u2026';
        line.textContent = '';
        return;
    }
    const datasets = Object.values(state.datasets || {});
    const mapped = datasets.filter(d => Object.keys(d.mapping || {}).length > 0);
    const corpus = state.corpus || {};
    const goal = (state.settings?.current_goal || '').trim();

    // Highlight active goal card.
    document.querySelectorAll('#home-goal-grid .goal-card').forEach(el => {
        el.classList.toggle('active', el.dataset.goal === goal);
    });

    // Goal-specific next-step copy.
    let nextTarget = 'import';
    let stepTag = 'Step 1 of 2';
    let stepHtml = 'Head to <strong>Import</strong> to upload your first CSV or XLSX.';
    let lineText = '';

    if (datasets.length === 0) {
        stepTag = 'Start here';
        stepHtml = 'Head to <strong>Import</strong> to upload your first CSV or XLSX.';
        nextTarget = 'import';
    } else if (mapped.length === 0) {
        stepTag = 'Step 1 of 2';
        stepHtml = `You have ${datasets.length} dataset${datasets.length === 1 ? '' : 's'} loaded. Open <strong>Import</strong>, click one, and save its column mapping.`;
        nextTarget = 'import';
    } else if (!corpus.built) {
        stepTag = 'Step 2 of 2';
        stepHtml = `${mapped.length} dataset${mapped.length === 1 ? ' is' : 's are'} mapped and ready. Go to <strong>Corpus</strong> to combine them.`;
        nextTarget = 'corpus';
    } else {
        // Corpus is built — branch on goal.
        stepTag = 'Ready';
        const sliceCount = Object.keys(state.slices || {}).length;
        const cbCount = Object.keys(state.codebooks || {}).length;
        const tsCount = Object.keys(state.topic_sets || {}).length;
        if (goal === 'code') {
            if (cbCount === 0) {
                stepHtml = `Your ${formatInt(corpus.rows)}-row corpus is ready. Head to <strong>Codebook</strong> to create or import the categories you'll tag with.`;
                nextTarget = 'codebook';
            } else {
                stepHtml = `Your corpus + codebook are ready. Open <strong>Codebook</strong> to tag by hand, or <strong>AI Coding</strong> to preflight an automated run.`;
                nextTarget = 'codebook';
            }
        } else if (goal === 'explore') {
            if (tsCount === 0) {
                stepHtml = `Your ${formatInt(corpus.rows)}-row corpus is ready. Head to <strong>Topics</strong> to induce themes, or <strong>Analytics</strong> for charts and time-series.`;
                nextTarget = 'topics';
            } else {
                stepHtml = `You have ${tsCount} topic run${tsCount === 1 ? '' : 's'}. Open <strong>Analytics</strong> to chart topic trends, or <strong>Topics</strong> to run another induction.`;
                nextTarget = 'analytics';
            }
        } else {
            // "build" goal or none chosen
            if (sliceCount === 0) {
                stepHtml = `Your corpus has <strong>${formatInt(corpus.rows)}</strong> rows. Open <strong>Corpus</strong> to filter, or <strong>Slicer</strong> to carve out named subsets with boolean queries.`;
                nextTarget = 'slicer';
            } else {
                stepHtml = `Your corpus has <strong>${formatInt(corpus.rows)}</strong> rows and <strong>${sliceCount}</strong> saved slice${sliceCount === 1 ? '' : 's'}. Open <strong>Slicer</strong> to refine or compare, or <strong>Export</strong> to package up your results.`;
                nextTarget = 'slicer';
            }
        }
        lineText = `All sections are unlocked — switch goals above any time to change the suggested next step.`;
    }

    tag.textContent = stepTag;
    body.innerHTML = stepHtml;
    line.textContent = lineText;
    _homeNextTarget = nextTarget;
    const cta = $('#home-step-cta');
    if (cta) cta.hidden = false;

    renderResumeBanner(state);
    renderTourCTA(state);
}

function renderTourCTA(state) {
    const cta = $('#home-tour-cta');
    if (!cta) return;
    const dismissed = localStorage.getItem('ci_tour_dismissed') === '1';
    const datasets = Object.values(state?.datasets || {});
    const corpus = state?.corpus || {};
    // Keep the CTA visible until the user has built a corpus OR explicitly dismissed it.
    const beyondStart = !!corpus.built || datasets.length >= 2;
    cta.style.display = (dismissed || beyondStart) ? 'none' : 'flex';
}

const Tour = { steps: [], idx: 0, id: null };

async function startTour(tourId = 'first_time') {
    try {
        const data = await fetchJson(`/api/didactics/tour/${encodeURIComponent(tourId)}`);
        Tour.steps = Array.isArray(data.steps) ? data.steps : [];
        Tour.id = tourId;
        Tour.idx = 0;
        if (!Tour.steps.length) return;
        openTourModal();
        showTourStep();
        logJourney('tour_started', { tour_id: tourId, steps: Tour.steps.length });
    } catch (err) {
        alert(`Could not start tour: ${err.message}`);
    }
}

function openTourModal() {
    const m = $('#tour-modal');
    if (m) m.hidden = false;
}

function closeTourModal() {
    const m = $('#tour-modal');
    if (m) m.hidden = true;
    Tour.steps = [];
    Tour.idx = 0;
}

function showTourStep() {
    const step = Tour.steps[Tour.idx];
    if (!step) { closeTourModal(); return; }
    $('#tour-modal-title').textContent = step.title || 'Tour';
    $('#tour-modal-body-text').textContent = step.body || '';
    $('#tour-modal-step').textContent = `Step ${Tour.idx + 1} of ${Tour.steps.length}`;
    const prev = $('#tour-prev');
    const next = $('#tour-next');
    if (prev) prev.disabled = (Tour.idx === 0);
    if (next) {
        const isLast = (Tour.idx === Tour.steps.length - 1);
        next.innerHTML = isLast ? 'Done <i class="fa-solid fa-check"></i>' : 'Next <i class="fa-solid fa-chevron-right"></i>';
    }
    // Navigate to the step's page if provided.
    if (step.page) {
        const nav = [...document.querySelectorAll('nav a')].find(a => {
            const href = (a.getAttribute('href') || '').replace('#', '');
            return href === step.page || href === `page-${step.page}`;
        });
        nav?.click();
    }
    if (step.anchor) {
        setTimeout(() => {
            const el = document.querySelector(step.anchor);
            el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 250);
    }
}

function nextTourStep() {
    if (Tour.idx >= Tour.steps.length - 1) { closeTourModal(); return; }
    Tour.idx += 1;
    showTourStep();
}

function prevTourStep() {
    if (Tour.idx <= 0) return;
    Tour.idx -= 1;
    showTourStep();
}

async function startFreshWorkspace() {
    const state = App.state || {};
    const datasets = Object.values(state.datasets || {});
    const corpus = state.corpus || {};
    const cbCount = Object.keys(state.codebooks || {}).length;
    const sliceCount = Object.keys(state.slices || {}).length;
    const tsCount = Object.keys(state.topic_sets || {}).length;
    const pieces = [];
    if (datasets.length) pieces.push(`${datasets.length} dataset${datasets.length === 1 ? '' : 's'}`);
    if (corpus.built)    pieces.push(`${formatInt(corpus.rows)}-row corpus`);
    if (cbCount)         pieces.push(`${cbCount} codebook${cbCount === 1 ? '' : 's'}`);
    if (sliceCount)      pieces.push(`${sliceCount} slice${sliceCount === 1 ? '' : 's'}`);
    if (tsCount)         pieces.push(`${tsCount} topic run${tsCount === 1 ? '' : 's'}`);
    const scope = pieces.length ? pieces.join(', ') : 'the current (empty) session';
    const typed = window.prompt(
        `This will permanently delete ${scope}, plus uploaded files, caches, and the provenance log.\n` +
        `Your API key and coder name will also be cleared.\n\n` +
        `This cannot be undone. Type RESET (uppercase) to confirm.`,
        '',
    );
    if ((typed || '').trim() !== 'RESET') return;
    try {
        // Abort any in-flight AI / topic runs, close tracked SSE streams,
        // and dispose chart instances before we rebuild. Otherwise the old
        // connections keep sending events into a dead state.
        try { App.ai.aborter?.abort(); } catch (_) {}
        try { App.topics.aborter?.abort(); } catch (_) {}
        try { closeAllStreams(); } catch (_) {}
        try {
            for (const inst of Object.values(Analytics.charts || {})) {
                if (inst) { try { inst.dispose(); } catch (_) {} }
            }
            Object.keys(Analytics.charts || {}).forEach(k => { Analytics.charts[k] = null; });
        } catch (_) {}

        await fetchJson('/api/session/reset', { method: 'POST' });
        sessionStorage.removeItem('ci_resume_dismissed');
        localStorage.removeItem('ci_tour_dismissed');

        // Reset the session-scoped App.* caches so nothing from the cleared
        // workspace leaks into the next one.
        App.activeDatasetId = null;
        App.activeDataset = null;
        Object.assign(App.corpus, {
            built: false, rows: 0, facets: null, stats: null, builtAt: '',
            preview: [], previewColumns: [], totalFiltered: 0, page: 1,
            filter: emptyFilter(), sortBy: '', sortDesc: true, loadedOnce: false,
        });
        Object.assign(App.slicer, {
            query: '', lastRunQuery: '', matched: 0, totalInCorpus: 0,
            preview: [], previewColumns: [], page: 1, running: false, errorMessage: '',
            diffA: '', diffB: '', diff: null,
        });
        if (App.slicer.sample) Object.assign(App.slicer.sample, {
            lastSpec: null, rowsSampled: 0, rowsInCorpus: 0, preview: [],
            previewColumns: [], page: 1, splitLastResult: null, columnsLoaded: false,
            categoricalCols: [], numericCols: [],
        });
        Object.assign(App.codebook, {
            focusRowIdx: null, focusRowData: null, scopeSliceId: '',
            scopeRows: [], scopePtr: 0, progress: null, rowTags: [], irr: null,
        });
        Object.assign(App.ai, {
            preflight: null, running: false, aborter: null, log: [],
            progress: 0, suggest: null, suggestLoading: false, cache: null,
            reviewRows: [], reviewLoading: false, reviewLowConfOnly: false,
        });
        Object.assign(App.topics, {
            running: false, mode: '', aborter: null, log: [],
            progress: 0, progressLabel: '', preflight: null,
            selected: new Set(), exampleRows: {},
        });

        await refreshState();
        navigateTo('home');
        alert('Workspace cleared. Head to Import to add new datasets.');
    } catch (err) {
        alert(`Reset failed: ${err.message}`);
    }
}

function renderResumeBanner(state) {
    const banner = $('#home-resume-banner');
    if (!banner) return;
    // Show only if the user has prior work AND hasn't dismissed this tab.
    if (sessionStorage.getItem('ci_resume_dismissed') === '1') { banner.hidden = true; return; }
    const datasets = Object.values(state.datasets || {});
    const corpus = state.corpus || {};
    const cbCount = Object.keys(state.codebooks || {}).length;
    const sliceCount = Object.keys(state.slices || {}).length;
    const tsCount = Object.keys(state.topic_sets || {}).length;
    const rowsTagged = state.coding?.rows_tagged || 0;
    const hasWork = datasets.length > 0 || corpus.built || cbCount > 0 || sliceCount > 0 || tsCount > 0;
    if (!hasWork) { banner.hidden = true; return; }

    const parts = [];
    if (datasets.length) parts.push(`${datasets.length} dataset${datasets.length === 1 ? '' : 's'}`);
    if (corpus.built)    parts.push(`${formatInt(corpus.rows)}-row corpus`);
    if (cbCount)         parts.push(`${cbCount} codebook${cbCount === 1 ? '' : 's'}`);
    if (rowsTagged)      parts.push(`${formatInt(rowsTagged)} tagged row${rowsTagged === 1 ? '' : 's'}`);
    if (sliceCount)      parts.push(`${sliceCount} slice${sliceCount === 1 ? '' : 's'}`);
    if (tsCount)         parts.push(`${tsCount} topic run${tsCount === 1 ? '' : 's'}`);
    const built = corpus.built_at ? ` · corpus built ${formatTimestamp(corpus.built_at)}` : '';
    $('#home-resume-summary').textContent = `Picking up where you left off — ${parts.join(' · ')}${built}.`;
    banner.hidden = false;
}

function formatTimestamp(iso) {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    } catch (_) { return iso; }
}

async function setGoal(goal) {
    if (!goal) return;
    try {
        await fetchJson('/api/settings/goal', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({goal}),
        });
        await refreshState();
    } catch (err) {
        console.error('[goal] save failed', err);
    }
}

/* ── 4. IMPORT ──────────────────────────────────────────────────────────── */
function renderImport(state) {
    renderDatasetList(state);
    if (App.activeDatasetId && state?.datasets?.[App.activeDatasetId]) {
        loadDataset(App.activeDatasetId);
    } else {
        hideMapping();
    }
}

function renderDatasetList(state) {
    const list = $('#dataset-list');
    const summary = $('#dataset-summary');
    if (!list || !state) return;
    const items = Object.values(state.datasets || {});
    if (items.length === 0) {
        summary.textContent = 'No datasets uploaded yet.';
        list.innerHTML = `
          <div class="empty-state">
            <i class="fa-solid fa-cloud-arrow-up"></i>
            <h4>No datasets yet</h4>
            <p>Drag a CSV or Excel export onto the upload box above, or click it to browse. Brandwatch, Twitter/X, Meta, TikTok, YouTube, Reddit, Mastodon and generic CSVs are all supported — Claude will try to auto-detect the source.</p>
            <span class="empty-hint">Tip: you can upload several files at once and merge them into one corpus in the next step.</span>
          </div>`;
        return;
    }
    summary.textContent = `${items.length} dataset${items.length === 1 ? '' : 's'} loaded.`;
    list.innerHTML = items.map(m => renderDatasetItem(m)).join('');
    $$('.dataset-item', list).forEach(el => {
        el.addEventListener('click', ev => {
            if (ev.target.closest('button')) return;
            loadDataset(el.dataset.id);
        });
    });
    $$('.dataset-item button.icon-btn.danger', list).forEach(btn => {
        btn.addEventListener('click', ev => {
            ev.stopPropagation();
            deleteDataset(btn.dataset.id);
        });
    });
}

function renderDatasetItem(meta) {
    const isActive = meta.dataset_id === App.activeDatasetId;
    const confRaw = meta.source_confidence ?? 0;
    const low = confRaw < 0.55;
    const adapterName = findAdapterName(meta.source_id);
    const conf = Math.round(confRaw * 100);
    const badgeLabel = low
        ? `Unknown source`
        : escapeHtml(adapterName);
    const badgeTitle = low
        ? `We couldn't confidently match this file to a known source (best guess: ${adapterName} at ${conf}%). Open the mapping editor to pick a source manually.`
        : `Detected source. Confidence is an estimate — open the mapping editor to review individual columns.`;
    const mapped = Object.values(meta.mapping || {}).filter(Boolean).length;
    const flagEntries = Object.entries(meta.quality_flags || {});
    const flagCount = flagEntries.length;
    const flagTooltip = flagEntries.map(([k, v]) => qualityFlagText(k, v)).join('\n');
    const firstFlagText = flagCount
        ? qualityFlagText(flagEntries[0][0], flagEntries[0][1])
        : '';
    const moreFlagsText = flagCount > 1 ? ` +${flagCount - 1} more` : '';
    return `
      <div class="dataset-item ${isActive ? 'active' : ''}" data-id="${meta.dataset_id}">
        <div class="dataset-meta">
          <h4>${escapeHtml(meta.original_filename)}</h4>
          <div class="info-row">
            <span>${formatInt(meta.row_count)} rows</span>
            <span>·</span>
            <span>${meta.columns.length} columns</span>
            <span>·</span>
            <span>${mapped} of ${meta.columns.length} columns matched</span>
            ${flagCount ? `<span>·</span><span class="badge badge-warn" title="${escapeHtml(flagTooltip)}"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(firstFlagText)}${moreFlagsText}</span>` : ''}
          </div>
        </div>
        <span class="source-badge ${low ? 'low' : ''}" title="${escapeHtml(badgeTitle)}">
          <i class="fa-solid fa-compass"></i> ${badgeLabel} · ${conf}%
        </span>
        <button class="icon-btn danger" data-id="${meta.dataset_id}" title="Remove this dataset">
          <i class="fa-solid fa-trash"></i>
        </button>
      </div>`;
}

function findAdapterName(id) {
    const found = (App.state?.adapters || []).find(a => a.id === id);
    return found ? found.name : id;
}

async function loadDataset(datasetId) {
    try {
        const data = await fetchJson(`/api/datasets/${encodeURIComponent(datasetId)}`);
        App.activeDatasetId = datasetId;
        App.activeDataset = data;
        renderMapping(data);
        renderDatasetList(App.state);
    } catch (err) {
        setStatus(`Could not load dataset: ${err.message}`, 'error');
    }
}

async function deleteDataset(datasetId) {
    try {
        await fetchJson(`/api/datasets/${encodeURIComponent(datasetId)}`, { method: 'DELETE' });
        if (App.activeDatasetId === datasetId) {
            App.activeDatasetId = null;
            App.activeDataset = null;
            hideMapping();
        }
        await refreshState();
    } catch (err) {
        setStatus(`Delete failed: ${err.message}`, 'error');
    }
}

function hideMapping() {
    const card = $('#mapping-card');
    if (card) card.style.display = 'none';
}

function renderMapping(data) {
    const card = $('#mapping-card');
    if (!card) return;
    card.style.display = 'block';
    const btnNext = $('#btn-go-to-corpus');
    if (btnNext) btnNext.style.display = 'none';
    const meta = data.meta;
    $('#mapping-title').textContent = `Column mapping · ${meta.original_filename}`;
    const conf = Math.round((meta.source_confidence ?? 0) * 100);
    const origin = MAPPING_SOURCE_LABELS[meta.mapping_source] || meta.mapping_source;
    $('#mapping-subtitle').textContent =
        `${formatInt(meta.row_count)} rows · detected ${findAdapterName(meta.source_id)} (${conf}% confidence) · current mapping: ${origin}`;

    // Source picker
    const picker = $('#source-picker');
    picker.innerHTML = (App.state.adapters || []).map(a =>
        `<option value="${a.id}" ${a.id === meta.source_id ? 'selected' : ''}>${escapeHtml(a.name)}</option>`
    ).join('');

    // Quality flags
    renderQualityFlags(meta.quality_flags);

    // Build a <select> of standard column names with human-friendly labels.
    // "— don't use —" is the explicit skip option; required fields are marked *.
    const canonicalOptions = ['<option value="">— don\u2019t use —</option>']
        .concat((App.state.canonical_schema || []).map(f =>
            `<option value="${escapeHtml(f.name)}" title="${escapeHtml(f.description || '')}">${escapeHtml(fieldLabel(f.name))}${f.required ? ' *' : ''}</option>`
        )).join('');

    const sample = (data.preview && data.preview[0]) || {};
    const tbody = $('#mapping-tbody');
    tbody.innerHTML = meta.columns.map(col => {
        const s = (data.suggestions || {})[col] || {};
        const current = meta.mapping?.[col] || s.canonical || '';
        const confRaw = Number(s.confidence || 0);
        const confPct = s.confidence ? `${Math.round(confRaw * 100)}%` : '—';
        const confClass = !s.confidence ? '' :
            confRaw >= 0.9 ? 'conf-high' :
            confRaw >= 0.7 ? 'conf-mid'  : 'conf-low';
        const methodLbl = METHOD_LABELS[s.method] || s.method || '—';
        const sampleVal = sample[col] !== undefined ? sample[col] : '';
        const sampleStr = sampleVal === null ? '' : String(sampleVal);
        return `
          <tr data-col="${escapeHtml(col)}">
            <td><code>${escapeHtml(col)}</code></td>
            <td><span class="sample-preview" title="${escapeHtml(sampleStr)}">${escapeHtml(sampleStr.slice(0, 120))}</span></td>
            <td>
              <select class="map-select" data-col="${escapeHtml(col)}">
                ${canonicalOptions.replace(`value="${escapeHtml(current)}"`, `value="${escapeHtml(current)}" selected`)}
              </select>
            </td>
            <td class="conf-cell ${confClass}">${confPct}</td>
            <td><span class="muted small">${escapeHtml(methodLbl)}</span></td>
          </tr>`;
    }).join('');
}

function renderQualityFlags(flags) {
    const el = $('#quality-flags');
    if (!el) return;
    const entries = Object.entries(flags || {});
    if (entries.length === 0) {
        el.innerHTML = '<span class="badge badge-good"><i class="fa-solid fa-check"></i> Looks clean — no issues found</span>';
        return;
    }
    el.innerHTML = entries.map(([k, v]) => {
        const text = qualityFlagText(k, v);
        const cls = (k === 'missing_text' || k === 'missing_post_id') ? 'badge-danger' : 'badge-warn';
        return `<span class="badge ${cls}"><i class="fa-solid fa-triangle-exclamation"></i> ${escapeHtml(text)}</span>`;
    }).join('');
}

async function saveMapping() {
    if (!App.activeDatasetId) return;
    const mapping = {};
    $$('.map-select').forEach(sel => {
        if (sel.value) mapping[sel.dataset.col] = sel.value;
    });
    const source_id = $('#source-picker').value;
    try {
        setStatus('Saving mapping…');
        const resp = await fetchJson(
            `/api/datasets/${encodeURIComponent(App.activeDatasetId)}/mapping`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mapping, source_id }),
            }
        );
        const problems = resp.problems || [];
        if (problems.length === 0) {
            const corpusBuilt = !!(App.state?.corpus?.built);
            setStatus(
                corpusBuilt
                  ? 'Mapping saved. Rebuild the corpus from the Corpus tab to include the updated mapping.'
                  : 'Mapping saved. Head to the Corpus tab to combine your mapped datasets into a working corpus.',
                'ok'
            );
            const btnNext = $('#btn-go-to-corpus');
            if (btnNext) btnNext.style.display = 'inline-flex';
        } else {
            const friendly = problems.map(p => {
                if (p.startsWith('missing_required:')) {
                    const fld = p.split(':')[1];
                    return `missing required column: ${fieldLabel(fld)}`;
                }
                if (p.startsWith('duplicate_target:')) {
                    const fld = p.split(':')[1];
                    return `two columns both map to ${fieldLabel(fld)}`;
                }
                return p;
            });
            setStatus(`Mapping saved, but: ${friendly.join('; ')}`, 'warn');
        }
        await refreshState();
        await loadDataset(App.activeDatasetId);
    } catch (err) {
        setStatus(`Save failed: ${err.message}`, 'error');
    }
}

async function aiSuggest() {
    if (!App.activeDatasetId) return;
    if (!App.state?.has_api_key) {
        setStatus('To use AI suggestions, add an Anthropic API key under Settings first.', 'warn');
        return;
    }
    try {
        setStatus('Asking AI for a mapping\u2026');
        const resp = await fetchJson(
            `/api/datasets/${encodeURIComponent(App.activeDatasetId)}/mapping/suggest`,
            { method: 'POST' }
        );
        setStatus('AI suggested a mapping — review it and click "Save mapping" if it looks right.', 'ok');
        App.activeDataset = {
            meta: resp.meta,
            suggestions: resp.suggestions,
            preview: App.activeDataset?.preview || [],
        };
        renderMapping(App.activeDataset);
        await refreshState();
    } catch (err) {
        setStatus(`AI suggest failed: ${err.message}`, 'error');
    }
}

/* ── 5. UPLOAD ──────────────────────────────────────────────────────────── */
function setupUpload() {
    const zone = $('#drop-zone');
    const input = $('#file-input');
    if (!zone || !input) return;

    zone.addEventListener('click', () => input.click());
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', e => {
        e.preventDefault();
        zone.classList.remove('dragover');
        const file = e.dataTransfer.files?.[0];
        if (file) uploadFile(file);
    });
    input.addEventListener('change', e => {
        const file = e.target.files?.[0];
        if (file) uploadFile(file);
        input.value = '';
    });
}

async function uploadFile(file) {
    const existing = Object.values(App.state?.datasets || {})
        .filter(d => d.original_filename === file.name);
    if (existing.length > 0) {
        const ok = confirm(
            `You already have ${existing.length} dataset${existing.length===1?'':'s'} named "${file.name}". Upload another copy?\n\n` +
            `Click Cancel if you meant to review or replace the existing one from the Datasets list.`
        );
        if (!ok) {
            setStatus('Upload cancelled — review existing datasets in the list below.', 'warn');
            return;
        }
    }
    const zone = $('#drop-zone');
    const loader = $('#upload-loader');
    zone?.classList.add('loading');
    loader?.classList.add('visible');
    setStatus(`Uploading ${file.name}…`);
    try {
        const form = new FormData();
        form.append('file', file);
        let res = await fetch('/api/datasets/upload', { method: 'POST', body: form });
        // 409 duplicate_content_hash — byte-identical blob already uploaded.
        // Offer to replace the old one and retry with replace_hash_match=true.
        if (res.status === 409) {
            const body = await res.json().catch(() => ({}));
            if (body.blocked === 'duplicate_content_hash') {
                const d = body.duplicate_of || {};
                const replace = confirm(
                    `This file is byte-identical to "${d.original_filename || 'an existing dataset'}" ` +
                    `(${d.row_count ?? '?'} rows, uploaded ${d.uploaded_at || '?'}).\n\n` +
                    `Click OK to delete the old copy and register this as a replacement, or Cancel to keep both (not recommended).`
                );
                if (!replace) {
                    setStatus('Upload cancelled — duplicate file.', 'warn');
                    return;
                }
                const form2 = new FormData();
                form2.append('file', file);
                res = await fetch('/api/datasets/upload?replace_hash_match=true', { method: 'POST', body: form2 });
            }
        }
        if (!res.ok) {
            let detail = res.statusText;
            try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
            throw new Error(detail);
        }
        const data = await res.json();
        const conf = data.meta.source_confidence ?? 0;
        const confPct = Math.round(conf * 100);
        const detected = findAdapterNameFromCandidates(data.meta.source_candidates);
        if (conf < 0.55) {
            setStatus(
                `Uploaded · low-confidence source match (best guess: ${detected} at ${confPct}%). Review the column mapping before building the corpus.`,
                'warn'
            );
        } else {
            setStatus(`Uploaded · detected ${detected} (${confPct}% confidence)`, 'ok');
        }
        App.activeDatasetId = data.dataset_id;
        App.activeDataset = { meta: data.meta, suggestions: data.suggestions, preview: data.preview };
        await refreshState();
        renderMapping(App.activeDataset);
    } catch (err) {
        setStatus(`Upload failed: ${err.message}`, 'error');
    } finally {
        zone?.classList.remove('loading');
        loader?.classList.remove('visible');
    }
}

function findAdapterNameFromCandidates(candidates) {
    if (!candidates || !candidates.length) return 'unknown';
    return candidates[0].name;
}

function setStatus(text, level = 'info') {
    const el = $('#upload-status');
    if (!el) return;
    el.textContent = text;
    el.style.color =
        level === 'error' ? 'var(--danger)' :
        level === 'warn'  ? 'var(--warn)' :
        level === 'ok'    ? 'var(--success)' :
                            'var(--text-muted)';
}

/* ── 5b. CORPUS ─────────────────────────────────────────────────────────── */
function renderCorpus(state) {
    const datasets = Object.values(state?.datasets || {});
    const emptyCard    = $('#corpus-empty-card');
    const pickerCard   = $('#corpus-picker-card');
    const summaryCard  = $('#corpus-summary-card');
    const filterCard   = $('#corpus-filter-card');
    const resultsCard  = $('#corpus-results-card');

    // No datasets imported → nudge to Import.
    if (datasets.length === 0) {
        emptyCard.style.display = 'block';
        pickerCard.style.display = 'none';
        summaryCard.style.display = 'none';
        filterCard.style.display = 'none';
        resultsCard.style.display = 'none';
        return;
    }
    emptyCard.style.display = 'none';

    const built = !!(state?.corpus?.built);
    if (built) {
        // If user clicked Rebuild, we keep the picker open — check a flag.
        if (App.corpus.pickerSelection instanceof Set) {
            renderCorpusPicker(state, /*rebuild=*/true);
            pickerCard.style.display = 'block';
        } else {
            pickerCard.style.display = 'none';
        }
        summaryCard.style.display = 'block';
        filterCard.style.display = 'block';
        resultsCard.style.display = 'block';
        renderCorpusSummary(state);
        // Lazy-load the corpus body (facets + first page) once per page visit.
        if (!App.corpus.loadedOnce) {
            App.corpus.loadedOnce = true;
            fetchCorpusSummary().then(() => reloadCorpusFilter());
        } else {
            renderFilterControls();
            renderResultsTable();
        }
    } else {
        // Not yet built — show picker with everything pre-checked.
        if (!(App.corpus.pickerSelection instanceof Set)) {
            App.corpus.pickerSelection = new Set(datasets.map(d => d.dataset_id));
            App.corpus.pickerDedupeNear = true;
        }
        pickerCard.style.display = 'block';
        summaryCard.style.display = 'none';
        filterCard.style.display = 'none';
        resultsCard.style.display = 'none';
        renderCorpusPicker(state, /*rebuild=*/false);
    }
}

function renderCorpusPicker(state, rebuild) {
    const list = $('#corpus-picker-list');
    const datasets = Object.values(state?.datasets || {});
    const selection = App.corpus.pickerSelection;

    $('#corpus-picker-title').textContent = rebuild ? 'Rebuild the corpus' : 'Build your corpus';
    $('#btn-build-corpus-label').textContent = rebuild ? 'Rebuild corpus' : 'Build corpus';
    $('#btn-cancel-rebuild').style.display = rebuild ? 'inline-flex' : 'none';
    $('#corpus-dedupe-near').checked = !!App.corpus.pickerDedupeNear;

    list.innerHTML = datasets.map(d => {
        const mapped = Object.values(d.mapping || {}).filter(Boolean).length;
        const noMapping = mapped === 0;
        const checked = selection.has(d.dataset_id) && !noMapping;
        const disabled = noMapping ? 'disabled' : '';
        const warn = noMapping ? 'warn' : '';
        const flags = Object.keys(d.quality_flags || {}).length;
        const flagBit = flags ? `<span class="badge badge-warn" style="margin-left:0.4rem;">${flags} ${flags===1?'issue':'issues'}</span>` : '';
        const mapBit = noMapping
            ? '<span class="badge badge-danger" style="margin-left:0.4rem;">No mapping — save one in Import first</span>'
            : '';
        return `
          <label class="dataset-check ${warn}" data-id="${d.dataset_id}">
            <input type="checkbox" ${checked ? 'checked' : ''} ${disabled} data-id="${d.dataset_id}">
            <div style="flex:1; min-width:0;">
              <div class="dc-title">${escapeHtml(d.original_filename)}${flagBit}${mapBit}</div>
              <div class="dc-meta">${formatInt(d.row_count)} rows · ${d.columns.length} columns · ${mapped} mapped · detected ${escapeHtml(findAdapterName(d.source_id))}</div>
            </div>
          </label>`;
    }).join('');

    $$('#corpus-picker-list input[type="checkbox"]').forEach(cb => {
        cb.addEventListener('change', ev => {
            const id = ev.target.dataset.id;
            if (ev.target.checked) selection.add(id);
            else selection.delete(id);
        });
    });
}

function renderCorpusSummary(state) {
    const c = state?.corpus || {};
    const stats = c.stats || {};
    const rows = c.rows || 0;
    const builtAt = c.built_at ? new Date(c.built_at).toLocaleString() : '—';

    // Detect stale: any source dataset the corpus was built from that is no longer present.
    const currentIds = new Set(Object.keys(state?.datasets || {}));
    const missing = (c.source_dataset_ids || []).filter(id => !currentIds.has(id));

    let baseLine = `${formatInt(rows)} rows from ${formatInt(stats.datasets || 0)} dataset${(stats.datasets||0)===1?'':'s'} · built ${builtAt}`;
    
    const dupsRemoved = (stats.near_text_duplicates || 0) + (stats.exact_post_id_duplicates || 0);
    if (dupsRemoved > 0) {
        baseLine += ` <span class="badge badge-warn" style="margin-left:0.5rem;"><i class="fa-solid fa-scissors"></i> ${formatInt(dupsRemoved)} duplicates removed</span>`;
    }

    const line = $('#corpus-summary-line');
    if (missing.length) {
        line.innerHTML = `${baseLine}<br><span class="badge badge-warn mt-1" style="margin-top:0.4rem;"><i class="fa-solid fa-triangle-exclamation"></i> ${missing.length} source dataset${missing.length===1?'':'s'} removed since this corpus was built — rebuild to refresh.</span>`;
    } else {
        line.innerHTML = baseLine;
    }

    const removedExact = stats.exact_post_id_duplicates || 0;
    const removedNear  = stats.near_text_duplicates || 0;
    const inputRows    = stats.input_rows || 0;
    const grid = $('#corpus-stats-grid');
    grid.innerHTML = `
      <div class="stat-tile accent">
        <div class="stat-label">Final rows <i class="hint-trigger" data-hint="rows-total" tabindex="0" title="What does final rows mean?">i</i></div>
        <div class="stat-value">${formatInt(rows)}</div>
      </div>
      <div class="stat-tile">
        <div class="stat-label">Rows in <i class="hint-trigger" data-hint="rows-in" tabindex="0" title="What does rows-in mean?">i</i></div>
        <div class="stat-value">${formatInt(inputRows)}</div>
      </div>
      <div class="stat-tile">
        <div class="stat-label">Exact ID dupes removed <i class="hint-trigger" data-hint="exact-id-dupes" tabindex="0" title="What are exact ID duplicates?">i</i></div>
        <div class="stat-value">${formatInt(removedExact)}</div>
      </div>
      <div class="stat-tile">
        <div class="stat-label">Near-duplicates removed <i class="hint-trigger" data-hint="near-dupes-removed" tabindex="0" title="What are near-duplicates?">i</i></div>
        <div class="stat-value">${formatInt(removedNear)}</div>
      </div>`;

    refreshSnapshotsPicker();
}

async function refreshSnapshotsPicker() {
    const panel = $('#corpus-snapshots-panel');
    const picker = $('#snapshot-picker');
    if (!panel || !picker) return;
    try {
        const data = await fetchJson('/api/corpus/snapshots');
        const items = data?.items || [];
        if (items.length === 0) {
            panel.style.display = 'none';
            return;
        }
        panel.style.display = 'block';
        picker.innerHTML = items.map(it => {
            const when = it.created_at ? new Date(it.created_at).toLocaleString() : '';
            const activeFlag = it.active ? ' · active' : '';
            const label = `${it.name} · ${formatInt(it.rows || 0)} rows · ${when}${activeFlag}`;
            return `<option value="${escapeHtml(it.snapshot_id)}"${it.active ? ' selected' : ''}>${escapeHtml(label)}</option>`;
        }).join('');
    } catch (e) {
        panel.style.display = 'none';
    }
}

async function activateSnapshot() {
    const picker = $('#snapshot-picker');
    const id = picker?.value;
    if (!id) return;
    try {
        await fetchJson('/api/corpus/snapshots/activate', { method: 'POST', body: JSON.stringify({ snapshot_id: id }) });
        setCorpusBuildStatus('Snapshot activated.', 'ok');
        App.corpus.loadedOnce = false;
        await refreshState();
    } catch (e) {
        setCorpusBuildStatus(`Activate failed: ${e.message || e}`, 'error');
    }
}

async function deleteSnapshot() {
    const picker = $('#snapshot-picker');
    const id = picker?.value;
    if (!id) return;
    const opt = picker.options[picker.selectedIndex];
    if (opt && /·\s*active$/.test(opt.textContent)) {
        setCorpusBuildStatus('You cannot delete the active snapshot. Activate another first.', 'warn');
        return;
    }
    if (!confirm('Delete this snapshot? The parquet file will be removed.')) return;
    try {
        await fetchJson(`/api/corpus/snapshots/${encodeURIComponent(id)}`, { method: 'DELETE' });
        setCorpusBuildStatus('Snapshot deleted.', 'ok');
        refreshSnapshotsPicker();
    } catch (e) {
        setCorpusBuildStatus(`Delete failed: ${e.message || e}`, 'error');
    }
}

async function fetchCorpusSummary() {
    try {
        const data = await fetchJson('/api/corpus');
        if (!data.built) {
            App.corpus.built = false;
            return;
        }
        App.corpus.built = true;
        App.corpus.rows = data.rows;
        App.corpus.facets = data.facets || {};
        App.corpus.stats = data.stats || {};
        App.corpus.builtAt = data.built_at || '';
        App.corpus.preview = data.preview || [];
        App.corpus.previewColumns = data.preview_columns || [];
        App.corpus.totalFiltered = data.rows;
    } catch (err) {
        console.error('[corpus] summary failed', err);
    }
}

async function buildCorpus() {
    const selection = App.corpus.pickerSelection;
    if (!selection || selection.size === 0) {
        setCorpusBuildStatus('Pick at least one dataset first.', 'warn');
        return;
    }
    const ids = [...selection];
    const dedupe = !!$('#corpus-dedupe-near').checked;
    App.corpus.pickerDedupeNear = dedupe;

    const btn = $('#btn-build-corpus');
    const loader = $('#corpus-build-loader');
    btn.disabled = true;
    loader.classList.add('visible');
    setCorpusBuildStatus(`Combining ${ids.length} dataset${ids.length===1?'':'s'}\u2026`);
    try {
        const kick = await fetchJson('/api/corpus/merge_async', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dataset_ids: ids, dedupe_near_text: dedupe }),
        });
        const jobId = kick.job_id;
        const finalJob = await trackJob(jobId, (pct, msg) => setCorpusBuildStatus(`${msg} — ${Math.round(pct)}%`));
        if (finalJob.status !== 'done') {
            throw new Error(finalJob.error || finalJob.status);
        }
        const data = finalJob.result || {};
        setCorpusBuildStatus(
            `Built ${formatInt(data.rows)} rows (removed ${formatInt((data.stats||{}).exact_post_id_duplicates||0)} exact + ${formatInt((data.stats||{}).near_text_duplicates||0)} near-duplicates).`,
            'ok'
        );
        App.corpus.built = true;
        App.corpus.rows = data.rows;
        App.corpus.facets = data.facets || {};
        App.corpus.stats = data.stats || {};
        App.corpus.builtAt = data.built_at || '';
        App.corpus.preview = data.preview || [];
        App.corpus.previewColumns = data.preview_columns || [];
        App.corpus.totalFiltered = data.rows;
        App.corpus.filter = emptyFilter();
        App.corpus.page = 1;
        App.corpus.pickerSelection = null; // close picker
        App.corpus.loadedOnce = true;
        await refreshState();     // re-renders Corpus view
    } catch (err) {
        setCorpusBuildStatus(`Build failed: ${err.message}`, 'error');
    } finally {
        btn.disabled = false;
        loader.classList.remove('visible');
    }
}

async function clearCorpus() {
    const ok = await showDecisionCard('clear_corpus');
    if (!ok) return;
    const rowsNow = Number(App.corpus?.rows || App.state?.corpus?.rows || 0);
    if (rowsNow > 0) {
        const typed = window.prompt(
            `This will discard the working corpus (${rowsNow.toLocaleString()} rows).\n` +
            `Your uploaded datasets and saved snapshots stay intact.\n\n` +
            `Type CLEAR (uppercase) to confirm.`,
            ''
        );
        if ((typed || '').trim() !== 'CLEAR') {
            setCorpusBuildStatus('Clear cancelled.', 'warn');
            return;
        }
    }
    try {
        logJourney('clear_corpus', { rows: rowsNow });
        await fetchJson('/api/corpus', { method: 'DELETE' });
        App.corpus = {
            built: false, rows: 0, facets: null, stats: null, builtAt: '',
            preview: [], previewColumns: [], totalFiltered: 0,
            page: 1, pageSize: 50,
            pickerSelection: null, pickerDedupeNear: true,
            filter: emptyFilter(), loadedOnce: false, filterDebounce: null,
        };
        await refreshState();
        setCorpusBuildStatus('Corpus cleared. Rebuild any time — your datasets are unchanged.', 'ok');
    } catch (err) {
        alert(`Clear failed: ${err.message}`);
    }
}

function setCorpusBuildStatus(text, level) {
    const el = $('#corpus-build-status');
    if (!el) return;
    el.textContent = text || '';
    el.style.color =
        level === 'error' ? 'var(--danger)' :
        level === 'warn'  ? 'var(--warn)' :
        level === 'ok'    ? 'var(--success)' :
                            'var(--text-muted)';
}

// ── Filters ──────────────────────────────────────────────────────────────
function renderFilterControls() {
    const f = App.corpus.filter;
    const facets = App.corpus.facets || {};

    const textEl = $('#f-text');
    const regexEl = $('#f-regex');
    const caseEl = $('#f-case');
    const dfEl = $('#f-date-from');
    const dtEl = $('#f-date-to');
    if (document.activeElement !== textEl) textEl.value = f.text || '';
    regexEl.checked = !!f.regex;
    caseEl.checked = !!f.case_sensitive;
    if (document.activeElement !== dfEl) dfEl.value = f.date_from || '';
    if (document.activeElement !== dtEl) dtEl.value = f.date_to || '';

    const engFields = [
        ['like_count',    '#f-like-min'],
        ['share_count',   '#f-share-min'],
        ['comment_count', '#f-comment-min'],
        ['view_count',    '#f-view-min'],
    ];
    for (const [col, sel] of engFields) {
        const el = $(sel);
        if (!el || document.activeElement === el) continue;
        const bound = (f.engagement || {})[col];
        el.value = bound && bound.min != null ? String(bound.min) : '';
    }

    // Date hint shows full range of the corpus.
    const hint = $('#f-date-hint');
    if (facets.date_min && facets.date_max) {
        hint.textContent = `Corpus range: ${facets.date_min.slice(0,10)} → ${facets.date_max.slice(0,10)}`;
    } else {
        hint.textContent = '';
    }

    const groups = [
        { key: 'platforms',       label: 'Platform',       filterKey: 'platforms' },
        { key: 'languages',       label: 'Language',       filterKey: 'languages' },
        { key: 'source_datasets', label: 'Source file',    filterKey: 'source_datasets' },
        { key: 'countries',       label: 'Country',        filterKey: 'countries' },
    ];
    const host = $('#f-chip-groups');
    const html = groups.map(g => {
        const values = facets[g.key] || [];
        if (values.length === 0) return '';
        const active = new Set(f[g.filterKey] || []);
        const chips = values.map(v => {
            const isActive = active.has(v.value);
            return `<span class="chip ${isActive ? 'active' : ''}" data-group="${g.filterKey}" data-value="${escapeHtml(v.value)}">
                ${escapeHtml(v.value)} <span class="chip-count">${formatInt(v.count)}</span>
            </span>`;
        }).join('');
        return `
          <div class="chip-group">
            <div class="chip-group-label">${g.label}</div>
            <div class="chip-row">${chips}</div>
          </div>`;
    }).join('');
    host.innerHTML = html || '<span class="muted small">No facet values available.</span>';

    $$('.chip', host).forEach(chip => {
        chip.addEventListener('click', () => {
            const group = chip.dataset.group;
            const value = chip.dataset.value;
            const arr = App.corpus.filter[group] || [];
            const idx = arr.indexOf(value);
            if (idx >= 0) arr.splice(idx, 1);
            else arr.push(value);
            App.corpus.filter[group] = arr;
            App.corpus.page = 1;
            reloadCorpusFilter();
        });
    });
}

function readFilterFromUI() {
    const f = App.corpus.filter;
    f.text = $('#f-text').value || '';
    f.regex = !!$('#f-regex').checked;
    f.case_sensitive = !!$('#f-case').checked;
    f.date_from = $('#f-date-from').value || null;
    f.date_to   = $('#f-date-to').value || null;
    const eng = {};
    const engFields = [
        ['like_count',    '#f-like-min'],
        ['share_count',   '#f-share-min'],
        ['comment_count', '#f-comment-min'],
        ['view_count',    '#f-view-min'],
    ];
    for (const [col, sel] of engFields) {
        const raw = $(sel)?.value || '';
        const n = raw === '' ? null : Number(raw);
        if (n != null && Number.isFinite(n) && n >= 0) eng[col] = { min: n };
    }
    f.engagement = eng;
}

function resetFilters() {
    App.corpus.filter = emptyFilter();
    App.corpus.sortBy = '';
    App.corpus.sortDesc = true;
    App.corpus.page = 1;
    renderFilterControls();
    reloadCorpusFilter();
}

function scheduleFilterReload() {
    clearTimeout(App.corpus.filterDebounce);
    App.corpus.filterDebounce = setTimeout(() => {
        readFilterFromUI();
        App.corpus.page = 1;
        reloadCorpusFilter();
    }, 250);
}

async function reloadCorpusFilter() {
    if (!App.corpus.built) return;
    const loader = $('#corpus-filter-loader');
    loader.classList.add('visible');
    try {
        const body = Object.assign({}, App.corpus.filter, {
            page: App.corpus.page,
            page_size: App.corpus.pageSize,
            sort_by: App.corpus.sortBy || null,
            sort_desc: !!App.corpus.sortDesc,
        });
        const data = await fetchJson('/api/corpus/filter', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        App.corpus.totalFiltered = data.rows_filtered;
        App.corpus.preview = data.preview || [];
        App.corpus.previewColumns = data.preview_columns || [];
        renderFilterControls();
        renderResultsTable();
    } catch (err) {
        $('#corpus-results-subtitle').textContent = `Filter failed: ${err.message}`;
    } finally {
        loader.classList.remove('visible');
    }
}

async function exportCorpusCsv() {
    if (!App.corpus.built) return;
    const btn = $('#btn-export-corpus-csv');
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Exporting…'; }
    try {
        const body = Object.assign({}, App.corpus.filter, {
            sort_by: App.corpus.sortBy || null,
            sort_desc: !!App.corpus.sortDesc,
        });
        const resp = await fetch('/api/corpus/export.csv', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const disp = resp.headers.get('Content-Disposition') || '';
        const m = disp.match(/filename="([^"]+)"/);
        const filename = m ? m[1] : 'corpus.csv';
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
        alert(`Export failed: ${err.message}`);
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = origHtml; }
    }
}

const SORTABLE_CORPUS_COLS = new Set([
    'created_at', 'platform', 'source_id', 'source_dataset',
    'author_handle', 'author_name', 'language', 'country', 'region',
    'like_count', 'share_count', 'comment_count', 'view_count',
]);

function toggleCorpusSort(col) {
    if (!SORTABLE_CORPUS_COLS.has(col)) return;
    const cur = App.corpus.sortBy;
    if (cur === col) {
        App.corpus.sortDesc = !App.corpus.sortDesc;
    } else {
        App.corpus.sortBy = col;
        App.corpus.sortDesc = CORPUS_NUM_COLS.has(col) || col === 'created_at';
    }
    App.corpus.page = 1;
    reloadCorpusFilter();
}

// ── Results table ────────────────────────────────────────────────────────
let corpusGridApi = null;

function renderResultsTable() {
    const cols = (App.corpus.previewColumns || []).filter(c => c !== '_row_idx');
    const rows = App.corpus.preview || [];

    // Per-column maxima so the inline bar scales relative to what's actually in view
    const colMax = {};
    for (const c of cols) {
        if (!CORPUS_NUM_COLS.has(c)) continue;
        let m = 0;
        for (const r of rows) {
            const n = Number(r?.[c]);
            if (Number.isFinite(n) && n > m) m = n;
        }
        colMax[c] = m;
    }

    const columnDefs = cols.map(c => {
        const headerName = corpusColLabel(c);
        let cellRenderer = null;

        if (CORPUS_LINK_COLS.has(c)) {
            cellRenderer = params => {
                if (!params.value) return '<span class="muted small">—</span>';
                const safe = escapeHtml(String(params.value));
                return `<a class="col-link" href="${safe}" target="_blank" rel="noopener"><i class="fa-solid fa-arrow-up-right-from-square" style="color: #C8175D;"></i></a>`;
            };
        } else if (c === 'text') {
            cellRenderer = params => `<div class="corpus-text-cell" title="${escapeHtml(String(params.value))}">${escapeHtml(String(params.value))}</div>`;
        } else if (CORPUS_DATE_COLS.has(c)) {
            cellRenderer = params => `<span class="col-date">${escapeHtml(formatCorpusDate(params.value))}</span>`;
        } else if (CORPUS_NUM_COLS.has(c)) {
             const maxForCol = Math.max(1, colMax[c] || 1);
             cellRenderer = params => {
                 if (params.value === null || params.value === undefined) return '<span class="muted small">—</span>';
                 const raw = Number(params.value);
                 if (!Number.isFinite(raw)) return '<span class="muted small">—</span>';
                 const v = formatInt(raw);
                 const percent = Math.min(100, Math.max(0, (raw / maxForCol) * 100));
                 return `<div style="display:flex; align-items:center; gap: 8px; width:100%;">
                            <span class="col-num" style="min-width: 40px; text-align: right; font-variant-numeric: tabular-nums;">${v}</span>
                            <div style="flex:1; height: 4px; background: rgba(0,0,0,0.06); border-radius: 2px; overflow:hidden;">
                                 <div style="height: 100%; width: ${percent}%; background: linear-gradient(90deg,#C8175D,#ff5d9a); border-radius: 2px;"></div>
                            </div>
                         </div>`;
             };
        }

        const isSorted = App.corpus.sortBy === c;
        const sortState = isSorted ? (App.corpus.sortDesc ? 'desc' : 'asc') : null;

        return {
            field: c,
            headerName: headerName,
            sortable: true,
            sort: sortState,
            filter: true,
            resizable: true,
            flex: c === 'text' ? 1 : undefined,
            minWidth: c === 'text' ? 400 : 100,
            autoHeight: c === 'text',
            wrapText: c === 'text',
            cellRenderer: cellRenderer || (params => {
                if (params.value === null || params.value === undefined || params.value === '') return '<span class="muted small">—</span>';
                return escapeHtml(String(params.value));
            })
        };
    });

    const eGridDiv = document.querySelector('#corpus-grid');
    if (!eGridDiv) return;

    if (!corpusGridApi) {
        const gridOptions = {
            columnDefs: columnDefs,
            rowData: rows,
            onRowClicked: (event) => {
                const idx = Number(event.data._row_idx);
                if (!Number.isNaN(idx)) openRowModal(idx);
            },
            onSortChanged: (event) => {
                const sortCols = event.api.getColumnState().filter(s => s.sort);
                if (sortCols.length > 0) {
                    App.corpus.sortBy = sortCols[0].colId;
                    App.corpus.sortDesc = sortCols[0].sort === 'desc';
                } else {
                    App.corpus.sortBy = '';
                }
                App.corpus.page = 1;
                reloadCorpusFilter();
            },
            defaultColDef: {
                sortable: true,
                resizable: true,
                suppressMovable: false
            },
            rowHeight: 60,
            overlayNoRowsTemplate: renderCorpusZeroState()
        };
        corpusGridApi = agGrid.createGrid(eGridDiv, gridOptions);
    } else {
        corpusGridApi.updateGridOptions({ columnDefs });
        corpusGridApi.updateGridOptions({ rowData: rows });
    }

    // Results subtitle + pagination.
    const total = App.corpus.totalFiltered;
    const rowsInCorpus = App.corpus.rows;
    const activeCount = activeFilterCount();
    let subtitle;
    if (activeCount === 0) {
        subtitle = `Showing all ${formatInt(total)} rows.`;
    } else {
        subtitle = `${formatInt(total)} of ${formatInt(rowsInCorpus)} rows match — ${activeCount} filter${activeCount===1?'':'s'} active.`;
    }
    $('#corpus-results-subtitle').textContent = subtitle;
    renderPagination();
    renderGallery(rows);
}

function renderGallery(rows) {
    const gal = $('#corpus-gallery');
    if (!gal) return;
    if (rows.length === 0) {
        gal.innerHTML = `<div class="muted small" style="text-align:center; margin-top:2rem;">No rows to display.</div>`;
        return;
    }
    
    let html = '';
    for (const r of rows) {
        const text = r.text ? escapeHtml(String(r.text)) : '<i class="muted">No text.</i>';
        const urlMatch = text.match(/https?:\/\/[^\s]+/);
        const urlStr = r.url || (urlMatch ? urlMatch[0] : null);
        let mediaHtml = '';
        if (urlStr) {
            mediaHtml = `<div class="gc-media"><i class="fa-regular fa-image"></i> External Media Attached<br/><a href="${escapeHtml(urlStr)}" target="_blank" style="margin-top:0.4rem; font-size: 0.75rem;">Link &rarr;</a></div>`;
        }

        const author = escapeHtml(r.author_name || r.author_handle || 'Unknown Author');
        const handleMatch = author.replace(/[^a-zA-Z0-9]/g, '').substring(0, 2).toUpperCase() || 'NA';
        
        let statsHtml = '';
        if (r.like_count || r.share_count || r.comment_count || r.view_count) {
             statsHtml = `<div class="gc-stats">
                 ${r.like_count ? `<div class="gc-stat"><i class="fa-regular fa-heart"></i> ${formatInt(r.like_count)}</div>` : ''}
                 ${r.share_count ? `<div class="gc-stat"><i class="fa-solid fa-retweet"></i> ${formatInt(r.share_count)}</div>` : ''}
                 ${r.comment_count ? `<div class="gc-stat"><i class="fa-regular fa-comment"></i> ${formatInt(r.comment_count)}</div>` : ''}
                 ${r.view_count ? `<div class="gc-stat"><i class="fa-regular fa-eye"></i> ${formatInt(r.view_count)}</div>` : ''}
             </div>`;
        }

        html += `<div class="gallery-card" onclick="openRowModal(${r._row_idx})" title="Click to open row inspector">
            <div class="gc-header">
                <div class="gc-avatar">${handleMatch}</div>
                <div class="gc-meta">
                    <div class="gc-author">${author}</div>
                    <div class="gc-date">${r.created_at ? formatCorpusDate(r.created_at) : 'No date'} · ${escapeHtml(r.platform || 'Unknown')}</div>
                </div>
            </div>
            <div class="gc-text">${text}</div>
            ${mediaHtml}
            ${statsHtml}
        </div>`;
    }
    gal.innerHTML = html;
}

// ─── Phase 4: Visual Query Builder ──────────────────────────────────────
const VQB_FIELDS = [
    { value: 'text', label: 'Full Text' },
    { value: 'author', label: 'Author Name/Handle' },
    { value: 'url', label: 'Link/URL' },
    { value: 'hashtag', label: 'Hashtag' },
    { value: 'lang', label: 'Language (e.g. en)' },
    { value: 'country', label: 'Country (e.g. US)' },
    { value: 'platform', label: 'Platform ID' }
];

let vqbState = {
    type: 'GROUP',
    operator: 'AND',
    children: [
        { type: 'RULE', field: 'text', value: '' }
    ]
};

function renderVQB() {
    const root = $('#vqb-root');
    if (!root) return;
    
    function buildGroupHTML(group, path) {
        let html = `<div class="vqb-group">
            <div class="vqb-group-controls">
                <select onchange="updateVQB('${path}', 'operator', this.value)">
                    <option value="AND" ${group.operator==='AND'?'selected':''}>AND</option>
                    <option value="OR"  ${group.operator==='OR' ?'selected':''}>OR</option>
                    <option value="NOT" ${group.operator==='NOT'?'selected':''}>NOT</option>
                </select>
                <div style="flex:1;"></div>
                <button class="btn-icon small" title="Add Rule" onclick="addVQBChild('${path}', 'RULE')"><i class="fa-solid fa-plus"></i></button>
                <button class="btn-icon small" title="Add Group nested" onclick="addVQBChild('${path}', 'GROUP')"><i class="fa-solid fa-layer-group"></i></button>
                ${path !== 'root' ? `<button class="btn-icon small" style="color:var(--text-muted);" title="Remove Group" onclick="removeVQB('${path}')"><i class="fa-solid fa-trash"></i></button>` : ''}
            </div>`;
        group.children.forEach((c, idx) => {
            const childPath = path === 'root' ? String(idx) : `${path}-${idx}`;
            if (c.type === 'GROUP') {
                 html += buildGroupHTML(c, childPath);
            } else {
                 html += `<div class="vqb-rule">
                    <select onchange="updateVQB('${childPath}', 'field', this.value)">
                        ${VQB_FIELDS.map(f => `<option value="${f.value}" ${c.field===f.value?'selected':''}>${escapeHtml(f.label)}</option>`).join('')}
                    </select>
                    <span class="muted small">contains</span>
                    <input type="text" value="${escapeHtml(c.value)}" placeholder="term..." onchange="updateVQB('${childPath}', 'value', this.value)">
                    <button class="btn-icon small" style="color:var(--text-muted);" onclick="removeVQB('${childPath}')"><i class="fa-solid fa-xmark"></i></button>
                 </div>`;
            }
        });
        html += `</div>`;
        return html;
    }
    
    root.innerHTML = buildGroupHTML(vqbState, 'root');
    syncVQBToText();
}

function getVQBNode(state, indices) {
    let curr = state;
    for (let i of indices) curr = curr.children[parseInt(i)];
    return curr;
}

window.updateVQB = function(path, key, value) {
    if (path === 'root') {
        vqbState[key] = value;
    } else {
        const parts = path.split('-');
        const idx = parts.pop();
        const parent = path === idx ? vqbState : getVQBNode(vqbState, parts);
        parent.children[parseInt(idx)][key] = value;
    }
    renderVQB();
};

window.addVQBChild = function(path, type) {
    const parent = path === 'root' ? vqbState : getVQBNode(vqbState, path.split('-'));
    if (type === 'RULE') parent.children.push({ type: 'RULE', field: 'text', value: '' });
    else parent.children.push({ type: 'GROUP', operator: 'AND', children: [{ type: 'RULE', field: 'text', value: '' }] });
    renderVQB();
};

window.removeVQB = function(path) {
    const parts = path.split('-');
    const idx = parseInt(parts.pop());
    const parent = path === String(idx) ? vqbState : getVQBNode(vqbState, parts);
    parent.children.splice(idx, 1);
    renderVQB();
};

function compileVQBGroup(group) {
    if (group.children.length === 0) return '';
    let terms = [];
    for (const c of group.children) {
        if (c.type === 'GROUP') {
             const sub = compileVQBGroup(c);
             if (sub) terms.push(`(${sub})`);
        } else {
             if (!c.value.trim()) continue;
             let val = c.value.trim();
             if (val.includes(' ') && !val.startsWith('"') && !val.endsWith('"')) val = `"${val}"`;
             if (c.field === 'text') terms.push(val);
             else terms.push(`${c.field}:${val}`);
        }
    }
    if (terms.length === 0) return '';
    return terms.join(` ${group.operator} `);
}

function syncVQBToText() {
    const q = compileVQBGroup(vqbState);
    const input = $('#slicer-query-input');
    if (input) {
        input.value = q;
        App.slicer.query = q;
    }
}

function renderCorpusCell(col, value) {
    if (value === null || value === undefined || value === '') {
        return `<td class="muted small">—</td>`;
    }
    if (CORPUS_LINK_COLS.has(col)) {
        const safe = escapeHtml(String(value));
        return `<td class="col-link"><a href="${safe}" target="_blank" rel="noopener"><i class="fa-solid fa-arrow-up-right-from-square"></i></a></td>`;
    }
    if (CORPUS_NUM_COLS.has(col)) {
        return `<td class="col-num">${formatInt(Number(value))}</td>`;
    }
    if (CORPUS_DATE_COLS.has(col)) {
        return `<td class="col-date">${escapeHtml(formatCorpusDate(value))}</td>`;
    }
    if (col === 'text') {
        return `<td><div class="corpus-text-cell">${escapeHtml(String(value))}</div></td>`;
    }
    return `<td>${escapeHtml(String(value))}</td>`;
}

function formatCorpusDate(v) {
    if (!v) return '';
    try {
        const d = new Date(v);
        if (Number.isNaN(d.getTime())) return String(v);
        return d.toISOString().slice(0, 16).replace('T', ' ');
    } catch (_) {
        return String(v);
    }
}

function activeFilterCount() {
    const f = App.corpus.filter;
    let n = 0;
    if (f.text) n++;
    if (f.date_from) n++;
    if (f.date_to) n++;
    ['platforms','languages','source_datasets','countries','source_ids'].forEach(k => {
        n += (f[k] || []).length;
    });
    return n;
}

function renderCorpusZeroState() {
    const rowsInCorpus = App.corpus.rows || 0;
    if (rowsInCorpus === 0) {
        return `
          <div class="empty-state">
            <i class="fa-solid fa-database"></i>
            <h4>Corpus is empty</h4>
            <p>Upload a dataset on the Import tab, then build your corpus to see rows here.</p>
          </div>`;
    }
    const f = App.corpus.filter || {};
    const active = [];
    if (f.text) active.push('text search');
    if (f.date_from || f.date_to) active.push('date range');
    if ((f.platforms || []).length) active.push('platform');
    if ((f.languages || []).length) active.push('language');
    if ((f.countries || []).length) active.push('country');
    if ((f.source_datasets || []).length) active.push('dataset');
    if ((f.source_ids || []).length) active.push('source');
    const suggestion = active.length
        ? `Try removing the ${active.join(' / ')} filter${active.length > 1 ? 's' : ''} or widening the date range.`
        : 'Try adjusting your filters.';
    return `
      <div class="empty-state">
        <i class="fa-solid fa-filter-circle-xmark"></i>
        <h4>No rows match these filters</h4>
        <p>${escapeHtml(suggestion)}</p>
        <span class="empty-hint">Your corpus has ${formatInt(rowsInCorpus)} row${rowsInCorpus === 1 ? '' : 's'} total.</span>
        <button class="btn-secondary btn-sm mt-1" id="corpus-clear-filters-empty"><i class="fa-solid fa-eraser"></i> Clear all filters</button>
      </div>`;
}

function renderPagination() {
    const total = App.corpus.totalFiltered || 0;
    const size = App.corpus.pageSize;
    const page = App.corpus.page;
    const pages = Math.max(1, Math.ceil(total / size));
    const host = $('#corpus-pagination');
    if (total === 0) { host.innerHTML = ''; return; }
    const start = (page - 1) * size + 1;
    const end = Math.min(total, page * size);
    host.innerHTML = `
      <button class="btn-secondary" id="pg-prev" ${page <= 1 ? 'disabled' : ''} title="Previous page"><i class="fa-solid fa-chevron-left"></i></button>
      <span class="page-label">${formatInt(start)}\u2013${formatInt(end)} of ${formatInt(total)}</span>
      <button class="btn-secondary" id="pg-next" ${page >= pages ? 'disabled' : ''} title="Next page"><i class="fa-solid fa-chevron-right"></i></button>
    `;
    $('#pg-prev')?.addEventListener('click', () => {
        if (App.corpus.page > 1) { App.corpus.page--; reloadCorpusFilter(); }
    });
    $('#pg-next')?.addEventListener('click', () => {
        if (App.corpus.page < pages) { App.corpus.page++; reloadCorpusFilter(); }
    });
}

// ── Row drawer ───────────────────────────────────────────────────────────
async function openRowModal(rowIdx) {
    const overlay = $('#row-drawer-overlay');
    const drawer = $('#row-drawer');
    const body = $('#row-drawer-body');
    const subtitle = $('#row-drawer-subtitle');
    
    body.innerHTML = '<div class="flex-row" style="justify-content:center; padding:2rem;"><div class="loader visible"></div></div>';
    subtitle.textContent = `Row #${rowIdx}`;
    
    if (overlay && drawer) {
        overlay.style.display = 'block';
        setTimeout(() => {
            overlay.style.opacity = '1';
            drawer.classList.add('open');
        }, 10);
    }
    
    try {
        const data = await fetchJson(`/api/corpus/row/${encodeURIComponent(rowIdx)}`);
        renderRowDrawer(data, rowIdx);
        loadMemosIntoDrawer(rowIdx);
    } catch (err) {
        body.innerHTML = `<p class="muted small">Could not load row: ${escapeHtml(err.message)}</p>`;
    }
}

function renderRowDrawer(data, rowIdx) {
    const row = data.row || data;
    const body = $('#row-drawer-body');
    const fields = Object.keys(row);
    const preferred = ['created_at','platform','source_type','text','author_name','author_handle','author_id',
                       'language','country','region','like_count','share_count','comment_count','view_count',
                       'url','hashtags','mentions','in_reply_to','parent_id','post_id','source_dataset','source_id'];
    const ordered = preferred.filter(k => fields.includes(k))
                             .concat(fields.filter(k => !preferred.includes(k) && k !== '_row_idx'));

    const fieldsHtml = ordered.map(k => {
        const v = row[k];
        const label = corpusColLabel(k);
        const val = (v === null || v === undefined || v === '')
            ? '<span class="rf-value muted">—</span>'
            : k === 'url'
              ? `<span class="rf-value"><a href="${escapeHtml(String(v))}" target="_blank" rel="noopener" style="color:var(--isd-pink); text-decoration:underline;">${escapeHtml(String(v))}</a></span>`
              : k === 'created_at'
                ? `<span class="rf-value">${escapeHtml(formatCorpusDate(v))}</span>`
                : CORPUS_NUM_COLS.has(k)
                  ? `<span class="rf-value">${formatInt(Number(v))}</span>`
                  : `<span class="rf-value">${escapeHtml(String(v))}</span>`;
        return `<div class="row-field"><span class="rf-label">${escapeHtml(label)}</span>${val}</div>`;
    }).join('');

    const memoHtml = `
        <div class="drawer-memos" data-row-idx="${Number(rowIdx)}">
            <div class="rf-label flex-row" style="align-items:center; gap:0.5rem; margin-bottom:0.5rem;">
                <span>Memos</span>
                <i class="fa-solid fa-circle-info hint-trigger" data-hint="memo-pad" title="What's a memo?"></i>
            </div>
            <p class="muted small" style="margin-top:0; margin-bottom:0.6rem;">Jot what you noticed — why this row matters, what inductive code it suggests. Memos are private to the coder and never affect IRR.</p>
            <textarea id="memo-new" rows="3" placeholder="Write a memo…" style="width:100%; box-sizing:border-box; padding:0.55rem 0.7rem; border:1px solid var(--border-color); border-radius:8px; background:rgba(255,255,255,0.8); resize:vertical; font: inherit; color:var(--text-main);"></textarea>
            <div class="flex-row" style="justify-content:flex-end; gap:0.5rem; margin-top:0.45rem;">
                <button class="btn-secondary" id="memo-add-btn"><i class="fa-solid fa-plus"></i> Add memo</button>
            </div>
            <div id="memo-list" style="margin-top:0.75rem;"><p class="muted small" style="margin:0;">Loading memos…</p></div>
        </div>`;

    body.innerHTML = fieldsHtml + memoHtml;
    $('#memo-add-btn')?.addEventListener('click', () => addMemoFromDrawer(Number(rowIdx)));
}

function renderMemoList(rowIdx, memos) {
    const list = $('#memo-list');
    if (!list) return;
    if (!memos || !memos.length) {
        list.innerHTML = '<p class="muted small" style="margin:0;">No memos yet for this row.</p>';
        return;
    }
    list.innerHTML = memos.map(m => {
        const when = m.ts ? new Date(m.ts).toLocaleString() : '';
        const coder = escapeHtml(m.coder || 'anonymous');
        const text = escapeHtml(String(m.text || ''));
        const id = escapeHtml(String(m.memo_id || ''));
        return `
            <div class="memo-entry" data-memo-id="${id}" style="border:1px solid var(--border-color); border-radius:8px; padding:0.6rem 0.75rem; background:rgba(255,255,255,0.55); margin-bottom:0.5rem;">
                <div class="flex-row" style="justify-content:space-between; gap:0.5rem; margin-bottom:0.3rem;">
                    <span class="muted small"><strong>${coder}</strong> · ${escapeHtml(when)}</span>
                    <span class="flex-row" style="gap:0.3rem;">
                        <button class="btn-icon" data-memo-edit="${id}" title="Edit memo"><i class="fa-solid fa-pen"></i></button>
                        <button class="btn-icon" data-memo-del="${id}" title="Delete memo"><i class="fa-solid fa-trash"></i></button>
                    </span>
                </div>
                <div class="memo-text" style="white-space:pre-wrap; word-break:break-word;">${text}</div>
            </div>`;
    }).join('');

    list.querySelectorAll('[data-memo-edit]').forEach(btn => {
        btn.addEventListener('click', () => {
            const mid = btn.getAttribute('data-memo-edit') || '';
            const entry = memos.find(x => x.memo_id === mid);
            editMemoInDrawer(rowIdx, mid, entry ? entry.text : '');
        });
    });
    list.querySelectorAll('[data-memo-del]').forEach(btn => {
        btn.addEventListener('click', () => {
            const mid = btn.getAttribute('data-memo-del') || '';
            deleteMemoFromDrawer(rowIdx, mid);
        });
    });
}

async function loadMemosIntoDrawer(rowIdx) {
    try {
        const data = await fetchJson(`/api/memos/row/${encodeURIComponent(rowIdx)}`);
        renderMemoList(rowIdx, data.memos || []);
    } catch (err) {
        const list = $('#memo-list');
        if (list) list.innerHTML = `<p class="muted small" style="margin:0;">Could not load memos: ${escapeHtml(err.message)}</p>`;
    }
}

async function addMemoFromDrawer(rowIdx) {
    const ta = $('#memo-new');
    const text = (ta?.value || '').trim();
    if (!text) { ta?.focus(); return; }
    const btn = $('#memo-add-btn');
    if (btn) btn.disabled = true;
    try {
        const res = await fetchJson('/api/memos/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ row_idx: Number(rowIdx), text }),
        });
        if (ta) ta.value = '';
        renderMemoList(rowIdx, res.row_memos || []);
        logJourney('memo_added', { row_idx: Number(rowIdx), length: text.length });
    } catch (err) {
        alert(`Could not save memo: ${err.message}`);
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function editMemoInDrawer(rowIdx, memoId, currentText) {
    const next = window.prompt('Edit memo:', currentText || '');
    if (next === null) return;
    const text = next.trim();
    if (!text) return;
    try {
        const res = await fetchJson('/api/memos/edit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ row_idx: Number(rowIdx), memo_id: memoId, text }),
        });
        renderMemoList(rowIdx, res.row_memos || []);
    } catch (err) {
        alert(`Could not edit memo: ${err.message}`);
    }
}

async function deleteMemoFromDrawer(rowIdx, memoId) {
    if (!window.confirm('Delete this memo? This cannot be undone.')) return;
    try {
        const res = await fetchJson('/api/memos/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ row_idx: Number(rowIdx), memo_id: memoId }),
        });
        renderMemoList(rowIdx, res.row_memos || []);
    } catch (err) {
        alert(`Could not delete memo: ${err.message}`);
    }
}

function closeRowModal() {
    const overlay = $('#row-drawer-overlay');
    const drawer = $('#row-drawer');
    if (drawer) drawer.classList.remove('open');
    if (overlay) {
        overlay.style.opacity = '0';
        setTimeout(() => overlay.style.display = 'none', 300);
    }
}

/* ── 6. SETTINGS ────────────────────────────────────────────────────────── */
function renderSettings(state) {
    const hasKey = !!state?.has_api_key;
    const currentCoder = state?.coding?.coder_name || '';

    const st = $('#key-status');
    if (st) {
        if (hasKey) {
            st.textContent = 'Key ready — kept in memory for this session only.';
            st.style.color = 'var(--success)';
        } else {
            st.textContent = 'No key added yet.';
            st.style.color = 'var(--text-muted)';
        }
    }

    const coderInput = $('#s-coder-name');
    const coderStatus = $('#coder-status');
    if (coderInput && document.activeElement !== coderInput) {
        coderInput.value = currentCoder;
    }
    if (coderStatus) {
        if (currentCoder) {
            coderStatus.textContent = `Signed in as "${currentCoder}". All tags you place will carry this name.`;
            coderStatus.style.color = 'var(--success)';
        } else {
            coderStatus.textContent = 'No coder set — required before you can tag rows.';
            coderStatus.style.color = 'var(--text-muted)';
        }
    }

    const apikeyNudge = $('#apikey-nudge');
    if (apikeyNudge) apikeyNudge.style.display = hasKey ? 'none' : 'flex';
    const coderNudge = $('#coder-nudge');
    if (coderNudge) coderNudge.style.display = currentCoder ? 'none' : 'flex';

    const clearBtn = $('#btn-clear-key');
    if (clearBtn) clearBtn.disabled = !hasKey;

    // Auto-focus the coder input when Settings opens and no coder is on file.
    const settingsPage = document.querySelector('#page-settings');
    if (!currentCoder && settingsPage?.classList.contains('active')
        && coderInput && document.activeElement !== coderInput && !coderInput.value) {
        setTimeout(() => { try { coderInput.focus(); } catch (_) {} }, 80);
    }

    const budgetInput = $('#s-budget');
    const budgetSummary = $('#budget-summary');
    if (budgetInput && document.activeElement !== budgetInput) {
        const b = Number(state?.settings?.monthly_budget_usd || 0);
        budgetInput.value = b > 0 ? b.toFixed(2) : '';
    }
    if (budgetSummary) {
        refreshBudgetSummary();
    }
}

async function refreshBudgetSummary() {
    const el = $('#budget-summary');
    if (!el) return;
    try {
        const r = await fetchJson('/api/settings/budget');
        const b = Number(r.budget_usd || 0);
        const sp = Number(r.spent_usd || 0);
        if (b <= 0) {
            el.textContent = 'No monthly ceiling — AI runs are not budget-limited.';
            el.style.color = 'var(--text-muted)';
        } else {
            const pct = Math.min(100, Math.round((sp / b) * 100));
            const remaining = Math.max(0, b - sp).toFixed(2);
            el.innerHTML = `This month (${r.month}): <strong>$${sp.toFixed(2)}</strong> spent of <strong>$${b.toFixed(2)}</strong> (${pct}% used, $${remaining} remaining).`;
            el.style.color = pct >= 100 ? 'var(--danger)' : pct >= 80 ? 'var(--warn)' : 'var(--text-muted)';
        }
    } catch {
        el.textContent = '';
    }
}

async function saveBudget() {
    const field = $('#s-budget');
    const st = $('#budget-status');
    if (!field) return;
    const v = Number(field.value || 0);
    if (!(v >= 0) || Number.isNaN(v)) {
        if (st) { st.textContent = 'Enter a non-negative number, or 0 to disable.'; st.style.color = 'var(--warn)'; }
        return;
    }
    try {
        const r = await fetchJson('/api/settings/budget', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ monthly_budget_usd: v }),
        });
        if (st) {
            st.textContent = r.budget_usd > 0 ? `Saved: $${r.budget_usd.toFixed(2)}/month` : 'Budget disabled.';
            st.style.color = 'var(--success)';
        }
        await refreshState();
        renderSettings(App.state);
    } catch (err) {
        if (st) { st.textContent = `Save failed: ${err.message}`; st.style.color = 'var(--danger)'; }
    }
}

async function saveApiKey() {
    const field = $('#s-api-key');
    const st = $('#key-status');
    if (!field) return;
    const key = field.value.trim();
    if (!key) {
        st.textContent = 'Please paste a key first.';
        st.style.color = 'var(--warn)';
        return;
    }
    try {
        await fetchJson('/api/settings/api_key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: key }),
        });
        field.value = '';
        await refreshState();
        renderSettings(App.state);
        try { refreshAiHealth(); } catch (_) {}
    } catch (err) {
        st.textContent = `Save failed: ${err.message}`;
        st.style.color = 'var(--danger)';
    }
}

async function clearApiKey() {
    if (!App.state?.has_api_key) return;
    if (!window.confirm('Forget the API key from this session? You\'ll need to paste it again next time. To fully revoke it, also disable it on console.anthropic.com.')) return;
    const st = $('#key-status');
    try {
        await fetchJson('/api/settings/api_key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: '' }),
        });
        if (st) { st.textContent = 'Forgotten from this session.'; st.style.color = 'var(--text-muted)'; }
        await refreshState();
        renderSettings(App.state);
        try { refreshAiHealth(); } catch (_) {}
    } catch (err) {
        if (st) { st.textContent = `Clear failed: ${err.message}`; st.style.color = 'var(--danger)'; }
    }
}

async function saveCoderName() {
    const field = $('#s-coder-name');
    const st = $('#coder-status');
    if (!field) return;
    const name = field.value.trim();
    if (!name) {
        if (st) {
            st.textContent = 'Please enter a name first.';
            st.style.color = 'var(--warn)';
        }
        field.focus();
        return;
    }
    try {
        await fetchJson('/api/settings/coder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ coder_name: name }),
        });
        await refreshState();
        renderSettings(App.state);
    } catch (err) {
        if (st) {
            st.textContent = `Save failed: ${err.message}`;
            st.style.color = 'var(--danger)';
        }
    }
}

/* ── 6.5 SLICER (Phase 3) ───────────────────────────────────────────────── */
function renderSlicer(state) {
    const corpusBuilt = !!(state?.corpus?.built);
    $('#slicer-empty-card').style.display     = corpusBuilt ? 'none' : 'block';
    $('#slicer-builder-card').style.display   = corpusBuilt ? 'block' : 'none';
    $('#slicer-sample-card').style.display    = corpusBuilt ? 'block' : 'none';
    $('#slicer-saved-card').style.display     = corpusBuilt ? 'block' : 'none';

    if (!corpusBuilt) {
        $('#slicer-result-card').style.display = 'none';
        $('#slicer-diff-card').style.display   = 'none';
        return;
    }

    App.slicer.totalInCorpus = state.corpus.rows || 0;

    // Corpus tag in the builder card header.
    const tag = $('#slicer-corpus-tag');
    if (tag) tag.textContent = `Corpus: ${formatInt(state.corpus.rows)} rows`;

    // Query input value stays across nav between pages.
    const input = $('#slicer-query-input');
    if (input && input.value !== App.slicer.query) input.value = App.slicer.query;

    // Sample card: load column list once, sync UI.
    loadSampleColumns().then(() => renderSampleCard());

    renderSavedSlices(state);
    renderSlicerResult();
    renderDiffCard(state);
}

/* ── 6b. SAMPLE & SPLIT ─────────────────────────────────────────────────── */
const SAMPLE_TAB_HELP = {
    random:     'Draw a uniform random sample of N rows. Reproducible with the same seed.',
    stratified: 'Keep the proportional mix of a column (e.g. platform) in a smaller sample.',
    top_n:      'Pick the rows ranked highest (or lowest) on a numeric column — useful for engagement analysis.',
    systematic: 'Take every Nth row in the current corpus order — quick deterministic thin-out.',
    split:      'Divide the corpus into K equal chunks for parallel coding. Add overlap for inter-coder reliability.',
};

async function loadSampleColumns() {
    if (App.slicer.sample.columnsLoaded) return;
    try {
        const data = await fetchJson('/api/slices/sample/columns');
        App.slicer.sample.categoricalCols = data.categorical || [];
        App.slicer.sample.numericCols = data.numeric || [];
        App.slicer.sample.columnsLoaded = true;
    } catch (err) {
        console.warn('[slicer] failed to load sample columns', err);
    }
}

function renderSampleCard() {
    const s = App.slicer.sample;
    const help = $('#sample-tab-help');
    if (help) help.textContent = SAMPLE_TAB_HELP[s.method] || '';

    $$('.sample-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.sampleMethod === s.method);
    });

    showSampleControls(s.method);
    populateSampleByColDropdown();
    renderSampleResult();
}

function showSampleControls(method) {
    const vis = (id, show) => {
        const el = document.getElementById(id);
        if (el) el.style.display = show ? '' : 'none';
    };
    // Controls visible per method
    const isSplit     = method === 'split';
    const isStrat     = method === 'stratified';
    const isTopN      = method === 'top_n';
    const isSystem    = method === 'systematic';
    const needsN      = method === 'random' || isStrat || isTopN;
    const needsSeed   = method === 'random' || isStrat || isSplit;
    const needsByCol  = isStrat || isTopN;
    const needsDir    = isTopN;

    vis('sample-n-wrap',        needsN);
    vis('sample-seed-wrap',     needsSeed);
    vis('sample-bycol-wrap',    needsByCol);
    vis('sample-dir-wrap',      needsDir);
    vis('sample-step-wrap',     isSystem);
    vis('split-k-wrap',         isSplit);
    vis('split-overlap-wrap',   isSplit);
    vis('sample-dedupe-wrap',   !isSplit);   // split doesn't apply dedupe pre-step

    // Label for the by-col dropdown changes with method
    const lbl = $('#sample-bycol-label');
    if (lbl) lbl.textContent = isTopN ? 'Rank by column' : 'Stratify by column';

    // Save rows toggle
    vis('sample-save-row', !isSplit);
    vis('split-save-row',  isSplit);
}

function populateSampleByColDropdown() {
    const sel = $('#sample-by-col');
    if (!sel) return;
    const method = App.slicer.sample.method;
    const opts = method === 'top_n'
        ? App.slicer.sample.numericCols
        : App.slicer.sample.categoricalCols;
    const current = App.slicer.sample.byCol;
    sel.innerHTML = (opts || []).map(c => {
        const label = corpusColLabel(c);
        return `<option value="${escapeHtml(c)}">${escapeHtml(label)}</option>`;
    }).join('');
    if (opts.length === 0) {
        sel.innerHTML = '<option value="">(no columns available)</option>';
    } else if (current && opts.includes(current)) {
        sel.value = current;
    } else {
        App.slicer.sample.byCol = sel.value || opts[0];
    }
}

function switchSampleTab(method) {
    if (!method || method === App.slicer.sample.method) return;
    App.slicer.sample.method = method;
    // Reset preview when switching methods — different spec space.
    App.slicer.sample.lastSpec = null;
    App.slicer.sample.preview = [];
    App.slicer.sample.rowsSampled = 0;
    App.slicer.sample.splitLastResult = null;
    setSampleStatus('', '');
    renderSampleCard();
}

function readSampleInputs() {
    const s = App.slicer.sample;
    s.n            = Math.max(1, parseInt($('#sample-n')?.value || '200', 10) || 200);
    s.seed         = parseInt($('#sample-seed')?.value || '42', 10) || 42;
    s.byCol        = $('#sample-by-col')?.value || '';
    s.ascending    = ($('#sample-ascending')?.value === 'true');
    s.step         = Math.max(2, parseInt($('#sample-step')?.value || '10', 10) || 10);
    s.splitK       = Math.max(2, Math.min(20, parseInt($('#split-k')?.value || '2', 10) || 2));
    s.splitOverlap = Math.max(0, Math.min(50, parseFloat($('#split-overlap')?.value || '0') || 0));
    s.baseQuery    = ($('#sample-base-query')?.value || '').trim();
    s.dedupe       = !!$('#sample-dedupe')?.checked;
}

function buildSampleSpecBody(page = 1) {
    const s = App.slicer.sample;
    return {
        method:      s.method,
        n:           s.n,
        seed:        s.seed,
        by_col:      s.byCol || null,
        ascending:   s.ascending,
        step:        s.step,
        base_query:  s.baseQuery,
        dedupe_text: s.dedupe,
        page,
        page_size:   s.pageSize,
    };
}

async function runSamplePreview(page = 1) {
    readSampleInputs();
    const s = App.slicer.sample;
    if (s.method === 'split') {
        // Split preview = show equal-chunks sizes (no save yet).
        return runSplitPreview();
    }
    setSampleStatus('Running…', '');
    try {
        const data = await fetchJson('/api/slices/sample/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(buildSampleSpecBody(page)),
        });
        s.lastSpec        = buildSampleSpecBody(page);
        s.rowsSampled     = data.rows_sampled;
        s.rowsInCorpus    = data.rows_in_corpus;
        s.description     = data.description || '';
        s.preview         = data.preview || [];
        s.previewColumns  = data.preview_columns || [];
        s.page            = data.page || page;
        s.splitLastResult = null;
        setSampleStatus(`${formatInt(data.rows_sampled)} of ${formatInt(data.rows_in_corpus)} rows selected.`, 'ok');
        renderSampleResult();
    } catch (err) {
        setSampleStatus(err.message || String(err), 'err');
        s.preview = [];
        s.rowsSampled = 0;
        $('#sample-result-wrap').style.display = 'none';
    }
}

async function runSplitPreview() {
    const s = App.slicer.sample;
    // Dry-run: ask the server for column counts via `/api/slices/sample/preview`
    // isn't the right endpoint for splits — instead we just compute expected sizes locally.
    const total = App.slicer.totalInCorpus || 0;
    if (!total) {
        setSampleStatus('Corpus is empty.', 'err');
        return;
    }
    // Optional base query: use preview endpoint to get filtered count
    let base = total;
    if (s.baseQuery) {
        try {
            const data = await fetchJson('/api/slices/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query: s.baseQuery, page: 1, page_size: 1 }),
            });
            base = data.rows_matched;
        } catch (err) {
            setSampleStatus(`Base filter failed: ${err.message}`, 'err');
            return;
        }
    }
    const k = s.splitK;
    const overlapFrac = s.splitOverlap / 100;
    const perChunk = Math.ceil(base / k);
    const withOverlap = Math.ceil(perChunk * (1 + overlapFrac));
    s.splitLastResult = { base, k, perChunk, withOverlap, overlap: s.splitOverlap };
    s.preview = [];
    setSampleStatus(
        `Will create ${k} chunks of ~${formatInt(withOverlap)} rows each from ${formatInt(base)} base rows.`,
        'ok'
    );
    renderSampleResult();
}

function setSampleStatus(message, kind) {
    const el = $('#sample-status');
    if (!el) return;
    el.textContent = message || '';
    el.className = 'muted small';
    if (kind) el.classList.add(kind);
}

function renderSampleResult() {
    const s = App.slicer.sample;
    const wrap = $('#sample-result-wrap');
    if (!wrap) return;

    // Split preview just shows a summary — no table.
    if (s.method === 'split') {
        if (!s.splitLastResult) { wrap.style.display = 'none'; return; }
        wrap.style.display = 'block';
        const r = s.splitLastResult;
        $('#sample-result-title').textContent = `Equal-chunk split preview`;
        $('#sample-result-subtitle').textContent =
            `${r.k} chunks of roughly ${formatInt(r.withOverlap)} rows each `
            + `(from ${formatInt(r.base)} base rows, ${r.overlap}% overlap). `
            + `Name and save all chunks below.`;
        $('#sample-result-table').innerHTML =
            `<thead><tr><th>Chunk</th><th>~Rows</th></tr></thead>`
            + `<tbody>${Array.from({length: r.k}, (_, i) =>
                `<tr><td>Chunk ${i + 1} of ${r.k}</td><td>${formatInt(r.withOverlap)}</td></tr>`
            ).join('')}</tbody>`;
        $('#sample-result-pagination').innerHTML = '';
        $('#btn-split-save').disabled = false;
        return;
    }

    if (!s.lastSpec) {
        wrap.style.display = 'none';
        return;
    }
    wrap.style.display = 'block';

    $('#sample-result-title').textContent = `Preview — ${formatInt(s.rowsSampled)} rows sampled`;
    $('#sample-result-subtitle').textContent =
        `${s.description} · ${formatInt(s.rowsSampled)} of ${formatInt(s.rowsInCorpus)} rows. `
        + `Name and save this sample to come back to it later.`;
    $('#btn-sample-save').disabled = s.rowsSampled === 0;

    const cols = (s.previewColumns || []).filter(c => c !== '_row_idx');
    const rows = s.preview || [];
    const thead = `<thead><tr>${cols.map(c => `<th>${escapeHtml(corpusColLabel(c))}</th>`).join('')}</tr></thead>`;
    let tbody;
    if (rows.length === 0) {
        tbody = `<tbody><tr><td colspan="${cols.length || 1}" class="muted small" style="text-align:center; padding:1.5rem;">No rows sampled.</td></tr></tbody>`;
    } else {
        tbody = '<tbody>' + rows.map(r => {
            const idx = r._row_idx;
            const cells = cols.map(c => renderCorpusCell(c, r[c])).join('');
            return `<tr class="row-clickable" data-idx="${escapeHtml(String(idx))}">${cells}</tr>`;
        }).join('') + '</tbody>';
    }
    $('#sample-result-table').innerHTML = thead + tbody;
    $$('#sample-result-table tbody tr.row-clickable').forEach(tr => {
        tr.addEventListener('click', ev => {
            if (ev.target.closest('a')) return;
            const idx = Number(tr.dataset.idx);
            if (!Number.isNaN(idx)) openRowModal(idx);
        });
    });

    renderSamplePagination();
}

function renderSamplePagination() {
    const el = $('#sample-result-pagination');
    if (!el) return;
    const s = App.slicer.sample;
    const total = s.rowsSampled;
    const size = s.pageSize;
    const page = s.page;
    const maxPage = Math.max(1, Math.ceil(total / size));
    if (total === 0 || maxPage <= 1) { el.innerHTML = ''; return; }

    const btn = (label, targetPage, { disabled = false, active = false } = {}) =>
        `<button data-page="${targetPage}" class="${active ? 'active' : ''}" ${disabled ? 'disabled' : ''}>${label}</button>`;
    const pages = [];
    const w = 2;
    pages.push(1);
    for (let p = Math.max(2, page - w); p <= Math.min(maxPage - 1, page + w); p++) pages.push(p);
    if (maxPage > 1) pages.push(maxPage);
    const uniq = [...new Set(pages)].sort((a, b) => a - b);
    let html = btn('<i class="fa-solid fa-angle-left"></i>', page - 1, { disabled: page <= 1 });
    let prev = 0;
    for (const p of uniq) {
        if (p - prev > 1) html += `<span class="pag-info">…</span>`;
        html += btn(String(p), p, { active: p === page });
        prev = p;
    }
    html += btn('<i class="fa-solid fa-angle-right"></i>', page + 1, { disabled: page >= maxPage });
    html += `<span class="pag-info">Page ${page} of ${maxPage} · ${formatInt(total)} rows</span>`;
    el.innerHTML = html;
    $$('#sample-result-pagination button[data-page]').forEach(b => {
        b.addEventListener('click', () => {
            const p = Number(b.dataset.page);
            if (!Number.isNaN(p) && p !== App.slicer.sample.page) runSamplePreview(p);
        });
    });
}

async function saveSampleAsSlice() {
    const s = App.slicer.sample;
    const nameEl = $('#sample-name-input');
    const name = (nameEl?.value || '').trim();
    if (!s.lastSpec) { setSampleStatus('Preview a sample first.', 'err'); return; }
    if (!name) { setSampleStatus('Give the sample a name before saving.', 'err'); nameEl?.focus(); return; }
    try {
        const body = { ...buildSampleSpecBody(1), name };
        await fetchJson('/api/slices/sample', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        nameEl.value = '';
        setSampleStatus(`Saved slice "${name}".`, 'ok');
        await refreshState();
    } catch (err) {
        setSampleStatus(`Save failed: ${err.message}`, 'err');
    }
}

async function saveSplitAsSlices() {
    readSampleInputs();
    const s = App.slicer.sample;
    const prefixEl = $('#split-prefix-input');
    const prefix = (prefixEl?.value || '').trim();
    if (!prefix) { setSampleStatus('Give the chunks a name prefix.', 'err'); prefixEl?.focus(); return; }
    const k = s.splitK;
    if (!confirm(`Create ${k} new slices named "${prefix} — chunk 1 of ${k}" … "${prefix} — chunk ${k} of ${k}"?`)) return;
    try {
        await fetchJson('/api/slices/split', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name_prefix: prefix,
                k,
                seed: s.seed,
                overlap_pct: s.splitOverlap,
                base_query: s.baseQuery,
            }),
        });
        prefixEl.value = '';
        setSampleStatus(`Saved ${k} chunks.`, 'ok');
        await refreshState();
    } catch (err) {
        setSampleStatus(`Split failed: ${err.message}`, 'err');
    }
}

function resetSampleForm() {
    const s = App.slicer.sample;
    s.n = 200; s.seed = 42; s.byCol = ''; s.ascending = false; s.step = 10;
    s.splitK = 2; s.splitOverlap = 0; s.baseQuery = ''; s.dedupe = false;
    s.lastSpec = null; s.preview = []; s.rowsSampled = 0; s.splitLastResult = null;

    $('#sample-n').value            = '200';
    $('#sample-seed').value         = '42';
    $('#sample-ascending').value    = 'false';
    $('#sample-step').value         = '10';
    $('#split-k').value             = '2';
    $('#split-overlap').value       = '0';
    $('#sample-base-query').value   = '';
    $('#sample-dedupe').checked     = false;
    $('#sample-name-input').value   = '';
    $('#split-prefix-input').value  = '';
    setSampleStatus('', '');
    renderSampleCard();
}

async function composeSlicesAs(op) {
    const a = App.slicer.diffA, b = App.slicer.diffB;
    if (!a || !b || a === b) return;
    const sa = App.state?.slices?.[a];
    const sb = App.state?.slices?.[b];
    if (!sa || !sb) return;
    const labels = {
        and:     `${sa.name} ∩ ${sb.name}`,
        or:      `${sa.name} ∪ ${sb.name}`,
        and_not: `${sa.name} − ${sb.name}`,
        or_not:  `${sa.name} ∪ ¬${sb.name}`,
    };
    const suggested = labels[op] || `${sa.name} ${op} ${sb.name}`;
    const name = prompt(`Save combined slice as:`, suggested);
    if (!name || !name.trim()) return;
    try {
        await fetchJson('/api/slices/compose', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slice_a_id: a, slice_b_id: b, op, name: name.trim() }),
        });
        await refreshState();
    } catch (err) {
        const el = $('#slicer-diff-result');
        if (el) {
            const note = document.createElement('p');
            note.className = 'muted small';
            note.style.color = '#b42318';
            note.textContent = `Compose failed: ${err.message}`;
            el.appendChild(note);
        }
    }
}

async function runSlicePreview(page = 1) {
    const input = $('#slicer-query-input');
    const query = (input?.value || '').trim();
    App.slicer.query = query;
    App.slicer.page = page;
    App.slicer.errorMessage = '';

    if (!query) {
        App.slicer.lastRunQuery = '';
        App.slicer.matched = 0;
        App.slicer.preview = [];
        App.slicer.previewColumns = [];
        setSlicerStatus('Type a query, then press Run.', '');
        $('#slicer-result-card').style.display = 'none';
        $('#btn-slicer-save').disabled = true;
        return;
    }

    setSlicerStatus('Running…', '');
    App.slicer.running = true;
    try {
        const data = await fetchJson('/api/slices/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query, page, page_size: App.slicer.pageSize }),
        });
        App.slicer.lastRunQuery = query;
        App.slicer.matched = data.rows_matched;
        App.slicer.totalInCorpus = data.rows_in_corpus;
        App.slicer.preview = data.preview || [];
        App.slicer.previewColumns = data.preview_columns || [];
        App.slicer.errorMessage = '';
        setSlicerStatus(`${formatInt(data.rows_matched)} of ${formatInt(data.rows_in_corpus)} rows match.`, 'ok');
        renderSlicerResult();
    } catch (err) {
        App.slicer.errorMessage = err.message || String(err);
        App.slicer.matched = 0;
        App.slicer.preview = [];
        setSlicerStatus(App.slicer.errorMessage, 'err');
        $('#slicer-result-card').style.display = 'none';
        $('#btn-slicer-save').disabled = true;
    } finally {
        App.slicer.running = false;
    }
}

function setSlicerStatus(message, kind) {
    const el = $('#slicer-status');
    if (!el) return;
    el.textContent = message || '';
    el.className = 'muted small';
    if (kind) el.classList.add(kind);
}

function renderSlicerResult() {
    const card = $('#slicer-result-card');
    if (!App.slicer.lastRunQuery) {
        card.style.display = 'none';
        return;
    }
    card.style.display = 'block';

    const matched = App.slicer.matched;
    const total = App.slicer.totalInCorpus;
    const pct = total ? ((matched / total) * 100).toFixed(1) : '0.0';
    $('#slicer-result-title').textContent = `Preview — ${formatInt(matched)} rows match`;
    $('#slicer-result-subtitle').textContent =
        `${formatInt(matched)} of ${formatInt(total)} rows (${pct}%). `
        + `Name and save this slice to come back to it later.`;
    $('#btn-slicer-save').disabled = matched === 0;

    const table = $('#slicer-result-table');
    const cols = (App.slicer.previewColumns || []).filter(c => c !== '_row_idx');
    const rows = App.slicer.preview || [];

    const thead = `<thead><tr>${cols.map(c => `<th>${escapeHtml(corpusColLabel(c))}</th>`).join('')}</tr></thead>`;
    let tbody;
    if (rows.length === 0) {
        tbody = `<tbody><tr><td colspan="${cols.length || 1}" style="padding:0;">
          <div class="empty-state">
            <i class="fa-solid fa-filter-circle-xmark"></i>
            <h4>No rows match this query</h4>
            <p>Try broadening your terms, removing AND clauses, or widening the date range. Use <code>~N</code> for proximity and <code>*</code> for wildcards.</p>
          </div>
        </td></tr></tbody>`;
    } else {
        tbody = '<tbody>' + rows.map(r => {
            const idx = r._row_idx;
            const cells = cols.map(c => renderCorpusCell(c, r[c])).join('');
            return `<tr class="row-clickable" data-idx="${escapeHtml(String(idx))}">${cells}</tr>`;
        }).join('') + '</tbody>';
    }
    table.innerHTML = thead + tbody;
    $$('#slicer-result-table tbody tr.row-clickable').forEach(tr => {
        tr.addEventListener('click', ev => {
            if (ev.target.closest('a')) return;
            const idx = Number(tr.dataset.idx);
            if (!Number.isNaN(idx)) openRowModal(idx);
        });
    });

    renderSlicerPagination();
}

function renderSlicerPagination() {
    const el = $('#slicer-result-pagination');
    if (!el) return;
    const total = App.slicer.matched;
    const size = App.slicer.pageSize;
    const page = App.slicer.page;
    const maxPage = Math.max(1, Math.ceil(total / size));
    if (total === 0 || maxPage <= 1) { el.innerHTML = ''; return; }

    const btn = (label, targetPage, { disabled = false, active = false } = {}) =>
        `<button data-page="${targetPage}" class="${active ? 'active' : ''}" ${disabled ? 'disabled' : ''}>${label}</button>`;

    const pages = [];
    const window_ = 2;
    pages.push(1);
    for (let p = Math.max(2, page - window_); p <= Math.min(maxPage - 1, page + window_); p++) pages.push(p);
    if (maxPage > 1) pages.push(maxPage);
    const uniq = [...new Set(pages)].sort((a, b) => a - b);

    let html = btn('<i class="fa-solid fa-angle-left"></i>', page - 1, { disabled: page <= 1 });
    let prev = 0;
    for (const p of uniq) {
        if (p - prev > 1) html += `<span class="pag-info">…</span>`;
        html += btn(String(p), p, { active: p === page });
        prev = p;
    }
    html += btn('<i class="fa-solid fa-angle-right"></i>', page + 1, { disabled: page >= maxPage });
    html += `<span class="pag-info">Page ${page} of ${maxPage} · ${formatInt(total)} matches</span>`;
    el.innerHTML = html;
    $$('#slicer-result-pagination button[data-page]').forEach(b => {
        b.addEventListener('click', () => {
            const p = Number(b.dataset.page);
            if (!Number.isNaN(p) && p !== App.slicer.page) runSlicePreview(p);
        });
    });
}

async function saveSlice() {
    const nameEl = $('#slice-name-input');
    const query = App.slicer.lastRunQuery;
    const name = (nameEl?.value || '').trim();
    if (!query) {
        setSlicerStatus('Run a preview first.', 'err');
        return;
    }
    if (!name) {
        setSlicerStatus('Give the slice a name before saving.', 'err');
        nameEl?.focus();
        return;
    }
    try {
        await fetchJson('/api/slices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, query }),
        });
        nameEl.value = '';
        setSlicerStatus(`Saved slice "${name}".`, 'ok');
        await refreshState();
    } catch (err) {
        setSlicerStatus(`Save failed: ${err.message}`, 'err');
    }
}

function renderSavedSlices(state) {
    const container = $('#slicer-saved-list');
    const countEl = $('#slicer-saved-count');
    if (!container) return;
    const slices = Object.values(state?.slices || {});
    slices.sort((a, b) => (a.created_at > b.created_at ? -1 : 1));
    countEl.textContent = slices.length === 0 ? 'None yet' : `${slices.length} saved`;

    if (slices.length === 0) {
        container.innerHTML = '<p class="muted small">No slices saved yet. Run a query above and click <strong>Save slice</strong>.</p>';
        return;
    }

    const kindLabel = { query: 'query', sample: 'frozen', compose: 'combo' };
    container.innerHTML = slices.map(s => {
        const total = App.slicer.totalInCorpus || state?.corpus?.rows || 0;
        const pct = total ? ((s.row_count / total) * 100).toFixed(1) : '0.0';
        const kind = s.kind || 'query';
        const isQuery = kind === 'query';
        const loadTitle = isQuery ? 'Load into query box' : 'Frozen slice — cannot be edited';
        const warnings = [];
        if (s.invalidated_at) {
            warnings.push(`<div class="slice-warning" title="${escapeHtml(s.invalidated_reason || 'Frozen indices no longer reference the same rows.')}"><i class="fa-solid fa-triangle-exclamation"></i> Stale since ${escapeHtml(formatCorpusDate(s.invalidated_at))} — re-sample to refresh.</div>`);
        }
        if (s.dangling) {
            const miss = (s.parent_missing || []).join(', ');
            warnings.push(`<div class="slice-warning" title="Composed slice references slices that no longer exist: ${escapeHtml(miss)}"><i class="fa-solid fa-link-slash"></i> Dangling parent slice — re-evaluation may be incomplete.</div>`);
        }
        return `
            <div class="slice-item${s.invalidated_at || s.dangling ? ' slice-item-warn' : ''}" data-slice-id="${escapeHtml(s.slice_id)}">
                <div class="sl-main">
                    <div class="sl-name">
                        ${escapeHtml(s.name)}
                        <span class="slice-kind-badge kind-${escapeHtml(kind)}">${escapeHtml(kindLabel[kind] || kind)}</span>
                    </div>
                    <div class="sl-query">${escapeHtml(s.query)}</div>
                    <div class="sl-meta">Saved ${escapeHtml(formatCorpusDate(s.created_at))}</div>
                    ${warnings.join('')}
                </div>
                <div style="text-align:right;">
                    <div class="sl-count">${formatInt(s.row_count)}</div>
                    <div class="muted small">${pct}% of corpus</div>
                </div>
                <div class="sl-actions">
                    <button class="btn-secondary" data-action="load" title="${escapeHtml(loadTitle)}"${isQuery ? '' : ' disabled'}>
                        <i class="fa-solid fa-arrow-up-from-bracket"></i> Load
                    </button>
                    <button class="btn-secondary" data-action="export" title="Download slice as CSV">
                        <i class="fa-solid fa-file-csv"></i>
                    </button>
                    <button class="btn-secondary btn-danger" data-action="delete" title="Delete slice">
                        <i class="fa-solid fa-trash"></i>
                    </button>
                </div>
            </div>`;
    }).join('');

    $$('#slicer-saved-list .slice-item').forEach(item => {
        const id = item.dataset.sliceId;
        item.querySelector('[data-action="load"]')?.addEventListener('click', () => loadSliceIntoEditor(id));
        item.querySelector('[data-action="export"]')?.addEventListener('click', () => exportSliceCsv(id));
        item.querySelector('[data-action="delete"]')?.addEventListener('click', () => deleteSavedSlice(id));
    });
}

function exportSliceCsv(sliceId) {
    window.location.href = `/api/slices/${encodeURIComponent(sliceId)}/export.csv`;
}

function loadSliceIntoEditor(sliceId) {
    const sd = App.state?.slices?.[sliceId];
    if (!sd) return;
    const input = $('#slicer-query-input');
    if (input) { input.value = sd.query; input.focus(); }
    App.slicer.query = sd.query;
    runSlicePreview(1);
    $('#slicer-builder-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function deleteSavedSlice(sliceId) {
    const sd = App.state?.slices?.[sliceId];
    if (!sd) return;
    if (!confirm(`Delete the slice "${sd.name}"?`)) return;
    const alsoPurge = confirm(
        `Also purge tags and AI classifications for rows that live ONLY in "${sd.name}"?\n\n` +
        `OK   — purge those tags/classifications too (irreversible).\n` +
        `Cancel — keep the tags/classifications on those rows.`
    );
    const url = `/api/slices/${encodeURIComponent(sliceId)}${alsoPurge ? '?purge_tags=true' : ''}`;
    try {
        const resp = await fetchJson(url, { method: 'DELETE' });
        await refreshState();
        const bits = [`Deleted "${sd.name}".`];
        if (alsoPurge && resp.purged_rows) bits.push(`${resp.purged_rows} row(s) of tags purged.`);
        setSlicerStatus(bits.join(' '), 'ok');
    } catch (err) {
        setSlicerStatus(`Delete failed: ${err.message}`, 'err');
    }
}

function renderDiffCard(state) {
    const slices = Object.values(state?.slices || {});
    const card = $('#slicer-diff-card');
    if (!card) return;
    if (slices.length < 2) { card.style.display = 'none'; return; }
    card.style.display = 'block';

    const a = $('#slicer-diff-a');
    const b = $('#slicer-diff-b');
    const opts = slices.map(s => `<option value="${escapeHtml(s.slice_id)}">${escapeHtml(s.name)} (${formatInt(s.row_count)})</option>`).join('');
    a.innerHTML = opts;
    b.innerHTML = opts;
    if (!App.slicer.diffA || !state.slices[App.slicer.diffA]) App.slicer.diffA = slices[0].slice_id;
    if (!App.slicer.diffB || !state.slices[App.slicer.diffB]) App.slicer.diffB = slices[1].slice_id;
    a.value = App.slicer.diffA;
    b.value = App.slicer.diffB;

    renderDiffResult();
}

function renderDiffResult() {
    const el = $('#slicer-diff-result');
    if (!el) return;
    const d = App.slicer.diff;
    if (!d) {
        el.innerHTML = '<p class="muted small">Pick two slices and click <strong>Compare</strong> to see the overlap.</p>';
        return;
    }
    const total = d.rows_in_corpus || 1;
    const tile = (label, value, hint, op, accent = false) => `
        <div class="stat-tile${accent ? ' accent' : ''}">
            <div class="stat-label">${escapeHtml(label)}</div>
            <div class="stat-value">${formatInt(value)}</div>
            <div class="muted small">${escapeHtml(hint)}</div>
            ${op ? `<button class="compose-btn" data-op="${escapeHtml(op)}" title="Save this set as a new slice">
                <i class="fa-solid fa-floppy-disk"></i> Save as slice
            </button>` : ''}
        </div>`;
    const pct = v => ((v / total) * 100).toFixed(1) + '%';
    el.innerHTML = `
        <div class="slicer-diff-venn">
            ${tile(d.a.name, d.a.rows, pct(d.a.rows), null)}
            ${tile(d.b.name, d.b.rows, pct(d.b.rows), null)}
            ${tile('In both (A ∩ B)', d.both, pct(d.both), 'and', true)}
            ${tile('A only', d.a_only, pct(d.a_only), 'and_not')}
            ${tile('B only', d.b_only, pct(d.b_only), 'b_only')}
            ${tile('Either (A ∪ B)', d.either, pct(d.either), 'or')}
        </div>`;
    $$('#slicer-diff-result .compose-btn').forEach(b => {
        b.addEventListener('click', () => {
            const op = b.dataset.op;
            if (op === 'b_only') {
                // "B only" == B AND NOT A → flip A/B then compose with and_not
                const a = App.slicer.diffA, bId = App.slicer.diffB;
                App.slicer.diffA = bId; App.slicer.diffB = a;
                composeSlicesAs('and_not').finally(() => {
                    App.slicer.diffA = a; App.slicer.diffB = bId;
                });
            } else {
                composeSlicesAs(op);
            }
        });
    });
}

async function runSliceDiff() {
    const aId = $('#slicer-diff-a')?.value;
    const bId = $('#slicer-diff-b')?.value;
    if (!aId || !bId) return;
    if (aId === bId) {
        App.slicer.diff = null;
        const el = $('#slicer-diff-result');
        if (el) el.innerHTML = '<p class="muted small" style="color:#b42318;">Pick two <em>different</em> slices.</p>';
        return;
    }
    App.slicer.diffA = aId;
    App.slicer.diffB = bId;
    try {
        const data = await fetchJson('/api/slices/diff', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slice_a_id: aId, slice_b_id: bId }),
        });
        App.slicer.diff = data;
        renderDiffResult();
    } catch (err) {
        App.slicer.diff = null;
        const el = $('#slicer-diff-result');
        if (el) el.innerHTML = `<p class="muted small" style="color:#b42318;">Diff failed: ${escapeHtml(err.message)}</p>`;
    }
}

/* ── 6.6 CODEBOOK + CODING (Phase 4) ────────────────────────────────────── */
function activeCodebook(state) {
    const id = state?.active_codebook;
    if (!id) return null;
    const cb = state?.codebooks?.[id];
    return cb || null;
}

function renderCodebook(state) {
    if (!state) return;
    const cb = activeCodebook(state);
    const codebooks = Object.values(state.codebooks || {});
    const hasCoder = !!(state.coding?.coder_name);

    $('#cb-coder-banner').style.display = hasCoder ? 'none' : 'block';
    $('#cb-empty-card').style.display    = codebooks.length === 0 ? 'block' : 'none';
    $('#cb-list-card').style.display     = codebooks.length > 0 ? 'block' : 'none';
    $('#cb-editor-card').style.display   = cb ? 'block' : 'none';
    $('#cb-scope-card').style.display    = (cb && state.corpus?.built) ? 'block' : 'none';
    $('#cb-coding-card').style.display   = (cb && state.corpus?.built && hasCoder) ? 'block' : 'none';
    $('#cb-bulk-card').style.display     = (cb && state.corpus?.built && hasCoder) ? 'block' : 'none';
    $('#cb-irr-card').style.display      = cb ? 'block' : 'none';

    renderCodebookList(state);
    if (cb) {
        renderCodebookEditor(state, cb);
        renderScopePicker(state);
        renderCodingUI(state, cb);
        renderBulkPanel(state, cb);
        renderIrrPanel(state, cb);
    }
    bindCodingKeys();
}

function renderCodebookList(state) {
    const wrap = $('#cb-list');
    if (!wrap) return;
    const codebooks = Object.values(state.codebooks || {});
    if (codebooks.length === 0) { wrap.innerHTML = ''; return; }
    const active = state.active_codebook || '';
    wrap.innerHTML = codebooks.map(cb => {
        const isActive = cb.codebook_id === active;
        return `
          <div class="cb-list-row${isActive ? ' active' : ''}" data-cb="${escapeHtml(cb.codebook_id)}">
            <div>
              <strong>${escapeHtml(cb.name)}</strong>
              <span class="muted small">  v${escapeHtml(cb.version || '1.0')} · ${cb.categories.length} cats</span>
            </div>
            <div class="flex-row" style="gap:0.35rem; flex-wrap:wrap;">
              ${isActive
                ? '<span class="chip active" style="pointer-events:none;">Active</span>'
                : `<button class="btn-secondary" data-cb-activate="${escapeHtml(cb.codebook_id)}">Activate</button>`}
            </div>
          </div>`;
    }).join('');
    wrap.querySelectorAll('[data-cb-activate]').forEach(btn => {
        btn.addEventListener('click', () => activateCodebook(btn.dataset.cbActivate));
    });
}

function renderCodebookEditor(state, cb) {
    $('#cb-editor-title').textContent = cb.name;
    $('#cb-editor-subtitle').textContent =
        `ID ${cb.codebook_id} · v${cb.version || '1.0'} · ${cb.categories.length} categor${cb.categories.length === 1 ? 'y' : 'ies'}`;

    // Warnings
    const warnWrap = $('#cb-warnings');
    const warnings = codebookWarnings(cb);
    warnWrap.innerHTML = warnings.length
        ? `<div class="phase-stub" style="color:#b42318;"><span class="stub-tag">Warnings</span><ul>${warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('')}</ul></div>`
        : '';

    // Category list
    const listEl = $('#cb-cat-list');
    if (cb.categories.length === 0) {
        listEl.innerHTML = `<p class="muted small">No categories yet — add one below to start tagging.</p>`;
    } else {
        listEl.innerHTML = `
          <table class="cb-cat-table">
            <thead><tr>
              <th style="width:7%;">Key</th>
              <th>Title</th>
              <th>Description</th>
              <th style="width:15%;">Group</th>
              <th style="width:6%;">Color</th>
              <th style="width:10%;"></th>
            </tr></thead>
            <tbody>
              ${cb.categories.map(c => `
                <tr data-cat="${escapeHtml(c.cat_id)}">
                  <td><kbd>${escapeHtml(c.shortcut_key || '—')}</kbd></td>
                  <td><strong>${escapeHtml(c.title)}</strong><div class="muted small">${escapeHtml(c.cat_id)}</div></td>
                  <td class="muted small">${escapeHtml(c.description || '')}</td>
                  <td class="muted small">${escapeHtml(c.exclusion_group || '—')}</td>
                  <td><span class="cb-color-dot" style="background:${escapeHtml(c.color || '#C8175D')};"></span></td>
                  <td><button class="btn-secondary btn-danger" data-cat-remove="${escapeHtml(c.cat_id)}">Remove</button></td>
                </tr>`).join('')}
            </tbody>
          </table>`;
        listEl.querySelectorAll('[data-cat-remove]').forEach(btn => {
            btn.addEventListener('click', () => removeCategory(btn.dataset.catRemove));
        });
    }

    // Populate Regex category dropdown
    const regexCat = $('#cb-regex-category');
    if (regexCat) {
        regexCat.innerHTML = cb.categories.map(c => `<option value="${escapeHtml(c.cat_id)}">${escapeHtml(c.title)}</option>`).join('');
    }
}

function codebookWarnings(cb) {
    const ws = [];
    const keyMap = {};
    for (const c of cb.categories) {
        const k = (c.shortcut_key || '').toLowerCase();
        if (!k) continue;
        keyMap[k] = (keyMap[k] || []).concat(c.cat_id);
    }
    Object.entries(keyMap).forEach(([k, ids]) => {
        if (ids.length > 1) ws.push(`Shortcut '${k}' used by multiple categories: ${ids.join(', ')}`);
    });
    return ws;
}

function renderScopePicker(state) {
    const sel = $('#cb-scope-select');
    const slices = Object.values(state.slices || {});
    const prev = App.codebook.scopeSliceId;
    sel.innerHTML = `<option value="">Whole corpus</option>` +
        slices.map(s => `<option value="${escapeHtml(s.slice_id)}">${escapeHtml(s.name)} (${formatInt(s.rows_matched || 0)})</option>`).join('');
    sel.value = prev || (state.coding?.coding_slice_id || '');
    App.codebook.scopeSliceId = sel.value;
    renderProgressBlock(state);
    // Lazy-load scope rows if we don't have them yet and a row hasn't been focused.
    if (App.codebook.scopeRows.length === 0 && state.corpus?.built) {
        loadScopeRows();
    }
}

/* ── STANDARD CODEBOOK LIBRARY & REGEX PRE-CODING ──────────────────────── */
const STANDARD_LIBRARY = [
    {
        id: 'un-hate-speech',
        title: 'UN Hate Speech Framework',
        desc: 'Based on UN guidelines for distinguishing severe hate speech from offensive or legitimate speech.',
        categories: [
            { cat_id: 'hs_severe', title: 'Severe Hate Speech', description: 'Direct incitement to violence or discrimination against a protected group.', shortcut_key: '1', color: '#c91432', exclusion_group: 'type' },
            { cat_id: 'hs_offensive', title: 'Offensive Speech', description: 'Insulting or derogatory language that does not reach the threshold of hate speech.', shortcut_key: '2', color: '#f78f1e', exclusion_group: 'type' },
            { cat_id: 'hs_legit', title: 'Legitimate Expression', description: 'Political commentary, criticism, or general discussion without hate speech indicators.', shortcut_key: '3', color: '#108a00', exclusion_group: 'type' }
        ]
    },
    {
        id: 'sentiment-basic',
        title: 'Basic Sentiment Analysis',
        desc: 'Standard three-way sentiment classification.',
        categories: [
            { cat_id: 'sent_pos', title: 'Positive', description: 'Expresses a favorable or positive opinion.', shortcut_key: 'p', color: '#108a00', exclusion_group: 'sentiment' },
            { cat_id: 'sent_neu', title: 'Neutral', description: 'Objective statement or lacking clear sentiment.', shortcut_key: 'n', color: '#666666', exclusion_group: 'sentiment' },
            { cat_id: 'sent_neg', title: 'Negative', description: 'Expresses an unfavorable or negative opinion.', shortcut_key: 'm', color: '#c91432', exclusion_group: 'sentiment' }
        ]
    }
];

function setupCodebookLibrary() {
    const btn = $('#btn-cb-library');
    const modal = $('#cb-library-modal');
    const closeBtn = $('#cb-library-close');
    const mask = $('#cb-library-close-mask');
    const grid = $('#cb-library-grid');
    if (!btn || !modal) return;
    
    grid.innerHTML = STANDARD_LIBRARY.map(lib => `
        <div class="glass-card flex-row" style="justify-content:space-between; align-items:center; padding:1rem;">
            <div>
                <strong>${escapeHtml(lib.title)}</strong>
                <p class="muted small" style="margin:0.25rem 0 0 0;">${escapeHtml(lib.desc)}</p>
                <div class="muted small mt-1">${lib.categories.length} categories</div>
            </div>
            <button class="btn-primary btn-sm btn-load-lib" data-lib="${lib.id}">Use this</button>
        </div>
    `).join('');
    
    grid.querySelectorAll('.btn-load-lib').forEach(b => {
        b.addEventListener('click', async () => {
            const lib = STANDARD_LIBRARY.find(x => x.id === b.dataset.lib);
            if (!lib) return;
            modal.hidden = true;
            await loadStandardCodebook(lib);
        });
    });

    btn.addEventListener('click', () => modal.hidden = false);
    closeBtn.addEventListener('click', () => modal.hidden = true);
    mask.addEventListener('click', () => modal.hidden = true);
}

async function loadStandardCodebook(lib) {
    try {
        const payload = {
            name: lib.title,
            goal: lib.desc,
            categories: lib.categories
        };
        const resp = await fetchJson('/api/codebooks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        App.state.active_codebook = resp.codebook.codebook_id;
        await refreshState();
        renderCodebook(App.state);
    } catch (err) {
        alert('Failed to load codebook: ' + err.message);
    }
}

function setupRegexPrecoding() {
    $('#btn-cb-run-regex')?.addEventListener('click', runRegexPrecoding);
}

async function runRegexPrecoding() {
    const pattern = $('#cb-regex-pattern')?.value.trim();
    const catId = $('#cb-regex-category')?.value;
    const status = $('#cb-regex-status');
    if (!pattern || !catId) {
        if(status) status.textContent = 'Please provide a pattern and select a category.';
        return;
    }
    
    const state = App.state;
    const coder = state.coding?.coder_name;
    if (!coder) { alert('Set your coder name in Settings first.'); return; }
    
    const cb = activeCodebook(state);
    if (!cb) return;
    
    if (status) status.textContent = 'Running auto-tag...';
    
    try {
        const query = pattern;
        const data = await fetchJson('/api/coding/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                cat_id: catId,
                query: query,
                dry_run: false
            })
        });
        
        if (status) {
            status.innerHTML = `<span style="color:var(--isd-blue);"><i class="fa-solid fa-check"></i> Success! Tagged ${formatInt(data.patch ? Object.keys(data.patch).length : 0)} rows.</span>`;
            setTimeout(() => { if(status) status.textContent = ''; }, 5000);
        }
        await refreshState();
        await loadProgress();
        renderCodebook(App.state);
    } catch (err) {
        if (status) status.textContent = 'Auto-tag failed: ' + err.message;
    }
}

function renderProgressBlock(state) {
    const p = App.codebook.progress;
    const el = $('#cb-progress');
    const summaryEl = $('#cb-scope-summary');
    if (!p) {
        el.innerHTML = '<p class="muted small">Loading progress…</p>';
        summaryEl.textContent = '';
        return;
    }
    const tagged = p.rows_tagged || 0;
    const total = p.rows_in_scope || state.corpus?.rows || 0;
    const pct = total > 0 ? Math.min(100, Math.round((tagged / total) * 100)) : 0;
    summaryEl.textContent = total ? `${formatInt(tagged)} / ${formatInt(total)} rows tagged (${pct}%)` : '';
    const perCat = p.by_category || {};
    const perCoder = p.by_coder || {};
    el.innerHTML = `
      <div class="cb-progress-bar"><div class="cb-progress-fill" style="width:${pct}%"></div></div>
      <div class="flex-row mt-2" style="gap:1.5rem; flex-wrap:wrap;">
        <div>
          <div class="muted small">By category</div>
          ${Object.keys(perCat).length
            ? Object.entries(perCat).map(([k, v]) => `<div><strong>${escapeHtml(k)}</strong>: ${formatInt(v)}</div>`).join('')
            : '<div class="muted small">—</div>'}
        </div>
        <div>
          <div class="muted small">By coder</div>
          ${Object.keys(perCoder).length
            ? Object.entries(perCoder).map(([k, v]) => `<div><strong>${escapeHtml(k)}</strong>: ${formatInt(v)} rows</div>`).join('')
            : '<div class="muted small">—</div>'}
        </div>
      </div>`;
}

function renderCodingUI(state, cb) {
    const ri = App.codebook.focusRowIdx;
    const data = App.codebook.focusRowData;
    const titleEl = $('#cb-focus-title');
    const subEl = $('#cb-focus-subtitle');
    const bodyEl = $('#cb-focus-body');

    if (ri === null || ri === undefined) {
        titleEl.textContent = 'No row selected';
        subEl.textContent = 'Pick a scope above and press Next, or type a row number.';
        bodyEl.innerHTML = '';
    } else if (data) {
        titleEl.textContent = `Row ${formatInt(ri)}`;
        const pos = (App.codebook.scopeRows.indexOf(ri) + 1) || 0;
        const total = App.codebook.scopeRows.length;
        subEl.textContent = pos ? `Position ${pos} of ${formatInt(total)} in scope` : '';
        bodyEl.innerHTML = renderFocusRowBody(data);
    }

    // Category buttons
    const btnWrap = $('#cb-cat-buttons');
    const mine = new Set(
        (App.codebook.rowTags || [])
            .filter(t => t.coder === state.coding?.coder_name && t.codebook_id === cb.codebook_id)
            .map(t => t.cat_id)
    );
    if (cb.categories.length === 0) {
        btnWrap.innerHTML = '<p class="muted small">Add at least one category above.</p>';
    } else {
        btnWrap.innerHTML = cb.categories.map(c => {
            const placed = mine.has(c.cat_id);
            const color = c.color || '#C8175D';
            return `<button class="cb-cat-btn${placed ? ' placed' : ''}"
                            data-cat-tag="${escapeHtml(c.cat_id)}"
                            style="--cat-color:${escapeHtml(color)};"
                            title="${escapeHtml(c.description || '')}">
                      ${c.shortcut_key ? `<kbd>${escapeHtml(c.shortcut_key)}</kbd>` : ''}
                      <span>${escapeHtml(c.title)}</span>
                      ${placed ? '<i class="fa-solid fa-check"></i>' : ''}
                    </button>`;
        }).join('');
        btnWrap.querySelectorAll('[data-cat-tag]').forEach(btn => {
            btn.addEventListener('click', () => toggleTag(btn.dataset.catTag));
        });
    }

    // Existing tags
    const tagsEl = $('#cb-row-tags');
    const tags = App.codebook.rowTags || [];
    if (!tags.length) {
        tagsEl.innerHTML = '<p class="muted small">No tags on this row yet.</p>';
    } else {
        tagsEl.innerHTML = tags.map(t => {
            const cat = cb.categories.find(c => c.cat_id === t.cat_id);
            const title = cat ? cat.title : t.cat_id;
            return `<span class="cb-tag-pill">
                      <strong>${escapeHtml(title)}</strong>
                      <span class="muted small">by ${escapeHtml(t.coder || '?')} · ${escapeHtml(t.source || 'manual')}${t.ts ? ' · ' + escapeHtml(t.ts) : ''}</span>
                    </span>`;
        }).join('');
    }

    $('#btn-cb-undo').disabled = !(state.coding?.undo_available > 0);
}

function renderFocusRowBody(row) {
    if (!row || typeof row !== 'object') return '';
    const parts = [];
    const text = row.text ?? '';
    if (text) {
        parts.push(`<div class="cb-focus-text">${escapeHtml(String(text))}</div>`);
    }
    const meta = [];
    if (row.platform)      meta.push(`<strong>${escapeHtml(row.platform)}</strong>`);
    if (row.author_handle) meta.push(`@${escapeHtml(row.author_handle)}`);
    if (row.language)      meta.push(escapeHtml(row.language));
    if (row.created_at)    meta.push(escapeHtml(String(row.created_at).slice(0, 19).replace('T', ' ')));
    if (row.url)           meta.push(`<a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">Open source</a>`);
    if (meta.length) parts.push(`<div class="muted small mt-1">${meta.join(' · ')}</div>`);
    return parts.join('');
}

function renderBulkPanel(state, cb) {
    const catSel = $('#cb-bulk-cat');
    const sliceSel = $('#cb-bulk-slice');
    const sourceSel = $('#cb-bulk-source');
    catSel.innerHTML = cb.categories.map(c => `<option value="${escapeHtml(c.cat_id)}">${escapeHtml(c.title)}</option>`).join('');
    const slices = Object.values(state.slices || {});
    sliceSel.innerHTML = slices.map(s => `<option value="${escapeHtml(s.slice_id)}">${escapeHtml(s.name)} (${formatInt(s.rows_matched || 0)})</option>`).join('');
    const mode = App.codebook.bulkMode;
    sourceSel.value = mode;
    $('#cb-bulk-query-wrap').style.display = mode === 'query' ? 'block' : 'none';
    $('#cb-bulk-slice-wrap').style.display = mode === 'slice' ? 'block' : 'none';
}

function renderIrrPanel(state, cb) {
    const coders = state.coding?.coders || {};
    const names = Object.keys(coders);
    const el = $('#cb-irr-coders');
    if (names.length === 0) {
        el.innerHTML = '<p class="muted small">No coder has tagged anything in this codebook yet.</p>';
    } else {
        el.innerHTML = `
          <p class="muted small">Coders active in <strong>${escapeHtml(cb.name)}</strong>:</p>
          <div class="flex-row mt-1" style="gap:0.4rem; flex-wrap:wrap;">
            ${names.map(n => `<span class="chip">${escapeHtml(n)} <span class="chip-count">${formatInt(coders[n])}</span></span>`).join('')}
          </div>`;
    }

    const res = App.codebook.irr;
    const rwrap = $('#cb-irr-result');
    if (!res) { rwrap.innerHTML = ''; return; }
    if (res.ok === false) {
        rwrap.innerHTML = `<p class="muted small">${escapeHtml(res.message || 'Not enough data for IRR.')}</p>`;
        return;
    }
    const headline = res.method === 'cohens_kappa'
        ? `<p><strong>Cohen's κ</strong> — ${escapeHtml(res.coder_a)} vs ${escapeHtml(res.coder_b)} · overlap: ${formatInt(res.overlap_rows || 0)} rows</p>
           <p>Weighted κ across categories: <strong>${fmtScore(res.weighted_kappa)}</strong></p>`
        : `<p><strong>Krippendorff's α (nominal)</strong> — coders: ${escapeHtml((res.coders || []).join(', '))} · scope: ${formatInt(res.scope_rows || 0)} rows</p>
           <p>Mean α across categories: <strong>${fmtScore(res.mean_alpha)}</strong></p>`;
    const cats = res.categories || {};
    const rows = Object.entries(cats).map(([k, v]) => {
        if (!v) return `<tr><td>${escapeHtml(k)}</td><td colspan="3" class="muted small">—</td></tr>`;
        const score = v.kappa !== undefined ? v.kappa : v.alpha;
        const n = v.n || v.observations || 0;
        const detail = v.kappa !== undefined ? `Po ${fmtScore(v.po)} / Pe ${fmtScore(v.pe)}` : `Do ${fmtScore(v.Do)} / De ${fmtScore(v.De)}`;
        return `<tr><td>${escapeHtml(k)}</td><td>${fmtScore(score)}</td><td>${formatInt(n)}</td><td class="muted small">${detail}</td></tr>`;
    }).join('');
    rwrap.innerHTML = `${headline}
      <table class="cb-cat-table mt-1">
        <thead><tr><th>Category</th><th>${res.method === 'cohens_kappa' ? 'κ' : 'α'}</th><th>N</th><th>Detail</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
}

function fmtScore(x) {
    if (x === null || x === undefined) return '—';
    return Number(x).toFixed(3);
}

/* ── Codebook actions ──────────────────────────────────────────────────── */
async function createCodebook(preset) {
    const defaultName = preset ? 'Hate-speech starter' : 'My codebook';
    const name = prompt(preset ? 'Name this codebook (a copy of the hate-speech starter):' : 'Name this codebook:', defaultName);
    if (!name) return;
    const body = preset ? { name, preset: 'hate_speech_starter' } : { name };
    try {
        await fetchJson('/api/codebooks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        await refreshState();
        renderCodebook(App.state);
    } catch (err) { alert('Create failed: ' + err.message); }
}

async function activateCodebook(cbId) {
    try {
        await fetchJson(`/api/codebooks/${encodeURIComponent(cbId)}/activate`, { method: 'POST' });
        App.codebook.irr = null;
        await refreshState();
        await loadProgress();
        renderCodebook(App.state);
    } catch (err) { alert('Activate failed: ' + err.message); }
}

async function deleteActiveCodebook() {
    const cb = activeCodebook(App.state);
    if (!cb) return;
    if (!confirm(`Delete codebook "${cb.name}"? All tags tied to it will also be removed.`)) return;
    try {
        await fetchJson(`/api/codebooks/${encodeURIComponent(cb.codebook_id)}`, { method: 'DELETE' });
        App.codebook.focusRowIdx = null;
        App.codebook.focusRowData = null;
        App.codebook.rowTags = [];
        App.codebook.irr = null;
        await refreshState();
        await loadProgress();
        renderCodebook(App.state);
    } catch (err) { alert('Delete failed: ' + err.message); }
}

async function addCategory() {
    const cb = activeCodebook(App.state);
    if (!cb) return;
    const title = $('#cb-add-title').value.trim();
    const shortcut = $('#cb-add-shortcut').value.trim().toLowerCase();
    const group = $('#cb-add-group').value.trim();
    const color = $('#cb-add-color').value || '#C8175D';
    const desc = $('#cb-add-desc').value.trim();
    const st = $('#cb-add-status');
    if (!title) { st.textContent = 'Title is required.'; st.style.color = 'var(--warn)'; return; }
    try {
        await fetchJson(`/api/codebooks/${encodeURIComponent(cb.codebook_id)}/categories`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title, description: desc, exclusion_group: group, shortcut_key: shortcut, color }),
        });
        $('#cb-add-title').value = '';
        $('#cb-add-shortcut').value = '';
        $('#cb-add-group').value = '';
        $('#cb-add-desc').value = '';
        st.textContent = 'Added.';
        st.style.color = 'var(--success)';
        await refreshState();
        renderCodebook(App.state);
    } catch (err) {
        st.textContent = `Failed: ${err.message}`;
        st.style.color = 'var(--danger)';
    }
}

async function removeCategory(catId) {
    const cb = activeCodebook(App.state);
    if (!cb) return;
    if (!confirm(`Remove category "${catId}"? Existing tags with this id will remain in the data until you undo or re-code.`)) return;
    try {
        await fetchJson(
            `/api/codebooks/${encodeURIComponent(cb.codebook_id)}/categories/${encodeURIComponent(catId)}`,
            { method: 'DELETE' },
        );
        await refreshState();
        renderCodebook(App.state);
    } catch (err) { alert('Remove failed: ' + err.message); }
}

async function exportCodebook() {
    const cb = activeCodebook(App.state);
    if (!cb) return;
    try {
        const data = await fetchJson(`/api/codebooks/${encodeURIComponent(cb.codebook_id)}/export`);
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${(cb.name || 'codebook').replace(/\s+/g, '_')}.json`;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
    } catch (err) { alert('Export failed: ' + err.message); }
}

function promptImportCodebook() {
    $('#cb-import-file').click();
}

async function handleImportFile(ev) {
    const file = ev.target.files?.[0];
    ev.target.value = '';
    if (!file) return;
    try {
        const text = await file.text();
        const payload = JSON.parse(text);
        await fetchJson('/api/codebooks/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ payload }),
        });
        await refreshState();
        renderCodebook(App.state);
    } catch (err) { alert('Import failed: ' + err.message); }
}

/* ── Scope & focus row ─────────────────────────────────────────────────── */
async function loadScopeRows() {
    const sliceId = App.codebook.scopeSliceId || '';
    // Tell the server which slice we're actively coding (purely UI state).
    try {
        await fetchJson('/api/coding/slice', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slice_id: sliceId }),
        });
    } catch (_) { /* non-fatal */ }

    // For efficient row stepping we need the list of row indices in the scope.
    try {
        if (sliceId) {
            const slice = App.state?.slices?.[sliceId];
            if (slice?.rows_matched) {
                const data = await fetchJson(`/api/slicer/preview?slice_id=${encodeURIComponent(sliceId)}&page=1&page_size=${slice.rows_matched}`);
                App.codebook.scopeRows = (data.preview || []).map(r => r._row_idx).filter(x => Number.isInteger(x));
            } else {
                App.codebook.scopeRows = [];
            }
        } else {
            const total = App.state?.corpus?.rows || 0;
            App.codebook.scopeRows = Array.from({ length: total }, (_, i) => i);
        }
    } catch (_) {
        const total = App.state?.corpus?.rows || 0;
        App.codebook.scopeRows = Array.from({ length: total }, (_, i) => i);
    }
    App.codebook.scopePtr = 0;
    await loadProgress();
    renderProgressBlock(App.state);
}

async function loadProgress() {
    const sliceId = App.codebook.scopeSliceId || '';
    try {
        const qs = sliceId ? `?slice_id=${encodeURIComponent(sliceId)}` : '';
        App.codebook.progress = await fetchJson(`/api/coding/progress${qs}`);
    } catch (err) {
        App.codebook.progress = null;
    }
}

async function focusRow(rowIdx) {
    if (rowIdx === null || rowIdx === undefined || rowIdx < 0) return;
    App.codebook.focusRowIdx = rowIdx;
    try {
        const data = await fetchJson(`/api/corpus/row/${rowIdx}`);
        App.codebook.focusRowData = data.row || null;
    } catch (_) { App.codebook.focusRowData = null; }
    try {
        const t = await fetchJson(`/api/coding/row/${rowIdx}`);
        App.codebook.rowTags = t.tags || [];
    } catch (_) { App.codebook.rowTags = []; }
    const cb = activeCodebook(App.state);
    if (cb) renderCodingUI(App.state, cb);
}

function stepRow(delta) {
    const rows = App.codebook.scopeRows;
    if (!rows.length) return;
    let ptr = App.codebook.scopePtr;
    if (App.codebook.focusRowIdx === null) {
        ptr = 0;
    } else {
        const cur = rows.indexOf(App.codebook.focusRowIdx);
        ptr = cur < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, cur + delta));
    }
    App.codebook.scopePtr = ptr;
    focusRow(rows[ptr]);
}

function jumpToRow() {
    const val = parseInt($('#cb-jump-row').value, 10);
    if (!Number.isInteger(val) || val < 0) return;
    focusRow(val);
}

async function toggleTag(catId) {
    const ri = App.codebook.focusRowIdx;
    if (ri === null || ri === undefined) { alert('Pick a row first.'); return; }
    const state = App.state;
    const coder = state.coding?.coder_name;
    if (!coder) { alert('Set your coder name in Settings first.'); return; }
    const cb = activeCodebook(state);
    if (!cb) return;
    const mine = (App.codebook.rowTags || []).some(t =>
        t.cat_id === catId && t.coder === coder && t.codebook_id === cb.codebook_id
    );
    const url = mine ? '/api/coding/untag' : '/api/coding/tag';
    try {
        const data = await fetchJson(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ row_idx: ri, cat_id: catId }),
        });
        App.codebook.rowTags = data.row_tags || [];
        await refreshState();
        await loadProgress();
        renderCodebook(App.state);
    } catch (err) { alert('Tag failed: ' + err.message); }
}

async function undoCoding() {
    try {
        await fetchJson('/api/coding/undo', { method: 'POST' });
        if (App.codebook.focusRowIdx !== null) {
            const t = await fetchJson(`/api/coding/row/${App.codebook.focusRowIdx}`);
            App.codebook.rowTags = t.tags || [];
        }
        await refreshState();
        await loadProgress();
        renderCodebook(App.state);
    } catch (err) { alert('Undo failed: ' + err.message); }
}

async function runBulkTag(dryRun) {
    const cb = activeCodebook(App.state);
    if (!cb) return;
    const catId = $('#cb-bulk-cat').value;
    const mode = $('#cb-bulk-source').value;
    const body = { cat_id: catId, dry_run: !!dryRun };
    if (mode === 'slice') body.slice_id = $('#cb-bulk-slice').value;
    else                  body.query = $('#cb-bulk-query').value.trim();
    const st = $('#cb-bulk-status');
    st.textContent = 'Working…';
    st.style.color = 'var(--text-muted)';
    try {
        const data = await fetchJson('/api/coding/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (data.dry_run) {
            st.textContent = `${formatInt(data.rows_targeted)} rows would be tagged.`;
            st.style.color = 'var(--text-muted)';
        } else {
            st.textContent = `Tagged ${formatInt(data.rows_affected)} / ${formatInt(data.rows_targeted)} rows.`;
            st.style.color = 'var(--success)';
            await refreshState();
            await loadProgress();
            if (App.codebook.focusRowIdx !== null) await focusRow(App.codebook.focusRowIdx);
            renderCodebook(App.state);
        }
    } catch (err) {
        st.textContent = `Failed: ${err.message}`;
        st.style.color = 'var(--danger)';
    }
}

async function runIRR() {
    const st = $('#cb-irr-status');
    st.textContent = 'Computing…';
    st.style.color = 'var(--text-muted)';
    try {
        const body = {};
        if (App.codebook.scopeSliceId) body.slice_id = App.codebook.scopeSliceId;
        App.codebook.irr = await fetchJson('/api/coding/irr', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        st.textContent = '';
        const cb = activeCodebook(App.state);
        if (cb) renderIrrPanel(App.state, cb);
    } catch (err) {
        st.textContent = `Failed: ${err.message}`;
        st.style.color = 'var(--danger)';
        App.codebook.irr = null;
    }
}

/* ── Keyboard shortcuts for coding ─────────────────────────────────────── */
function bindCodingKeys() {
    if (App.codebook.keysBound) return;
    App.codebook.keysBound = true;
    document.addEventListener('keydown', ev => {
        if (App.currentPage !== 'codebook') return;
        const tag = (ev.target?.tagName || '').toUpperCase();
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
        if (ev.metaKey || ev.altKey) return;
        if (ev.ctrlKey && (ev.key === 'z' || ev.key === 'Z')) {
            ev.preventDefault(); undoCoding(); return;
        }
        if (ev.ctrlKey) return;
        if (ev.key === 'ArrowLeft')  { ev.preventDefault(); stepRow(-1); return; }
        if (ev.key === 'ArrowRight') { ev.preventDefault(); stepRow(1);  return; }
        const cb = activeCodebook(App.state);
        if (!cb) return;
        const k = (ev.key || '').toLowerCase();
        const hit = cb.categories.find(c => (c.shortcut_key || '').toLowerCase() === k);
        if (hit) { ev.preventDefault(); toggleTag(hit.cat_id); }
    });
}

/* ── 6.5  AI CODING (Phase 5) ──────────────────────────────────────────── */
function renderAICoding(state) {
    if (!state) return;
    const cb = activeCodebook(state);
    const hasKey = !!state.has_api_key;
    const hasCorpus = !!(state.corpus?.built);
    const hasCb = !!cb && (cb.categories?.length || 0) > 0;
    const ready = hasKey && hasCorpus && hasCb;

    $('#ai-gate-card').style.display     = ready ? 'none' : 'block';
    $('#ai-scope-card').style.display    = ready ? 'block' : 'none';
    $('#ai-run-card').style.display      = (ready && (App.ai.log.length || App.ai.running)) ? 'block' : 'none';
    $('#ai-suggest-card').style.display  = (hasKey && hasCorpus) ? 'block' : 'none';
    $('#ai-review-card').style.display   = ready ? 'block' : 'none';
    $('#ai-cache-card').style.display    = ready ? 'block' : 'none';

    if (!ready) {
        const missing = [];
        if (!hasKey)    missing.push('Anthropic API key (Settings)');
        if (!hasCb)     missing.push('active codebook with at least one category (Codebook)');
        if (!hasCorpus) missing.push('a built corpus (Import → Corpus)');
        $('#ai-gate-msg').textContent = 'Before you can run AI coding, you need: ' + missing.join(', ') + '.';
        return;
    }

    renderAIScopeCard(state);
    renderAISuggestCard(state);
    renderAIRunCard();
    renderAIReviewCard(state, cb);
    renderAICacheCard();
}

function renderAIScopeCard(state) {
    const slices = Object.values(state.slices || {});
    const sel = $('#ai-scope-select');
    if (sel) {
        const prev = sel.value;
        sel.innerHTML = `<option value="">Whole corpus</option>` +
            slices.map(s => `<option value="${escapeHtml(s.slice_id)}">${escapeHtml(s.name)} (${formatInt(s.rows_matched || 0)})</option>`).join('');
        if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
    }
    const summary = $('#ai-scope-summary');
    const modeSel = $('#ai-mode');
    const wrap = $('#ai-sample-size-wrap');
    const mode = modeSel?.value || 'sample';
    if (wrap) wrap.style.display = mode === 'sample' ? '' : 'none';
    if (summary) {
        const rowsInScope = resolveScopeRowCount(state, sel?.value || '');
        const sample = parseInt($('#ai-sample-size')?.value || '0', 10) || 0;
        const will = (mode === 'sample' && sample > 0) ? Math.min(sample, rowsInScope) : rowsInScope;
        summary.textContent = rowsInScope
            ? `${formatInt(will)} of ${formatInt(rowsInScope)} rows will be classified`
            : '';
    }
    const pre = App.ai.preflight;
    const pc = $('#ai-preflight-card');
    if (pre) {
        pc.style.display = 'block';
        pc.innerHTML = renderPreflightCard(pre);
    } else {
        pc.style.display = 'none';
        pc.innerHTML = '';
    }
    $('#btn-ai-run').disabled = !(pre && pre.rows_total > 0) || App.ai.running;
    $('#btn-ai-preflight').disabled = App.ai.running;
    $('#btn-ai-stop').style.display = App.ai.running ? 'inline-flex' : 'none';
}

function resolveScopeRowCount(state, sliceId) {
    if (sliceId) {
        const sl = state.slices?.[sliceId];
        return sl?.rows_matched || 0;
    }
    return state.corpus?.rows || 0;
}

function renderPreflightCard(p) {
    const lines = [
        `<div class="flex-row" style="gap:1.5rem; flex-wrap:wrap;">`,
        `  <div><div class="muted small">Rows in scope</div><strong>${formatInt(p.rows_total)}</strong></div>`,
        `  <div><div class="muted small">Cache hits (free)</div><strong>${formatInt(p.cache_hits)}</strong></div>`,
        `  <div><div class="muted small">To classify</div><strong>${formatInt(p.rows_to_classify)}</strong></div>`,
        `  <div><div class="muted small">Batches</div><strong>${formatInt(p.batches)}</strong></div>`,
        `  <div><div class="muted small">Model</div><strong>${escapeHtml(p.model || '')}</strong></div>`,
        `  <div><div class="muted small">Est. cost</div><strong>$${Number(p.estimated_cost_usd || 0).toFixed(4)}</strong></div>`,
        `  <div><div class="muted small">Est. time</div><strong>${Number(p.estimated_seconds || 0).toFixed(0)}s</strong></div>`,
        `</div>`,
    ];
    if (p.cache_hits > 0 && p.rows_to_classify === 0) {
        lines.push(`<p class="muted small mt-2">All rows are in the cache — this run will cost $0.</p>`);
    }
    if (p.budget) {
        lines.push(renderBudgetEnvelope(p.budget));
    }
    return lines.join('');
}

function renderBudgetEnvelope(b) {
    if (!b || b.status === 'disabled') return '';
    const css = b.status === 'block' ? 'budget-block' : b.status === 'warn' ? 'budget-warn' : 'budget-ok';
    const icon = b.status === 'block'
        ? 'fa-ban'
        : b.status === 'warn' ? 'fa-triangle-exclamation' : 'fa-wallet';
    const bar = b.budget_usd > 0
        ? Math.min(100, Math.round(((b.spent_usd + b.estimated_usd) / b.budget_usd) * 100))
        : 0;
    return `
      <div class="budget-envelope ${css} mt-2">
        <i class="fa-solid ${icon}"></i>
        <div class="budget-text">
          <strong>${escapeHtml(b.message || '')}</strong>
          <div class="small muted mt-1">Spent ${'$' + Number(b.spent_usd).toFixed(2)} of ${'$' + Number(b.budget_usd).toFixed(2)} this month · projected total after run: ${'$' + (Number(b.spent_usd) + Number(b.estimated_usd)).toFixed(2)}</div>
          <div class="budget-bar"><span style="width:${bar}%"></span></div>
        </div>
      </div>`;
}

function renderAIRunCard() {
    const card = $('#ai-run-card');
    if (!card) return;
    const logEl = $('#ai-run-log');
    const summaryEl = $('#ai-run-summary');
    const fill = $('#ai-progress-fill');
    if (!App.ai.log.length && !App.ai.running) {
        logEl.innerHTML = '';
        summaryEl.textContent = '';
        if (fill) fill.style.width = '0%';
        return;
    }
    if (fill) fill.style.width = `${Math.min(100, App.ai.progress)}%`;
    const rows = [];
    let cost = 0;
    let applied = 0;
    let batches = 0;
    for (const ev of App.ai.log) {
        if (ev.type === 'start') {
            rows.push(`<div>Start · ${formatInt(ev.rows_total)} rows · ${formatInt(ev.cache_hits)} cache hits · ${formatInt(ev.batches)} batches · model ${escapeHtml(ev.model || '')}</div>`);
        } else if (ev.type === 'cache') {
            rows.push(`<div>Cache applied: ${formatInt(ev.applied)} rows at $0</div>`);
            applied += ev.applied || 0;
        } else if (ev.type === 'batch') {
            cost += ev.cost_usd || 0;
            batches += 1;
            applied = ev.applied_total ?? applied + (ev.applied || 0);
            rows.push(`<div>Batch ${ev.batch_idx} · ${formatInt(ev.applied)}/${formatInt(ev.rows_in_batch)} applied · $${Number(ev.cost_usd || 0).toFixed(5)} · ${Number(ev.seconds || 0).toFixed(1)}s</div>`);
        } else if (ev.type === 'done') {
            cost = ev.cost_usd ?? cost;
            applied = ev.applied_total ?? applied;
            rows.push(`<div><strong>Done.</strong> Applied ${formatInt(applied)} rows · total $${Number(cost).toFixed(5)}</div>`);
        } else if (ev.type === 'error') {
            rows.push(`<div style="color:var(--danger);"><strong>Error:</strong> ${escapeHtml(ev.message || '')}</div>`);
        }
    }
    logEl.innerHTML = rows.join('');
    summaryEl.textContent = App.ai.running
        ? `Running… ${formatInt(batches)} batches so far · $${cost.toFixed(5)}`
        : `Total: ${formatInt(applied)} rows · $${cost.toFixed(5)}`;
}

function renderAISuggestCard(state) {
    const sel = $('#ai-sug-scope');
    if (sel) {
        const prev = sel.value;
        const slices = Object.values(state.slices || {});
        sel.innerHTML = `<option value="">Whole corpus</option>` +
            slices.map(s => `<option value="${escapeHtml(s.slice_id)}">${escapeHtml(s.name)} (${formatInt(s.rows_matched || 0)})</option>`).join('');
        if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
    }
    const result = App.ai.suggest;
    const out = $('#ai-suggest-result');
    if (!out) return;
    if (App.ai.suggestLoading) {
        out.innerHTML = '<p class="muted small">Thinking… (Sonnet takes 10–30s)</p>';
        return;
    }
    if (!result) { out.innerHTML = ''; return; }
    if (!result.ok) {
        out.innerHTML = `<p style="color:var(--danger);">${escapeHtml(result.error || 'Suggestion failed.')}</p>`;
        return;
    }
    const p = result.proposal || {};
    const cats = Array.isArray(p.categories) ? p.categories : [];
    const rationale = p.rationale || p.reasoning || '';
    out.innerHTML = `
      <div class="muted small">Model: ${escapeHtml(result.model || '')} · cost $${Number(result.cost_usd || 0).toFixed(5)}</div>
      ${rationale ? `<p class="mt-1">${escapeHtml(rationale)}</p>` : ''}
      <table class="cb-cat-table mt-2">
        <thead><tr><th style="width:7%;">Key</th><th>Title</th><th>Description</th><th style="width:15%;">Group</th><th style="width:6%;">Color</th></tr></thead>
        <tbody>
          ${cats.map(c => `
            <tr>
              <td><kbd>${escapeHtml(c.shortcut_key || '—')}</kbd></td>
              <td><strong>${escapeHtml(c.title || c.cat_id || '')}</strong><div class="muted small">${escapeHtml(c.cat_id || '')}</div></td>
              <td class="muted small">${escapeHtml(c.description || '')}</td>
              <td class="muted small">${escapeHtml(c.exclusion_group || '—')}</td>
              <td><span class="cb-color-dot" style="background:${escapeHtml(c.color || '#C8175D')};"></span></td>
            </tr>`).join('')}
        </tbody>
      </table>
      <div class="slicer-toolbar mt-2">
        <button class="btn-primary" id="btn-ai-accept-suggest">
          <i class="fa-solid fa-plus"></i> Create codebook from this
        </button>
      </div>
    `;
    $('#btn-ai-accept-suggest')?.addEventListener('click', () => acceptSuggestedCodebook(result));
}

function renderAIReviewCard(state, cb) {
    const list = $('#ai-review-list');
    if (!list) return;
    $('#ai-review-lowconf-only').checked = !!App.ai.reviewLowConfOnly;
    if (App.ai.reviewLoading) {
        list.innerHTML = '<p class="muted small">Loading…</p>';
        return;
    }
    const rows = App.ai.reviewRows || [];
    if (!rows.length) {
        list.innerHTML = '<p class="muted small">No AI tags yet — run AI coding first, then refresh.</p>';
        return;
    }
    const byTitle = {};
    for (const c of cb.categories) byTitle[c.cat_id] = c.title;
    list.innerHTML = rows.map(r => {
        const text = (r.row?.text || '').toString();
        const tagPills = (r.tags || []).map(t => {
            const conf = (t.confidence !== undefined) ? ` · ${Number(t.confidence).toFixed(2)}` : '';
            const reason = t.reason ? ` — ${escapeHtml(t.reason)}` : '';
            return `<span class="cb-tag-pill">
                      <strong>${escapeHtml(byTitle[t.cat_id] || t.cat_id)}</strong>
                      <span class="muted small">${escapeHtml(t.source || 'ai')}${conf}${reason}</span>
                    </span>`;
        }).join('');
        return `
          <div class="cb-list-row" style="display:block;">
            <div class="flex-row" style="justify-content:space-between; align-items:flex-start; gap:0.6rem;">
              <div style="flex:1;">
                <div class="muted small">Row ${formatInt(r.row_idx)}${r.row?.platform ? ' · ' + escapeHtml(r.row.platform) : ''}${r.row?.author_handle ? ' · @' + escapeHtml(r.row.author_handle) : ''}</div>
                <div class="cb-focus-text mt-1">${escapeHtml(text.slice(0, 400))}${text.length > 400 ? '…' : ''}</div>
                <div class="flex-row mt-1" style="gap:0.35rem; flex-wrap:wrap;">${tagPills}</div>
              </div>
              <div class="flex-row" style="gap:0.35rem; flex-wrap:wrap;">
                <button class="btn-secondary" data-ai-open-row="${r.row_idx}"><i class="fa-solid fa-arrow-right"></i> Open in Codebook</button>
              </div>
            </div>
          </div>`;
    }).join('');
    list.querySelectorAll('[data-ai-open-row]').forEach(btn => {
        btn.addEventListener('click', () => {
            const ri = parseInt(btn.dataset.aiOpenRow, 10);
            navigateTo('codebook');
            setTimeout(() => focusRow(ri), 50);
        });
    });
}

function renderAICacheCard() {
    const el = $('#ai-cache-body');
    if (!el) return;
    const s = App.ai.cache;
    if (!s) {
        el.innerHTML = `<div class="slicer-toolbar"><button class="btn-secondary" id="btn-ai-cache-refresh"><i class="fa-solid fa-rotate"></i> Load stats</button></div>`;
    } else {
        el.innerHTML = `
          <div class="flex-row" style="gap:1.5rem; flex-wrap:wrap;">
            <div><div class="muted small">Entries</div><strong>${formatInt(s.entries || 0)}</strong></div>
            <div><div class="muted small">Path</div><span class="muted small">${escapeHtml(s.path || '')}</span></div>
          </div>
          <div class="slicer-toolbar mt-2">
            <button class="btn-secondary" id="btn-ai-cache-refresh"><i class="fa-solid fa-rotate"></i> Refresh</button>
            <button class="btn-secondary btn-danger" id="btn-ai-cache-clear"><i class="fa-solid fa-trash"></i> Clear cache</button>
          </div>`;
    }
    $('#btn-ai-cache-refresh')?.addEventListener('click', loadCacheStats);
    $('#btn-ai-cache-clear')?.addEventListener('click', clearCacheAction);
}

/* ── AI actions ────────────────────────────────────────────────────────── */
async function runAIPreflight() {
    const body = readAIRunBody();
    const st = $('#ai-preflight-status');
    st.textContent = 'Estimating…';
    st.style.color = 'var(--text-muted)';
    try {
        App.ai.preflight = await fetchJson('/api/coding/ai/preflight', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        st.textContent = '';
        renderAIScopeCard(App.state);
    } catch (err) {
        st.textContent = `Failed: ${err.message}`;
        st.style.color = 'var(--danger)';
        App.ai.preflight = null;
        renderAIScopeCard(App.state);
    }
}

function readAIRunBody() {
    const mode = $('#ai-mode')?.value || 'sample';
    const sampleSize = mode === 'full'
        ? 0
        : Math.max(1, parseInt($('#ai-sample-size')?.value || '200', 10) || 200);
    const body = {
        slice_id: $('#ai-scope-select')?.value || null,
        sample_size: sampleSize,
        batch_size: Math.max(1, Math.min(50, parseInt($('#ai-batch-size')?.value || '20', 10) || 20)),
    };
    if (App.ai._budgetOverride != null) body.budget_override = App.ai._budgetOverride;
    return body;
}

function promptBudgetOverride(envelope) {
    // Returns:
    //   null  → no override needed, proceed
    //   false → user cancelled or typed wrong amount, abort
    //   number → override value to pass to backend
    if (!envelope || envelope.status !== 'block') return null;
    const typed = prompt(
        `${envelope.message}\n\nTo override, type the exact monthly budget amount (number only):`,
        ''
    );
    if (typed === null) return false;
    const v = Number(typed);
    if (!(Math.abs(v - Number(envelope.budget_usd)) < 1e-6)) {
        alert('That does not match the monthly budget amount. The run was cancelled.');
        return false;
    }
    return v;
}

async function runAICoding() {
    if (App.ai.running) return;
    const pre = App.ai.preflight;
    if (!pre) { alert('Estimate cost first.'); return; }
    const cost = Number(pre.estimated_cost_usd || 0);
    if (cost > 0 && !confirm(`This run will cost about $${cost.toFixed(4)} across ${pre.batches} batch${pre.batches === 1 ? '' : 'es'}. Continue?`)) return;
    const override = promptBudgetOverride(pre.budget);
    if (override === false) return;
    App.ai._budgetOverride = (typeof override === 'number') ? override : null;

    App.ai.running = true;
    App.ai.log = [];
    App.ai.progress = 0;
    App.ai.aborter = new AbortController();
    const postEl = $('#ai-run-postrun'); if (postEl) { postEl.style.display = 'none'; postEl.innerHTML = ''; }
    renderAIScopeCard(App.state);
    renderAIRunCard();
    $('#ai-run-card').style.display = 'block';

    const body = readAIRunBody();
    try {
        const res = await fetch('/api/coding/ai/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: App.ai.aborter.signal,
        });
        if (!res.ok) {
            let detail = res.statusText;
            try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
            throw new Error(`${res.status} ${detail}`);
        }
        await consumeSSE(res);
    } catch (err) {
        if (err.name === 'AbortError') {
            App.ai.log.push({ type: 'error', message: 'Stopped by user.' });
        } else {
            App.ai.log.push({ type: 'error', message: err.message });
        }
    } finally {
        App.ai.running = false;
        App.ai.aborter = null;
        // Refresh state & cache so review panel + cache stats stay honest.
        try { await refreshState(); } catch (_) {}
        try { App.ai.cache = await fetchJson('/api/coding/ai/cache/stats'); } catch (_) {}
        try { await loadAIReview(); } catch (_) {}
        renderAICoding(App.state);
        renderAIPostRun();
    }
}

function renderAIPostRun() {
    const el = $('#ai-run-postrun');
    if (!el) return;
    const log = App.ai.log || [];
    const doneEv = [...log].reverse().find(e => e && e.type === 'done');
    const startEv = log.find(e => e && e.type === 'start');
    const errorEv = log.find(e => e && e.type === 'error');
    if (!doneEv && !errorEv) { el.style.display = 'none'; el.innerHTML = ''; return; }

    if (errorEv && !doneEv) {
        el.style.display = 'block';
        el.innerHTML = `
            <div class="glass-card" style="border-left:4px solid var(--danger); padding:0.9rem 1.1rem; background:rgba(255,235,238,0.5);">
                <strong style="color:var(--danger);"><i class="fa-solid fa-triangle-exclamation"></i> Run stopped</strong>
                <p class="muted small" style="margin:0.3rem 0 0;">${escapeHtml(errorEv.message || 'Unknown error.')}</p>
            </div>`;
        return;
    }

    const totalRows = Number(startEv?.rows_total || 0);
    const cacheHits = Number(startEv?.cache_hits || 0);
    const applied = Number(doneEv?.applied_total || 0);
    const cost = Number(doneEv?.cost_usd || 0);
    const batches = Number(startEv?.batches || 0);
    const rows = App.ai.reviewRows || [];
    const lowConf = rows.filter(r => {
        const tags = (r && r.tags) || [];
        return tags.some(t => Number(t.confidence || 0) > 0 && Number(t.confidence) < 0.7);
    }).length;

    el.style.display = 'block';
    el.innerHTML = `
        <div class="glass-card" style="border-left:4px solid var(--isd-pink); padding:0.95rem 1.1rem;">
            <div class="flex-row" style="justify-content:space-between; align-items:center; gap:0.6rem; flex-wrap:wrap;">
                <strong><i class="fa-solid fa-circle-check" style="color:var(--success);"></i> Run complete</strong>
                <span class="muted small">Applied ${formatInt(applied)} of ${formatInt(totalRows)} rows · ${formatInt(batches)} batch${batches === 1 ? '' : 'es'} · $${cost.toFixed(5)}</span>
            </div>
            <ul style="margin:0.6rem 0 0; padding-left:1.1rem; line-height:1.55;">
                <li>${formatInt(cacheHits)} row${cacheHits === 1 ? '' : 's'} served from cache (no cost).</li>
                <li>${lowConf > 0
                    ? `<span style="color:var(--warn);"><strong>${formatInt(lowConf)}</strong> low-confidence row${lowConf === 1 ? '' : 's'}</span> — spot-check these first.`
                    : 'No low-confidence rows in the review panel below.'}</li>
                <li>Next: scroll down to <strong>Review AI tags</strong>, or jump to <a href="#page-slicer" id="ai-postrun-slicer" style="color:var(--isd-pink);">Slicer</a> to filter by tag.</li>
            </ul>
        </div>`;
    $('#ai-postrun-slicer')?.addEventListener('click', (e) => { e.preventDefault(); navigateTo('slicer'); });
}

async function consumeSSE(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nlIdx;
        while ((nlIdx = buf.indexOf('\n\n')) !== -1) {
            const raw = buf.slice(0, nlIdx);
            buf = buf.slice(nlIdx + 2);
            const line = raw.split('\n').find(l => l.startsWith('data: '));
            if (!line) continue;
            const payload = line.slice(6);
            try {
                const ev = JSON.parse(payload);
                handleAIEvent(ev);
            } catch (e) {
                console.warn('bad SSE payload', payload);
            }
        }
    }
}

function handleAIEvent(ev) {
    App.ai.log.push(ev);
    if (ev.type === 'start') {
        const total = ev.rows_total || 1;
        const done = ev.cache_hits || 0;
        App.ai.progress = Math.round((done / total) * 100);
    } else if (ev.type === 'cache') {
        // covered by start
    } else if (ev.type === 'batch') {
        const startEv = App.ai.log.find(e => e.type === 'start');
        if (startEv) {
            const total = startEv.rows_total || 1;
            App.ai.progress = Math.min(100, Math.round(((ev.applied_total || 0) / total) * 100));
        }
    } else if (ev.type === 'done') {
        App.ai.progress = 100;
    }
    renderAIRunCard();
}

function stopAIRun() {
    if (App.ai.aborter) {
        App.ai.aborter.abort();
    }
}

async function runSuggestCodebook() {
    if (App.ai.suggestLoading) return;
    App.ai.suggestLoading = true;
    App.ai.suggest = null;
    const st = $('#ai-suggest-status');
    st.textContent = 'Asking Sonnet…';
    st.style.color = 'var(--text-muted)';
    renderAISuggestCard(App.state);
    try {
        const body = {
            slice_id: $('#ai-sug-scope')?.value || null,
            sample_size: Math.max(10, Math.min(80, parseInt($('#ai-sug-size')?.value || '40', 10) || 40)),
            goal: ($('#ai-sug-goal')?.value || '').trim(),
        };
        App.ai.suggest = await fetchJson('/api/coding/ai/suggest-codebook', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        st.textContent = '';
    } catch (err) {
        App.ai.suggest = { ok: false, error: err.message };
        st.textContent = '';
    } finally {
        App.ai.suggestLoading = false;
        renderAISuggestCard(App.state);
    }
}

async function acceptSuggestedCodebook(result) {
    if (!result || !result.ok) return;
    const proposal = result.proposal || {};
    const suggestedName = proposal.name || 'AI-suggested codebook';
    const name = prompt('Name this codebook:', suggestedName);
    if (!name) return;
    try {
        // Create empty codebook first.
        const created = await fetchJson('/api/codebooks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        const cbId = created.codebook_id || created.codebook?.codebook_id;
        if (!cbId) throw new Error('Codebook creation returned no id.');
        // Add each category.
        const cats = Array.isArray(proposal.categories) ? proposal.categories : [];
        for (const c of cats) {
            await fetchJson(`/api/codebooks/${encodeURIComponent(cbId)}/categories`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cat_id: c.cat_id,
                    title: c.title || c.cat_id,
                    description: c.description || '',
                    exclusion_group: c.exclusion_group || '',
                    shortcut_key: c.shortcut_key || '',
                    color: c.color || '',
                }),
            });
        }
        // Activate.
        await fetchJson(`/api/codebooks/${encodeURIComponent(cbId)}/activate`, { method: 'POST' });
        await refreshState();
        alert(`Created codebook "${name}" with ${cats.length} categor${cats.length === 1 ? 'y' : 'ies'}. It's now active.`);
        renderAICoding(App.state);
    } catch (err) {
        alert('Could not create codebook: ' + err.message);
    }
}

async function loadAIReview() {
    const cb = activeCodebook(App.state);
    if (!cb) { App.ai.reviewRows = []; return; }
    App.ai.reviewLoading = true;
    renderAIReviewCard(App.state, cb);

    const threshold = 0.7;
    const byRow = [];
    const tagsMap = App.state?.tags || {};
    for (const [key, tags] of Object.entries(tagsMap)) {
        const aiTags = (tags || []).filter(t =>
            t.source === 'ai' && t.codebook_id === cb.codebook_id
        );
        if (!aiTags.length) continue;
        if (App.ai.reviewLowConfOnly) {
            const anyLow = aiTags.some(t => (t.confidence ?? 1) < threshold);
            if (!anyLow) continue;
        }
        const ri = parseInt(key, 10);
        if (!Number.isInteger(ri)) continue;
        byRow.push({ row_idx: ri, tags: aiTags });
    }
    // Sort by lowest confidence first (most in need of review).
    byRow.sort((a, b) => {
        const la = Math.min(...a.tags.map(t => t.confidence ?? 1));
        const lb = Math.min(...b.tags.map(t => t.confidence ?? 1));
        return la - lb;
    });
    // Limit to first 30 for performance.
    const page = byRow.slice(0, 30);
    // Fetch row bodies in parallel.
    const rowFetches = await Promise.all(page.map(async r => {
        try {
            const resp = await fetchJson(`/api/corpus/row/${r.row_idx}`);
            return { ...r, row: resp.row || {} };
        } catch (_) {
            return { ...r, row: {} };
        }
    }));
    App.ai.reviewRows = rowFetches;
    App.ai.reviewLoading = false;
    renderAIReviewCard(App.state, cb);
}

async function loadCacheStats() {
    try {
        App.ai.cache = await fetchJson('/api/coding/ai/cache/stats');
    } catch (err) {
        alert('Could not load cache stats: ' + err.message);
    }
    renderAICacheCard();
}

async function clearCacheAction() {
    if (!confirm('Clear the AI answer cache? Future runs will need to re-classify every row.')) return;
    try {
        const res = await fetchJson('/api/coding/ai/cache/clear', { method: 'POST' });
        App.ai.cache = { entries: 0, path: App.ai.cache?.path || '' };
        alert(`Removed ${res.removed || 0} cached answers.`);
    } catch (err) {
        alert('Clear failed: ' + err.message);
    }
    renderAICacheCard();
}

/* ── 6b. TOPICS (Phase 6) ───────────────────────────────────────────────── */
function activeTopicSet(state) {
    if (!state) return null;
    const id = state.active_topic_set;
    if (!id) return null;
    return (state.topic_sets || {})[id] || null;
}

function renderTopics(state) {
    if (!state) return;
    const hasKey = !!state.has_api_key;
    const hasCorpus = !!(state.corpus?.built);
    const ready = hasKey && hasCorpus;

    const gate = $('#tp-gate-card');
    const list = $('#tp-list-card');
    const create = $('#tp-create-card');
    const run = $('#tp-run-card');
    const viewer = $('#tp-viewer-card');
    if (!gate || !list || !create || !run || !viewer) return;

    gate.style.display    = ready ? 'none' : 'block';
    list.style.display    = ready ? 'block' : 'none';
    create.style.display  = ready ? 'block' : 'none';

    if (!ready) {
        const missing = [];
        if (!hasKey)    missing.push('an Anthropic API key (Settings)');
        if (!hasCorpus) missing.push('a built corpus (Import → Corpus)');
        $('#tp-gate-msg').textContent = 'Before you can run topic modelling, you need: ' + missing.join(' and ') + '.';
        run.style.display = 'none';
        viewer.style.display = 'none';
        return;
    }

    renderTopicsListCard(state);
    renderTopicsCreateCard(state);

    const ts = activeTopicSet(state);
    run.style.display = ts ? 'block' : 'none';
    viewer.style.display = (ts && (ts.topics || []).length) ? 'block' : 'none';
    if (ts) {
        renderTopicsRunCard(state, ts);
        if ((ts.topics || []).length) renderTopicsViewerCard(state, ts);
    }
}

function renderTopicsListCard(state) {
    const body = $('#tp-list-body');
    if (!body) return;
    const sets = Object.values(state.topic_sets || {});
    if (!sets.length) {
        body.innerHTML = `<p class="muted small">No topic runs yet. Create one below to get started.</p>`;
        return;
    }
    sets.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    const activeId = state.active_topic_set || '';
    body.innerHTML = sets.map(ts => {
        const isActive = ts.topic_set_id === activeId;
        const topicCount = (ts.topics || []).length;
        const rowCount = Object.keys(ts.row_assignments || {}).length;
        const scopeLabel = ts.scope_slice_id
            ? (state.slices?.[ts.scope_slice_id]?.name || ts.scope_slice_id)
            : 'Whole corpus';
        return `
            <div class="slice-item ${isActive ? 'active' : ''}" data-ts-id="${escapeHtml(ts.topic_set_id)}">
                <div class="sl-main">
                    <div class="sl-name">${escapeHtml(ts.name || '(unnamed)')} ${isActive ? '<span class="pill">active</span>' : ''}</div>
                    <div class="sl-meta">
                        ${escapeHtml(scopeLabel)} · ${topicCount} topic${topicCount === 1 ? '' : 's'} · ${formatInt(rowCount)} classified · status: ${escapeHtml(ts.status || 'draft')}
                    </div>
                </div>
                <div class="sl-actions">
                    ${isActive ? '' : `<button class="btn-secondary" data-action="activate">Activate</button>`}
                    <button class="btn-secondary btn-danger" data-action="delete">Delete</button>
                </div>
            </div>`;
    }).join('');

    body.querySelectorAll('.slice-item').forEach(el => {
        const id = el.dataset.tsId;
        el.querySelector('[data-action="activate"]')?.addEventListener('click', () => activateTopicSet(id));
        el.querySelector('[data-action="delete"]')?.addEventListener('click', () => deleteTopicSet(id));
    });
}

function renderTopicsCreateCard(state) {
    const sel = $('#tp-new-scope');
    if (sel) {
        const prev = sel.value;
        const slices = Object.values(state.slices || {});
        sel.innerHTML = `<option value="">Whole corpus (${formatInt(state.corpus?.rows || 0)} rows)</option>` +
            slices.map(s => `<option value="${escapeHtml(s.slice_id)}">${escapeHtml(s.name)} (${formatInt(s.rows_matched || 0)})</option>`).join('');
        if (prev && [...sel.options].some(o => o.value === prev)) sel.value = prev;
    }
}

function renderTopicsRunCard(state, ts) {
    const meta = $('#tp-run-meta');
    const title = $('#tp-run-title');
    if (title) title.textContent = `Active run: ${ts.name || '(unnamed)'}`;
    if (meta) {
        const scopeLabel = ts.scope_slice_id
            ? (state.slices?.[ts.scope_slice_id]?.name || ts.scope_slice_id)
            : 'Whole corpus';
        const cost = Number(ts.cost_usd || 0);
        meta.textContent = `Scope: ${scopeLabel} · Sample ${ts.sample_size || 200} · Status: ${ts.status || 'draft'} · Spent: $${cost.toFixed(4)}`;
    }

    const hasTopics = (ts.topics || []).length > 0;
    const running = App.topics.running;
    $('#btn-tp-preflight').disabled = running;
    $('#btn-tp-induce').disabled = running;
    $('#btn-tp-classify').disabled = running || !hasTopics;
    $('#btn-tp-stop').style.display = running ? 'inline-flex' : 'none';

    const pre = App.topics.preflight;
    const preCard = $('#tp-preflight-body');
    if (pre) {
        preCard.style.display = 'block';
        preCard.innerHTML = renderTopicsPreflight(pre);
    } else {
        preCard.style.display = 'none';
        preCard.innerHTML = '';
    }

    const wrap = $('#tp-progress-wrap');
    const fill = $('#tp-progress-fill');
    const lbl = $('#tp-progress-label');
    if (running || App.topics.log.length) {
        wrap.style.display = 'block';
        if (fill) fill.style.width = `${App.topics.progress || 0}%`;
        if (lbl) lbl.textContent = App.topics.progressLabel || '';
    } else {
        wrap.style.display = 'none';
    }

    const log = $('#tp-log');
    if (log) log.innerHTML = App.topics.log.map(renderTopicsLogLine).join('');
}

function renderTopicsPreflight(p) {
    const induceModel = p.induce_model || p.model || '';
    const classifyModel = p.classify_model || p.model || '';
    const indCost = Number(p.estimated_cost_induce_usd ?? 0);
    const clsCost = Number(p.estimated_cost_classify_usd ?? 0);
    const totCost = Number(p.estimated_cost_usd ?? (indCost + clsCost));
    const lines = [
        `<div class="flex-row" style="gap:1.5rem; flex-wrap:wrap;">`,
        `  <div><div class="muted small">Rows in scope</div><strong>${formatInt(p.scope_rows || 0)}</strong></div>`,
        `  <div><div class="muted small">Sample for induction</div><strong>${formatInt(p.sample_size || 0)}</strong></div>`,
        `  <div><div class="muted small">To classify</div><strong>${formatInt(p.classify_rows || 0)}</strong></div>`,
        `  <div><div class="muted small">Batches</div><strong>${formatInt(p.batches || 0)}</strong></div>`,
        `  <div><div class="muted small">Induce model</div><strong>${escapeHtml(induceModel)}</strong></div>`,
        `  <div><div class="muted small">Classify model</div><strong>${escapeHtml(classifyModel)}</strong></div>`,
        `  <div><div class="muted small">Induce cost</div><strong>$${indCost.toFixed(4)}</strong></div>`,
        `  <div><div class="muted small">Classify cost</div><strong>$${clsCost.toFixed(4)}</strong></div>`,
        `  <div><div class="muted small">Total if both</div><strong>$${totCost.toFixed(4)}</strong></div>`,
        `  <div><div class="muted small">Est. time</div><strong>${Number(p.estimated_seconds || 0).toFixed(0)}s</strong></div>`,
        `</div>`,
    ];
    if (p.budget) {
        lines.push(renderBudgetEnvelope(p.budget));
    }
    return lines.join('');
}

function renderTopicsLogLine(ev) {
    if (ev.type === 'start') {
        const parts = [];
        if (ev.sample_rows != null) parts.push(`${formatInt(ev.sample_rows)} sample rows`);
        if (ev.rows_total != null)  parts.push(`${formatInt(ev.rows_total)} rows total`);
        if (ev.batches != null)     parts.push(`${ev.batches} batch${ev.batches === 1 ? '' : 'es'}`);
        if (ev.model)               parts.push(escapeHtml(ev.model));
        return `<div class="muted small">▸ start — ${parts.join(' · ')}</div>`;
    }
    if (ev.type === 'batch') {
        return `<div class="muted small">· batch ${ev.batch_idx} — ${formatInt(ev.applied || 0)} rows · ${Number(ev.seconds || 0).toFixed(1)}s · $${Number(ev.cumulative_cost_usd || 0).toFixed(4)} so far</div>`;
    }
    if (ev.type === 'done') {
        if (ev.topics != null) {
            return `<div class="small" style="color: var(--isd-pink);">✓ induced ${ev.topics} topic${ev.topics === 1 ? '' : 's'} in ${Number(ev.seconds || 0).toFixed(1)}s · $${Number(ev.cost_usd || 0).toFixed(4)}</div>`;
        }
        return `<div class="small" style="color: var(--isd-pink);">✓ classified ${formatInt(ev.applied_total || 0)} rows · ${formatInt(ev.other_count || 0)} other · $${Number(ev.cost_usd || 0).toFixed(4)}</div>`;
    }
    if (ev.type === 'error') {
        return `<div class="small" style="color: var(--danger, #d44);">✗ ${escapeHtml(ev.message || 'error')}${ev.batch_idx ? ' (batch ' + ev.batch_idx + ')' : ''}</div>`;
    }
    return `<div class="muted small">${escapeHtml(JSON.stringify(ev))}</div>`;
}

function renderTopicsViewerCard(state, ts) {
    const stats = $('#tp-viewer-stats');
    const body = $('#tp-viewer-body');
    if (!body) return;
    const topics = ts.topics || [];
    const totalAssigned = Object.keys(ts.row_assignments || {}).length;
    if (stats) stats.textContent = `${topics.length} topic${topics.length === 1 ? '' : 's'} · ${formatInt(totalAssigned)} rows classified`;

    body.innerHTML = topics.map(t => {
        const id = t.topic_id;
        const checked = App.topics.selected.has(id) ? 'checked' : '';
        const count = Number(t.count || 0);
        const kw = Array.isArray(t.keywords) ? t.keywords : [];
        const examples = App.topics.exampleRows[id] || [];
        const exHtml = examples.length
            ? examples.slice(0, 3).map(r => `<li class="small">${escapeHtml(truncate(r.text || '', 220))}</li>`).join('')
            : `<li class="muted small">No examples loaded — click "Load examples".</li>`;
        return `
            <div class="topic-card" data-topic-id="${escapeHtml(id)}" style="padding:0.8rem 1rem; border:1px solid rgba(0,0,0,0.08); border-radius:12px; margin-bottom:0.8rem; background:rgba(255,255,255,0.5);">
                <div class="flex-row" style="justify-content:space-between; align-items:flex-start; gap:0.6rem;">
                    <label class="flex-row" style="gap:0.5rem; align-items:center;">
                        <input type="checkbox" data-action="select" ${checked} />
                        <strong>${escapeHtml(t.name || id)}</strong>
                    </label>
                    <span class="muted small">${formatInt(count)} row${count === 1 ? '' : 's'}</span>
                </div>
                <p class="mt-1 small">${escapeHtml(t.description || '')}</p>
                ${kw.length ? `<div class="flex-row mt-1">${kw.map(k => `<span class="pill">${escapeHtml(String(k))}</span>`).join('')}</div>` : ''}
                <ul class="mt-2" style="margin-left:1rem;">${exHtml}</ul>
                <div class="slicer-toolbar mt-2">
                    <button class="btn-secondary" data-action="examples">Load examples</button>
                    <button class="btn-secondary" data-action="rename">Rename</button>
                </div>
                <div class="slicer-toolbar mt-1" style="border-top:1px dashed rgba(0,0,0,0.08); padding-top:0.55rem;">
                    <span class="muted small" style="margin-right:0.4rem;">Next:</span>
                    <button class="btn-secondary" data-action="promote" title="Create a codebook category from this topic"><i class="fa-solid fa-tag"></i> Promote to codebook</button>
                    <button class="btn-secondary" data-action="tagall" title="Bulk-tag every row assigned to this topic with a codebook category"><i class="fa-solid fa-tags"></i> Tag all rows</button>
                    <button class="btn-secondary" data-action="drill" title="Save these rows as a slice and open the Slicer"><i class="fa-solid fa-magnifying-glass"></i> Drill in</button>
                </div>
            </div>`;
    }).join('');

    body.querySelectorAll('.topic-card').forEach(el => {
        const id = el.dataset.topicId;
        el.querySelector('[data-action="select"]')?.addEventListener('change', ev => {
            if (ev.target.checked) App.topics.selected.add(id);
            else App.topics.selected.delete(id);
            $('#btn-tp-merge').disabled = App.topics.selected.size < 2;
        });
        el.querySelector('[data-action="examples"]')?.addEventListener('click', () => loadTopicExamples(id));
        el.querySelector('[data-action="rename"]')?.addEventListener('click', () => renameTopic(id));
        el.querySelector('[data-action="promote"]')?.addEventListener('click', () => promoteTopicToCodebook(id));
        el.querySelector('[data-action="tagall"]')?.addEventListener('click', () => tagAllInTopic(id));
        el.querySelector('[data-action="drill"]')?.addEventListener('click', () => drillIntoTopic(id));
    });

    $('#btn-tp-merge').disabled = App.topics.selected.size < 2;
}

async function createTopicSet() {
    const name = ($('#tp-new-name')?.value || '').trim();
    if (!name) { setText('#tp-create-msg', 'Give this run a name first.'); return; }
    const scope_slice_id = $('#tp-new-scope')?.value || '';
    const sample_size = Math.max(50, Math.min(500, parseInt($('#tp-new-sample')?.value || '200', 10) || 200));
    const goal = ($('#tp-new-goal')?.value || '').trim();
    const target_k = Math.max(3, Math.min(20, parseInt($('#tp-new-targetk')?.value || '8', 10) || 8));
    setText('#tp-create-msg', 'Creating…');
    try {
        await fetchJson('/api/topics/topic-sets', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, scope_slice_id, sample_size, goal, target_k }),
        });
        $('#tp-new-name').value = '';
        $('#tp-new-goal').value = '';
        setText('#tp-create-msg', 'Created — and activated.');
        App.topics.preflight = null;
        App.topics.log = [];
        App.topics.selected.clear();
        App.topics.exampleRows = {};
        await refreshState();
    } catch (err) {
        setText('#tp-create-msg', 'Failed: ' + err.message);
    }
}

async function activateTopicSet(id) {
    try {
        await fetchJson(`/api/topics/topic-sets/${encodeURIComponent(id)}/activate`, { method: 'POST' });
        App.topics.preflight = null;
        App.topics.log = [];
        App.topics.selected.clear();
        App.topics.exampleRows = {};
        await refreshState();
    } catch (err) {
        alert('Could not activate: ' + err.message);
    }
}

async function deleteTopicSet(id) {
    const ts = App.state?.topic_sets?.[id];
    if (!ts) return;
    if (!confirm(`Delete topic run "${ts.name || id}"? This also removes its topic assignments.`)) return;
    try {
        await fetchJson(`/api/topics/topic-sets/${encodeURIComponent(id)}`, { method: 'DELETE' });
        App.topics.preflight = null;
        App.topics.log = [];
        App.topics.selected.clear();
        App.topics.exampleRows = {};
        await refreshState();
    } catch (err) {
        alert('Delete failed: ' + err.message);
    }
}

async function runTopicsPreflight() {
    const ts = activeTopicSet(App.state);
    if (!ts) return;
    const st = $('#tp-preflight-status');
    if (st) { st.textContent = 'Estimating…'; st.style.color = 'var(--text-muted)'; }
    const body = {
        topic_set_id: ts.topic_set_id,
        scope_slice_id: ts.scope_slice_id || null,
        sample_size: ts.sample_size || 200,
        batch_size: Math.max(10, Math.min(60, parseInt($('#tp-batch-size')?.value || '30', 10) || 30)),
    };
    try {
        App.topics.preflight = await fetchJson('/api/topics/preflight', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (st) st.textContent = '';
    } catch (err) {
        App.topics.preflight = null;
        if (st) { st.textContent = 'Failed: ' + err.message; st.style.color = 'var(--danger)'; }
    }
    renderTopicsRunCard(App.state, ts);
}

async function runTopicsInduce() {
    const ts = activeTopicSet(App.state);
    if (!ts || App.topics.running) return;
    const pre = App.topics.preflight;
    // Induce only runs induction — don't scare users with full classify cost here.
    const induceCost = Number(pre?.estimated_cost_induce_usd ?? pre?.estimated_cost_usd ?? 0);
    if (induceCost > 0) {
        if (!confirm(`Induction is estimated at about $${induceCost.toFixed(4)}. Induce topics now?`)) return;
    }
    const override = promptBudgetOverride(pre && pre.budget);
    if (override === false) return;
    const body = { topic_set_id: ts.topic_set_id };
    if (typeof override === 'number') body.budget_override = override;

    App.topics.running = true;
    App.topics.mode = 'induce';
    App.topics.log = [];
    App.topics.progress = 0;
    App.topics.progressLabel = 'Reading sample…';
    App.topics.aborter = new AbortController();
    renderTopicsRunCard(App.state, ts);

    try {
        const res = await fetch('/api/topics/induce', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: App.topics.aborter.signal,
        });
        if (!res.ok) {
            let detail = res.statusText;
            try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
            throw new Error(`${res.status} ${detail}`);
        }
        await consumeTopicsSSE(res);
    } catch (err) {
        if (err.name === 'AbortError') App.topics.log.push({ type: 'error', message: 'Stopped by user.' });
        else App.topics.log.push({ type: 'error', message: err.message });
    } finally {
        App.topics.running = false;
        App.topics.mode = '';
        App.topics.aborter = null;
        App.topics.progressLabel = '';
        try { await refreshState(); } catch (_) {}
        renderTopics(App.state);
    }
}

async function runTopicsClassify() {
    const ts = activeTopicSet(App.state);
    if (!ts || App.topics.running) return;
    if (!(ts.topics || []).length) { alert('Induce topics first.'); return; }
    const classifyMode = $('#tp-classify-mode')?.value || 'full';
    const rowLimit = classifyMode === 'limit'
        ? Math.max(1, parseInt($('#tp-row-limit')?.value || '1000', 10) || 1000)
        : 0;
    const pre = App.topics.preflight;
    const override = promptBudgetOverride(pre && pre.budget);
    if (override === false) return;
    const body = {
        topic_set_id: ts.topic_set_id,
        batch_size: Math.max(10, Math.min(60, parseInt($('#tp-batch-size')?.value || '30', 10) || 30)),
        row_limit: rowLimit,
    };
    if (typeof override === 'number') body.budget_override = override;

    App.topics.running = true;
    App.topics.mode = 'classify';
    App.topics.log = [];
    App.topics.progress = 0;
    App.topics.progressLabel = 'Starting…';
    App.topics.aborter = new AbortController();
    renderTopicsRunCard(App.state, ts);

    try {
        const res = await fetch('/api/topics/classify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: App.topics.aborter.signal,
        });
        if (!res.ok) {
            let detail = res.statusText;
            try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
            throw new Error(`${res.status} ${detail}`);
        }
        await consumeTopicsSSE(res);
    } catch (err) {
        if (err.name === 'AbortError') App.topics.log.push({ type: 'error', message: 'Stopped by user.' });
        else App.topics.log.push({ type: 'error', message: err.message });
    } finally {
        App.topics.running = false;
        App.topics.mode = '';
        App.topics.aborter = null;
        App.topics.progressLabel = '';
        App.topics.exampleRows = {};
        try { await refreshState(); } catch (_) {}
        renderTopics(App.state);
    }
}

async function consumeTopicsSSE(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nlIdx;
        while ((nlIdx = buf.indexOf('\n\n')) !== -1) {
            const raw = buf.slice(0, nlIdx);
            buf = buf.slice(nlIdx + 2);
            const line = raw.split('\n').find(l => l.startsWith('data: '));
            if (!line) continue;
            try {
                const ev = JSON.parse(line.slice(6));
                handleTopicsEvent(ev);
            } catch (_) { /* ignore */ }
        }
    }
}

function handleTopicsEvent(ev) {
    App.topics.log.push(ev);
    if (ev.type === 'start') {
        App.topics._batchesTotal = ev.batches || 1;
        if (App.topics.mode === 'induce') {
            App.topics.progress = 30;
            App.topics.progressLabel = `Reading ${formatInt(ev.sample_rows || 0)} sample rows…`;
        } else {
            App.topics.progress = 5;
            App.topics.progressLabel = `Classifying ${formatInt(ev.rows_total || 0)} rows in ${ev.batches || 1} batch${(ev.batches || 1) === 1 ? '' : 'es'}…`;
        }
    } else if (ev.type === 'batch') {
        const total = App.topics._batchesTotal || 1;
        App.topics.progress = Math.min(95, Math.round((ev.batch_idx / total) * 100));
        App.topics.progressLabel = `Batch ${ev.batch_idx}/${total} — ${formatInt(ev.applied_total || 0)} rows classified`;
    } else if (ev.type === 'done') {
        App.topics.progress = 100;
        if (ev.topics != null) App.topics.progressLabel = `Induced ${ev.topics} topics.`;
        else App.topics.progressLabel = `Classified ${formatInt(ev.applied_total || 0)} rows.`;
    } else if (ev.type === 'error') {
        App.topics.progressLabel = ev.message || 'Error';
    }
    const ts = activeTopicSet(App.state);
    if (ts) renderTopicsRunCard(App.state, ts);
}

function stopTopicsRun() {
    if (App.topics.aborter) App.topics.aborter.abort();
}

async function renameTopic(topicId) {
    const ts = activeTopicSet(App.state);
    if (!ts) return;
    const topic = (ts.topics || []).find(t => t.topic_id === topicId);
    if (!topic) return;
    const newName = prompt(`Rename topic "${topic.name}" to:`, topic.name || '');
    if (!newName || newName.trim() === topic.name) return;
    try {
        await fetchJson(
            `/api/topics/topic-sets/${encodeURIComponent(ts.topic_set_id)}/topics/${encodeURIComponent(topicId)}`,
            {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newName.trim() }),
            },
        );
        await refreshState();
    } catch (err) {
        alert('Rename failed: ' + err.message);
    }
}

async function mergeTopics() {
    const ts = activeTopicSet(App.state);
    if (!ts) return;
    const ids = Array.from(App.topics.selected);
    if (ids.length < 2) return;
    const topics = ts.topics || [];
    const options = topics.filter(t => ids.includes(t.topic_id));
    const listStr = options.map(t => `  ${t.topic_id} — ${t.name}`).join('\n');
    const target = prompt(`Which topic_id should absorb the others?\n\n${listStr}\n\nEnter the topic_id to keep:`);
    if (!target) return;
    if (!ids.includes(target)) { alert('That topic_id was not in the selection.'); return; }
    const sources = ids.filter(i => i !== target);
    if (!sources.length) return;
    try {
        await fetchJson(
            `/api/topics/topic-sets/${encodeURIComponent(ts.topic_set_id)}/merge`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source_ids: sources, target_id: target }),
            },
        );
        App.topics.selected.clear();
        App.topics.exampleRows = {};
        await refreshState();
    } catch (err) {
        alert('Merge failed: ' + err.message);
    }
}

async function loadTopicExamples(topicId) {
    const ts = activeTopicSet(App.state);
    if (!ts) return;
    try {
        const res = await fetchJson(
            `/api/topics/topic-sets/${encodeURIComponent(ts.topic_set_id)}/topics/${encodeURIComponent(topicId)}/rows?n=5`,
        );
        App.topics.exampleRows[topicId] = res.rows || [];
    } catch (err) {
        alert('Could not load examples: ' + err.message);
        App.topics.exampleRows[topicId] = [];
    }
    renderTopicsViewerCard(App.state, ts);
}

function _topicById(ts, topicId) {
    return (ts.topics || []).find(t => t.topic_id === topicId);
}

function _rowsForTopic(ts, topicId) {
    const assignments = ts.row_assignments || {};
    return Object.keys(assignments)
        .filter(k => assignments[k] === topicId)
        .map(k => Number(k));
}

async function promoteTopicToCodebook(topicId) {
    const ts = activeTopicSet(App.state);
    if (!ts) return;
    const topic = _topicById(ts, topicId);
    if (!topic) return;
    const cbId = App.state?.active_codebook;
    if (!cbId) {
        alert('No active codebook. Open Codebook first and create or activate one.');
        return;
    }
    const title = window.prompt(
        `Create a new category in the active codebook from this topic?\n\n` +
        `Category title:`,
        topic.name || topic.topic_id || '',
    );
    if (title === null) return;
    const t = (title || '').trim();
    if (!t) return;
    try {
        await fetchJson(`/api/codebooks/${encodeURIComponent(cbId)}/categories`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                title: t,
                description: topic.description || '',
                color: '#C8175D',
            }),
        });
        await refreshState();
        logJourney('topic_promoted_to_codebook', { topic_id: topicId, title: t });
        alert(`Added "${t}" to the active codebook. Open Codebook to tag with it.`);
    } catch (err) {
        alert('Could not promote: ' + err.message);
    }
}

async function tagAllInTopic(topicId) {
    const ts = activeTopicSet(App.state);
    if (!ts) return;
    const topic = _topicById(ts, topicId);
    if (!topic) return;
    const cbId = App.state?.active_codebook;
    const cb = cbId ? App.state?.codebooks?.[cbId] : null;
    if (!cb) {
        alert('No active codebook. Open Codebook first and create or activate one.');
        return;
    }
    const coder = App.state?.coding?.coder_name || '';
    if (!coder) {
        alert('Set your coder name in Settings before bulk-tagging.');
        return;
    }
    const rowIndices = _rowsForTopic(ts, topicId);
    if (!rowIndices.length) {
        alert('No rows are classified into this topic yet. Run "Classify rest" first.');
        return;
    }
    const cats = cb.categories || [];
    if (!cats.length) {
        alert('The active codebook has no categories. Add one first (or promote this topic).');
        return;
    }
    const listing = cats.map((c, i) => `${i + 1}. ${c.title}`).join('\n');
    const pick = window.prompt(
        `Tag all ${rowIndices.length.toLocaleString()} rows in "${topic.name || topicId}" with which category?\n\n` +
        `${listing}\n\n` +
        `Enter the number (1–${cats.length}):`,
        '1',
    );
    if (pick === null) return;
    const idx = parseInt(pick, 10);
    if (!Number.isFinite(idx) || idx < 1 || idx > cats.length) return;
    const cat = cats[idx - 1];
    if (!window.confirm(`Tag ${rowIndices.length.toLocaleString()} rows with "${cat.title}"? This creates a snapshot first and can be undone.`)) return;
    try {
        const res = await fetchJson('/api/coding/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ row_indices: rowIndices, cat_id: cat.cat_id }),
        });
        logJourney('topic_tag_all', { topic_id: topicId, cat_id: cat.cat_id, rows: rowIndices.length });
        alert(`Tagged ${res.rows_affected} rows with "${cat.title}".`);
        await refreshState();
    } catch (err) {
        alert('Bulk-tag failed: ' + err.message);
    }
}

async function drillIntoTopic(topicId) {
    const ts = activeTopicSet(App.state);
    if (!ts) return;
    const topic = _topicById(ts, topicId);
    if (!topic) return;
    const rowIndices = _rowsForTopic(ts, topicId);
    if (!rowIndices.length) {
        alert('No rows are classified into this topic yet. Run "Classify rest" first.');
        return;
    }
    const defaultName = `Topic · ${topic.name || topicId}`;
    const name = window.prompt(
        `Save ${rowIndices.length.toLocaleString()} rows as a slice and open the Slicer.\n\n` +
        `Slice name:`,
        defaultName,
    );
    if (name === null) return;
    const n = (name || '').trim();
    if (!n) return;
    try {
        const res = await fetchJson('/api/slices/from_indices', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: n,
                indices: rowIndices,
                note: `Drill-in from topic "${topic.name || topicId}" (${ts.name})`,
            }),
        });
        logJourney('topic_drill_in', { topic_id: topicId, slice_id: res.slice?.slice_id, rows: rowIndices.length });
        await refreshState();
        navigateTo('slicer');
    } catch (err) {
        alert('Could not create slice: ' + err.message);
    }
}

function setText(sel, text) {
    const el = $(sel);
    if (el) el.textContent = text;
}

function truncate(s, n) {
    s = String(s || '');
    if (s.length <= n) return s;
    return s.slice(0, n - 1) + '…';
}

/* ── 6c. ANALYTICS (Phase 7) ────────────────────────────────────────────── */
const Analytics = {
    columns: null,          // {categorical, numeric, datetime, list, slices, corpus_rows}
    scopeA: '',             // slice_id or '' for whole corpus
    scopeB: '',
    compare: false,
    ts: {freq: 'month', group: '', agg: 'count', value_col: ''},
    ct: {row: '', col: '', normalize: ''},
    ng: {n: 1, top_k: 25, min_count: 2, drop_stopwords: true, extra_stopwords: '', stopword_langs: ''},
    charts: {tsA: null, tsB: null, ngA: null, ngB: null},
    latest: {tsA: null, tsB: null, ctA: null, ctB: null, ngA: null, ngB: null, descA: null, descB: null, cmp: null},
    initDone: false,
    stopwordLangsLoaded: false,
};

async function loadStopwordLanguages() {
    if (Analytics.stopwordLangsLoaded) return;
    Analytics.stopwordLangsLoaded = true;
    const sel = $('#an-ng-lang');
    if (!sel) return;
    try {
        const data = await fetchJson('/api/analytics/stopword_languages');
        const items = data?.items || [];
        const current = sel.value;
        const options = ['<option value="">Auto (EN + ES + PT)</option>']
            .concat(items.map(it => `<option value="${escapeHtml(it.code)}">${escapeHtml(it.label)}</option>`))
            .concat(['<option value="__none">None (raw counts)</option>']);
        sel.innerHTML = options.join('');
        if (current) sel.value = current;
    } catch (_) {
        // Leave the default option in place.
    }
}

function renderAnalytics(state) {
    const hasCorpus = !!(state?.corpus?.built);
    const gate  = $('#an-gate-card');
    const scope = $('#an-scope-card');
    const desc  = $('#an-desc-card');
    const ts    = $('#an-ts-card');
    const ct    = $('#an-ct-card');
    const ng    = $('#an-ng-card');
    if (!gate || !scope) return;

    if (!hasCorpus) {
        gate.style.display  = 'block';
        scope.style.display = 'none';
        desc.style.display  = 'none';
        ts.style.display    = 'none';
        ct.style.display    = 'none';
        ng.style.display    = 'none';
        setText('#an-gate-msg', 'Build a corpus on the Corpus tab first, then come back here.');
        return;
    }
    gate.style.display  = 'none';
    scope.style.display = 'block';
    desc.style.display  = 'block';
    ts.style.display    = 'block';
    ct.style.display    = 'block';
    ng.style.display    = 'block';

    attachAnalyticsHandlers();
    loadAnalyticsColumns().catch(err => console.error('[analytics] columns', err));
    loadStopwordLanguages().catch(err => console.error('[analytics] stopword langs', err));
}

function attachAnalyticsHandlers() {
    if (Analytics.initDone) return;
    Analytics.initDone = true;

    $('#an-scope-a')?.addEventListener('change', e => {
        Analytics.scopeA = e.target.value;
        redrawAllAnalytics();
    });
    $('#an-scope-b')?.addEventListener('change', e => {
        Analytics.scopeB = e.target.value;
        redrawAllAnalytics();
    });
    $('#an-compare-toggle')?.addEventListener('change', e => {
        Analytics.compare = !!e.target.checked;
        const row = $('#an-scope-b-row');
        if (row) row.style.display = Analytics.compare ? '' : 'none';
        if (Analytics.compare && !Analytics.scopeB) {
            const firstSlice = Analytics.columns?.slices?.[0]?.slice_id || '';
            Analytics.scopeB = firstSlice;
            const sel = $('#an-scope-b');
            if (sel) sel.value = firstSlice;
        }
        ['#an-ts-wrap-b', '#an-ng-wrap-b'].forEach(id => {
            const el = $(id);
            if (el) el.style.display = Analytics.compare ? '' : 'none';
        });
        redrawAllAnalytics();
    });

    $('#an-ts-freq')?.addEventListener('change', e => { Analytics.ts.freq = e.target.value; drawTimeseries(); });
    $('#an-ts-group')?.addEventListener('change', e => { Analytics.ts.group = e.target.value; drawTimeseries(); });
    $('#an-ts-metric')?.addEventListener('change', e => {
        Analytics.ts.agg = e.target.value;
        const wrap = $('#an-ts-valuecol-wrap');
        if (wrap) wrap.style.display = (Analytics.ts.agg === 'count') ? 'none' : '';
        drawTimeseries();
    });
    $('#an-ts-valuecol')?.addEventListener('change', e => { Analytics.ts.value_col = e.target.value; drawTimeseries(); });

    $('#an-ct-row')?.addEventListener('change',  e => { Analytics.ct.row = e.target.value; drawCrosstab(); });
    $('#an-ct-col')?.addEventListener('change',  e => { Analytics.ct.col = e.target.value; drawCrosstab(); });
    $('#an-ct-norm')?.addEventListener('change', e => { Analytics.ct.normalize = e.target.value; drawCrosstab(); });

    $('#an-ng-n')?.addEventListener('change', e => { Analytics.ng.n = Number(e.target.value); drawNgrams(); });
    $('#an-ng-topk')?.addEventListener('change', e => { Analytics.ng.top_k = Math.max(5, Math.min(100, Number(e.target.value || 25))); drawNgrams(); });
    $('#an-ng-min')?.addEventListener('change', e => { Analytics.ng.min_count = Math.max(1, Number(e.target.value || 2)); drawNgrams(); });
    $('#an-ng-extra')?.addEventListener('change', e => { Analytics.ng.extra_stopwords = e.target.value || ''; drawNgrams(); });
    $('#an-ng-stop')?.addEventListener('change', e => { Analytics.ng.drop_stopwords = !!e.target.checked; drawNgrams(); });
    $('#an-ng-lang')?.addEventListener('change', e => {
        const v = e.target.value || '';
        Analytics.ng.stopword_langs = v;
        // Treat the sentinel "__none" as drop_stopwords=false; the chip stays useful
        // even if the user forgets to uncheck the box.
        const stopChk = $('#an-ng-stop');
        if (v === '__none') {
            Analytics.ng.drop_stopwords = false;
            if (stopChk) stopChk.checked = false;
        } else if (stopChk && !stopChk.checked) {
            Analytics.ng.drop_stopwords = true;
            stopChk.checked = true;
        }
        drawNgrams();
    });

    // CSV download buttons
    $('#an-desc-csv')?.addEventListener('click', () => downloadDescriptivesCsv());
    $('#an-ts-csv')?.addEventListener('click',   () => downloadTimeseriesCsv());
    $('#an-ct-csv')?.addEventListener('click',   () => downloadCrosstabCsv());
    $('#an-ng-csv')?.addEventListener('click',   () => downloadNgramsCsv());
}

async function loadAnalyticsColumns() {
    const cols = await fetchJson('/api/analytics/columns');
    Analytics.columns = cols;

    const sliceOptions = [
        `<option value="">Whole corpus (${formatInt(cols.corpus_rows)} rows)</option>`,
        ...((cols.slices || []).map(sl =>
            `<option value="${escapeHtml(sl.slice_id)}">${escapeHtml(sl.name)} · ${formatInt(sl.row_count)} rows</option>`
        )),
    ].join('');
    const selA = $('#an-scope-a'); if (selA) selA.innerHTML = sliceOptions;
    const selB = $('#an-scope-b'); if (selB) selB.innerHTML = sliceOptions;
    if (selA) selA.value = Analytics.scopeA;
    if (selB) selB.value = Analytics.scopeB;

    // Populate group-by + crosstab selectors from categorical columns.
    const cats = cols.categorical || [];
    const tsGroup = $('#an-ts-group');
    if (tsGroup) tsGroup.innerHTML =
        `<option value="">None</option>` +
        cats.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');

    // Populate value-col dropdown from numeric columns (engagement first).
    const nums = cols.numeric || [];
    const preferred = ['like_count', 'share_count', 'comment_count', 'view_count'];
    const ordered = [...preferred.filter(c => nums.includes(c)), ...nums.filter(c => !preferred.includes(c))];
    const tsVal = $('#an-ts-valuecol');
    if (tsVal) {
        tsVal.innerHTML = ordered.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
        if (!Analytics.ts.value_col || !ordered.includes(Analytics.ts.value_col))
            Analytics.ts.value_col = ordered[0] || '';
        tsVal.value = Analytics.ts.value_col;
    }

    // Default picks for crosstab: platform × language when available, else first two.
    const pickRow = cats.includes('platform') ? 'platform' : (cats[0] || '');
    const pickCol = cats.includes('language') ? 'language' : (cats[1] || cats[0] || '');
    Analytics.ct.row = Analytics.ct.row || pickRow;
    Analytics.ct.col = Analytics.ct.col || pickCol;
    const rowSel = $('#an-ct-row');
    const colSel = $('#an-ct-col');
    if (rowSel) { rowSel.innerHTML = cats.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join(''); rowSel.value = Analytics.ct.row; }
    if (colSel) { colSel.innerHTML = cats.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join(''); colSel.value = Analytics.ct.col; }

    redrawAllAnalytics();
}

function redrawAllAnalytics() {
    drawDescriptives();
    drawTimeseries();
    drawCrosstab();
    drawNgrams();
    updateScopeCountLabels();
}

function updateScopeCountLabels() {
    const slices = Analytics.columns?.slices || [];
    const labelFor = id => {
        if (!id) return `${formatInt(Analytics.columns?.corpus_rows || 0)} rows · whole corpus`;
        const sl = slices.find(s => s.slice_id === id);
        return sl ? `${formatInt(sl.row_count)} rows · ${sl.kind}` : '';
    };
    setText('#an-scope-a-count', labelFor(Analytics.scopeA));
    setText('#an-scope-b-count', Analytics.compare ? labelFor(Analytics.scopeB) : '\u00a0');
}

/* ── Descriptives ─────────────────────────────────────────────────────── */
async function drawDescriptives() {
    const body = $('#an-desc-body');
    if (!body) return;
    body.innerHTML = `<p class="muted small">Loading…</p>`;
    try {
        if (Analytics.compare && Analytics.scopeA !== Analytics.scopeB) {
            const q = new URLSearchParams({slice_a_id: Analytics.scopeA, slice_b_id: Analytics.scopeB});
            const res = await fetchJson(`/api/analytics/compare?${q}`);
            Analytics.latest.cmp = res;
            Analytics.latest.descA = null;
            body.innerHTML = renderDescriptivesCompare(res);
        } else {
            const q = new URLSearchParams({slice_id: Analytics.scopeA});
            const res = await fetchJson(`/api/analytics/descriptives?${q}`);
            Analytics.latest.descA = res;
            Analytics.latest.cmp = null;
            body.innerHTML = renderDescriptivesSingle(res);
        }
    } catch (err) {
        body.innerHTML = `<p class="an-error">${escapeHtml(err.message || String(err))}</p>`;
    }
}

function formatDescTiles(result) {
    const tiles = [];
    tiles.push({label: 'Rows', value: formatInt(result.rows)});
    if (result.unique_authors !== undefined)
        tiles.push({label: 'Unique authors', value: formatInt(result.unique_authors)});
    if (result.date_min && result.date_max) {
        tiles.push({label: 'First post', value: result.date_min.slice(0, 10)});
        tiles.push({label: 'Last post',  value: result.date_max.slice(0, 10)});
        if (result.date_span_days !== undefined)
            tiles.push({label: 'Span (days)', value: formatInt(Math.round(result.date_span_days))});
    }
    if (result.text_nonempty_rows !== undefined)
        tiles.push({label: 'Rows with text', value: formatInt(result.text_nonempty_rows)});
    const eng = result.engagement || {};
    ['like_count', 'share_count', 'comment_count', 'view_count'].forEach(k => {
        if (eng[k]) tiles.push({label: `mean ${k.replace('_count','')}`, value: formatInt(Math.round(eng[k].mean))});
    });
    return tiles;
}

function renderDescriptivesSingle(res) {
    const result = res.result || {};
    const tiles = formatDescTiles(result);
    const tileHtml = tiles.map(t =>
        `<div class="stat-tile"><div class="stat-label">${escapeHtml(t.label)}</div><div class="stat-value">${escapeHtml(String(t.value))}</div></div>`
    ).join('');

    const facets = result.facets || {};
    const facetSections = Object.entries(facets)
        .filter(([, items]) => items && items.length)
        .map(([col, items]) => renderTopList(col, items)).join('');

    const authorsHtml = (result.top_authors && result.top_authors.length)
        ? renderTopList('top authors by post count', result.top_authors) : '';

    const engAuthorsHtml = (result.top_authors_by_engagement && result.top_authors_by_engagement.length)
        ? renderTopList(
            `top authors by ${(result.top_authors_by_engagement_metric || 'engagement').replace('_count','')}`,
            result.top_authors_by_engagement.map(r => ({value: r.value, count: r.sum, share: r.share})))
        : '';

    const listsHtml = Object.entries(result.lists || {})
        .map(([col, items]) => renderTopList(col, items)).join('');

    return `
        <div class="an-tile-grid">${tileHtml}</div>
        <div class="an-compare-grid mt-2">
            <div>${facetSections}</div>
            <div>${authorsHtml}${engAuthorsHtml}${listsHtml}</div>
        </div>`;
}

function renderDescriptivesCompare(res) {
    const a = res.a?.result || {};
    const b = res.b?.result || {};
    const delta = res.delta || {};
    const labelA = res.a?.scope?.slice_name || 'Whole corpus';
    const labelB = res.b?.scope?.slice_name || 'Whole corpus';
    const tilesA = formatDescTiles(a);
    const tilesB = formatDescTiles(b);
    const tile = (t) => `<div class="stat-tile"><div class="stat-label">${escapeHtml(t.label)}</div><div class="stat-value">${escapeHtml(String(t.value))}</div></div>`;

    const deltaLine = (label, d) => {
        if (!d || d.abs === null) return '';
        const cls = d.abs > 0 ? 'an-delta-up' : (d.abs < 0 ? 'an-delta-down' : 'an-delta-flat');
        const arrow = d.abs > 0 ? '▲' : (d.abs < 0 ? '▼' : '·');
        const relTxt = (d.rel === null || d.rel === undefined) ? '' : ` (${(d.rel * 100).toFixed(1)}%)`;
        const abs = Number.isInteger(d.abs) ? formatInt(d.abs) : d.abs.toFixed(2);
        return `<div class="an-hint">${escapeHtml(label)}: <span class="${cls}">${arrow} ${abs}${relTxt}</span></div>`;
    };

    let deltaHtml = deltaLine('Rows', delta.rows) + deltaLine('Unique authors', delta.unique_authors);
    Object.entries(delta.engagement_mean || {}).forEach(([col, d]) => {
        deltaHtml += deltaLine(`mean ${col.replace('_count','')}`, d);
    });

    return `
        <div class="an-compare-grid">
            <div>
                <div class="an-scope-label">${escapeHtml(labelA)}</div>
                <div class="an-tile-grid">${tilesA.map(tile).join('')}</div>
            </div>
            <div>
                <div class="an-scope-label">${escapeHtml(labelB)}</div>
                <div class="an-tile-grid">${tilesB.map(tile).join('')}</div>
            </div>
        </div>
        ${deltaHtml ? `<div class="mt-2">${deltaHtml}</div>` : ''}`;
}

function renderTopList(title, items) {
    const rows = items.slice(0, 10).map(it =>
        `<li><span class="an-val">${escapeHtml(it.value)}</span><span class="an-n">${formatInt(it.count)}${it.share !== undefined ? ' · ' + (it.share*100).toFixed(1) + '%' : ''}</span></li>`
    ).join('');
    return `<div class="an-section-label">${escapeHtml(title)}</div><ul class="an-top-list">${rows}</ul>`;
}

/* ── Time-series ──────────────────────────────────────────────────────── */
async function drawTimeseries() {
    const wrapB = $('#an-ts-wrap-b');
    if (wrapB) wrapB.style.display = Analytics.compare ? '' : 'none';
    await drawTimeseriesOne('tsA', '#an-ts-chart-a', Analytics.scopeA);
    if (Analytics.compare) await drawTimeseriesOne('tsB', '#an-ts-chart-b', Analytics.scopeB);
}

async function drawTimeseriesOne(key, containerSel, sliceId) {
    const container = $(containerSel);
    if (!container) return;
    const q = new URLSearchParams({slice_id: sliceId, freq: Analytics.ts.freq, agg: Analytics.ts.agg});
    if (Analytics.ts.group) q.set('group', Analytics.ts.group);
    if (Analytics.ts.agg !== 'count' && Analytics.ts.value_col) q.set('value_col', Analytics.ts.value_col);
    try {
        const res = await fetchJson(`/api/analytics/timeseries?${q}`);
        Analytics.latest[key] = res;
        const result = res.result || {};
        const scopeLabel = res.scope?.slice_name || 'Whole corpus';
        const metricTxt = Analytics.ts.agg === 'count' ? 'row count' :
            `${Analytics.ts.agg} ${Analytics.ts.value_col || ''}`.trim();
        const title = `${scopeLabel} · ${formatInt(result.rows_counted || 0)} rows · ${Analytics.ts.freq} · ${metricTxt}`;
        const palette = chartPalette(result.series.length);
        
        const series = result.series.map((s, i) => ({
            name: s.name,
            type: 'line',
            data: s.data,
            stack: Analytics.ts.group ? 'Total' : undefined,
            areaStyle: Analytics.ts.group ? { opacity: 0.3 } : undefined,
            itemStyle: { color: palette[i] },
            lineStyle: { width: 2 },
            symbolSize: result.buckets.length > 80 ? 0 : 6,
            showSymbol: result.buckets.length <= 80,
            smooth: true
        }));

        const option = {
            title: { text: title, left: 'center', textStyle: { fontSize: 13, fontWeight: 'normal', color: '#0f0f0f' } },
            tooltip: { trigger: 'axis' },
            legend: { bottom: 0, show: !!Analytics.ts.group, type: 'scroll' },
            dataZoom: [
                { type: 'slider', show: true, bottom: Analytics.ts.group ? '5%' : '0%' },
                { type: 'inside' }
            ],
            grid: { left: '3%', right: '4%', bottom: Analytics.ts.group ? '20%' : '15%', containLabel: true },
            xAxis: { type: 'category', boundaryGap: false, data: result.buckets.map(b => b.slice(0, 10)) },
            yAxis: { type: 'value' },
            series: series
        };

        replaceChart(key, container, option);
    } catch (err) {
        console.error('[analytics] timeseries', err);
        if (Analytics.charts[key]) { Analytics.charts[key].dispose(); Analytics.charts[key] = null; }
        container.innerHTML = `<p class="muted small">Failed to load chart: ${escapeHtml(err.message)}</p>`;
    }
}

/* ── Crosstab ─────────────────────────────────────────────────────────── */
async function drawCrosstab() {
    const body = $('#an-ct-body');
    if (!body) return;
    if (!Analytics.ct.row || !Analytics.ct.col) {
        body.innerHTML = `<p class="muted small">Pick a row and column to render a cross-tab.</p>`;
        return;
    }
    body.innerHTML = `<p class="muted small">Loading…</p>`;
    try {
        const blocks = [await crosstabBlock(Analytics.scopeA, 'A')];
        if (Analytics.compare && Analytics.scopeA !== Analytics.scopeB) {
            blocks.push(await crosstabBlock(Analytics.scopeB, 'B'));
        }
        body.innerHTML = Analytics.compare && blocks.length === 2
            ? `<div class="an-compare-grid">${blocks.join('')}</div>`
            : blocks.join('');
    } catch (err) {
        body.innerHTML = `<p class="an-error">${escapeHtml(err.message || String(err))}</p>`;
    }
}

async function crosstabBlock(sliceId, label) {
    const q = new URLSearchParams({
        slice_id: sliceId,
        row: Analytics.ct.row,
        col: Analytics.ct.col,
    });
    if (Analytics.ct.normalize) q.set('normalize', Analytics.ct.normalize);
    const res = await fetchJson(`/api/analytics/crosstab?${q}`);
    Analytics.latest['ct' + label] = res;
    return renderCrosstabTable(res, label);
}

function renderCrosstabTable(res, label) {
    const r = res.result || {};
    const scopeLabel = res.scope?.slice_name || 'Whole corpus';
    const normLabel = {row: '% of row', col: '% of column', all: '% of total'}[r.normalize] || 'counts';
    const hasNorm = !!r.normalized;
    const displayMatrix = hasNorm ? r.normalized : r.counts;

    // Max for intensity shading
    let maxVal = 0;
    for (const row of displayMatrix) for (const v of row) if (v > maxVal) maxVal = v;
    const intensity = v => (maxVal > 0 ? Math.min(1, v / maxVal) : 0);
    const fmt = hasNorm ? (v => (v * 100).toFixed(1) + '%') : (v => formatInt(v));

    const head = `<tr><th class="an-ct-corner">${escapeHtml(r.row_col)} \\ ${escapeHtml(r.col_col)}</th>` +
        r.col_labels.map(c => `<th>${escapeHtml(c)}</th>`).join('') +
        `<th class="an-ct-total">total</th></tr>`;
    const rows = r.row_labels.map((row, i) => {
        const cells = displayMatrix[i].map((v, j) => {
            const int = intensity(v);
            return `<td data-intensity style="--an-int:${int.toFixed(3)};">${fmt(v)}</td>`;
        }).join('');
        return `<tr><th>${escapeHtml(row)}</th>${cells}<td class="an-ct-total">${formatInt(r.totals_row[i])}</td></tr>`;
    }).join('');
    const footCells = r.totals_col.map(v => `<td class="an-ct-total">${formatInt(v)}</td>`).join('');
    const foot = `<tr><th class="an-ct-total">total</th>${footCells}<td class="an-ct-total">${formatInt(r.total)}</td></tr>`;

    const header = `<div class="an-scope-label">${escapeHtml(scopeLabel)} · ${escapeHtml(normLabel)} · ${formatInt(r.rows_counted)} rows counted${(r.truncated_rows || r.truncated_cols) ? ' · top buckets only' : ''}</div>`;
    return `<div class="an-ct-wrap">${header}<table class="an-ct-table"><thead>${head}</thead><tbody>${rows}</tbody><tfoot>${foot}</tfoot></table></div>`;
}

/* ── N-grams ──────────────────────────────────────────────────────────── */
async function drawNgrams() {
    const wrapB = $('#an-ng-wrap-b');
    if (wrapB) wrapB.style.display = Analytics.compare ? '' : 'none';
    await drawNgramsOne('ngA', '#an-ng-chart-a', Analytics.scopeA);
    if (Analytics.compare) await drawNgramsOne('ngB', '#an-ng-chart-b', Analytics.scopeB);
}

async function drawNgramsOne(key, containerSel, sliceId) {
    const container = $(containerSel);
    if (!container) return;
    const q = new URLSearchParams({
        slice_id: sliceId,
        n: String(Analytics.ng.n),
        top_k: String(Analytics.ng.top_k),
        min_count: String(Analytics.ng.min_count || 2),
        drop_stopwords: String(!!Analytics.ng.drop_stopwords),
    });
    if (Analytics.ng.extra_stopwords) q.set('extra_stopwords', Analytics.ng.extra_stopwords);
    // "" = server default (EN+ES+PT). "__none" means "don't drop anything" — handled
    // via drop_stopwords=false by the change handler. A real language code flows through.
    if (Analytics.ng.stopword_langs && Analytics.ng.stopword_langs !== '__none') {
        q.set('stopword_langs', Analytics.ng.stopword_langs);
    }
    try {
        const res = await fetchJson(`/api/analytics/ngrams?${q}`);
        Analytics.latest[key] = res;
        const result = res.result || {};
        const scopeLabel = res.scope?.slice_name || 'Whole corpus';
        const displayData = result.items.map(it => it.count).reverse();
        const displayLabels = result.items.map(it => wrapLabel(it.gram, 32, 2)).reverse();
        const title = `${scopeLabel} · top ${result.top_k} · ${formatInt(result.docs)} docs · vocab ${formatInt(result.vocabulary)}`;

        const option = {
            title: { text: title, left: 'center', textStyle: { fontSize: 13, fontWeight: 'normal', color: '#0f0f0f' } },
            tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
            grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
            xAxis: { type: 'value', boundaryGap: [0, 0.01] },
            yAxis: { type: 'category', data: displayLabels, axisLabel: { interval: 0, fontSize: 11, color: '#0f0f0f' } },
            series: [{
                name: `n=${result.n}`,
                type: 'bar',
                data: displayData,
                itemStyle: { color: 'rgba(200, 23, 93, 0.7)' },
                barMaxWidth: 30
            }]
        };

        // Dynamic height so long lists stay readable.
        const parent = container.parentElement;
        if (parent) parent.style.height = Math.max(320, Math.min(900, 28 * displayData.length + 60)) + 'px';
        
        replaceChart(key, container, option);
    } catch (err) {
        console.error('[analytics] ngrams', err);
        if (Analytics.charts[key]) { Analytics.charts[key].dispose(); Analytics.charts[key] = null; }
        container.innerHTML = `<p class="muted small">Failed to load chart: ${escapeHtml(err.message)}</p>`;
    }
}

/* ── Chart helpers ────────────────────────────────────────────────────── */
function replaceChart(key, container, option) {
    // If a different instance is tracked under this key (happens after a
    // nav re-renders the container), dispose the old one before adopting
    // the fresh one. Prevents the "tens of detached canvases" memory path.
    const tracked = Analytics.charts[key];
    const live = echarts.getInstanceByDom(container);
    if (tracked && tracked !== live) {
        try { tracked.dispose(); } catch (_) {}
        Analytics.charts[key] = null;
    }
    let myChart = live;
    if (!myChart) {
        myChart = echarts.init(container);
        Analytics.charts[key] = myChart;
    } else {
        Analytics.charts[key] = myChart;
    }
    myChart.setOption(option, true);
    myChart.resize();
}

// Charts whose containers live under #page-<name>. Dispose on navigation so
// detached canvases don't accumulate as the user flips between pages.
const ChartsByPage = {
    analytics: ['tsA', 'tsB', 'ngA', 'ngB'],
    topics: [],
    ai: [],
};

function disposePageCharts(pageId) {
    if (!pageId) return;
    const keys = ChartsByPage[pageId] || [];
    for (const k of keys) {
        const inst = Analytics.charts[k];
        if (inst) {
            try { inst.dispose(); } catch (_) {}
            Analytics.charts[k] = null;
        }
    }
}

function chartPalette(n) {
    const base = [
        '#C8175D', '#0F766E', '#2563EB', '#F59E0B', '#7C3AED',
        '#059669', '#DB2777', '#374151', '#B91C1C', '#0EA5E9',
    ];
    const out = [];
    for (let i = 0; i < n; i++) out.push(base[i % base.length]);
    return out;
}

/* ── CSV download helpers ─────────────────────────────────────────────── */
function csvEscape(v) {
    if (v === null || v === undefined) return '';
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}
function downloadCsv(filename, rows) {
    const text = rows.map(r => r.map(csvEscape).join(',')).join('\r\n');
    // BOM so Excel opens UTF-8 cleanly
    const blob = new Blob(['\ufeff' + text], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 100);
}
function scopeSlug(res) {
    const name = (res?.scope?.slice_name || 'corpus').toString();
    return (name.replace(/[^A-Za-z0-9_-]+/g, '_').slice(0, 40)) || 'corpus';
}

function downloadDescriptivesCsv() {
    const rows = [['metric', 'scope', 'value']];
    const pushResult = (res) => {
        if (!res) return;
        const scope = res.scope?.slice_name || 'Whole corpus';
        const r = res.result || {};
        ['rows', 'unique_authors', 'date_min', 'date_max', 'date_span_days', 'text_nonempty_rows'].forEach(k => {
            if (r[k] !== undefined) rows.push([k, scope, r[k]]);
        });
        const eng = r.engagement || {};
        Object.entries(eng).forEach(([col, summary]) => {
            if (summary) Object.entries(summary).forEach(([stat, v]) => rows.push([`${col}.${stat}`, scope, v]));
        });
        (r.top_authors || []).forEach((a, i) =>
            rows.push([`top_author_${i+1}`, scope, `${a.value} (${a.count})`]));
        (r.top_authors_by_engagement || []).forEach((a, i) =>
            rows.push([`top_author_by_${r.top_authors_by_engagement_metric || 'engagement'}_${i+1}`, scope, `${a.value} (${a.sum})`]));
        Object.entries(r.facets || {}).forEach(([col, items]) =>
            items.forEach((it, i) => rows.push([`top_${col}_${i+1}`, scope, `${it.value} (${it.count})`])));
    };
    if (Analytics.latest.cmp) {
        pushResult(Analytics.latest.cmp.a);
        pushResult(Analytics.latest.cmp.b);
    } else {
        pushResult(Analytics.latest.descA);
    }
    if (rows.length === 1) { alert('Nothing to export yet.'); return; }
    downloadCsv(`descriptives_${scopeSlug(Analytics.latest.descA || Analytics.latest.cmp?.a)}.csv`, rows);
}

function downloadTimeseriesCsv() {
    const packs = [Analytics.latest.tsA];
    if (Analytics.compare && Analytics.latest.tsB) packs.push(Analytics.latest.tsB);
    const rows = [['scope', 'bucket', 'series', 'value']];
    let any = false;
    for (const res of packs) {
        if (!res) continue;
        const scope = res.scope?.slice_name || 'Whole corpus';
        const r = res.result || {};
        (r.buckets || []).forEach((b, i) => {
            (r.series || []).forEach(s => {
                rows.push([scope, b, s.name, s.data[i]]);
                any = true;
            });
        });
    }
    if (!any) { alert('Nothing to export yet.'); return; }
    downloadCsv(`timeseries_${scopeSlug(packs[0])}.csv`, rows);
}

function downloadCrosstabCsv() {
    const packs = [Analytics.latest.ctA];
    if (Analytics.compare && Analytics.latest.ctB) packs.push(Analytics.latest.ctB);
    const rows = [];
    let any = false;
    for (const res of packs) {
        if (!res) continue;
        const r = res.result || {};
        const scope = res.scope?.slice_name || 'Whole corpus';
        rows.push([`# ${scope} · ${r.row_col} × ${r.col_col} · ${r.normalize || 'counts'}`]);
        rows.push([`${r.row_col} \\ ${r.col_col}`, ...r.col_labels, 'total']);
        const mat = r.normalized || r.counts;
        (r.row_labels || []).forEach((lbl, i) => {
            rows.push([lbl, ...mat[i], r.totals_row[i]]);
        });
        rows.push(['total', ...r.totals_col, r.total]);
        rows.push([]);
        any = true;
    }
    if (!any) { alert('Nothing to export yet.'); return; }
    downloadCsv(`crosstab_${scopeSlug(packs[0])}.csv`, rows);
}

function downloadNgramsCsv() {
    const packs = [Analytics.latest.ngA];
    if (Analytics.compare && Analytics.latest.ngB) packs.push(Analytics.latest.ngB);
    const rows = [['scope', 'rank', 'gram', 'count', 'doc_count', 'doc_share']];
    let any = false;
    for (const res of packs) {
        if (!res) continue;
        const scope = res.scope?.slice_name || 'Whole corpus';
        (res.result?.items || []).forEach((it, i) => {
            rows.push([scope, i + 1, it.gram, it.count, it.doc_count, it.doc_share]);
            any = true;
        });
    }
    if (!any) { alert('Nothing to export yet.'); return; }
    downloadCsv(`ngrams_${scopeSlug(packs[0])}.csv`, rows);
}

/* ── 6.X EXPORT & PROVENANCE (Phase 8) ───────────────────────────────────── */
const Export = { preview: null, busy: false };

function renderExport(state) {
    // Disabled stub guards — the page also works even if pieces are empty.
    refreshExportPreview();
    const btn = $('#btn-export-bundle');
    if (btn && !btn.dataset.bound) {
        btn.dataset.bound = '1';
        btn.addEventListener('click', runExportBundle);
    }
    // Bundle checkbox change listeners — repaint the preview line.
    ['#ex-corpus-csv','#ex-corpus-xlsx','#ex-codebooks','#ex-topics','#ex-slices','#ex-provenance'].forEach(sel => {
        const el = $(sel);
        if (el && !el.dataset.bound) {
            el.dataset.bound = '1';
            el.addEventListener('change', updateExportReadyHint);
        }
    });
}

async function refreshExportPreview() {
    const tiles = $('#export-preview-tiles');
    if (!tiles) return;
    tiles.innerHTML = '<div class="stat-tile"><div class="stat-label">Loading…</div><div class="stat-value">—</div></div>';
    try {
        const p = await fetchJson('/api/export/preview');
        Export.preview = p;
        const items = [
            { label: 'Corpus rows',     value: (p.corpus_rows || 0).toLocaleString(), accent: true },
            { label: 'Rows with tag',   value: (p.rows_with_tag || 0).toLocaleString() },
            { label: 'Rows with topic', value: (p.rows_with_topic || 0).toLocaleString() },
            { label: 'Codebooks',       value: `${p.codebooks || 0} · ${p.codebook_categories || 0} cats` },
            { label: 'Topic runs',      value: `${p.topic_sets || 0} · ${p.topics || 0} topics` },
            { label: 'Saved slices',    value: (p.slices || 0).toString() },
            { label: 'Log events',      value: (p.events || 0).toLocaleString() },
        ];
        tiles.innerHTML = items.map(it =>
            `<div class="stat-tile${it.accent ? ' accent' : ''}"><div class="stat-label">${it.label}</div><div class="stat-value">${it.value}</div></div>`
        ).join('');

        // Disable XLSX checkbox + link when openpyxl isn't installed.
        const xlsxBox = $('#ex-corpus-xlsx');
        const xlsxLink = $('#dl-corpus-xlsx');
        if (xlsxBox) {
            xlsxBox.disabled = !p.xlsx_available;
            if (!p.xlsx_available) xlsxBox.checked = false;
            const lbl = xlsxBox.closest('label');
            if (lbl) lbl.title = p.xlsx_available ? '' : 'Install openpyxl to enable XLSX export.';
        }
        if (xlsxLink) {
            xlsxLink.classList.toggle('disabled-link', !p.xlsx_available);
            xlsxLink.title = p.xlsx_available ? '' : 'Install openpyxl to enable XLSX export.';
        }
        // Disable corpus CSV link when no corpus exists.
        const canCorpus = (p.corpus_rows || 0) > 0;
        const csvBox = $('#ex-corpus-csv');
        if (csvBox) csvBox.disabled = !canCorpus;
        $('#dl-corpus-csv')?.classList.toggle('disabled-link', !canCorpus);
        updateExportReadyHint();
    } catch (err) {
        tiles.innerHTML = `<div class="stat-tile"><div class="stat-label">Error</div><div class="stat-value small">${err.message}</div></div>`;
    }
}

function updateExportReadyHint() {
    const hint = $('#export-bundle-status');
    const any = ['#ex-corpus-csv','#ex-corpus-xlsx','#ex-codebooks','#ex-topics','#ex-slices','#ex-provenance']
        .some(sel => $(sel)?.checked);
    const btn = $('#btn-export-bundle');
    if (btn) btn.disabled = !any;
    renderExportWarnings();
    renderExportBundleTree();
    if (!hint) return;
    // Only write "pick at least one" when nothing is checked — never stomp a
    // success/error message we set elsewhere.
    if (!any) hint.textContent = 'Pick at least one item to include.';
    else if (hint.textContent === 'Pick at least one item to include.') hint.textContent = '';
}

function renderExportWarnings() {
    const box = $('#export-warnings');
    if (!box) return;
    const s = App.state || {};
    const p = Export.preview || {};
    const warns = [];
    const coder = (s.coding?.coder_name || '').trim();
    if (!coder) {
        warns.push(`<strong>No coder name on file.</strong> Tags in the export won't have an author attribution — set one in <a href="#page-settings" id="exw-settings" style="color:var(--isd-pink);">Settings</a> before exporting if this matters for reproducibility.`);
    }
    if ((p.corpus_rows || 0) === 0 && $('#ex-corpus-csv')?.checked) {
        warns.push(`<strong>No corpus built yet</strong> — the CSV/XLSX output will be empty. Build a corpus first, or uncheck those boxes.`);
    }
    const tagged = p.rows_with_tag || 0;
    const total = p.corpus_rows || 0;
    if (total > 0 && tagged === 0 && $('#ex-codebooks')?.checked) {
        warns.push(`<strong>No rows tagged.</strong> The codebook will export with zero applied tags — collaborators will need to tag from scratch.`);
    }
    if (!warns.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
    box.style.display = 'block';
    box.innerHTML = warns.map(w => `
        <div class="glass-card" style="border-left:4px solid var(--warn); padding:0.65rem 0.9rem; margin-bottom:0.45rem; background:rgba(255,247,230,0.55);">
            <i class="fa-solid fa-triangle-exclamation" style="color:var(--warn); margin-right:0.4rem;"></i>${w}
        </div>`).join('');
    $('#exw-settings')?.addEventListener('click', e => { e.preventDefault(); navigateTo('settings'); });
}

function renderExportBundleTree() {
    const box = $('#export-bundle-tree');
    if (!box) return;
    const p = Export.preview || {};
    const state = App.state || {};
    const items = [];
    if ($('#ex-corpus-csv')?.checked && (p.corpus_rows || 0) > 0) {
        items.push({ path: 'corpus.csv', meta: `${(p.corpus_rows || 0).toLocaleString()} rows` });
    }
    if ($('#ex-corpus-xlsx')?.checked && (p.corpus_rows || 0) > 0 && p.xlsx_available) {
        items.push({ path: 'corpus.xlsx', meta: `${(p.corpus_rows || 0).toLocaleString()} rows` });
    }
    if ($('#ex-codebooks')?.checked) {
        items.push({ path: 'codebooks.json', meta: `${p.codebooks || 0} codebook${(p.codebooks || 0) === 1 ? '' : 's'} · ${p.codebook_categories || 0} categories` });
    }
    if ($('#ex-topics')?.checked) {
        items.push({ path: 'topics.json', meta: `${p.topic_sets || 0} run${(p.topic_sets || 0) === 1 ? '' : 's'} · ${p.topics || 0} topics` });
    }
    if ($('#ex-slices')?.checked) {
        items.push({ path: 'slices.json', meta: `${p.slices || 0} saved slice${(p.slices || 0) === 1 ? '' : 's'}` });
    }
    if ($('#ex-provenance')?.checked) {
        items.push({ path: 'provenance.md', meta: `methods-style narrative` });
        items.push({ path: 'events.json', meta: `${(p.events || 0).toLocaleString()} raw event${(p.events || 0) === 1 ? '' : 's'}` });
    }
    if (!items.length) { box.style.display = 'none'; box.innerHTML = ''; return; }
    box.style.display = 'block';
    box.innerHTML = `
        <div class="glass-card" style="padding:0.75rem 1rem; background:rgba(255,255,255,0.4);">
            <div class="flex-row" style="justify-content:space-between; align-items:center; gap:0.5rem;">
                <strong>Bundle contents</strong>
                <span class="muted small">${items.length} file${items.length === 1 ? '' : 's'} in the ZIP</span>
            </div>
            <ul style="margin:0.5rem 0 0; padding-left:1.1rem; line-height:1.55; font-family:ui-monospace, monospace; font-size:0.88rem;">
                ${items.map(it => `<li><code>${escapeHtml(it.path)}</code> <span class="muted small">— ${escapeHtml(it.meta)}</span></li>`).join('')}
            </ul>
        </div>`;
}

async function runExportBundle() {
    if (Export.busy) return;
    const body = {
        corpus_csv:  $('#ex-corpus-csv')?.checked  || false,
        corpus_xlsx: $('#ex-corpus-xlsx')?.checked || false,
        codebooks:   $('#ex-codebooks')?.checked   || false,
        topics:      $('#ex-topics')?.checked      || false,
        slices:      $('#ex-slices')?.checked      || false,
        provenance:  $('#ex-provenance')?.checked  || false,
    };
    const btn = $('#btn-export-bundle');
    const hint = $('#export-bundle-status');
    Export.busy = true;
    if (btn) { btn.disabled = true; btn.classList.add('loading'); }
    if (hint) hint.textContent = 'Building bundle…';
    try {
        const res = await fetch('/api/export/bundle', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            let detail = res.statusText;
            try { const j = await res.json(); detail = j.detail || detail; } catch(_) {}
            throw new Error(`${res.status} ${detail}`);
        }
        const blob = await res.blob();
        const cd = res.headers.get('Content-Disposition') || '';
        const m = cd.match(/filename="([^"]+)"/);
        const filename = m ? m[1] : 'corpus-intel-bundle.zip';
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click();
        setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 200);
        // Refresh tiles only (full refreshState re-renders and would clear the hint).
        refreshExportPreview();
        if (hint) hint.textContent = `Downloaded ${filename} (${(blob.size/1024).toFixed(1)} KB).`;
    } catch (err) {
        if (hint) hint.textContent = `Error: ${err.message}`;
    } finally {
        Export.busy = false;
        if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
    }
}

/* ── 7. BOOTSTRAP ───────────────────────────────────────────────────────── */
async function refreshState() {
    App.state = await fetchJson('/api/state');
    maybeShowAIHealthBanner(App.state);
    // Re-render any open page
    const hook = PageHooks[App.currentPage];
    if (typeof hook === 'function') hook(App.state);
}

async function init() {
    // Nav
    const navEl = $('.nav-links');
    if (navEl) navEl.addEventListener('click', e => {
        const a = e.target.closest('a[data-page]');
        if (a) { e.preventDefault(); navigateTo(a.dataset.page); }
    });

    // Home — goal picker
    $('#home-goal-grid')?.addEventListener('click', (e) => {
        const card = e.target.closest('.goal-card');
        if (card) setGoal(card.dataset.goal);
    });
    // Home — next-step CTA
    $('#btn-home-next')?.addEventListener('click', () => {
        if (_homeNextTarget) navigateTo(_homeNextTarget);
    });
    // Home — resume banner
    $('#btn-resume-continue')?.addEventListener('click', () => {
        if (_homeNextTarget) navigateTo(_homeNextTarget);
    });
    $('#btn-resume-dismiss')?.addEventListener('click', () => {
        sessionStorage.setItem('ci_resume_dismissed', '1');
        const b = $('#home-resume-banner'); if (b) b.hidden = true;
    });
    $('#btn-resume-fresh')?.addEventListener('click', startFreshWorkspace);

    // Home — take-a-tour CTA
    $('#home-tour-start')?.addEventListener('click', () => startTour('first_time'));
    $('#home-tour-dismiss')?.addEventListener('click', () => {
        localStorage.setItem('ci_tour_dismissed', '1');
        const cta = $('#home-tour-cta'); if (cta) cta.style.display = 'none';
    });
    // Tour modal buttons
    $('#tour-next')?.addEventListener('click', nextTourStep);
    $('#tour-prev')?.addEventListener('click', prevTourStep);
    document.querySelectorAll('#tour-modal [data-tour-close]').forEach(el => {
        el.addEventListener('click', closeTourModal);
    });

    // Glossary tooltip — global triggers
    setupGlossary();

    // Standard Library & Regex Pre-Coding
    setupCodebookLibrary();
    setupRegexPrecoding();

    // Upload + mapping wiring
    setupUpload();
    $('#btn-save-mapping')?.addEventListener('click', saveMapping);
    $('#btn-ai-suggest')?.addEventListener('click', aiSuggest);
    $('#btn-go-to-corpus')?.addEventListener('click', () => navigateTo('corpus'));

    // Settings
    $('#btn-save-key')?.addEventListener('click', saveApiKey);
    $('#btn-clear-key')?.addEventListener('click', clearApiKey);
    $('#btn-save-coder')?.addEventListener('click', saveCoderName);
    $('#btn-save-budget')?.addEventListener('click', saveBudget);
    $('#s-coder-name')?.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); saveCoderName(); }
    });

    // Corpus — builder
    $('#btn-build-corpus')?.addEventListener('click', buildCorpus);
    $('#btn-rebuild-corpus')?.addEventListener('click', () => {
        const datasets = Object.keys(App.state?.datasets || {});
        App.corpus.pickerSelection = new Set(App.corpus.stats?.per_dataset?.map(d => d.dataset_id) || datasets);
        renderCorpus(App.state);
        $('#corpus-picker-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    $('#btn-cancel-rebuild')?.addEventListener('click', () => {
        App.corpus.pickerSelection = null;
        renderCorpus(App.state);
    });
    $('#btn-clear-corpus')?.addEventListener('click', clearCorpus);
    $('#btn-activate-snapshot')?.addEventListener('click', activateSnapshot);
    $('#btn-delete-snapshot')?.addEventListener('click', deleteSnapshot);
    $('#jobs-indicator-cancel')?.addEventListener('click', cancelCurrentJob);
    setupCommandPalette();
    setupStatusDock();

    // Corpus — filters
    $('#f-text')?.addEventListener('input', scheduleFilterReload);
    $('#f-regex')?.addEventListener('change', () => { readFilterFromUI(); App.corpus.page = 1; reloadCorpusFilter(); });
    $('#f-case') ?.addEventListener('change', () => { readFilterFromUI(); App.corpus.page = 1; reloadCorpusFilter(); });
    $('#f-date-from')?.addEventListener('change', () => { readFilterFromUI(); App.corpus.page = 1; reloadCorpusFilter(); });
    $('#f-date-to')  ?.addEventListener('change', () => { readFilterFromUI(); App.corpus.page = 1; reloadCorpusFilter(); });
    ['#f-like-min', '#f-share-min', '#f-comment-min', '#f-view-min'].forEach(sel => {
        $(sel)?.addEventListener('input', scheduleFilterReload);
    });
    $('#btn-reset-filters')?.addEventListener('click', resetFilters);
    $('#btn-export-corpus-csv')?.addEventListener('click', exportCorpusCsv);
    // Ag-grid no-rows overlay injects this button; delegate the click.
    $('#corpus-grid')?.addEventListener('click', (ev) => {
        if (ev.target.closest('#corpus-clear-filters-empty')) {
            ev.preventDefault();
            resetFilters();
        }
    });

    // Slicer
    $('#btn-slicer-run')?.addEventListener('click', () => runSlicePreview(1));
    $('#btn-slicer-clear-query')?.addEventListener('click', () => {
        const input = $('#slicer-query-input');
        if (input) input.value = '';
        App.slicer.query = '';
        App.slicer.lastRunQuery = '';
        App.slicer.preview = [];
        $('#slicer-result-card').style.display = 'none';
        setSlicerStatus('', '');
    });
    $('#slicer-query-input')?.addEventListener('keydown', ev => {
        // Cmd/Ctrl+Enter runs the preview without a mouse.
        if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') {
            ev.preventDefault();
            runSlicePreview(1);
        }
    });
    $('#btn-slicer-save')?.addEventListener('click', saveSlice);
    $('#slice-name-input')?.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); saveSlice(); }
    });
    $('#btn-slicer-go-corpus')?.addEventListener('click', () => navigateTo('corpus'));
    $('#btn-slicer-diff')?.addEventListener('click', runSliceDiff);
    $('#slicer-diff-a')?.addEventListener('change', ev => { App.slicer.diffA = ev.target.value; App.slicer.diff = null; renderDiffResult(); });
    $('#slicer-diff-b')?.addEventListener('change', ev => { App.slicer.diffB = ev.target.value; App.slicer.diff = null; renderDiffResult(); });

    // Slicer — sample & split
    $$('.sample-tab').forEach(btn => {
        btn.addEventListener('click', () => switchSampleTab(btn.dataset.sampleMethod));
    });
    $('#sample-by-col')?.addEventListener('change', ev => { App.slicer.sample.byCol = ev.target.value; });
    $('#btn-sample-preview')?.addEventListener('click', () => runSamplePreview(1));
    $('#btn-sample-reset')?.addEventListener('click', resetSampleForm);
    $('#btn-sample-save')?.addEventListener('click', saveSampleAsSlice);
    $('#btn-split-save')?.addEventListener('click', saveSplitAsSlices);
    $('#sample-name-input')?.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); saveSampleAsSlice(); }
    });
    $('#split-prefix-input')?.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); saveSplitAsSlices(); }
    });

    // Codebook & coding (Phase 4)
    $('#btn-cb-go-settings')?.addEventListener('click', () => navigateTo('settings'));
    $('#btn-cb-preset')?.addEventListener('click', () => createCodebook(true));
    $('#btn-cb-new-empty')?.addEventListener('click', () => createCodebook(false));
    $('#btn-cb-new')?.addEventListener('click', () => createCodebook(false));
    $('#btn-cb-import')?.addEventListener('click', promptImportCodebook);
    $('#btn-cb-import-empty')?.addEventListener('click', promptImportCodebook);
    $('#cb-import-file')?.addEventListener('change', handleImportFile);
    $('#btn-cb-export')?.addEventListener('click', exportCodebook);
    $('#btn-cb-delete')?.addEventListener('click', deleteActiveCodebook);
    $('#btn-cb-add-cat')?.addEventListener('click', addCategory);
    $('#cb-add-title')?.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); addCategory(); }
    });
    $('#cb-scope-select')?.addEventListener('change', async ev => {
        App.codebook.scopeSliceId = ev.target.value || '';
        App.codebook.focusRowIdx = null;
        App.codebook.focusRowData = null;
        App.codebook.rowTags = [];
        await loadScopeRows();
        renderCodebook(App.state);
    });
    $('#cb-jump-row')?.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); jumpToRow(); }
    });
    $('#btn-cb-prev')?.addEventListener('click', () => stepRow(-1));
    $('#btn-cb-next')?.addEventListener('click', () => stepRow(1));
    $('#btn-cb-undo')?.addEventListener('click', undoCoding);
    $('#cb-bulk-source')?.addEventListener('change', ev => {
        App.codebook.bulkMode = ev.target.value;
        const cb = activeCodebook(App.state);
        if (cb) renderBulkPanel(App.state, cb);
    });
    $('#btn-cb-bulk-dry')?.addEventListener('click', () => runBulkTag(true));
    $('#btn-cb-bulk-apply')?.addEventListener('click', () => runBulkTag(false));
    $('#btn-cb-irr-run')?.addEventListener('click', runIRR);

    // AI coding (Phase 5)
    $('#btn-ai-go-codebook')?.addEventListener('click', () => navigateTo('codebook'));
    $('#btn-ai-go-settings')?.addEventListener('click', () => navigateTo('settings'));
    $('#ai-scope-select')?.addEventListener('change', () => {
        App.ai.preflight = null;
        renderAIScopeCard(App.state);
    });
    $('#ai-mode')?.addEventListener('change', () => {
        App.ai.preflight = null;
        renderAIScopeCard(App.state);
    });
    $('#ai-sample-size')?.addEventListener('input', () => {
        App.ai.preflight = null;
        renderAIScopeCard(App.state);
    });
    $('#ai-batch-size')?.addEventListener('input', () => {
        App.ai.preflight = null;
        renderAIScopeCard(App.state);
    });
    $('#btn-ai-preflight')?.addEventListener('click', runAIPreflight);
    $('#btn-ai-run')?.addEventListener('click', runAICoding);
    $('#btn-ai-stop')?.addEventListener('click', stopAIRun);
    $('#btn-ai-suggest-cb')?.addEventListener('click', runSuggestCodebook);
    $('#btn-ai-review-refresh')?.addEventListener('click', loadAIReview);
    $('#ai-review-lowconf-only')?.addEventListener('change', ev => {
        App.ai.reviewLowConfOnly = !!ev.target.checked;
        loadAIReview();
    });

    // Topics (Phase 6)
    $('#btn-tp-go-settings')?.addEventListener('click', () => navigateTo('settings'));
    $('#btn-tp-go-corpus')?.addEventListener('click', () => navigateTo('corpus'));
    $('#btn-tp-create')?.addEventListener('click', createTopicSet);
    $('#tp-new-name')?.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); createTopicSet(); }
    });
    $('#btn-tp-preflight')?.addEventListener('click', runTopicsPreflight);
    $('#btn-tp-induce')?.addEventListener('click', runTopicsInduce);
    $('#btn-tp-classify')?.addEventListener('click', runTopicsClassify);
    $('#btn-tp-stop')?.addEventListener('click', stopTopicsRun);
    $('#btn-tp-merge')?.addEventListener('click', mergeTopics);
    $('#tp-batch-size')?.addEventListener('input', () => {
        App.topics.preflight = null;
        const ts = activeTopicSet(App.state);
        if (ts) renderTopicsRunCard(App.state, ts);
    });
    $('#tp-row-limit')?.addEventListener('input', () => {
        App.topics.preflight = null;
        const ts = activeTopicSet(App.state);
        if (ts) renderTopicsRunCard(App.state, ts);
    });
    $('#tp-classify-mode')?.addEventListener('change', ev => {
        const wrap = $('#tp-row-limit-wrap');
        if (wrap) wrap.style.display = ev.target.value === 'limit' ? '' : 'none';
        App.topics.preflight = null;
        const ts = activeTopicSet(App.state);
        if (ts) renderTopicsRunCard(App.state, ts);
    });

    // Row-detail drawer
    $('#btn-close-drawer')?.addEventListener('click', closeRowModal);
    $('#row-drawer-overlay')?.addEventListener('click', closeRowModal);
    
    // View toggles
    $('#btn-view-grid')?.addEventListener('click', () => {
        const btn1 = $('#btn-view-grid');
        const btn2 = $('#btn-view-gallery');
        btn1.classList.add('active');
        btn1.style.background = 'white';
        btn1.style.boxShadow = '0 1px 2px rgba(0,0,0,0.1)';
        btn1.querySelector('i').style.color = 'var(--isd-blue)';
        
        btn2.classList.remove('active');
        btn2.style.background = 'transparent';
        btn2.style.boxShadow = 'none';
        btn2.querySelector('i').style.color = 'var(--text-muted)';
        
        $('#corpus-grid').style.display = '';
        $('#corpus-gallery').style.display = 'none';
    });
    $('#btn-view-gallery')?.addEventListener('click', () => {
        const btn1 = $('#btn-view-grid');
        const btn2 = $('#btn-view-gallery');
        btn2.classList.add('active');
        btn2.style.background = 'white';
        btn2.style.boxShadow = '0 1px 2px rgba(0,0,0,0.1)';
        btn2.querySelector('i').style.color = 'var(--isd-blue)';
        
        btn1.classList.remove('active');
        btn1.style.background = 'transparent';
        btn1.style.boxShadow = 'none';
        btn1.querySelector('i').style.color = 'var(--text-muted)';
        
        $('#corpus-grid').style.display = 'none';
        $('#corpus-gallery').style.display = 'block';
    });
    document.addEventListener('keydown', ev => {
        if (ev.key === 'Escape' && $('#row-drawer')?.classList.contains('open')) closeRowModal();
    });

    // VQB switch
    const vqbToggle = $('#vqb-toggle');
    if (vqbToggle) {
        vqbToggle.addEventListener('change', ev => {
            const root = $('#vqb-root');
            const inputArea = $('#slicer-query-input');
            if (ev.target.checked) {
                root.style.display = 'block';
                inputArea.style.display = 'none';
                renderVQB();
            } else {
                root.style.display = 'none';
                inputArea.style.display = 'block';
            }
        });
        if (vqbToggle.checked) {
            $('#vqb-root').style.display = 'block';
            $('#slicer-query-input').style.display = 'none';
            renderVQB();
        }
    }

    try {
        App.state = await fetchJson('/api/state');
        console.log('[Corpus Intel] state loaded', App.state);
        maybeShowRecoveryBanner(App.state);
        maybeShowAIHealthBanner(App.state);
    } catch (err) {
        console.error('[Corpus Intel] /api/state failed', err);
    }
    renderHome(App.state);
}

function maybeShowAIHealthBanner(state) {
    const h = state?.ai_health;
    const el = document.getElementById('ai-health-banner');
    if (!h || h.reachable) {
        if (el) el.remove();
        document.body.classList.remove('ai-offline');
        return;
    }
    document.body.classList.add('ai-offline');
    if (!document.getElementById('ai-health-banner')) {
        const div = document.createElement('div');
        div.id = 'ai-health-banner';
        div.className = 'ai-health-banner';
        div.innerHTML = `
          <i class="fa-solid fa-cloud-slash"></i>
          <div class="ai-health-msg">
            <strong>Anthropic API unreachable.</strong>
            Corpus building, manual coding, slicing, analytics and export all still work. AI features (classification, topic induction, codebook suggestions, report writing) are disabled until the connection recovers.
            <button class="ai-health-retry" id="ai-health-retry">Re-check now</button>
          </div>`;
        document.body.prepend(div);
        document.getElementById('ai-health-retry').addEventListener('click', async () => {
            try {
                const r = await fetchJson('/api/ai/health');
                if (r.reachable) {
                    document.getElementById('ai-health-banner')?.remove();
                    document.body.classList.remove('ai-offline');
                    await refreshState();
                }
            } catch {}
        });
    }
}

function maybeShowRecoveryBanner(state) {
    const from = state?.settings?._recovered_from;
    if (!from) return;
    let el = document.getElementById('recovery-banner');
    if (!el) {
        el = document.createElement('div');
        el.id = 'recovery-banner';
        el.className = 'recovery-banner';
        document.body.prepend(el);
    }
    el.innerHTML = `
      <i class="fa-solid fa-life-ring"></i>
      <div class="recovery-msg">
        <strong>Session recovered.</strong>
        Your previous <code>latest.ci</code> could not be read, so we restored from <code>${from}</code>. Check your work looks right before continuing.
      </div>
      <button class="recovery-dismiss" id="recovery-dismiss">Got it</button>`;
    document.getElementById('recovery-dismiss').addEventListener('click', async () => {
        try { await fetchJson('/api/session/ack_recovery', { method: 'POST' }); } catch {}
        el.remove();
    });
}

document.addEventListener('DOMContentLoaded', init);

window.addEventListener('resize', () => {
    Object.values(Analytics.charts || {}).forEach(chart => {
        if (chart && typeof chart.resize === 'function') {
            chart.resize();
        }
    });
});
