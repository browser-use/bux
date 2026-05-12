const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const params = new URLSearchParams(window.location.search);
if (params.get("dev") === "1") localStorage.buxMiniAppDev = "1";
const initData = tg?.initData || (localStorage.buxMiniAppDev === "1" ? "dev" : "");
const cursorKey = "buxMiniAppCursorCardId";
const goalKey = "buxMiniAppActiveGoalId";

const state = {
  cards: [],
  goals: [],
  stats: {},
  currentIndex: 0,
  activeGoalId: localStorage.getItem(goalKey) || "all",
  passedByScroll: new Set(),
  comments: {},
  openContextCardId: null,
  suppressScrollPassUntil: 0,
};

const els = {
  feed: document.querySelector("#feed"),
  goalTabs: document.querySelector("#goalTabs"),
  activeGoal: document.querySelector("#activeGoal"),
  drawer: document.querySelector("#drawer"),
  drawerButton: document.querySelector("#drawerButton"),
  goalList: document.querySelector("#goalList"),
  newGoalButton: document.querySelector("#newGoalButton"),
  goalDialog: document.querySelector("#goalDialog"),
  goalForm: document.querySelector("#goalForm"),
  goalInput: document.querySelector("#goalInput"),
  goalRecordButton: document.querySelector("#goalRecordButton"),
  statsButton: document.querySelector("#statsButton"),
  statsDialog: document.querySelector("#statsDialog"),
  statsGrid: document.querySelector("#statsGrid"),
  contextDialog: document.querySelector("#contextDialog"),
  contextForm: document.querySelector("#contextForm"),
  contextInput: document.querySelector("#contextInput"),
  recordButton: document.querySelector("#recordButton"),
  passButton: document.querySelector("#passButton"),
  voiceButton: document.querySelector("#voiceButton"),
  startButton: document.querySelector("#startButton"),
  topButton: document.querySelector("#topButton"),
  toast: document.querySelector("#toast"),
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

function goal() {
  if (state.activeGoalId === "all") return null;
  return state.goals.find((item) => String(item.id) === String(state.activeGoalId)) || null;
}

function currentCard() {
  return visibleCards()[state.currentIndex];
}

function visibleCards() {
  if (state.activeGoalId === "all") return state.cards;
  return state.cards.filter((card) => String(card.goal_id || "") === String(state.activeGoalId));
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => els.toast.classList.remove("show"), 2200);
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
  renderStats();
  const active = goal();
  els.activeGoal.textContent = active?.title || "All cards";
  els.feed.innerHTML = "";
  const cards = visibleCards();
  if (!cards.length) {
    const title = active ? "Working on the first card." : "Make the next move obvious.";
    const copy = active
      ? "This goal is empty for now. Add context, then Agency can turn it into cards."
      : "Create a goal or wait for Agency to find the next useful thing.";
    els.feed.innerHTML = `
      <article class="empty">
        <div>
          <h1>${escapeHtml(title)}</h1>
          <p>${escapeHtml(copy)}</p>
          <button class="create-goal" type="button" data-open-goal>${active ? "Add context" : "Create goal"}</button>
        </div>
      </article>
    `;
    els.feed.querySelector("[data-open-goal]").addEventListener("click", openGoal);
    syncDock();
    return;
  }
  cards.forEach((card, index) => els.feed.append(renderCard(card, index)));
  requestAnimationFrame(() => {
    restoreCursor();
    syncDock();
  });
}

