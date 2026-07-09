let network = null;
let nodesDataSet = null;
let edgesDataSet = null;
let currentSubsystem = null;

const FORWARD_COLOR = "#4c956c";
const ZERO_COLOR = "#adb5bd";
const RXN_COLOR = "#264653";
const MET_COLOR = "#2a9d8f";

function loadSubsystems() {
  fetch("/api/subsystems")
    .then(r => r.json())
    .then(list => {
      const sel = document.getElementById("subsystemSelect");
      sel.innerHTML = "";
      for (const item of list) {
        const opt = document.createElement("option");
        opt.value = item.name;
        opt.textContent = `${item.name} (${item.n_reactions})`;
        sel.appendChild(opt);
      }
      if (list.length > 0) {
        currentSubsystem = list[0].name;
        sel.value = currentSubsystem;
        renderPathway(currentSubsystem);
      }
    });
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
 * Full (re)render: used when the user picks a different pathway. Creates a
 * fresh layout and fits the view, since node positions from the previous
 * pathway aren't meaningful here.
 */
function renderPathway(subsystem) {
  fetch(`/api/pathway_data?subsystem=${encodeURIComponent(subsystem)}`)
    .then(r => r.json())
    .then(data => {
      const nodes = data.nodes.map(toVisNode);
      const edges = data.edges.map(toVisEdge);

      nodesDataSet = new vis.DataSet(nodes);
      edgesDataSet = new vis.DataSet(edges);

      const options = {
        physics: {
          barnesHut: { gravitationalConstant: -8000, springLength: 120, springConstant: 0.03 },
          stabilization: { iterations: 150 },
        },
        interaction: { hover: true, tooltipDelay: 100 },
        layout: { improvedLayout: true },
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
 * pathway and updates node/edge colors and widths in place, without
 * recreating the network — so the user's current zoom and pan are kept.
 */
function refreshData() {
  if (!currentSubsystem || !nodesDataSet || !edgesDataSet) {
    return;
  }
  const btn = document.getElementById("refreshBtn");
  btn.disabled = true;
  btn.textContent = "Refreshing...";

  fetch(`/api/pathway_data?subsystem=${encodeURIComponent(currentSubsystem)}`)
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

document.getElementById("subsystemSelect").addEventListener("change", (e) => {
  currentSubsystem = e.target.value;
  renderPathway(currentSubsystem);
});

document.getElementById("refreshBtn").addEventListener("click", refreshData);

loadSubsystems();
