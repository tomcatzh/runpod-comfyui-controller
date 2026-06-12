from __future__ import annotations

import datetime as dt
import html
import json
import re
from typing import Any

from .costing import TERMINAL_STATES, split_active_recent
from .i18n import get_locale, js_translation_bundle, t, t_column


BASE_CSS = r"""
    :root {
      --bg: #f3f5fa; --surface: #ffffff; --surface-2: #f8fafc;
      --border: #e2e8f0; --border-strong: #cbd5e1;
      --text: #0f172a; --muted: #64748b;
      --accent: #0f766e; --accent-strong: #0d5f59; --accent-contrast: #ffffff; --accent-soft: #f0fdfa;
      --danger: #b91c1c; --danger-soft: #fef2f2;
      --warn: #92400e; --warn-soft: #fffbeb;
      --ok: #166534; --ok-soft: #f0fdf4;
      --neutral: #475569; --neutral-soft: #f1f5f9;
      --radius: 12px; --radius-sm: 8px;
      --shadow: 0 1px 2px rgba(15, 23, 42, .04), 0 6px 20px -12px rgba(15, 23, 42, .18);
      color-scheme: light dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0b1019; --surface: #131a26; --surface-2: #0f1622;
        --border: #243043; --border-strong: #334155;
        --text: #e2e8f0; --muted: #94a3b8;
        --accent: #14b8a6; --accent-strong: #2dd4bf; --accent-contrast: #04211d; --accent-soft: #11302d;
        --danger: #f87171; --danger-soft: #2c1212;
        --warn: #fbbf24; --warn-soft: #2a2208;
        --ok: #4ade80; --ok-soft: #0c2616;
        --neutral: #94a3b8; --neutral-soft: #1c2636;
        --shadow: none;
      }
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5; }
    header.site {
      position: sticky; top: 0; z-index: 30;
      display: flex; align-items: center; justify-content: space-between; gap: 18px;
      padding: 10px 24px; border-bottom: 1px solid var(--border);
      background: color-mix(in srgb, var(--surface) 86%, transparent);
      backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    }
    .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 15px; letter-spacing: .01em; }
    .brand-dot { width: 10px; height: 10px; border-radius: 3px; background: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft); }
    main { max-width: 1200px; margin: 0 auto; padding: 26px 24px 64px; }
    h1 { font-size: 20px; margin: 0; }
    h2 { font-size: 16px; margin: 26px 0 10px; letter-spacing: -.01em; }
    h3 { font-size: 13px; margin: 18px 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    nav.site-nav { display: flex; align-items: center; gap: 4px; }
    nav.site-nav a { padding: 6px 12px; border-radius: 999px; color: var(--muted); font-weight: 500; }
    nav.site-nav a:hover { background: var(--neutral-soft); color: var(--text); text-decoration: none; }
    nav.site-nav a.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px 18px; }
    .card + .card { margin-top: 14px; }
    .card > h2:first-child { margin-top: 0; }
    form { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px; margin: 0 0 18px; }
    dialog { width: min(980px, calc(100vw - 32px)); border: 1px solid var(--border-strong); border-radius: var(--radius); padding: 0; background: var(--surface); color: var(--text); }
    dialog::backdrop { background: rgba(2, 8, 20, .55); }
    dialog form { border: 0; margin: 0; }
    .page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 14px; margin-bottom: 18px; flex-wrap: wrap; }
    .page-head h2 { margin: 0 0 4px; font-size: 20px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
    .form-grid { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 12px; align-items: start; }
    .form-row { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .form-row .subtle { overflow-wrap: anywhere; line-height: 1.35; }
    .form-row label { min-height: 18px; display: flex; align-items: center; gap: 4px; }
    .form-row.wide { grid-column: 1 / -1; }
    .asset-grid { grid-column: 1 / -1; display: grid; grid-template-columns: minmax(220px, 1fr) 170px 180px; gap: 8px; align-items: end; }
    .asset-list { grid-column: 1 / -1; }
    .asset-list table input { min-width: 110px; }
    .cand-list { display: flex; flex-direction: column; gap: 8px; }
    details.cand { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; }
    details.cand.cand-won { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
    details.cand.cand-deleted { opacity: .62; }
    details.cand > summary {
      list-style: none; cursor: pointer; user-select: none;
      display: grid; grid-template-columns: minmax(88px, 150px) max-content minmax(180px, 1fr) max-content 16px;
      gap: 14px; align-items: center; padding: 10px 14px;
    }
    details.cand > summary::-webkit-details-marker { display: none; }
    details.cand > summary:hover { background: var(--surface-2); }
    .cand-dc { font-weight: 700; font-size: 14px; white-space: nowrap; }
    .cand-progress { display: flex; align-items: center; gap: 10px; min-width: 0; }
    .cand-progress progress { flex: 1; height: 6px; accent-color: var(--accent); min-width: 60px; }
    .cand-progress .subtle { white-space: nowrap; }
    .cand-meta { white-space: nowrap; text-align: right; }
    .cand-chevron { color: var(--muted); font-size: 11px; transition: transform .15s ease; justify-self: end; }
    details.cand[open] > summary .cand-chevron { transform: rotate(180deg); }
    details.cand[open] > summary { border-bottom: 1px solid var(--border); }
    .cand-body { padding: 12px 14px; background: var(--surface-2); display: grid; gap: 10px; }
    .cand-error { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--danger); background: var(--danger-soft); border-radius: var(--radius-sm); padding: 8px 10px; overflow-wrap: anywhere; white-space: pre-wrap; }
    .cand-note { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--muted); background: var(--neutral-soft); border-radius: var(--radius-sm); padding: 8px 10px; overflow-wrap: anywhere; white-space: pre-wrap; }
    @media (max-width: 760px) {
      details.cand > summary { grid-template-columns: 1fr max-content 16px; }
      .cand-progress { grid-column: 1 / -1; }
      .cand-meta { grid-column: 1 / 2; text-align: left; }
    }
    .wizard { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 16px; align-items: start; }
    .stepper { position: sticky; top: 68px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 10px; }
    .stepper button { width: 100%; text-align: left; margin-bottom: 6px; border: 0; background: transparent; border-radius: var(--radius-sm); padding: 8px 10px; font-weight: 500; display: flex; align-items: center; gap: 9px; }
    .stepper button:hover { background: var(--neutral-soft); }
    .stepper button.active { background: var(--accent); color: var(--accent-contrast); }
    .step-mark { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; border-radius: 999px; background: var(--neutral-soft); color: var(--muted); font-size: 11px; font-weight: 700; flex: none; }
    .stepper button.active .step-mark { background: color-mix(in srgb, var(--accent-contrast) 25%, transparent); color: var(--accent-contrast); }
    .stepper button.done .step-mark { background: var(--ok-soft); color: var(--ok); }
    .stepper button.done:not(.active) { color: var(--ok); }
    .wizard-banner {
      display: flex; align-items: center; gap: 12px;
      padding: 12px 16px; margin: 0 0 16px;
      background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent);
      border-radius: var(--radius); box-shadow: var(--shadow); font-size: 14px;
    }
    .wizard-banner .banner-icon { flex: none; width: 18px; height: 18px; border-radius: 999px; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 800; background: var(--accent-soft); color: var(--accent); }
    .wizard-banner #wizard-banner-text { flex: 1; min-width: 0; }
    .wizard-banner .banner-step { flex: none; color: var(--muted); font-size: 12px; font-weight: 600; white-space: nowrap; }
    .wizard-banner.success { border-left-color: var(--ok); }
    .wizard-banner.success .banner-icon { background: var(--ok-soft); color: var(--ok); }
    .wizard-banner.error { border-left-color: var(--danger); background: var(--danger-soft); }
    .wizard-banner.error .banner-icon { background: var(--surface); color: var(--danger); }
    .wizard-banner.busy .banner-icon { background: transparent; border: 2px solid var(--border-strong); border-top-color: var(--accent); animation: wizard-spin .8s linear infinite; color: transparent; }
    @keyframes wizard-spin { to { transform: rotate(360deg); } }
    .wizard-nav { display: flex; align-items: center; gap: 12px; margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--border); }
    .wizard-nav .nav-reason { flex: 1; color: var(--muted); font-size: 12px; text-align: right; }
    .help { display: inline-flex; align-items: center; justify-content: center; width: 15px; height: 15px; border-radius: 999px; background: var(--neutral-soft); color: var(--muted); font-size: 10px; font-weight: 700; cursor: help; position: relative; vertical-align: 1px; }
    .help::after {
      content: attr(data-tip); position: absolute; left: 50%; bottom: calc(100% + 8px); transform: translateX(-50%);
      width: 280px; background: var(--text); color: var(--bg); padding: 9px 11px; border-radius: 8px;
      font-size: 12px; font-weight: 400; line-height: 1.45; text-align: left; white-space: normal; text-transform: none; letter-spacing: 0;
      opacity: 0; pointer-events: none; transition: opacity .12s ease; z-index: 60;
    }
    .help:hover::after, .help:focus::after { opacity: 1; }
    #volume-estimate:empty { display: none; }
    .wizard-panel { display: none; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px; margin-bottom: 14px; }
    .wizard-panel.active { display: block; }
    .inline-grid { display: grid; grid-template-columns: minmax(220px, 1fr) 180px 140px; gap: 8px; align-items: end; }
    .node-resolution-grid { display: grid; grid-template-columns: minmax(420px, 1fr) minmax(150px, 180px); gap: 8px; align-items: end; }
    .row-actions { display: flex; gap: 6px; flex-wrap: wrap; }
    .operation-progress { display: none; margin: 10px 0 12px; padding: 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); }
    .operation-progress.active { display: block; }
    .operation-progress .progress-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 6px; }
    .operation-progress progress { width: 100%; height: 10px; accent-color: var(--accent); }
    .workflow-library-stack { display: grid; gap: 8px; max-width: 820px; }
    .workflow-name-row { display: grid; grid-template-columns: minmax(220px, 420px) max-content; gap: 8px; align-items: end; }
    .workflow-library-actions { justify-content: flex-start; }
    .workflow-upload-row { display: grid; grid-template-columns: minmax(220px, 520px) max-content; gap: 8px; align-items: center; }
    .workflow-name-row button, .workflow-upload-row button { justify-self: start; white-space: nowrap; }
    .asset-url-row td { background: var(--surface-2); padding-top: 0; }
    .asset-url-input { width: 100%; }
    .badge { display: inline-block; padding: 2px 8px; border: 1px solid var(--border-strong); border-radius: 999px; font-size: 12px; color: var(--muted); }
    label { color: var(--muted); font-size: 12px; font-weight: 600; }
    input, textarea, select { width: 100%; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); padding: 7px 10px; font: inherit; font-size: 13px; }
    input:focus, textarea:focus, select:focus { outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent); outline-offset: 0; border-color: var(--accent); }
    textarea { min-height: 88px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: top; }
    td { white-space: nowrap; }
    td.wrap { white-space: normal; min-width: 180px; max-width: 460px; overflow-wrap: anywhere; }
    tbody tr:last-child td { border-bottom: 0; }
    tbody tr:hover td { background: color-mix(in srgb, var(--neutral-soft) 55%, transparent); }
    th { color: var(--muted); background: var(--surface-2); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; position: sticky; top: 0; }
    button, .button {
      border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
      background: var(--surface); color: var(--text);
      padding: 7px 12px; font-size: 13px; font-weight: 500; cursor: pointer; display: inline-block;
      transition: background .12s ease, border-color .12s ease;
    }
    button:hover, .button:hover { background: var(--neutral-soft); text-decoration: none; }
    button:disabled, input:disabled, select:disabled { opacity: .5; cursor: not-allowed; }
    button:disabled:hover { background: var(--surface); }
    .primary, button.primary { background: var(--accent); color: var(--accent-contrast); border-color: var(--accent); }
    .primary:hover, button.primary:hover { background: var(--accent-strong); border-color: var(--accent-strong); }
    button.danger, .button.danger { border-color: color-mix(in srgb, var(--danger) 55%, var(--border-strong)); color: var(--danger); background: var(--surface); }
    button.danger:hover { background: var(--danger-soft); }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 0 0 18px; }
    .metric { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); padding: 14px 16px; color: var(--muted); font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
    .metric strong { display: block; font-size: 22px; margin-bottom: 4px; color: var(--text); font-weight: 700; letter-spacing: 0; text-transform: none; }
    .pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
    .pill::before { content: ""; width: 7px; height: 7px; border-radius: 999px; background: currentColor; }
    .pill-ok { background: var(--ok-soft); color: var(--ok); }
    .pill-warn { background: var(--warn-soft); color: var(--warn); }
    .pill-danger { background: var(--danger-soft); color: var(--danger); }
    .pill-neutral { background: var(--neutral-soft); color: var(--neutral); }
    .state { display: inline-block; padding: 2px 8px; border-radius: 999px; background: var(--neutral-soft); color: var(--neutral); font-size: 12px; font-weight: 600; }
    .subtle { color: var(--muted); font-size: 12px; }
    .tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--border); margin: 0 0 18px; flex-wrap: wrap; }
    .tabs a { padding: 9px 14px; color: var(--muted); font-weight: 500; border-bottom: 2px solid transparent; margin-bottom: -1px; }
    .tabs a:hover { color: var(--text); text-decoration: none; }
    .tabs a.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
    .session-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 14px; }
    .session-card { display: flex; flex-direction: column; gap: 12px; }
    .session-card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .session-card-head code { font-size: 13px; }
    .session-card-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; }
    .session-card-grid label { display: block; }
    .session-card-grid span { font-size: 13px; word-break: break-all; }
    .session-card-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: auto; }
    .empty-state { text-align: center; padding: 46px 20px; color: var(--muted); }
    .empty-state strong { display: block; font-size: 16px; color: var(--text); margin-bottom: 6px; }
    .kv-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 18px; }
    .kv-grid > div { min-width: 0; }
    .kv-grid label { display: block; margin-bottom: 2px; }
    .kv-grid span, .kv-grid code { font-size: 13px; word-break: break-all; }
    @media (max-width: 760px) {
      .form-grid { grid-template-columns: 1fr 1fr; }
      .asset-grid, .inline-grid, .node-resolution-grid, .workflow-name-row, .workflow-upload-row, .wizard { grid-template-columns: 1fr; }
      .stepper { position: static; }
      main { padding: 18px 14px 48px; }
    }
"""


_STATE_OK = {
    "running", "won", "succeeded", "hydrated", "passed", "configured", "healthy",
    "verified", "complete", "completed", "downloaded", "moved", "already_present",
    "accepted", "active",
}
_STATE_NEUTRAL = {
    "deleted", "reclaimed", "stopped", "closed", "terminated", "finalized",
    "exited", "skipped", "none", "terminal",
}