function renderCard(card, index) {
  const meta = sourceMeta(card);
  const action = usefulAction(card);
  const actionButtons = cardActionButtons(card);
  const subtitle = meta.brand ? relativeAge(card.created_at) : `@${meta.handle} · ${relativeAge(card.created_at)}`;
  const postText = renderPostText(card);
  const article = document.createElement("article");
  article.className = "story";
  article.dataset.index = String(index);
  article.dataset.cardId = String(card.id);
  article.innerHTML = `
    <section class="post-row">
      <div class="avatar-column">
        ${appIconHtml(meta)}
      </div>
      <div class="post-main">
        <header class="tweet-head">
          <div>
            <strong>${escapeHtml(meta.name)}</strong>
            <span>${escapeHtml(subtitle)}</span>
          </div>
        </header>
        <div class="post-body">
          <div class="post-text">${postText}</div>
        </div>
        ${action ? detailHtml("Show draft", action) : ""}
        ${mediaHtml(card)}
        ${sourceFooter(card, meta)}
        ${commentPanelHtml(card, meta)}
        <div class="post-actions ${actionButtons.length ? "" : "no-primary"}">
          <div class="secondary-actions">
            <button class="icon-action danger icon-only" data-delete="${card.id}" type="button" aria-label="Delete">${trashSvg()}</button>
            <button class="icon-action" data-context="${card.id}" type="button" aria-label="Refine">${replySvg()}<span>Refine</span></button>
          </div>
          <div class="primary-actions">
            ${actionButtons.map((label) => `<button class="start-inline" data-start="${card.id}" data-button="${escapeAttr(label.raw)}" type="button">${escapeHtml(label.text)}</button>`).join("")}
          </div>
        </div>
      </div>
    </section>
  `;
  article.querySelector("[data-context]").addEventListener("click", () => openInlineContext(card.id));
  article.querySelector("[data-delete]").addEventListener("click", () => passCard(card.id, "left"));
  article.querySelectorAll("[data-start]").forEach((button) => {
    button.addEventListener("click", () => startCard(card.id, button.dataset.button || ""));
  });
  attachSwipe(article, card.id);
  article.querySelector("[data-inline-context-form]")?.addEventListener("submit", submitInlineContext);
  return article;
}

function detailHtml(label, text) {
  return `
    <details class="tweet-detail">
      <summary>${escapeHtml(label)}</summary>
      <div>${renderRichText(text)}</div>
    </details>
  `;
}

function commentPanelHtml(card, meta) {
  if (String(state.openContextCardId) !== String(card.id)) return "";
  const comments = state.comments[card.id] || [];
  const rows = comments.length
    ? comments.map((comment) => `
      <div class="comment-row">
        ${appIconHtml(meta, "small")}
        <div>
          <strong>Magnus</strong>
          <span>${escapeHtml(relativeAge(comment.created_at))}</span>
          <p>${renderRichText(comment.body)}</p>
        </div>
      </div>
    `).join("")
    : `<p class="comment-empty">Add context. The agent will use it to revise or continue this card.</p>`;
  return `
    <section class="comment-panel">
      ${rows}
      <form data-inline-context-form data-card-id="${card.id}" class="comment-form">
        <textarea rows="2" placeholder="Add context for the agent"></textarea>
        <button type="submit">Reply</button>
      </form>
    </section>
  `;
}

function plainPostText(card) {
  const title = String(card.title || "").trim();
  const why = String(card.why || "").trim();
  return why && why !== title ? `${title}\n${why}` : title || why || "Ready when you are.";
}

function renderPostText(card) {
  return renderRichText(plainPostText(card));
}

