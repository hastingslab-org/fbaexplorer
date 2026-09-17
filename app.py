"""
Flask + COBRApy Flux Balance Analysis (FBA) web app.

Workflow:
  1. Upload a genome-scale metabolic model (SBML/.xml, JSON, YAML, or MATLAB/.mat)
  2. Optionally edit reaction bounds, the objective, gene knockouts, and medium
  3. Run FBA and view the resulting flux distribution as a sortable/searchable table
  4. Open a separate visualisation window that dynamically renders a pathway
     (subsystem) map, colouring/sizing reactions and metabolites by predicted flux

Notes on architecture
----------------------
This is intentionally a *simple* single-process app: loaded models and the most
recent FBA solution are kept in an in-memory dict keyed by a per-browser session
id (a cookie). That means it must be run as a single process (Flask dev server,
or `gunicorn -w 1`). See README.md for details and how to scale this up if needed.
"""

import io
import os
import uuid
import datetime
import tempfile
import traceback
from collections import defaultdict

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash, send_file
)
from werkzeug.utils import secure_filename

import cobra
import pandas as pd
from cobra import Model, Reaction, Metabolite
from cobra.io import (
    read_sbml_model, load_json_model, load_matlab_model, load_yaml_model
)

import runs_db

# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FBA_APP_SECRET", "dev-secret-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB uploads

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
RUNS_DB_PATH = os.path.join(DATA_DIR, "runs.db")
runs_db.init_db(RUNS_DB_PATH)

# The default model shown on the upload page. If a bundled FlySilico file
# isn't present in data/, the app falls back gracefully (see
# find_default_model_file / upload()) and tells the user how to add it.
DEFAULT_MODEL_NAME = "FlySilico"
DEFAULT_MODEL_SOURCE_URL = "https://gitlab.com/Beller-Lab/flysilico/-/blob/master/FlySilico_v1_sbml.xls"

ALLOWED_EXTENSIONS = {"xml", "sbml", "json", "yaml", "yml", "mat", "xls", "xlsx"}

# In-memory per-session storage. Simple and fine for a single-process dev app.
MODELS = {}         # sid -> cobra.Model
SOLUTIONS = {}       # sid -> cobra.core.Solution
MODEL_SOURCES = {}   # sid -> human-readable description of where the model came from
KNOCKOUTS = {}       # sid -> set of gene IDs knocked out so far this session


def find_default_model_file():
    """Look for a bundled FlySilico model file under data/. Returns the path
    if found, else None. Accepts any of the common export formats."""
    if not os.path.isdir(DATA_DIR):
        return None
    for fname in sorted(os.listdir(DATA_DIR)):
        if fname.lower().startswith("flysilico") and allowed_file(fname):
            return os.path.join(DATA_DIR, fname)
    return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def get_sid(create=True):
    """Return the current browser session's model-store id, creating one if needed."""
    sid = session.get("sid")
    if sid is None and create:
        sid = str(uuid.uuid4())
        session["sid"] = sid
    return sid


def get_model():
    sid = get_sid(create=False)
    if sid is None:
        return None
    return MODELS.get(sid)


def get_solution():
    sid = get_sid(create=False)
    if sid is None:
        return None
    return SOLUTIONS.get(sid)


def get_knockouts(sid):
    return KNOCKOUTS.setdefault(sid, set())


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _find_col(columns, candidates):
    """Case/whitespace-insensitive lookup of the first matching column name."""
    cols_lower = {str(c).lower().strip(): c for c in columns}
    for cand in candidates:
        if cand in cols_lower:
            return cols_lower[cand]
    return None


