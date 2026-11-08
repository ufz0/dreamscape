// Wiki graph view: pages and links on a canvas with a force-directed layout.
// Plain canvas 2D, no library: own Barnes-Hut repulsion, link springs, centre gravity, collision.
import { api, escapeHtml } from "./api.js";
import { icon } from "./icons.js";
import { renderMarkdown } from "./markdown.js";

const KEY = "dreamscape.graph";
const MIN_R = 3;
const MAX_R = 14;
const TOP_LABELS = 10;

const DEFAULTS = {
  colorMode: "new",
  filters: { showOrphans: true, showGhosts: true, minLinks: 0 },
  display: { nodeScale: 100, linkWidth: 80, labelZoom: 160, arrows: false },
  forces: { center: 40, repel: 55, linkStrength: 45, linkDistance: 80 },
};

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

function loadState() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (error) { saved = {}; }
  return {
    colorMode: saved.colorMode || DEFAULTS.colorMode,
    filters: { ...DEFAULTS.filters, ...saved.filters },
    display: { ...DEFAULTS.display, ...saved.display },
    forces: { ...DEFAULTS.forces, ...saved.forces },
  };
}

function palette() {
  const styles = getComputedStyle(document.documentElement);
  const read = (name, fallback) => (styles.getPropertyValue(name) || "").trim() || fallback;
  return {
    bg: read("--bg", "#faf9f5"),
    node: read("--graph-node", "#b8b5a8"),
    nodeFill: read("--graph-node-fill", "#c4c1b4"),
    edge: read("--graph-edge", "rgba(112,110,102,0.22)"),
    accent: read("--accent", "#d97757"),
    text: read("--text", "#1f1e1d"),
    muted: read("--muted", "#73726c"),
  };
}

export class GraphView {
  constructor(root, options = {}) {
    this.root = root;
    this.compact = Boolean(options.compact);
    this.onOpen = options.onOpen || (() => {});
    this.onEdit = options.onEdit || (() => {});

    this.state = loadState();
    this.data = { nodes: [], edges: [], broken: [], stats: { pages: 0, links: 0, runs: 1, from_git: false } };
    this.nodes = [];
    this.edges = [];
    this.byId = new Map();
    this.maxDegree = 1;

    this.view = { scale: 1, x: 0, y: 0 };
    this.hover = null;
    this.selected = null;
    this.timeline = { k: 0, baseline: 0, playing: false, stepAt: 0 };
    this.query = "";
    this.matches = new Set();
    this.subset = null;
    this.lastFps = 0;

    this.alpha = 0;
    this.frame = 0;
    this.running = false;
    this.dragging = null;
    this.pan = null;
    this.pointers = new Map();
    this.pinch = null;
    this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    this.colors = palette();
    new MutationObserver(() => { this.colors = palette(); this.draw(); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    this.build();
  }

  // ---------- structure ----------

  build() {
    this.root.classList.add("graph-view");
    this.root.innerHTML = this.compact
      ? `<canvas class="graph-canvas" aria-label="Local graph"></canvas>`
      : `<div class="graph-toolbar">
          <div class="graph-search">
            <span data-icon="search" data-size="15"></span>
            <input id="graph-query" type="search" placeholder="Search pages" aria-label="Search pages">
          </div>
          <button class="icon-btn" id="graph-fit" data-icon="fit" title="Fit all (F)" aria-label="Fit all"></button>
          <button class="icon-btn" id="graph-gear" data-icon="gear" title="Graph settings" aria-label="Graph settings"></button>
        </div>
        <div class="graph-controls" id="graph-controls" hidden></div>
        <canvas class="graph-canvas" aria-label="Wiki graph"></canvas>
        <aside class="graph-side" id="graph-side" hidden></aside>
        <div class="graph-stage">
          <p class="graph-caption" id="graph-caption"></p>
          <div class="graph-legend" id="graph-legend"></div>
          <div class="graph-timeline" id="graph-timeline"></div>
        </div>
        <div class="graph-tooltip" id="graph-tooltip" hidden></div>`;

    for (const node of this.root.querySelectorAll("[data-icon]")) {
      node.innerHTML = icon(node.dataset.icon, Number(node.dataset.size) || 18);
    }

    this.canvas = this.root.querySelector("canvas");
    this.context = this.canvas.getContext("2d");

    this.canvas.addEventListener("pointerdown", (event) => this.pointerDown(event));
    this.canvas.addEventListener("pointermove", (event) => this.pointerMove(event));
    this.canvas.addEventListener("pointerup", (event) => this.pointerUp(event));
    this.canvas.addEventListener("pointercancel", (event) => this.pointerUp(event));
    this.canvas.addEventListener("wheel", (event) => this.zoomAt(event), { passive: false });
    this.canvas.addEventListener("dblclick", () => this.fitAll());

    if (!this.compact) {
      const search = this.root.querySelector("#graph-query");
      search.addEventListener("input", (event) => this.setQuery(event.target.value));
      search.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && this.matches.size) this.focusNode([...this.matches][0]);
      });
      this.root.querySelector("#graph-fit").addEventListener("click", () => this.fitAll());
      this.root.querySelector("#graph-gear").addEventListener("click", () => {
        const panel = this.root.querySelector("#graph-controls");
        panel.hidden = !panel.hidden;
        if (!panel.hidden) this.buildControls();
      });
      this.buildControls();
      this.buildTimeline();
    }

