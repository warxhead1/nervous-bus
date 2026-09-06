-- adapters/board/create_view.sql
--
-- GENERATED FILE -- do not hand-edit.
--   regenerate: python3 adapters/board/viewgen.py --emit > adapters/board/create_view.sql
--   verify:     python3 adapters/board/viewgen.py --check
--
-- Federated "total board" views: one row per issue, and one row per
-- dependency edge, across all 10 project beads DBs on the shared dolt SQL
-- server (127.0.0.1:39502, data_dir /home/eric/.beads/dolt). `beads_global` itself is
-- excluded -- it is the routing/federation DB, not a project with its own work
-- queue -- and it is where the views live, because it is the one federation-
-- aware DB every project already talks to and because writing the object into
-- a single project's DB would bias the "total" toward that project.
--
-- Idempotent: CREATE OR REPLACE, safe to re-run.
--
-- Apply with:
--   dolt --data-dir /home/eric/.beads/dolt sql < adapters/board/create_view.sql
--
-- SCHEMA DRIFT. bd >= 1.2 migrates a project DB's `dependencies` table from
-- `depends_on_id` to `depends_on_issue_id`/`depends_on_wisp_id`/
-- `depends_on_external`. The migration is lazy and per-DB (first bd 1.2 client
-- to touch that DB), so the fleet is mixed for as long as it takes every
-- project to get touched. A single stale arm does not degrade one project --
-- dolt rejects the WHOLE view ("references invalid table(s) or column(s)"),
-- so `blocked_by` dies for everyone at once. That is why these arms are
-- generated from live `information_schema` shape instead of hand-maintained:
-- the 2026-08-31 hand-written snapshot went stale when deer_flow migrated, and
-- the view was broken on 2026-09-06 until regenerated.
--
-- `all_dependencies` normalises both variants to the same 5 columns.
-- `depends_on_id` is the COALESCE of the split trio so no edge is lost, and
-- `depends_on_kind` ('issue'|'wisp'|'external'|'unknown') keeps the three
-- target kinds distinguishable -- board.py joins only `kind = 'issue'` rows to
-- `all_issues`, so a wisp/external id can never masquerade as a blocker.
--
-- Roster: dolt DB name -> repo-facing project name, and the resolved
-- `dependencies` variant pinned at 2026-09-06:
--   app_to_market              ->  app-to-market              [legacy]
--   biz_worthy                 ->  biz-worthy                 [split]
--   deer_flow                  ->  deer-flow                  [split]
--   hearth                     ->  hearth                     [split]
--   hearth-loom                ->  hearth-loom                [split]
--   nervous_bus                ->  nervous-bus                [split]
--   sweepers_adventures        ->  sweepers-adventures        [split]
--   temple_stuart_accounting   ->  temple-stuart-accounting   [legacy]
--   tengine                    ->  tengine                    [split]
--   unreal_battlebots_gamedev  ->  unreal-battlebots-gamedev  [legacy]

