// Vertex AI Usage Dashboard — frontend logic.
//
// Flow:
//   1. On page load, fetch /api/projects → populate the project dropdown.
//   2. Fetch /api/metrics for the selected project + timeframe → render
//      stat cards, doughnuts, and the table.
//   3. When the user changes either dropdown or clicks Refresh, re-fetch.

const state = {
  projectId: null,
  timeframe: "24h",
  charts: {}, // canvas id → Chart.js instance (so we can update in place)
};

// Stable color palette — Chart.js will cycle through these for slices.
// Hand-picked to look reasonable on the dark theme without being garish.
const PALETTE = [
  "#4c8bf5", "#34a853", "#fbbc04", "#ea4335", "#a142f4",
  "#24c1e0", "#f06292", "#9ccc65", "#ffb74d", "#7e57c2",
];

// ---------- Boot ----------

window.addEventListener("DOMContentLoaded", init);

async function init() {
  bindEvents();
  await loadProjects();
  if (state.projectId) await refresh();
}

function bindEvents() {
  document.getElementById("project-select").addEventListener("change", (e) => {
    state.projectId = e.target.value;
    refresh();
  });
  document.getElementById("timeframe-select").addEventListener("change", (e) => {
    state.timeframe = e.target.value;
    refresh();
  });
  document.getElementById("refresh-btn").addEventListener("click", refresh);
}

// ---------- Data fetching ----------

async function loadProjects() {
  showStatus("Loading projects…", "info");
  try {
    const res = await fetch("/api/projects");
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);

    const select = document.getElementById("project-select");
    select.innerHTML = "";
    for (const p of body) {
      const opt = document.createElement("option");
      opt.value = p.project_id;
      opt.textContent = `${p.display_name} (${p.project_id})`;
      select.appendChild(opt);
    }

    if (body.length === 0) {
      showStatus("No accessible GCP projects found.");
      return;
    }
    state.projectId = body[0].project_id;
    hideStatus();
  } catch (err) {
    showStatus(`Failed to load projects: ${err.message}`);
  }
}

async function refresh() {
  if (!state.projectId) return;
  setRefreshing(true);
  showStatus(`Loading metrics for ${state.projectId} (${state.timeframe})…`, "info");

  try {
    const url = `/api/metrics?project_id=${encodeURIComponent(
      state.projectId
    )}&timeframe=${encodeURIComponent(state.timeframe)}`;
    const res = await fetch(url);
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);

    renderStatCards(body);
    renderDoughnut("chart-model", body.by_model);
    renderDoughnut("chart-region", body.by_region);
    renderDoughnut("chart-response", body.by_response_code);
    renderTable(body.per_model_table);
    renderAvgTokensPerQuery(body.avg_tokens_per_query_by_model);
    renderTokensPerDay(body.tokens_per_day_by_model, body.timeframe);
    renderMeta(body);
    hideStatus();
  } catch (err) {
    showStatus(`Failed to load metrics: ${err.message}`);
  } finally {
    setRefreshing(false);
  }
}

// ---------- Renderers ----------

function renderStatCards(d) {
  document.getElementById("stat-total").textContent = fmt(d.total_queries);
  document.getElementById("stat-daily").textContent = fmt(d.daily_average);
  document.getElementById("stat-peak").textContent = fmt(d.peak_queries_per_minute);
  document.getElementById("stat-avg").textContent = fmt(d.avg_queries_per_minute);
}

function renderDoughnut(canvasId, percentByGroup) {
  const labels = Object.keys(percentByGroup);
  const values = labels.map((k) => percentByGroup[k]);
  const colors = labels.map((_, i) => PALETTE[i % PALETTE.length]);

  const config = {
    type: "doughnut",
    data: {
      labels,
      datasets: [
        {
          data: values,
          backgroundColor: colors,
          borderColor: "#161b22",
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { color: "#e6edf3", boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.label}: ${ctx.parsed}%`,
          },
        },
      },
    },
  };

  // Reuse the existing chart instance if there is one — calling new Chart()
  // on the same canvas without destroy() leaks the previous instance and
  // causes flicker/double-render.
  const existing = state.charts[canvasId];
  if (existing) {
    existing.data.labels = labels;
    existing.data.datasets[0].data = values;
    existing.data.datasets[0].backgroundColor = colors;
    existing.update();
  } else {
    state.charts[canvasId] = new Chart(document.getElementById(canvasId), config);
  }
}

// Generic bar-chart helper. Same create-or-update pattern as renderDoughnut
// — never call new Chart() on a canvas that already has an instance, or
// Chart.js leaks the previous one and tooltips behave strangely.
function renderBar(canvasId, { labels, datasets, stacked = false }) {
  const config = {
    type: "bar",
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { color: "#e6edf3", boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label || ""}: ${ctx.parsed.y.toLocaleString()}`,
          },
        },
      },
      scales: {
        x: { stacked, ticks: { color: "#8b949e" }, grid: { color: "#2a313a" } },
        y: {
          stacked,
          beginAtZero: true,
          ticks: { color: "#8b949e", callback: (v) => v.toLocaleString() },
          grid: { color: "#2a313a" },
        },
      },
    },
  };

  const existing = state.charts[canvasId];
  if (existing) {
    existing.data.labels = labels;
    existing.data.datasets = datasets;
    existing.options.scales.x.stacked = stacked;
    existing.options.scales.y.stacked = stacked;
    existing.update();
  } else {
    state.charts[canvasId] = new Chart(document.getElementById(canvasId), config);
  }
}