    window.addEventListener("keydown", (event) => {
      if (event.target instanceof Element && event.target.matches("input, textarea, select")) return;
      if (event.key === "Escape") this.select(null);
      if (event.key === "/") { event.preventDefault(); this.root.querySelector("#graph-query")?.focus(); }
      if (event.key.toLowerCase() === "f") this.fitAll();
    });

    window.addEventListener("resize", () => { this.resizeCanvas(); this.draw(); });
    this.resizeCanvas();
  }

  // Called when the view becomes visible: the canvas has a real size only then.
  show() {
    this.resizeCanvas();
    this.draw();
    if (this.nodes.length) this.fitAll();
    if (!this.compact) window.graph = this;   // handy from the browser console and the tests
  }

  // ---------- control panel ----------

  field(label, control) {
    const row = document.createElement("div");
    row.className = "field";
    const text = document.createElement("label");
    text.textContent = label;
    row.append(text, control);
    return row;
  }

  slider(name, group, value, min, max) {
    const input = document.createElement("input");
    input.type = "range";
    input.min = min;
    input.max = max;
    input.value = value;
    input.setAttribute("aria-label", name);
    input.addEventListener("input", () => {
      this.state[group][name] = Number(input.value);
      this.save();
      if (group === "forces") this.reheat(0.5);
      this.draw();
    });
    return input;
  }

  checkbox(name, group, value) {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    input.addEventListener("change", () => {
      this.state[group][name] = input.checked;
      this.save();
      this.draw();
    });
    return input;
  }

  save() {
    localStorage.setItem(KEY, JSON.stringify(this.state));
  }

  buildControls() {
    const panel = this.root.querySelector("#graph-controls");
    if (!panel) return;
    panel.innerHTML = "";

    const section = (title, ...rows) => {
      const block = document.createElement("section");
      const heading = document.createElement("h3");
      heading.textContent = title;
      block.append(heading, ...rows);
      return block;
    };

    const reset = document.createElement("button");
    reset.className = "small-button";
    reset.textContent = "Reset";
    reset.addEventListener("click", () => {
      for (const key of ["colorMode", "filters", "display", "forces"]) this.state[key] = { ...DEFAULTS[key] };
      this.save();
      this.buildControls();
      this.reheat(0.6);
    });

    const mode = document.createElement("select");
    mode.setAttribute("aria-label", "Color mode");
    for (const value of ["new", "prefix", "degree"]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = { new: "New vs old", prefix: "By prefix", degree: "By degree" }[value];
      option.selected = this.state.colorMode === value;
      mode.append(option);
    }
    mode.addEventListener("change", () => { this.state.colorMode = mode.value; this.save(); this.draw(); });

    panel.append(
      section("Filters",
        this.field("Show orphans", this.checkbox("showOrphans", "filters", this.state.filters.showOrphans)),
        this.field("Show broken links", this.checkbox("showGhosts", "filters", this.state.filters.showGhosts)),
        this.field("Minimum links", this.slider("minLinks", "filters", this.state.filters.minLinks, 0, 30)),
      ),
      section("Display",
        this.field("Node size", this.slider("nodeScale", "display", this.state.display.nodeScale, 40, 200)),
        this.field("Link thickness", this.slider("linkWidth", "display", this.state.display.linkWidth, 30, 200)),
        this.field("Labels past zoom", this.slider("labelZoom", "display", this.state.display.labelZoom, 50, 300)),
        this.field("Show arrows", this.checkbox("arrows", "display", this.state.display.arrows)),
      ),
      section("Forces",
        this.field("Centre force", this.slider("center", "forces", this.state.forces.center, 0, 100)),
        this.field("Repel force", this.slider("repel", "forces", this.state.forces.repel, 0, 100)),
        this.field("Link strength", this.slider("linkStrength", "forces", this.state.forces.linkStrength, 0, 100)),
        this.field("Link distance", this.slider("linkDistance", "forces", this.state.forces.linkDistance, 10, 200)),
        this.field("", reset),
      ),
      section("Colour", this.field("Mode", mode), this.field("Legend", this.legend())),
    );
  }

  legend() {
    const legend = document.createElement("div");
    legend.className = "legend";
    const swatch = (color, label) => {
      const item = document.createElement("span");
      const dot = document.createElement("i");
      dot.style.background = color;
      item.append(dot, document.createTextNode(label));
      legend.append(item);
    };
    if (this.state.colorMode === "degree") {
      swatch(this.colors.node, "Few links");
      swatch(this.colors.nodeFill, "Many links");
    } else if (this.state.colorMode === "prefix") {
      swatch(this.colors.node, "Colour by the first word of the slug");
    } else {
      swatch(this.colors.node, "Existing page");
      swatch(this.colors.accent, "Added later");
    }
    return legend;
  }

  // ---------- data ----------

  async load() {
    this.data = await api("/api/wiki/graph");
    this.byId.clear();
    this.nodes = [];
    this.edges = [];

    const make = (record, ghost) => ({
      id: record.id, title: record.title || record.id, degree: record.degree, bytes: record.bytes || 0,
      run: record.dream_run ?? 0, orphan: Boolean(record.orphan), description: record.description || "",
      updated: record.updated_at || "", ghost, referencedBy: record.referenced_by || [],
      neighbors: new Set(), x: (Math.random() - 0.5) * 300, y: (Math.random() - 0.5) * 300,
      vx: 0, vy: 0, pinned: false, placed: false, appear: 1,
      labelAlpha: 0, labelTarget: 0, topLabel: false,
    });

    for (const record of this.data.nodes) {
      const node = make(record, false);
      this.nodes.push(node);
      this.byId.set(node.id, node);
    }
    for (const ghost of this.data.broken) {
      const node = make(ghost, true);
      this.nodes.push(node);
      this.byId.set(node.id, node);
    }

    for (const record of this.data.edges) {
      const a = this.byId.get(record.source), b = this.byId.get(record.target);
      if (!a || !b) continue;
      a.neighbors.add(b.id);
      b.neighbors.add(a.id);
      this.edges.push({ a, b, run: record.run });
    }

    this.maxDegree = Math.max(1, ...this.nodes.map((node) => node.degree));
    for (const node of [...this.nodes].sort((a, b) => b.degree - a.degree).slice(0, TOP_LABELS)) {
      node.topLabel = true;
    }

    this.timeline.k = Math.max(0, this.data.stats.runs - 1);
    this.timeline.baseline = Math.max(0, this.data.stats.runs - 2);

    if (!this.nodes.length) {
      this.root.classList.add("empty");
      this.drawCaption();
      return false;
    }

    this.buildTimeline();
    this.drawCaption();
    this.reheat(1);
    if (this.reducedMotion) this.preRun(300);
    this.fitAll();
    return true;
  }

  // ---------- what is on screen ----------

  visible(node) {
    if (this.subset && !this.subset.has(node.id)) return false;
    if (node.run > this.timeline.k) return false;
    if (node.ghost && !this.state.filters.showGhosts) return false;
    if (node.orphan && !this.state.filters.showOrphans) return false;
    if (node.degree < this.state.filters.minLinks) return false;
    return true;
  }

  radius(node) {
    const size = MIN_R + (MAX_R - MIN_R) * Math.sqrt(node.degree / this.maxDegree);
    return Math.max(1.5, size * (this.state.display.nodeScale / 100));
  }

  nodeColor(node) {
    if (this.state.colorMode === "degree") {
      return node.degree / this.maxDegree > 0.55 ? this.colors.nodeFill : this.colors.node;
    }
    if (this.state.colorMode === "prefix") {
      const index = "abcdefghijklmnopqrstuvwxyz".indexOf(node.id[0] || "a");
      return `hsl(${(Math.max(0, index) * 47) % 360}, 20%, 62%)`;
    }
    return node.run > this.timeline.baseline ? this.colors.accent : this.colors.node;
  }

  dimmed(node) {
    if (this.query && !this.matches.has(node.id)) return 0.18;
    const focus = this.hover || this.selected;
    if (focus && node !== focus && !focus.neighbors.has(node.id)) return 0.2;
    return 1;
  }

  // ---------- simulation ----------

  reheat(alpha = 0.5) {
    this.alpha = Math.max(this.alpha, alpha);
    this.start();
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.lastTime = performance.now();
    requestAnimationFrame((now) => this.loop(now));
  }

  loop(now) {
    const delta = Math.max(1, now - this.lastTime);
    this.lastTime =now;
    this.lastFps = Math.round(0.9 * this.lastFps + 0.1 * (1000 / delta));

    if (this.timeline.playing) this.stepTimeline(now);
    if (this.alpha > 0.004) this.tick();

    let labelsMoving = false;
    for (const node of this.nodes) {
      if (!this.visible(node)) { node.labelAlpha = 0; continue; }
      node.labelTarget = this.wantLabel(node) ? 1 : 0;
      if (Math.abs(node.labelTarget - node.labelAlpha) > 0.01) {
        node.labelAlpha += (node.labelTarget - node.labelAlpha) * 0.12;
        labelsMoving = true;
      } else {
        node.labelAlpha = node.labelTarget;
      }
      if (node.appear < 1) { node.appear = Math.min(1, node.appear + 0.08); labelsMoving = true; }
    }

    this.draw();
    this.frame++;

    if (this.alpha > 0.004 || labelsMoving || this.dragging || this.timeline.playing) {
      requestAnimationFrame((next) => this.loop(next));
    } else {
      this.running = false;   // idle: the loop stops until something wakes it up
    }
  }

  preRun(ticks) {
    this.alpha = 1;
    for (let i = 0; i < ticks; i++) this.tick();
    this.alpha = 0;
    for (const node of this.nodes) { node.appear = 1; node.labelAlpha = this.wantLabel(node) ? 1 : 0; }
  }

  tick() {
    const nodes = this.nodes.filter((node) => this.visible(node));
    if (!nodes.length) { this.alpha *= 0.9; return; }

    const forces = this.state.forces;
    const repel = (forces.repel / 100) * 3000;
    const gravity = (forces.center / 100) * 0.12;
    const strength = (forces.linkStrength / 100) * 0.9;
    const distance = forces.linkDistance;

    for (const node of nodes) {
      node.fx = -node.x * gravity;
      node.fy = -node.y * gravity;
    }

    const tree = buildTree(nodes);
    for (const node of nodes) accumulate(tree, node, repel, this.radius(node));

    for (const edge of this.edges) {
      if (edge.run > this.timeline.k || !this.visible(edge.a) || !this.visible(edge.b)) continue;
      const dx = edge.b.x - edge.a.x, dy = edge.b.y - edge.a.y;
      const length = Math.hypot(dx, dy) || 1;
      const pull = ((length - distance) / length) * strength;
      edge.a.fx += dx * pull;
      edge.a.fy += dy * pull;
      edge.b.fx -= dx * pull;
      edge.b.fy -= dy * pull;
    }

    for (const node of nodes) {
      if (!node.placed) {
        // New pages appear next to a neighbour already on screen, so growth reads as growth.
        const anchor = [...node.neighbors].map((id) => this.byId.get(id)).find((n) => n && n.placed && this.visible(n));
        if (anchor) {
          node.x = anchor.x + (Math.random() - 0.5) * 40;
          node.y = anchor.y + (Math.random() - 0.5) * 40;
        }
        node.placed = true;
        node.appear = this.reducedMotion ? 1 : 0;
      }
      if (node === this.dragging || node.pinned) continue;
      node.vx = (node.vx + node.fx * this.alpha) * 0.85;
      node.vy = (node.vy + node.fy * this.alpha) * 0.85;
      node.x += clamp(node.vx, -12, 12);
      node.y += clamp(node.vy, -12, 12);
    }

    this.alpha *= 0.985;
  }

  // ---------- drawing ----------

  resizeCanvas() {
    const box = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.width = box.width || 1;
    this.height = box.height || 1;
    this.canvas.width = Math.round(this.width * dpr);
    this.canvas.height = Math.round(this.height * dpr);
    this.context.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  toScreen(node) {
    return {
      x: (node.x - this.view.x) * this.view.scale + this.width / 2,
      y: (node.y - this.view.y) * this.view.scale + this.height / 2,
    };
  }

  draw() {
    const context = this.context;
    context.clearRect(0, 0, this.width, this.height);
    context.fillStyle = this.colors.bg;
    context.fillRect(0, 0, this.width, this.height);

    const focus = this.hover || this.selected;
    const width = this.state.display.linkWidth / 100;

    // edges: one path per colour group
    const groups = new Map();
    for (const edge of this.edges) {
      if (edge.run > this.timeline.k || !this.visible(edge.a) || !this.visible(edge.b)) continue;
      const from = this.toScreen(edge.a), to = this.toScreen(edge.b);
      if (offscreen(from, this.width, this.height) && offscreen(to, this.width, this.height)) continue;
      const emphasis = focus && (edge.a === focus || edge.b === focus);
      const alpha = clamp((emphasis ? 1 : this.dimmed(edge.a) * this.dimmed(edge.b)) * 0.9, 0, 1);
      const accent = emphasis || (this.state.colorMode === "new" &&
        (edge.a.run > this.timeline.baseline || edge.b.run > this.timeline.baseline));
      const color = accent ? this.colors.accent : this.colors.edge;
      const key = `${color}|${Math.round(alpha * 20) / 20}|${emphasis ? 1 : 0}`;
      if (!groups.has(key)) groups.set(key, { color, alpha, lineWidth: emphasis ? Math.max(1, width) : width, lines: [] });
      groups.get(key).lines.push([from, to]);
    }

    for (const group of groups.values()) {
      context.globalAlpha = group.alpha;
      context.strokeStyle = group.color;
      context.lineWidth = group.lineWidth;
      context.beginPath();
      for (const [from, to] of group.lines) {
        context.moveTo(from.x, from.y);
        context.lineTo(to.x, to.y);
      }
      context.stroke();
    }

    if (this.state.display.arrows) drawArrows(this, context, focus);

    // nodes: one path per colour group
    const fills = new Map();
    for (const node of this.nodes) {
      if (!this.visible(node) || node.ghost) continue;
      const point = this.toScreen(node);
      if (offscreen(point, this.width, this.height)) continue;
      const alpha = clamp(this.dimmed(node) * node.appear, 0, 1);
      const color = node === focus ? this.colors.accent : this.nodeColor(node);
      const key = `${color}|${Math.round(alpha * 20) / 20}`;
      if (!fills.has(key)) fills.set(key, { color, alpha, circles: [] });
      fills.get(key).circles.push({ point, radius: this.radius(node) * node.appear });
    }

    for (const group of fills.values()) {
      context.globalAlpha = group.alpha;
      context.fillStyle = group.color;
      context.beginPath();
      for (const circle of group.circles) {
        context.moveTo(circle.point.x + circle.radius, circle.point.y);
        context.arc(circle.point.x, circle.point.y, circle.radius, 0, Math.PI * 2);
      }
      context.fill();
    }

    // rings: thin outline on the largest nodes, accent ring on the selected one
    for (const node of this.nodes) {
      if (!this.visible(node) || node.ghost || node.degree < 12) continue;
      const point = this.toScreen(node);
      context.globalAlpha = this.dimmed(node);
      context.strokeStyle = this.colors.nodeFill;
      context.lineWidth = 1;
      context.beginPath();
      context.arc(point.x, point.y, this.radius(node) * node.appear + 2, 0, Math.PI * 2);
      context.stroke();
    }

    for (const node of this.nodes) {
      if (!this.visible(node)) continue;
      const point = this.toScreen(node);
      const size = this.radius(node) * node.appear;
      if (node.ghost) {
        context.globalAlpha = this.dimmed(node) * node.appear;
        context.setLineDash([3, 3]);
        context.strokeStyle = this.colors.muted;
        context.lineWidth = 1;
        context.beginPath();
        context.arc(point.x, point.y, size, 0, Math.PI * 2);
        context.stroke();
        context.setLineDash([]);
      } else if (node === this.selected) {
        ring(context, point, size + 3, this.colors.accent, 2);
      } else if (node === this.hover) {
        ring(context, point, size + 2, this.colors.text, 1);
      }
    }

    // labels, only where they are wanted
    context.globalAlpha = 1;
    context.font = "11px system-ui, -apple-system, sans-serif";
    context.textBaseline = "top";
    context.lineJoin = "round";
    for (const node of this.nodes) {
      if (!this.visible(node) || node.labelAlpha <= 0.02) continue;
      const point = this.toScreen(node);
      if (offscreen(point, this.width, this.height)) continue;
      const size = this.radius(node) * node.appear;
      const at = { x: point.x - size - 2, y: point.y + size + 3 };
      context.globalAlpha = node.labelAlpha * this.dimmed(node);
      context.strokeStyle = this.colors.bg;
      context.lineWidth = 3;
      context.strokeText(node.title, at.x, at.y);
      context.fillStyle = node === focus ? this.colors.text : this.colors.muted;
      context.fillText(node.title, at.x, at.y);
    }
    context.globalAlpha = 1;
  }

  wantLabel(node) {
    const focus = this.hover || this.selected;
    if (node === focus) return true;
    if (focus && focus.neighbors.has(node.id)) return true;
    if (this.query && this.matches.has(node.id)) return true;
    if (this.view.scale > this.state.display.labelZoom / 100) return true;
    return node.topLabel;
  }

  // ---------- pointer and keyboard ----------

  pointerDown(event) {
    this.pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });
    if (this.pointers.size === 2) {
      this.pinch = { distance: distance([...this.pointers.values()]), scale: this.view.scale };
      return;
    }
    const node = this.nodeAt(event.offsetX, event.offsetY);
    if (node) {
      this.dragging = node;
      this.dragMoved = false;
      this.canvas.setPointerCapture(event.pointerId);
      this.reheat(0.4);
    } else {
      this.pan = { x: event.offsetX, y: event.offsetY, viewX: this.view.x, viewY: this.view.y, moved: false };
    }
  }

  pointerMove(event) {
    if (this.pointers.has(event.pointerId)) this.pointers.set(event.pointerId, { x: event.offsetX, y: event.offsetY });

    if (this.pointers.size === 2 && this.pinch) {
      const now = distance([...this.pointers.values()]);
      this.view.scale = clamp(this.pinch.scale * (now / this.pinch.distance), 0.05, 8);
      this.draw();
      return;
    }

    if (this.dragging) {
      const point = this.toScreen(this.dragging);
      const dx = event.offsetX - point.x, dy = event.offsetY - point.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) this.dragMoved = true;
      this.dragging.x += dx / this.view.scale;
      this.dragging.y += dy / this.view.scale;
      this.dragging.vx = 0;
      this.dragging.vy = 0;
      this.reheat(0.3);
      this.draw();
      return;
    }

    if (this.pan) {
      const dx = event.offsetX - this.pan.x, dy = event.offsetY - this.pan.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) this.pan.moved = true;
      this.view.x = this.pan.viewX - dx / this.view.scale;
      this.view.y = this.pan.viewY - dy / this.view.scale;
      this.draw();
      return;
    }

    const node = this.nodeAt(event.offsetX, event.offsetY);
    if (node !== this.hover) {
      this.hover = node;
      this.reheat(0.02);
    }
    if (node) this.showTooltip(node, event);
    this.draw();
  }

  pointerUp(event) {
    this.pointers.delete(event.pointerId);
    if (this.pointers.size < 2) this.pinch = null;

    if (this.dragging) {
      if (!this.dragMoved) this.select(this.dragging.id);
      else if (event.shiftKey) this.dragging.pinned = true;   // Shift keeps it where it was put
      this.dragging = null;
      this.dragMoved = false;
      this.reheat(0.3);
      return;
    }
    if (this.pan && !this.pan.moved) this.select(null);
    this.pan = null;
  }

  zoomAt(event) {
    event.preventDefault();
    const before = this.worldFromScreen(event.offsetX, event.offsetY);
    this.view.scale = clamp(this.view.scale * Math.exp(-event.deltaY * 0.0015), 0.05, 8);
    const after = this.worldFromScreen(event.offsetX, event.offsetY);
    this.view.x += before.x - after.x;
    this.view.y += before.y - after.y;
    this.draw();
  }

  worldFromScreen(px, py) {
    return { x: (px - this.width / 2) / this.view.scale + this.view.x,
             y: (py - this.height / 2) / this.view.scale + this.view.y };
  }

  nodeAt(px, py) {
    let best = null, bestDistance = Infinity;
    for (const node of this.nodes) {
      if (!this.visible(node)) continue;
      const point = this.toScreen(node);
      const distance = Math.hypot(point.x - px, point.y - py);
      if (distance <= this.radius(node) * node.appear + 4 && distance < bestDistance) {
        best = node;
        bestDistance = distance;
      }
    }
    return best;
  }

  fitAll() {
    const nodes = this.nodes.filter((node) => this.visible(node));
    if (!nodes.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of nodes) {
      const margin = this.radius(node) + 24;
      minX = Math.min(minX, node.x - margin);
      maxX = Math.max(maxX, node.x + margin);
      minY = Math.min(minY, node.y - margin);
      maxY = Math.max(maxY, node.y + margin);
    }
    this.view.x = (minX + maxX) / 2;
    this.view.y = (minY + maxY) / 2;
    this.view.scale = clamp(Math.min(this.width / (maxX - minX), this.height / (maxY - minY)), 0.05, 8);
    this.draw();
  }

  focusNode(id) {
    const node = this.byId.get(id);
    if (!node) return;
    this.select(id);
    if (this.reducedMotion) {
      this.view.x = node.x;
      this.view.y = node.y;
      this.view.scale = Math.max(this.view.scale, 1.2);
      this.draw();
      return;
    }
    const from = { ...this.view };
    const to = { x: node.x, y: node.y, scale: Math.max(this.view.scale, 1.4) };
    const started = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - started) / 350);
      const ease = t * (2 - t);
      this.view.x = from.x + (to.x - from.x) * ease;
      this.view.y = from.y + (to.y - from.y) * ease;
      this.view.scale = from.scale + (to.scale - from.scale) * ease;
      this.draw();
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ---------- selection, search, tooltip ----------

  select(id) {
    this.selected = id ? this.byId.get(id) || null : null;
    if (!this.compact && this.selected) {
      const panel = this.root.querySelector("#graph-side");
      panel.hidden = false;
      renderSidePanel(this, panel, this.selected);
    } else if (!this.compact) {
      this.root.querySelector("#graph-side").hidden = true;
    }
    this.draw();
  }

  setSubset(ids) {
    this.subset = new Set(ids);
    const centre = this.byId.get(ids[0]);
    if (!centre) return;
    for (const node of this.nodes) {
      if (!this.subset.has(node.id)) continue;
      if (node === centre) { node.x = 0; node.y = 0; }
      else {
        const angle = Math.random() * Math.PI * 2;
        node.x = Math.cos(angle) * 90;
        node.y = Math.sin(angle) * 90;
      }
      node.placed = true;
      node.appear = 1;
    }
    this.select(ids[0]);
    this.reheat(0.6);
    if (this.reducedMotion) this.preRun(120);
    this.fitAll();
  }

  setQuery(text) {
    this.query = text.trim().toLowerCase();
    this.matches = new Set();
    if (!this.query) { this.draw(); return; }
    for (const node of this.nodes) {
      if (node.title.toLowerCase().includes(this.query) || node.description.toLowerCase().includes(this.query)) {
        this.matches.add(node.id);
      }
    }
    if (!this.matches.size) this.contentSearch(this.query);
    this.draw();
  }

  async contentSearch(query) {
    try {
      const data = await api(`/api/wiki/search?q=${encodeURIComponent(query)}`);
      for (const hit of data.results) {
        const slug = hit.file.split("/").pop().replace(".md", "");
        if (this.byId.has(slug)) this.matches.add(slug);
      }
      this.draw();
    } catch (error) {
      // search is best effort
    }
  }

  showTooltip(node, event) {
    if (this.compact) return;
    const tip = this.root.querySelector("#graph-tooltip");
    if (!node) { tip.hidden = true; return; }
    tip.hidden = false;
    const links = node.ghost ? `${node.degree} references` : `${node.degree} links`;
    const firstLine = node.description || (node.ghost ? "no page with this name" : "no text yet");
    tip.innerHTML = `<strong>${escapeHtml(node.title)}</strong>` +
      `<span class="line">${escapeHtml(firstLine)}</span>` +
      `<span class="meta">${escapeHtml(node.id)} · ${escapeHtml(links)} · updated ` +
      `${escapeHtml(node.updated ? node.updated.slice(0, 10) : "unknown")}</span>`;
    const box = this.root.getBoundingClientRect();
    tip.style.left = `${clamp(event.clientX - box.left + 14, 8, this.width - 260)}px`;
    tip.style.top = `${clamp(event.clientY - box.top + 14, 8, this.height - 90)}px`;
  }

  // ---------- growth timeline ----------

  buildTimeline() {
    if (this.compact) return;
    const row = this.root.querySelector("#graph-timeline");
    const total = Math.max(1, this.data.stats.runs);

    row.innerHTML = `<button class="icon-btn" id="graph-play" data-icon="play" title="Play the growth" aria-label="Play the growth"></button>
      <label for="graph-run">Dreaming runs</label>
      <input id="graph-run" type="range" min="0" max="${total - 1}" value="${this.timeline.k}" aria-label="Show the wiki as it was after run">
      <output id="graph-run-value">${this.timeline.k}</output>
      <label for="graph-baseline">Highlight added since run</label>
      <input id="graph-baseline" type="number" min="0" max="${total - 1}" value="${this.timeline.baseline}" aria-label="Highlight added since run">
      ${this.data.stats.from_git ? "" : "<span class='approx'>runs from file times</span>"}`;

    const play = row.querySelector("#graph-play");
    play.innerHTML = icon("play", 16);
    const runInput = row.querySelector("#graph-run");
    const runValue = row.querySelector("#graph-run-value");
    const baseline = row.querySelector("#graph-baseline");

    runInput.addEventListener("input", () => {
      this.timeline.k = Number(runInput.value);
      runValue.textContent = this.timeline.k;
      this.drawCaption();
      this.reheat(0.5);
    });
    baseline.addEventListener("change", () => {
      this.timeline.baseline = clamp(Number(baseline.value), 0, total - 1);
      baseline.value = this.timeline.baseline;
      this.draw();
    });
    play.addEventListener("click", () => {
      this.timeline.playing = !this.timeline.playing;
      play.classList.toggle("on", this.timeline.playing);
      play.title = this.timeline.playing ? "Pause" : "Play the growth";
      if (this.timeline.playing) {
        this.timeline.k = 0;
        this.timeline.stepAt = performance.now();
        runInput.value = 0;
        runValue.textContent = 0;
        this.drawCaption();
        this.reheat(0.6);
      }
    });
  }

  stepTimeline(now) {
    const total = Math.max(1, this.data.stats.runs);
    if (now - (this.timeline.stepAt || 0) < 1000) return;
    this.timeline.stepAt = now;
    if (this.timeline.k >= total - 1) {
      this.timeline.playing = false;
      const play = this.root.querySelector("#graph-play");
      if (play) { play.classList.remove("on"); play.title = "Play the growth"; }
      return;
    }
    this.timeline.k += 1;
    const input = this.root.querySelector("#graph-run");
    if (input) {
      input.value = this.timeline.k;
      this.root.querySelector("#graph-run-value").textContent = this.timeline.k;
    }
    this.drawCaption();
    this.alpha = Math.max(this.alpha, 0.35);
  }

  drawCaption() {
    const caption = this.root.querySelector(".graph-caption");
    if (!caption) return;
    const pages = this.nodes.filter((node) => !node.ghost && node.run <= this.timeline.k).length;
    const links = this.edges.filter((edge) => edge.run <= this.timeline.k).length;
    caption.textContent = `${pages} pages, ${links} links`;
    const legend = this.root.querySelector("#graph-legend");
    if (legend) legend.innerHTML = this.legend().innerHTML;
  }
}

