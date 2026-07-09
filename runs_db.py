"""
Persistence layer for saved FBA runs.

Each "run" is a self-contained snapshot: the model/constraints in effect
(every reaction's bounds, objective, GPR), the gene knockouts applied, and
the resulting flux for every reaction, plus run-level metadata (name, notes,
timestamp, objective value, solver status). Snapshots are self-contained on
purpose — even if the underlying model is edited or re-uploaded later, a
saved run remains a faithful, independently interpretable record of exactly
what was run and what it produced.

Storage is plain SQLite so runs can be queried directly with SQL or loaded
into pandas (`pd.read_sql`) for downstream comparative analyses, without
needing this application at all.
"""

import sqlite3
import uuid
import datetime
import contextlib


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    model_id TEXT,
    model_source TEXT,
    objective_reaction TEXT,
    objective_direction TEXT,
    objective_value REAL,
    status TEXT,
    n_reactions INTEGER,
    n_knockouts INTEGER
);

CREATE TABLE IF NOT EXISTS run_reactions (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    reaction_id TEXT NOT NULL,
    name TEXT,
    subsystem TEXT,
    lower_bound REAL,
    upper_bound REAL,
    gene_reaction_rule TEXT,
    flux REAL
);
CREATE INDEX IF NOT EXISTS idx_run_reactions_run ON run_reactions(run_id);
CREATE INDEX IF NOT EXISTS idx_run_reactions_rxn ON run_reactions(reaction_id);

CREATE TABLE IF NOT EXISTS run_knockouts (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    gene_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_knockouts_run ON run_knockouts(run_id);
"""


@contextlib.contextmanager
def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path):
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)


def save_run(db_path, *, name, notes, model, solution, model_source,
             knockout_genes=None):
    """Persist a full snapshot of the given model + FBA solution as a run.

    Returns the run's UUID.
    """
    knockout_genes = sorted(set(knockout_genes or []))
    objective_ids = [r.id for r in model.reactions if r.objective_coefficient != 0]
    objective_reaction = objective_ids[0] if objective_ids else None
    fluxes = solution.fluxes if solution is not None else None

    run_uuid = str(uuid.uuid4())
    created_at = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO runs
               (run_uuid, name, notes, created_at, model_id, model_source,
                objective_reaction, objective_direction, objective_value,
                status, n_reactions, n_knockouts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_uuid, name, notes, created_at, model.id, model_source,
             objective_reaction, model.objective_direction,
             float(solution.objective_value) if solution is not None else None,
             solution.status if solution is not None else None,
             len(model.reactions), len(knockout_genes)),
        )
        run_id = cur.lastrowid

        rxn_rows = []
        for rxn in model.reactions:
            flux = float(fluxes[rxn.id]) if fluxes is not None and rxn.id in fluxes.index else None
            sub = getattr(rxn, "subsystem", "") or ""
            if isinstance(sub, (list, tuple, set)):
                sub = "; ".join(str(s) for s in sub if s)
            rxn_rows.append((
                run_id, rxn.id, rxn.name or rxn.id, str(sub),
                rxn.lower_bound, rxn.upper_bound, rxn.gene_reaction_rule, flux,
            ))
        conn.executemany(
            """INSERT INTO run_reactions
               (run_id, reaction_id, name, subsystem, lower_bound, upper_bound,
                gene_reaction_rule, flux)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rxn_rows,
        )

        if knockout_genes:
            conn.executemany(
                "INSERT INTO run_knockouts (run_id, gene_id) VALUES (?, ?)",
                [(run_id, g) for g in knockout_genes],
            )

    return run_uuid


def list_runs(db_path):
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_run(db_path, run_uuid):
    with _connect(db_path) as conn:
        run_row = conn.execute(
            "SELECT * FROM runs WHERE run_uuid = ?", (run_uuid,)
        ).fetchone()
        if run_row is None:
            return None
        run = dict(run_row)
        run_id = run["id"]
        reactions = conn.execute(
            "SELECT reaction_id, name, subsystem, lower_bound, upper_bound, "
            "gene_reaction_rule, flux FROM run_reactions WHERE run_id = ? "
            "ORDER BY reaction_id",
            (run_id,),
        ).fetchall()
        knockouts = conn.execute(
            "SELECT gene_id FROM run_knockouts WHERE run_id = ? ORDER BY gene_id",
            (run_id,),
        ).fetchall()
        run["reactions"] = [dict(r) for r in reactions]
        run["knockouts"] = [k["gene_id"] for k in knockouts]
        return run


def delete_run(db_path, run_uuid):
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM runs WHERE run_uuid = ?", (run_uuid,)
        ).fetchone()
        if row is None:
            return False
        run_id = row["id"]
        conn.execute("DELETE FROM run_reactions WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM run_knockouts WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return True


def compare_runs(db_path, run_uuids):
    """Build a wide reaction x run comparison structure.

    Returns {"runs": [metadata...], "reactions": [{reaction_id, name,
    subsystem, fluxes: {run_uuid: value_or_None}}, ...]}.
    """
    runs = []
    per_run_reactions = {}
    with _connect(db_path) as conn:
        for run_uuid in run_uuids:
            run_row = conn.execute(
                "SELECT * FROM runs WHERE run_uuid = ?", (run_uuid,)
            ).fetchone()
            if run_row is None:
                continue
            run = dict(run_row)
            run_id = run["id"]
            knockouts = conn.execute(
                "SELECT gene_id FROM run_knockouts WHERE run_id = ? ORDER BY gene_id",
                (run_id,),
            ).fetchall()
            run["knockouts"] = [k["gene_id"] for k in knockouts]
            runs.append(run)

            rxn_rows = conn.execute(
                "SELECT reaction_id, name, subsystem, lower_bound, upper_bound, flux "
                "FROM run_reactions WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            per_run_reactions[run_uuid] = {r["reaction_id"]: dict(r) for r in rxn_rows}

    # Union of all reaction ids across the selected runs, preserving a stable order
    all_reaction_ids = []
    seen = set()
    for run_uuid in run_uuids:
        for rid in per_run_reactions.get(run_uuid, {}):
            if rid not in seen:
                seen.add(rid)
                all_reaction_ids.append(rid)
    all_reaction_ids.sort()

    reactions = []
    for rid in all_reaction_ids:
        name, subsystem = rid, ""
        fluxes = {}
        bounds = {}
        for run_uuid in run_uuids:
            info = per_run_reactions.get(run_uuid, {}).get(rid)
            if info:
                name = info["name"] or rid
                subsystem = info["subsystem"] or subsystem
                fluxes[run_uuid] = info["flux"]
                bounds[run_uuid] = [info["lower_bound"], info["upper_bound"]]
            else:
                fluxes[run_uuid] = None
                bounds[run_uuid] = None
        reactions.append({
            "reaction_id": rid, "name": name, "subsystem": subsystem,
            "fluxes": fluxes, "bounds": bounds,
        })

    return {"runs": runs, "reactions": reactions}
