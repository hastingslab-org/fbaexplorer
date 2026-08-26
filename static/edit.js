let reactionsCache = [];
let dataTable = null;

function loadReactions() {
  return fetch("/api/reactions")
    .then(r => r.json())
    .then(data => {
      reactionsCache = data;
      const tbody = document.querySelector("#rxnTable tbody");
      tbody.innerHTML = "";
      for (const rxn of data) {
        const tr = document.createElement("tr");
        tr.dataset.id = rxn.id;
        tr.innerHTML = `
          <td>${rxn.id}</td>
          <td>${rxn.name}</td>
          <td>${rxn.subsystem}</td>
          <td><input type="number" step="any" class="form-control form-control-sm lb-input" value="${rxn.lower_bound}"></td>
          <td><input type="number" step="any" class="form-control form-control-sm ub-input" value="${rxn.upper_bound}"></td>
          <td class="small text-muted">${rxn.reaction_string}</td>
        `;
        tbody.appendChild(tr);
      }
      if (dataTable) {
        dataTable.destroy();
      }
      dataTable = $("#rxnTable").DataTable({
        pageLength: 15,
        order: [],
      });

      const objSelect = document.getElementById("objectiveSelect");
      objSelect.innerHTML = "";
      for (const rxn of data) {
        const opt = document.createElement("option");
        opt.value = rxn.id;
        opt.textContent = `${rxn.id} — ${rxn.name}`;
        objSelect.appendChild(opt);
      }
    });
}

function collectEdits() {
  const edits = [];
  const rows = dataTable
    ? dataTable.rows().nodes().toArray()
    : Array.from(document.querySelectorAll("#rxnTable tbody tr"));
  rows.forEach(tr => {
    const lbInput = tr.querySelector(".lb-input");
    const ubInput = tr.querySelector(".ub-input");
    if (!lbInput || !ubInput) return;
    edits.push({
      id: tr.dataset.id,
      lower_bound: lbInput.value,
      upper_bound: ubInput.value,
    });
  });
  return edits;
}

function applyEdits() {
  const statusMsg = document.getElementById("statusMsg");
  statusMsg.textContent = "Applying edits...";
  const geneText = document.getElementById("geneKnockouts").value;
  const geneList = geneText.split(/[\s,]+/).filter(Boolean);

  const payload = {
    edits: collectEdits(),
    objective: document.getElementById("objectiveSelect").value,
    objective_direction: document.getElementById("objectiveDirection").value,
    gene_knockouts: geneList,
  };

  return fetch("/api/apply_edits", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
    .then(r => r.json())
    .then(res => {
      if (res.errors && res.errors.length) {
        statusMsg.textContent = "Applied with warnings: " + res.errors.join("; ");
      } else {
        statusMsg.textContent = "Edits applied.";
      }
      return res;
    })
    .catch(err => {
      statusMsg.textContent = "Error applying edits: " + err;
    });
}

document.getElementById("applyBtn").addEventListener("click", applyEdits);

document.getElementById("runBtn").addEventListener("click", () => {
  const statusMsg = document.getElementById("statusMsg");
  applyEdits().then(() => {
    statusMsg.textContent = "Running FBA...";
    fetch("/api/run", { method: "POST" })
      .then(r => r.json())
      .then(res => {
        if (res.error) {
          statusMsg.textContent = "FBA error: " + res.error;
        } else {
          window.location.href = "/results";
        }
      });
  });
});

document.getElementById("addRxnBtn").addEventListener("click", () => {
  const msg = document.getElementById("addRxnMsg");
  const btn = document.getElementById("addRxnBtn");

  const payload = {
    id: document.getElementById("newRxnId").value.trim(),
    name: document.getElementById("newRxnName").value.trim(),
    subsystem: document.getElementById("newRxnSubsystem").value.trim(),
    formula: document.getElementById("newRxnFormula").value.trim(),
    lower_bound: document.getElementById("newRxnLB").value,
    upper_bound: document.getElementById("newRxnUB").value,
    gpr: document.getElementById("newRxnGPR").value.trim(),
    new_metabolites: document.getElementById("newRxnMetabolites").value,
  };

  if (!payload.id || !payload.formula) {
    msg.innerHTML = `<span class="text-danger">Reaction ID and formula are required.</span>`;
    return;
  }

  btn.disabled = true;
  msg.textContent = "Applying pending edits, then adding reaction...";

  // Persist any in-progress bound/objective/knockout edits first, so
  // reloading the reaction table afterwards doesn't discard them.
  applyEdits().then(() => {
    fetch("/api/add_reaction", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(r => r.json())
      .then(res => {
        if (res.error) {
          msg.innerHTML = `<span class="text-danger">${res.error}</span>`;
          return;
        }
        msg.innerHTML = res.warning
          ? `<span class="text-warning">${res.warning}</span>`
          : `<span class="text-success">Added reaction '${res.reaction.id}'.</span>`;
        // Clear the form and refresh the table so the new reaction is visible
        ["newRxnId", "newRxnName", "newRxnSubsystem", "newRxnFormula",
         "newRxnGPR", "newRxnMetabolites"].forEach(id => {
          document.getElementById(id).value = "";
        });
        document.getElementById("newRxnLB").value = "0";
        document.getElementById("newRxnUB").value = "1000";
        loadReactions();
      })
      .catch(err => {
        msg.innerHTML = `<span class="text-danger">Error: ${err}</span>`;
      })
      .finally(() => {
        btn.disabled = false;
      });
  });
});

loadReactions();