// ---------- helpers ----------

function ring(context, point, size, color, lineWidth) {
  context.globalAlpha = 1;
  context.strokeStyle = color;
  context.lineWidth = lineWidth;
  context.beginPath();
  context.arc(point.x, point.y, size, 0, Math.PI * 2);
  context.stroke();
}

function drawArrows(view, context, focus) {
  const width = view.state.display.linkWidth / 100;
  for (const edge of view.edges) {
    if (edge.run > view.timeline.k || !view.visible(edge.a) || !view.visible(edge.b)) continue;
    const from = view.toScreen(edge.a), to = view.toScreen(edge.b);
    if (offscreen(from, view.width, view.height) && offscreen(to, view.width, view.height)) continue;
    const emphasis = focus && (edge.a === focus || edge.b === focus);
    context.globalAlpha = clamp((emphasis ? 1 : view.dimmed(edge.a) * view.dimmed(edge.b)) * 0.8, 0, 1);
    context.strokeStyle = emphasis ? view.colors.accent : view.colors.edge;
    context.lineWidth = width;
    const angle = Math.atan2(to.y - from.y, to.x - from.x);
    const tip = { x: to.x - Math.cos(angle) * (view.radius(edge.b) + 3), y: to.y - Math.sin(angle) * (view.radius(edge.b) + 3) };
    context.beginPath();
    context.moveTo(tip.x, tip.y);
    context.lineTo(tip.x - Math.cos(angle - 0.4) * 5, tip.y - Math.sin(angle - 0.4) * 5);
    context.moveTo(tip.x, tip.y);
    context.lineTo(tip.x - Math.cos(angle + 0.4) * 5, tip.y - Math.sin(angle + 0.4) * 5);
    context.stroke();
  }
  context.globalAlpha = 1;
}