function renderRichText(value) {
  const links = [];
  let html = escapeHtml(value);
  html = html.replace(
    /\[([^\]]{1,100})\]\((https?:\/\/[^)\s]+)\)/g,
    (_match, label, url) => {
      const token = `__BUX_LINK_${links.length}__`;
      links.push(`<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
      return token;
    },
  );
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*{1,2}/g, "");
  html = html.replace(/\n+/g, "<br />");
  html = html.replace(
    /(https?:\/\/[^\s<]+)/g,
    (url) => `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(shortUrl(url))}</a>`,
  );
  links.forEach((link, index) => {
    html = html.replace(`__BUX_LINK_${index}__`, link);
  });
  return html;
}

function shortUrl(url) {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/$/, "");
    return `${parsed.hostname}${path ? path.slice(0, 28) : ""}`;
  } catch {
    return url;
  }
}

function usefulAction(card) {
  const action = String(card.action || "").trim();
  if (!action || action === card.title || action === card.why) return "";
  if (looksOperational(action)) return "";
  return action;
}

function cardActionButtons(card) {
  const prompt = String(card.action || "").trim();
  const labels = Array.isArray(card.buttons) ? card.buttons : [];
  const actionable = labels
    .map((raw) => ({ raw: String(raw || "").trim(), text: buttonText(raw) }))
    .filter((item) => item.raw && item.text);
  if (!prompt && !actionable.length) return [];
  if (!prompt) return actionable.filter((item) => !isDefaultActionButton(item.raw));
  if (!actionable.length) return [{ raw: "Do it", text: "Do it" }];
  return actionable;
}

function buttonText(label) {
  const raw = String(label || "").trim();
  const normalized = raw.toLowerCase();
  if (/(skip|dismiss|delete|no\b|pass)/i.test(raw)) return "";
  if (/(edit|refine|change|context)/i.test(raw)) return "";
  if (isDefaultActionButton(raw)) return "Do it";
  return raw.replace(/^[^\p{L}\p{N}]+/u, "").trim().slice(0, 28);
}

function isDefaultActionButton(label) {
  const normalized = String(label || "").toLowerCase().replace(/[^a-z]+/g, " ").trim();
  return ["yes", "yes new thread", "do it", "start"].includes(normalized);
}

function looksOperational(text) {
  const value = text.toLowerCase();
  return (
    value.includes("execute action_if_yes") ||
    value.includes("find pending entry") ||
    value.includes("read, find") ||
    value.includes("capture #") ||
    value.includes("agency") && value.includes("pending")
  );
}

function sourceFooter(card, meta) {
  const label = card.source_label || sourceLabel(card.source) || meta.name;
  const url = card.source_url || firstUrl([card.title, card.why, card.action].join(" "));
  const source = url
    ? `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`
    : `<span>${escapeHtml(label)}</span>`;
  return `<footer class="source-footer">${source}</footer>`;
}

function firstUrl(value) {
  const match = String(value || "").match(/https?:\/\/[^\s)]+/);
  return match ? match[0] : "";
}

function sourceMeta(cardOrSource) {
  const source = typeof cardOrSource === "string" ? cardOrSource : cardOrSource?.source;
  const combined = typeof cardOrSource === "string"
    ? cardOrSource
    : [cardOrSource?.source, cardOrSource?.title, cardOrSource?.why, cardOrSource?.action].join(" ");
  const value = String(combined || "").toLowerCase();
  if (/(twitter|tweet|x\.com|octolens|\bx\b)/.test(value)) return brandMeta("x", "X", "x");
  if (value.includes("gmail") || value.includes("email")) return brandMeta("gmail", "Gmail", "gmail");
  if (value.includes("slack")) return brandMeta("slack", "Slack", "slack");
  if (value.includes("reddit")) return brandMeta("reddit", "Reddit", "reddit");
  if (value.includes("github") || value.includes("pr")) return brandMeta("github", "GitHub", "github");
  if (value.includes("linear")) return brandMeta("linear", "Linear", "linear");
  if (value.includes("calendar")) return brandMeta("calendar", "Calendar", "googlecalendar");
  if (value.includes("telegram")) return brandMeta("telegram", "Telegram", "telegram");
  const label = sourceLabel(source);
  return { kind: "agency", mark: initials(source) || "B", name: titleCase(label), handle: slug(label), brand: false };
}

function brandMeta(kind, name, icon) {
  return {
    kind,
    name,
    handle: slug(name),
    icon,
    favicon: faviconDomain(kind),
    brand: true,
  };
}

function faviconDomain(kind) {
  const domains = {
    gmail: "mail.google.com",
    slack: "slack.com",
    reddit: "reddit.com",
    x: "x.com",
    github: "github.com",
    linear: "linear.app",
    calendar: "calendar.google.com",
    telegram: "telegram.org",
  };
  return domains[kind] || "";
}

function appIconHtml(meta, size = "") {
  const sizeClass = size === "small" ? " small" : "";
  if (meta.favicon) {
    const src = `https://www.google.com/s2/favicons?domain_url=${encodeURIComponent(`https://${meta.favicon}`)}&sz=64`;
    return `<div class="app-icon logo ${escapeHtml(meta.kind)}${sizeClass}" aria-label="${escapeAttr(meta.name)}"><img src="${escapeAttr(src)}" alt="" onerror="this.remove()" />${brandSvg(meta.icon)}</div>`;
  }
  if (meta.icon) {
    return `<div class="app-icon logo ${escapeHtml(meta.kind)}${sizeClass}" aria-label="${escapeAttr(meta.name)}">${brandSvg(meta.icon)}</div>`;
  }
  return `<div class="app-icon ${escapeHtml(meta.kind)}${sizeClass}">${escapeHtml(meta.mark)}</div>`;
}

function mediaHtml(card) {
  const visual = card.visual || {};
  if (visual.kind === "image" && visual.src) {
    return `<div class="media-card"><img class="card-image" src="${escapeAttr(visual.src)}" alt="" /></div>`;
  }
  return "";
}