def load_model_from_excel(path):
    """Parse a genome-scale model distributed as an Excel spreadsheet.

    Supports the common "COBRA Toolbox" Excel model layout used by many
    published genome-scale models: a 'Reaction List' sheet with columns
    such as Abbreviation, Description, Reaction, GPR, Lower bound,
    Upper bound, Objective, Subsystem — and, optionally, a 'Metabolite
    List' sheet with Abbreviation, Description, Neutral formula, Charge,
    Compartment. Column names are matched case-insensitively and a few
    common synonyms are recognised, since published models vary slightly
    in their headers.
    """
    xls = pd.ExcelFile(path)

    rxn_sheet = next((n for n in xls.sheet_names if "reaction" in n.lower()), None)
    if rxn_sheet is None:
        rxn_sheet = xls.sheet_names[0]
    rxn_df = xls.parse(rxn_sheet).dropna(how="all")

    id_col = _find_col(rxn_df.columns, ["abbreviation", "id", "reaction abbreviation", "rxn id"])
    name_col = _find_col(rxn_df.columns, ["description", "name", "reaction name"])
    formula_col = _find_col(rxn_df.columns, ["reaction", "reaction formula", "formula", "equation"])
    gpr_col = _find_col(rxn_df.columns, ["gpr", "gene-reaction association", "gene reaction rule", "genes"])
    lb_col = _find_col(rxn_df.columns, ["lower bound", "lb", "lower_bound"])
    ub_col = _find_col(rxn_df.columns, ["upper bound", "ub", "upper_bound"])
    obj_col = _find_col(rxn_df.columns, ["objective", "obj", "objective coefficient"])
    subsystem_col = _find_col(rxn_df.columns, ["subsystem", "pathway"])

    missing = [n for n, c in [("reaction ID", id_col), ("reaction formula", formula_col)] if c is None]
    if missing:
        raise ValueError(
            f"Could not find required column(s) {missing} in sheet '{rxn_sheet}'. "
            f"Found columns: {list(rxn_df.columns)}. Expected a COBRA-style model "
            f"sheet with at least an 'Abbreviation' and a 'Reaction' column."
        )

    # Optional metabolite metadata (name, formula, charge, compartment)
    met_info = {}
    met_sheet = next((n for n in xls.sheet_names if "metabolite" in n.lower()), None)
    if met_sheet:
        met_df = xls.parse(met_sheet).dropna(how="all")
        m_id_col = _find_col(met_df.columns, ["abbreviation", "id", "metabolite abbreviation"])
        m_name_col = _find_col(met_df.columns, ["description", "name"])
        m_formula_col = _find_col(met_df.columns, ["neutral formula", "formula"])
        m_charge_col = _find_col(met_df.columns, ["charge"])
        m_compartment_col = _find_col(met_df.columns, ["compartment"])
        if m_id_col:
            for _, row in met_df.iterrows():
                mid = str(row[m_id_col]).strip()
                if not mid or mid == "nan":
                    continue
                met_info[mid] = {
                    "name": str(row[m_name_col]).strip() if m_name_col and pd.notna(row.get(m_name_col)) else "",
                    "formula": str(row[m_formula_col]).strip() if m_formula_col and pd.notna(row.get(m_formula_col)) else None,
                    "charge": row[m_charge_col] if m_charge_col and pd.notna(row.get(m_charge_col)) else None,
                    "compartment": str(row[m_compartment_col]).strip() if m_compartment_col and pd.notna(row.get(m_compartment_col)) else None,
                }

    model = Model("uploaded_excel_model")
    seen_metabolites = set()
    skipped = []

    for _, row in rxn_df.iterrows():
        rid = row.get(id_col)
        if pd.isna(rid) or str(rid).strip() == "":
            continue
        rid = str(rid).strip()
        formula = row.get(formula_col)
        if pd.isna(formula) or str(formula).strip() == "":
            skipped.append(rid)
            continue
        if rid in model.reactions:
            skipped.append(f"{rid} (duplicate)")
            continue

        rxn = Reaction(rid)
        rxn.name = str(row[name_col]).strip() if name_col and pd.notna(row.get(name_col)) else rid
        rxn.subsystem = str(row[subsystem_col]).strip() if subsystem_col and pd.notna(row.get(subsystem_col)) else ""
        model.add_reactions([rxn])

        try:
            rxn.build_reaction_from_string(str(formula).strip())
        except Exception as e:
            model.remove_reactions([rxn])
            skipped.append(f"{rid} (unparsable formula: {e})")
            continue

        # build_reaction_from_string infers bounds from the arrow type (-->
        # forces (0, 1000), <=> forces (-1000, 1000)) — re-apply the
        # spreadsheet's explicit bounds afterwards so they aren't lost.
        lb = float(row[lb_col]) if lb_col and pd.notna(row.get(lb_col)) else rxn.lower_bound
        ub = float(row[ub_col]) if ub_col and pd.notna(row.get(ub_col)) else rxn.upper_bound
        if lb <= ub:
            rxn.bounds = (lb, ub)

        for met in rxn.metabolites:
            if met.id in seen_metabolites:
                continue
            info = met_info.get(met.id, {})
            met.name = info.get("name") or met.id
            met.formula = info.get("formula")
            if info.get("charge") is not None:
                try:
                    met.charge = int(float(info["charge"]))
                except (ValueError, TypeError):
                    pass
            met.compartment = info.get("compartment") or (
                met.id.rsplit("_", 1)[-1] if "_" in met.id else "c"
            )
            seen_metabolites.add(met.id)

        if gpr_col and pd.notna(row.get(gpr_col)):
            try:
                rxn.gene_reaction_rule = str(row[gpr_col]).strip()
            except Exception:
                pass

        if obj_col and pd.notna(row.get(obj_col)):
            try:
                coeff = float(row[obj_col])
                if coeff != 0:
                    rxn.objective_coefficient = coeff
            except (ValueError, TypeError):
                pass

    if len(model.reactions) == 0:
        raise ValueError("No valid reactions could be parsed from the Excel file.")
    if skipped:
        model.notes["skipped_rows"] = "; ".join(skipped[:50])
    return model