def state_pill(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    lowered = text.lower()
    if "fail" in lowered or "error" in lowered or lowered in {"cancelled", "missing", "interrupted"}:
        css = "danger"
    elif lowered.endswith("_ready") or lowered in _STATE_OK:
        css = "ok"
    elif lowered in _STATE_NEUTRAL:
        css = "neutral"
    else:
        css = "warn"
    return f'<span class="pill pill-{css}">{html.escape(text)}</span>'


def page(title: str, body: str, *, nav: str = "") -> bytes:
    nav_links = "".join(
        f'<a href="{href}" class="{"active" if nav == key else ""}">{t(label)}</a>'
        for key, label, href in (
            ("active", "Active", "/"),
            ("history", "History", "/history"),
            ("capabilities", "Capabilities", "/api/v1/capabilities"),
        )
    )
    return f"""<!doctype html>
<html lang="{get_locale()}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{BASE_CSS}</style>
</head>
<body>
  {js_translation_bundle()}
  <header class="site">
    <a class="brand" href="/" style="color: inherit;"><span class="brand-dot"></span>RunPod Controller</a>
    <nav class="site-nav">{nav_links}</nav>
  </header>
  <main>{body}</main>
</body>
</html>""".encode("utf-8")




def dashboard(report: dict[str, Any]) -> bytes:
    summary = report["summary"]
    sessions = report["sessions"]
    volumes = report["volumes"]
    pods = report["pods"]
    active_sessions, _recent_sessions = split_active_recent(sessions)
    active_volumes, _recent_volumes = split_active_recent(volumes)
    active_pods, _recent_pods = split_active_recent(pods)
    baseline = {
        "active_sessions": summary["active_sessions"],
        "active_pods": summary["active_pods"],
        "active_volumes": summary["active_volumes"],
        "sessions": summary["sessions"],
        "pods": summary["pods"],
        "volumes": summary["volumes"],
    }
    body = f"""
<section class="page-head">
  <div>
    <h2>{t("Active sessions")}</h2>
    <div class="subtle">{t("Current compute, storage, tunnel, and watchdog state.")}</div>
  </div>
  <div class="actions">
    <a class="button primary" href="/comfyui/new">{t("Create ComfyUI")}</a>
    <a class="button" href="/history">{t("History")}</a>
  </div>
</section>
<section class="metrics">
  <div class="metric"><strong data-summary-key="active_sessions">{summary['active_sessions']}</strong>{t("Active sessions")}</div>
  <div class="metric"><strong data-summary-key="active_pods">{summary['active_pods']}</strong>{t("Active Pods")}</div>
  <div class="metric"><strong data-summary-key="active_volumes">{summary['active_volumes']}</strong>{t("Active volumes")}</div>
  <div class="metric"><strong data-summary-key="effective_cost_usd" data-format="money">${summary['effective_cost_usd']:.6f}</strong>{t("Spend")}</div>
  <div class="metric"><strong data-summary-key="effective_compute_cost_usd" data-format="money">${summary['effective_compute_cost_usd']:.6f}</strong>{t("Compute spend")}</div>
  <div class="metric"><strong data-summary-key="effective_storage_cost_usd" data-format="money">${summary['effective_storage_cost_usd']:.6f}</strong>{t("Storage spend")}</div>
</section>
{session_cards(active_sessions)}
{live_refresh_script(baseline)}
"""
    return page("RunPod Controller", body, nav="active")


def session_cards(sessions: list[dict[str, Any]]) -> str:
    if not sessions:
        return (
            f'<section class="card empty-state"><strong>{t("No active sessions")}</strong>'
            f'{t("Launch a workflow-first ComfyUI session to get started.")}'
            f'<p class="actions" style="justify-content:center"><a class="button primary" href="/comfyui/new">{t("Create ComfyUI")}</a></p></section>'
        )
    cards = []
    for session in sessions:
        session_id = html.escape(str(session.get("id") or ""))
        ui_url = str(session.get("local_ui_url") or "")
        open_button = f'<a class="button primary" href="{html.escape(ui_url)}" target="_blank">{t("Open ComfyUI")}</a>' if ui_url else ""
        cost = f"${float(session.get('effective_cost_usd') or 0):.6f}"
        cards.append(
            f"""
<article class="card session-card" data-row-id="{session_id}">
  <div class="session-card-head">
    <a href="/sessions/{session_id}"><code>{session_id}</code></a>
    <span data-column="state" data-pill="1">{state_pill(session.get('state'))}</span>
  </div>
  <div class="session-card-grid">
    <div><label>{t("Phase")}</label><span data-column="phase">{html.escape(str(session.get('phase') or ''))}</span></div>
    <div><label>{t("Spend")}</label><span data-column="effective_cost_usd">{cost}</span></div>
    <div><label>{t("Elapsed")}</label><span data-column="elapsed">{html.escape(str(session.get('elapsed') or ''))}</span></div>
    <div><label>{t("Pod runtime")}</label><span data-column="aggregate_pod_runtime">{html.escape(str(session.get('aggregate_pod_runtime') or ''))}</span></div>
    <div><label>{t("Idle deadline")}</label><span data-column="idle_deadline" data-time="1">{html.escape(fmt_timestamp(session.get('idle_deadline')) or '—')}</span></div>
    <div><label>{t("Cost cap")}</label><span data-column="max_total_usd">{f"${float(session.get('max_total_usd') or 0):.2f}" if session.get('max_total_usd') else '—'}</span></div>
    <div><label>{t("Candidates")}</label><span data-column="candidate_count">{html.escape(str(session.get('candidate_count') or 0))}</span></div>
    <div><label>{t("Tunnel")}</label><span data-column="tunnel_status">{html.escape(str(session.get('tunnel_status') or ''))}</span></div>
  </div>
  <div class="session-card-actions">
    {open_button}
    <a class="button" href="/sessions/{session_id}">{t("Overview")}</a>
    <a class="button" href="/sessions/{session_id}/workflow">{t("Workflow")}</a>
  </div>
</article>
"""
        )
    return '<section class="session-cards">' + "".join(cards) + "</section>"


def history_page(report: dict[str, Any]) -> bytes:
    sessions = report["sessions"]
    _active_sessions, recent_sessions = split_active_recent(sessions)
    body = f"""
<section class="page-head">
  <div>
    <h2>{t("History")}</h2>
    <div class="subtle">{t("Recent terminal sessions. Full pagination and filters can be added later.")}</div>
  </div>
  <div class="actions">
    <a class="button" href="/">{t("Active")}</a>
  </div>
</section>
{table(['id','state','phase','product','mode','elapsed','aggregate_pod_runtime','effective_cost_usd','cost_source','gpu_attempts','tunnel_status','created_at','updated_at'], recent_sessions, link_session=True)}
"""
    return page("History", body, nav="history")


def comfyui_new_page() -> bytes:
    body = f"""
<section class="page-head">
  <div>
    <h2>{t("New ComfyUI Session")}</h2>
    <div class="subtle">{t("Workflow first: upload JSON, resolve nodes, size models, probe dependencies, then launch.")}</div>
  </div>
  <div class="actions">
    <a class="button" href="/">{t("Close")}</a>
  </div>
</section>
<div id="wizard-banner" class="wizard-banner info">
  <span class="banner-icon" id="wizard-banner-icon" aria-hidden="true"></span>
  <span id="wizard-banner-text">{t("Upload a ComfyUI workflow JSON to start, or pick one from the library.")}</span>
  <span class="banner-step" id="wizard-banner-step">{t("Step")} 1 / 5 · {t("Workflow")}</span>
</div>
<section class="wizard">
  <aside class="stepper">
    <button type="button" class="active" data-step-button="workflow"><span class="step-mark">1</span>{t("Workflow")}</button>
    <button type="button" data-step-button="nodes"><span class="step-mark">2</span>{t("Nodes")}</button>
    <button type="button" data-step-button="models"><span class="step-mark">3</span>{t("Models")}</button>
    <button type="button" data-step-button="probe"><span class="step-mark">4</span>{t("Probe / Lock")}</button>
    <button type="button" data-step-button="launch"><span class="step-mark">5</span>{t("Launch")}</button>
  </aside>
  <div>
    <section class="wizard-panel active" data-step-panel="workflow">
      <h2>{t("Workflow")}</h2>
      <div class="form-grid">
        <div class="form-row wide">
          <label for="workflow-select">{t("Workflow library")}</label>
          <div class="workflow-library-stack">
            <select id="workflow-select"><option value="">{t("No workflow")}</option></select>
            <div class="workflow-name-row">
              <input id="workflow-name" placeholder="{t('Workflow name')}">
              <button type="button" id="workflow-rename">{t("Rename")}</button>
            </div>
            <div class="row-actions workflow-library-actions">
              <button type="button" id="workflow-use" class="primary">{t("Use selected workflow")}</button>
              <button type="button" id="workflow-reanalyze">{t("Re-analyze")}</button>
              <button type="button" id="workflow-export">{t("Export package")}</button>
              <button type="button" id="copy-missing-nodes">{t("Copy missing-node summary")}</button>
              <button type="button" id="workflow-delete" class="danger">{t("Delete")}</button>
            </div>
          </div>
        </div>
        <div class="form-row wide">
          <label for="workflow-file">{t("Upload workflow JSON or shared package (.zip)")}</label>
          <div class="workflow-upload-row">
            <input id="workflow-file" type="file" accept=".json,.zip,application/json,application/zip">
            <button type="button" id="workflow-upload" class="primary">{t("Upload workflow")}</button>
          </div>
        </div>
      </div>
      <div id="workflow-summary"></div>
      <div class="wizard-nav" data-step-nav="workflow"></div>
    </section>

    <section class="wizard-panel" data-step-panel="nodes">
      <h2>{t("Nodes")}</h2>
      <div class="subtle">{t("Registry matches are suggestions until accepted and locked. Unresolved executable nodes block probe and launch.")}</div>
      <div id="node-summary"></div>
      <h2>{t("Needs decision")}</h2>
      <div class="row-actions">
        <button type="button" id="nodes-resolve-all">{t("Resolve all with URLs")}</button>
      </div>
      <div class="subtle" id="nodes-resolve-all-hint">{t("Locks every unresolved node row that already has a Git repo URL. Rows without URLs stay here for manual input.")}</div>
      <div id="nodes-progress" class="operation-progress" aria-describedby="nodes-progress-label">
        <div class="progress-head">
          <strong id="nodes-progress-label">{t("Resolving nodes")}</strong>
          <span id="nodes-progress-count" class="subtle">0 / 0</span>
        </div>
        <progress id="nodes-progress-bar" value="0" max="1">0%</progress>
        <div id="nodes-progress-detail" class="subtle">{t("Preparing...")}</div>
      </div>
      <div id="unresolved-node-list"></div>
      <h2>{t("Resolved custom nodes")}</h2>
      <div id="resolved-node-list"></div>
      <h2>{t("Built-in / assumed core")}</h2>
      <div id="builtin-node-list"></div>
      <div class="wizard-nav" data-step-nav="nodes"></div>
    </section>

    <section class="wizard-panel" data-step-panel="models">
      <h2>{t("Models")}</h2>
      <div class="subtle">{t("Workflow-extracted model requirements come first. Extra models can be added below.")}</div>
      <div class="row-actions">
        <button type="button" id="assets-save-all">{t("Save all model rows")}</button>
      </div>
      <div id="model-list"></div>
      <h2>{t("Add extra model")}</h2>
      <div class="inline-grid">
        <input id="asset-url" placeholder="https://huggingface.co/.../model.safetensors">
        {model_folder_select("asset-folder", "asset_folder")}
        <div class="row-actions">
          <button type="button" id="asset-add">{t("Add")}</button>
          <button type="button" id="asset-refresh">{t("Refresh metadata")}</button>
        </div>
      </div>
      <div class="wizard-nav" data-step-nav="models"></div>
    </section>

    <section class="wizard-panel" data-step-panel="probe">
      <h2>{t("Probe / Lock")}</h2>
      <div class="subtle">{t("The CPU dependency probe is optional here: if it has not run when you launch, the controller runs it automatically before creating paid resources.")}</div>
      <div id="lock-summary"></div>
      <div class="row-actions">
        <button type="button" id="workflow-probe" class="primary">{t("Run dependency probe")}</button>
      </div>
      <div id="probe-result"></div>
      <div class="wizard-nav" data-step-nav="probe"></div>
    </section>

    <section class="wizard-panel" data-step-panel="launch">
      <h2>{t("Launch")}</h2>
      <div class="form-grid">
        <div class="form-row">
          <label for="new-session-volume">{t("Volume GB")} <span class="help" tabindex="0" data-tip="{t('Auto-sized from model metadata: max(10GB, ceil(total bytes × 1.20) + 5GB). The computed value is a minimum — you can raise it but not go below. It recalculates whenever model URLs gain metadata.')}">?</span></label>
          <input id="new-session-volume" name="network_volume_size_gb" type="number" min="10" step="1" value="10">
          <div id="volume-estimate" class="subtle"></div>
        </div>
        <div class="form-row">
          <label for="new-session-lease">{t("Lease minutes")}</label>
          <input id="new-session-lease" name="lease_minutes" type="number" min="5" step="5" value="120">
        </div>
        <div class="form-row">
          <label for="new-session-max-rate">{t("Max $/hr")}</label>
          <input id="new-session-max-rate" name="max_gpu_usd_per_hr" type="number" min="0" step="0.01" value="1.25">
        </div>
        <div class="form-row">
          <label for="new-session-max-total">{t("Max total $")}</label>
          <input id="new-session-max-total" name="max_total_usd" type="number" min="0" step="0.01" value="5.00">
        </div>
        <div class="form-row">
          <label for="new-session-min-vram">{t("Min VRAM GB")}</label>
          <input id="new-session-min-vram" name="min_vram_gb" type="number" min="1" step="1" value="24">
        </div>
        <div class="form-row">
          <label for="new-session-gpu-vendor">{t("GPU vendor")}</label>
          <select id="new-session-gpu-vendor" name="gpu_vendor">
            <option value="NVIDIA" selected>NVIDIA</option>
          </select>
        </div>
      </div>
      <p class="subtle">{t("Location and GPU candidates are planned automatically during workflow startup.")}</p>
      <div class="row-actions">
        <button type="button" id="new-session-dryrun" disabled>{t("Dry run")}</button>
        <button type="button" id="create-comfyui" class="primary" disabled>{t("Create ComfyUI")}</button>
      </div>
      <p id="launch-readiness" class="subtle"></p>
      <div id="dryrun-result"></div>
      <div class="wizard-nav" data-step-nav="launch"></div>
    </section>
  </div>
</section>
{comfyui_new_script()}
"""
    return page("New ComfyUI", body)


def comfyui_new_script() -> str:
    return r"""
<script>
(() => {
  let workflows = [];
  let current = null;
  let nodeBusy = false;
  const gib = 1024 * 1024 * 1024;
  const T = window.__T || (s => s);
  const STEPS = ["workflow", "nodes", "models", "probe", "launch"];
  const STEP_LABELS = {workflow: T("Workflow"), nodes: T("Nodes"), models: T("Models"), probe: T("Probe / Lock"), launch: T("Launch")};
  let activeStep = "workflow";

  const esc = text => String(text ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const money = value => `$${Number(value || 0).toFixed(6)}`;
  const sizeLabel = value => {
    if (value === null || value === undefined || value === "") return "unknown";
    const size = Number(value || 0);
    if (!Number.isFinite(size)) return "unknown";
    if (size >= gib) return `${(size / gib).toFixed(2)} GB`;
    return `${Math.ceil(size / (1024 * 1024))} MB`;
  };
  const normalizedUrl = value => {
    try {
      const parsed = new URL(String(value || "").trim());
      parsed.hash = "";
      parsed.protocol = parsed.protocol.toLowerCase();
      parsed.hostname = parsed.hostname.toLowerCase();
      for (const key of [...parsed.searchParams.keys()]) {
        const lower = key.toLowerCase();
        if (lower.includes("token") || lower.includes("key") || lower.includes("secret") || lower.includes("authorization") || lower.includes("api_key")) {
          parsed.searchParams.delete(key);
        }
      }
      parsed.searchParams.sort();
      return parsed.toString();
    } catch (_err) {
      return String(value || "").trim();
    }
  };
  function packageFromRepoUrl(url) {
    const cleaned = String(url || "").trim().replace(/\/+$/, "");
    let name = cleaned.split("/").pop() || "custom-node";
    if (name.endsWith(".git")) name = name.slice(0, -4);
    return name || "custom-node";
  }
  function setStatus(text, kind = "info") {
    const banner = document.getElementById("wizard-banner");
    const icon = document.getElementById("wizard-banner-icon");
    const target = document.getElementById("wizard-banner-text");
    if (!banner || !target) return;
    banner.className = `wizard-banner ${kind}`;
    target.textContent = T(text);
    if (icon) icon.textContent = kind === "success" ? "✓" : kind === "error" ? "✕" : kind === "busy" ? "" : "i";
  }
  function updateBannerStep() {
    const chip = document.getElementById("wizard-banner-step");
    if (chip) chip.textContent = `${T("Step")} ${STEPS.indexOf(activeStep) + 1} / ${STEPS.length} · ${STEP_LABELS[activeStep]}`;
  }
  function stepStates() {
    const unresolved = current?.analysis?.unresolved_custom_nodes || [];
    const analysisBlocked = !current || (current.analysis && current.analysis.ok === false) || unresolved.length > 0;
    const incompleteAssets = current ? allAssets().filter(asset => !asset.url || !asset.size_bytes) : [];
    const probeState = current?.last_probe_result?.state || "";
    return {
      workflow: {done: !!current, nextReason: current ? "" : T("Upload or select a workflow first.")},
      nodes: {
        done: !!current && !analysisBlocked,
        nextReason: !current ? T("Upload or select a workflow first.") : (analysisBlocked ? `${unresolved.length || T("Some")} ${T("node decision(s) still needed.")}` : ""),
      },
      models: {
        done: !!current && !analysisBlocked && incompleteAssets.length === 0,
        nextReason: !current ? T("Upload or select a workflow first.") : (incompleteAssets.length ? `${incompleteAssets.length} ${T("model asset(s) still need URL or metadata.")}` : ""),
      },
      probe: {
        done: probeState === "passed",
        nextReason: "",
        hint: probeState === "passed" ? "" : T("Optional: launch runs the probe automatically if it has not passed yet."),
      },
      launch: {done: false, nextReason: ""},
    };
  }
  function renderStepper() {
    const states = stepStates();
    for (const button of document.querySelectorAll("[data-step-button]")) {
      const step = button.dataset.stepButton;
      const mark = button.querySelector(".step-mark");
      const done = !!states[step]?.done;
      button.classList.toggle("done", done);
      if (mark) mark.textContent = done ? "✓" : String(STEPS.indexOf(step) + 1);
    }
  }
  function renderWizardNav() {
    const states = stepStates();
    for (const nav of document.querySelectorAll("[data-step-nav]")) {
      const step = nav.dataset.stepNav;
      const index = STEPS.indexOf(step);
      const previous = index > 0 ? STEPS[index - 1] : null;
      const next = index < STEPS.length - 1 ? STEPS[index + 1] : null;
      const blocked = states[step]?.nextReason || "";
      const hint = states[step]?.hint || "";
      const parts = [];
      if (previous) parts.push(`<button type="button" data-nav-back="${previous}">← ${esc(STEP_LABELS[previous])}</button>`);
      parts.push(`<span class="nav-reason">${esc(blocked || hint)}</span>`);
      if (next) parts.push(`<button type="button" class="primary" data-nav-next="${next}" ${blocked ? "disabled" : ""}>${esc(T("Continue:"))} ${esc(STEP_LABELS[next])} →</button>`);
      nav.innerHTML = parts.join("");
      for (const button of nav.querySelectorAll("[data-nav-back]")) {
        button.addEventListener("click", () => goStep(button.dataset.navBack));
      }
      for (const button of nav.querySelectorAll("[data-nav-next]")) {
        button.addEventListener("click", () => goStep(button.dataset.navNext));
      }
    }
  }
  function setNodeControlsDisabled(disabled) {
    const panel = document.querySelector('[data-step-panel="nodes"]');
    if (panel) {
      panel.setAttribute("aria-busy", disabled ? "true" : "false");
      for (const control of panel.querySelectorAll("button, input, select, textarea")) {
        control.disabled = disabled;
      }
    }
    for (const button of document.querySelectorAll("[data-step-button]")) {
      button.disabled = disabled;
    }
  }
  function setNodeBusy(busy) {
    nodeBusy = busy;
    setNodeControlsDisabled(busy);
  }
  function updateNodeProgress({done, total, label, detail}) {
    const wrapper = document.getElementById("nodes-progress");
    const bar = document.getElementById("nodes-progress-bar");
    const count = document.getElementById("nodes-progress-count");
    const title = document.getElementById("nodes-progress-label");
    const detailTarget = document.getElementById("nodes-progress-detail");
    const safeTotal = Math.max(1, Number(total || 1));
    const safeDone = Math.min(safeTotal, Math.max(0, Number(done || 0)));
    wrapper.classList.add("active");
    bar.max = safeTotal;
    bar.value = safeDone;
    bar.textContent = `${Math.round((safeDone / safeTotal) * 100)}%`;
    count.textContent = `${safeDone} / ${safeTotal}`;
    title.textContent = label || "Resolving nodes";
    detailTarget.textContent = detail || "";
  }
  function goStep(name) {
    if (nodeBusy) return;
    activeStep = name;
    for (const button of document.querySelectorAll("[data-step-button]")) {
      button.classList.toggle("active", button.dataset.stepButton === name);
    }
    for (const panel of document.querySelectorAll("[data-step-panel]")) {
      panel.classList.toggle("active", panel.dataset.stepPanel === name);
    }
    updateBannerStep();
    renderStepper();
    renderWizardNav();
    window.scrollTo({top: 0, behavior: "smooth"});
  }
  for (const button of document.querySelectorAll("[data-step-button]")) {
    button.addEventListener("click", () => goStep(button.dataset.stepButton));
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
      cache: "no-store"
    });
    const result = await response.json();
    if (!response.ok || result.error) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }
  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
  function resourceRequestSessionId(request) {
    return request.session_id || (request.result_json && request.result_json.session_id) || (request.result && request.result.session_id) || "";
  }
  async function waitForCreateSession(requestId) {
    let lastState = "accepted";
    for (let attempt = 0; attempt < 180; attempt += 1) {
      const request = await api(`/api/v1/resource-requests/${encodeURIComponent(requestId)}`);
      lastState = request.state || lastState;
      const sessionId = resourceRequestSessionId(request);
      if (sessionId) return sessionId;
      if (lastState === "failed") throw new Error(request.error || T("Create request failed"));
      setStatus(`${T("Creating ComfyUI...")} ${lastState}`, "busy");
      await sleep(Math.max(1000, Number(request.poll_after_seconds || 2) * 1000));
    }
    throw new Error(`Create request ${requestId} is still ${lastState}`);
  }
  async function loadWorkflows(selectedId = null) {
    const result = await api("/api/v1/comfyui/workflows?product=comfyui");
    workflows = result.workflows || [];
    const select = document.getElementById("workflow-select");
    select.innerHTML = `<option value="">${esc(T("No workflow"))}</option>` + workflows.map(workflow => {
      const badge = workflow.verification_state === "live_verified" ? ` · ${T("verified")}` : ` · ${workflow.status || "new"}`;
      return `<option value="${esc(workflow.id)}">${esc((workflow.name || workflow.hash_prefix || workflow.id) + badge)}</option>`;
    }).join("");
    if (selectedId) select.value = selectedId;
  }
  async function selectWorkflow(id) {
    if (!id) {
      current = null;
      renderAll();
      return;
    }
    current = await api(`/api/v1/comfyui/workflows/${encodeURIComponent(id)}`);
    document.getElementById("workflow-select").value = current.id;
    document.getElementById("workflow-name").value = current.name || "";
    renderAll();
  }
  function renderAll() {
    renderWorkflowSummary();
    renderNodes();
    renderModels();
    renderLockSummary();
    recomputeVolume();
    updateActionState();
    renderStepper();
    renderWizardNav();
    updateBannerStep();
  }
  function nextStepForCurrentWorkflow() {
    if (!current) return "workflow";
    const unresolved = current.analysis?.unresolved_custom_nodes || [];
    const analysisBlocked = (current.analysis && current.analysis.ok === false) || unresolved.length > 0;
    if (analysisBlocked) return "nodes";
    const incompleteAssets = allAssets().filter(asset => !asset.url || !asset.size_bytes);
    if (incompleteAssets.length) return "models";
    return "launch";
  }
  function useCurrentWorkflow() {
    if (!current) {
      setStatus("Select a workflow from the library first.", "error");
      goStep("workflow");
      return;
    }
    const step = nextStepForCurrentWorkflow();
    goStep(step);
    if (step === "launch") {
      setStatus("Workflow ready. Review budget, dry run, then create.", "success");
    } else if (step === "models") {
      setStatus("Workflow selected. Fill model URLs and sizes next.");
    } else {
      setStatus("Workflow selected. Resolve node decisions next.");
    }
  }
  function renderWorkflowSummary() {
    const target = document.getElementById("workflow-summary");
    if (!current) {
      target.innerHTML = `<p class="subtle">${esc(T("No workflow selected."))}</p>`;
      return;
    }
    const analysis = current.analysis || {};
    const warnings = analysis.warnings || [];
    target.innerHTML = `
      <section class="metrics">
        <div class="metric"><strong>${esc(current.hash_prefix || "")}</strong>${esc(T("Hash"))}</div>
        <div class="metric"><strong>${Number(analysis.node_count || 0)}</strong>${esc(T("Nodes"))}</div>
        <div class="metric"><strong>${Number(analysis.core_node_count || 0) + Number(analysis.assumed_builtin_count || 0)}</strong>${esc(T("Built-in / assumed"))}</div>
        <div class="metric"><strong>${Number((analysis.unresolved_custom_nodes || []).length)}</strong>${esc(T("Needs decision"))}</div>
        <div class="metric"><strong>${esc(current.verification_state || "unverified")}</strong>${esc(T("Verification"))}</div>
      </section>
      <p>
        <span class="badge">${esc(current.status || "")}</span>
        <span class="badge">${esc(current.workflow_hash || "")}</span>
        ${current.original_filename ? `<span class="badge">${esc(current.original_filename)}</span>` : ""}
      </p>
      ${warnings.length ? `<h2>${esc(T("Warnings"))}</h2><div class="table-wrap"><table><tbody>${warnings.map(w => `<tr><td class="wrap">${esc(w)}</td></tr>`).join("")}</tbody></table></div>` : `<p class="subtle">${esc(T("No analyzer warnings."))}</p>`}
    `;
  }
  function renderNodes() {
    const analysis = current ? (current.analysis || {}) : {};
    const unresolved = analysis.unresolved_custom_nodes || [];
    const resolved = analysis.resolved_custom_nodes || [];
    const core = [...(analysis.core_nodes || []), ...(analysis.assumed_builtin_nodes || [])];
    document.getElementById("node-summary").innerHTML = current
      ? `<p><span class="badge">${Number(resolved.length)} ${esc(T("resolved packages"))}</span> <span class="badge">${Number(unresolved.length)} ${esc(T("needs decision"))}</span> <span class="badge">${Number(core.length)} ${esc(T("built-in / assumed"))}</span></p>`
      : `<p class="subtle">${esc(T("Upload a workflow first."))}</p>`;
    document.getElementById("resolved-node-list").innerHTML = resolved.length ? table(["package","repo_url","ref","source","node_types"], resolved) : `<p class="subtle">${esc(T("No resolved custom nodes."))}</p>`;
    document.getElementById("builtin-node-list").innerHTML = core.length ? table(["node"], core.map(node => ({node}))) : `<p class="subtle">${esc(T("No built-in nodes recorded."))}</p>`;
    document.getElementById("unresolved-node-list").innerHTML = unresolved.length ? unresolved.map((node, index) => unresolvedNodeCard(node, index)).join("") : `<p class="subtle">${esc(T("No unresolved custom nodes."))}</p>`;
    for (const button of document.querySelectorAll("[data-use-registry]")) {
      button.addEventListener("click", () => resolveNode({decision: "use_registry", class_type: button.dataset.useRegistry}));
    }
    for (const button of document.querySelectorAll("[data-treat-builtin]")) {
      button.addEventListener("click", () => resolveNode({decision: "treat_builtin", class_type: button.dataset.treatBuiltin}));
    }
    for (const button of document.querySelectorAll("[data-install-node]")) {
      button.addEventListener("click", () => {
        const index = button.dataset.installNode;
        const payload = nodeInstallPayload(button);
        if (!payload) return setStatus("Git repo URL is required for this node.", "error");
        resolveNode(payload);
      });
    }
    setNodeControlsDisabled(nodeBusy);
  }
  function nodeInstallPayload(button) {
    const index = button.dataset.installNode;
    const repoInput = document.getElementById(`node-repo-${index}`);
    const refInput = document.getElementById(`node-ref-${index}`);
    if (!repoInput || !refInput) return null;
    const repoUrl = repoInput.value.trim();
    if (!repoUrl) return null;
    return {
      decision: "install_git_repo",
      class_type: button.dataset.classType,
      package: packageFromRepoUrl(repoUrl),
      repo_url: repoUrl,
      requested_ref: refInput.value.trim()
    };
  }
  function unresolvedNodeCard(node, index) {
    const classType = node.class_type || "";
    const suggestion = node.suggestion || {};
    const sources = (node.sources || []).map(source => `${source.source || ""}:${source.id || ""}${source.title ? " " + source.title : ""}`).join(", ");
    return `
      <article class="candidate">
        <strong>${esc(classType)}</strong>
        <p class="subtle">${esc(node.reason || "")}${sources ? " · " + esc(sources) : ""}</p>
        ${suggestion.repo_url ? `<p class="subtle">${esc(T("Registry suggestion:"))} ${esc(suggestion.package || suggestion.display_name || "")} · ${esc(suggestion.repo_url)}</p>` : `<p class="subtle">${esc(T("No accepted Registry suggestion yet."))}</p>`}
        <div class="row-actions">
          ${suggestion.repo_url ? `<button type="button" data-use-registry="${esc(classType)}">${esc(T("Use Registry result"))}</button>` : ""}
          <button type="button" data-treat-builtin="${esc(classType)}">${esc(T("Treat as built-in"))}</button>
        </div>
        <div class="node-resolution-grid" style="margin-top:8px">
          <input id="node-repo-${index}" placeholder="${esc(T("Git repo URL"))}" value="${esc(suggestion.repo_url || "")}">
          <input id="node-ref-${index}" placeholder="${esc(T("tag / branch / commit optional"))}">
        </div>
        <p><button type="button" data-install-node="${index}" data-class-type="${esc(classType)}">${esc(T("Resolve and lock Git repo"))}</button></p>
      </article>`;
  }
  async function resolveNode(payload) {
    if (!current) return;
    if (nodeBusy) return;
    setStatus("Resolving node lock...", "busy");
    setNodeBusy(true);
    updateNodeProgress({done: 0, total: 1, label: "Resolving node", detail: payload.class_type || "Node lock"});
    try {
      current = await saveNodeLock(payload);
      updateNodeProgress({done: 1, total: 1, label: "Node resolved", detail: payload.class_type || "Node lock saved"});
      await loadWorkflows(current.id);
      renderAll();
      setStatus("Node lock saved", "success");
    } catch (err) {
      updateNodeProgress({done: 1, total: 1, label: "Node resolve failed", detail: String(err.message || err)});
      setStatus(String(err.message || err), "error");
    } finally {
      setNodeBusy(false);
    }
  }
  async function saveNodeLock(payload) {
    return api(`/api/v1/comfyui/workflows/${encodeURIComponent(current.id)}/nodes/resolve`, {
      method: "POST",
      body: JSON.stringify(payload)
    });
  }
  async function resolveAllNodesWithUrls() {
    if (!current) return setStatus("Upload or select a workflow first.", "error");
    if (nodeBusy) return;
    const payloads = [];
    let missing = 0;
    for (const button of document.querySelectorAll("[data-install-node]")) {
      const payload = nodeInstallPayload(button);
      if (payload) payloads.push(payload);
      else missing += 1;
    }
    if (!payloads.length) return setStatus(`No Git repo URLs to resolve. ${missing} node(s) still need URL.`, "error");
    setStatus(`Resolving ${payloads.length} node lock(s)...`, "busy");
    setNodeBusy(true);
    updateNodeProgress({done: 0, total: payloads.length, label: "Resolving node locks", detail: `${payloads.length} row(s) queued.`});
    const errors = [];
    let done = 0;
    for (const payload of payloads) {
      updateNodeProgress({done, total: payloads.length, label: "Resolving node locks", detail: `Locking ${payload.class_type || payload.repo_url}`});
      try {
        current = await saveNodeLock(payload);
      } catch (err) {
        errors.push(`${payload.class_type}: ${err.message || err}`);
      }
      done += 1;
      updateNodeProgress({done, total: payloads.length, label: "Resolving node locks", detail: `${done} of ${payloads.length} complete.`});
    }
    try {
      await selectWorkflow(current.id);
      const remaining = current?.analysis?.unresolved_custom_nodes?.length || 0;
      if (errors.length) {
        const message = `Resolved ${payloads.length - errors.length}; ${errors.length} failed; ${remaining} still need decision.`;
        updateNodeProgress({done, total: payloads.length, label: "Resolve all finished with errors", detail: message});
        setStatus(message);
      } else {
        const message = `Resolved ${payloads.length}; ${remaining} still need decision${missing ? `, including ${missing} without URL` : ""}.`;
        updateNodeProgress({done, total: payloads.length, label: "Resolve all complete", detail: message});
        setStatus(message);
      }
    } finally {
      setNodeBusy(false);
    }
  }
  function renderModels() {
    const target = document.getElementById("model-list");
    if (!current) {
      target.innerHTML = `<p class="subtle">${esc(T("Upload a workflow first."))}</p>`;
      return;
    }
    const extracted = current.extracted_assets || [];
    const extra = current.extra_assets || [];
    target.innerHTML = `
      <h2>${esc(T("Workflow-extracted models"))}</h2>
      ${assetTable("extracted", extracted)}
      <h2>${esc(T("Extra models"))}</h2>
      ${assetTable("extra", extra)}
    `;
    bindAssetButtons();
  }
  function assetTable(kind, rows) {
    const visibleRows = rows
      .map((asset, index) => ({asset, index}))
      .filter(row => row.asset.status !== "removed" && row.asset.status !== "replaced");
    if (!visibleRows.length) return `<p class="subtle">${esc(T("No active rows."))}</p>`;
    return `<div class="table-wrap"><table><thead><tr><th>${esc(T("filename"))}</th><th>${esc(T("folder"))}</th><th>${esc(T("size"))}</th><th>${esc(T("status"))}</th><th></th></tr></thead><tbody>` +
      visibleRows.map(({asset, index}) => `
        <tr>
          <td><code>${esc(asset.filename || "")}</code><div class="subtle">${esc(asset.source_node_type || asset.source || "")}</div></td>
          <td><input id="${kind}-folder-${index}" value="${esc(asset.model_folder || "checkpoints")}"></td>
          <td><span id="${kind}-size-${index}">${esc(assetSizeText(asset))}</span></td>
          <td>${esc(T(assetStatus(asset)))}</td>
          <td>
            <div class="row-actions">
              <button type="button" data-save-asset="${kind}:${index}">${esc(T("Save"))}</button>
              <button type="button" class="danger" data-remove-model="${kind}:${index}">${esc(T("Remove"))}</button>
            </div>
          </td>
        </tr>
        <tr class="asset-url-row">
          <td colspan="5"><input class="asset-url-input" id="${kind}-url-${index}" value="${esc(asset.url || "")}" placeholder="${esc(T("model URL"))}"></td>
        </tr>`).join("") + "</tbody></table>";
  }
  function bindAssetButtons() {
    for (const button of document.querySelectorAll("[data-save-asset]")) {
      button.addEventListener("click", () => saveAssetRow(button.dataset.saveAsset));
    }
    for (const button of document.querySelectorAll("[data-remove-model]")) {
      button.addEventListener("click", () => removeAssetRow(button.dataset.removeModel));
    }
  }
  function rowParts(token) {
    const [kind, rawIndex] = token.split(":");
    return {kind, index: Number(rawIndex), list: kind === "extra" ? [...(current.extra_assets || [])] : [...(current.extracted_assets || [])]};
  }
  function assetStatus(asset) {
    if (asset.status === "removed" || asset.status === "replaced") return asset.status;
    if (asset.status === "metadata_failed") return asset.status;
    if (!asset.url) return "needs_url";
    if (asset.size_bytes) return "ready";
    if (asset.size_unknown) return "metadata_unknown";
    return "needs_metadata";
  }
  function assetSizeText(asset) {
    if (asset.size_bytes) return sizeLabel(asset.size_bytes);
    if (asset.url) return asset.size_unknown ? T("metadata unknown") : T("metadata needed");
    return T("pending URL");
  }
  function readAssetRow(kind, index, existing) {
    const row = {...existing};
    const previousUrl = normalizedUrl(row.url || "");
    row.model_folder = document.getElementById(`${kind}-folder-${index}`).value.trim() || "checkpoints";
    row.url = document.getElementById(`${kind}-url-${index}`).value.trim();
    if (normalizedUrl(row.url || "") !== previousUrl) {
      row.size_bytes = null;
      row.size_unknown = false;
      row.provider = "";
      row.target = "";
      row.cache_hit = false;
    }
    row.status = assetStatus(row);
    return row;
  }
  function readAssetList(kind, rows) {
    return rows.map((row, index) => {
      if (!document.getElementById(`${kind}-folder-${index}`)) return row;
      return readAssetRow(kind, index, row);
    });
  }
  async function saveAssetRow(token) {
    const {kind, index, list} = rowParts(token);
    const row = readAssetRow(kind, index, list[index]);
    if (row.url && isDuplicateUrl(row.url, kind, index)) return setStatus("Already added. Remove the existing row first.", "error");
    let failed = false;
    if (row.url && !row.size_bytes) {
      setStatus("Saving row and peeking metadata...", "busy");
      try {
        await peekMetadataForAsset(row, false);
      } catch (err) {
        failed = true;
        row.status = "metadata_failed";
        row.last_error = String(err.message || err);
      }
    }
    list[index] = row;
    await updateAssetLists(kind, list);
    if (failed) setStatus(`Saved row, but metadata peek failed: ${row.last_error || "unknown error"}`, "error");
    else if (row.url && row.size_bytes) setStatus(row.cache_hit ? "Saved row from metadata cache" : "Saved row and metadata.", "success");
    else setStatus(row.url ? "Saved row. Metadata is still needed." : "Saved row.");
  }
  async function saveAllAssetRows() {
    if (!current) return setStatus("Upload or select a workflow first.", "error");
    const extracted = readAssetList("extracted", [...(current.extracted_assets || [])]);
    const extra = readAssetList("extra", [...(current.extra_assets || [])]);
    const duplicate = duplicateUrlInRows([...extracted, ...extra]);
    if (duplicate) return setStatus(`Duplicate model URL: ${duplicate}`, "error");
    const rowsToPeek = [...extracted, ...extra].filter(row => row.url && !row.size_bytes);
    if (rowsToPeek.length) setStatus(`Saving all and peeking metadata for ${rowsToPeek.length} asset(s)...`, "busy");
    let failed = 0;
    for (const row of rowsToPeek) {
      try {
        await peekMetadataForAsset(row, false);
      } catch (err) {
        failed += 1;
        row.status = "metadata_failed";
        row.last_error = String(err.message || err);
      }
    }
    current = await api(`/api/v1/comfyui/workflows/${encodeURIComponent(current.id)}`, {
      method: "PUT",
      body: JSON.stringify({extracted_assets: extracted, extra_assets: extra})
    });
    await loadWorkflows(current.id);
    renderAll();
    const pending = allAssets().filter(asset => !asset.url || !asset.size_bytes).length;
    if (failed) setStatus(`Saved all model rows. ${failed} metadata peek(s) failed; ${pending} asset(s) still need metadata.`, "error");
    else setStatus(pending ? `Saved all model rows. ${pending} asset(s) still need URL or metadata.` : "Saved all model rows and metadata.", pending ? "info" : "success");
  }
  async function peekMetadataForAsset(row, forceRefresh) {
    const result = await api("/api/v1/assets/peek", {
      method: "POST",
      body: JSON.stringify({url: row.url, model_folder: row.model_folder || "checkpoints", force_refresh: forceRefresh})
    });
    const sizeUnknown = !!result.size_unknown || !result.size_bytes;
    Object.assign(row, {
      url: result.download_url_redacted || row.url,
      provider: result.provider,
      model_folder: result.model_folder || row.model_folder || "checkpoints",
      filename: result.filename || row.filename,
      size_bytes: result.size_bytes,
      size_unknown: sizeUnknown,
      target: result.target,
      status: sizeUnknown ? "metadata_unknown" : "ready",
      cache_hit: !!result.cache_hit
    });
    return row;
  }
  async function removeAssetRow(token) {
    const {kind, index, list} = rowParts(token);
    if (kind === "extracted") {
      list[index] = {...list[index], status: "removed"};
    } else {
      list.splice(index, 1);
    }
    await updateAssetLists(kind, list);
    setStatus("Model row removed.", "success");
  }
  async function updateAssetLists(kind, list) {
    const payload = kind === "extra" ? {extra_assets: list} : {extracted_assets: list};
    current = await api(`/api/v1/comfyui/workflows/${encodeURIComponent(current.id)}`, {
      method: "PUT",
      body: JSON.stringify(payload)
    });
    await loadWorkflows(current.id);
    renderAll();
  }
  function allAssets() {
    if (!current) return [];
    return [...(current.extracted_assets || []), ...(current.extra_assets || [])].filter(asset => asset.status !== "removed" && asset.status !== "replaced");
  }
  function isDuplicateUrl(url, kind, index) {
    const key = normalizedUrl(url);
    if (!key) return false;
    return allAssets().some((asset, assetIndex) => {
      if (kind === "extracted" && asset === (current.extracted_assets || [])[index]) return false;
      if (kind === "extra" && asset === (current.extra_assets || [])[index]) return false;
      return normalizedUrl(asset.url || "") === key;
    });
  }
  function duplicateUrlInRows(rows) {
    const seen = new Set();
    for (const row of rows) {
      if (row.status === "removed" || row.status === "replaced" || !row.url) continue;
      const key = normalizedUrl(row.url || "");
      if (!key) continue;
      if (seen.has(key)) return row.url;
      seen.add(key);
    }
    return "";
  }
  async function addExtraAsset(forceRefresh = false) {
    if (!current) return setStatus("Upload or select a workflow first.", "error");
    const url = document.getElementById("asset-url").value.trim();
    if (!url) return;
    if (allAssets().some(asset => normalizedUrl(asset.url || "") === normalizedUrl(url))) return setStatus("Already added. Remove the existing row first.", "error");
    setStatus(forceRefresh ? "Refreshing metadata..." : "Peeking metadata...", "busy");
    try {
      const folder = document.getElementById("asset-folder").value || "checkpoints";
      const result = await api("/api/v1/assets/peek", {
        method: "POST",
        body: JSON.stringify({url, model_folder: folder, force_refresh: forceRefresh})
      });
      const sizeUnknown = !!result.size_unknown || !result.size_bytes;
      const savedUrl = result.download_url_redacted || url;
      if (allAssets().some(asset => normalizedUrl(asset.url || "") === normalizedUrl(savedUrl))) return setStatus("Already added. Remove the existing row first.", "error");
      const extra = [...(current.extra_assets || []), {
        url: savedUrl,
        provider: result.provider,
        model_folder: result.model_folder || folder,
        filename: result.filename,
        size_bytes: result.size_bytes,
        size_unknown: sizeUnknown,
        target: result.target,
        status: sizeUnknown ? "metadata_unknown" : "ready",
        source: result.cache_hit ? "cache" : "peek"
      }];
      current = await api(`/api/v1/comfyui/workflows/${encodeURIComponent(current.id)}`, {
        method: "PUT",
        body: JSON.stringify({extra_assets: extra})
      });
      document.getElementById("asset-url").value = "";
      await loadWorkflows(current.id);
      renderAll();
      setStatus(result.cache_hit ? "Added from cache" : "Added", "success");
    } catch (err) {
      setStatus(String(err.message || err), "error");
    }
  }
  function volumeEstimate() {
    const assets = allAssets();
    const readyAssets = assets.filter(asset => asset.url && asset.size_bytes);
    const pendingAssets = assets.filter(asset => !asset.url || !asset.size_bytes);
    const total = readyAssets.reduce((sum, asset) => sum + Number(asset.size_bytes || 0), 0);
    const volume = Math.max(10, Math.ceil((total * 1.20) / gib) + 5);
    return {assets, readyAssets, pendingAssets, total, volume};
  }
  function recomputeVolume() {
    const estimate = volumeEstimate();
    const input = document.getElementById("new-session-volume");
    if (input) {
      const currentValue = Number(input.value || 0);
      if (!currentValue || currentValue < estimate.volume) input.value = String(estimate.volume);
      input.min = String(estimate.volume);
    }
    const hint = document.getElementById("volume-estimate");
    if (hint) {
      if (!current) {
        hint.textContent = "";
      } else if (estimate.pendingAssets.length) {
        hint.textContent = `Interim ${estimate.volume}GB · ${estimate.pendingAssets.length} asset(s) awaiting metadata`;
      } else {
        hint.textContent = `Auto ${estimate.volume}GB from ${sizeLabel(estimate.total)} of models`;
      }
    }
    return estimate.volume;
  }
  function readiness() {
    if (!current) {
      return {
        canDryRun: false,
        canProbe: false,
        canCreate: false,
        reason: "Upload or select a workflow first."
      };
    }
    const unresolved = current.analysis?.unresolved_custom_nodes || [];
    const analysisBlocked = (current.analysis && current.analysis.ok === false) || unresolved.length > 0;
    const incompleteAssets = allAssets().filter(asset => !asset.url || !asset.size_bytes);
    if (analysisBlocked) {
      return {
        canDryRun: false,
        canProbe: false,
        canCreate: false,
        reason: `Resolve ${unresolved.length || "workflow"} custom node decision(s) before probing or launching.`
      };
    }
    if (incompleteAssets.length) {
      return {
        canDryRun: false,
        canProbe: true,
        canCreate: false,
        reason: `Fill URL and fetch metadata for ${incompleteAssets.length} model asset(s) before dry run or launch.`
      };
    }
    return {
      canDryRun: true,
      canProbe: true,
      canCreate: true,
      reason: "Ready for dry run or launch. If dependency probe is missing, the controller runs it before paid launch."
    };
  }
  function updateActionState() {
    const state = readiness();
    const dryRun = document.getElementById("new-session-dryrun");
    const create = document.getElementById("create-comfyui");
    const probe = document.getElementById("workflow-probe");
    const hint = document.getElementById("launch-readiness");
    if (dryRun) {
      dryRun.disabled = !state.canDryRun;
      dryRun.title = state.canDryRun ? "" : state.reason;
    }
    if (create) {
      create.disabled = !state.canCreate;
      create.title = state.canCreate ? "" : state.reason;
    }
    if (probe) {
      probe.disabled = !state.canProbe;
      probe.title = state.canProbe ? "" : state.reason;
    }
    if (hint) hint.textContent = state.reason;
  }
  function renderLockSummary() {
    const target = document.getElementById("lock-summary");
    if (!current) {
      target.innerHTML = `<p class="subtle">${esc(T("Upload a workflow first."))}</p>`;
      return;
    }
    const locks = current.node_locks || [];
    const probe = current.last_probe_result || null;
    target.innerHTML = `
      <div class="table-wrap"><table><tbody>
        <tr><th>${esc(T("workflow hash"))}</th><td><code title="${esc(current.workflow_hash || "")}">${esc(shortRef(current.workflow_hash || ""))}</code></td></tr>
        <tr><th>${esc(T("dependency fingerprint"))}</th><td><code title="${esc(current.dependency_fingerprint || "")}">${esc(shortRef(current.dependency_fingerprint || ""))}</code></td></tr>
        <tr><th>${esc(T("launch fingerprint"))}</th><td><code title="${esc(current.launch_fingerprint || "")}">${esc(shortRef(current.launch_fingerprint || ""))}</code></td></tr>
        <tr><th>${esc(T("base template"))}</th><td class="wrap"><code>${esc(JSON.stringify(current.base_template_lock || {}))}</code></td></tr>
        <tr><th>${esc(T("probe"))}</th><td>${probe ? esc(`${probe.state || ""}${probe.cached ? " " + T("cached") : ""}`) : esc(T("not run"))}</td></tr>
      </tbody></table></div>
      <h2>${esc(T("Node locks"))}</h2>
      ${locks.length ? table(["decision","package","repo_url","requested_ref","locked_ref","node_types"], locks) : `<p class="subtle">${esc(T("No custom-node locks."))}</p>`}
    `;
  }
  async function runProbe() {
    if (!current) return setStatus("Upload or select a workflow first.", "error");
    setStatus("Running dependency probe...", "busy");
    try {
      const result = await api(`/api/v1/comfyui/workflows/${encodeURIComponent(current.id)}/probe`, {method: "POST", body: "{}"});
      document.getElementById("probe-result").innerHTML = `<p class="subtle">${esc(T("Probe"))} ${esc(result.state || "")}${result.cached ? " " + esc(T("cached")) : ""}</p>`;
      await selectWorkflow(current.id);
      setStatus(result.cached ? "Probe cache hit" : "Probe complete", "success");
    } catch (err) {
      document.getElementById("probe-result").innerHTML = `<p class="subtle">${esc(T("Probe failed:"))} ${esc(err.message || err)}</p>`;
      setStatus(String(err.message || err), "error");
    }
  }
  function launchPayload() {
    if (!current) return {error: "Upload or select a workflow first."};
    const incomplete = allAssets().filter(asset => !asset.url || !asset.size_bytes);
    if ((current.analysis && current.analysis.ok === false) || (current.analysis?.unresolved_custom_nodes || []).length) return {error: "Resolve workflow custom nodes before creating."};
    if (incomplete.length) return {error: "Fill model URLs and fetch metadata before creating."};
    return {
      payload: {
        product: "comfyui",
        mode: "interactive",
        workflow_id: current.id,
        network_volume_size_gb: Number(document.getElementById("new-session-volume").value || 10),
        lease_minutes: Number(document.getElementById("new-session-lease").value || 120),
        max_gpu_usd_per_hr: Number(document.getElementById("new-session-max-rate").value || 1.25),
        max_total_usd: Number(document.getElementById("new-session-max-total").value || 5),
        min_vram_gb: Number(document.getElementById("new-session-min-vram").value || 24),
        gpu_vendor: document.getElementById("new-session-gpu-vendor").value || "NVIDIA"
      }
    };
  }
  function renderDryRun(result) {
    const target = document.getElementById("dryrun-result");
    if (!result.ok) {
      target.innerHTML = `<p class="subtle">${esc(T("Dry run failed:"))} ${esc(result.error || "unknown")}</p>`;
      return;
    }
    const rows = (result.candidate_groups || []).map(group => `
      <tr><td><code>${esc(group.data_center_id)}</code></td><td>${esc((group.gpu_types || []).map(gpu => {
        const rate = gpu.quoted_cost_usd_per_hr ?? gpu.estimated_cost_usd_per_hr;
        return `${gpu.gpu_type_id || ""}${rate ? " " + money(rate) + "/hr" : ""}${gpu.stock_status ? " " + gpu.stock_status : ""}`;
      }).join(", "))}</td></tr>`).join("");
    target.innerHTML = `
      <h2>${esc(T("Dry run result"))}</h2>
      <section class="metrics">
        <div class="metric"><strong>${Number(result.data_center_count || 0)}</strong>${esc(T("Datacenters"))}</div>
        <div class="metric"><strong>${Number(result.candidate_count || 0)}</strong>${esc(T("GPU rows"))}</div>
        <div class="metric"><strong>${Number(result.confirmed_candidate_count || 0)}</strong>${esc(T("Stock rows"))}</div>
        <div class="metric"><strong>${Number(result.volume_size_gb || 0)}</strong>${esc(T("Volume GB"))}</div>
      </section>
      <div class="table-wrap"><table><thead><tr><th>${esc(T("datacenter"))}</th><th>${esc(T("eligible GPU rows"))}</th></tr></thead><tbody>${rows || `<tr><td colspan="2" class="subtle">${esc(T("No matching GPU rows."))}</td></tr>`}</tbody></table></div>
      <p class="subtle">${esc(T("No resources created. Empty datacenters are hidden."))}</p>`;
  }
  function shortRef(value) {
    const text = String(value || "");
    return /^[0-9a-f]{12,64}$/i.test(text) ? text.slice(0, 10) : text;
  }
  function repoLabel(url) {
    try {
      const parsed = new URL(String(url));
      const parts = parsed.pathname.replace(/\.git$/, "").split("/").filter(Boolean);
      return parts.length >= 2 ? parts.slice(-2).join("/") : parsed.hostname + parsed.pathname;
    } catch (_err) {
      return String(url || "");
    }
  }
  function tableCell(column, value) {
    const raw = Array.isArray(value) ? value.join(", ") : String(value ?? "");
    if (!raw) return '<td><span class="subtle">—</span></td>';
    if (column === "repo_url") {
      return `<td><a href="${esc(raw)}" target="_blank" title="${esc(raw)}">${esc(repoLabel(raw))}</a></td>`;
    }
    if (column.endsWith("ref") || column.endsWith("hash")) {
      return `<td><code title="${esc(raw)}">${esc(shortRef(raw))}</code></td>`;
    }
    if (column === "node_types" || column === "node") {
      return `<td class="wrap">${esc(raw)}</td>`;
    }
    return `<td>${esc(raw)}</td>`;
  }
  function table(columns, rows) {
    if (!rows || !rows.length) return `<p class="subtle">${esc(T("No rows."))}</p>`;
    return `<div class="table-wrap"><table><thead><tr>${columns.map(column => `<th>${esc(T(column).replaceAll("_", " "))}</th>`).join("")}</tr></thead><tbody>` +
      rows.map(row => `<tr>${columns.map(column => tableCell(column, row[column])).join("")}</tr>`).join("") +
      "</tbody></table></div>";
  }
  document.getElementById("workflow-select").addEventListener("change", event => selectWorkflow(event.target.value));
  document.getElementById("workflow-use").addEventListener("click", async () => {
    const selectedId = document.getElementById("workflow-select").value;
    if (!selectedId) return setStatus("Select a workflow from the library first.", "error");
    if (!current || current.id !== selectedId) {
      await selectWorkflow(selectedId);
    }
    useCurrentWorkflow();
  });
  document.getElementById("workflow-upload").addEventListener("click", async () => {
    const file = document.getElementById("workflow-file").files[0];
    if (!file) return setStatus("Choose a workflow JSON file first.", "error");
    const isPackage = /\.zip$/i.test(file.name);
    setStatus(isPackage ? "Importing workflow package..." : "Uploading workflow...", "busy");
    try {
      let result;
      if (isPackage) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = "";
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
        result = await api("/api/v1/comfyui/workflows/import", {
          method: "POST",
          body: JSON.stringify({filename: file.name, name: document.getElementById("workflow-name").value.trim() || undefined, package_base64: btoa(binary)})
        });
      } else {
        const content = await file.text();
        result = await api("/api/v1/comfyui/workflows/upload", {
          method: "POST",
          body: JSON.stringify({filename: file.name, name: document.getElementById("workflow-name").value.trim() || file.name, content})
        });
      }
      await loadWorkflows(result.id);
      await selectWorkflow(result.id);
      setStatus(isPackage ? "Workflow package imported" : "Workflow uploaded", "success");
      goStep(nextStepForCurrentWorkflow());
    } catch (err) {
      setStatus(String(err.message || err), "error");
    }
  });
  document.getElementById("workflow-export").addEventListener("click", () => {
    const selectedId = document.getElementById("workflow-select").value;
    if (!selectedId) return setStatus("Select a workflow from the library first.", "error");
    window.location.href = `/api/v1/comfyui/workflows/${encodeURIComponent(selectedId)}/export`;
    setStatus("Workflow package downloading", "success");
  });
  document.getElementById("workflow-rename").addEventListener("click", async () => {
    if (!current) return;
    current = await api(`/api/v1/comfyui/workflows/${encodeURIComponent(current.id)}`, {
      method: "PUT",
      body: JSON.stringify({name: document.getElementById("workflow-name").value.trim()})
    });
    await loadWorkflows(current.id);
    renderAll();
    setStatus("Workflow renamed", "success");
  });
  document.getElementById("workflow-delete").addEventListener("click", async () => {
    if (!current || !window.confirm(T("Delete this workflow record? Active sessions are protected."))) return;
    await api(`/api/v1/comfyui/workflows/${encodeURIComponent(current.id)}`, {method: "DELETE"});
    current = null;
    await loadWorkflows();
    document.getElementById("workflow-select").value = "";
    document.getElementById("workflow-name").value = "";
    document.getElementById("workflow-file").value = "";
    renderAll();
    setStatus("Workflow deleted", "success");
  });
  document.getElementById("workflow-reanalyze").addEventListener("click", async () => {
    if (!current) return;
    await api(`/api/v1/comfyui/workflows/${encodeURIComponent(current.id)}/analyze`, {method: "POST", body: "{}"});
    await selectWorkflow(current.id);
    setStatus("Workflow analyzed", "success");
  });
  document.getElementById("copy-missing-nodes").addEventListener("click", async () => {
    const unresolved = current?.analysis?.unresolved_custom_nodes || [];
    const text = unresolved.map(node => `${node.class_type}: ${node.reason || ""}`).join("\n");
    await navigator.clipboard.writeText(text);
    setStatus("Missing-node summary copied", "success");
  });
  document.getElementById("nodes-resolve-all").addEventListener("click", resolveAllNodesWithUrls);
  document.getElementById("asset-add").addEventListener("click", () => addExtraAsset(false));
  document.getElementById("asset-refresh").addEventListener("click", () => addExtraAsset(true));
  document.getElementById("assets-save-all").addEventListener("click", saveAllAssetRows);
  document.getElementById("workflow-probe").addEventListener("click", runProbe);
  document.getElementById("new-session-dryrun").addEventListener("click", async () => {
    const built = launchPayload();
    if (built.error) return setStatus(built.error);
    setStatus("Running dry run...");
    try {
      const result = await api("/api/v1/resource-requests/dry-run", {method: "POST", body: JSON.stringify(built.payload)});
      renderDryRun(result);
      setStatus(result.ok ? "Dry run complete" : (result.error || "Dry run failed"));
    } catch (err) {
      setStatus(String(err.message || err), "error");
    }
  });
  document.getElementById("create-comfyui").addEventListener("click", async () => {
    const built = launchPayload();
    if (built.error) return setStatus(built.error);
    const createButton = document.getElementById("create-comfyui");
    const dryRunButton = document.getElementById("new-session-dryrun");
    setStatus("Creating ComfyUI...", "busy");
    createButton.disabled = true;
    dryRunButton.disabled = true;
    try {
      const result = await api("/api/v1/resource-requests", {method: "POST", body: JSON.stringify(built.payload)});
      const sessionId = resourceRequestSessionId(result) || (result.id ? await waitForCreateSession(result.id) : "");
      if (sessionId) window.location.href = `/sessions/${encodeURIComponent(sessionId)}/workflow`;
      else setStatus(`Request ${result.id || ""} accepted`);
    } catch (err) {
      setStatus(String(err.message || err), "error");
      renderAll();
    } finally {
      createButton.disabled = false;
      dryRunButton.disabled = false;
    }
  });
  renderAll();
  loadWorkflows()
    .then(() => renderAll())
    .catch(err => setStatus(String(err.message || err), "error"));
})();
</script>
"""


def model_folder_select(element_id: str, name: str, *, selected: str = "checkpoints") -> str:
    folders = [
        ("checkpoints", "Checkpoint / diffusion checkpoint"),
        ("diffusion_models", "Diffusion model / unet"),
        ("loras", "LoRA"),
        ("vae", "VAE"),
        ("controlnet", "ControlNet"),
        ("text_encoders", "Text encoders"),
        ("clip", "CLIP"),
        ("clip_vision", "CLIP Vision"),
        ("embeddings", "Embeddings"),
        ("upscale_models", "Upscale models"),
        ("ultralytics", "Ultralytics"),
        ("SEEDVR2", "SEEDVR2"),
        ("configs", "Configs"),
        ("gligen", "GLIGEN"),
        ("hypernetworks", "Hypernetworks"),
    ]
    options = []
    for value, label in folders:
        selected_attr = " selected" if value == selected else ""
        options.append(f'<option value="{html.escape(value)}"{selected_attr}>{html.escape(label)}</option>')
    return f'<select id="{html.escape(element_id)}" name="{html.escape(name)}">' + "".join(options) + "</select>"


def model_manager_panel(session_id: str, operations: list[dict[str, Any]], *, active: bool) -> str:
    escaped_id = html.escape(session_id)
    controls = ""
    if active:
        controls = f"""
<div class="form-grid" id="runtime-model-form">
  <div class="form-row wide">
    <label for="runtime-model-url">{t("Add runtime model / LoRA URL")}</label>
    <input id="runtime-model-url" placeholder="https://huggingface.co/... or https://civitai...">
  </div>
  <div class="form-row">
    <label for="runtime-model-folder">{t("ComfyUI folder")}</label>
    {model_folder_select("runtime-model-folder", "runtime_model_folder", selected="loras")}
  </div>
  <div class="form-row">
    <label for="runtime-model-filename">{t("Filename override")}</label>
    <input id="runtime-model-filename" placeholder="optional">
  </div>
  <div class="form-row">
    <label>&nbsp;</label>
    <button type="button" class="primary" id="runtime-model-download">{t("Add download")}</button>
  </div>
</div>
"""
    else:
        controls = f'<p class="subtle">{t("Terminal sessions are read-only; runtime model download and move controls are hidden.")}</p>'
    return f"""
<h2>{t("Models")}</h2>
<p class="actions">
  <button type="button" id="runtime-model-refresh">{t("Refresh tree")}</button>
</p>
{controls}
<div class="operation-progress" id="runtime-model-progress">
  <div class="progress-head"><strong id="runtime-model-progress-title">Working</strong><span id="runtime-model-progress-detail"></span></div>
  <progress id="runtime-model-progress-bar"></progress>
</div>
<div id="runtime-model-status" class="subtle"></div>
<h3>{t("Runtime model operations")}</h3>
{table(['id','operation_type','state','model_folder','filename','size_bytes','source_url_redacted','source_path','target_path','error','created_at','finished_at'], operations)}
<h3>{t("Model tree")}</h3>
<div id="runtime-model-tree" class="subtle">{t("Click Refresh tree to load current files from the running Pod.")}</div>
{model_manager_script(escaped_id, active)}
"""


def model_manager_script(session_id: str, active: bool) -> str:
    session_json = json.dumps(session_id)
    active_json = json.dumps(bool(active))
    return f"""
<script>
(() => {{
  const sessionId = {session_json};
  const active = {active_json};
  const T = window.__T || (s => s);
  const status = document.getElementById("runtime-model-status");
  const tree = document.getElementById("runtime-model-tree");
  const refreshButton = document.getElementById("runtime-model-refresh");
  const downloadButton = document.getElementById("runtime-model-download");
  const progress = document.getElementById("runtime-model-progress");
  const progressTitle = document.getElementById("runtime-model-progress-title");
  const progressDetail = document.getElementById("runtime-model-progress-detail");
  const progressBar = document.getElementById("runtime-model-progress-bar");
  function esc(value) {{
    return String(value ?? "").replace(/[&<>"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}})[ch]);
  }}
  function pathTail(value) {{
    const parts = String(value || "").split("/").filter(Boolean);
    return parts.length > 2 ? ".../" + parts.slice(-2).join("/") : String(value || "");
  }}
  function setBusy(button, busy, label) {{
    if (!button) return;
    button.disabled = busy;
    if (busy) {{
      button.dataset.originalText = button.textContent;
      button.textContent = label || T("Working...");
      progress.classList.add("active");
      progressBar.removeAttribute("value");
      progressTitle.textContent = label || T("Working");
      progressDetail.textContent = "";
    }} else {{
      button.textContent = button.dataset.originalText || button.textContent;
      progress.classList.remove("active");
    }}
  }}
  function sizeLabel(bytes) {{
    const value = Number(bytes || 0);
    if (!value) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let n = value, i = 0;
    while (n >= 1024 && i < units.length - 1) {{ n /= 1024; i += 1; }}
    return `${{n.toFixed(i ? 2 : 0)}} ${{units[i]}}`;
  }}
  function renderTree(data) {{
    const files = data.files || [];
    if (!files.length) {{
      tree.innerHTML = `<p class="subtle">${{esc(T("No model files found."))}}</p>`;
      return;
    }}
    const groups = new Map();
    for (const file of files) {{
      const key = `${{file.source || "unknown"}} / ${{file.folder || ""}}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(file);
    }}
    tree.innerHTML = [...groups.entries()].map(([group, rows]) => `
      <details open>
        <summary><strong>${{esc(group)}}</strong> <span class="badge">${{rows.length}} ${{esc(T("file(s)"))}}</span></summary>
        <div class="table-wrap"><table><thead><tr><th>name</th><th>size</th><th>path</th><th>link</th><th>action</th></tr></thead>
        <tbody>${{rows.map(row => `
          <tr>
            <td><code>${{esc(row.name)}}</code></td>
            <td>${{sizeLabel(row.size_bytes)}}</td>
            <td><code title="${{esc(row.path)}}">${{esc(row.relative_path || row.name)}}</code></td>
            <td>${{row.is_symlink ? `<code title="${{esc(row.symlink_target || row.resolved_path || "")}}">${{esc(pathTail(row.symlink_target || row.resolved_path || ""))}}</code>` : ""}}</td>
            <td>${{active && row.controller_managed && row.source === "controller_assets" ? `<button type="button" data-model-move="${{esc(row.path)}}">${{esc(T("Move"))}}</button>` : ""}}</td>
          </tr>`).join("")}}</tbody></table>
      </details>
    `).join("");
    bindMoveButtons();
  }}
  async function refreshTree() {{
    setBusy(refreshButton, true, T("Refreshing tree..."));
    try {{
      const response = await fetch(`/api/v1/sessions/${{encodeURIComponent(sessionId)}}/models/tree`, {{cache: "no-store"}});
      const data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || data.reason || "refresh failed");
      renderTree(data);
      status.textContent = `${{T("Loaded")}} ${{(data.files || []).length}} ${{T("model row(s).")}}`;
    }} catch (err) {{
      status.textContent = String(err);
    }} finally {{
      setBusy(refreshButton, false);
    }}
  }}
  async function addDownload() {{
    if (!active) return;
    const url = document.getElementById("runtime-model-url").value.trim();
    const modelFolder = document.getElementById("runtime-model-folder").value;
    const filename = document.getElementById("runtime-model-filename").value.trim();
    if (!url) {{
      status.textContent = T("URL is required.");
      return;
    }}
    setBusy(downloadButton, true, "Queueing download...");
    try {{
      const response = await fetch(`/api/v1/sessions/${{encodeURIComponent(sessionId)}}/models/downloads`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{url, model_folder: modelFolder, filename}})
      }});
      const data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || data.state || "download failed");
      status.textContent = `Download queued: ${{data.operation_id}}`;
      window.setTimeout(() => window.location.reload(), 1200);
    }} catch (err) {{
      status.textContent = String(err);
    }} finally {{
      setBusy(downloadButton, false);
    }}
  }}
  function bindMoveButtons() {{
    for (const button of document.querySelectorAll("[data-model-move]")) {{
      button.addEventListener("click", async () => {{
        const sourcePath = button.dataset.modelMove;
        const targetFolder = window.prompt(T("Move to ComfyUI folder, e.g. loras / checkpoints / vae"), "loras");
        if (!targetFolder) return;
        const targetFilename = window.prompt(T("Filename"), sourcePath.split("/").pop() || "");
        if (!targetFilename) return;
        setBusy(button, true, T("Moving..."));
        try {{
          const response = await fetch(`/api/v1/sessions/${{encodeURIComponent(sessionId)}}/models/move`, {{
            method: "POST",
            headers: {{"Content-Type": "application/json"}},
            body: JSON.stringify({{source_path: sourcePath, target_folder: targetFolder, target_filename: targetFilename}})
          }});
          const data = await response.json();
          if (!response.ok || data.ok === false) throw new Error(data.error || data.state || "move failed");
          status.textContent = `${{T("Moved:")}} ${{data.operation_id}}`;
          await refreshTree();
        }} catch (err) {{
          status.textContent = String(err);
        }} finally {{
          setBusy(button, false);
        }}
      }});
    }}
  }}
  refreshButton?.addEventListener("click", refreshTree);
  downloadButton?.addEventListener("click", addDownload);
  const growButton = document.getElementById("runtime-volume-grow");
  growButton?.addEventListener("click", async () => {{
    const sizeInput = document.getElementById("runtime-volume-size");
    const sizeGb = Number(sizeInput?.value || 0);
    if (!sizeGb) {{
      status.textContent = T("Enter a target size in GB.");
      return;
    }}
    setBusy(growButton, true, T("Growing volume..."));
    try {{
      const response = await fetch(`/api/v1/sessions/${{encodeURIComponent(sessionId)}}/volume/resize`, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{size_gb: sizeGb}}),
        cache: "no-store"
      }});
      const data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || data.state || "resize failed");
      status.textContent = `Volume resized to ${{sizeGb}} GB.`;
      window.location.reload();
    }} catch (err) {{
      status.textContent = String(err);
    }} finally {{
      setBusy(growButton, false);
    }}
  }});
}})();
</script>
"""


SESSION_TABS = [
    ("overview", "Overview", ""),
    ("workflow", "Workflow", "/workflow"),
    ("models", "Models", "/models"),
    ("outputs", "Outputs", "/outputs"),
    ("debug", "Debug", "/debug"),
]


def session_header(session: dict[str, Any], active_tab: str) -> str:
    session_id = html.escape(str(session.get("id") or ""))
    session_state = str(session.get("state") or "")
    has_active_controls = session_state not in TERMINAL_STATES
    ui_url = str(session.get("ui_url") or "")
    output_collection_running = str(session.get("output_collection_state") or "") == "running"
    shutdown_disabled = " disabled" if output_collection_running else ""
    tabs = "".join(
        f'<a href="/sessions/{session_id}{suffix}" class="{"active" if key == active_tab else ""}">{t(label)}</a>'
        for key, label, suffix in SESSION_TABS
    )
    output_file_count = int(session.get("output_collection_file_count") or 0)
    if has_active_controls and ui_url:
        open_button = f'<a class="button primary" href="{html.escape(ui_url)}" target="_blank">{t("Open ComfyUI")}</a>'
    elif not has_active_controls and output_file_count > 0:
        # The proxy URL dies with the GPU pod; what remains useful is the local copy.
        open_button = f'<button class="primary" data-post="/api/v1/sessions/{session_id}/outputs/open-local" data-no-reload="1">{t("Open output folder")} ({output_file_count})</button>'
    else:
        open_button = ""
    shutdown = ""
    if has_active_controls:
        shutdown = (
            f'<button class="danger" data-post="/api/v1/sessions/{session_id}/reclaim" data-body=\'{{"force":true}}\' data-redirect="back" '
            f'data-confirm="{t("Shutdown session")} {session_id}? {t("This stops GPU compute first, collects ComfyUI outputs through S3, then deletes the network volume only after collection succeeds.")}"{shutdown_disabled}>{t("Shutdown: stop GPU + collect outputs")}</button>'
        )
    return f"""
<section class="page-head">
  <div>
    <h2>{t("Session")} <code>{session_id}</code> {state_pill(session_state)}</h2>
    <div class="subtle">{t("Phase")}: <span data-workflow-phase>{html.escape(str(session.get("phase") or ""))}</span> · {t("Mode")}: {html.escape(str(session.get("mode") or ""))} · {t("Datacenter")}: {html.escape(str(session.get("data_center_id") or "-"))}</div>
  </div>
  <div class="actions">
    {open_button}
    {shutdown}
  </div>
</section>
<nav class="tabs">{tabs}</nav>
"""


class raw_html(str):
    """Marks a kv_grid value as pre-rendered HTML that must not be escaped."""


_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?$")


def fmt_timestamp(value: Any) -> str:
    """Render stored UTC timestamps in the controller host's local timezone."""
    text = ("" if value is None else str(value)).strip()
    if not _TIMESTAMP_RE.match(text):
        return "" if value is None else str(value)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def kv_grid(items: list[tuple[str, Any]]) -> str:
    cells = []
    for label_text, value in items:
        if isinstance(value, raw_html):
            text = str(value)
        else:
            plain = "" if value is None else str(value)
            text = html.escape(fmt_timestamp(plain)) if plain else '<span class="subtle">—</span>'
        cells.append(f"<div><label>{html.escape(label_text)}</label><span>{text}</span></div>")
    return '<div class="kv-grid">' + "".join(cells) + "</div>"


def session_detail(session: dict[str, Any]) -> bytes:
    session = dict(session)
    session_id = html.escape(session["id"])
    session_state = str(session.get("state") or "")
    has_active_controls = session_state not in TERMINAL_STATES
    workflow = session.get("workflow") or {}
    candidates = session.get("candidates") or []
    workflow_events = session.get("workflow_events") or []
    output_summary = session.get("output_summary") or {}
    controls = ""
    if has_active_controls:
        controls = f"""
<section class="card">
  <h2>{t("Controls")}</h2>
  <p class="actions" style="justify-content:flex-start">
    <button data-post="/api/v1/sessions/{session_id}/promote-to-gpu">{t("Promote GPU")}</button>
    <button data-post="/api/v1/sessions/{session_id}/tunnel/restart">{t("Restart tunnel")}</button>
    <button data-post="/api/v1/sessions/{session_id}/outputs/collect" data-body='{{"mode":"manual"}}'>{t("Collect outputs")}</button>
    <button data-post="/api/v1/sessions/{session_id}/lease" data-body='{{"minutes":30}}'>{t("+30m lease")}</button>
    <button data-post="/api/v1/sessions/{session_id}/lease" data-body='{{"minutes":60}}'>{t("+60m lease")}</button>
    <button data-post="/api/v1/sessions/{session_id}/watchdog/pause">{t("Pause watchdog")}</button>
    <button data-post="/api/v1/sessions/{session_id}/watchdog/resume">{t("Resume watchdog")}</button>
  </p>
</section>
"""
    body = f"""
{session_header(session, "overview")}
<section class="metrics">
  <div class="metric"><strong>${float(session.get('effective_cost_usd') or 0):.6f}</strong>{t("Spend")}</div>
  <div class="metric"><strong>{html.escape(str(session.get('elapsed') or '-'))}</strong>{t("Elapsed")}</div>
  <div class="metric"><strong>{html.escape(str(session.get('aggregate_pod_runtime') or '-'))}</strong>{t("Pod runtime")}</div>
  <div class="metric"><strong>{len(candidates)}</strong>{t("Candidates")}</div>
  <div class="metric"><strong>{html.escape(str(output_summary.get('file_count') or 0))}</strong>{t("Output files")}</div>
</section>
{controls}
<section class="card">
  <h2>{t("Session")}</h2>
  {kv_grid([
      (t("Product"), session.get("product")),
      (t("Mode"), session.get("mode")),
      (t("Datacenter"), session.get("data_center_id")),
      (t("GPU intent"), f"{session.get('min_vram_gb') or ''} GB VRAM · {session.get('gpu_vendor') or ''}"),
      (t("Lease until"), session.get("lease_until")),
      (t("Idle shutdown at"), session.get("idle_shutdown_at")),
      (t("Cost cap"), f"${float(session.get('max_total_usd') or 0):.2f}" if session.get("max_total_usd") else None),
      (t("Watchdog reason"), session.get("watchdog_last_reason")),
      (t("UI URL"), raw_html(f'<a href="{html.escape(str(session.get("ui_url")))}" target="_blank">{html.escape(str(session.get("ui_url")))}</a>') if session.get("ui_url") else None),
      (t("Retention policy"), session.get("retention_policy")),
      (t("Created"), session.get("created_at")),
      (t("Updated"), session.get("updated_at")),
  ])}
</section>
<section class="card">
  <h2>{t("Workflow")}</h2>
  <p class="actions" style="justify-content:flex-start"><a class="button" href="/sessions/{session_id}/workflow">{t("Open workflow")}</a></p>
  {kv_grid([
      (t("Workflow"), raw_html(f"<code>{html.escape(str(workflow.get('id') or ''))}</code>") if workflow.get("id") else None),
      (t("State"), raw_html(state_pill(workflow.get("state"))) if workflow.get("state") else None),
      (t("ComfyUI workflow"), raw_html(f"<code>{html.escape(str(workflow.get('comfyui_workflow_id') or ''))}</code>") if workflow.get("comfyui_workflow_id") else None),
      (t("Winner candidate"), raw_html(f"<code>{html.escape(str(workflow.get('winner_candidate_id') or ''))}</code>") if workflow.get("winner_candidate_id") else None),
      (t("Volume"), f"{workflow.get('volume_size_gb')} GB" if workflow.get("volume_size_gb") else None),
      (t("Max GPU rate"), f"${float(workflow.get('max_gpu_usd_per_hr') or 0):.2f}/hr" if workflow.get("max_gpu_usd_per_hr") else None),
      (t("Max total"), f"${float(workflow.get('max_total_usd') or 0):.2f}" if workflow.get("max_total_usd") else None),
      (t("Completed"), workflow.get("completed_at")),
  ]) if workflow else f'<p class="subtle">{t("No workflow attached to this session.")}</p>'}
</section>
<h2>{t("Candidate Plan")}</h2>
{candidate_plan(candidates, (workflow or {}).get('winner_candidate_id'))}
<h2>{t("Workflow Events")}</h2>
{table(['event_type','data_center_id','message','candidate_id','created_at'], workflow_events)}
{action_script()}
"""
    return page(f"Session {session['id']}", body)


def session_models_page(session: dict[str, Any]) -> bytes:
    session = dict(session)
    session_id = html.escape(session["id"])
    session_state = str(session.get("state") or "")
    has_active_controls = session_state not in TERMINAL_STATES
    model_operations = session.get("model_operations") or []
    volume = session.get("volume") or {}
    volume_size = int(volume.get("size_gb") or 0)
    resize = ""
    if has_active_controls and volume:
        resize = f"""
<section class="card">
  <h2>{t("Network volume")}</h2>
  <div class="form-grid">
    <div class="form-row">
      <label>{t("Current size")}</label>
      <span style="padding:7px 0">{volume_size} GB</span>
    </div>
    <div class="form-row">
      <label for="runtime-volume-size">{t("Grow to (GB)")}</label>
      <input id="runtime-volume-size" type="number" min="{max(10, volume_size + 1)}" step="1" value="{max(10, volume_size + 5)}">
    </div>
    <div class="form-row">
      <label>&nbsp;</label>
      <button type="button" id="runtime-volume-grow">{t("Grow volume")}</button>
    </div>
  </div>
  <p class="subtle">{t("Resize is grow-only. The controller verifies the running Pod actually sees the new size with")} <code>df /workspace</code></p>
</section>
"""
    body = f"""
{session_header(session, "models")}
<section class="card">
{model_manager_panel(session_id, model_operations, active=has_active_controls)}
</section>
{resize}
{action_script()}
"""
    return page(f"Models · {session['id']}", body)


def session_outputs_page(session: dict[str, Any]) -> bytes:
    session = dict(session)
    session_id = html.escape(session["id"])
    session_state = str(session.get("state") or "")
    has_active_controls = session_state not in TERMINAL_STATES
    output_summary = session.get("output_summary") or {}
    output_collections = session.get("output_collections") or []
    output_artifacts = session.get("output_artifacts") or []
    volume = session.get("volume") or {}
    retained = session_state in {"output_collection_failed_keep_volume", "output_collection_empty_keep_volume"}
    actions = []
    if int(output_summary.get("file_count") or 0) > 0:
        actions.append(f'<button data-post="/api/v1/sessions/{session_id}/outputs/open-local" data-no-reload="1">{t("Open local folder")}</button>')
    if has_active_controls or retained:
        actions.append(f'<button data-post="/api/v1/sessions/{session_id}/outputs/collect" data-body=\'{{"mode":"manual"}}\'>{t("Collect outputs now")}</button>')
    if retained:
        actions.append(
            f'<button class="danger" data-post="/api/v1/sessions/{session_id}/reclaim" data-body=\'{{"force":true,"discard_outputs":true}}\' data-redirect="back" '
            f'data-confirm="{t("Discard uncollected outputs and delete the retained network volume for session")} {session_id}? {t("This cannot be undone.")}">{t("Discard outputs + delete volume")}</button>'
        )
    bytes_total = int(output_summary.get("bytes") or 0)
    body = f"""
{session_header(session, "outputs")}
<section class="metrics">
  <div class="metric"><strong>{html.escape(str(output_summary.get('file_count') or 0))}</strong>{t("Output files")}</div>
  <div class="metric"><strong>{bytes_total / (1024 * 1024):.1f} MB</strong>{t("Collected bytes")}</div>
  <div class="metric"><strong>{html.escape(str(output_summary.get('state') or '-'))}</strong>{t("Collection state")}</div>
  <div class="metric"><strong>{t('yes') if output_summary.get('retained_volume') else t('no')}</strong>{t("Volume retained")}</div>
</section>
<section class="card">
  <h2>{t("Collection")}</h2>
  <p class="subtle">{t("Outputs are copied from the network volume through the RunPod S3-compatible API into the local artifacts directory. The volume is only deleted after a successful final collection.")}</p>
  <p class="actions" style="justify-content:flex-start">{''.join(actions) if actions else f'<span class="subtle">{t("No collection actions available for this session state.")}</span>'}</p>
  <div class="subtle">{t("Last checked:")} {html.escape(fmt_timestamp(output_summary.get('last_checked_at')) or '-')} · {t("Last error:")} {html.escape(str(output_summary.get('last_error') or '-'))}</div>
</section>
<h2>{t("Collection runs")}</h2>
{table(['id','mode','state','file_count','byte_count','downloaded_count','skipped_count','error','started_at','finished_at'], output_collections)}
<h2>{t("Collected files")}</h2>
{table(['id','kind','local_path','remote_uri','checksum_sha256','size_bytes','created_at'], output_artifacts)}
<h2>{t("Volume")}</h2>
{table(['id','provider_volume_id','state','size_gb','data_center_id','effective_cost_usd','cost_source'], [volume] if volume else [])}
{action_script()}
"""
    return page(f"Outputs · {session['id']}", body)


def session_debug_page(session: dict[str, Any]) -> bytes:
    session = dict(session)
    tasks = session.pop("tasks", [])
    artifacts = session.pop("artifacts", [])
    session.pop("output_artifacts", [])
    session.pop("output_collections", [])
    session.pop("output_summary", {})
    session.pop("model_operations", [])
    audits = session.pop("audit_events", [])
    attempts = session.pop("gpu_acquisition_attempts", [])
    tunnels = session.pop("tunnels", [])
    watchdog_events = session.pop("watchdog_events", [])
    pods = session.pop("pods", [])
    volume = session.pop("volume", None)
    session.pop("workflow", None)
    session.pop("candidates", [])
    session.pop("workflow_events", [])
    body = f"""
{session_header(session, "debug")}
<h2>{t("Session row")}</h2>
{table(['key','value'], [{'key': key, 'value': fmt_timestamp(value) if isinstance(value, str) else value} for key, value in session.items()])}
<h2>{t("GPU Acquisition Attempts")}</h2>
{table(['attempt_number','state','data_center_id','gpu_type_id','quoted_cost_usd_per_hr','quote_source','provider_pod_id','error','backoff_seconds','created_at'], attempts)}
<h2>{t("Tunnel")}</h2>
{table(['id','state','protocol','remote_host','remote_port','local_host','local_port','local_url','restart_count','auto_recover','last_health_check_at','last_error'], tunnels)}
<h2>{t("Watchdog")}</h2>
{table(['event_type','reason','created_at'], watchdog_events)}
<h2>{t("Pods")}</h2>
{table(['id','provider_pod_id','provider_mode','role','compute_type','state','effective_start_at','effective_stop_at','runtime','effective_cost_usd','estimated_cost_usd','actual_cost_usd','cost_source','rate_usd_per_hr'], pods)}
<h2>Volume</h2>
{table(['id','provider_volume_id','provider_mode','state','effective_start_at','effective_stop_at','runtime','effective_cost_usd','estimated_cost_usd','actual_cost_usd','cost_source','rate_usd_per_hr'], [volume] if volume else [])}
<h2>{t("Tasks")}</h2>
{table(['id','state','workflow_ref','prompt','external_id','created_at'], tasks)}
<h2>{t("Artifacts")}</h2>
{table(['id','kind','local_path','checksum_sha256','size_bytes','created_at'], artifacts)}
<h2>{t("Audit")}</h2>
{table(['event_type','message','created_at'], audits)}
{action_script()}
"""
    return page(f"Debug · {session['id']}", body)


def workflow_debug_sections(workflow: dict[str, Any]) -> str:
    analyzer = workflow.get("analyzer_result") or {}
    probe = workflow.get("probe_result") or {}
    install_plan = workflow.get("install_plan") or {}
    validation_plan = workflow.get("validation_plan") or {}
    comfyui_workflow = workflow.get("comfyui_workflow") or {}
    launch_template = workflow.get("launch_template") or {}
    resolved = analyzer.get("resolved_custom_nodes") or workflow.get("custom_nodes") or []
    unresolved = analyzer.get("unresolved_custom_nodes") or []
    warnings = analyzer.get("warnings") or []
    install_steps = install_plan.get("steps") or []
    validation_checks = validation_plan.get("checks") or []
    template_rows = []
    if comfyui_workflow:
        template_rows.append(
            {
                "key": "workflow",
                "value": f"{comfyui_workflow.get('name') or ''} ({comfyui_workflow.get('workflow_hash') or ''})",
            }
        )
        template_rows.extend(
            [
                {"key": "verification_state", "value": comfyui_workflow.get("verification_state")},
                {"key": "dependency_fingerprint", "value": comfyui_workflow.get("dependency_fingerprint")},
                {"key": "launch_fingerprint", "value": comfyui_workflow.get("launch_fingerprint")},
            ]
        )
    elif launch_template:
        template_rows.append(
            {
                "key": "legacy_launch_template",
                "value": f"{launch_template.get('name') or ''} ({launch_template.get('id') or ''})",
            }
        )
    if analyzer:
        template_rows.extend(
            [
                {"key": "analysis_ok", "value": analyzer.get("ok")},
                {"key": "node_count", "value": analyzer.get("node_count")},
                {"key": "ui_workflow_present", "value": analyzer.get("ui_workflow_present")},
                {"key": "api_workflow_present", "value": analyzer.get("api_workflow_present")},
            ]
        )
    if probe:
        template_rows.extend(
            [
                {"key": "probe_id", "value": probe.get("id")},
                {"key": "probe_state", "value": probe.get("state")},
                {"key": "probe_cached", "value": probe.get("cached")},
                {"key": "probe_error", "value": probe.get("error")},
            ]
        )
    return f"""
<h2>{t("Workflow Lock")}</h2>
{table(['key','value'], template_rows) if template_rows else f'<p class="subtle">{t("No workflow lock metadata.")}</p>'}
<h2>{t("Workflow Analysis")}</h2>
{table(['package','repo_url','ref','source','node_types'], resolved)}
{table(['class_type','reason'], unresolved) if unresolved else f'<p class="subtle">{t("No unresolved custom nodes.")}</p>'}
{table(['warning'], [{'warning': item} for item in warnings]) if warnings else f'<p class="subtle">{t("No analyzer warnings.")}</p>'}
<h2>{t("Dependency Probe")}</h2>
{table(['key','value'], [{'key': key, 'value': value} for key, value in probe.items() if key not in {'result_json'}]) if probe else f'<p class="subtle">{t("No CPU probe result.")}</p>'}
<h2>{t("Install Plan")}</h2>
{table(['package','repo_url','ref','install_method','target_dir','run_requirements'], install_steps) if install_steps else f'<p class="subtle">{t("No custom-node install steps.")}</p>'}
<h2>{t("Validation Plan")}</h2>
{table(['check'], [{'check': check} for check in validation_checks]) if validation_checks else f'<p class="subtle">{t("No validation checks recorded.")}</p>'}
"""


def workflow_page(session: dict[str, Any]) -> bytes:
    session = dict(session)
    workflow = session.get("workflow") or {}
    candidates = workflow.get("candidates") or session.get("candidates") or []
    events = workflow.get("events") or session.get("workflow_events") or []
    active_controls = str(session.get("state") or "") not in TERMINAL_STATES
    output_summary = session.get("output_summary") or {}
    output_collection_running = str(session.get("output_collection_state") or "") == "running"
    terminate_disabled = " disabled" if output_collection_running else ""
    workflow_actions = f"""
    {f'<button data-post="/api/v1/sessions/{html.escape(session["id"])}/workflow/verify">{t("Mark workflow verified")}</button>' if workflow.get("comfyui_workflow_id") else ''}
    {f'<button class="danger" data-post="/api/v1/sessions/{html.escape(session["id"])}/workflow/terminate" data-redirect="back" data-confirm="{t("Terminate this workflow? GPU compute stops first; outputs are collected before the network volume can be deleted.")}"{terminate_disabled}>{t("Terminate: stop GPU + collect outputs")}</button>' if active_controls else ''}
    """
    body = f"""
{session_header(session, "workflow")}
<section class="metrics">
  <div class="metric"><strong data-workflow-state>{html.escape(str(workflow.get('state') or session.get('state') or ''))}</strong>{t("Workflow state")}</div>
  <div class="metric"><strong>{len(candidates)}</strong>Candidates</div>
  <div class="metric"><strong>{html.escape(str(workflow.get('volume_size_gb') or ''))}</strong>Volume GB</div>
  <div class="metric"><strong>${float(session.get('effective_cost_usd') or 0):.6f}</strong>Spend</div>
  <div class="metric"><strong>{html.escape(str(output_summary.get('file_count') or 0))}</strong>Output files</div>
</section>
<section class="card">
  <h2>{t("Workflow actions")}</h2>
  <p class="actions" style="justify-content:flex-start">{workflow_actions}</p>
</section>
<h2>{t("Candidate progress")}</h2>
{candidate_plan(candidates, workflow.get('winner_candidate_id'))}
<h2>{t("Events")}</h2>
{table(['event_type','data_center_id','message','candidate_id','created_at'], events)}
{workflow_debug_sections(workflow)}
{action_script()}
{workflow_refresh_script(session['id'])}
"""
    return page(f"Workflow · {session['id']}", body)


def human_bytes(value) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "0 B"
    if size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


def candidate_plan(candidates: list[dict[str, Any]], winner_id: str | None = None) -> str:
    """Expandable candidate rows: datacenter, state, and progress at a glance;
    resource ids, cleanup status, and full errors behind a click."""
    if not candidates:
        return f'<p class="subtle">{t("No candidates.")}</p>'
    rows = []
    for candidate in candidates:
        candidate_id = html.escape(str(candidate.get("id") or ""))
        state = str(candidate.get("state") or "")
        total = int(candidate.get("download_total_bytes") or 0)
        done = int(candidate.get("download_done_bytes") or 0)
        percent = min(100, round(done / total * 100)) if total > 0 else 0
        progress_label = f"{human_bytes(done)} / {human_bytes(total)}" if total > 0 else t("no download")
        attempts = int(candidate.get("attempt_count") or 0)
        last_error = str(candidate.get("last_error") or "")
        cleanup_status = str(candidate.get("cleanup_status") or "")
        gpu_type = str(candidate.get("gpu_type_id") or "")
        rate = candidate.get("quoted_cost_usd_per_hr")
        classes = "cand"
        if (winner_id and candidate.get("id") == winner_id) or state == "won":
            classes += " cand-won"
        if state == "deleted":
            classes += " cand-deleted"
        meta_bits = []
        if gpu_type:
            meta_bits.append(html.escape(gpu_type))
        if rate:
            meta_bits.append(f"${float(rate):.4f}/hr")
        meta_bits.append(f"{attempts} {t('attempts') if attempts != 1 else t('attempt')}")
        if last_error and state != "deleted":
            meta_bits.append("⚠")
        detail = kv_grid(
            [
                (t("Candidate"), candidate.get("id")),
                (t("Volume"), candidate.get("volume_id") or "—"),
                (t("CPU Pod"), candidate.get("cpu_pod_id") or "—"),
                (t("GPU Pod"), candidate.get("gpu_pod_id") or "—"),
                (t("Cleanup"), cleanup_status or "—"),
                (t("Downloaded"), f"{done:,} / {total:,} bytes" if total else "—"),
            ]
        )
        error_block = ""
        if last_error:
            if state == "deleted":
                # Losers often record provider 404s from their own cleanup; that is
                # evidence of deletion, not a user-facing failure.
                error_block = f'<div class="cand-note">{t("cleanup evidence:")} {html.escape(last_error)}</div>'
            else:
                error_block = f'<div class="cand-error">{html.escape(last_error)}</div>'
        rows.append(
            f"""
<details class="{classes}" data-candidate-id="{candidate_id}">
  <summary>
    <span class="cand-dc">{html.escape(str(candidate.get('data_center_id') or ''))}</span>
    <span data-candidate-state>{state_pill(state)}</span>
    <span class="cand-progress">
      <progress max="100" value="{percent}" data-candidate-progress></progress>
      <span class="subtle" data-candidate-progress-label>{html.escape(progress_label)}</span>
    </span>
    <span class="cand-meta subtle">{' · '.join(meta_bits)}</span>
    <span class="cand-chevron">▾</span>
  </summary>
  <div class="cand-body">
    {detail}
    {error_block}
  </div>
</details>
"""
        )
    return '<div class="cand-list">' + "".join(rows) + "</div>"


WRAP_COLUMNS = {
    "message",
    "error",
    "last_error",
    "value",
    "reason",
    "prompt",
    "cleanup_status",
    "local_path",
    "remote_uri",
    "source_path",
    "target_path",
    "source_url_redacted",
    "workflow_ref",
    "external_id",
}

MONO_COLUMNS = {"id", "key", "provider_pod_id", "provider_volume_id", "candidate_id", "comfyui_workflow_id", "winner_candidate_id", "volume_id", "cpu_pod_id", "gpu_pod_id", "session_id", "record_key"}

_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{12,64}$")
SHORT_HASH_COLUMNS = {"checksum_sha256", "workflow_hash", "dependency_fingerprint", "launch_fingerprint", "record_key"}


def _is_time_column(column: str) -> bool:
    return column.endswith("_at") or column.endswith("deadline") or column.endswith("until")


def table(columns: list[str], rows: list[dict[str, Any]], *, link_session: bool = False) -> str:
    if not rows:
        return f'<p class="subtle">{html.escape(t("No rows."))}</p>'
    head = "".join(f"<th>{html.escape(t_column(column))}</th>" for column in columns)
    body_rows = []
    for row in rows:
        row_id = html.escape(str(row.get("id", "")))
        cells = []
        for column in columns:
            value = row.get(column)
            plain = "" if value is None else str(value)
            if _is_time_column(column):
                plain = fmt_timestamp(plain)
            text = html.escape(plain)
            if not plain:
                text = '<span class="subtle">—</span>'
            elif column in SHORT_HASH_COLUMNS and _HEX_DIGEST_RE.match(plain.lower()):
                # Full digests force tables wide for no reading value; hover shows all.
                text = f'<code title="{html.escape(plain)}">{html.escape(plain[:10])}</code>'
            elif column in MONO_COLUMNS:
                text = f"<code>{text}</code>"
            if column == "id" and link_session and plain:
                text = f'<a href="/sessions/{html.escape(plain)}">{text}</a>'
            if column in {"ui_url", "local_ui_url"} and value:
                text = f'<a href="{html.escape(str(value))}" target="_blank">{html.escape(str(value))}</a>'
            if column == "state" or column.endswith("_state") or column == "tunnel_status":
                text = state_pill(value) or text
            if column == "provider_mode" and value == "fake":
                text = '<span class="state">FAKE</span>'
            if column in {"estimated_cost_usd", "actual_cost_usd", "effective_cost_usd"}:
                try:
                    text = f"${float(value or 0):.6f}"
                except (TypeError, ValueError):
                    text = "$0.000000"
            if column == "cost_source" and plain:
                text = f'<span class="state">{html.escape(plain)}</span>'
            if column in {"rate_usd_per_hr", "quoted_cost_usd_per_hr"}:
                try:
                    text = f"${float(value or 0):.6f}/hr"
                except (TypeError, ValueError):
                    text = "$0.000000/hr"
            if column in {"size_bytes", "byte_count", "download_done_bytes", "download_total_bytes"} and value not in (None, ""):
                try:
                    text = f'<span title="{html.escape(plain)}">{html.escape(human_bytes(value))}</span>'
                except (TypeError, ValueError):
                    pass
            classes = " wrap" if column in WRAP_COLUMNS else ""
            pill_attr = ' data-pill="1"' if (column == "state" or column.endswith("_state") or column == "tunnel_status") else ""
            cells.append(f'<td data-column="{html.escape(column)}"{pill_attr} class="cell{classes}">{text}</td>')
        body_rows.append(f'<tr data-row-id="{row_id}">' + "".join(cells) + "</tr>")
    return '<div class="table-wrap"><table><thead><tr>' + head + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table></div>"


def action_script() -> str:
    return """
<script>
(() => {
  function confirmationBypassed() {
    try {
      const params = new URLSearchParams(window.location.search);
      const flag = String(params.get("bypass_confirm") || params.get("confirm_bypass") || "").toLowerCase();
      if (["1", "true", "yes"].includes(flag)) return true;
      return window.localStorage && window.localStorage.getItem("runpodControllerBypassConfirm") === "1";
    } catch (_err) {
      return false;
    }
  }
  function goBackOrHome() {
    try {
      if (document.referrer && new URL(document.referrer).origin === window.location.origin) {
        window.location.href = document.referrer;
        return;
      }
    } catch (_err) {}
    window.location.href = "/";
  }
  for (const button of document.querySelectorAll("button[data-post]")) {
    button.addEventListener("click", async () => {
      if (button.dataset.confirm && !confirmationBypassed() && !window.confirm(button.dataset.confirm)) return;
      button.disabled = true;
      try {
        const body = button.dataset.body ? JSON.parse(button.dataset.body) : {};
        const response = await fetch(button.dataset.post, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
          cache: "no-store"
        });
        if (button.dataset.redirect === "back" && response.ok) {
          goBackOrHome();
          return;
        }
        if (button.dataset.noReload) return;
        window.location.reload();
      } finally {
        button.disabled = false;
      }
    });
  }
})();
</script>
"""


def live_refresh_script(baseline: dict[str, Any]) -> str:
    import json

    baseline_json = json.dumps(baseline, sort_keys=True)
    return f"""
<script>
(() => {{
  const baseline = {baseline_json};
  const money = value => `$${{Number(value || 0).toFixed(6)}}`;
  const rate = value => `$${{Number(value || 0).toFixed(6)}}/hr`;
  const byId = rows => Object.fromEntries(rows.map(row => [row.id, row]));
  const pillOk = ["running","won","succeeded","hydrated","passed","configured","healthy","verified","complete","completed","downloaded","moved","already_present","accepted","active"];
  const pillNeutral = ["deleted","reclaimed","stopped","closed","terminated","finalized","exited","skipped","none","terminal"];
  function pillClass(value) {{
    const text = String(value || "").toLowerCase();
    if (!text) return "pill-neutral";
    if (text.includes("fail") || text.includes("error") || ["cancelled","missing","interrupted"].includes(text)) return "pill-danger";
    if (text.endsWith("_ready") || pillOk.includes(text)) return "pill-ok";
    if (pillNeutral.includes(text)) return "pill-neutral";
    return "pill-warn";
  }}
  function fmtTime(value) {{
    const raw = String(value || "");
    if (!/^\\d{{4}}-\\d{{2}}-\\d{{2}}T\\d{{2}}:\\d{{2}}:\\d{{2}}/.test(raw)) return raw;
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return raw;
    const pad = n => String(n).padStart(2, "0");
    return `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}} ${{pad(date.getHours())}}:${{pad(date.getMinutes())}}:${{pad(date.getSeconds())}}`;
  }}
  function renderCell(column, value) {{
    if (column === "estimated_cost_usd" || column === "actual_cost_usd" || column === "effective_cost_usd") return money(value);
    if (column === "rate_usd_per_hr") return rate(value);
    if (column === "provider_mode" && value === "fake") return "FAKE";
    if (column.endsWith("_at") || column.endsWith("deadline") || column.endsWith("until")) return fmtTime(value) || "—";
    return value === null || value === undefined ? "" : String(value);
  }}
  function updateRows(rows) {{
    for (const row of rows) {{
      const container = document.querySelector(`[data-row-id="${{CSS.escape(row.id)}}"]`);
      if (!container) continue;
      for (const cell of container.querySelectorAll("[data-column]")) {{
        const column = cell.dataset.column;
        if (!(column in row)) continue;
        const next = renderCell(column, row[column]);
        if (cell.dataset.pill) {{
          if (cell.textContent !== next) {{
            const span = document.createElement("span");
            span.className = `pill ${{pillClass(next)}}`;
            span.textContent = next;
            cell.replaceChildren(span);
          }}
        }} else if (cell.textContent !== next) {{
          cell.textContent = next;
        }}
      }}
    }}
  }}
  async function refreshCosts() {{
    try {{
      const response = await fetch("/api/v1/reports/costs", {{ cache: "no-store" }});
      if (!response.ok) return;
      const data = await response.json();
      const summary = data.summary || {{}};
      for (const el of document.querySelectorAll("[data-summary-key]")) {{
        const key = el.dataset.summaryKey;
        if (!(key in summary)) continue;
        const next = el.dataset.format === "money" ? money(summary[key]) : String(summary[key]);
        if (el.textContent !== next) el.textContent = next;
      }}
      const shapeChanged = ["active_sessions", "active_pods", "active_volumes", "sessions", "pods", "volumes"]
        .some(key => Number(summary[key] || 0) !== Number(baseline[key] || 0));
      if (shapeChanged) {{
        window.location.reload();
        return;
      }}
      updateRows([...(data.sessions || []), ...(data.pods || []), ...(data.volumes || [])]);
    }} catch (_err) {{}}
  }}
  setInterval(refreshCosts, 1000);
  refreshCosts();
}})();
</script>
"""


def workflow_refresh_script(session_id: str) -> str:
    return f"""
<script>
(() => {{
  const pillOk = ["running","won","succeeded","hydrated","passed","configured","healthy","verified","complete","completed","downloaded","moved","already_present","accepted","active"];
  const pillNeutral = ["deleted","reclaimed","stopped","closed","terminated","finalized","exited","skipped","none","terminal"];
  function pillClass(value) {{
    const text = String(value || "").toLowerCase();
    if (!text) return "pill-neutral";
    if (text.includes("fail") || text.includes("error") || ["cancelled","missing","interrupted"].includes(text)) return "pill-danger";
    if (text.endsWith("_ready") || pillOk.includes(text)) return "pill-ok";
    if (pillNeutral.includes(text)) return "pill-neutral";
    return "pill-warn";
  }}
  function humanBytes(value) {{
    let size = Number(value || 0);
    if (!Number.isFinite(size) || size <= 0) return "0 B";
    for (const unit of ["B", "KB", "MB", "GB", "TB"]) {{
      if (size < 1024 || unit === "TB") return unit === "B" ? `${{Math.round(size)}} B` : `${{size.toFixed(1)}} ${{unit}}`;
      size /= 1024;
    }}
    return "0 B";
  }}
  async function refreshWorkflow() {{
    try {{
      const response = await fetch("/api/v1/sessions/{html.escape(session_id)}", {{cache: "no-store"}});
      if (!response.ok) return;
      const session = await response.json();
      const workflow = session.workflow || {{}};
      const phase = document.querySelector("[data-workflow-phase]");
      if (phase) phase.textContent = session.phase || "";
      const state = document.querySelector("[data-workflow-state]");
      if (state) state.textContent = workflow.state || session.state || "";
      for (const candidate of (workflow.candidates || [])) {{
        const row = document.querySelector(`[data-candidate-id="${{CSS.escape(candidate.id)}}"]`);
        if (!row) continue;
        const stateEl = row.querySelector("[data-candidate-state]");
        const nextState = candidate.state || "";
        if (stateEl && stateEl.textContent.trim() !== nextState) {{
          const span = document.createElement("span");
          span.className = `pill ${{pillClass(nextState)}}`;
          span.textContent = nextState;
          stateEl.replaceChildren(span);
        }}
        const total = Number(candidate.download_total_bytes || 0);
        const done = Number(candidate.download_done_bytes || 0);
        const bar = row.querySelector("[data-candidate-progress]");
        const label = row.querySelector("[data-candidate-progress-label]");
        if (bar && total > 0) bar.value = Math.min(100, Math.round(done / total * 100));
        if (label && total > 0) {{
          const next = `${{humanBytes(done)}} / ${{humanBytes(total)}}`;
          if (label.textContent !== next) label.textContent = next;
        }}
      }}
    }} catch (_err) {{}}
  }}
  setInterval(refreshWorkflow, 1000);
  refreshWorkflow();
}})();
</script>
"""