function replySvg() {
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 7 5 12l5 5M5 12h9a5 5 0 0 1 5 5v1" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function trashSvg() {
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 14h10l1-14M9 7V4h6v3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function brandSvg(icon) {
  const icons = {
    gmail: `<svg viewBox="0 0 48 48" aria-hidden="true"><path fill="#fff" d="M6 12h36v24H6z"/><path fill="#EA4335" d="M6 12l18 14L42 12v6L24 32 6 18z"/><path fill="#4285F4" d="M38 36h4V12l-4 6z"/><path fill="#34A853" d="M6 36h4V18l-4-6z"/><path fill="#FBBC04" d="M10 36h28V20L24 31 10 20z"/></svg>`,
    slack: `<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="22" fill="#fff"/><rect x="21" y="7" width="6" height="15" rx="3" fill="#36C5F0"/><rect x="26" y="21" width="15" height="6" rx="3" fill="#2EB67D"/><rect x="21" y="26" width="6" height="15" rx="3" fill="#ECB22E"/><rect x="7" y="21" width="15" height="6" rx="3" fill="#E01E5A"/><circle cx="16" cy="16" r="4" fill="#36C5F0"/><circle cx="32" cy="16" r="4" fill="#2EB67D"/><circle cx="32" cy="32" r="4" fill="#ECB22E"/><circle cx="16" cy="32" r="4" fill="#E01E5A"/></svg>`,
    reddit: `<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="20" fill="#FF4500"/><text x="24" y="32" text-anchor="middle" font-size="24" font-family="Arial, sans-serif" font-weight="700" fill="#fff">r</text></svg>`,
    x: `<svg viewBox="0 0 48 48" aria-hidden="true"><rect width="48" height="48" rx="24" fill="#fff"/><path d="M14 12h7.2l5.1 7.1 6.2-7.1h3.2l-8 9.2L37 36h-7.2l-5.8-8.1-7 8.1h-3.2l8.7-10L14 12Zm5.5 2.4 11.6 19.2h2.4L21.9 14.4h-2.4Z" fill="#000"/></svg>`,
    github: `<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="21" fill="#181717"/><text x="24" y="30" text-anchor="middle" font-size="13" font-family="Arial, sans-serif" font-weight="700" fill="#fff">GH</text></svg>`,
    linear: `<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="21" fill="#5E6AD2"/><path d="M14 29 29 14M15 36 36 15M24 38l14-14" stroke="#fff" stroke-width="4" stroke-linecap="round"/></svg>`,
    googlecalendar: `<svg viewBox="0 0 48 48" aria-hidden="true"><rect x="8" y="10" width="32" height="30" rx="4" fill="#fff"/><path d="M8 18h32" stroke="#4285F4" stroke-width="5"/><text x="24" y="34" text-anchor="middle" font-size="15" font-family="Arial, sans-serif" font-weight="700" fill="#1f2937">31</text></svg>`,
    telegram: `<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="21" fill="#26A5E4"/><path d="M35 14 29.8 35c-.4 1.5-1.4 1.9-2.8 1.2l-7.7-5.7-3.7 3.6c-.4.4-.8.8-1.6.8l.6-7.9L29 17.8c.7-.6-.1-.9-1-.4L12.5 27.2 5 24.8c-1.6-.5-1.6-1.6.3-2.3L33.4 11.7c1.3-.5 2.4.3 1.6 2.3Z" fill="#fff"/></svg>`,
  };
  return icons[icon] || "";
}

function sourceLabel(source) {
  const text = String(source || "Agency").split("-").filter(Boolean).slice(0, 2).join(" ");
  return text || "Agency";
}

function initials(source) {
  return sourceLabel(source)
    .split(/\s+/)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() || "")
    .join("");
}

function slug(value) {
  return String(value || "agency").toLowerCase().replace(/[^a-z0-9]+/g, "").slice(0, 18) || "agency";
}