def load_model_from_path(path):
    ext = path.rsplit(".", 1)[1].lower()
    if ext in ("xml", "sbml"):
        return read_sbml_model(path)
    if ext == "json":
        return load_json_model(path)
    if ext in ("yaml", "yml"):
        return load_yaml_model(path)
    if ext == "mat":
        return load_matlab_model(path)
    if ext in ("xls", "xlsx"):
        return load_model_from_excel(path)
    raise ValueError(f"Unsupported file extension: {ext}")


def reaction_subsystem(rxn):
    """Best-effort subsystem/pathway label for a reaction across model formats."""
    sub = getattr(rxn, "subsystem", None)
    if isinstance(sub, (list, tuple, set)):
        sub = "; ".join(str(s) for s in sub if s)
    if sub:
        return str(sub)
    # Fall back to SBML groups (newer models use groups instead of the
    # legacy 'subsystem' field)
    try:
        model = rxn.model
        if model is not None and getattr(model, "groups", None):
            for g in model.groups:
                if rxn in g.members:
                    return g.name or g.id
    except Exception:
        pass
    return "Unassigned"


def reaction_to_dict(rxn, flux=None):
    return {
        "id": rxn.id,
        "name": rxn.name or rxn.id,
        "subsystem": reaction_subsystem(rxn),
        "lower_bound": rxn.lower_bound,
        "upper_bound": rxn.upper_bound,
        "reaction_string": rxn.build_reaction_string(use_metabolite_names=True),
        "genes": ", ".join(g.id for g in rxn.genes),
        "flux": None if flux is None else float(flux),
    }


def clear_solution_for(sid):
    SOLUTIONS.pop(sid, None)


# --------------------------------------------------------------------------
# Page routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        default_model_available=find_default_model_file() is not None,
        default_model_name=DEFAULT_MODEL_NAME,
        default_model_source_url=DEFAULT_MODEL_SOURCE_URL,
    )


