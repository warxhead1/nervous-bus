-- adapters/board/create_view.sql
--
-- Federated "total board" view: one row per issue across all 10 project
-- beads DBs living on the shared dolt SQL server (127.0.0.1:39502,
-- data_dir /home/eric/.beads/dolt). beads_global itself is excluded (it is
-- the routing/federation DB, not a project with its own work queue).
--
-- Idempotent: CREATE OR REPLACE, safe to re-run. Lives in beads_global
-- because that DB is the one federation-aware DB every project already
-- talks to (federation_peers/routes tables), and because writing a new
-- object into a single project's DB would bias the "total" toward that
-- project.
--
-- DB name -> repo-facing project name mapping (snake_case dolt DB name on
-- the left, kebab-case repo name on the right; hearth-loom's DB name
-- already has the hyphen and needs backticks everywhere since `-` is not a
-- valid unquoted SQL identifier character):
--   app_to_market             -> app-to-market
--   biz_worthy                -> biz-worthy
--   deer_flow                 -> deer-flow
--   hearth                    -> hearth
--   hearth-loom               -> hearth-loom   (backtick-quoted DB name)
--   nervous_bus               -> nervous-bus
--   sweepers_adventures       -> sweepers-adventures
--   temple_stuart_accounting  -> temple-stuart-accounting
--   tengine                   -> tengine
--   unreal_battlebots_gamedev -> unreal-battlebots-gamedev
--
-- Apply with:
--   dolt --data-dir /home/eric/.beads/dolt sql < adapters/board/create_view.sql

CREATE OR REPLACE VIEW beads_global.all_issues AS
SELECT 'app-to-market' AS project, id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM app_to_market.issues
UNION ALL
SELECT 'biz-worthy', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM biz_worthy.issues
UNION ALL
SELECT 'deer-flow', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM deer_flow.issues
UNION ALL
SELECT 'hearth', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM hearth.issues
UNION ALL
SELECT 'hearth-loom', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM `hearth-loom`.issues
UNION ALL
SELECT 'nervous-bus', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM nervous_bus.issues
UNION ALL
SELECT 'sweepers-adventures', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM sweepers_adventures.issues
UNION ALL
SELECT 'temple-stuart-accounting', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM temple_stuart_accounting.issues
UNION ALL
SELECT 'tengine', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM tengine.issues
UNION ALL
SELECT 'unreal-battlebots-gamedev', id, title, description, status, priority,
       issue_type, assignee, created_at, updated_at, closed_at, notes
  FROM unreal_battlebots_gamedev.issues;

-- Companion view: dependency edges across every project DB, needed to derive
-- board.py's per-issue `blocked_by` list. beads_global.blocked_issues (the
-- per-DB view that already exists in every project DB) only exposes a
-- COUNT, not the actual blocking ids, so board.py needs the raw edges.
-- Issue ids already carry their project as a prefix (e.g. "hearth-01i",
-- "nervous-bus-05g1"), so no separate project column is required here --
-- board.py can resolve an id's project the same way it resolves it for
-- all_issues rows.
-- SCHEMA DRIFT WARNING: bd >= 1.2 migrates a project DB's dependencies table
-- from `depends_on_id` to `depends_on_issue_id`/`depends_on_wisp_id`/
-- `depends_on_external`. The migration runs per-DB (first bd 1.2 client to
-- touch it), so the fleet is mixed: as of 2026-08-31 biz_worthy, hearth,
-- hearth-loom, nervous_bus, sweepers_adventures and tengine are on the NEW
-- schema; app_to_market, deer_flow, temple_stuart_accounting and
-- unreal_battlebots_gamedev are still OLD. Each SELECT below must match its DB's current
-- schema — a single stale arm breaks the whole view for every consumer
-- (bit 2026-08-31: maintenance-train selector died at 05:01, zero dispatch).
-- When another DB migrates, move its arm to the COALESCE form and re-apply.
CREATE OR REPLACE VIEW beads_global.all_dependencies AS
SELECT issue_id, depends_on_id, type, created_at FROM app_to_market.dependencies
UNION ALL
SELECT issue_id, COALESCE(depends_on_issue_id, depends_on_wisp_id, depends_on_external), type, created_at FROM biz_worthy.dependencies
UNION ALL
SELECT issue_id, depends_on_id, type, created_at FROM deer_flow.dependencies
UNION ALL
SELECT issue_id, COALESCE(depends_on_issue_id, depends_on_wisp_id, depends_on_external), type, created_at FROM hearth.dependencies
UNION ALL
SELECT issue_id, COALESCE(depends_on_issue_id, depends_on_wisp_id, depends_on_external), type, created_at FROM `hearth-loom`.dependencies
UNION ALL
SELECT issue_id, COALESCE(depends_on_issue_id, depends_on_wisp_id, depends_on_external), type, created_at FROM nervous_bus.dependencies
UNION ALL
SELECT issue_id, COALESCE(depends_on_issue_id, depends_on_wisp_id, depends_on_external), type, created_at FROM sweepers_adventures.dependencies
UNION ALL
SELECT issue_id, depends_on_id, type, created_at FROM temple_stuart_accounting.dependencies
UNION ALL
SELECT issue_id, COALESCE(depends_on_issue_id, depends_on_wisp_id, depends_on_external), type, created_at FROM tengine.dependencies
UNION ALL
SELECT issue_id, depends_on_id, type, created_at FROM unreal_battlebots_gamedev.dependencies;