function titleCase(value) {
  return String(value || "Agency").replace(/\b\w/g, (letter) => letter.toUpperCase());
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

function renderGoals() {
  renderGoalTabs();
  els.goalList.innerHTML = "";
  if (!state.goals.length) {
    els.goalList.innerHTML = `<div class="goal-item"><strong>No goals yet</strong></div>`;
    return;
  }
  state.goals.forEach((item) => {
    const button = document.createElement("button");
    button.className = "goal-item";
    button.type = "button";
    button.innerHTML = `
      <strong>${escapeHtml(item.title)}</strong>
      <span>${goalCardCount(item.id)} open</span>
    `;
    button.addEventListener("click", () => {
      selectGoal(item.id);
      closeDrawer();
    });
    els.goalList.append(button);
  });
}

function renderGoalTabs() {
  const tabs = [
    { id: "all", title: "All", count: unhandledCount(state.cards) },
    ...state.goals
      .map((item) => ({ id: String(item.id), title: item.title, count: goalCardCount(item.id) }))
      .filter((item) => item.count > 0 || String(item.id) === String(state.activeGoalId)),
  ];
  els.goalTabs.innerHTML = tabs
    .map((item) => {
      const active = String(item.id) === String(state.activeGoalId) ? "active" : "";
      return `<button class="${active}" type="button" data-goal="${escapeHtml(item.id)}">${escapeHtml(item.title)} <span>${item.count}</span></button>`;
    })
    .join("");
  els.goalTabs.querySelectorAll("[data-goal]").forEach((button) => {
    button.addEventListener("click", () => selectGoal(button.dataset.goal || "all"));
  });
}

function goalCardCount(goalId) {
  return state.cards.filter((card) => !card.handled && String(card.goal_id || "") === String(goalId)).length;
}

function unhandledCount(cards) {
  return cards.filter((card) => !card.handled).length;
}

function selectGoal(id) {
  state.activeGoalId = String(id || "all");
  state.currentIndex = 0;
  localStorage.setItem(goalKey, state.activeGoalId);
  render();
}

function renderStats() {
  const rows = [
    ["Open", state.stats.open || 0],
    ["Started", state.stats.accepted || 0],
    ["Done", state.stats.completed || 0],
    ["Passed", state.stats.dismissed || 0],
    ["Goals", state.stats.goals || 0],
    ["Replies", state.stats.comments || 0],
  ];
  els.statsGrid.innerHTML = rows
    .map(([label, value]) => `<div class="stat-card"><strong>${value}</strong><span>${label}</span></div>`)
    .join("");
}

function syncDock() {
  const enabled = Boolean(currentCard());
  [els.passButton, els.voiceButton, els.startButton].forEach((button) => {
    button.disabled = !enabled;
  });
  els.topButton.hidden = els.feed.scrollTop < 260;
  els.goalTabs.classList.toggle("scrolled", els.feed.scrollTop > 20);
}

function updateCurrentFromScroll() {
  const center = els.feed.scrollTop + els.feed.clientHeight / 2;
  let bestIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  [...els.feed.querySelectorAll(".story")].forEach((item) => {
    const itemCenter = item.offsetTop + item.offsetHeight / 2;
    const distance = Math.abs(center - itemCenter);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = Number(item.dataset.index || "0");
    }
  });
  const previousIndex = state.currentIndex;
  if (bestIndex > previousIndex && Date.now() > state.suppressScrollPassUntil) {
    visibleCards()
      .slice(previousIndex, bestIndex)
      .forEach((card) => passByScroll(card));
  }
  state.currentIndex = bestIndex;
  const id = currentCard()?.id;
  if (id) localStorage.setItem(cursorKey, String(id));
  syncDock();
}

function restoreCursor() {
  const savedId = localStorage.getItem(cursorKey);
  if (!savedId) return;
  const index = visibleCards().findIndex((card) => String(card.id) === savedId);
  if (index < 0) return;
  state.currentIndex = index;
  els.feed.querySelector(`[data-card-id="${CSS.escape(savedId)}"]`)?.scrollIntoView({ block: "start" });
}

function removeCurrent() {
  const id = currentCard()?.id;
  removeCard(id);
}

function removeCard(id, direction = "left") {
  state.suppressScrollPassUntil = Date.now() + 900;
  const article = els.feed.querySelector(`[data-card-id="${CSS.escape(String(id))}"]`);
  if (article && !article.classList.contains("removing")) {
    article.classList.add("removing", direction === "right" ? "remove-right" : "remove-left");
    setTimeout(() => removeCardNow(id), 220);
    return;
  }
  removeCardNow(id);
}

function removeCardNow(id) {
  state.cards = state.cards.filter((card) => card.id !== id);
  const cards = visibleCards();
  if (state.currentIndex >= cards.length) {
    state.currentIndex = Math.max(0, cards.length - 1);
  }
  render();
  if (visibleCards()[state.currentIndex]) {
    document.querySelector(`[data-index="${state.currentIndex}"]`)?.scrollIntoView();
  }
}