function offscreen(point, width, height) {
  return point.x < -20 || point.y < -20 || point.x > width + 20 || point.y > height + 20;
}

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y) || 1;
}

// Barnes-Hut: one quadtree per tick. Distant cells act as their aggregate mass, close ones exactly.
function cell(x, y, size) {
  return { x, y, size, mass: 0, cx: 0, cy: 0, children: null, node: null };
}

function buildTree(nodes) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const node of nodes) {
    minX = Math.min(minX, node.x); maxX = Math.max(maxX, node.x);
    minY = Math.min(minY, node.y); maxY = Math.max(maxY, node.y);
  }
  const size = Math.max(1, Math.max(maxX - minX, maxY - minY));
  const root = cell(minX, minY, size);
  for (const node of nodes) insert(root, node);
  return root;
}

function insert(c, node) {
  if (!c.children) {
    if (!c.node) {
      c.node = node;
      c.mass = 1;
      c.cx = node.x;
      c.cy = node.y;
      return;
    }
    const existing = c.node;
    c.node = null;
    c.children = [null, null, null, null];
    insert(c, existing);
  }
  c.mass += 1;
  c.cx += node.x;
  c.cy += node.y;
  const half = c.size / 2;
  const quadrant = (node.x >= c.x + half ? 2 : 0) + (node.y >= c.y + half ? 1 : 0);
  if (!c.children[quadrant]) {
    c.children[quadrant] = cell(c.x + (quadrant & 2 ? half : 0), c.y + (quadrant & 1 ? half : 0), half);
  }
  insert(c.children[quadrant], node);
}

