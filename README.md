# FBA Explorer

A small Flask web app for running and visualising Flux Balance Analysis (FBA)
on genome-scale metabolic models using [COBRApy](https://cobrapy.readthedocs.io/).

## Workflow

1. **Upload** — load the bundled default model (**FlySilico**, a curated
   *Drosophila melanogaster* genome-scale model — see below), a small
   *E. coli* textbook model for quick testing, or upload your own model
   (SBML `.xml`/`.sbml`, COBRA JSON, YAML, MATLAB `.mat`, or Excel
   `.xls`/`.xlsx`).
2. **Edit** — review/edit reaction flux bounds (including exchange reactions,
   i.e. the growth medium), change the objective reaction and direction,
   apply gene knockouts, and **add new reactions** to the model (ID, formula,
   bounds, subsystem, optional GPR — new metabolites referenced in the
   formula can be described inline or are auto-created). Existing reactions
   are never deleted by the app; constrain a reaction's bounds to `(0, 0)`
   to effectively remove its effect instead.
3. **Run & Results** — run FBA (`model.optimize()`) and browse the full flux
   distribution as a searchable, sortable table; download it as CSV.
4. **Visualise** — open a separate window/tab that renders an interactive
   network diagram for a selected pathway (subsystem), with reactions and
   metabolites coloured/sized by predicted flux. A **Refresh** button
   re-fetches flux values for the current pathway without resetting your
   zoom/pan, so if you re-run FBA with new constraints in the other tab you
   can bring this view up to date without losing your place. Switching
   pathways always does a full re-layout.
5. **Save & compare runs** — on the results page, save a run with a name and
   optional notes. Each saved run is a self-contained snapshot (every
   reaction's bounds, the objective, gene knockouts, and the resulting flux
   for every reaction) stored in a local SQLite database
   (`data/runs.db`) — independent of the live session, so it survives model
   edits, re-uploads, or app restarts. The **Saved runs** page lists every
   run; select two or more and click **Compare** to see them side by side —
   metadata cards plus a reaction × run flux table that highlights reactions
   whose flux differs between runs. Comparisons (or the raw per-run data)
   can be exported as CSV for downstream analysis in pandas, R, Excel, etc.

## Default model: FlySilico

The app's default model is [**FlySilico**](https://gitlab.com/Beller-Lab/flysilico),
a curated genome-scale metabolic model of *Drosophila melanogaster* larval
metabolism (Schönborn et al. 2019, *Scientific Reports*), distributed as an
Excel spreadsheet.

**This repo does not bundle the FlySilico file itself** — it isn't hosted
anywhere this app's build environment can reach automatically. To enable the
one-click default:

1. Download `FlySilico_v1_sbml.xls` from
   <https://gitlab.com/Beller-Lab/flysilico/-/blob/master/FlySilico_v1_sbml.xls>
2. Save it as `data/FlySilico_v1_sbml.xls` (next to `app.py`).
3. Restart the app — the homepage will now show a **Load FlySilico** button.

Until then, the homepage explains this and lets you upload the file manually
via the regular upload form (identical result, just one extra click each
time). A small bundled *E. coli* textbook model remains available as a
"quick test" option regardless.

## Saved runs & comparison

The **Saved runs** page (and the **Save run** button on the results page)
persist full snapshots of individual FBA runs to a local SQLite database at
`data/runs.db`, independent of the in-memory session state — saved runs
survive model edits, new uploads, and app restarts. Each snapshot captures:

- every reaction's bounds, name, subsystem, and GPR at the time of the run
- the objective reaction, direction, and resulting objective value
- the list of gene knockouts applied during that session
- the resulting flux for every reaction

Select two or more saved runs on the **Saved runs** page and click **Compare**
to see them side by side: metadata cards (model, constraints, knockouts,
notes) plus a reaction × run flux table, with cells highlighted where flux
differs between runs. Use **Export selected (CSV)** (from either the list or
the comparison page) to pull the data into pandas/R/Excel for further
analysis — or query `data/runs.db` directly with SQL/`pandas.read_sql`, since
the schema (`runs`, `run_reactions`, `run_knockouts`) is plain, normalized
SQLite designed for exactly that. See `runs_db.py` for the schema and helper
functions if you want to build on this programmatically.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Then open http://localhost:5000 in your browser.

For a slightly more production-like run:

```bash
gunicorn -w 1 -b 0.0.0.0:5000 app:app
```

**Important:** this app keeps uploaded models and FBA solutions in an
in-memory dictionary keyed by a browser session cookie, for simplicity. This
means:

- It must run as a **single process** (`-w 1` with gunicorn, or the Flask dev
  server). Running multiple worker processes will cause "no model loaded"
  errors, because each worker has its own memory.
- Data is lost on restart. There's no database — this is meant as a local /
  single-user analysis tool, not a multi-tenant production service.
- If you need multi-user or persistent support, swap `MODELS`/`SOLUTIONS` in
  `app.py` for a proper cache (Redis, filesystem pickle store, etc.) keyed by
  session id.

## How pathway grouping works

"Pathways" are derived from each reaction's `subsystem` annotation (falling
back to SBML `groups` if present). Most curated genome-scale models — e.g.
BiGG models such as iML1515, iJO1366, iAF1260 — have this populated for every
reaction, which is what step 4 uses to group reactions into a pathway map. If
your model lacks subsystem/group annotations, everything will fall under a
single "Unassigned" pathway.