async function startCard(id, button = "") {
  const index = visibleCards().findIndex((card) => card.id === id);
  if (index >= 0) state.currentIndex = index;
  await startCurrent(button);
}

async function startCurrent(button = "") {
  const card = currentCard();
  if (!card) return;
  els.startButton.disabled = true;
  removeCurrent();
  requestAnimationFrame(() => document.querySelector(`[data-index="${state.currentIndex}"]`)?.scrollIntoView({ block: "start" }));
  try {
    const result = await api(`/api/cards/${card.id}/start`, {
      method: "POST",
      body: JSON.stringify({ button }),
    });
    await refreshStats();
    toast(result.topic_created ? "Topic created. Agent started." : "Agent started.");
    tg?.HapticFeedback?.notificationOccurred?.("success");
  } catch (error) {
    toast(error.message);
  } finally {
    syncDock();
  }
}

async function passByScroll(card) {
  if (!card || state.passedByScroll.has(card.id)) return;
  state.passedByScroll.add(card.id);
  card.handled = true;
  renderGoalTabs();
  try {
    await api(`/api/cards/${card.id}/dismiss`, { method: "POST", body: "{}" });
    removeCard(card.id, "left");
    await refreshStats();
  } catch {
    state.passedByScroll.delete(card.id);
  }
}

async function passCard(id, direction = "left") {
  const index = state.cards.findIndex((card) => card.id === id);
  if (index >= 0) state.currentIndex = index;
  await passCurrent(direction);
}

async function passCurrent(direction = "left") {
  const card = currentCard();
  if (!card) return;
  try {
    await api(`/api/cards/${card.id}/dismiss`, { method: "POST", body: "{}" });
    removeCard(card.id, direction);
    await refreshStats();
    toast("Deleted.");
  } catch (error) {
    toast(error.message);
  }
}

async function different(id) {
  try {
    await api(`/api/cards/${id}/different`, { method: "POST", body: "{}" });
    removeCurrent();
    await refreshStats();
    toast("Marked for a different version.");
  } catch (error) {
    toast(error.message);
  }
}

function openDrawer() {
  els.drawer.classList.add("open");
  els.drawer.setAttribute("aria-hidden", "false");
}

function closeDrawer() {
  els.drawer.classList.remove("open");
  els.drawer.setAttribute("aria-hidden", "true");
}

function openGoal() {
  els.goalDialog.showModal();
  els.goalInput.focus();
}

function openContext() {
  els.contextDialog.showModal();
  els.contextInput.focus();
}

async function openInlineContext(id) {
  state.openContextCardId = String(id);
  if (!state.comments[id]) {
    try {
      const result = await api(`/api/cards/${id}/comments`);
      state.comments[id] = result.comments || [];
    } catch {
      state.comments[id] = [];
    }
  }
  render();
  requestAnimationFrame(() => {
    els.feed.querySelector(`[data-card-id="${CSS.escape(String(id))}"] .comment-form textarea`)?.focus();
  });
}

async function submitInlineContext(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const id = form.dataset.cardId;
  const input = form.querySelector("textarea");
  const comment = input.value.trim();
  if (!id || !comment) return;
  try {
    await api(`/api/cards/${id}/comment`, { method: "POST", body: JSON.stringify({ comment }) });
    input.value = "";
    const result = await api(`/api/cards/${id}/comments`);
    state.comments[id] = result.comments || [];
    await refreshStats();
    render();
    toast("Context added.");
  } catch (error) {
    toast(error.message);
  }
}

function attachSwipe(article, id) {
  let startX = 0;
  let startY = 0;
  let tracking = false;
  article.addEventListener("pointerdown", (event) => {
    if (event.target.closest("button, a, textarea, summary, details")) return;
    tracking = true;
    startX = event.clientX;
    startY = event.clientY;
    article.setPointerCapture?.(event.pointerId);
  });
  article.addEventListener("pointermove", (event) => {
    if (!tracking) return;
    const dx = event.clientX - startX;
    const dy = event.clientY - startY;
    if (Math.abs(dy) > Math.abs(dx) + 24) return;
    article.style.setProperty("--swipe-x", `${Math.max(-96, Math.min(96, dx))}px`);
    article.classList.toggle("swiping-delete", dx < -36);
    article.classList.toggle("swiping-context", dx > 36);
  });
  article.addEventListener("pointerup", (event) => {
    if (!tracking) return;
    tracking = false;
    const dx = event.clientX - startX;
    article.style.removeProperty("--swipe-x");
    article.classList.remove("swiping-delete", "swiping-context");
    if (dx < -90) passCard(id, "left");
    if (dx > 90) openInlineContext(id);
  });
  article.addEventListener("pointercancel", () => {
    tracking = false;
    article.style.removeProperty("--swipe-x");
    article.classList.remove("swiping-delete", "swiping-context");
  });
}