@app.route("/upload", methods=["POST"])
def upload():
    sid = get_sid()

    use_example = request.form.get("use_example")

    if use_example == "flysilico":
        default_path = find_default_model_file()
        if default_path is None:
            flash(
                f"The bundled {DEFAULT_MODEL_NAME} model isn't available yet. "
                f"Download it from {DEFAULT_MODEL_SOURCE_URL} and save it as "
                f"'data/FlySilico_v1_sbml.xls' next to app.py (or just upload "
                f"the file yourself using the form above), then try again.",
                "warning",
            )
            return redirect(url_for("index"))
        try:
            model = load_model_from_path(default_path)
        except Exception as e:
            flash(f"Could not load the bundled {DEFAULT_MODEL_NAME} model: {e}", "danger")
            return redirect(url_for("index"))
        if not model.id or model.id == "uploaded_excel_model":
            model.id = "FlySilico_v1"
        MODELS[sid] = model
        MODEL_SOURCES[sid] = f"default:{DEFAULT_MODEL_NAME}"
        KNOCKOUTS[sid] = set()
        clear_solution_for(sid)
        flash(f"Loaded {DEFAULT_MODEL_NAME} model '{model.id}' "
              f"({len(model.reactions)} reactions, {len(model.metabolites)} metabolites).",
              "success")
        return redirect(url_for("edit"))

    if use_example == "textbook":
        try:
            model = cobra.io.load_model("textbook")
            model.id = model.id or "textbook_example"
        except Exception as e:
            flash(f"Could not load the example model: {e}", "danger")
            return redirect(url_for("index"))
        MODELS[sid] = model
        MODEL_SOURCES[sid] = "example:textbook"
        KNOCKOUTS[sid] = set()
        clear_solution_for(sid)
        flash(f"Loaded example model '{model.id}' "
              f"({len(model.reactions)} reactions, {len(model.metabolites)} metabolites).",
              "success")
        return redirect(url_for("edit"))

    file = request.files.get("model_file")
    if file is None or file.filename == "":
        flash("Please choose a model file to upload.", "warning")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("Unsupported file type. Please upload .xml/.sbml, .json, .yaml, .mat, .xls, or .xlsx.", "warning")
        return redirect(url_for("index"))

    filename = secure_filename(file.filename)
    save_path = os.path.join(UPLOAD_DIR, f"{sid}_{filename}")
    file.save(save_path)

    try:
        model = load_model_from_path(save_path)
    except Exception as e:
        flash(f"Failed to parse model file: {e}", "danger")
        return redirect(url_for("index"))
    finally:
        try:
            os.remove(save_path)
        except OSError:
            pass

    if len(model.reactions) == 0:
        flash("The uploaded model has no reactions.", "warning")
        return redirect(url_for("index"))

    MODELS[sid] = model
    MODEL_SOURCES[sid] = f"upload:{filename}"
    KNOCKOUTS[sid] = set()
    clear_solution_for(sid)
    flash(f"Loaded model '{model.id}' "
          f"({len(model.reactions)} reactions, {len(model.metabolites)} metabolites, "
          f"{len(model.genes)} genes).", "success")
    return redirect(url_for("edit"))


@app.route("/edit")
def edit():
    model = get_model()
    if model is None:
        flash("Please upload a model first.", "warning")
        return redirect(url_for("index"))
    objective_ids = [r.id for r in model.reactions if r.objective_coefficient != 0]
    return render_template(
        "edit.html",
        model_id=model.id,
        n_reactions=len(model.reactions),
        n_metabolites=len(model.metabolites),
        n_genes=len(model.genes),
        objective_ids=objective_ids,
    )


@app.route("/results")
def results():
    model = get_model()
    if model is None:
        flash("Please upload a model first.", "warning")
        return redirect(url_for("index"))
    solution = get_solution()
    if solution is None:
        flash("Run FBA first to see results.", "warning")
        return redirect(url_for("edit"))
    return render_template(
        "results.html",
        model_id=model.id,
        status=solution.status,
        objective_value=solution.objective_value,
    )


@app.route("/visualize")
def visualize():
    model = get_model()
    if model is None:
        flash("Please upload a model first.", "warning")
        return redirect(url_for("index"))
    return render_template("visualize.html", model_id=model.id,
                            has_solution=get_solution() is not None)


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------

@app.route("/api/reactions")
def api_reactions():
    model = get_model()
    if model is None:
        return jsonify({"error": "no model loaded"}), 400
    solution = get_solution()
    fluxes = solution.fluxes if solution is not None else None
    data = []
    for rxn in model.reactions:
        flux = None
        if fluxes is not None and rxn.id in fluxes.index:
            flux = fluxes[rxn.id]
        data.append(reaction_to_dict(rxn, flux))
    return jsonify(data)


@app.route("/api/objective", methods=["GET", "POST"])
def api_objective():
    model = get_model()
    if model is None:
        return jsonify({"error": "no model loaded"}), 400
    if request.method == "POST":
        payload = request.get_json(force=True)
        rxn_id = payload.get("reaction_id")
        direction = payload.get("direction", "max")
        if rxn_id not in model.reactions:
            return jsonify({"error": f"unknown reaction {rxn_id}"}), 400
        model.objective = rxn_id
        model.objective_direction = "max" if direction == "max" else "min"
        clear_solution_for(get_sid())
        return jsonify({"ok": True})
    return jsonify({
        "objective_ids": [r.id for r in model.reactions if r.objective_coefficient != 0],
        "direction": model.objective_direction,
    })


