let network = null;
let nodesDataSet = null;
let edgesDataSet = null;
let currentSubsystems = [];

const FORWARD_COLOR = "#4c956c";
const ZERO_COLOR = "#adb5bd";
const RXN_COLOR = "#264653";
const MET_COLOR = "#2a9d8f";
const LARGE_GRAPH_NODES = 400;

function loadSubsystems() {
  fetch("/api/subsystems")
    .then(r => r.json())
    .then(list => {
      const listEl = document.getElementById("subsystemList");
      listEl.innerHTML = "";
      list.forEach((item, i) => {
        const wrapper = document.createElement("div");
        wrapper.className = "form-check";
        const box = document.createElement("input");
        box.type = "checkbox";
        box.className = "form-check-input subsystem-check";
        box.id = `subsystem-${i}`;
        box.value = item.name;
        box.checked = i === 0;
        const label = document.createElement("label");
        label.className = "form-check-label small";
        label.htmlFor = box.id;
        label.textContent = `${item.name} (${item.n_reactions})`;
        wrapper.append(box, label);
        listEl.appendChild(wrapper);
      });
      applySelection();
    });
}

function checkedSubsystems() {
  return [...document.querySelectorAll(".subsystem-check:checked")].map(b => b.value);
}

function setAllChecked(checked) {
  document.querySelectorAll(".subsystem-check").forEach(b => { b.checked = checked; });
}

function updateToggleLabel() {
  const n = currentSubsystems.length;
  document.getElementById("subsystemToggle").textContent =
    n === 0 ? "No pathway selected" : n === 1 ? currentSubsystems[0] : `${n} pathways selected`;
}

function applySelection() {
  currentSubsystems = checkedSubsystems();
  updateToggleLabel();
  renderPathway(currentSubsystems);
}

function pathwayUrl(subsystems) {
  const params = new URLSearchParams();
  subsystems.forEach(s => params.append("subsystem", s));
  return `/api/pathway_data?${params}`;
}

function edgeColor(active) {
  return active ? FORWARD_COLOR : ZERO_COLOR;
}

function toVisNode(n) {
  if (n.type === "reaction") {
    const flux = n.flux || 0;
    const magnitude = Math.min(1, Math.abs(flux) / 10);
    return {
      id: n.id,
      label: n.label,
      title: n.title,
      shape: "box",
      color: {
        background: Math.abs(flux) < 1e-9 ? "#f1f3f5" : (flux > 0 ? "#d8f3dc" : "#ffe5d9"),
        border: RXN_COLOR,
      },
      font: { size: 12, color: "#212529" },
      borderWidth: 1 + 2 * magnitude,
    };
  }
  return {
    id: n.id,
    label: n.label,
    title: n.title,
    shape: "ellipse",
    color: { background: "#e8f6f3", border: MET_COLOR },
    font: { size: 11, color: "#212529" },
  };
}

function toVisEdge(e) {
  return {
    id: `${e.from}__${e.to}`,
    from: e.from,
    to: e.to,
    arrows: "to",
    color: { color: edgeColor(e.active), opacity: e.active ? 0.9 : 0.35 },
    width: e.active ? Math.min(8, 1 + e.flux * 0.8) : 1,
    smooth: { type: "dynamic" },
  };
}

/**
 * Full (re)render: used when the user picks different pathways. Creates a
 * fresh layout and fits the view, since node positions from the previous
 * selection aren't meaningful here.
 */
function renderPathway(subsystems) {
  fetch(pathwayUrl(subsystems))
    .then(r => r.json())
    .then(data => {
      const nodes = data.nodes.map(toVisNode);
      const edges = data.edges.map(toVisEdge);

      nodesDataSet = new vis.DataSet(nodes);
      edgesDataSet = new vis.DataSet(edges);

      // improvedLayout gets very slow on big graphs (e.g. "All" pathways)
      const large = nodes.length > LARGE_GRAPH_NODES;
      const options = {
        physics: {
          barnesHut: { gravitationalConstant: -3000, springLength: 70, springConstant: 0.03},
          stabilization: { iterations: large ? 300 : 150 },
        },
        interaction: { hover: true, tooltipDelay: 100 },
        layout: { improvedLayout: !large },
      };

      const container = document.getElementById("network");
      if (network) {
        network.destroy();
      }
      network = new vis.Network(container, { nodes: nodesDataSet, edges: edgesDataSet }, options);
    });
}

/**
 * Data-only refresh: re-fetches flux values for the currently displayed
 * pathways and updates node/edge colors and widths in place, without
 * recreating the network — so the user's current zoom and pan are kept.
 */
function refreshData() {
  if (!currentSubsystems.length || !nodesDataSet || !edgesDataSet) {
    return;
  }
  const btn = document.getElementById("refreshBtn");
  btn.disabled = true;
  btn.textContent = "Refreshing...";

  fetch(pathwayUrl(currentSubsystems))
    .then(r => r.json())
    .then(data => {
      const newNodeIds = new Set(data.nodes.map(n => n.id));
      const newEdgeIds = new Set(data.edges.map(e => `${e.from}__${e.to}`));

      // Update/insert current nodes & edges
      nodesDataSet.update(data.nodes.map(toVisNode));
      edgesDataSet.update(data.edges.map(toVisEdge));

      // Remove any nodes/edges that no longer exist (e.g. model was edited)
      const staleNodes = nodesDataSet.getIds().filter(id => !newNodeIds.has(id));
      const staleEdges = edgesDataSet.getIds().filter(id => !newEdgeIds.has(id));
      if (staleNodes.length) nodesDataSet.remove(staleNodes);
      if (staleEdges.length) edgesDataSet.remove(staleEdges);
    })
    .finally(() => {
      btn.disabled = false;
      btn.textContent = "⟳ Refresh";
    });
}

document.getElementById("selectAllBtn").addEventListener("click", () => setAllChecked(true));
document.getElementById("selectNoneBtn").addEventListener("click", () => setAllChecked(false));
document.getElementById("showBtn").addEventListener("click", () => {
  applySelection();
  bootstrap.Dropdown.getOrCreateInstance(document.getElementById("subsystemToggle")).hide();
});

document.getElementById("refreshBtn").addEventListener("click", refreshData);

loadSubsystems();