function attachSpeech(button, input, idleLabel) {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    button.querySelector("strong").textContent = "Voice unavailable";
    return;
  }
  const recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.onstart = () => {
    button.classList.add("recording");
    button.querySelector("strong").textContent = "Listening";
  };
  recognition.onend = () => {
    button.classList.remove("recording");
    button.querySelector("strong").textContent = idleLabel;
  };
  recognition.onresult = (event) => {
    const text = [...event.results].map((result) => result[0].transcript).join(" ");
    input.value = text;
  };
  button.addEventListener("click", () => recognition.start());
}

els.drawerButton.addEventListener("click", openDrawer);
els.drawer.addEventListener("click", (event) => {
  if (event.target === els.drawer) closeDrawer();
});
document.querySelector("[data-close-drawer]").addEventListener("click", closeDrawer);
els.newGoalButton.addEventListener("click", openGoal);
els.statsButton.addEventListener("click", () => els.statsDialog.showModal());
document.querySelector("[data-close-stats]").addEventListener("click", () => els.statsDialog.close());
els.voiceButton.addEventListener("click", openContext);
els.startButton.addEventListener("click", startCurrent);
els.passButton.addEventListener("click", passCurrent);
els.feed.addEventListener("scroll", () => requestAnimationFrame(updateCurrentFromScroll));
els.topButton.addEventListener("click", () => els.feed.scrollTo({ top: 0, behavior: "smooth" }));
els.goalDialog.addEventListener("click", (event) => {
  if (event.target === els.goalDialog) {
    els.goalInput.value = "";
    els.goalDialog.close();
  }
});
els.goalDialog.addEventListener("cancel", () => {
  els.goalInput.value = "";
});
els.contextDialog.addEventListener("click", (event) => {
  if (event.target === els.contextDialog) {
    els.contextInput.value = "";
    els.contextDialog.close();
  }
});
els.statsDialog.addEventListener("click", (event) => {
  if (event.target === els.statsDialog) els.statsDialog.close();
});

els.goalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const raw = els.goalInput.value.trim();
  if (!raw) return;
  const title = raw.split("\n")[0].slice(0, 120);
  try {
    const result = await api("/api/goals", {
      method: "POST",
      body: JSON.stringify({ title, context: raw, cadence: "" }),
    });
    els.goalInput.value = "";
    state.activeGoalId = String(result.goal_id || "all");
    localStorage.setItem(goalKey, state.activeGoalId);
    els.goalDialog.close();
    closeDrawer();
    await load();
    toast("Goal created.");
  } catch (error) {
    toast(error.message);
  }
});

els.contextForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const card = currentCard();
  const comment = els.contextInput.value.trim();
  if (!card || !comment) return;
  try {
    await api(`/api/cards/${card.id}/comment`, {
      method: "POST",
      body: JSON.stringify({ comment }),
    });
    els.contextInput.value = "";
    els.contextDialog.close();
    await refreshStats();
    toast("Reply added.");
  } catch (error) {
    toast(error.message);
  }
});

async function refreshStats() {
  const stats = await api("/api/stats");
  state.stats = stats.stats || {};
  renderStats();
}

async function load() {
  try {
    const [goals, cards, stats] = await Promise.all([
      api("/api/goals"),
      api("/api/cards"),
      api("/api/stats"),
    ]);
    state.goals = goals.goals || [];
    state.cards = cards.cards || [];
    state.stats = stats.stats || {};
    if (state.activeGoalId !== "all" && !state.goals.some((item) => String(item.id) === String(state.activeGoalId))) {
      state.activeGoalId = "all";
      localStorage.setItem(goalKey, "all");
    }
    state.currentIndex = 0;
    render();
  } catch (error) {
    els.feed.innerHTML = `
      <article class="empty">
        <div>
          <h1>Login failed</h1>
          <p>${escapeHtml(error.message)}</p>
        </div>
      </article>
    `;
  }
}

attachSpeech(els.recordButton, els.contextInput, "Tap to speak");
attachSpeech(els.goalRecordButton, els.goalInput, "Speak goal");
load();