CREATE OR REPLACE VIEW `beads_global`.`all_issues` AS
SELECT 'app-to-market' AS project, `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `app_to_market`.`issues`
UNION ALL
SELECT 'biz-worthy', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `biz_worthy`.`issues`
UNION ALL
SELECT 'deer-flow', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `deer_flow`.`issues`
UNION ALL
SELECT 'hearth', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `hearth`.`issues`
UNION ALL
SELECT 'hearth-loom', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `hearth-loom`.`issues`
UNION ALL
SELECT 'nervous-bus', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `nervous_bus`.`issues`
UNION ALL
SELECT 'sweepers-adventures', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `sweepers_adventures`.`issues`
UNION ALL
SELECT 'temple-stuart-accounting', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `temple_stuart_accounting`.`issues`
UNION ALL
SELECT 'tengine', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `tengine`.`issues`
UNION ALL
SELECT 'unreal-battlebots-gamedev', `id`, `title`, `description`, `status`, `priority`, `issue_type`, `assignee`, `created_at`, `updated_at`, `closed_at`, `notes`
  FROM `unreal_battlebots_gamedev`.`issues`;

CREATE OR REPLACE VIEW `beads_global`.`all_dependencies` AS
SELECT `issue_id` AS `issue_id`, `depends_on_id` AS `depends_on_id`,
       `type` AS `type`, `created_at` AS `created_at`,
       CASE WHEN `depends_on_id` IS NOT NULL THEN 'issue' ELSE 'unknown' END AS `depends_on_kind`
  FROM `app_to_market`.`dependencies`
UNION ALL
SELECT `issue_id`, COALESCE(`depends_on_issue_id`, `depends_on_wisp_id`, `depends_on_external`),
       `type`, `created_at`,
       CASE WHEN `depends_on_issue_id` IS NOT NULL THEN 'issue' WHEN `depends_on_wisp_id` IS NOT NULL THEN 'wisp' WHEN `depends_on_external` IS NOT NULL THEN 'external' ELSE 'unknown' END
  FROM `biz_worthy`.`dependencies`
UNION ALL
SELECT `issue_id`, COALESCE(`depends_on_issue_id`, `depends_on_wisp_id`, `depends_on_external`),
       `type`, `created_at`,
       CASE WHEN `depends_on_issue_id` IS NOT NULL THEN 'issue' WHEN `depends_on_wisp_id` IS NOT NULL THEN 'wisp' WHEN `depends_on_external` IS NOT NULL THEN 'external' ELSE 'unknown' END
  FROM `deer_flow`.`dependencies`
UNION ALL
SELECT `issue_id`, COALESCE(`depends_on_issue_id`, `depends_on_wisp_id`, `depends_on_external`),
       `type`, `created_at`,
       CASE WHEN `depends_on_issue_id` IS NOT NULL THEN 'issue' WHEN `depends_on_wisp_id` IS NOT NULL THEN 'wisp' WHEN `depends_on_external` IS NOT NULL THEN 'external' ELSE 'unknown' END
  FROM `hearth`.`dependencies`
UNION ALL
SELECT `issue_id`, COALESCE(`depends_on_issue_id`, `depends_on_wisp_id`, `depends_on_external`),
       `type`, `created_at`,
       CASE WHEN `depends_on_issue_id` IS NOT NULL THEN 'issue' WHEN `depends_on_wisp_id` IS NOT NULL THEN 'wisp' WHEN `depends_on_external` IS NOT NULL THEN 'external' ELSE 'unknown' END
  FROM `hearth-loom`.`dependencies`
UNION ALL
SELECT `issue_id`, COALESCE(`depends_on_issue_id`, `depends_on_wisp_id`, `depends_on_external`),
       `type`, `created_at`,
       CASE WHEN `depends_on_issue_id` IS NOT NULL THEN 'issue' WHEN `depends_on_wisp_id` IS NOT NULL THEN 'wisp' WHEN `depends_on_external` IS NOT NULL THEN 'external' ELSE 'unknown' END
  FROM `nervous_bus`.`dependencies`
UNION ALL
SELECT `issue_id`, COALESCE(`depends_on_issue_id`, `depends_on_wisp_id`, `depends_on_external`),
       `type`, `created_at`,
       CASE WHEN `depends_on_issue_id` IS NOT NULL THEN 'issue' WHEN `depends_on_wisp_id` IS NOT NULL THEN 'wisp' WHEN `depends_on_external` IS NOT NULL THEN 'external' ELSE 'unknown' END
  FROM `sweepers_adventures`.`dependencies`
UNION ALL
SELECT `issue_id`, `depends_on_id`,
       `type`, `created_at`,
       CASE WHEN `depends_on_id` IS NOT NULL THEN 'issue' ELSE 'unknown' END
  FROM `temple_stuart_accounting`.`dependencies`
UNION ALL
SELECT `issue_id`, COALESCE(`depends_on_issue_id`, `depends_on_wisp_id`, `depends_on_external`),
       `type`, `created_at`,
       CASE WHEN `depends_on_issue_id` IS NOT NULL THEN 'issue' WHEN `depends_on_wisp_id` IS NOT NULL THEN 'wisp' WHEN `depends_on_external` IS NOT NULL THEN 'external' ELSE 'unknown' END
  FROM `tengine`.`dependencies`
UNION ALL
SELECT `issue_id`, `depends_on_id`,
       `type`, `created_at`,
       CASE WHEN `depends_on_id` IS NOT NULL THEN 'issue' ELSE 'unknown' END
  FROM `unreal_battlebots_gamedev`.`dependencies`;
