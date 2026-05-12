const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const params = new URLSearchParams(window.location.search);
if (params.get("dev") === "1") localStorage.buxMiniAppDev = "1";
const initData = tg?.initData || (localStorage.buxMiniAppDev === "1" ? "dev" : "");
const goalKey = "buxTinderGoalId";
const indexKey = "buxTinderIndex";

const state = {
  cards: [],
  goals: [],
  topics: [],
  stats: {},
  activeGoalId: localStorage.getItem(goalKey) || "all",
  railCollapsed: localStorage.getItem("buxTinderRailCollapsed") === "1",
  index: Number(localStorage.getItem(indexKey) || "0"),
  started: Number(localStorage.getItem("buxTinderStarted") || "0"),
  skipped: Number(localStorage.getItem("buxTinderSkipped") || "0"),
};

const els = {
  rail: document.querySelector("#goalRail"),
  tabs: document.querySelector("#goalTabs"),
  deck: document.querySelector("#deck"),
  meta: document.querySelector("#deckMeta"),
  toast: document.querySelector("#toast"),
  context: document.querySelector("#contextButton"),
  more: document.querySelector("#moreButton"),
  newGoal: document.querySelector("#newGoalButton"),
  collapseRail: document.querySelector("#collapseRailButton"),
  sheet: document.querySelector("#contextSheet"),
  form: document.querySelector("#contextForm"),
  input: document.querySelector("#contextInput"),
  voice: document.querySelector("#voiceButton"),
  goalSheet: document.querySelector("#goalSheet"),
  goalForm: document.querySelector("#goalForm"),
  goalInput: document.querySelector("#goalInput"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": initData,
      ...(options.headers || {}),
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function visibleCards() {
  if (state.activeGoalId.startsWith("topic:")) {
    const topicId = state.activeGoalId.slice("topic:".length);
    return state.cards.filter((card) => String(card.topic_id || "0") === topicId);
  }
  if (state.activeGoalId === "all") return state.cards;
  return state.cards.filter((card) => String(card.goal_id || "") === String(state.activeGoalId));
}

function currentCard() {
  const cards = visibleCards();
  if (state.index >= cards.length) state.index = Math.max(0, cards.length - 1);
  return cards[state.index] || null;
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => els.toast.classList.remove("show"), 1800);
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

function render() {
  renderGoals();
  const cards = visibleCards();
  const card = currentCard();
  els.meta.textContent = `${openCount()} open · ${state.started} do · ${state.skipped} skip`;
  localStorage.setItem(indexKey, String(state.index));
  if (!card) {
    els.deck.innerHTML = `
      <article class="empty">
        <strong>No cards here</strong>
        <p>Give context or generate more cards for this goal.</p>
      </article>
    `;
    return;
  }
  els.deck.innerHTML = cardHtml(card);
  bindCard(card);
}

function renderGoals() {
  document.body.classList.toggle("rail-collapsed", state.railCollapsed);
  els.collapseRail.innerHTML = railSvg(state.railCollapsed);
  els.collapseRail.setAttribute("aria-label", state.railCollapsed ? "Expand goals" : "Collapse goals");
  const tabs = [
    { id: "all", title: "All", count: Number(state.stats.open || state.cards.length) },
    ...state.goals.map((goal) => {
      const threadId = Number(goal.tg_thread_id || 0);
      const id = threadId ? `topic:${threadId}` : String(goal.id);
      return { id, title: goal.title, count: countFor(id) };
    }),
    ...state.topics.map((topic) => ({ id: `topic:${topic.thread_id || topic.id}`, title: topic.title || `Topic ${topic.thread_id || topic.id}`, count: Number(topic.count || 0) })),
  ];
  const seen = new Set();
  els.tabs.innerHTML = tabs
    .filter((tab) => {
      if (seen.has(tab.id)) return false;
      seen.add(tab.id);
      return true;
    })
    .map((tab) => `<button class="${tab.id === state.activeGoalId ? "active" : ""}" data-goal="${escapeAttr(tab.id)}"><span>${escapeHtml(tab.title)}</span><small>${tab.count}</small></button>`)
    .join("");
  els.tabs.querySelectorAll("[data-goal]").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeGoalId = button.dataset.goal || "all";
      state.index = 0;
      localStorage.setItem(goalKey, state.activeGoalId);
      render();
    });
  });
}

function openCount() {
  if (state.activeGoalId === "all") return Number(state.stats.open || state.cards.length);
  return visibleCards().length;
}

