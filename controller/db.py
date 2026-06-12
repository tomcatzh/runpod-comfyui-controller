from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from typing import Any

from .config import Settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_requests (
  id TEXT PRIMARY KEY,
  product TEXT NOT NULL,
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  poll_after_seconds INTEGER NOT NULL DEFAULT 15,
  requested_json TEXT NOT NULL,
  result_json TEXT,
  error TEXT,
  session_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  request_id TEXT,
  product TEXT NOT NULL,
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  phase TEXT NOT NULL DEFAULT 'created',
  data_center_id TEXT NOT NULL,
  min_vram_gb INTEGER NOT NULL DEFAULT 24,
  gpu_vendor TEXT NOT NULL DEFAULT 'NVIDIA',
  max_gpu_usd_per_hr REAL,
  max_total_usd REAL,
  lease_until TEXT,
  hard_terminate_at TEXT,
  idle_shutdown_at TEXT,
  reclaim_warning_at TEXT,
  watchdog_paused INTEGER NOT NULL DEFAULT 0,
  watchdog_last_checked_at TEXT,
  watchdog_last_reason TEXT,
  output_collection_state TEXT,
  output_collection_last_checked_at TEXT,
  output_collection_last_error TEXT,
  output_collection_file_count INTEGER NOT NULL DEFAULT 0,
  output_collection_bytes INTEGER NOT NULL DEFAULT 0,
  output_collection_retained_volume INTEGER NOT NULL DEFAULT 0,
  missing_finalization_reason TEXT,
  ui_url TEXT,
  network_volume_id TEXT,
  hydration_id TEXT,
  cpu_pod_id TEXT,
  gpu_pod_id TEXT,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  actual_cost_usd REAL,
  actual_cost_observed_at TEXT,
  billed_start_at TEXT,
  billed_end_at TEXT,
  retention_policy TEXT NOT NULL DEFAULT 'delete_after_collection',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gpu_acquisition_attempts (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  state TEXT NOT NULL,
  data_center_id TEXT NOT NULL,
  gpu_type_id TEXT,
  quoted_cost_usd_per_hr REAL,
  quote_source TEXT,
  provider_pod_id TEXT,
  error TEXT,
  backoff_seconds INTEGER NOT NULL DEFAULT 0,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tunnels (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  pod_id TEXT,
  provider_pod_id TEXT,
  protocol TEXT NOT NULL DEFAULT 'ssh',
  remote_host TEXT NOT NULL DEFAULT '127.0.0.1',
  remote_port INTEGER NOT NULL,
  local_host TEXT NOT NULL DEFAULT '127.0.0.1',
  local_port INTEGER NOT NULL,
  state TEXT NOT NULL,
  pid INTEGER,
  restart_count INTEGER NOT NULL DEFAULT 0,
  auto_recover INTEGER NOT NULL DEFAULT 1,
  health_url TEXT,
  last_health_check_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchdog_events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  reason TEXT NOT NULL,
  observed_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS network_volumes (
  id TEXT PRIMARY KEY,
  provider_volume_id TEXT NOT NULL,
  name TEXT NOT NULL,
  data_center_id TEXT NOT NULL,
  size_gb INTEGER NOT NULL,
  state TEXT NOT NULL,
  hydration_state TEXT NOT NULL DEFAULT 'not_started',
  hydration_ttl_until TEXT,
  retention_policy TEXT NOT NULL DEFAULT 'delete_after_collection',
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  actual_cost_usd REAL,
  actual_cost_observed_at TEXT,
  billed_start_at TEXT,
  billed_end_at TEXT,
  billed_time_ms INTEGER,
  billing_source TEXT,
  last_payload_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS pods (
  id TEXT PRIMARY KEY,
  provider_pod_id TEXT NOT NULL,
  session_id TEXT,
  volume_id TEXT,
  role TEXT NOT NULL,
  compute_type TEXT NOT NULL,
  state TEXT NOT NULL,
  data_center_id TEXT NOT NULL,
  image TEXT,
  cpu_flavor_ids TEXT,
  gpu_type_id TEXT,
  cost_per_hr REAL,
  actual_cost_usd REAL,
  actual_cost_observed_at TEXT,
  billed_start_at TEXT,
  billed_end_at TEXT,
  billed_time_ms INTEGER,
  billing_source TEXT,
  last_payload_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  stopped_at TEXT,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS hydration_requests (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  volume_id TEXT NOT NULL,
  state TEXT NOT NULL,
  assets_json TEXT NOT NULL,
  cpu_pod_id TEXT,
  artifact_root TEXT,
  ttl_until TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS asset_metadata_cache (
  id TEXT PRIMARY KEY,
  product TEXT NOT NULL,
  url_key TEXT NOT NULL,
  original_url_redacted TEXT NOT NULL,
  final_url_redacted TEXT,
  provider TEXT NOT NULL,
  model_folder TEXT NOT NULL,
  filename TEXT NOT NULL,
  size_bytes INTEGER,
  size_unknown INTEGER NOT NULL DEFAULT 0,
  content_type TEXT,
  etag TEXT,
  last_modified TEXT,
  redirects_json TEXT NOT NULL,
  target TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(product, url_key, model_folder)
);

CREATE TABLE IF NOT EXISTS model_templates (
  id TEXT PRIMARY KEY,
  product TEXT NOT NULL,
  name TEXT NOT NULL,
  assets_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(product, name)
);

CREATE TABLE IF NOT EXISTS comfyui_launch_templates (
  id TEXT PRIMARY KEY,
  product TEXT NOT NULL,
  name TEXT NOT NULL,
  ui_workflow_json TEXT NOT NULL,
  api_workflow_json TEXT,
  assets_json TEXT NOT NULL,
  custom_nodes_json TEXT NOT NULL,
  analyzer_result_json TEXT NOT NULL,
  install_plan_json TEXT NOT NULL,
  validation_plan_json TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  last_probe_id TEXT,
  last_probe_result_json TEXT,
  bake_candidate_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(product, name)
);

CREATE TABLE IF NOT EXISTS comfyui_workflows (
  id TEXT PRIMARY KEY,
  product TEXT NOT NULL,
  name TEXT NOT NULL,
  workflow_hash TEXT NOT NULL,
  canonical_workflow_json TEXT NOT NULL,
  original_filename TEXT,
  analysis_json TEXT NOT NULL,
  extracted_assets_json TEXT NOT NULL,
  extra_assets_json TEXT NOT NULL,
  node_mappings_json TEXT NOT NULL,
  node_locks_json TEXT NOT NULL,
  install_plan_json TEXT NOT NULL,
  validation_plan_json TEXT NOT NULL,
  base_template_lock_json TEXT NOT NULL,
  dependency_fingerprint TEXT NOT NULL,
  launch_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  verification_state TEXT NOT NULL,
  last_probe_id TEXT,
  last_live_verified_session_id TEXT,
  last_verified_output_path TEXT,
  verified_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(product, workflow_hash)
);

CREATE TABLE IF NOT EXISTS comfyui_dependency_probes (
  id TEXT PRIMARY KEY,
  template_id TEXT,
  workflow_id TEXT,
  product TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  state TEXT NOT NULL,
  volume_id TEXT,
  cpu_pod_id TEXT,
  result_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS session_workflows (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  state TEXT NOT NULL,
  winner_candidate_id TEXT,
  launch_template_id TEXT,
  comfyui_workflow_id TEXT,
  dependency_fingerprint TEXT,
  launch_fingerprint TEXT,
  selected_data_centers_json TEXT NOT NULL,
  excluded_data_centers_json TEXT NOT NULL,
  assets_json TEXT NOT NULL,
  ui_workflow_json TEXT,
  api_workflow_json TEXT,
  analyzer_result_json TEXT,
  probe_id TEXT,
  probe_result_json TEXT,
  custom_nodes_json TEXT,
  install_plan_json TEXT,
  validation_plan_json TEXT,
  volume_size_gb INTEGER NOT NULL,
  min_vram_gb INTEGER NOT NULL DEFAULT 24,
  gpu_vendor TEXT NOT NULL DEFAULT 'NVIDIA',
  max_gpu_usd_per_hr REAL,
  max_total_usd REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_candidates (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  data_center_id TEXT NOT NULL,
  state TEXT NOT NULL,
  volume_id TEXT,
  cpu_pod_id TEXT,
  gpu_pod_id TEXT,
  hydration_id TEXT,
  gpu_type_id TEXT,
  quoted_cost_usd_per_hr REAL,
  download_done_bytes INTEGER NOT NULL DEFAULT 0,
  download_total_bytes INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  cleanup_status TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_events (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  candidate_id TEXT,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  details_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comfyui_live_verifications (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  launch_fingerprint TEXT NOT NULL,
  output_artifact_path TEXT,
  output_checksum_sha256 TEXT,
  object_info_result_json TEXT,
  model_visibility_result_json TEXT,
  node_visibility_result_json TEXT,
  base_template_lock_json TEXT,
  cost_snapshot_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS output_collections (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  volume_id TEXT,
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  file_count INTEGER NOT NULL DEFAULT 0,
  byte_count INTEGER NOT NULL DEFAULT 0,
  downloaded_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  volume_delete_allowed INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_model_operations (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  operation_type TEXT NOT NULL,
  state TEXT NOT NULL,
  source_url_key TEXT,
  source_url_redacted TEXT,
  provider TEXT,
  source_path TEXT,
  target_path TEXT,
  model_folder TEXT,
  filename TEXT,
  size_bytes INTEGER,
  checksum_sha256 TEXT,
  progress_json TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  state TEXT NOT NULL,
  workflow_ref TEXT,
  prompt TEXT,
  metadata_json TEXT,
  external_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  task_id TEXT,
  kind TEXT NOT NULL,
  local_path TEXT NOT NULL,
  remote_uri TEXT,
  checksum_sha256 TEXT,
  size_bytes INTEGER,
  mime_type TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_events (
  id TEXT PRIMARY KEY,
  session_id TEXT,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  amount_usd REAL NOT NULL DEFAULT 0,
  unit_price_usd_per_hr REAL,
  occurred_at TEXT NOT NULL,
  details_json TEXT
);

CREATE TABLE IF NOT EXISTS billing_records (
  id TEXT PRIMARY KEY,
  record_key TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  provider TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT,
  provider_resource_id TEXT,
  bucket_start_at TEXT NOT NULL,
  bucket_end_at TEXT,
  bucket_size TEXT NOT NULL,
  amount_usd REAL NOT NULL DEFAULT 0,
  time_billed_ms INTEGER,
  disk_space_billed_gb REAL,
  raw_json TEXT NOT NULL,
  observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  details_json TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resource_requests_state ON resource_requests(state);
CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
CREATE INDEX IF NOT EXISTS idx_sessions_volume ON sessions(network_volume_id);
CREATE INDEX IF NOT EXISTS idx_pods_session ON pods(session_id);
CREATE INDEX IF NOT EXISTS idx_gpu_attempts_session ON gpu_acquisition_attempts(session_id);
CREATE INDEX IF NOT EXISTS idx_tunnels_session ON tunnels(session_id);
CREATE INDEX IF NOT EXISTS idx_watchdog_session ON watchdog_events(session_id);
CREATE INDEX IF NOT EXISTS idx_volumes_state ON network_volumes(state);
CREATE INDEX IF NOT EXISTS idx_hydration_volume ON hydration_requests(volume_id);
CREATE INDEX IF NOT EXISTS idx_asset_cache_product ON asset_metadata_cache(product);
CREATE INDEX IF NOT EXISTS idx_model_templates_product ON model_templates(product);
CREATE INDEX IF NOT EXISTS idx_comfyui_launch_templates_product ON comfyui_launch_templates(product);
CREATE INDEX IF NOT EXISTS idx_comfyui_workflows_product ON comfyui_workflows(product);
CREATE INDEX IF NOT EXISTS idx_comfyui_workflows_hash ON comfyui_workflows(product, workflow_hash);
CREATE INDEX IF NOT EXISTS idx_comfyui_dependency_probes_fingerprint ON comfyui_dependency_probes(fingerprint);
CREATE INDEX IF NOT EXISTS idx_workflows_session ON session_workflows(session_id);
CREATE INDEX IF NOT EXISTS idx_candidates_workflow ON workflow_candidates(workflow_id);
CREATE INDEX IF NOT EXISTS idx_workflow_events_session ON workflow_events(session_id);
CREATE INDEX IF NOT EXISTS idx_comfyui_live_verifications_workflow ON comfyui_live_verifications(workflow_id);
CREATE INDEX IF NOT EXISTS idx_output_collections_session ON output_collections(session_id);
CREATE INDEX IF NOT EXISTS idx_session_model_operations_session ON session_model_operations(session_id);
CREATE INDEX IF NOT EXISTS idx_session_model_operations_url ON session_model_operations(session_id, source_url_key);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);
CREATE INDEX IF NOT EXISTS idx_billing_records_resource ON billing_records(resource_type, resource_id);
CREATE INDEX IF NOT EXISTS idx_billing_records_provider ON billing_records(resource_type, provider_resource_id);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_events(subject_type, subject_id);
"""


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_dirs()
        self.path = settings.db_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.executescript(SCHEMA)
                self._ensure_columns(conn)
                conn.execute("PRAGMA user_version = 1")

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        self._ensure_table_columns(
            conn,
            "sessions",
            {
                "actual_cost_usd": "REAL",
                "actual_cost_observed_at": "TEXT",
                "billed_start_at": "TEXT",
                "billed_end_at": "TEXT",
                "phase": "TEXT NOT NULL DEFAULT 'created'",
                "idle_shutdown_at": "TEXT",
                "reclaim_warning_at": "TEXT",
                "watchdog_paused": "INTEGER NOT NULL DEFAULT 0",
                "watchdog_last_checked_at": "TEXT",
                "watchdog_last_reason": "TEXT",
                "output_collection_state": "TEXT",
                "output_collection_last_checked_at": "TEXT",
                "output_collection_last_error": "TEXT",
                "output_collection_file_count": "INTEGER NOT NULL DEFAULT 0",
                "output_collection_bytes": "INTEGER NOT NULL DEFAULT 0",
                "output_collection_retained_volume": "INTEGER NOT NULL DEFAULT 0",
                "missing_finalization_reason": "TEXT",
                "min_vram_gb": "INTEGER NOT NULL DEFAULT 24",
                "gpu_vendor": "TEXT NOT NULL DEFAULT 'NVIDIA'",
            },
        )
        self._drop_table_column_if_exists(conn, "sessions", "gpu_profile")
        self._drop_table_column_if_exists(conn, "sessions", "allow_live_gpu")
        self._ensure_table_columns(
            conn,
            "network_volumes",
            {
                "deleted_at": "TEXT",
                "actual_cost_usd": "REAL",
                "actual_cost_observed_at": "TEXT",
                "billed_start_at": "TEXT",
                "billed_end_at": "TEXT",
                "billed_time_ms": "INTEGER",
                "billing_source": "TEXT",
                "warm_expires_at": "TEXT",
                "warm_assets_key": "TEXT",
                "warm_session_id": "TEXT",
            },
        )
        self._ensure_table_columns(
            conn,
            "pods",
            {
                "actual_cost_usd": "REAL",
                "actual_cost_observed_at": "TEXT",
                "billed_start_at": "TEXT",
                "billed_end_at": "TEXT",
                "billed_time_ms": "INTEGER",
                "billing_source": "TEXT",
            },
        )
        self._drop_table_column_if_exists(conn, "pods", "billing_empty_sync_count")
        self._drop_table_column_if_exists(conn, "pods", "billing_first_empty_sync_at")
        self._drop_table_column_if_exists(conn, "pods", "billing_last_empty_sync_at")
        self._ensure_table_columns(
            conn,
            "session_workflows",
            {
                "min_vram_gb": "INTEGER NOT NULL DEFAULT 24",
                "gpu_vendor": "TEXT NOT NULL DEFAULT 'NVIDIA'",
                "launch_template_id": "TEXT",
                "comfyui_workflow_id": "TEXT",
                "dependency_fingerprint": "TEXT",
                "launch_fingerprint": "TEXT",
                "ui_workflow_json": "TEXT",
                "api_workflow_json": "TEXT",
                "analyzer_result_json": "TEXT",
                "probe_id": "TEXT",
                "probe_result_json": "TEXT",
                "custom_nodes_json": "TEXT",
                "install_plan_json": "TEXT",
                "validation_plan_json": "TEXT",
            },
        )
        self._ensure_table_columns(
            conn,
            "hydration_requests",
            {
                "launch_template_id": "TEXT",
                "install_plan_json": "TEXT",
                "validation_plan_json": "TEXT",
                "ui_workflow_json": "TEXT",
                "api_workflow_json": "TEXT",
                "custom_nodes_json": "TEXT",
            },
        )
        self._ensure_table_columns(
            conn,
            "comfyui_dependency_probes",
            {
                "workflow_id": "TEXT",
            },
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_model_operations (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              operation_type TEXT NOT NULL,
              state TEXT NOT NULL,
              source_url_key TEXT,
              source_url_redacted TEXT,
              provider TEXT,
              source_path TEXT,
              target_path TEXT,
              model_folder TEXT,
              filename TEXT,
              size_bytes INTEGER,
              checksum_sha256 TEXT,
              progress_json TEXT NOT NULL DEFAULT '{}',
              error TEXT,
              started_at TEXT,
              finished_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_model_operations_session ON session_model_operations(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_model_operations_url ON session_model_operations(session_id, source_url_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_phase ON sessions(phase)")
        self._clear_deleted_comfyui_workflows(conn)

    def _clear_deleted_comfyui_workflows(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE comfyui_workflows
               SET analysis_json = '{}',
                   extracted_assets_json = '[]',
                   extra_assets_json = '[]',
                   node_mappings_json = '{}',
                   node_locks_json = '[]',
                   install_plan_json = '{"version":1,"steps":[]}',
                   validation_plan_json = '{}',
                   base_template_lock_json = '{}',
                   dependency_fingerprint = '',
                   launch_fingerprint = '',
                   verification_state = 'unverified',
                   last_probe_id = NULL,
                   last_live_verified_session_id = NULL,
                   last_verified_output_path = NULL,
                   verified_at = NULL
             WHERE status = 'deleted'
            """
        )

    def _ensure_table_columns(self, conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _drop_table_column_if_exists(self, conn: sqlite3.Connection, table: str, column: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in existing:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(sql, tuple(params))

    def execute_with_rowcount(self, sql: str, params: Iterable[Any] = ()) -> int:
        with closing(self.connect()) as conn:
            with conn:
                return conn.execute(sql, tuple(params)).rowcount

    def insert(self, table: str, values: dict[str, Any]) -> None:
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(sql, [values[column] for column in columns])

    def update(self, table: str, row_id: str, values: dict[str, Any]) -> None:
        assignments = ", ".join(f"{column} = ?" for column in values)
        sql = f"UPDATE {table} SET {assignments} WHERE id = ?"
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(sql, [*values.values(), row_id])

    def get(self, table: str, row_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with closing(self.connect()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]