@app.route("/api/apply_edits", methods=["POST"])
def api_apply_edits():
    """Apply bound edits, objective change, and gene knockouts in one shot."""
    model = get_model()
    if model is None:
        return jsonify({"error": "no model loaded"}), 400
    payload = request.get_json(force=True)

    errors = []

    # 1) Reaction bound edits: [{id, lower_bound, upper_bound}, ...]
    for edit_item in payload.get("edits", []):
        rid = edit_item.get("id")
        try:
            rxn = model.reactions.get_by_id(rid)
        except KeyError:
            errors.append(f"Unknown reaction: {rid}")
            continue
        try:
            lb = float(edit_item["lower_bound"])
            ub = float(edit_item["upper_bound"])
            if lb > ub:
                errors.append(f"{rid}: lower bound > upper bound, skipped")
                continue
            rxn.lower_bound = lb
            rxn.upper_bound = ub
        except (KeyError, ValueError, TypeError):
            errors.append(f"{rid}: invalid bound values")

    # 2) Objective
    objective_id = payload.get("objective")
    if objective_id:
        try:
            model.objective = objective_id
            model.objective_direction = payload.get("objective_direction", "max")
        except Exception as e:
            errors.append(f"Objective error: {e}")

    # 3) Gene knockouts (comma/whitespace separated gene IDs)
    ko_genes = payload.get("gene_knockouts", [])
    knocked_out = []
    sid = get_sid()
    session_knockouts = get_knockouts(sid)
    for gid in ko_genes:
        gid = gid.strip()
        if not gid:
            continue
        try:
            gene = model.genes.get_by_id(gid)
            gene.knock_out()
            knocked_out.append(gid)
            session_knockouts.add(gid)
        except KeyError:
            errors.append(f"Unknown gene: {gid}")

    clear_solution_for(sid)
    return jsonify({"ok": True, "errors": errors, "knocked_out": knocked_out})


def _parse_new_metabolites(text):
    """Parse the optional 'New metabolites' textarea: one metabolite per
    line, pipe-separated: id | name | formula | compartment | charge.
    Only id is required; trailing fields may be omitted."""
    metabolites = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        mid = parts[0]
        if not mid:
            continue
        metabolites[mid] = {
            "name": parts[1] if len(parts) > 1 and parts[1] else mid,
            "formula": parts[2] if len(parts) > 2 and parts[2] else None,
            "compartment": parts[3] if len(parts) > 3 and parts[3] else (
                mid.rsplit("_", 1)[-1] if "_" in mid else "c"
            ),
            "charge": parts[4] if len(parts) > 4 and parts[4] else None,
        }
    return metabolites


@app.route("/api/add_reaction", methods=["POST"])
def api_add_reaction():
    """Add a brand-new reaction to the model (and any metabolites it
    introduces). Existing reactions/metabolites are never removed by this
    app — constrain a reaction's bounds to (0, 0) to effectively knock it
    out instead."""
    model = get_model()
    if model is None:
        return jsonify({"error": "no model loaded"}), 400
    payload = request.get_json(force=True) or {}

    rid = (payload.get("id") or "").strip()
    formula = (payload.get("formula") or "").strip()
    if not rid:
        return jsonify({"error": "A reaction ID is required."}), 400
    if not formula:
        return jsonify({"error": "A reaction formula is required."}), 400
    if rid in model.reactions:
        return jsonify({"error": f"Reaction '{rid}' already exists in the model."}), 400

    # Pre-create any newly described metabolites with proper metadata, so
    # they aren't left as bare auto-created placeholders.
    new_met_info = _parse_new_metabolites(payload.get("new_metabolites"))
    to_add = []
    for mid, info in new_met_info.items():
        if mid in model.metabolites:
            continue
        met = Metabolite(mid, name=info["name"], compartment=info["compartment"])
        if info["formula"]:
            met.formula = info["formula"]
        if info["charge"] is not None:
            try:
                met.charge = int(float(info["charge"]))
            except (ValueError, TypeError):
                pass
        to_add.append(met)
    if to_add:
        model.add_metabolites(to_add)

    rxn = Reaction(rid)
    rxn.name = (payload.get("name") or "").strip() or rid
    rxn.subsystem = (payload.get("subsystem") or "").strip()
    model.add_reactions([rxn])

    try:
        rxn.build_reaction_from_string(formula)
    except Exception as e:
        model.remove_reactions([rxn])
        return jsonify({"error": f"Could not parse reaction formula: {e}"}), 400

    # build_reaction_from_string infers bounds from the arrow type; apply the
    # user's explicit bounds afterwards so they aren't silently overridden.
    try:
        lb = float(payload.get("lower_bound", rxn.lower_bound))
        ub = float(payload.get("upper_bound", rxn.upper_bound))
        if lb > ub:
            model.remove_reactions([rxn])
            return jsonify({"error": "Lower bound cannot exceed upper bound."}), 400
        rxn.bounds = (lb, ub)
    except (TypeError, ValueError):
        model.remove_reactions([rxn])
        return jsonify({"error": "Invalid lower/upper bound."}), 400

    gpr = (payload.get("gpr") or "").strip()
    if gpr:
        try:
            rxn.gene_reaction_rule = gpr
        except Exception as e:
            # Reaction is still added even if the GPR string is malformed —
            # just report it so the user can fix it.
            clear_solution_for(get_sid())
            return jsonify({
                "ok": True,
                "reaction": reaction_to_dict(rxn),
                "warning": f"Reaction added, but GPR could not be parsed: {e}",
            })

    clear_solution_for(get_sid())
    return jsonify({"ok": True, "reaction": reaction_to_dict(rxn)})