function countFor(id) {
  if (id.startsWith("topic:")) {
    const topicId = id.slice("topic:".length);
    return state.cards.filter((card) => String(card.topic_id || "0") === topicId).length;
  }
  return state.cards.filter((card) => String(card.goal_id || "") === String(id)).length;
}

function cardHtml(card) {
  const meta = sourceMeta(card);
  const buttons = cardActionButtons(card);
  const hasAction = buttons.length > 0;
  const text = renderRichText(plainText(card));
  return `
    <article class="card" data-card-id="${card.id}">
      <header class="card-head">
        ${appIconHtml(meta)}
        <div class="headline">
          <strong>${escapeHtml(meta.name)}</strong>
          <span>${escapeHtml(relativeAge(card.created_at))}${sourceInline(card)}</span>
        </div>
      </header>
      <section class="card-body">
        <p class="post-text">${text}</p>
        ${blocksHtml(card)}
        ${detailHtml(card)}
        ${mediaHtml(card)}
      </section>
      ${actionsHtml(buttons, hasAction)}
    </article>
  `;
}

function actionsHtml(buttons, hasAction) {
  return `
    <footer class="actions ${hasAction ? "" : "info-only"}">
      <div class="utility-actions">
        <button class="round no" data-delete type="button" aria-label="Skip">${xSvg()}</button>
        <button class="round comment" data-comment type="button" aria-label="Comment">${commentSvg()}</button>
      </div>
      <div class="choices">
        ${buttons.map((button) => `<button class="choice" data-start data-button="${escapeAttr(button.raw)}">${escapeHtml(button.text)}</button>`).join("")}
      </div>
    </footer>
  `;
}

function bindCard(card) {
  const item = els.deck.querySelector(".card");
  els.deck.querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", () => dismissCard(card.id, item)));
  els.deck.querySelectorAll("[data-comment]").forEach((button) => button.addEventListener("click", openContext));
  els.deck.querySelectorAll("[data-start]").forEach((button) => button.addEventListener("click", () => startCard(card.id, button.dataset.button || "", item)));
  let startX = 0;
  let startY = 0;
  item.addEventListener("touchstart", (event) => {
    startX = event.touches[0].clientX;
    startY = event.touches[0].clientY;
  }, { passive: true });
  item.addEventListener("touchend", (event) => {
    const touch = event.changedTouches[0];
    const dx = touch.clientX - startX;
    const dy = touch.clientY - startY;
    if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy)) {
      if (dx < 0) {
        dismissCard(card.id, item);
      } else {
        const buttons = cardActionButtons(card);
        buttons.length === 1 ? startCard(card.id, buttons[0].raw, item) : toast("Choose an option.");
      }
    } else if (Math.abs(dy) > 75) {
      openContext();
    }
  }, { passive: true });
  item.addEventListener("wheel", (event) => {
    if (Math.abs(event.deltaY) > 40) openContext();
  }, { passive: true });
}

function plainText(card) {
  const title = String(card.title || "").trim();
  const why = String(card.why || "").trim();
  return why && why !== title ? `${title}\n${why}` : title || why || "Ready when you are.";
}

function renderRichText(value) {
  return escapeHtml(value)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*{1,2}/g, "")
    .replace(/(https?:\/\/[^\s<]+)/g, (url) => `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(shortUrl(url))}</a>`)
    .replace(/\n+/g, "<br />");
}

function shortUrl(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "link";
  }
}

function mediaHtml(card) {
  const visual = card.visual || {};
  if (visual.kind === "image" && visual.src) return `<div class="media"><img src="${escapeAttr(visual.src)}" alt="" /></div>`;
  if (visual.kind === "video" && visual.src) return `<div class="media"><video src="${escapeAttr(visual.src)}" controls playsinline preload="metadata"></video></div>`;
  return "";
}

function detailHtml(card) {
  const action = String(card.action || "").trim();
  if (!action || action === card.title || action === card.why) return "";
  return `<details><summary>Details</summary><div>${renderRichText(action)}</div></details>`;
}

function blocksHtml(card) {
  const blocks = Array.isArray(card.blocks) ? card.blocks : [];
  return blocks.map((block) => {
    const label = `${block.emoji ? `${block.emoji} ` : ""}${block.title || "Details"}`;
    return `<details><summary>${escapeHtml(label)}</summary><div>${renderRichText(block.body || "")}</div></details>`;
  }).join("");
}

function sourceInline(card) {
  const url = card.source_url || firstUrl([card.title, card.why].join(" "));
  if (!url) return "";
  return ` · <a class="source-link" href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(card.source_label || "Source")}</a>`;
}

