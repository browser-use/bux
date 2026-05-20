const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

try {
  tg?.setHeaderColor?.("#f6f1e8");
  tg?.setBackgroundColor?.("#f6f1e8");
  tg?.setBottomBarColor?.("#f6f1e8");
} catch {
  // Telegram client capabilities vary by version.
}

const params = new URLSearchParams(window.location.search);
if (params.get("dev") === "1") localStorage.buxMiniAppDev = "1";
const initData = tg?.initData || (localStorage.buxMiniAppDev === "1" ? "dev" : "");
const app = document.querySelector("#app");
const toastEl = document.querySelector("#toast");
const STORE_KEY = "buxMiniAppGoalGameLab:v1";
const CONCEPT_COUNT = 10;

const CONCEPTS = [
  ["kingdom-map", "Kingdom Map", "quest", "A world map where every AI-prepared action expands one goal realm.", "Conquer one realm", "XP, rank, next realm", "Tap a quest node"],
  ["boss-swipe", "Boss Swipe", "deck", "A Tinder-style boss fight where one high-leverage decision clears the queue.", "Swipe or tap a move", "Combo meter and boss damage", "Right = do, left = skip"],
  ["royal-coach", "Royal Coach", "coach", "A calm coach that turns fuzzy goals into the next concrete approval.", "Choose today's move", "Streak save and confidence", "Tap the coach card"],
  ["crown-skill-tree", "Crown Skill Tree", "roadmap", "Approvals unlock powers like distribution, customers, health, and shipping.", "Unlock a branch", "New abilities and source scans", "Tap a branch"],
  ["momentum-rings", "Momentum Rings", "habit", "Close bright rings for the goals that matter today and see the next unlock.", "Close one ring", "Visible completion and new cards", "Tap to close"],
  ["command-throne", "Command Throne", "command", "A Telegram-native cockpit for open cards, live agent work, level, and daily focus.", "Clear the queue", "Level-up and fewer stale cards", "Tap a command"],
  ["treasure-forge", "Treasure Forge", "arcade", "A reward reveal where the treasure is real agent work already prepared.", "Reveal a reward", "Useful card, not fake coins", "Tap to claim"],
  ["one-tap-crown", "One Tap Crown", "onebutton", "The lowest-friction version: one giant approval when the agent did enough work.", "Approve the win", "Instant progress and clean feed", "One thumb tap"],
  ["season-league", "Season League", "sports", "A weekly league board for approvals, skipped cards, goal progress, and milestones.", "Win the week", "Milestones and progress tiers", "Tap a match"],
  ["crown-mission", "Crown Mission", "mission", "A cinematic single-mission card with payoff, boundary, and reward in one glance.", "Launch the mission", "A new agent session or final result", "Tap launch"],
].map(([slug, name, layout, line, loop, reward, gesture], index) => ({
  id: index + 1,
  slug,
  name,
  layout,
  line,
  loop,
  reward,
  gesture,
  accent: palette(index),
}));

const DEMO_CARDS = [
  {
    id: "goal-startup",
    title: "Draft three Product Hunt launch replies for today’s top comments",
    why: "The agent can prepare warm, founder-style replies now and wait before posting anything publicly.",
    source: "miniapp-game:startup",
    source_label: "Startup goal",
    importance: "high",
    buttons: ["Draft replies", "Find comments", "Make variants"],
    blocks: [
      { title: "Win condition", body: "Approve three useful moves today that move distribution, activation, or trust." },
      { title: "Agent work", body: "Find the best public comments, draft replies in your voice, and ask again before anything is posted." },
      { title: "Reward", body: "+150 XP, streak protection, and a sharper next batch." },
    ],
    category: "growth",
  },
  {
    id: "goal-inbox",
    title: "Clear the people waiting on you without opening inboxes",
    why: "The agent finds named asks, drafts replies, and turns your inbox into approve-or-skip cards.",
    source: "miniapp-game:inbox",
    source_label: "Inbox quest",
    importance: "high",
    buttons: ["Draft replies", "Only VIPs", "Run every 30 min"],
    blocks: [
      { title: "What counts", body: "A person, thread, exact ask, and a draft that can be sent after approval." },
      { title: "No slop", body: "Spam and FYIs are hidden. Generic monitor cards do not score." },
      { title: "Reward", body: "+140 XP per useful reply approval." },
    ],
    category: "inbox",
  },
  {
    id: "goal-slack",
    title: "Unblock one teammate before the thread goes stale",
    why: "Fast internal replies keep projects moving and preserve your founder leverage.",
    source: "miniapp-game:slack",
    source_label: "People quest",
    importance: "high",
    buttons: ["Find blocker", "Draft answer", "Daily people radar"],
    blocks: [
      { title: "Signal", body: "DMs, mentions, repeated pings, and threads where your answer changes the outcome." },
      { title: "Approval boundary", body: "Agency drafts the answer and waits before anything visible is sent." },
      { title: "Combo", body: "Two people unblocked in a day extends the relationship streak." },
    ],
    category: "people",
  },
  {
    id: "goal-github",
    title: "Ship the safest PR instead of staring at the queue",
    why: "The agent checks CI, risky files, and missing tests, then gives you one merge/fix decision.",
    source: "miniapp-game:github",
    source_label: "Code quest",
    importance: "med",
    buttons: ["Pick safest PR", "Review diff", "Watch until green"],
    blocks: [
      { title: "Scoring", body: "High confidence, low blast radius, and user-visible value score highest." },
      { title: "Agent work", body: "Inspect the diff and logs before asking for the merge tap." },
      { title: "Reward", body: "+120 ship XP, plus a reliability badge when tests pass." },
    ],
    category: "code",
  },
  {
    id: "goal-distribution",
    title: "Find one warm distribution opening and draft the move",
    why: "Growth becomes a game when every card is a named person, post, community, or launch window.",
    source: "miniapp-game:growth",
    source_label: "Growth quest",
    importance: "high",
    buttons: ["Find opening", "Draft outreach", "Make launch list"],
    blocks: [
      { title: "Good card", body: "Names the platform, exact object, and the action that helps your goal." },
      { title: "Bad card", body: "Generic 'monitor growth channels' suggestions score zero." },
      { title: "Reward", body: "+160 XP when the card is specific enough to approve in 2 seconds." },
    ],
    category: "growth",
  },
  {
    id: "goal-customer",
    title: "Save one customer risk before it becomes churn",
    why: "Agency watches complaints, unresolved bugs, silence, and usage drops, then drafts the safest next step.",
    source: "miniapp-game:customers",
    source_label: "Customer quest",
    importance: "high",
    buttons: ["Find risk", "Draft save plan", "Start radar"],
    blocks: [
      { title: "Risk signs", body: "Complaint language, unresolved incidents, billing pain, or leadership escalation." },
      { title: "Output", body: "Customer, symptom, suggested contact, and the draft or fix already prepared." },
      { title: "Reward", body: "+200 XP because retention beats vanity activity." },
    ],
    category: "customer",
  },
  {
    id: "goal-health",
    title: "Protect your energy while the agent handles the noise",
    why: "Not every win is external. The agent can batch low-value interruptions and surface only true blockers.",
    source: "miniapp-game:focus",
    source_label: "Focus quest",
    importance: "med",
    buttons: ["Start focus block", "Batch replies", "Only urgent"],
    blocks: [
      { title: "Quiet mode", body: "Watch connected surfaces without interrupting unless a named blocker appears." },
      { title: "End state", body: "A recap of what was ignored, drafted, or needs approval." },
      { title: "Reward", body: "+90 focus XP and streak credit for staying healthy while shipping." },
    ],
    category: "focus",
  },
  {
    id: "goal-meeting",
    title: "Walk into the next meeting with a prep packet ready",
    why: "The agent can fetch the people, history, docs, decisions, and likely objections before you join.",
    source: "miniapp-game:calendar",
    source_label: "Calendar quest",
    importance: "med",
    buttons: ["Prep next meeting", "Find last context", "Daily agenda"],
    blocks: [
      { title: "Packet", body: "Attendees, prior threads, open decisions, and three questions that change the outcome." },
      { title: "Afterward", body: "Draft the recap as the follow-up card." },
      { title: "Reward", body: "+110 XP when you approve a packet before the meeting starts." },
    ],
    category: "calendar",
  },
  {
    id: "goal-launch",
    title: "Run the launch loop from post to replies to follow-up",
    why: "Launch work becomes a season: copy, assets, posting approval, monitoring, and the next response card.",
    source: "miniapp-game:launch",
    source_label: "Launch season",
    importance: "high",
    buttons: ["Plan launch", "Draft copy", "Watch reactions"],
    blocks: [
      { title: "Season pass", body: "Every accepted card unlocks the next launch step automatically." },
      { title: "Boundary", body: "Publishing and replies wait for approval; research and drafts happen autonomously." },
      { title: "Reward", body: "+220 XP for visible launch momentum." },
    ],
    category: "launch",
  },
  {
    id: "goal-daily-brief",
    title: "Start the 9am command brief and keep the feed alive",
    why: "If there are no pending cards, Agency should create useful goal-grounded cards or ask one goal question.",
    source: "miniapp-game:brief",
    source_label: "Daily brief",
    importance: "med",
    buttons: ["Set 9am brief", "Show sample", "Pick sources"],
    blocks: [
      { title: "Sections", body: "Money, users, bugs, shipping, people, and risks." },
      { title: "Rule", body: "Empty feed is not a steady state. Generate useful cards or goal-lock questions." },
      { title: "Reward", body: "+100 XP for keeping the system alive without spam." },
    ],
    category: "ops",
  },
];

