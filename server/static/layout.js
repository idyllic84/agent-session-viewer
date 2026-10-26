// Responsive layout: three panes side by side on wide screens; on phones/tablets
// one pane at a time (sessions → conversation → message), with browser Back
// support, a Back button and a collapsible filter panel.
const $ = id => document.getElementById(id);
const narrow = window.matchMedia('(max-width: 900px)');
const body = document.body;
const FILTER_IDS = ['machineSelect', 'sourceSelect', 'projectSelect', 'sessionSearch'];

function linkedSession() { return new URLSearchParams(location.hash.slice(1)).get('session'); }

function setView(view) {
    body.dataset.view = view;
    $('backBtn').hidden = view === 'list';
    $('appSubtitle').textContent = view === 'list' ? 'Cloud' : $('conversationTitle').textContent;
    if (view !== 'list') window.scrollTo(0, 0);
}

function go(view) {
    if (!narrow.matches || body.dataset.view === view) { setView(view); return; }
    history.pushState({ view }, '', location.href);
    setView(view);
}

// Opening a session or a message moves forward one pane on narrow screens.
$('sessionList').addEventListener('click', event => {
    if (event.target.closest('.session-item')) { closeFilters(); go('conversation'); }
});
$('conversationHistory').addEventListener('click', event => {
    if (event.target.closest('.message-item') && narrow.matches) go('details');
});
$('backBtn').addEventListener('click', () => {
    if (history.state?.view) history.back();
    else setView(body.dataset.view === 'details' ? 'conversation' : 'list');
});
window.addEventListener('popstate', event => {
    setView(event.state?.view || (linkedSession() && narrow.matches ? 'conversation' : 'list'));
});
new MutationObserver(() => { if (body.dataset.view !== 'list') $('appSubtitle').textContent = $('conversationTitle').textContent; })
    .observe($('conversationTitle'), { childList: true, characterData: true, subtree: true });
narrow.addEventListener('change', () => { if (!narrow.matches) { setView(body.dataset.view); closeFilters(); } });

// Collapsible filters with an active-filter count.
function closeFilters() { body.classList.remove('filters-open'); $('filtersBtn').setAttribute('aria-expanded', 'false'); }
$('filtersBtn').addEventListener('click', () => {
    const open = body.classList.toggle('filters-open');
    $('filtersBtn').setAttribute('aria-expanded', String(open));
    if (open) $('sessionSearch').focus({ preventScroll: true });
});
function updateFilterCount() {
    const active = FILTER_IDS.filter(id => $(id).value.trim() !== '').length;
    $('filterCount').textContent = String(active); $('filterCount').hidden = active === 0;
}
for (const id of FILTER_IDS) { $(id).addEventListener('change', updateFilterCount); $(id).addEventListener('input', updateFilterCount); }
for (const id of ['clearSessionSearchBtn', 'resetSearchScopeBtn']) $(id).addEventListener('click', () => setTimeout(updateFilterCount));
$('sessionSearch').addEventListener('keydown', event => { if (event.key === 'Enter' && narrow.matches) { closeFilters(); event.target.blur(); } });

// A shared deep link (#session=…) opens directly on the conversation pane.
setView(linkedSession() && narrow.matches ? 'conversation' : 'list');
updateFilterCount();
