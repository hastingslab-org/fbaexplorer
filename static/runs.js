let runsTable = null;

function fmtDate(iso) {
  try {
    return new Date(iso).toLocaleString();
  } catch (e) {
    return iso;
  }
}

function loadRuns() {
  fetch("/api/runs")
    .then(r => r.json())
    .then(runs => {
      const tbody = document.querySelector("#runsTable tbody");
      tbody.innerHTML = "";
      for (const run of runs) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td><input type="checkbox" class="run-checkbox" value="${run.run_uuid}"></td>
          <td>${run.name}</td>
          <td>${run.model_id || ""}</td>
          <td class="small text-muted">${run.model_source || ""}</td>
          <td>${run.objective_reaction || ""}</td>
          <td>${run.objective_value !== null ? Number(run.objective_value).toPrecision(6) : "n/a"}</td>
          <td><span class="badge bg-${run.status === "optimal" ? "success" : "danger"}">${run.status || ""}</span></td>
          <td>${run.n_knockouts}</td>
          <td class="small">${fmtDate(run.created_at)}</td>
          <td class="small">${run.notes || ""}</td>
          <td><button class="btn btn-sm btn-outline-danger delete-btn" data-uuid="${run.run_uuid}">Delete</button></td>
        `;
        tbody.appendChild(tr);
      }
      if (runsTable) runsTable.destroy();
      runsTable = $("#runsTable").DataTable({ pageLength: 15, order: [[8, "desc"]] });

      document.querySelectorAll(".run-checkbox").forEach(cb => {
        cb.addEventListener("change", updateButtons);
      });
      document.querySelectorAll(".delete-btn").forEach(btn => {
        btn.addEventListener("click", () => deleteRun(btn.dataset.uuid));
      });
      updateButtons();
    });
}

function getSelected() {
  return Array.from(document.querySelectorAll(".run-checkbox:checked")).map(cb => cb.value);
}

function updateButtons() {
  const selected = getSelected();
  document.getElementById("compareBtn").disabled = selected.length < 1;
  document.getElementById("exportBtn").disabled = selected.length < 1;
}

function deleteRun(uuid) {
  if (!confirm("Delete this saved run? This cannot be undone.")) return;
  fetch("/api/runs/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_uuid: uuid }),
  })
    .then(r => r.json())
    .then(() => loadRuns());
}

document.getElementById("compareBtn").addEventListener("click", () => {
  const ids = getSelected().join(",");
  window.location.href = `/compare?ids=${encodeURIComponent(ids)}`;
});

document.getElementById("exportBtn").addEventListener("click", () => {
  const ids = getSelected().join(",");
  window.location.href = `/api/runs/export?ids=${encodeURIComponent(ids)}`;
});

loadRuns();