const CATEGORY_META = {
  inbox: { label: "Inbox", short: "IN", color: "#ff5a7a" },
  people: { label: "People", short: "DM", color: "#19c37d" },
  code: { label: "Code", short: "PR", color: "#2bb6ff" },
  growth: { label: "Growth", short: "GR", color: "#f7a72b" },
  customer: { label: "Customers", short: "CU", color: "#ff7a1a" },
  calendar: { label: "Calendar", short: "CA", color: "#9b7cff" },
  ops: { label: "Ops", short: "OP", color: "#19b7a8" },
  quality: { label: "Quality", short: "QA", color: "#ef4444" },
  focus: { label: "Focus", short: "FO", color: "#64748b" },
  launch: { label: "Launch", short: "LA", color: "#e84aa7" },
};

const state = {
  cards: [],
  goals: [],
  topics: [],
  stats: {},
  game: null,
  activity: [],
  me: { settings: {} },
  conceptId: conceptIdFromPath(),
  focusCardId: null,
  selected: {},
  apiOnline: false,
  apiError: "",
  local: loadLocalState(),
};
const scene = initConceptScene();

function palette(index) {
  return [
    "#ff5a7a",
    "#111827",
    "#f59e0b",
    "#22c55e",
    "#38bdf8",
    "#8b5cf6",
    "#f97316",
    "#14b8a6",
    "#ef4444",
    "#eab308",
  ][index % 10];
}

function conceptIdFromPath() {
  const path = window.location.pathname.replace(/\/+$/, "");
  const match = path.match(/(?:mini[-_]?app|miniapp)[-/]?(\d{1,2})$/i);
  if (!match) return 0;
  const value = Number(match[1]);
  return value >= 1 && value <= CONCEPT_COUNT ? value : 0;
}

function conceptPath(id) {
  const suffix = params.get("dev") === "1" || localStorage.buxMiniAppDev === "1" ? "?dev=1" : "";
  return `/mini-app-${id}${suffix}`;
}

function hubPath() {
  return `/mini-apps${params.get("dev") === "1" || localStorage.buxMiniAppDev === "1" ? "?dev=1" : ""}`;
}

function loadLocalState() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    return {
      decisions: parsed.decisions || {},
      cards: Array.isArray(parsed.cards) ? parsed.cards : [],
      notes: parsed.notes || {},
      points: Number(parsed.points || 0),
      combo: Number(parsed.combo || 0),
      streak: Number(parsed.streak || 0),
      lastReward: parsed.lastReward || "",
    };
  } catch {
    return { decisions: {}, cards: [], notes: {}, points: 0, combo: 0, streak: 0, lastReward: "" };
  }
}

function saveLocalState() {
  localStorage.setItem(STORE_KEY, JSON.stringify(state.local));
}

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

async function refresh() {
  try {
    const [goals, topics, cards, stats, game, activity, me] = await Promise.all([
      api("/api/goals"),
      api("/api/topics"),
      api("/api/cards"),
      api("/api/stats"),
      api("/api/game-state"),
      api("/api/activity"),
      api("/api/me"),
    ]);
    state.apiOnline = true;
    state.apiError = "";
    state.goals = goals.goals || [];
    state.topics = topics.topics || [];
    state.stats = stats.stats || {};
    state.game = game.game || null;
    state.activity = activity.activity || [];
    state.me = me || { settings: {} };
    state.cards = mergeCards(cards.cards || []);
  } catch (error) {
    state.apiOnline = false;
    state.apiError = error.message || "API unavailable";
    state.goals = [];
    state.topics = [];
    state.stats = { open: 10, done: Object.keys(state.local.decisions).length };
    state.game = null;
    state.activity = localActivity();
    state.cards = mergeCards([]);
  }
  if (!state.focusCardId || !state.cards.some((card) => String(card.id) === String(state.focusCardId))) {
    state.focusCardId = activeCards(1)[0]?.id || state.cards[0]?.id || null;
  }
  render();
}

function mergeCards(apiCards) {
  const normalized = apiCards.map((card) => normalizeCard(card, false));
  const existingSources = new Set(normalized.map((card) => card.source));
  const generated = state.local.cards.map((card) => normalizeCard(card, true));
  const generatedIds = new Set(generated.map((card) => String(card.id)));
  const needed = Math.max(0, 10 - normalized.length - generated.length);
  const demos = DEMO_CARDS
    .filter((card) => !existingSources.has(card.source) && !generatedIds.has(String(card.id)))
    .slice(0, needed)
    .map((card) => normalizeCard(card, true));
  return [...normalized, ...generated, ...demos];
}

function normalizeCard(raw, demo) {
  const fallback = DEMO_CARDS.find((item) => item.source === raw.source) || {};
  return {
    ...raw,
    id: raw.id,
    title: raw.title || fallback.title || "Untitled action",
    why: raw.why || raw.description || fallback.why || "This is ready for a one-tap decision.",
    source: raw.source || fallback.source || "miniapp-demo",
    source_label: raw.source_label || fallback.source_label || raw.topic_title || "bux",
    source_url: raw.source_url || "",
    buttons: ensureButtons(raw.buttons || fallback.buttons),
    blocks: Array.isArray(raw.blocks) && raw.blocks.length ? raw.blocks : fallback.blocks || [],
    category: raw.category || fallback.category || inferCategory(raw),
    importance: raw.importance || fallback.importance || "med",
    demo,
    visual: conceptVisual(raw.visual || fallback.visual, raw.category || fallback.category || "ops"),
    created_at: raw.created_at || Math.round(Date.now() / 1000),
  };
}

function conceptVisual(visual, category) {
  if (visual?.src && ["image", "video"].includes(visual.kind)) return visual;
  return demoVisual(category);
}