@app.route("/api/run", methods=["POST"])
def api_run():
    model = get_model()
    if model is None:
        return jsonify({"error": "no model loaded"}), 400
    try:
        solution = model.optimize()
    except Exception as e:
        return jsonify({"error": f"Optimization failed: {e}"}), 500
    SOLUTIONS[get_sid()] = solution
    return jsonify({
        "ok": True,
        "status": solution.status,
        "objective_value": solution.objective_value,
    })


@app.route("/api/subsystems")
def api_subsystems():
    model = get_model()
    if model is None:
        return jsonify({"error": "no model loaded"}), 400
    counts = defaultdict(int)
    for rxn in model.reactions:
        counts[reaction_subsystem(rxn)] += 1
    subsystems = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return jsonify([{"name": name, "n_reactions": n} for name, n in subsystems])


@app.route("/api/pathway_data")
def api_pathway_data():
    """Build a bipartite reaction/metabolite graph for a subsystem, annotated
    with flux values from the most recent FBA solution (if any)."""
    model = get_model()
    if model is None:
        return jsonify({"error": "no model loaded"}), 400

    # Repeated ?subsystem=A&subsystem=B params (names may contain commas)
    wanted = set(request.args.getlist("subsystem"))
    solution = get_solution()
    fluxes = solution.fluxes if solution is not None else None

    rxns = [r for r in model.reactions if reaction_subsystem(r) in wanted]
    if not rxns:
        return jsonify({"nodes": [], "edges": [], "has_flux": fluxes is not None})

    nodes = {}
    edges = []

    for rxn in rxns:
        flux = float(fluxes[rxn.id]) if fluxes is not None and rxn.id in fluxes.index else 0.0
        rxn_subsystem = reaction_subsystem(rxn)
        nodes[f"rxn:{rxn.id}"] = {
            "id": f"rxn:{rxn.id}",
            "label": rxn.id,
            "title": f"{rxn.name}\npathway: {rxn_subsystem}\nflux = {flux:.4g}\n{rxn.build_reaction_string()}",
            "type": "reaction",
            "subsystem": rxn_subsystem,
            "flux": flux,
            "reversible": rxn.lower_bound < 0,
        }
        for met, coeff in rxn.metabolites.items():
            met_node_id = f"met:{met.id}"
            if met_node_id not in nodes:
                nodes[met_node_id] = {
                    "id": met_node_id,
                    "label": met.id,
                    "title": met.name or met.id,
                    "type": "metabolite",
                    "flux": None,
                    "reversible": False,
                }
            # Determine stoichiometric direction, then flip if flux is negative
            substrate = coeff < 0
            forward = substrate  # substrate -> reaction node
            if flux < 0:
                forward = not forward
            if forward:
                src, dst = met_node_id, f"rxn:{rxn.id}"
            else:
                src, dst = f"rxn:{rxn.id}", met_node_id
            edges.append({
                "from": src,
                "to": dst,
                "flux": abs(flux),
                "active": abs(flux) > 1e-9,
            })

    return jsonify({
        "nodes": list(nodes.values()),
        "edges": edges,
        "has_flux": fluxes is not None,
    })


