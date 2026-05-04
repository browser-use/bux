const SWIPE_THRESHOLD = 90;
const VERTICAL_THRESHOLD = 110;

const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor?.('#14161c');
  tg.setBackgroundColor?.('#14161c');
}

const params = new URLSearchParams(location.search);
const isDemo = params.get('demo') === '1';
const initData = tg?.initData || '';

const stack = document.getElementById('stack');
const empty = document.getElementById('empty');
const counter = document.getElementById('counter');
const toast = document.getElementById('toast');
const modal = document.getElementById('feedback-modal');
const modalText = document.getElementById('feedback-text');
const modalContext = document.getElementById('feedback-context');
const btnLeft = document.getElementById('btn-left');
const btnRight = document.getElementById('btn-right');
const btnUp = document.getElementById('btn-up');

document.getElementById('refresh').addEventListener('click', () => loadDeck());
document.getElementById('feedback-cancel').addEventListener('click', closeFeedback);
document.getElementById('feedback-submit').addEventListener('click', submitFeedback);
btnLeft.addEventListener('click', () => programmaticSwipe('left'));
btnRight.addEventListener('click', () => programmaticSwipe('right'));
btnUp.addEventListener('click', () => openFeedback());

document.addEventListener('keydown', (e) => {
  if (modal.open) return;
  if (e.key === 'ArrowLeft') programmaticSwipe('left');
  else if (e.key === 'ArrowRight') programmaticSwipe('right');
  else if (e.key === 'ArrowUp') openFeedback();
});

let deck = [];

function authHeaders() {
  if (isDemo) return {};
  return { 'Authorization': `tma ${initData}` };
}

function apiUrl(path) {
  if (!isDemo) return path;
  const u = new URL(path, location.origin);
  u.searchParams.set('demo', '1');
  return u.toString();
}