function accumulate(c, node, repel, radius) {
  if (!c || !c.mass) return;
  const cx = c.cx / c.mass, cy = c.cy / c.mass;
  const dx = node.x - cx, dy = node.y - cy;
  const d2 = dx * dx + dy * dy;

  if (!c.children) {
    if (!c.node || c.node === node) return;
    const minimum = radius + thisRadius(c.node);
    if (d2 < minimum * minimum) {
      const d = Math.sqrt(d2) || 0.01;
      const push = ((minimum - d) / d) * 0.6;
      node.fx += dx * push;
      node.fy += dy * push;
      return;
    }
  } else if (c.size * c.size / d2 >= 0.36) {
    for (const child of c.children) accumulate(child, node, repel, radius);
    return;
  }

  if (d2 < 1) return;
  const force = (repel * c.mass) / d2;
  const d = Math.sqrt(d2);
  node.fx += (dx / d) * force;
  node.fy += (dy / d) * force;
}

function thisRadius(node) {
  const size = MIN_R + (MAX_R - MIN_R) * Math.sqrt(node.degree / Math.max(1, node.maxDegree || 1));
  return size;
}

async function renderSidePanel(view, panel, node) {
  panel.innerHTML = "";

  const head = document.createElement("div");
  head.className = "panel-head";
  const title = document.createElement("strong");
  title.textContent = node.title;
  const close = document.createElement("button");
  close.className = "icon-btn";
  close.title = "Close panel";
  close.setAttribute("aria-label", "Close panel");
  close.innerHTML = icon("x", 16);
  close.addEventListener("click", () => view.select(null));
  head.append(title, close);

  const actions = document.createElement("div");
  actions.className = "panel-actions";
  for (const [label, action] of [["Open full page", () => view.onOpen(node.id)], ["Edit", () => view.onEdit(node.id)]]) {
    const button = document.createElement("button");
    button.className = "small-button";
    button.textContent = label;
    button.addEventListener("click", action);
    actions.append(button);
  }

  const body = document.createElement("div");
  body.className = "panel-page text";

  panel.append(head, actions);

  if (node.ghost) {
    body.innerHTML = `<p class="muted">No page with this name. Referenced by: ${escapeHtml(node.referencedBy.join(", "))}</p>`;
    panel.append(body);
    return;
  }

  try {
    const data = await api(`/api/wiki/page?name=${encodeURIComponent(node.id)}`);
    body.innerHTML = renderMarkdown(data.content, { wikiLinks: true });
    for (const link of body.querySelectorAll("a.wikilink")) {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        view.focusNode(link.getAttribute("href").replace("?p=", ""));
      });
    }
    const outgoing = document.createElement("div");
    const incoming = document.createElement("div");
    linkList(view, outgoing, "links to", [...node.neighbors]);
    linkList(view, incoming, "linked from", data.backlinks || []);
    panel.append(body, outgoing, incoming);
  } catch (error) {
    body.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    panel.append(body);
  }
}

function linkList(view, container, label, ids) {
  const block = document.createElement("div");
  block.className = "panel-links";
  const title = document.createElement("strong");
  title.textContent = label;
  block.append(title);
  if (!ids.length) {
    const empty = document.createElement("span");
    empty.className = "muted";
    empty.textContent = "nothing";
    block.append(empty);
  }
  for (const id of ids) {
    const link = document.createElement("a");
    link.href = `?p=${encodeURIComponent(id)}`;
    link.textContent = id;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      view.focusNode(id);
    });
    block.append(link);
  }
  container.append(block);
}
