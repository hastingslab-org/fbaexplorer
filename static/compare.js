let compareTable = null;

function fmtDate(iso) {
  try {
    return new Date(iso).toLocaleString();
  } catch (e) {
    return iso;
  }
}

function renderRunCards(runs) {
  const container = document.getElementById("runCards");
  container.innerHTML = "";
  for (const run of runs) {
    const col = document.createElement("div");
    col.className = "col-md-6 col-lg-4";
    col.innerHTML = `
      <div class="card run-card h-100">
        <div class="card-body">
          <h6 class="card-title mb-1">${run.name}</h6>
          <div class="small text-muted mb-2">${fmtDate(run.created_at)}</div>
          <div class="small">
            <div><strong>Model:</strong> ${run.model_id || "n/a"} <span class="text-muted">(${run.model_source || ""})</span></div>
            <div><strong>Objective:</strong> ${run.objective_reaction || "n/a"} (${run.objective_direction || ""})</div>
            <div><strong>Objective value:</strong> ${run.objective_value !== null ? Number(run.objective_value).toPrecision(6) : "n/a"}</div>
            <div><strong>Status:</strong> <span class="badge bg-${run.status === "optimal" ? "success" : "danger"}">${run.status || ""}</span></div>
            <div><strong>Gene knockouts (${run.knockouts.length}):</strong> ${run.knockouts.join(", ") || "none"}</div>
            ${run.notes ? `<div class="mt-1"><strong>Notes:</strong> ${run.notes}</div>` : ""}
          </div>
        </div>
      </div>
    `;
    container.appendChild(col);
  }
}

function buildTable(data) {
  const runs = data.runs;
  const headerRow = document.getElementById("compareHeaderRow");
  headerRow.innerHTML = `<th>Reaction ID</th><th>Name</th><th>Subsystem</th>` +
    runs.map(r => `<th>Flux — ${r.name}</th>`).join("");

  const tbody = document.querySelector("#compareTable tbody");
  tbody.innerHTML = "";

  for (const rxn of data.reactions) {
    const values = runs.map(r => rxn.fluxes[r.run_uuid]);
    const present = values.filter(v => v !== null && v !== undefined);
    const differs = present.length > 1 && (Math.max(...present) - Math.min(...present) > 1e-6);

    const tr = document.createElement("tr");
    tr.dataset.differs = differs ? "1" : "0";
    let cells = `<td>${rxn.reaction_id}</td><td>${rxn.name}</td><td>${rxn.subsystem}</td>`;
    for (const r of runs) {
      const v = rxn.fluxes[r.run_uuid];
      const display = v === null || v === undefined ? "—" : Number(v).toPrecision(4);
      cells += `<td class="${differs ? "differs" : ""}" data-order="${v ?? ""}">${display}</td>`;
    }
    tr.innerHTML = cells;
    tbody.appendChild(tr);
  }

  if (compareTable) compareTable.destroy();
  compareTable = $("#compareTable").DataTable({ pageLength: 25, order: [] });

  $.fn.dataTable.ext.search.push((settings, searchData, index, rowData, counter) => {
    if (settings.nTable.id !== "compareTable") return true;
    const onlyDiffering = document.getElementById("onlyDiffering").checked;
    if (!onlyDiffering) return true;
    const row = compareTable.row(index).node();
    return row && row.dataset.differs === "1";
  });

  document.getElementById("onlyDiffering").addEventListener("change", () => compareTable.draw());
}

function load() {
  if (!RUN_IDS) return;
  document.getElementById("exportLink").href = `/api/runs/export?ids=${encodeURIComponent(RUN_IDS)}`;
  fetch(`/api/compare?ids=${encodeURIComponent(RUN_IDS)}`)
    .then(r => r.json())
    .then(data => {
      renderRunCards(data.runs);
      buildTable(data);
    });
}

load();