async function api(path, init = {}) {
  const headers = { 'Content-Type': 'application/json', ...authHeaders(), ...(init.headers || {}) };
  const res = await fetch(apiUrl(path), { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).error || detail; } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

async function loadDeck() {
  showToast('loading…');
  try {
    const { items, refilling } = await api('/api/suggestions');
    deck = items;
    render();
    if (refilling) showToast('topping up the deck…');
    else hideToast();
  } catch (err) {
    showToast(`error: ${err.message}`);
  }
}

function render() {
  stack.querySelectorAll('.card').forEach((el) => el.remove());
  empty.hidden = deck.length > 0;
  counter.textContent = deck.length ? `${deck.length} pending` : '';
  // Render top 3 (top is interactive, two beneath are visual stack).
  for (let i = Math.min(deck.length - 1, 2); i >= 0; i--) {
    stack.appendChild(makeCard(deck[i], i));
  }
}

function makeCard(item, depth) {
  const card = document.createElement('article');
  card.className = 'card';
  card.dataset.id = String(item.id);
  card.dataset.depth = String(depth);
  card.innerHTML = `
    <span class="badge yes">YES</span>
    <span class="badge no">NOPE</span>
    <span class="badge up">REVISE</span>
    <span class="source">${escapeHtml(item.source || '')}</span>
    <h2></h2>
    <p></p>
  `;
  card.querySelector('h2').textContent = item.title || '';
  card.querySelector('p').textContent = item.description || '';
  if (depth === 0) attachDrag(card);
  return card;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function attachDrag(card) {
  let startX = 0, startY = 0, dx = 0, dy = 0, dragging = false, pointerId = null;
  const onDown = (e) => {
    if (modal.open) return;
    pointerId = e.pointerId;
    card.setPointerCapture(pointerId);
    startX = e.clientX;
    startY = e.clientY;
    dragging = true;
    card.classList.add('dragging');
  };
  const onMove = (e) => {
    if (!dragging || e.pointerId !== pointerId) return;
    dx = e.clientX - startX;
    dy = e.clientY - startY;
    const rot = Math.max(-25, Math.min(25, dx / 12));
    card.style.transform = `translate(${dx}px, ${dy}px) rotate(${rot}deg)`;
    setBadge(card, dx, dy);
  };
  const onUp = (e) => {
    if (!dragging) return;
    dragging = false;
    card.classList.remove('dragging');
    if (pointerId !== null) try { card.releasePointerCapture(pointerId); } catch (_) {}
    if (-dy > VERTICAL_THRESHOLD && Math.abs(dy) > Math.abs(dx)) {
      card.style.transform = '';
      setBadge(card, 0, 0);
      openFeedback();
    } else if (dx > SWIPE_THRESHOLD) {
      flyAway(card, 'right');
    } else if (dx < -SWIPE_THRESHOLD) {
      flyAway(card, 'left');
    } else {
      card.style.transform = '';
      setBadge(card, 0, 0);
    }
    dx = dy = 0;
  };
  card.addEventListener('pointerdown', onDown);
  card.addEventListener('pointermove', onMove);
  card.addEventListener('pointerup', onUp);
  card.addEventListener('pointercancel', onUp);
}

function setBadge(card, dx, dy) {
  const yes = card.querySelector('.badge.yes');
  const no = card.querySelector('.badge.no');
  const up = card.querySelector('.badge.up');
  yes.style.opacity = dx > 30 ? Math.min(1, dx / SWIPE_THRESHOLD) : 0;
  no.style.opacity = dx < -30 ? Math.min(1, -dx / SWIPE_THRESHOLD) : 0;
  up.style.opacity = (-dy > 30 && Math.abs(dy) > Math.abs(dx)) ? Math.min(1, -dy / VERTICAL_THRESHOLD) : 0;
}

function programmaticSwipe(direction) {
  const card = stack.querySelector('.card[data-depth="0"]');
  if (!card) return;
  flyAway(card, direction);
}

function flyAway(card, direction) {
  card.classList.add(`flying-${direction}`);
  card.style.transform = '';
  const id = Number(card.dataset.id);
  const item = deck.find((d) => d.id === id);
  setTimeout(async () => {
    deck = deck.filter((d) => d.id !== id);
    render();
    try {
      const r = await api('/api/swipe', {
        method: 'POST',
        body: JSON.stringify({ suggestion_id: id, decision: direction }),
      });
      if (direction === 'right') {
        showToast(r.dispatched ? `dispatched: ${item?.title || ''}` : 'accepted (no chat bound)');
      } else {
        showToast(direction === 'left' ? 'dismissed' : 'noted');
      }
      if (r.refilling) setTimeout(loadDeck, 4000);
    } catch (err) {
      showToast(`error: ${err.message}`);
    }
  }, 280);
}

function openFeedback() {
  const card = stack.querySelector('.card[data-depth="0"]');
  if (!card) return;
  const id = Number(card.dataset.id);
  const item = deck.find((d) => d.id === id);
  modalContext.textContent = item?.title || '';
  modalText.value = '';
  modal.dataset.id = String(id);
  modal.showModal();
  setTimeout(() => modalText.focus(), 50);
}

function closeFeedback() {
  modal.close();
}

async function submitFeedback() {
  const id = Number(modal.dataset.id);
  const text = modalText.value.trim();
  if (!text) {
    closeFeedback();
    return;
  }
  modal.close();
  const card = stack.querySelector(`.card[data-id="${id}"]`);
  if (card) card.classList.add('flying-up');
  setTimeout(async () => {
    deck = deck.filter((d) => d.id !== id);
    render();
    try {
      await api('/api/swipe', {
        method: 'POST',
        body: JSON.stringify({ suggestion_id: id, decision: 'up', feedback_text: text }),
      });
      showToast('feedback queued — agent will revise');
    } catch (err) {
      showToast(`error: ${err.message}`);
    }
  }, 280);
}

function showToast(text) {
  toast.textContent = text;
  toast.hidden = false;
  clearTimeout(showToast._t);
  showToast._t = setTimeout(hideToast, 3500);
}

function hideToast() { toast.hidden = true; }

loadDeck();
