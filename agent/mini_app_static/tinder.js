const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();
try {
  tg?.setHeaderColor?.("#f7f4ec");
  tg?.setBackgroundColor?.("#f7f4ec");
  tg?.setBottomBarColor?.("#f7f4ec");
} catch {
  // Telegram clients expose different Mini App capabilities.
}

const params = new URLSearchParams(window.location.search);
if (params.get("dev") === "1") localStorage.buxMiniAppDev = "1";
const initData = tg?.initData || (localStorage.buxMiniAppDev === "1" ? "dev" : "");
const goalKey = "buxTinderGoalId";
const indexKey = "buxTinderIndex";
const variantKey = "buxTinderVariants";
let savedVariants = {};
try {
  savedVariants = JSON.parse(localStorage.getItem(variantKey) || "{}");
} catch {
  savedVariants = {};
}

const state = {
  cards: [],
  goals: [],
  topics: [],
  stats: {},
  game: null,
  activity: [],
  me: { settings: {} },
  activeGoalId: localStorage.getItem(goalKey) || "all",
  railCollapsed: localStorage.getItem("buxTinderRailCollapsed") === "1",
  index: Number(localStorage.getItem(indexKey) || "0"),
  started: Number(localStorage.getItem("buxTinderStarted") || "0"),
  skipped: Number(localStorage.getItem("buxTinderSkipped") || "0"),
  variants: savedVariants,
  commentOpenId: "",
  pending: new Set(),
};

const els = {
  rail: document.querySelector("#goalRail"),
  tabs: document.querySelector("#goalTabs"),
  kingPanel: document.querySelector("#kingPanel"),
  mobileGoals: document.querySelector("#mobileGoals"),
  goalCount: document.querySelector("#goalCountLabel"),
  deck: document.querySelector("#deck"),
  deckTitle: document.querySelector("#deckTitle"),
  meta: document.querySelector("#deckMeta"),
  activity: document.querySelector("#activityFeed"),
  provider: document.querySelector("#providerPill"),
  toast: document.querySelector("#toast"),
  context: document.querySelector("#contextButton"),
  autopilot: document.querySelector("#autopilotButton"),
  more: document.querySelector("#moreButton"),
  skipAction: document.querySelector("#skipAction"),
  startAction: document.querySelector("#startAction"),
  newGoal: document.querySelector("#newGoalButton"),
  mobileGoalButton: document.querySelector("#mobileGoalsButton"),
  collapseRail: document.querySelector("#collapseRailButton"),
  sheet: document.querySelector("#contextSheet"),
  form: document.querySelector("#contextForm"),
  input: document.querySelector("#contextInput"),
  voice: document.querySelector("#voiceButton"),
  goalSheet: document.querySelector("#goalSheet"),
  goalForm: document.querySelector("#goalForm"),
  goalInput: document.querySelector("#goalInput"),
  workSheet: document.querySelector("#workSheet"),
  workForm: document.querySelector("#workForm"),
  laneSheet: document.querySelector("#laneSheet"),
  laneList: document.querySelector("#laneList"),
};

let dragState = null;

const RANKS = [
  { name: "Farmer", floor: 0, icon: "seed" },
  { name: "Builder", floor: 260, icon: "hammer" },
  { name: "Merchant", floor: 720, icon: "coin" },
  { name: "Strategist", floor: 1380, icon: "map" },
  { name: "Regent", floor: 2300, icon: "crown" },
  { name: "King of Life", floor: 3600, icon: "king" },
];

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

function currentStack() {
  return visibleCards().slice(state.index, state.index + 3);
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => els.toast.classList.remove("show"), 1800);
}