function firstUrl(value) {
  return String(value || "").match(/https?:\/\/[^\s)"']+/)?.[0] || "";
}

function cardActionButtons(card) {
  const prompt = String(card.action || "").trim();
  const labels = Array.isArray(card.buttons) ? card.buttons : [];
  const buttons = labels
    .map((raw) => ({ raw: String(raw || "").trim(), text: buttonText(raw) }))
    .filter((button) => button.raw && button.text);
  if (!prompt && !buttons.length) return [];
  if (!buttons.length) return [{ raw: "Do it", text: "Do it" }];
  return buttons;
}

function buttonText(label) {
  const raw = String(label || "").trim();
  if (/(skip|dismiss|delete|no\b|pass|edit|refine|change|context)/i.test(raw)) return "";
  if (/^(yes|yes new thread|do it|start)$/i.test(raw.replace(/[^a-z ]/gi, " ").trim())) return "Do it";
  return raw.replace(/^[^\p{L}\p{N}]+/u, "").replace(/\s+/g, " ").trim().slice(0, 32);
}

function sourceMeta(card) {
  const host = sourceHost(card.source_url);
  const brands = [
    ["producthunt.com", "Product Hunt", "producthunt.com"],
    ["mail.google.com", "Gmail", "mail.google.com"],
    ["gmail", "Gmail", "mail.google.com"],
    ["slack.com", "Slack", "slack.com"],
    ["slack", "Slack", "slack.com"],
    ["reddit.com", "Reddit", "reddit.com"],
    ["reddit", "Reddit", "reddit.com"],
    ["github.com", "GitHub", "github.com"],
    ["github", "GitHub", "github.com"],
    ["whatsapp", "WhatsApp", "whatsapp.com"],
    ["telegram", "Telegram", "telegram.org"],
    ["x.com", "X", "x.com"],
    ["twitter.com", "X", "x.com"],
    ["tweet", "X", "x.com"],
  ];
  const sourceText = [host, card.source_label, card.source, card.title].join(" ").toLowerCase();
  const found = brands.find(([needle]) => sourceText.includes(needle));
  if (found) return { name: found[1], domain: found[2], mark: found[1][0] };
  const name = String(card.source || "Agency").split("-").filter(Boolean).slice(0, 2).join(" ") || "Agency";
  return { name: titleCase(name), mark: initials(name) };
}

function sourceHost(url) {
  try {
    return new URL(url || "").hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function appIconHtml(meta) {
  if (meta.domain) {
    const src = `https://www.google.com/s2/favicons?domain_url=${encodeURIComponent(`https://${meta.domain}`)}&sz=64`;
    return `<div class="avatar"><img src="${escapeAttr(src)}" alt="" onerror="this.parentElement.classList.add('avatar-empty');this.remove()" /></div>`;
  }
  return `<div class="avatar avatar-empty" aria-hidden="true"></div>`;
}

function titleCase(value) {
  return String(value || "Agency").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function initials(value) {
  return titleCase(value).split(/\s+/).slice(0, 2).map((word) => word[0] || "").join("");
}

function relativeAge(createdAt) {
  const seconds = Number(createdAt);
  if (!Number.isFinite(seconds) || seconds <= 0) return "new";
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - seconds));
  if (diff < 60) return "now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d`;
  return new Date(seconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

async function startCard(id, button, item) {
  item?.classList.add("accept-right");
  try {
    await api(`/api/cards/${id}/start`, { method: "POST", body: JSON.stringify({ button }) });
    state.started += 1;
    decrementOpenCount();
    localStorage.setItem("buxTinderStarted", String(state.started));
    removeLocal(id);
    toast("Started.");
  } catch (error) {
    item?.classList.remove("accept-right");
    toast(error.message);
  }
}

async function dismissCard(id, item) {
  item?.classList.add("dismiss-left");
  try {
    await api(`/api/cards/${id}/dismiss`, { method: "POST", body: "{}" });
    state.skipped += 1;
    decrementOpenCount();
    localStorage.setItem("buxTinderSkipped", String(state.skipped));
    removeLocal(id);
    toast("Skipped.");
  } catch (error) {
    item?.classList.remove("dismiss-left");
    toast(error.message);
  }
}

function decrementOpenCount() {
  const open = Number(state.stats.open || 0);
  if (open > 0) state.stats.open = open - 1;
}

function removeLocal(id) {
  setTimeout(() => {
    state.cards = state.cards.filter((card) => String(card.id) !== String(id));
    render();
  }, 180);
}

function openContext() {
  els.sheet.showModal();
  els.input.focus({ preventScroll: true });
}

async function sendContext(event) {
  event.preventDefault();
  const comment = els.input.value.trim();
  if (!comment) return;
  const card = currentCard();
  try {
    if (card?.id) {
      await api(`/api/cards/${card.id}/comment`, { method: "POST", body: JSON.stringify({ comment }) });
    } else if (state.activeGoalId.startsWith("topic:")) {
      await api(`/api/topics/${state.activeGoalId.slice("topic:".length)}/context`, { method: "POST", body: JSON.stringify({ comment }) });
    } else if (state.activeGoalId !== "all") {
      await api(`/api/goals/${state.activeGoalId}/context`, { method: "POST", body: JSON.stringify({ comment }) });
    }
    els.input.value = "";
    els.sheet.close();
    if (card?.id) removeLocal(card.id);
    toast("Comment sent.");
  } catch (error) {
    toast(error.message);
  }
}

async function createGoal(event) {
  event.preventDefault();
  const context = els.goalInput.value.trim();
  if (!context) return;
  const title = context.split(/\n+/)[0].slice(0, 72);
  try {
    const result = await api("/api/goals", {
      method: "POST",
      body: JSON.stringify({ title, context }),
    });
    els.goalInput.value = "";
    els.goalSheet.close();
    if (result.active_id) {
      state.activeGoalId = result.active_id;
      localStorage.setItem(goalKey, state.activeGoalId);
    }
    await refresh({ resetToTop: true });
    toast("Goal created.");
  } catch (error) {
    toast(error.message);
  }
}

async function generateMore() {
  try {
    if (state.activeGoalId.startsWith("topic:")) {
      await api(`/api/topics/${state.activeGoalId.slice("topic:".length)}/generate`, { method: "POST", body: "{}" });
    } else if (state.activeGoalId !== "all") {
      await api(`/api/goals/${state.activeGoalId}/generate`, { method: "POST", body: "{}" });
    } else {
      await api("/api/generate", { method: "POST", body: "{}" });
    }
    toast("Asked for more cards.");
    scheduleRefresh();
  } catch (error) {
    toast(error.message);
  }
}

function scheduleRefresh() {
  [1600, 6500].forEach((delay) => {
    setTimeout(() => refresh({ resetToTop: true }).catch((error) => toast(error.message)), delay);
  });
}

function attachSpeech() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;
  const recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.onresult = (event) => {
    els.input.value = [...event.results].map((result) => result[0].transcript).join(" ");
  };
  els.voice.addEventListener("click", () => recognition.start());
}

function xSvg() {
  return `<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M7 7l10 10M17 7 7 17" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>`;
}

function commentSvg() {
  return `<svg viewBox="0 0 24 24" width="21" height="21" aria-hidden="true"><path d="M5.5 18.5v-10A3.5 3.5 0 0 1 9 5h6a3.5 3.5 0 0 1 3.5 3.5v3A3.5 3.5 0 0 1 15 15H10l-4.5 3.5Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>`;
}

function railSvg(collapsed) {
  const path = collapsed ? "m9 6 6 6-6 6" : "m15 6-6 6 6 6";
  return `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="${path}" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

els.context?.addEventListener("click", openContext);
els.more.addEventListener("click", generateMore);
els.newGoal.addEventListener("click", () => {
  els.goalSheet.showModal();
  els.goalInput.focus({ preventScroll: true });
});
els.collapseRail.addEventListener("click", () => {
  state.railCollapsed = !state.railCollapsed;
  localStorage.setItem("buxTinderRailCollapsed", state.railCollapsed ? "1" : "0");
  renderGoals();
});
els.form.addEventListener("submit", sendContext);
els.goalForm.addEventListener("submit", createGoal);
document.querySelector("[data-close-context]").addEventListener("click", () => els.sheet.close());
document.querySelector("[data-close-goal]").addEventListener("click", () => els.goalSheet.close());
els.sheet.addEventListener("click", (event) => {
  if (event.target === els.sheet) els.sheet.close();
});
els.goalSheet.addEventListener("click", (event) => {
  if (event.target === els.goalSheet) els.goalSheet.close();
});
attachSpeech();

async function refresh(options = {}) {
  const [goals, topics, cards, stats] = await Promise.all([
    api("/api/goals"),
    api("/api/topics"),
    api("/api/cards"),
    api("/api/stats"),
  ]);
  state.goals = goals.goals || [];
  state.topics = topics.topics || [];
  state.cards = cards.cards || [];
  state.stats = stats.stats || {};
  if (options.resetToTop) state.index = 0;
  render();
}

try {
  await refresh();
} catch (error) {
  els.deck.innerHTML = `<article class="empty"><strong>Login failed</strong><p>${escapeHtml(error.message)}</p></article>`;
}