function demoVisual(category) {
  const meta = CATEGORY_META[category] || CATEGORY_META.ops;
  const color = meta.color || "#ff5a7a";
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="900" height="1200" viewBox="0 0 900 1200">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="${color}"/>
          <stop offset=".58" stop-color="#111827"/>
          <stop offset="1" stop-color="#14b8a6"/>
        </linearGradient>
      </defs>
      <rect width="900" height="1200" rx="64" fill="url(#g)"/>
      <path d="M90 850 C220 640 430 560 720 612 L810 1120 H90 Z" fill="#fff" opacity=".14"/>
      <path d="M180 240 L450 110 L720 240 L648 640 L252 640 Z" fill="#fff" opacity=".20"/>
      <path d="M292 636 L450 366 L608 636 Z" fill="#fff" opacity=".30"/>
      <circle cx="692" cy="250" r="78" fill="#fff" opacity=".22"/>
      <circle cx="220" cy="760" r="118" fill="#fff" opacity=".12"/>
    </svg>
  `;
  return { kind: "image", src: `data:image/svg+xml;base64,${btoa(svg.replace(/\n\s+/g, ""))}` };
}

function ensureButtons(buttons) {
  const labels = (Array.isArray(buttons) ? buttons : []).map((item) => String(item || "").trim()).filter(Boolean);
  return labels.length ? labels.slice(0, 4) : ["Start"];
}

function inferCategory(card) {
  const text = `${card.source || ""} ${card.source_label || ""} ${card.title || ""}`.toLowerCase();
  if (text.includes("gmail") || text.includes("email") || text.includes("inbox")) return "inbox";
  if (text.includes("slack") || text.includes("dm") || text.includes("people")) return "people";
  if (text.includes("github") || text.includes("pr") || text.includes("ci") || text.includes("repo")) return "code";
  if (text.includes("customer") || text.includes("churn") || text.includes("lead")) return "customer";
  if (text.includes("calendar") || text.includes("meeting")) return "calendar";
  if (text.includes("quality") || text.includes("bug") || text.includes("monitor")) return "quality";
  if (text.includes("focus") || text.includes("deep")) return "focus";
  if (text.includes("launch")) return "launch";
  if (text.includes("growth") || text.includes("distribution")) return "growth";
  return "ops";
}

function render() {
  const concept = CONCEPTS.find((item) => item.id === state.conceptId);
  if (!concept) {
    document.body.className = "hub";
    app.className = "concept-shell hub-mode";
    app.innerHTML = renderHub();
    scene?.set?.({ color: [1, 0.35, 0.48], mode: 0, progress: conceptProgress().pct / 100 });
    return;
  }
  document.body.className = `concept-page layout-${concept.layout} concept-${concept.id}`;
  app.className = "concept-shell";
  app.innerHTML = `
    ${renderLabNav(concept)}
    ${renderConcept(concept)}
  `;
  scene?.set?.({ color: hexToRgb(concept.accent), mode: concept.id, progress: conceptProgress().pct / 100 });
}

function initConceptScene() {
  const canvas = document.querySelector("#conceptScene");
  const gl = canvas?.getContext?.("webgl", { alpha: true, antialias: true, preserveDrawingBuffer: true });
  if (!canvas || !gl) return null;
  function compileShader(type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      console.warn("Agency concept shader failed", gl.getShaderInfoLog(shader));
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  }
  const vertex = compileShader(gl.VERTEX_SHADER, `
    attribute vec2 a_pos;
    attribute vec4 a_color;
    varying vec4 v_color;
    void main() {
      gl_Position = vec4(a_pos, 0.0, 1.0);
      v_color = a_color;
    }
  `);
  const fragment = compileShader(gl.FRAGMENT_SHADER, `
    precision mediump float;
    varying vec4 v_color;
    void main() { gl_FragColor = v_color; }
  `);
  if (!vertex || !fragment) return null;
  const program = gl.createProgram();
  gl.attachShader(program, vertex);
  gl.attachShader(program, fragment);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    console.warn("Agency concept program failed", gl.getProgramInfoLog(program));
    return null;
  }
  gl.useProgram(program);
  const buffer = gl.createBuffer();
  const pos = gl.getAttribLocation(program, "a_pos");
  const color = gl.getAttribLocation(program, "a_color");
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.enableVertexAttribArray(pos);
  gl.vertexAttribPointer(pos, 2, gl.FLOAT, false, 24, 0);
  gl.enableVertexAttribArray(color);
  gl.vertexAttribPointer(color, 4, gl.FLOAT, false, 24, 8);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  let settings = { color: [1, 0.35, 0.48], mode: 0, progress: 0 };

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const width = Math.max(1, Math.floor(rect.width * ratio));
    const height = Math.max(1, Math.floor(rect.height * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    gl.viewport(0, 0, width, height);
  }

  if (window.ResizeObserver) {
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
  } else {
    window.addEventListener("resize", resize, { passive: true });
  }
  resize();

  let rafId = 0;
  let timerId = 0;
  let running = true;

  function scheduleDraw(force = false) {
    if (!running || rafId || timerId) return;
    if (document.hidden) {
      if (!force) return;
      timerId = window.setTimeout(() => {
        timerId = 0;
        draw(performance.now());
      }, 16);
      return;
    }
    rafId = requestAnimationFrame(draw);
  }

  canvas.addEventListener("webglcontextlost", (event) => {
    event.preventDefault();
    running = false;
    cancelAnimationFrame(rafId);
    clearTimeout(timerId);
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && running && !rafId) scheduleDraw();
  });

  function push(vertices, x, y, c) {
    vertices.push(x, y, c[0], c[1], c[2], c[3]);
  }

  function quad(vertices, x, y, w, h, c) {
    push(vertices, x - w, y - h, c);
    push(vertices, x + w, y - h, c);
    push(vertices, x + w, y + h, c);
    push(vertices, x - w, y - h, c);
    push(vertices, x + w, y + h, c);
    push(vertices, x - w, y + h, c);
  }

  function draw(time) {
    resize();
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const vertices = [];
    const c = settings.color;
    const progress = Math.max(0.08, settings.progress || 0);
    for (let i = 0; i < 28; i += 1) {
      const row = Math.floor(i / 7);
      const col = i % 7;
      const x = -0.86 + col * 0.29 + Math.sin(time * 0.0005 + i) * 0.012;
      const y = -0.62 + row * 0.36 + Math.cos(time * 0.0004 + i * 0.7) * 0.014;
      const active = i / 28 < progress;
      const size = 0.018 + (active ? 0.018 : 0.006) + (settings.mode % 4) * 0.002;
      quad(vertices, x, y, size, size, active ? [c[0], c[1], c[2], 0.30] : [0.10, 0.12, 0.16, 0.10]);
      if (col > 0) {
        quad(vertices, x - 0.145, y, 0.09, 0.004, [c[0], c[1], c[2], active ? 0.10 : 0.035]);
      }
    }
    const pulse = 0.12 + Math.sin(time * 0.0016) * 0.03;
    quad(vertices, 0.72, -0.62, pulse, pulse, [c[0], c[1], c[2], 0.13]);
    quad(vertices, 0.72, -0.62, pulse * 0.52, pulse * 0.52, [1, 1, 1, 0.16]);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(vertices), gl.DYNAMIC_DRAW);
    gl.drawArrays(gl.TRIANGLES, 0, vertices.length / 6);
    rafId = 0;
    scheduleDraw();
  }

  scheduleDraw(true);
  return {
    set(next) {
      settings = { ...settings, ...next };
      scheduleDraw(true);
    },
  };
}

function hexToRgb(hex) {
  const value = String(hex || "#ff5a7a").replace("#", "");
  const int = Number.parseInt(value.length === 3 ? value.split("").map((x) => x + x).join("") : value, 16);
  return [((int >> 16) & 255) / 255, ((int >> 8) & 255) / 255, (int & 255) / 255];
}

function renderHub() {
  const progress = conceptProgress();
  return `
    <section class="hub-hero">
      <p class="eyebrow">agency prototypes</p>
      <h1>King of Life lab.</h1>
      <p>Ten focused Mini App prototypes for turning AI-suggested cards into goal realms, XP, streaks, levels, and one-tap wins.</p>
      <div class="hub-stats">
        <span>${escapeHtml(progress.rankName)}</span>
        <span>${progress.xp} XP</span>
        <span>${state.cards.length} cards loaded</span>
        <span>${Object.keys(groupByCategory()).length} source groups</span>
        <span>${state.apiOnline ? "live database" : "demo fallback"}</span>
      </div>
    </section>
    <section class="concept-grid">
      ${CONCEPTS.map((concept) => `
        <a class="concept-tile tile-${concept.layout}" style="--accent:${concept.accent}" href="${conceptPath(concept.id)}">
          <span>Version ${concept.id}</span>
          <strong>${escapeHtml(concept.name)}</strong>
          <p>${escapeHtml(concept.line)}</p>
        </a>
      `).join("")}
    </section>
  `;
}

function renderLabNav(concept) {
  const prev = concept.id === 1 ? CONCEPT_COUNT : concept.id - 1;
  const next = concept.id === CONCEPT_COUNT ? 1 : concept.id + 1;
  return `
    <nav class="lab-nav ${concept.id === 2 ? "boss-lab-nav" : ""}" aria-label="Mini App concepts">
      <a class="lab-home" href="${hubPath()}">All</a>
      <a href="${conceptPath(prev)}">Prev</a>
      <span>${concept.id} / ${CONCEPT_COUNT}</span>
      <a href="${conceptPath(next)}">Next</a>
      <small>${escapeHtml(concept.layout)}</small>
    </nav>
  `;
}

function renderConcept(concept) {
  const cards = activeCards(18);
  const card = focusedCard(cards);
  const ordered = prioritizeCard(cards, card);
  const renderer = LAYOUTS[concept.layout] || renderGeneric;
  const showGlobalStats = concept.id === 6;
  const showPreview = [1, 4].includes(concept.id);
  return `
    <section class="concept-screen" style="--accent:${concept.accent}">
      <header class="concept-title">
        <span>Version ${concept.id}</span>
        <h1>${escapeHtml(concept.name)}</h1>
        <p>${escapeHtml(concept.line)}</p>
      </header>
      ${showGlobalStats ? renderGameStats(concept) : ""}
      ${showPreview ? renderPreviewStrip(ordered, card) : ""}
      ${renderer(concept, ordered, card)}
    </section>
  `;
}

function renderGameLoop(concept, card) {
  const progress = conceptProgress();
  const combo = Number(state.local.combo || 0);
  const streak = Number(state.local.streak || 0);
  const reward = state.local.lastReward || concept.reward;
  return `
    <section class="game-loop-panel" style="--accent:${concept.accent}">
      <div class="loop-orbit" aria-hidden="true"><span></span><span></span><span></span></div>
      <div class="loop-copy">
        <span>Core loop</span>
        <strong>${escapeHtml(concept.loop)}</strong>
        <p>${escapeHtml(clip(card.title, 96))}</p>
      </div>
      <div class="loop-pill"><span>Gesture</span><strong>${escapeHtml(concept.gesture)}</strong></div>
      <div class="loop-pill"><span>Reward</span><strong>${escapeHtml(reward)}</strong></div>
      <div class="loop-pill live"><span>${escapeHtml(progress.rankName)}</span><strong>${combo} combo · ${streak} streak</strong></div>
    </section>
  `;
}

function renderGameStats(concept) {
  const progress = conceptProgress();
  const accepted = Object.values(state.local.decisions).filter((item) => item.status === "started").length;
  const combo = Number(state.local.combo || 0);
  return `
    <section class="game-stats" style="--accent:${concept.accent}">
      <div><span>Rank</span><strong>${escapeHtml(progress.rankName)}</strong></div>
      <div><span>XP</span><strong>${progress.xp}</strong></div>
      <div><span>Accepted</span><strong>${progress.done || accepted}</strong></div>
      <div><span>Combo</span><strong>${combo}</strong></div>
      <i style="--pct:${progress.pct}%"></i>
    </section>
  `;
}

function conceptProgress() {
  if (state.game?.rank) {
    return {
      rankName: state.game.rank.name || "Farmer",
      xp: Number(state.game.points || 0),
      done: Number(state.game.stats?.done || 0),
      pct: Number(state.game.progress || 0),
    };
  }
  const xp = Number(state.local.points || 0);
  const level = Math.max(1, Math.floor(xp / 500) + 1);
  return {
    rankName: level >= 8 ? "King of Life" : ["Farmer", "Builder", "Merchant", "Strategist", "Regent"][Math.min(4, level - 1)],
    xp,
    done: Object.values(state.local.decisions).filter((item) => item.status === "started").length,
    pct: Math.min(100, Math.round((xp % 500) / 5)),
  };
}

function renderPreviewStrip(cards, card) {
  const visible = cards.slice(0, 10);
  return `
    <nav class="card-preview" aria-label="Card previews">
      <button class="card-step" data-action="focus-prev" type="button">Prev card</button>
      <div class="preview-track">
        ${visible.map((item, index) => `
          <button class="${String(item.id) === String(card.id) ? "active" : ""}" data-action="focus" data-card-id="${item.id}" type="button" aria-pressed="${String(item.id) === String(card.id)}">
            <span>${index + 1}</span>
            <strong>${escapeHtml(clip(item.title, 42))}</strong>
          </button>
        `).join("")}
      </div>
      <button class="card-step" data-action="focus-next" type="button">Next card</button>
    </nav>
  `;
}

const LAYOUTS = {
  reel: renderReel,
  overview: renderOverviewReel,
  timeline: renderTimeline,
  stories: renderStories,
  deck: renderDeck,
  board: renderBoard,
  wallet: renderWallet,
  chat: renderChat,
  kanban: renderKanban,
  mail: renderMail,
  command: renderCommand,
  magazine: renderMagazine,
  gallery: renderGallery,
  checklist: renderChecklist,
  calendar: renderCalendar,
  arcade: renderArcade,
  split: renderSplit,
  stack: renderStack,
  voice: renderVoice,
  compact: renderCompact,
  table: renderTable,
  coach: renderCoach,
  doc: renderDoc,
  forum: renderForum,
  linear: renderLinear,
  playlist: renderPlaylist,
  quest: renderQuest,
  shop: renderShop,
  brief: renderBrief,
  focus: renderFocus,
  broadcast: renderBroadcast,
  crm: renderCrm,
  terminal: renderTerminal,
  comic: renderComic,
  roadmap: renderRoadmap,
  habit: renderHabit,
  market: renderMarket,
  onebutton: renderOneButton,
  draft: renderDraftStudio,
  team: renderTeam,
  shelves: renderShelves,
  receipt: renderReceipt,
  auction: renderAuction,
  launch: renderLaunch,
  letter: renderLetter,
  mission: renderMission,
  sports: renderSports,
  proof: renderProof,
  splitdeck: renderSplitDeck,
  tiles: renderTiles,
  concierge: renderConcierge,
};

function renderReel(concept, cards) {
  return `
    <div class="reel-stream">
      ${cards.slice(0, 5).map((card) => `
        <article class="phone-reel ${hasRealVisual(card) ? "has-media-card" : "no-visual-card"} concept-card" data-card-id="${card.id}">
          ${renderVisual(card, "reel-visual")}
          <div class="reel-copy">
            ${renderMeta(card)}
            <h2>${escapeHtml(clip(card.title, 82))}</h2>
            <p>${escapeHtml(clip(card.why, 150))}</p>
          </div>
          ${renderActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderOverviewReel(concept, cards, card) {
  const selected = focusedCard(cards);
  return `
    <div class="overview-reel">
      <section class="overview-list" aria-label="Open cards">
        ${cards.slice(0, 10).map((item, index) => `
          <button class="${String(item.id) === String(selected.id) ? "active" : ""}" data-action="focus" data-card-id="${item.id}" type="button">
            <span>${index + 1}</span>
            <strong>${escapeHtml(clip(item.title, 64))}</strong>
            <small>${escapeHtml(sourceName(item))}</small>
          </button>
        `).join("")}
      </section>
      <div class="reel-stream overview-detail">
        <article class="phone-reel ${hasRealVisual(selected) ? "has-media-card" : "no-visual-card"} concept-card" data-card-id="${selected.id}">
          ${renderVisual(selected, "reel-visual")}
          <div class="reel-copy">
            ${renderMeta(selected)}
            <h2>${escapeHtml(clip(selected.title, 82))}</h2>
            <p>${escapeHtml(clip(selected.why, 150))}</p>
          </div>
          ${renderActions(selected)}
        </article>
      </div>
    </div>
  `;
}

function renderTimeline(concept, cards) {
  return `
    <div class="timeline-shell">
      ${cards.slice(0, 8).map((card) => `
        <article class="social-post concept-card" data-card-id="${card.id}">
          <button class="avatar" data-action="focus" data-card-id="${card.id}">${escapeHtml(categoryMeta(card).short)}</button>
          <div>
            <header><strong>${escapeHtml(sourceName(card))}</strong><span>@${escapeHtml(card.category)}</span></header>
            <h2>${escapeHtml(clip(card.title, 92))}</h2>
            <p>${escapeHtml(clip(card.why, 160))}</p>
            ${renderVisual(card, "post-media")}
            ${renderActions(card, "inline-actions")}
          </div>
        </article>
      `).join("")}
    </div>
  `;
}

function renderStories(concept, cards, card) {
  return `
    <div class="story-layout">
      <div class="story-strip">
        ${cards.slice(0, 8).map((item) => `
          <button class="${String(item.id) === String(card.id) ? "active" : ""}" data-action="focus" data-card-id="${item.id}">
            ${renderVisual(item, "story-thumb")}
            <span>${escapeHtml(sourceName(item))}</span>
          </button>
        `).join("")}
      </div>
      <article class="story-stage concept-card" data-card-id="${card.id}">
        ${renderVisual(card, "story-visual")}
        <section>
          ${renderMeta(card)}
          <h2>${escapeHtml(clip(card.title, 92))}</h2>
          <p>${escapeHtml(clip(card.why, 170))}</p>
          ${renderActions(card)}
        </section>
      </article>
    </div>
  `;
}

function renderDeck(concept, cards, card) {
  const combo = Number(state.local.combo || 0);
  const stackCards = prioritizeCard(cards, card).slice(0, 3);
  const choice = buttonText(selectedRaw(card) || primaryButton(card));
  const blocks = Array.isArray(card.blocks) ? card.blocks.filter((block) => block?.body || block?.title).slice(0, 2) : [];
  const pending = activeCards(100).length;
  const approvedToday = Math.min(3, approvalsToday());
  const progressPct = Math.max(8, Math.min(100, (approvedToday / 3) * 100));
  return `
    <div class="deck-shell">
      <section class="boss-meter">
        <div><span>Daily objective</span><strong>${approvedToday}/3 approvals · ${pending} open</strong></div>
        <p><i style="--hp:${progressPct}%"></i></p>
        <em>Beat the day · ${combo} combo</em>
      </section>
      <div class="deck-swipe-cues" aria-hidden="true">
        <span>Skip</span>
        <strong>Swipe or choose</strong>
        <span>Do it</span>
      </div>
      ${stackCards.map((item, index) => `
        <article class="swipe-card concept-card ${hasRealVisual(item) ? "with-media" : "no-media-card"}" style="--stack:${index}; --z:${stackCards.length - index}; --shade:${index === 0 ? 1 : 0}" data-card-id="${item.id}" data-swipe-card="${index === 0 ? "1" : "0"}">
          <div class="swipe-intent left">Skip</div>
          <div class="swipe-intent right">Do it</div>
          <header class="boss-card-head">
            <div>
              <small>${escapeHtml(categoryMeta(item).label)}</small>
              <strong>${escapeHtml(sourceName(item))}</strong>
            </div>
            <b>+${pointsFor(item)} XP</b>
          </header>
          ${hasRealVisual(item) ? renderVisual(item, "deck-visual") : ""}
          <section class="boss-card-copy">
            <h2>${escapeHtml(clip(item.title, hasRealVisual(item) ? 84 : 112))}</h2>
            <p>${escapeHtml(clip(item.why, hasRealVisual(item) ? 150 : 230))}</p>
            ${index === 0 ? `<p class="lane-note">Choose a move below. Swipe right or tap Do it to start the selected move in Telegram.</p>` : ""}
            ${index === 0 && blocks.length ? `
              <div class="boss-proof">
                ${blocks.map((block) => `
                  <details>
                    <summary>${escapeHtml(block.title || "Detail")}</summary>
                    <p>${escapeHtml(clip(block.body || "", 220))}</p>
                  </details>
                `).join("")}
              </div>
            ` : ""}
          </section>
        </article>
      `).join("")}
      ${renderBossSwipeActions(card)}
    </div>
  `;
}

function renderBossSwipeActions(card) {
  const choices = agentChoices(card).slice(0, 4);
  return `
    <footer class="action-bar deck-actions">
      <div class="boss-action-label">Choose the move</div>
      <div class="boss-choice-grid">
        ${choices.map((button, index) => `
          <button class="${index === selectedIndex(card) ? "selected" : ""}" data-action="variant" data-card-id="${card.id}" data-index="${index}">
            <span>${index + 1}</span>${escapeHtml(button.text)}
          </button>
        `).join("")}
      </div>
      <div class="boss-utility-grid">
        <button class="skip-move" data-action="skip" data-card-id="${card.id}" aria-label="Skip">×</button>
        <button class="context-move" data-action="context" data-card-id="${card.id}">Comment</button>
        <button class="voice-move" data-action="voice" data-card-id="${card.id}" aria-label="Speak">Mic</button>
        <button class="launch-move" data-action="start" data-card-id="${card.id}" data-index="${selectedIndex(card)}">Do it</button>
      </div>
    </footer>
  `;
}

function renderBoard(concept, cards) {
  return `
    <div class="pin-board">
      ${cards.slice(0, 12).map((card, index) => `
        <article class="pin pin-${index % 5} concept-card" data-card-id="${card.id}">
          ${renderVisual(card, "pin-visual")}
          <h2>${escapeHtml(clip(card.title, 70))}</h2>
          <p>${escapeHtml(clip(card.why, 90))}</p>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderWallet(concept, cards) {
  return `
    <div class="wallet-stack">
      ${cards.slice(0, 6).map((card, index) => `
        <article class="wallet-pass concept-card" style="--i:${index}" data-card-id="${card.id}">
          <div>${renderMeta(card)}<h2>${escapeHtml(clip(card.title, 72))}</h2></div>
          <p>${escapeHtml(clip(card.why, 120))}</p>
          ${renderActions(card, "pass-actions")}
        </article>
      `).join("")}
    </div>
  `;
}

function renderChat(concept, cards, card) {
  return `
    <div class="chat-shell">
      <div class="chat-thread">
        ${cards.slice(0, 5).map((item, index) => `
          <article class="bubble ${index % 2 ? "user-bubble" : "agent-bubble"} concept-card" data-card-id="${item.id}">
            <span>${escapeHtml(sourceName(item))}</span>
            <h2>${escapeHtml(clip(item.title, 90))}</h2>
            <p>${escapeHtml(clip(item.why, 130))}</p>
          </article>
        `).join("")}
      </div>
      <div class="chat-composer">${renderMiniActions(card)}<button data-action="voice" data-card-id="${card.id}">Hold to explain</button></div>
    </div>
  `;
}

function renderKanban(concept, cards) {
  const groups = groupedCards(cards);
  return `
    <div class="kanban-board">
      ${groups.slice(0, 4).map(([key, items]) => `
        <section>
          <h2>${escapeHtml(categoryMeta({ category: key }).label)}</h2>
          ${items.slice(0, 4).map((card) => `
            <article class="lane-card concept-card" data-card-id="${card.id}">
              <strong>${escapeHtml(clip(card.title, 72))}</strong>
              <p>${escapeHtml(clip(card.why, 90))}</p>
              ${renderMiniActions(card)}
            </article>
          `).join("")}
        </section>
      `).join("")}
    </div>
  `;
}

function renderMail(concept, cards, card) {
  return `
    <div class="mail-app">
      <aside>
        ${cards.slice(0, 7).map((item) => `
          <button class="${String(item.id) === String(card.id) ? "active" : ""}" data-action="focus" data-card-id="${item.id}">
            <strong>${escapeHtml(clip(item.title, 48))}</strong>
            <span>${escapeHtml(sourceName(item))}</span>
          </button>
        `).join("")}
      </aside>
      <article class="mail-detail concept-card" data-card-id="${card.id}">
        ${renderMeta(card)}
        <h2>${escapeHtml(card.title)}</h2>
        <p>${escapeHtml(clip(card.why, 240))}</p>
        ${renderBlocks(card)}
        ${renderActions(card)}
      </article>
    </div>
  `;
}

function renderCommand(concept, cards, card) {
  return `
    <div class="command-grid">
      <section class="stat-card"><span>Open</span><strong>${activeCards(100).length}</strong><small>cards</small></section>
      <section class="stat-card"><span>XP</span><strong>${state.local.points || 0}</strong><small>earned</small></section>
      <section class="stat-card"><span>Sources</span><strong>${Object.keys(groupByCategory()).length}</strong><small>online</small></section>
      <article class="command-main concept-card" data-card-id="${card.id}">
        ${renderVisual(card, "command-visual")}
        <div>${renderMeta(card)}<h2>${escapeHtml(card.title)}</h2><p>${escapeHtml(clip(card.why, 180))}</p><div class="source-radar">${Object.entries(groupByCategory()).slice(0, 5).map(([key, items]) => `<span>${escapeHtml(categoryMeta({ category: key }).label)} ${items.length}</span>`).join("")}</div></div>
        ${renderActions(card)}
      </article>
      <section class="command-queue">
        ${cards.slice(1, 6).map((item) => `<button data-action="focus" data-card-id="${item.id}">${escapeHtml(clip(item.title, 58))}</button>`).join("")}
      </section>
    </div>
  `;
}

function renderMagazine(concept, cards, card) {
  return `
    <article class="magazine concept-card" data-card-id="${card.id}">
      ${renderVisual(card, "mag-cover")}
      <section>
        ${renderMeta(card)}
        <h2>${escapeHtml(card.title)}</h2>
        <p>${escapeHtml(clip(card.why, 220))}</p>
        ${renderActions(card)}
      </section>
    </article>
  `;
}

function renderGallery(concept, cards, card) {
  return `
    <div class="gallery-layout">
      <div class="gallery-grid">
        ${cards.slice(0, 9).map((item) => `
          <button data-action="focus" data-card-id="${item.id}">
            ${renderVisual(item, "gallery-thumb")}
          </button>
        `).join("")}
      </div>
      <article class="gallery-caption concept-card" data-card-id="${card.id}">
        <h2>${escapeHtml(clip(card.title, 70))}</h2>
        <p>${escapeHtml(clip(card.why, 130))}</p>
        ${renderActions(card)}
      </article>
    </div>
  `;
}

function renderChecklist(concept, cards) {
  return `
    <div class="checklist-shell">
      ${cards.slice(0, 9).map((card, index) => `
        <article class="check-row concept-card" data-card-id="${card.id}">
          <button data-action="start" data-card-id="${card.id}">${index + 1}</button>
          <div><strong>${escapeHtml(clip(card.title, 86))}</strong><p>${escapeHtml(clip(card.why, 100))}</p></div>
          <button data-action="skip" data-card-id="${card.id}">Skip</button>
        </article>
      `).join("")}
    </div>
  `;
}

function renderCalendar(concept, cards) {
  return `
    <div class="calendar-shell">
      ${cards.slice(0, 7).map((card, index) => `
        <article class="time-block concept-card" data-card-id="${card.id}">
          <time>${String(9 + index).padStart(2, "0")}:00</time>
          <div><h2>${escapeHtml(clip(card.title, 76))}</h2><p>${escapeHtml(clip(card.why, 100))}</p>${renderMiniActions(card)}</div>
        </article>
      `).join("")}
    </div>
  `;
}

function renderArcade(concept, cards, card) {
  const choices = cardButtons(card);
  return `
    <div class="arcade-shell">
      <div class="loot-marquee"><span>Rare useful work ready</span><strong>Claim one reward</strong></div>
      <div class="slot-window">
        ${choices.slice(0, 3).map((button) => `<span>${escapeHtml(button.text)}</span>`).join("")}
      </div>
      <article class="arcade-card concept-card" data-card-id="${card.id}">
        <div class="rarity-badge">${card.importance === "high" ? "Legendary" : "Useful"} drop</div>
        ${renderVisual(card, "arcade-visual")}
        <h2>${escapeHtml(card.title)}</h2>
        <p>${escapeHtml(clip(card.why, 150))}</p>
      </article>
      ${renderActions(card, "arcade-actions")}
    </div>
  `;
}

function renderSplit(concept, cards, card) {
  return `
    <article class="split-shell concept-card" data-card-id="${card.id}">
      ${renderVisual(card, "split-visual")}
      <section>
        ${renderMeta(card)}
        <h2>${escapeHtml(card.title)}</h2>
        <p>${escapeHtml(clip(card.why, 200))}</p>
        ${renderBlocks(card)}
        ${renderActions(card)}
      </section>
    </article>
  `;
}

function renderStack(concept, cards) {
  return `
    <div class="paper-stack">
      ${cards.slice(0, 5).map((card, index) => `
        <article class="paper-card concept-card" style="--i:${index}" data-card-id="${card.id}">
          ${renderMeta(card)}
          <h2>${escapeHtml(clip(card.title, 96))}</h2>
          <p>${escapeHtml(clip(card.why, 150))}</p>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderVoice(concept, cards, card) {
  return `
    <div class="voice-shell">
      <article class="voice-card concept-card" data-card-id="${card.id}">
        <div class="wave"><i></i><i></i><i></i><i></i><i></i><i></i></div>
        ${renderMeta(card)}
        <h2>${escapeHtml(card.title)}</h2>
        <p>${escapeHtml(clip(card.why, 180))}</p>
        <button class="voice-giant" data-action="voice" data-card-id="${card.id}">Tell agent what is wrong</button>
        ${renderActions(card)}
      </article>
    </div>
  `;
}

function renderCompact(concept, cards) {
  return `
    <div class="compact-list">
      ${cards.slice(0, 12).map((card) => `
        <article class="compact-row concept-card" data-card-id="${card.id}">
          <span>${escapeHtml(categoryMeta(card).short)}</span>
          <strong>${escapeHtml(clip(card.title, 64))}</strong>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderTable(concept, cards) {
  return `
    <div class="table-shell">
      <div class="table-head"><span>Source</span><span>Action</span><span>Impact</span></div>
      ${cards.slice(0, 10).map((card) => `
        <article class="table-row concept-card" data-card-id="${card.id}">
          <span>${escapeHtml(sourceName(card))}</span>
          <strong>${escapeHtml(clip(card.title, 60))}</strong>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderCoach(concept, cards, card) {
  const days = [1, 2, 3, 4, 5, 6, 7];
  const accepted = Object.values(state.local.decisions).filter((item) => item.status === "started").length;
  const streak = Number(state.local.streak || 0);
  return `
    <article class="coach-shell concept-card" data-card-id="${card.id}">
      <section class="streak-calendar">${days.map((day) => `<span class="${day <= Math.min(7, accepted + 1) ? "lit" : ""}">${day}</span>`).join("")}</section>
      <section class="coach-advice"><span>${streak || 1}-day momentum</span><h2>${escapeHtml(clip(card.title, 82))}</h2></section>
      <section><h3>Why now</h3><p>${escapeHtml(clip(card.why, 180))}</p></section>
      <section><h3>Evidence</h3>${renderBlocks(card)}</section>
      ${renderActions(card)}
    </article>
  `;
}

function renderDoc(concept, cards, card) {
  return `
    <article class="doc-shell concept-card" data-card-id="${card.id}">
      <h2>${escapeHtml(card.title)}</h2>
      <p>${escapeHtml(clip(card.why, 240))}</p>
      ${renderBlocks(card)}
      ${renderActions(card)}
    </article>
  `;
}

function renderForum(concept, cards) {
  return `
    <div class="forum-feed">
      ${cards.slice(0, 7).map((card) => `
        <article class="forum-post concept-card" data-card-id="${card.id}">
          <aside>${pointsFor(card)}<span>pts</span></aside>
          <section><h2>${escapeHtml(clip(card.title, 80))}</h2><p>${escapeHtml(clip(card.why, 120))}</p>${renderMiniActions(card)}</section>
        </article>
      `).join("")}
    </div>
  `;
}

function renderLinear(concept, cards) {
  return `
    <div class="linear-list">
      ${cards.slice(0, 10).map((card, index) => `
        <article class="linear-row concept-card" data-card-id="${card.id}">
          <span>BUX-${100 + index}</span>
          <strong>${escapeHtml(clip(card.title, 76))}</strong>
          <em>${escapeHtml(card.importance)}</em>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderPlaylist(concept, cards, card) {
  return `
    <div class="playlist-shell concept-card" data-card-id="${card.id}">
      ${renderVisual(card, "album-art")}
      <section><h2>${escapeHtml(card.title)}</h2><p>${escapeHtml(clip(card.why, 160))}</p>${renderActions(card)}</section>
      <ol>${cards.slice(1, 7).map((item) => `<li><button data-action="focus" data-card-id="${item.id}">${escapeHtml(clip(item.title, 64))}</button></li>`).join("")}</ol>
    </div>
  `;
}

function renderQuest(concept, cards) {
  const accepted = Object.values(state.local.decisions).filter((item) => item.status === "started").length;
  return `
    <div class="quest-ladder">
      <section class="quest-boss"><span>Goal boss</span><strong>${Math.max(0, 3 - accepted)} nodes to next unlock</strong><p>Pick the mission that is easiest to approve now. Each yes opens sharper cards.</p></section>
      ${cards.slice(0, 7).map((card, index) => `
        <article class="quest-step concept-card ${decisionFor(card) ? "claimed" : ""}" style="--i:${index}" data-card-id="${card.id}">
          <span>${index + 1}</span>
          <div><strong>${escapeHtml(clip(card.title, 76))}</strong><p>+${pointsFor(card)} XP · ${escapeHtml(sourceName(card))}</p></div>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderShop(concept, cards) {
  return `
    <div class="shop-shelf">
      ${cards.slice(0, 8).map((card) => `
        <article class="shop-card concept-card" data-card-id="${card.id}">
          ${renderVisual(card, "shop-visual")}
          <h2>${escapeHtml(clip(card.title, 62))}</h2>
          <p>${escapeHtml(clip(card.why, 92))}</p>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderBrief(concept, cards, card) {
  return `
    <article class="brief-shell concept-card" data-card-id="${card.id}">
      <time>Today</time>
      <h2>${escapeHtml(card.title)}</h2>
      <p>${escapeHtml(clip(card.why, 190))}</p>
      <div class="brief-lines">${cards.slice(1, 5).map((item) => `<span>${escapeHtml(clip(item.title, 54))}</span>`).join("")}</div>
      ${renderActions(card)}
    </article>
  `;
}

function renderFocus(concept, cards, card) {
  return `
    <article class="focus-shell concept-card" data-card-id="${card.id}">
      <span>${escapeHtml(sourceName(card))}</span>
      <h2>${escapeHtml(card.title)}</h2>
      <p>${escapeHtml(clip(card.why, 180))}</p>
      ${renderActions(card)}
    </article>
  `;
}

function renderBroadcast(concept, cards, card) {
  return `
    <div class="broadcast-shell">
      <article class="dispatch concept-card" data-card-id="${card.id}">
        <span>Dispatch ready</span>
        <h2>${escapeHtml(card.title)}</h2>
        <p>${escapeHtml(clip(card.why, 180))}</p>
      </article>
      <aside>${renderActions(card)}<button data-action="context" data-card-id="${card.id}">Edit before sending</button></aside>
    </div>
  `;
}

function renderCrm(concept, cards) {
  return `
    <div class="crm-pipeline">
      ${["Lead", "Risk", "Follow-up"].map((label, lane) => `
        <section><h2>${label}</h2>${cards.filter((_, index) => index % 3 === lane).slice(0, 4).map((card) => `
          <article class="crm-card concept-card" data-card-id="${card.id}">
            <strong>${escapeHtml(clip(card.title, 62))}</strong>
            <p>${escapeHtml(sourceName(card))}</p>
            ${renderMiniActions(card)}
          </article>
        `).join("")}</section>
      `).join("")}
    </div>
  `;
}

function renderTerminal(concept, cards, card) {
  return `
    <article class="terminal-shell concept-card" data-card-id="${card.id}">
      <pre>$ bux suggest --next\nsource=${escapeHtml(sourceName(card))}\nimpact=${pointsFor(card)}\n\n${escapeHtml(clip(card.title, 140))}</pre>
      <p>${escapeHtml(clip(card.why, 180))}</p>
      ${renderActions(card)}
    </article>
  `;
}

function renderComic(concept, cards, card) {
  const blocks = card.blocks.length ? card.blocks : [{ title: "Problem", body: card.why }, { title: "Agent", body: card.action || primaryButton(card) }, { title: "You", body: "Approve, skip, or comment." }];
  return `
    <div class="comic-strip concept-card" data-card-id="${card.id}">
      ${blocks.slice(0, 3).map((block) => `<section><strong>${escapeHtml(block.title)}</strong><p>${escapeHtml(clip(block.body, 110))}</p></section>`).join("")}
      ${renderActions(card)}
    </div>
  `;
}

function renderRoadmap(concept, cards) {
  const groups = groupedCards(cards);
  const card = cards[0];
  const accepted = Object.values(state.local.decisions).filter((item) => item.status === "started").length;
  return `
    <div class="roadmap-shell">
      <header class="tree-progress"><strong>${accepted} unlocked</strong><span>Branches open when cards get accepted.</span></header>
      ${groups.slice(0, 4).map(([key, items], lane) => `
        <section><h2>${escapeHtml(categoryMeta({ category: key }).label)}</h2><p>${items.length + 1} unlocks</p>${items.slice(0, 3).map((card, index) => `
          <article class="road-card concept-card ${index > accepted ? "locked" : ""}" style="--lane:${lane}" data-card-id="${card.id}"><span>${index > accepted ? "Locked" : `+${pointsFor(card)}`}</span>${escapeHtml(clip(card.title, 70))}</article>
        `).join("")}<article class="road-card concept-card locked"><span>Locked</span>Next ability opens after one accepted card.</article></section>
      `).join("")}
      ${card ? renderActions(card, "road-actions") : ""}
    </div>
  `;
}

function renderHabit(concept, cards, card) {
  const groups = groupedCards(cards).slice(0, 3);
  const accepted = Object.values(state.local.decisions).filter((item) => item.status === "started").length;
  return `
    <article class="habit-shell concept-card" data-card-id="${card.id}">
      <div class="ring-score"><strong>${accepted}/3</strong><span>rings closed today</span></div>
      <div class="rings">${groups.map(([key, items], index) => `<span style="--ring:${Math.min(96, 28 + items.length * 18 + index * 9)}%"><b>${escapeHtml(categoryMeta({ category: key }).short)}</b><em>${items.length}</em></span>`).join("")}</div>
      <h2>${escapeHtml(card.title)}</h2>
      <p>${escapeHtml(clip(card.why, 160))}</p>
      ${renderActions(card)}
    </article>
  `;
}

function renderMarket(concept, cards) {
  return renderShop(concept, cards);
}

function renderOneButton(concept, cards, card) {
  const reward = state.local.lastReward || `+${pointsFor(card)} XP`;
  return `
    <article class="one-button-shell concept-card" data-card-id="${card.id}">
      ${renderMeta(card)}
      <span class="win-label">Ready-to-approve win · ${escapeHtml(reward)}</span>
      <h2>${escapeHtml(card.title)}</h2>
      <p>${escapeHtml(clip(card.why, 180))}</p>
      <button class="mega-button" data-action="start" data-card-id="${card.id}">${escapeHtml(primaryButton(card))}</button>
      <button data-action="skip" data-card-id="${card.id}">Skip</button>
      <button data-action="context" data-card-id="${card.id}">Tell agent what to change</button>
    </article>
  `;
}

function renderDraftStudio(concept, cards, card) {
  return `
    <div class="draft-studio concept-card" data-card-id="${card.id}">
      <aside><h2>${escapeHtml(clip(card.title, 70))}</h2><p>${escapeHtml(clip(card.why, 120))}</p></aside>
      <section>${renderBlocks(card)}</section>
      ${renderActions(card)}
    </div>
  `;
}

function renderTeam(concept, cards) {
  return `
    <div class="team-room">
      ${cards.slice(0, 8).map((card) => `
        <article class="person-card concept-card" data-card-id="${card.id}">
          <span>${escapeHtml(categoryMeta(card).short)}</span>
          <strong>${escapeHtml(sourceName(card))}</strong>
          <p>${escapeHtml(clip(card.title, 82))}</p>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderShelves(concept, cards) {
  const groups = groupedCards(cards);
  const card = cards[0];
  return `
    <div class="shelves">
      ${groups.slice(0, 5).map(([key, items]) => `
        <section><h2>${escapeHtml(categoryMeta({ category: key }).label)}</h2><div>${items.map((card) => `<button data-action="focus" data-card-id="${card.id}">${escapeHtml(clip(card.title, 54))}</button>`).join("")}</div></section>
      `).join("")}
      ${card ? `<article class="concept-card shelf-action" data-card-id="${card.id}">${renderActions(card)}</article>` : ""}
    </div>
  `;
}

function renderReceipt(concept, cards, card) {
  return `
    <article class="receipt-shell concept-card" data-card-id="${card.id}">
      <h2>Agent receipt</h2>
      <p>${escapeHtml(card.title)}</p>
      ${renderBlocks(card)}
      <hr />
      ${renderActions(card)}
    </article>
  `;
}

function renderAuction(concept, cards) {
  return `
    <div class="auction-room">
      ${cards.slice(0, 6).map((card) => `
        <article class="bid-card concept-card" data-card-id="${card.id}">
          <span>${pointsFor(card)}</span>
          <h2>${escapeHtml(clip(card.title, 70))}</h2>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderLaunch(concept, cards, card) {
  return `
    <div class="launch-shell concept-card" data-card-id="${card.id}">
      <section><h2>${escapeHtml(card.title)}</h2><p>${escapeHtml(clip(card.why, 160))}</p>${renderActions(card)}</section>
      <ol>${["Copy", "Assets", "Post", "Watch", "Reply"].map((step) => `<li>${step}</li>`).join("")}</ol>
    </div>
  `;
}

function renderLetter(concept, cards, card) {
  return `
    <article class="letter-shell concept-card" data-card-id="${card.id}">
      <p>Dear Magnus,</p>
      <h2>${escapeHtml(card.title)}</h2>
      <p>${escapeHtml(clip(card.why, 210))}</p>
      <p>The agent will stay inside approval boundaries unless you tap start.</p>
      ${renderActions(card)}
    </article>
  `;
}

function renderMission(concept, cards, card) {
  return `
    <div class="mission-shell concept-card" data-card-id="${card.id}">
      <section class="orbit">${renderVisual(card, "mission-visual")}</section>
      <section><span>Mission objective</span><h2>${escapeHtml(card.title)}</h2><p>${escapeHtml(clip(card.why, 160))}</p><div class="mission-criteria"><strong>Success</strong><span>${escapeHtml(primaryButton(card))}</span><strong>Payoff</strong><span>${pointsFor(card)} XP and a cleaner next card</span><strong>Boundary</strong><span>Agent waits before visible external action.</span></div>${renderActions(card)}</section>
    </div>
  `;
}

function renderSports(concept, cards, card) {
  return `
    <article class="sports-shell concept-card" data-card-id="${card.id}">
      ${renderVisual(card, "sports-visual")}
      <h2>${escapeHtml(card.title)}</h2>
      <div class="season-track">${cards.slice(0, 5).map((item, index) => `<span class="${decisionFor(item) ? "claimed" : ""}"><b>Tier ${index + 1}</b><em>${pointsFor(item)} XP</em></span>`).join("")}</div>
      <div class="stats"><span>Impact ${pointsFor(card)}</span><span>${escapeHtml(card.importance)}</span><span>${escapeHtml(sourceName(card))}</span></div>
      ${renderActions(card)}
    </article>
  `;
}

function renderProof(concept, cards, card) {
  return `
    <article class="proof-shell concept-card" data-card-id="${card.id}">
      <section><h2>Evidence</h2>${renderBlocks(card)}</section>
      <section><h2>${escapeHtml(card.title)}</h2><p>${escapeHtml(clip(card.why, 160))}</p>${renderActions(card)}</section>
    </article>
  `;
}

function renderSplitDeck(concept, cards, card) {
  const buttons = cardButtons(card);
  return `
    <div class="splitdeck-shell concept-card" data-card-id="${card.id}">
      ${buttons.slice(0, 2).map((button, index) => `<section><span>Option ${index + 1}</span><h2>${escapeHtml(button.text)}</h2><p>${escapeHtml(clip(card.why, 130))}</p><button data-action="variant" data-card-id="${card.id}" data-index="${index}">Choose</button></section>`).join("")}
      ${renderActions(card)}
    </div>
  `;
}

function renderTiles(concept, cards) {
  return `
    <div class="tile-os">
      ${cards.slice(0, 12).map((card) => `
        <article class="os-tile concept-card" data-card-id="${card.id}">
          <button data-action="focus" data-card-id="${card.id}"><span>${escapeHtml(categoryMeta(card).short)}</span>${escapeHtml(clip(card.title, 48))}</button>
          ${renderMiniActions(card)}
        </article>
      `).join("")}
    </div>
  `;
}

function renderConcierge(concept, cards, card) {
  return `
    <article class="concierge-shell concept-card" data-card-id="${card.id}">
      <span>Concierge proposal</span>
      <h2>${escapeHtml(card.title)}</h2>
      <p>${escapeHtml(clip(card.why, 180))}</p>
      ${renderBlocks(card)}
      ${renderActions(card)}
    </article>
  `;
}

function renderGeneric(concept, cards, card) {
  return renderSplit(concept, cards, card);
}

function renderVisual(card, extra = "") {
  const meta = categoryMeta(card);
  const visual = card.visual || {};
  if (visual.kind === "video" && visual.src) {
    return `<figure class="visual-box has-media ${extra}"><video src="${escapeAttr(visual.src)}" autoplay loop muted playsinline></video></figure>`;
  }
  if (visual.kind === "image" && visual.src) {
    return `<figure class="visual-box has-media ${extra}"><img src="${escapeAttr(visual.src)}" alt="" loading="lazy" /></figure>`;
  }
  return `
    <figure class="visual-box visual-art no-media ${extra}" style="--card-accent:${meta.color}">
      <i></i><i></i><i></i>
      <span>${escapeHtml(meta.short)}</span>
    </figure>
  `;
}

function hasRealVisual(card) {
  if (!card?.visual?.src || !["image", "video"].includes(card.visual.kind)) return false;
  return !String(card.visual.src).startsWith("data:image/svg+xml");
}

function renderMeta(card) {
  const meta = categoryMeta(card);
  return `
    <div class="meta-line" style="--card-accent:${meta.color}">
      <span>${escapeHtml(meta.label)}</span>
      <strong>${escapeHtml(sourceName(card))}</strong>
    </div>
  `;
}

function renderActions(card, className = "") {
  const choices = agentChoices(card).slice(0, 3);
  return `
    <footer class="action-bar ${className}">
      <div class="agent-buttons">
        ${choices.map((button, index) => `
          <button class="${index === 0 ? "primary-action" : ""}" data-action="start" data-card-id="${card.id}" data-index="${button.index}">
            ${escapeHtml(button.text)}
          </button>
        `).join("")}
      </div>
      <div class="utility-buttons">
        <button data-action="skip" data-card-id="${card.id}">Skip</button>
        <button data-action="context" data-card-id="${card.id}">Add context</button>
      </div>
    </footer>
  `;
}

function renderMiniActions(card) {
  const primary = agentChoices(card)[0];
  return `
    <div class="mini-actions">
      <button data-action="start" data-card-id="${card.id}" data-index="${primary.index}">${escapeHtml(clip(primary.text, 22))}</button>
      <button data-action="skip" data-card-id="${card.id}">Skip</button>
    </div>
  `;
}

function renderBlocks(card) {
  const blocks = Array.isArray(card.blocks) && card.blocks.length ? card.blocks : [
    { title: "Context", body: card.action || card.why || "No extra context yet." },
  ];
  return `
    <div class="block-list">
      ${blocks.slice(0, 3).map((block) => `
        <section>
          <strong>${escapeHtml(block.title || "Detail")}</strong>
          <p>${escapeHtml(clip(block.body || "", 180))}</p>
        </section>
      `).join("")}
    </div>
  `;
}

function activeCards(limit = 100) {
  const pending = state.cards.filter((card) => !["started", "skipped"].includes(decisionFor(card)?.status));
  const cards = pending.length ? pending : state.cards;
  return cards.slice(0, limit);
}

function focusedCard(cards = activeCards(18)) {
  return cards.find((card) => String(card.id) === String(state.focusCardId)) || cards[0] || state.cards[0] || DEMO_CARDS[0];
}

function prioritizeCard(cards, card) {
  if (!card) return cards;
  return [card, ...cards.filter((item) => String(item.id) !== String(card.id))];
}

function groupedCards(cards = activeCards(100)) {
  return Object.entries(cards.reduce((acc, card) => {
    const key = card.category || inferCategory(card);
    acc[key] ||= [];
    acc[key].push(card);
    return acc;
  }, {}));
}

function groupByCategory() {
  return activeCards(100).reduce((acc, card) => {
    const key = card.category || inferCategory(card);
    acc[key] ||= [];
    acc[key].push(card);
    return acc;
  }, {});
}

function categoryMeta(card) {
  return CATEGORY_META[card.category || inferCategory(card)] || CATEGORY_META.ops;
}

function selectedIndex(card) {
  const total = agentChoices(card).length;
  if (!total) return 0;
  const raw = Number(state.selected[String(card.id)] || 0);
  return Math.max(0, Math.min(total - 1, raw));
}

function cardButtons(card) {
  return ensureButtons(card?.buttons).map((item) => ({ raw: item, text: buttonText(item) }));
}

function agentChoices(card) {
  const choices = cardButtons(card)
    .map((button, index) => ({ ...button, index }))
    .filter((button) => button.text.toLowerCase() !== "skip");
  return choices.length ? choices : [{ raw: "Start", text: "Start", index: 0 }];
}

function selectedRaw(card) {
  return agentChoices(card)[selectedIndex(card)]?.raw || "";
}

function primaryButton(card) {
  return agentChoices(card)[0]?.text || "Start";
}

function buttonText(value) {
  return String(value || "")
    .replace(/^✅\s*/, "")
    .replace(/^🛠️?\s*/, "")
    .replace(/^✏️\s*/, "")
    .trim() || "Start";
}

function sourceName(card) {
  return card.source_label || card.topic_title || card.source || "bux";
}

function decisionFor(card) {
  return state.local.decisions[String(card.id)] || null;
}

function approvalsToday() {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  return Object.values(state.local.decisions || {}).filter((item) => (
    item?.status === "started" && Number(item.at || 0) >= start.getTime()
  )).length;
}

function markDecision(card, status, detail = "") {
  if (!card) return;
  haptic(status === "started" ? "success" : "light");
  const gained = status === "started" ? pointsFor(card) : 0;
  const combo = status === "started" ? Number(state.local.combo || 0) + 1 : 0;
  state.local.decisions[String(card.id)] = {
    status,
    detail,
    title: card.title,
    source: sourceName(card),
    at: Date.now(),
  };
  state.local.combo = combo;
  state.local.streak = status === "started" ? Number(state.local.streak || 0) + 1 : Number(state.local.streak || 0);
  state.local.lastReward = status === "started"
    ? `+${gained} XP · ${combo} combo`
    : "Combo reset · better next card";
  if (gained) state.local.points = Number(state.local.points || 0) + gained;
  saveLocalState();
  state.focusCardId = activeCards(1)[0]?.id || state.cards[0]?.id || null;
}

function localActivity() {
  return Object.entries(state.local.decisions)
    .map(([id, item]) => ({ id, ...item }))
    .sort((a, b) => Number(b.at || 0) - Number(a.at || 0));
}

function pointsFor(card) {
  const base = card.importance === "high" ? 120 : card.importance === "low" ? 40 : 80;
  return base + Math.min(40, cardButtons(card).length * 10);
}

function remixCard(card) {
  const source = DEMO_CARDS[(Date.now() + state.local.cards.length) % DEMO_CARDS.length];
  const category = card?.category || source.category || "ops";
  const copy = {
    ...source,
    id: `local-${Date.now()}`,
    title: card ? `Sharper version: ${clip(card.title, 52)}` : source.title,
    why: card ? `A local remix with a clearer first action for ${sourceName(card)}.` : source.why,
    source: `miniapp-local:${Date.now()}`,
    source_label: "Local remix",
    buttons: ensureButtons(card?.buttons || source.buttons),
    category,
    demo: true,
    visual: { kind: "none" },
    created_at: Math.round(Date.now() / 1000),
  };
  state.local.cards.unshift(copy);
  saveLocalState();
  state.cards = mergeCards(state.cards.filter((item) => !item.demo || !String(item.id).startsWith("demo-")));
  state.focusCardId = copy.id;
  haptic("success");
  toast("New local variant generated.");
  render();
}

function clip(value, limit) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, Math.max(0, limit - 1)).trim()}...` : text;
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

function toast(message) {
  toastEl.textContent = message;
  toastEl.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => toastEl.classList.remove("show"), 2100);
}

function haptic(kind = "light") {
  try {
    if (kind === "selectionChanged") tg?.HapticFeedback?.selectionChanged?.();
    else if (kind === "success") tg?.HapticFeedback?.notificationOccurred?.("success");
    else tg?.HapticFeedback?.impactOccurred?.(kind);
  } catch {
    // Haptics are optional outside Telegram.
  }
}

function saveNote(card, note) {
  state.local.notes[String(card.id)] = [...(state.local.notes[String(card.id)] || []), note];
  saveLocalState();
}

function addContext(card) {
  const comment = window.prompt("What should the agent change?", "Make this more concrete.");
  if (!comment?.trim()) return;
  saveNote(card, comment.trim());
  haptic("success");
  toast("Context saved.");
  syncComment(card, comment.trim());
}

function addVoiceNote(card) {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    const comment = window.prompt("Voice fallback: what should the agent know?");
    if (!comment?.trim()) return;
    saveNote(card, `Voice note: ${comment.trim()}`);
    syncComment(card, comment.trim());
    toast("Voice note saved.");
    return;
  }
  const recognition = new SpeechRecognition();
  recognition.lang = "en-US";
  recognition.interimResults = false;
  recognition.onresult = (event) => {
    const text = event.results?.[0]?.[0]?.transcript || "";
    if (text.trim()) {
      saveNote(card, `Voice note: ${text.trim()}`);
      syncComment(card, text.trim());
      toast("Voice note saved.");
    }
  };
  recognition.onerror = () => toast("Voice capture failed.");
  recognition.start();
  toast("Listening...");
}

function syncStart(card) {
  if (isConceptDemoOnly(card)) return;
  api(`/api/cards/${card.id}/start`, {
    method: "POST",
    body: JSON.stringify({ button: selectedRaw(card) }),
  })
    .then(() => refresh())
    .catch(() => toast("Saved locally. Backend write did not accept it yet."));
}

function syncSkip(card) {
  if (isConceptDemoOnly(card)) return;
  api(`/api/cards/${card.id}/dismiss`, { method: "POST", body: "{}" })
    .then(() => refresh())
    .catch(() => toast("Skipped locally. Backend write did not accept it yet."));
}

function syncComment(card, comment) {
  if (isConceptDemoOnly(card)) return;
  api(`/api/cards/${card.id}/comment`, {
    method: "POST",
    body: JSON.stringify({ comment }),
  }).catch(() => toast("Note saved locally. Backend write did not accept it yet."));
}

function isConceptDemoOnly(card) {
  return !initData || card.demo || params.get("dev") === "1" || params.get("concept_sync") !== "1";
}

app.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  const cardId = target.dataset.cardId;
  const card = state.cards.find((item) => String(item.id) === String(cardId));

  if (action === "focus-next" || action === "focus-prev") {
    const cards = activeCards(100);
    if (!cards.length) return;
    const current = cards.findIndex((item) => String(item.id) === String(state.focusCardId));
    const fallback = current >= 0 ? current : 0;
    const direction = action === "focus-next" ? 1 : -1;
    const next = cards[(fallback + direction + cards.length) % cards.length];
    if (next) {
      state.focusCardId = next.id;
      haptic("selectionChanged");
      render();
    }
    return;
  }
  if (action === "focus" && card) {
    state.focusCardId = card.id;
    haptic("selectionChanged");
    render();
    return;
  }
  if (action === "variant" && card) {
    state.selected[String(card.id)] = Number(target.dataset.index || 0);
    haptic("selectionChanged");
    render();
    return;
  }
  if (action === "start" && card) {
    if (target.dataset.index !== undefined) {
      state.selected[String(card.id)] = Number(target.dataset.index || 0);
    }
    markDecision(card, "started", selectedRaw(card));
    haptic("success");
    toast(`Started: ${buttonText(selectedRaw(card))}`);
    render();
    syncStart(card);
    return;
  }
  if (action === "skip" && card) {
    markDecision(card, "skipped", "skip");
    haptic("medium");
    toast("Skipped.");
    render();
    syncSkip(card);
    return;
  }
  if (action === "context" && card) {
    addContext(card);
    return;
  }
  if (action === "voice" && card) {
    addVoiceNote(card);
    return;
  }
  if (action === "generate") {
    remixCard(focusedCard());
    if (initData) api("/api/generate", { method: "POST", body: "{}" }).catch(() => {});
  }
});

let swipe = null;

app.addEventListener("pointerdown", (event) => {
  const target = event.target.closest("[data-swipe-card]");
  if (!target) return;
  if (target.dataset.swipeCard !== "1") return;
  swipe = {
    el: target,
    id: target.dataset.cardId,
    x: event.clientX,
    y: event.clientY,
  };
  target.setPointerCapture?.(event.pointerId);
  target.classList.add("swiping");
});

app.addEventListener("pointermove", (event) => {
  if (!swipe) return;
  const dx = event.clientX - swipe.x;
  const dy = event.clientY - swipe.y;
  const rot = Math.max(-12, Math.min(12, dx / 14));
  swipe.el.style.transform = `translate(${dx}px, ${dy * 0.24}px) rotate(${rot}deg)`;
  swipe.el.dataset.intent = dx > 48 ? "start" : dx < -48 ? "skip" : "";
});

app.addEventListener("pointerup", (event) => {
  if (!swipe) return;
  const dx = event.clientX - swipe.x;
  const card = state.cards.find((item) => String(item.id) === String(swipe.id));
  swipe.el.classList.remove("swiping");
  swipe.el.style.transform = "";
  swipe.el.dataset.intent = "";
  swipe = null;
  if (!card || Math.abs(dx) < 96) return;
  if (dx > 0) {
    markDecision(card, "started", selectedRaw(card));
    toast(`Boss hit: ${buttonText(selectedRaw(card))}`);
    syncStart(card);
  } else {
    markDecision(card, "skipped", "swipe left");
    toast("Dodged.");
    syncSkip(card);
  }
  render();
});

app.addEventListener("pointercancel", () => {
  if (!swipe) return;
  swipe.el.classList.remove("swiping");
  swipe.el.style.transform = "";
  swipe.el.dataset.intent = "";
  swipe = null;
});

await refresh();