@app.route("/api/download_fluxes")
def download_fluxes():
    model = get_model()
    solution = get_solution()
    if model is None or solution is None:
        flash("Run FBA first.", "warning")
        return redirect(url_for("edit"))
    rows = [reaction_to_dict(r, solution.fluxes.get(r.id)) for r in model.reactions]
    df = pd.DataFrame(rows)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True,
                      download_name=f"{model.id}_fluxes.csv")


# --------------------------------------------------------------------------
# Saved runs: persist model + constraints + flux results for later comparison
# --------------------------------------------------------------------------

@app.route("/runs")
def runs_page():
    return render_template("runs.html")


@app.route("/compare")
def compare_page():
    ids = request.args.get("ids", "")
    return render_template("compare.html", ids=ids)


@app.route("/api/save_run", methods=["POST"])
def api_save_run():
    model = get_model()
    solution = get_solution()
    if model is None:
        return jsonify({"error": "no model loaded"}), 400
    if solution is None:
        return jsonify({"error": "run FBA before saving a run"}), 400

    payload = request.get_json(force=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        name = f"{model.id} — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    notes = (payload.get("notes") or "").strip()

    sid = get_sid()
    model_source = MODEL_SOURCES.get(sid, "unknown")
    knockouts = sorted(get_knockouts(sid))

    try:
        run_uuid = runs_db.save_run(
            RUNS_DB_PATH, name=name, notes=notes, model=model, solution=solution,
            model_source=model_source, knockout_genes=knockouts,
        )
    except Exception as e:
        return jsonify({"error": f"Could not save run: {e}"}), 500

    return jsonify({"ok": True, "run_uuid": run_uuid})


@app.route("/api/runs")
def api_list_runs():
    return jsonify(runs_db.list_runs(RUNS_DB_PATH))


@app.route("/api/runs/<run_uuid>")
def api_get_run(run_uuid):
    run = runs_db.get_run(RUNS_DB_PATH, run_uuid)
    if run is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify(run)


@app.route("/api/runs/delete", methods=["POST"])
def api_delete_run():
    payload = request.get_json(force=True) or {}
    run_uuid = payload.get("run_uuid")
    ok = runs_db.delete_run(RUNS_DB_PATH, run_uuid) if run_uuid else False
    return jsonify({"ok": ok})


@app.route("/api/compare")
def api_compare_runs():
    ids = request.args.get("ids", "")
    run_uuids = [i.strip() for i in ids.split(",") if i.strip()]
    if not run_uuids:
        return jsonify({"error": "no run ids given"}), 400
    return jsonify(runs_db.compare_runs(RUNS_DB_PATH, run_uuids))


@app.route("/api/runs/export")
def api_export_runs():
    ids = request.args.get("ids", "")
    run_uuids = [i.strip() for i in ids.split(",") if i.strip()]
    if not run_uuids:
        flash("No runs selected to export.", "warning")
        return redirect(url_for("runs_page"))

    data = runs_db.compare_runs(RUNS_DB_PATH, run_uuids)
    run_meta = {r["run_uuid"]: r for r in data["runs"]}

    rows = []
    for rxn in data["reactions"]:
        row = {"reaction_id": rxn["reaction_id"], "name": rxn["name"], "subsystem": rxn["subsystem"]}
        for run_uuid in run_uuids:
            label = run_meta.get(run_uuid, {}).get("name", run_uuid)
            row[f"flux [{label}]"] = rxn["fluxes"].get(run_uuid)
        rows.append(row)

    df = pd.DataFrame(rows)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    mem = io.BytesIO(buf.getvalue().encode("utf-8"))
    return send_file(mem, mimetype="text/csv", as_attachment=True,
                      download_name="fba_run_comparison.csv")


@app.route("/reset")
def reset():
    sid = get_sid(create=False)
    if sid:
        MODELS.pop(sid, None)
        SOLUTIONS.pop(sid, None)
        MODEL_SOURCES.pop(sid, None)
        KNOCKOUTS.pop(sid, None)
    session.clear()
    flash("Session cleared.", "info")
    return redirect(url_for("index"))


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", error=str(e), trace=traceback.format_exc()), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5012)