## Excel (.xls/.xlsx) model format

Excel uploads are parsed using the common "COBRA Toolbox" spreadsheet layout
used by many published genome-scale models. Expected sheets:

- **Reaction List** (required, sheet name just needs to contain "reaction"):
  columns `Abbreviation`, `Description`, `Reaction` (a formula string like
  `A + B <=> C + D` or `A + B --> C`), `GPR`, `Lower bound`, `Upper bound`,
  `Objective`, `Subsystem`. Column names are matched case-insensitively and a
  few common synonyms are recognised (e.g. `LB`/`lower_bound` for
  `Lower bound`).
- **Metabolite List** (optional, sheet name just needs to contain
  "metabolite"): columns `Abbreviation`, `Description`, `Neutral formula`,
  `Charge`, `Compartment`, used to enrich metabolite names/formulas.

Only `Abbreviation` and `Reaction` are strictly required; missing bounds
default to (-1000, 1000), and the app will report which rows it couldn't
parse.

## How the pathway map is built

For the selected subsystem, the app builds a bipartite graph: reaction nodes
(boxes) connected to the metabolite nodes (circles) they consume/produce,
oriented by the sign of the stoichiometric coefficient and the sign of the
predicted flux. This gives a lightweight, dependency-free alternative to a
curated map (e.g. Escher), generated directly and automatically from any
uploaded model — no pre-built map file required.

## Project structure

```
app.py                  Flask app: routes + COBRApy logic
runs_db.py               SQLite persistence for saved runs + comparison queries
templates/               Jinja2 HTML templates
static/style.css         Minor styling
static/edit.js           Reaction bounds table + apply/run logic
static/visualize.js      vis-network pathway rendering
static/runs.js            Saved runs listing + selection
static/compare.js         Run comparison table + metadata cards
data/                     runs.db (created on first run) + optional bundled FlySilico file
requirements.txt
```

## Extending this

Ideas if you want to build further:

- Add flux variability analysis (FVA) or parsimonious FBA (pFBA) as
  alternative solvers in `/api/run`.
- Persist models/solutions to disk (pickle) instead of memory, keyed by
  session id, so the app can run with multiple workers.
- Swap the custom bipartite-graph visualisation for a proper
  [Escher](https://escher.readthedocs.io/) map if you have curated map JSON
  for your model of interest.