// Toggle a chart canvas vs. an inline "No data" message inside its card.
// We keep the existing Chart.js instance untouched (just hide the canvas),
// so when data returns the next render reuses it and avoids leaks.
function setBarEmptyState(canvasId, isEmpty) {
  const canvas = document.getElementById(canvasId);
  const card = canvas.closest(".chart-card");
  let msg = card.querySelector(".empty-state");
  if (isEmpty) {
    canvas.classList.add("hidden");
    if (!msg) {
      msg = document.createElement("p");
      msg.className = "empty-state";
      msg.textContent = "No data in this timeframe.";
      card.appendChild(msg);
    } else {
      msg.classList.remove("hidden");
    }
  } else {
    canvas.classList.remove("hidden");
    if (msg) msg.classList.add("hidden");
  }
}

function renderAvgTokensPerQuery(byModel) {
  // byModel = {model_name: avg_tokens_per_query}
  // Single dataset, one bar per model. Sort descending so the heaviest
  // model is on the left.
  const sorted = Object.entries(byModel || {}).sort((a, b) => b[1] - a[1]);
  if (sorted.length === 0) {
    setBarEmptyState("chart-avg-tokens", true);
    return;
  }
  setBarEmptyState("chart-avg-tokens", false);

  const labels = sorted.map(([m]) => m);
  const values = sorted.map(([, v]) => v);
  const colors = labels.map((_, i) => PALETTE[i % PALETTE.length]);

  renderBar("chart-avg-tokens", {
    labels,
    datasets: [
      {
        label: "Avg Tokens / Query",
        data: values,
        backgroundColor: colors,
        borderColor: "#161b22",
        borderWidth: 1,
      },
    ],
  });
}

function renderTokensPerDay(payload, timeframe) {
  // Only show on week+/month timeframes (per the spec).
  const card = document.getElementById("tokens-per-day-card");
  const showOnTimeframes = ["7d", "30d"];
  if (!showOnTimeframes.includes(timeframe)) {
    card.classList.add("hidden");
    return;
  }
  card.classList.remove("hidden");

  // payload = {days: [...], models: [...], matrix: {model: [day_values...]}}
  // One Chart.js dataset per model, all sharing the `days` axis. By default
  // Chart.js renders multiple bar datasets side-by-side (grouped) — exactly
  // "a new bar per model" per day.
  const days = payload?.days || [];
  const models = payload?.models || [];
  const matrix = payload?.matrix || {};

  if (days.length === 0 || models.length === 0) {
    setBarEmptyState("chart-tokens-per-day", true);
    return;
  }
  setBarEmptyState("chart-tokens-per-day", false);

  const datasets = models.map((model, i) => ({
    label: model,
    data: matrix[model] || [],
    backgroundColor: PALETTE[i % PALETTE.length],
    borderColor: "#161b22",
    borderWidth: 1,
  }));

  renderBar("chart-tokens-per-day", { labels: days, datasets });
}

function renderTable(rows) {
  const tbody = document.querySelector("#model-table tbody");
  tbody.innerHTML = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="6" style="color:var(--muted);text-align:center">No data in this timeframe.</td>`;
    tbody.appendChild(tr);
    return;
  }
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(r.model_name)}</td>
      <td>${fmt(r.peak_input_tokens_per_min)}</td>
      <td>${fmt(r.avg_input_tokens_per_min)}</td>
      <td>${fmt(r.peak_queries_per_min)}</td>
      <td>${fmt(r.avg_queries_per_min)}</td>
      <td>${fmt(r.total_queries)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderMeta(d) {
  document.getElementById(
    "meta"
  ).textContent = `Project ${d.project_id} • ${d.timeframe} • fetched in ${d.elapsed_ms} ms`;
}

// ---------- Helpers ----------

function fmt(n) {
  if (n === null || n === undefined) return "—";
  if (typeof n === "number") return n.toLocaleString();
  return String(n);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function showStatus(msg, kind = "error") {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.classList.remove("hidden", "info");
  if (kind === "info") el.classList.add("info");
}

function hideStatus() {
  document.getElementById("status").classList.add("hidden");
}

function setRefreshing(busy) {
  document.getElementById("refresh-btn").disabled = busy;
}
