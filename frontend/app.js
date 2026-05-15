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