function haptic(kind = "selection") {
  try {
    if (kind === "success" || kind === "error" || kind === "warning") {
      tg?.HapticFeedback?.notificationOccurred?.(kind);
    } else if (kind === "selection") {
      tg?.HapticFeedback?.selectionChanged?.();
    } else {
      tg?.HapticFeedback?.impactOccurred?.(kind);
    }
  } catch {
    // Haptics are best-effort.
  }
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

function persistVariants() {
  localStorage.setItem(variantKey, JSON.stringify(state.variants));
}

function selectedVariantIndex(card) {
  const labels = cardActionButtons(card);
  if (!labels.length) return 0;
  const stored = Number(state.variants[String(card.id)] || 0);
  return Number.isFinite(stored) ? Math.min(Math.max(stored, 0), labels.length - 1) : 0;
}

function selectedButton(card) {
  const buttons = cardActionButtons(card);
  if (!buttons.length) return null;
  return buttons[selectedVariantIndex(card)] || buttons[0] || null;
}

function selectedBlock(card) {
  const blocks = Array.isArray(card.blocks) ? card.blocks : [];
  if (!blocks.length) return null;
  const buttons = cardActionButtons(card);
  const selected = buttons[selectedVariantIndex(card)];
  if (selected) {
    const matched = blockForButton(blocks, selected.text);
    if (matched) return matched;
  }
  if (buttons.length === blocks.length) return blocks[selectedVariantIndex(card)] || blocks[0];
  return blocks[0];
}

function blockForButton(blocks, label) {
  const needle = normalizeMatch(label);
  if (!needle) return null;
  return blocks.find((block) => {
    const haystack = normalizeMatch(`${block.title || ""} ${block.body || ""}`);
    if (!haystack) return false;
    return haystack.includes(needle) || needle.includes(haystack.slice(0, 24));
  }) || null;
}

function normalizeMatch(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/^[^a-z0-9]+/g, "")
    .replace(/\b(option|variant|draft|send|use|post|start|the|this)\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function otherBlocks(card) {
  const blocks = Array.isArray(card.blocks) ? card.blocks : [];
  const selected = selectedBlock(card);
  return blocks.filter((block) => block !== selected);
}

function render() {
  const cards = visibleCards();
  const card = currentCard();
  const progress = kingProgress();
  const position = card ? `${Math.min(state.index + 1, cards.length)}/${cards.length}` : "0/0";
  els.deckTitle.textContent = goalTitle();
  els.meta.textContent = `${cards.length} open · ${progress.done} done · ${progress.rank.name} · ${position}`;
  els.provider.textContent = providerLabel();
  localStorage.setItem(indexKey, String(state.index));
  renderKingPanel(progress);
  renderGoals();
  renderActivity();
  renderDeck(cards);
  syncGlobalButtons(Boolean(card));
}

function kingProgress() {
  if (state.game?.rank) {
    const stats = state.game.stats || state.stats || {};
    return {
      points: Number(state.game.points || 0),
      done: Number(stats.done || 0),
      skipped: Number(stats.dismissed || 0),
      comments: Number(stats.comments || 0),
      goals: Number(stats.goals || state.goals.length || 0),
      open: Number(stats.open || state.cards.length || 0),
      rank: state.game.rank,
      next: state.game.next_rank || state.game.rank,
      pct: Number(state.game.progress || 0),
    };
  }
  const done = Number(state.stats.done || 0) + state.started;
  const skipped = Number(state.stats.dismissed || 0) + state.skipped;
  const comments = Number(state.stats.comments || 0);
  const goals = Number(state.stats.goals || state.goals.length || 0);
  const open = Number(state.stats.open || state.cards.length || 0);
  const points = Math.max(0, done * 180 + comments * 45 + goals * 90 + Math.max(0, 10 - Math.min(open, 10)) * 12 - skipped * 6);
  let rankIndex = 0;
  RANKS.forEach((rank, index) => {
    if (points >= rank.floor) rankIndex = index;
  });
  const rank = RANKS[rankIndex] || RANKS[0];
  const next = RANKS[Math.min(rankIndex + 1, RANKS.length - 1)];
  const span = Math.max(1, next.floor - rank.floor);
  const pct = rank === next ? 100 : Math.min(100, Math.round(((points - rank.floor) / span) * 100));
  return { points, done, skipped, comments, goals, open, rank, next, pct };
}

function renderKingPanel(progress) {
  if (!els.kingPanel) return;
  els.kingPanel.innerHTML = `
    <div class="rank-orbit" aria-hidden="true">
      <span></span><span></span><span></span>
    </div>
    <div class="rank-copy">
      <span>${escapeHtml(progress.rank.icon)}</span>
      <strong>${escapeHtml(progress.rank.name)}</strong>
      <small>${progress.points} XP · ${progress.next.name === progress.rank.name ? "max rank" : `${progress.next.floor - progress.points} XP to ${progress.next.name}`}</small>
    </div>
    <div class="rank-meter"><i style="width:${progress.pct}%"></i></div>
    <div class="rank-stats">
      <span><strong>${progress.done}</strong> done</span>
      <span><strong>${progress.open}</strong> open</span>
      <span><strong>${progress.comments}</strong> comments</span>
    </div>
  `;
}

function syncGlobalButtons(hasCard) {
  els.startAction.disabled = !hasCard || !selectedButton(currentCard());
  els.skipAction.disabled = !hasCard;
  els.context.disabled = !hasCard && state.activeGoalId === "all";
}

function renderGoals() {
  document.body.classList.toggle("rail-collapsed", state.railCollapsed);
  els.collapseRail.innerHTML = railSvg(state.railCollapsed);
  els.collapseRail.setAttribute("aria-label", state.railCollapsed ? "Expand side rail" : "Collapse side rail");
  const tabs = goalTabs();
  els.goalCount.textContent = String(tabs.length - 1);
  const railTabs = prioritizeTabs(tabs).slice(0, 5);
  els.tabs.innerHTML = railTabs.map(goalTabHtml).join("");
  els.mobileGoals.innerHTML = activeLaneHtml(tabs);
  els.laneList.innerHTML = prioritizeTabs(tabs).map(goalTabHtml).join("");
  [els.tabs, els.laneList].forEach((container) => {
    container.querySelectorAll("[data-goal]").forEach((button) => {
      button.addEventListener("click", () => {
        state.activeGoalId = button.dataset.goal || "all";
        state.index = 0;
        localStorage.setItem(goalKey, state.activeGoalId);
        els.laneSheet.close();
        render();
      });
    });
  });
  els.mobileGoals.querySelector("[data-open-lanes]")?.addEventListener("click", () => {
    els.laneSheet.showModal();
  });
}

function goalTabs() {
  const tabs = [
    { id: "all", title: "All cards", count: Number(state.stats.open || state.cards.length), subtitle: "Everything still waiting" },
    ...state.goals.map((goal) => {
      const threadId = Number(goal.tg_thread_id || 0);
      const id = threadId ? `topic:${threadId}` : String(goal.id);
      return {
        id,
        title: goal.title || "Goal",
        count: countFor(id),
        subtitle: threadId ? "Telegram topic" : "Goal lane",
      };
    }),
    ...state.topics.map((topic) => ({
      id: `topic:${topic.thread_id || topic.id}`,
      title: topic.title || `Topic ${topic.thread_id || topic.id}`,
      count: Number(topic.count || 0),
      subtitle: "Telegram topic",
    })),
  ];
  const seen = new Set();
  return tabs.filter((tab) => {
    if (seen.has(tab.id)) return false;
    seen.add(tab.id);
    return true;
  });
}

function goalTabHtml(tab) {
  const active = tab.id === state.activeGoalId ? "active" : "";
  return `
    <button class="${active}" data-goal="${escapeAttr(tab.id)}" type="button">
      <div class="goal-copy">
        <strong class="goal-title">${escapeHtml(clipLabel(tab.title, 38))}</strong>
        <small class="goal-subline">${escapeHtml(tab.subtitle || "")}</small>
      </div>
      <span class="goal-count">${tab.count}</span>
    </button>
  `;
}

function activeLaneHtml(tabs) {
  const active = tabs.find((tab) => tab.id === state.activeGoalId) || tabs[0];
  return `
    <button class="active-lane" data-open-lanes type="button">
      <span>${escapeHtml(clipLabel(active?.title || "All cards", 28))}</span>
      <small>${Number(active?.count || 0)}</small>
    </button>
  `;
}

function prioritizeTabs(tabs) {
  return [...tabs].sort((a, b) => {
    if (a.id === state.activeGoalId) return -1;
    if (b.id === state.activeGoalId) return 1;
    if (a.id === "all") return -1;
    if (b.id === "all") return 1;
    return Number(b.count || 0) - Number(a.count || 0);
  });
}

function renderActivity() {
  if (!state.activity.length) {
    els.activity.innerHTML = `<article class="activity-item"><span class="activity-dot"></span><div class="activity-copy"><strong>No recent decisions</strong><small>Swipe or tap cards here and the sync log will fill in.</small></div></article>`;
    return;
  }
  els.activity.innerHTML = state.activity.map(activityHtml).join("");
  els.activity.querySelectorAll("[data-activity-thread]").forEach((button) => {
    button.addEventListener("click", () => {
      const threadId = button.dataset.activityThread || "";
      if (!threadId) return;
      state.activeGoalId = `topic:${threadId}`;
      state.index = 0;
      localStorage.setItem(goalKey, state.activeGoalId);
      render();
    });
  });
}

function activityHtml(item) {
  const status = String(item.status || "");
  const threadLabel = item.thread_id ? ` · ${clipLabel(item.thread_title || `Topic ${item.thread_id}`, 18)}` : "";
  return `
    <button class="activity-item" type="button" data-activity-thread="${escapeAttr(item.thread_id || "")}">
      <span class="activity-dot ${activityDotClass(status)}"></span>
      <div class="activity-copy">
        <strong>${escapeHtml(clipLabel(item.title || "Recent card", 52))}</strong>
        <small>${escapeHtml(activityStatusLabel(item))}${escapeHtml(threadLabel)}</small>
      </div>
    </button>
  `;
}

function activityDotClass(status) {
  if (status === "accepted") return "is-accepted";
  if (status === "dismissed") return "is-dismissed";
  if (status === "completed") return "is-completed";
  return "";
}

function activityStatusLabel(item) {
  const decision = String(item.decision || "").trim();
  const status = String(item.status || "");
  if (decision) return `${decision} · ${relativeAge(item.updated_at)}`;
  if (status) return `${titleCase(status)} · ${relativeAge(item.updated_at)}`;
  return relativeAge(item.updated_at);
}

function renderDeck(cards) {
  const stack = currentStack();
  if (!stack.length) {
    els.deck.innerHTML = `
      <article class="empty empty-kingdom">
        <div class="empty-crown" aria-hidden="true"></div>
        <strong>${escapeHtml(emptyTitle())}</strong>
        <p>Tell Agency your goal or generate quests. The feed should never stay empty when there is progress to make.</p>
        <button class="choice-button" data-empty-generate type="button">Generate quests</button>
      </article>
    `;
    els.deck.querySelector("[data-empty-generate]")?.addEventListener("click", generateMore);
    return;
  }
  els.deck.innerHTML = stack
    .map((card, offset) => cardHtml(card, offset, cards.length))
    .reverse()
    .join("");
  bindDeck();
}

function cardHtml(card, stackIndex) {
  const top = stackIndex === 0;
  const meta = sourceMeta(card);
  const action = selectedButton(card);
  const selected = selectedBlock(card);
  const prepared = completedWorkTags(card);
  const others = otherBlocks(card);
  const hero = heroVisual(card, meta);
  const progress = kingProgress();
  return `
    <article
      class="deck-card king-card stack-${stackIndex} ${top ? "is-top" : ""}"
      data-card-id="${card.id}"
      data-top="${top ? "1" : "0"}"
    >
      <section class="hero-panel ${hero.hasMedia ? "has-media" : ""}">
        ${hero.media}
        <div class="hero-sheen"></div>
        <div class="hero-copy">
          <div class="hero-meta">
            <span class="card-source">${sourceIconHtml(meta)}${escapeHtml(meta.name)}</span>
            <span class="hero-age">${escapeHtml(relativeAge(card.created_at))}</span>
          </div>
          <div class="hero-footer">
            <div>
              <h2 class="hero-title">${escapeHtml(cardHeadline(card))}</h2>
              <p class="hero-why">${escapeHtml(primaryWhy(card))}</p>
            </div>
            <span class="status-pill">${escapeHtml(pointsLabel(card))}</span>
          </div>
        </div>
        <div class="swipe-badge nope">Skip</div>
        <div class="swipe-badge like">Start</div>
      </section>

      <section class="quest-meter" aria-label="Progress">
        <span>${escapeHtml(progress.rank.name)}</span>
        <i><b style="width:${Math.max(10, progress.pct)}%"></b></i>
        <span>${escapeHtml(cardFooterStatus(card))}</span>
      </section>

      <section class="card-summary">
        <div class="summary-copy">
          <strong>${escapeHtml(summaryLabel(card))}</strong>
          <p>${escapeHtml(summaryValue(card, selected))}</p>
        </div>
        <span class="count-pill">${escapeHtml(countLabel(card))}</span>
      </section>

      <section class="card-body">
        <div class="insight-grid">
          <article class="insight-panel">
            <span class="insight-label">Why this moves the goal</span>
            <div class="insight-value">${renderRichText(primaryInsight(card, selected))}</div>
          </article>
          ${prepared.length ? preparedPanelHtml(prepared) : ""}
          ${others.map((block) => detailPanelHtml(block.title || "Details", block.body)).join("")}
        </div>
      </section>

      <footer class="card-footer">
        ${actionButtonsHtml(card)}
        <div class="utility-row">
          <button class="utility-button skip-mini" data-dismiss-current type="button" aria-label="Skip">×</button>
          <button class="utility-button comment-mini" data-toggle-comment type="button" aria-label="Comment">${commentSvg()}</button>
          <span>${escapeHtml(actionPreview(card, action))}</span>
        </div>
        ${top && String(state.commentOpenId) === String(card.id) ? inlineCommentHtml(card) : ""}
      </footer>
    </article>
  `;
}

function heroVisual(card, meta) {
  const visual = card.visual || {};
  if (visual.kind === "image" && visual.src) {
    return {
      hasMedia: true,
      media: `<div class="hero-media"><img src="${escapeAttr(visual.src)}" alt="${escapeAttr(meta.name)}" /></div>`,
    };
  }
  if (visual.kind === "video" && visual.src) {
    return {
      hasMedia: true,
      media: `<div class="hero-media"><video src="${escapeAttr(visual.src)}" muted autoplay loop playsinline preload="metadata"></video></div>`,
    };
  }
  return {
    hasMedia: false,
    media: `
      <div class="kingdom-visual" aria-hidden="true">
        <div class="sun"></div>
        <div class="castle"><span></span><span></span><span></span></div>
        <div class="road"></div>
        <div class="spark spark-a"></div>
        <div class="spark spark-b"></div>
      </div>
    `,
  };
}

function sourceIconHtml(meta) {
  if (meta.domain) {
    return `<img class="source-favicon" src="https://www.google.com/s2/favicons?domain=${escapeAttr(meta.domain)}&sz=64" alt="" />`;
  }
  const letters = String(meta.name || "A")
    .split(/\s+/)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return `<span class="source-initials" aria-hidden="true">${escapeHtml(letters || "A")}</span>`;
}

function pointsLabel(card) {
  return `+${pointsForCard(card)} XP`;
}

function pointsForCard(card) {
  const importance = String(card.importance || "").toLowerCase();
  if (importance === "high") return 220;
  if (importance === "low") return 80;
  if (String(card.source || "").startsWith("miniapp-goal:")) return 180;
  return 140;
}

function actionButtonsHtml(card) {
  const buttons = cardActionButtons(card);
  if (!buttons.length) {
    return `
      <div class="action-button-row">
        <button class="choice-button ghost" data-toggle-comment type="button">Add context</button>
      </div>
    `;
  }
  return `
    <div class="action-button-row">
      ${buttons
        .slice(0, 5)
        .map(
          (button, index) => `
            <button
              class="choice-button ${index === selectedVariantIndex(card) ? "active" : ""}"
              data-start-button="${escapeAttr(button.raw)}"
              type="button"
            >
              ${escapeHtml(button.text)}
            </button>
          `
        )
        .join("")}
    </div>
  `;
}

function inlineCommentHtml(card) {
  return `
    <form class="inline-comment" data-inline-comment="${escapeAttr(card.id)}">
      <textarea rows="2" placeholder="Add context, ask for variants, or change the angle."></textarea>
      <button class="primary-button" type="submit">Send</button>
    </form>
  `;
}

function commentSvg() {
  return `<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="M7 17.5 4.5 20v-4.2A7.8 7.8 0 0 1 3 11c0-4.2 3.8-7.5 9-7.5s9 3.3 9 7.5-3.8 7.5-9 7.5c-1.9 0-3.6-.4-5-1Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>`;
}

function variantStripHtml(card) {
  const buttons = cardActionButtons(card);
  if (buttons.length <= 1) return "";
  const selected = selectedVariantIndex(card);
  return `
    <section class="variant-strip" aria-label="Card versions">
      ${buttons
        .map(
          (button, index) => `
            <button
              class="variant-pill ${index === selected ? "active" : ""}"
              type="button"
              data-variant-card="${card.id}"
              data-variant-index="${index}"
            >
              ${escapeHtml(button.text)}
            </button>
          `
        )
        .join("")}
    </section>
  `;
}

function preparedPanelHtml(items) {
  return `
    <article class="insight-panel">
      <span class="insight-label">Already prepared</span>
      <ul class="prepared-list">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
    </article>
  `;
}

function detailPanelHtml(title, body, open = false) {
  return `
    <details class="detail-panel" ${open ? "open" : ""}>
      <summary>${escapeHtml(title)}</summary>
      <div class="detail-copy">${renderRichText(body)}</div>
    </details>
  `;
}

function actionDetailHtml(card) {
  const action = String(card.action || "").trim();
  if (!action || action === card.title || action === card.why) return "";
  return detailPanelHtml("Agent prompt", action);
}

function bindDeck() {
  els.deck.querySelectorAll("[data-variant-card]").forEach((button) => {
    button.addEventListener("click", () => {
      const id = String(button.dataset.variantCard || "");
      state.variants[id] = Number(button.dataset.variantIndex || "0");
      persistVariants();
      haptic("selection");
      render();
    });
  });

  const topCard = els.deck.querySelector(".deck-card.is-top");
  if (topCard) bindDrag(topCard);

  els.deck.querySelectorAll("[data-start-current]").forEach((button) => {
    button.addEventListener("click", () => startCurrentCard());
  });
  els.deck.querySelectorAll("[data-start-button]").forEach((button) => {
    button.addEventListener("click", () => {
      const card = currentCard();
      if (!card) return;
      const buttons = cardActionButtons(card);
      const index = buttons.findIndex((item) => item.raw === button.dataset.startButton);
      if (index >= 0) {
        state.variants[String(card.id)] = index;
        persistVariants();
      }
      startCard(card.id, button.dataset.startButton || "", els.deck.querySelector(".deck-card.is-top"));
    });
  });
  els.deck.querySelectorAll("[data-dismiss-current]").forEach((button) => {
    button.addEventListener("click", () => dismissCurrentCard());
  });
  els.deck.querySelectorAll("[data-toggle-comment]").forEach((button) => {
    button.addEventListener("click", () => toggleInlineComment());
  });
  els.deck.querySelectorAll("[data-inline-comment]").forEach((form) => {
    form.addEventListener("submit", sendInlineComment);
  });
}

function bindDrag(node) {
  node.addEventListener("pointerdown", (event) => {
    if (event.button && event.button !== 0) return;
    if (event.target.closest("button, a, summary, details, input, textarea")) return;
    dragState = {
      node,
      startX: event.clientX,
      startY: event.clientY,
      dx: 0,
      dy: 0,
      active: true,
    };
    node.classList.add("dragging");
    node.setPointerCapture?.(event.pointerId);
  });

  node.addEventListener("pointermove", (event) => {
    if (!dragState?.active || dragState.node !== node) return;
    dragState.dx = event.clientX - dragState.startX;
    dragState.dy = event.clientY - dragState.startY;
    const rotate = dragState.dx / 16;
    node.style.transform = `translate(${dragState.dx}px, ${dragState.dy * 0.18}px) rotate(${rotate}deg)`;
    node.classList.toggle("show-like", dragState.dx > 28);
    node.classList.toggle("show-nope", dragState.dx < -28);
  });

  function finish(pointerId) {
    if (!dragState?.active || dragState.node !== node) return;
    const { dx, dy } = dragState;
    dragState.active = false;
    node.classList.remove("dragging");
    node.releasePointerCapture?.(pointerId);
    if (Math.abs(dx) > 118 && Math.abs(dx) > Math.abs(dy)) {
      if (dx > 0) startCurrentCard(node);
      else dismissCurrentCard(node);
      dragState = null;
      return;
    }
    node.style.transform = "";
    node.classList.remove("show-like", "show-nope");
    dragState = null;
  }

  node.addEventListener("pointerup", (event) => finish(event.pointerId));
  node.addEventListener("pointercancel", (event) => finish(event.pointerId));
}

function renderRichText(value) {
  const links = [];
  const withMarkdownLinks = String(value || "").replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_match, label, url) => {
    const token = `@@LINK_${links.length}@@`;
    links.push(`<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
    return token;
  });
  return escapeHtml(withMarkdownLinks)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(https?:\/\/[^\s<]+)/g, (url) => `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(shortUrl(url))}</a>`)
    .replace(/@@LINK_(\d+)@@/g, (_match, index) => links[Number(index)] || "")
    .replace(/\n+/g, "<br />");
}

function summaryLabel(card) {
  return String(card.source || "").startsWith("miniapp-") ? "Best first move" : "Why this card matters";
}

function summaryValue(card, selected = null) {
  if (selected?.body) return clipLabel(selected.body, 132);
  const text = primaryWhy(card);
  return text || "Quick context is ready. You just decide if it should start.";
}

function primaryInsight(card, selected) {
  if (selected?.body) return selected.body;
  const title = cleanCardTitle(card.title);
  const why = String(card.why || "").trim();
  return why && why !== title ? why : title || why || "Ready when you are.";
}

function primaryWhy(card) {
  const why = String(card.why || "").trim();
  if (why) return why;
  const title = cleanCardTitle(card.title);
  return title || "Ready to act.";
}

function cardHeadline(card) {
  const source = String(card.source || "");
  const limit = source.startsWith("miniapp-") ? 28 : 38;
  return clipLabel(cleanCardTitle(card.title) || sourceMeta(card).name, limit);
}

function cardFooterStatus(card) {
  const comments = Number(card.comments || 0);
  if (comments > 0) return `${comments} notes`;
  if (String(card.source || "").startsWith("miniapp-")) return "Starter";
  if (card.topic_title) return clipLabel(card.topic_title, 14);
  return "Live";
}

function countLabel(card) {
  if (String(card.source || "").startsWith("miniapp-setup:")) return "Setup";
  if (String(card.source || "").startsWith("miniapp-goal:")) return "Goal";
  if (Number(card.comments || 0) > 0) return `${card.comments} notes`;
  return "Now";
}

function actionPreview(card, action) {
  if (action && String(card.source || "").startsWith("miniapp-setup:")) return "Guide setup, then start using real data.";
  if (action && String(card.source || "").startsWith("miniapp-goal:")) return "Lock this as a standing goal and generate sharper follow-ups.";
  if (action) return "Right swipe starts the selected version immediately.";
  return "Add context to improve the next version.";
}

function cardActionButtons(card) {
  const prompt = String(card.action || "").trim();
  const labels = Array.isArray(card.buttons) ? card.buttons : [];
  const buttons = labels
    .map((raw) => ({ raw: String(raw || "").trim(), text: buttonText(raw, card) }))
    .filter((button) => button.raw && button.text);
  if (!prompt && !buttons.length) return [];
  if (!buttons.length) return [{ raw: "Do it", text: inferredActionLabel(card) }];
  return buttons;
}

function buttonText(label, card = {}) {
  const raw = String(label || "").trim();
  const normalized = raw.replace(/[^a-z ]/gi, " ").replace(/\s+/g, " ").trim().toLowerCase();
  if (/^(skip|dismiss|delete|pass|edit|refine|change|context|more|more options?)$/.test(normalized)) return "";
  if (/^(no|no thanks|not now)$/.test(normalized)) return "";
  if (/^(yes|yes new thread|do it|start)$/.test(normalized)) return inferredActionLabel(card);
  return raw.replace(/^[^\p{L}\p{N}]+/u, "").replace(/\s+/g, " ").trim().slice(0, 32);
}

function inferredActionLabel(card) {
  if (String(card.source || "").startsWith("miniapp-setup:")) return "Set it up";
  if (String(card.source || "").startsWith("miniapp-goal:")) return "Lock goal";
  const text = [card.title, card.why, card.action, card.source_label, card.source].join(" ").toLowerCase();
  const rules = [
    [/\b(send|reply|dm|email|message)\b/, "Send draft"],
    [/\b(post|tweet|quote|repost|linkedin|reddit|hacker news|bookface)\b/, "Post it"],
    [/\b(merge|approve pr|pull request)\b/, "Merge PR"],
    [/\b(publish|launch|submit listing|ship live)\b/, "Publish"],
    [/\b(buy|purchase|pay|book|billing)\b/, "Review spend"],
    [/\b(close|delete|remove|archive)\b/, "Review change"],
    [/\b(draft|stage|prepare|write)\b/, "Open draft"],
    [/\b(test|check|inspect|review|analyze|triage)\b/, "Run check"],
    [/\b(build|implement|patch|add|replace|fix|store|feed|save)\b/, "Implement"],
  ];
  return (rules.find(([pattern]) => pattern.test(text)) || [null, "Start"])[1];
}

function completedWorkTags(card) {
  if (String(card.source || "").startsWith("miniapp-")) return [];
  const tags = new Set();
  const blocks = Array.isArray(card.blocks) ? card.blocks : [];
  const blockText = blocks.map((block) => `${block.title || ""} ${block.body || ""}`).join(" ").toLowerCase();
  const allText = [card.title, card.why, card.action, blockText].join(" ").toLowerCase();
  if (/\b(draft|variant|reply|message|post copy|script)\b/.test(blockText)) tags.add("Drafts are already prepared.");
  if (/\b(diff|pr|pull request|patch|test)\b/.test(allText)) tags.add("The code context was already inspected.");
  if (/\b(asset ready|image ready|video ready|screenshot|clip ready|attached asset)\b/.test(allText) || card.visual?.kind === "image" || card.visual?.kind === "video") tags.add("The supporting asset is ready.");
  if (/\b(analy[sz]e|data|metrics|scoreboard|signup|flight|compare|research)\b/.test(allText)) tags.add("The research step already happened.");
  return [...tags].slice(0, 3);
}

function sourceMeta(card) {
  const source = String(card.source || "");
  if (source.startsWith("miniapp-setup:gmail")) return { name: "Gmail", domain: "mail.google.com" };
  if (source.startsWith("miniapp-setup:slack")) return { name: "Slack", domain: "slack.com" };
  if (source.startsWith("miniapp-setup:github")) return { name: "GitHub", domain: "github.com" };
  if (source.startsWith("miniapp-goal:")) return { name: "Goal lane", domain: "" };

  const url = displaySourceUrl(card);
  const host = sourceHost(url);
  const brands = [
    ["producthunt.com", "Product Hunt", "producthunt.com"],
    ["linkedin.com", "LinkedIn", "linkedin.com"],
    ["news.ycombinator.com", "Hacker News", "news.ycombinator.com"],
    ["ycombinator.com", "YC", "ycombinator.com"],
    ["mail.google.com", "Gmail", "mail.google.com"],
    ["slack.com", "Slack", "slack.com"],
    ["reddit.com", "Reddit", "reddit.com"],
    ["github.com", "GitHub", "github.com"],
    ["telegram.org", "Telegram", "telegram.org"],
    ["x.com", "X", "x.com"],
    ["twitter.com", "X", "x.com"],
    ["linear.app", "Linear", "linear.app"],
    ["datadoghq.com", "Datadog", "datadoghq.com"],
  ];
  const explicitText = [card.source_label, card.source, card.title, card.why].join(" ").toLowerCase();
  const explicit = brands.find(([needle]) => explicitText.includes(needle));
  if (explicit) return { name: explicit[1], domain: explicit[2] };
  const found = brands.find(([needle]) => host.toLowerCase().includes(needle));
  if (found) return { name: found[1], domain: found[2] };
  const name = String(card.source || "Agency").split("-").filter(Boolean).slice(0, 2).join(" ") || "Agency";
  return { name: titleCase(name), domain: "" };
}

function cleanCardTitle(value) {
  const text = String(value || "").replace(/^goal:\s*/i, "").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

function displaySourceUrl(card) {
  const url = String(card.source_url || "").trim();
  if (!url) return "";
  const host = sourceHost(url);
  const labelText = [card.source_label, card.source, card.title, card.why].join(" ").toLowerCase();
  const genericBuxRepo = host === "github.com" && /github\.com\/browser-use\/bux\/?$/i.test(url);
  if (genericBuxRepo && !/\b(github|pull request|pr #|repo|issue)\b/i.test(labelText)) return "";
  return url;
}

function sourceHost(url) {
  try {
    return new URL(url || "").hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function shortUrl(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "link";
  }
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

function titleCase(value) {
  return String(value || "Agency").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function clipLabel(value, limit) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 1).trim()}…` : text;
}

function openCount() {
  if (state.activeGoalId === "all") return Number(state.stats.open || state.cards.length);
  return visibleCards().length;
}

function goalTitle() {
  if (state.activeGoalId === "all") return "All goals";
  if (state.activeGoalId.startsWith("topic:")) {
    const topicId = state.activeGoalId.slice("topic:".length);
    const topic = state.topics.find((item) => String(item.thread_id || item.id) === topicId);
    const goal = state.goals.find((item) => String(item.tg_thread_id || "") === topicId);
    return clipLabel((goal?.title || topic?.title || "Goal").trim(), 34);
  }
  const goal = state.goals.find((item) => String(item.id) === String(state.activeGoalId));
  return clipLabel((goal?.title || "Goal").trim(), 34);
}

function providerLabel() {
  const provider = String(state.me?.settings?.provider || "").trim().toLowerCase();
  if (provider === "codex") return "Telegram sync live · Codex";
  if (provider === "claude") return "Telegram sync live · Claude";
  return "Telegram sync live";
}

function emptyTitle() {
  if (state.activeGoalId === "all") return "No open cards";
  return "This lane is clear";
}

function countFor(id) {
  if (id.startsWith("topic:")) {
    const topicId = id.slice("topic:".length);
    return state.cards.filter((card) => String(card.topic_id || "0") === topicId).length;
  }
  return state.cards.filter((card) => String(card.goal_id || "") === String(id)).length;
}

async function startCurrentCard(item = null) {
  const card = currentCard();
  if (!card) return;
  const action = selectedButton(card);
  if (!action) {
    toast("Add context first.");
    openContext();
    return;
  }
  await startCard(card.id, action?.raw || "", item || els.deck.querySelector(".deck-card.is-top"));
}

async function dismissCurrentCard(item = null) {
  const card = currentCard();
  if (!card) return;
  await dismissCard(card.id, item || els.deck.querySelector(".deck-card.is-top"));
}

async function startCard(id, button, item) {
  if (state.pending.has(String(id))) return;
  state.pending.add(String(id));
  item?.classList.add("accept-right");
  try {
    const result = await api(`/api/cards/${id}/start`, { method: "POST", body: JSON.stringify({ button }) });
    if (result.game) state.game = result.game;
    haptic("success");
    state.started += 1;
    decrementOpenCount();
    localStorage.setItem("buxTinderStarted", String(state.started));
    removeLocal(id);
    scheduleRefresh();
    toast(result.reward?.points ? `Started. +${result.reward.points} XP` : "Started.");
  } catch (error) {
    item?.classList.remove("accept-right");
    haptic("error");
    toast(error.message);
  } finally {
    state.pending.delete(String(id));
  }
}

async function dismissCard(id, item) {
  if (state.pending.has(String(id))) return;
  state.pending.add(String(id));
  item?.classList.add("dismiss-left");
  try {
    const result = await api(`/api/cards/${id}/dismiss`, { method: "POST", body: "{}" });
    if (result.game) state.game = result.game;
    haptic("medium");
    state.skipped += 1;
    decrementOpenCount();
    localStorage.setItem("buxTinderSkipped", String(state.skipped));
    removeLocal(id);
    scheduleRefresh();
    toast("Skipped.");
  } catch (error) {
    item?.classList.remove("dismiss-left");
    haptic("error");
    toast(error.message);
  } finally {
    state.pending.delete(String(id));
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
  const card = currentCard();
  if (card?.id) {
    toggleInlineComment();
    return;
  }
  els.sheet.showModal();
  els.input.focus({ preventScroll: true });
}

function toggleInlineComment() {
  const card = currentCard();
  if (!card?.id) {
    openContext();
    return;
  }
  state.commentOpenId = String(state.commentOpenId) === String(card.id) ? "" : String(card.id);
  haptic("selection");
  render();
  if (state.commentOpenId) {
    setTimeout(() => {
      els.deck.querySelector(".inline-comment textarea")?.focus({ preventScroll: true });
    }, 0);
  }
}

async function sendInlineComment(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const textarea = form.querySelector("textarea");
  const comment = textarea?.value.trim() || "";
  if (!comment) return;
  const card = currentCard();
  if (!card?.id) return;
  form.classList.add("sending");
  toast("Refining it...");
  try {
    const result = await api(`/api/cards/${card.id}/comment`, { method: "POST", body: JSON.stringify({ comment }) });
    if (result.game) state.game = result.game;
    haptic("success");
    state.commentOpenId = "";
    removeLocal(card.id);
    scheduleRefresh();
  } catch (error) {
    form.classList.remove("sending");
    haptic("error");
    toast(error.message);
  }
}

async function sendContext(event) {
  event.preventDefault();
  const comment = els.input.value.trim();
  if (!comment) return;
  const card = currentCard();
  els.sheet.close();
  toast("Refining it...");
  try {
    if (card?.id) {
      const result = await api(`/api/cards/${card.id}/comment`, { method: "POST", body: JSON.stringify({ comment }) });
      if (result.game) state.game = result.game;
    } else if (state.activeGoalId.startsWith("topic:")) {
      await api(`/api/topics/${state.activeGoalId.slice("topic:".length)}/context`, { method: "POST", body: JSON.stringify({ comment }) });
    } else if (state.activeGoalId !== "all") {
      await api(`/api/goals/${state.activeGoalId}/context`, { method: "POST", body: JSON.stringify({ comment }) });
    }
    els.input.value = "";
    if (card?.id) removeLocal(card.id);
    scheduleRefresh();
  } catch (error) {
    els.input.value = comment;
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

async function startAutopilot() {
  try {
    if (state.activeGoalId.startsWith("topic:")) {
      await api(`/api/topics/${state.activeGoalId.slice("topic:".length)}/autopilot`, { method: "POST", body: "{}" });
    } else if (state.activeGoalId !== "all") {
      await api(`/api/goals/${state.activeGoalId}/autopilot`, { method: "POST", body: "{}" });
    } else {
      await api("/api/autopilot", { method: "POST", body: "{}" });
    }
    toast("Goal running.");
    scheduleRefresh();
  } catch (error) {
    toast(error.message);
  }
}

function openWorkSheet() {
  els.workSheet.showModal();
}

function scheduleRefresh() {
  [1200, 4200, 9000].forEach((delay) => {
    setTimeout(() => refresh({ resetToTop: false }).catch((error) => toast(error.message)), delay);
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

function railSvg(collapsed) {
  const path = collapsed ? "m9 6 6 6-6 6" : "m15 6-6 6 6 6";
  return `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="${path}" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

els.context.addEventListener("click", openContext);
els.autopilot.addEventListener("click", openWorkSheet);
els.more.addEventListener("click", generateMore);
els.skipAction.addEventListener("click", () => dismissCurrentCard());
els.startAction.addEventListener("click", () => startCurrentCard());
els.newGoal.addEventListener("click", () => {
  els.goalSheet.showModal();
  els.goalInput.focus({ preventScroll: true });
});
els.mobileGoalButton.addEventListener("click", () => {
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
document.querySelector("[data-close-work]").addEventListener("click", () => els.workSheet.close());
document.querySelector("[data-close-lanes]").addEventListener("click", () => els.laneSheet.close());
els.workForm.addEventListener("submit", (event) => {
  event.preventDefault();
  els.workSheet.close();
  startAutopilot();
});
els.sheet.addEventListener("click", (event) => {
  if (event.target === els.sheet) els.sheet.close();
});
els.goalSheet.addEventListener("click", (event) => {
  if (event.target === els.goalSheet) els.goalSheet.close();
});
els.workSheet.addEventListener("click", (event) => {
  if (event.target === els.workSheet) els.workSheet.close();
});
els.laneSheet.addEventListener("click", (event) => {
  if (event.target === els.laneSheet) els.laneSheet.close();
});
document.querySelectorAll("[data-goal-example]").forEach((button) => {
  button.addEventListener("click", () => {
    els.goalInput.value = button.dataset.goalExample || "";
    els.goalForm.requestSubmit();
  });
});
attachSpeech();

async function refresh(options = {}) {
  const [goals, topics, cards, stats, game, activity, me] = await Promise.all([
    api("/api/goals"),
    api("/api/topics"),
    api("/api/cards"),
    api("/api/stats"),
    api("/api/game-state"),
    api("/api/activity"),
    api("/api/me"),
  ]);
  state.goals = goals.goals || [];
  state.topics = topics.topics || [];
  state.cards = cards.cards || [];
  state.stats = stats.stats || {};
  state.game = game.game || null;
  state.activity = activity.activity || [];
  state.me = me || { settings: {} };
  if (options.resetToTop) state.index = 0;
  render();
}

try {
  await refresh();
  setInterval(() => {
    refresh().catch((error) => toast(error.message));
  }, 10000);
} catch (error) {
  const missingTelegram = !initData || /initData|signature|expired|owner/i.test(error.message);
  [els.more, els.autopilot, els.skipAction, els.startAction, els.context, els.newGoal].forEach((button) => {
    if (button) button.disabled = true;
  });
  els.deck.innerHTML = `
    <article class="empty">
      <strong>${missingTelegram ? "Open from Telegram" : "Could not load cards"}</strong>
      <p>${escapeHtml(missingTelegram ? "Reopen the Mini App from the Telegram bot so the session can be verified." : error.message)}</p>
    </article>
  `;
}
