"""Display-layer localization for the web UI.

Storage, the JSON API, and log/audit text stay English; only rendered HTML and
the strings the inline scripts show to the user are translated. The locale is
held in a context variable so the threaded HTTP server can localize per
request without threading a parameter through every render function.

English text doubles as the message key: ``t("Active sessions")`` returns the
Chinese translation when the request locale is ``zh`` and the input unchanged
otherwise, so untranslated strings degrade gracefully to English.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

DEFAULT_LOCALE = "en"

_locale: contextvars.ContextVar[str] = contextvars.ContextVar("locale", default=DEFAULT_LOCALE)


def normalize_locale(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "zh" if text.startswith("zh") else "en"


def detect_locale(accept_language: str, override: str = "") -> str:
    """Pick zh/en from an Accept-Language header, with an explicit override.

    The first language range whose primary tag we support wins; anything else
    falls back to English (the requested default)."""
    if override:
        return normalize_locale(override)
    for part in str(accept_language or "").split(","):
        tag = part.split(";")[0].strip().lower()
        if not tag:
            continue
        if tag.startswith("zh"):
            return "zh"
        if tag.startswith("en"):
            return "en"
    return DEFAULT_LOCALE


def set_locale(value: Any) -> None:
    _locale.set(normalize_locale(value))


def get_locale() -> str:
    return _locale.get()


def t(text: str) -> str:
    if _locale.get() == "zh":
        return ZH.get(text, text)
    return text


def t_column(column: str) -> str:
    """Short human label for a raw column name, localized."""
    label = COLUMN_LABELS.get(column)
    if label:
        return label[1] if _locale.get() == "zh" else label[0]
    return column.replace("_", " ")


def js_translation_bundle() -> str:
    """Inline script exposing the translation map to client-side code."""
    payload = json.dumps(ZH, ensure_ascii=False) if _locale.get() == "zh" else "{}"
    return (
        "<script>"
        f"window.__LANG = {json.dumps(_locale.get())};"
        f"window.__T_MAP = {payload};"
        "window.__T = s => (window.__T_MAP && window.__T_MAP[s]) || s;"
        "</script>"
    )


# Raw column name -> (short English label, Chinese label).
COLUMN_LABELS: dict[str, tuple[str, str]] = {
    "id": ("ID", "ID"),
    "key": ("Key", "键"),
    "value": ("Value", "值"),
    "state": ("State", "状态"),
    "phase": ("Phase", "阶段"),
    "product": ("Product", "产品"),
    "mode": ("Mode", "模式"),
    "elapsed": ("Elapsed", "持续时间"),
    "aggregate_pod_runtime": ("Pod runtime", "Pod 累计时长"),
    "effective_cost_usd": ("Cost", "费用"),
    "estimated_cost_usd": ("Estimated", "估算费用"),
    "actual_cost_usd": ("Billed", "账单费用"),
    "cost_source": ("Cost source", "费用来源"),
    "rate_usd_per_hr": ("Rate", "费率"),
    "quoted_cost_usd_per_hr": ("Quoted rate", "报价费率"),
    "gpu_attempts": ("GPU attempts", "GPU 尝试"),
    "tunnel_status": ("Tunnel", "隧道"),
    "created_at": ("Created", "创建时间"),
    "updated_at": ("Updated", "更新时间"),
    "started_at": ("Started", "开始时间"),
    "finished_at": ("Finished", "结束时间"),
    "completed_at": ("Completed", "完成时间"),
    "deleted_at": ("Deleted", "删除时间"),
    "stopped_at": ("Stopped", "停止时间"),
    "candidate_count": ("Candidates", "候选数"),
    "idle_deadline": ("Idle deadline", "空闲截止"),
    "local_ui_url": ("UI URL", "界面地址"),
    "ui_url": ("UI URL", "界面地址"),
    "data_center_id": ("Datacenter", "机房"),
    "event_type": ("Event", "事件"),
    "message": ("Message", "消息"),
    "candidate_id": ("Candidate", "候选"),
    "attempt_number": ("Attempt", "尝试序号"),
    "gpu_type_id": ("GPU type", "GPU 型号"),
    "quote_source": ("Quote source", "报价来源"),
    "provider_pod_id": ("Provider Pod", "供应商 Pod"),
    "provider_volume_id": ("Provider volume", "供应商卷"),
    "provider_mode": ("Provider", "供应商"),
    "error": ("Error", "错误"),
    "last_error": ("Last error", "最近错误"),
    "backoff_seconds": ("Backoff (s)", "退避（秒）"),
    "protocol": ("Protocol", "协议"),
    "remote_host": ("Remote host", "远端主机"),
    "remote_port": ("Remote port", "远端端口"),
    "local_host": ("Local host", "本地主机"),
    "local_port": ("Local port", "本地端口"),
    "local_url": ("Local URL", "本地地址"),
    "restart_count": ("Restarts", "重启次数"),
    "auto_recover": ("Auto recover", "自动恢复"),
    "last_health_check_at": ("Last health check", "最近健康检查"),
    "reason": ("Reason", "原因"),
    "role": ("Role", "角色"),
    "compute_type": ("Compute", "计算类型"),
    "effective_start_at": ("Start", "开始"),
    "effective_stop_at": ("Stop", "停止"),
    "runtime": ("Runtime", "运行时长"),
    "size_gb": ("Size (GB)", "容量 (GB)"),
    "workflow_ref": ("Workflow ref", "工作流引用"),
    "prompt": ("Prompt", "提示词"),
    "external_id": ("External ID", "外部 ID"),
    "kind": ("Kind", "类型"),
    "local_path": ("Local path", "本地路径"),
    "remote_uri": ("Remote URI", "远端 URI"),
    "checksum_sha256": ("Checksum", "校验和"),
    "size_bytes": ("Size", "大小"),
    "file_count": ("Files", "文件数"),
    "byte_count": ("Bytes", "字节数"),
    "downloaded_count": ("Downloaded", "已下载"),
    "skipped_count": ("Skipped", "已跳过"),
    "volume_delete_allowed": ("Delete allowed", "允许删卷"),
    "operation_type": ("Operation", "操作"),
    "model_folder": ("Folder", "目录"),
    "filename": ("Filename", "文件名"),
    "source_url_redacted": ("Source URL", "来源链接"),
    "source_path": ("Source path", "源路径"),
    "target_path": ("Target path", "目标路径"),
    "volume_id": ("Volume", "卷"),
    "cpu_pod_id": ("CPU Pod", "CPU Pod"),
    "gpu_pod_id": ("GPU Pod", "GPU Pod"),
    "download_done_bytes": ("Downloaded", "已下载"),
    "download_total_bytes": ("Total", "总量"),
    "attempt_count": ("Attempts", "尝试次数"),
    "cleanup_status": ("Cleanup", "清理状态"),
    "comfyui_workflow_id": ("ComfyUI workflow", "ComfyUI 工作流"),
    "winner_candidate_id": ("Winner", "胜出候选"),
    "volume_size_gb": ("Volume (GB)", "卷容量 (GB)"),
    "max_gpu_usd_per_hr": ("Max $/hr", "费率上限"),
    "max_total_usd": ("Cost cap", "费用上限"),
    "session_id": ("Session", "会话"),
    "package": ("Package", "包"),
    "repo_url": ("Repository", "仓库"),
    "ref": ("Ref", "提交"),
    "requested_ref": ("Requested ref", "请求提交"),
    "locked_ref": ("Locked ref", "锁定提交"),
    "source": ("Source", "来源"),
    "node_types": ("Node types", "节点类型"),
    "class_type": ("Node type", "节点类型"),
    "warning": ("Warning", "警告"),
    "check": ("Check", "检查项"),
    "decision": ("Decision", "决策"),
    "install_method": ("Install method", "安装方式"),
    "target_dir": ("Target dir", "目标目录"),
    "run_requirements": ("Run requirements", "安装依赖"),
    "node": ("Node", "节点"),
    "record_key": ("Record key", "记录键"),
}


# English display text -> Chinese. English text is the key.
ZH: dict[str, str] = {
    # Navigation / shell
    "Active": "活跃",
    "History": "历史",
    "Capabilities": "能力",
    # Dashboard
    "Active sessions": "活跃会话",
    "Current compute, storage, tunnel, and watchdog state.": "当前的计算、存储、隧道与看门狗状态。",
    "Create ComfyUI": "创建 ComfyUI",
    "Active Pods": "活跃 Pod",
    "Active volumes": "活跃卷",
    "Spend": "花费",
    "Compute spend": "计算花费",
    "Storage spend": "存储花费",
    "No active sessions": "没有活跃会话",
    "Launch a workflow-first ComfyUI session to get started.": "启动一个工作流优先的 ComfyUI 会话开始使用。",
    "Open ComfyUI": "打开 ComfyUI",
    "Overview": "总览",
    "Workflow": "工作流",
    "Phase": "阶段",
    "Elapsed": "持续时间",
    "Pod runtime": "Pod 累计时长",
    "Idle deadline": "空闲截止",
    "Cost cap": "费用上限",
    "Candidates": "候选",
    "Tunnel": "隧道",
    # History
    "Recent terminal sessions. Full pagination and filters can be added later.": "最近的已结束会话。完整分页与筛选可后续添加。",
    # Session header / tabs
    "Models": "模型",
    "Outputs": "输出",
    "Debug": "调试",
    "Session": "会话",
    "Mode": "模式",
    "Datacenter": "机房",
    "Shutdown: stop GPU + collect outputs": "关闭：停止 GPU 并采集输出",
    "Open output folder": "打开输出目录",
    # Overview
    "Controls": "操作",
    "Promote GPU": "提升到 GPU",
    "Restart tunnel": "重启隧道",
    "Collect outputs": "采集输出",
    "+30m lease": "续租 30 分钟",
    "+60m lease": "续租 60 分钟",
    "Pause watchdog": "暂停看门狗",
    "Resume watchdog": "恢复看门狗",
    "Output files": "输出文件",
    "Product": "产品",
    "GPU intent": "GPU 意图",
    "Lease until": "租约截止",
    "Idle shutdown at": "空闲关闭时间",
    "Watchdog reason": "看门狗原因",
    "UI URL": "界面地址",
    "Retention policy": "保留策略",
    "Created": "创建时间",
    "Updated": "更新时间",
    "Open workflow": "打开工作流",
    "No workflow attached to this session.": "此会话没有关联的工作流。",
    "Candidate Plan": "候选计划",
    "Workflow Events": "工作流事件",
    "State": "状态",
    "ComfyUI workflow": "ComfyUI 工作流",
    "Winner candidate": "胜出候选",
    "Volume": "卷",
    "Max GPU rate": "GPU 费率上限",
    "Max total": "总额上限",
    "Completed": "完成时间",
    # Candidate plan
    "No candidates.": "没有候选。",
    "no download": "无下载",
    "attempt": "次尝试",
    "attempts": "次尝试",
    "Candidate": "候选",
    "CPU Pod": "CPU Pod",
    "GPU Pod": "GPU Pod",
    "Cleanup": "清理状态",
    "Downloaded": "已下载",
    "cleanup evidence:": "清理证据：",
    # Workflow page
    "Workflow state": "工作流状态",
    "Volume GB": "卷容量 GB",
    "Workflow actions": "工作流操作",
    "Mark workflow verified": "标记工作流已验证",
    "Terminate: stop GPU + collect outputs": "终止：停止 GPU 并采集输出",
    "Candidate progress": "候选进度",
    "Events": "事件",
    "Workflow Lock": "工作流锁定",
    "No workflow lock metadata.": "没有工作流锁定元数据。",
    "Workflow Analysis": "工作流分析",
    "No unresolved custom nodes.": "没有未解析的自定义节点。",
    "No analyzer warnings.": "没有分析警告。",
    "Dependency Probe": "依赖探测",
    "No CPU probe result.": "没有 CPU 探测结果。",
    "Install Plan": "安装计划",
    "No custom-node install steps.": "没有自定义节点安装步骤。",
    "Validation Plan": "验证计划",
    "No validation checks recorded.": "没有记录验证检查。",
    # Models tab
    "Refresh tree": "刷新模型树",
    "Add runtime model / LoRA URL": "添加运行时模型 / LoRA 链接",
    "ComfyUI folder": "ComfyUI 目录",
    "Filename override": "文件名覆盖",
    "Add download": "添加下载",
    "Runtime model operations": "运行时模型操作",
    "Model tree": "模型树",
    "Click Refresh tree to load current files from the running Pod.": "点击「刷新模型树」从运行中的 Pod 加载当前文件。",
    "Terminal sessions are read-only; runtime model download and move controls are hidden.": "已结束会话为只读；运行时模型下载与移动控件已隐藏。",
    "Network volume": "网络卷",
    "Current size": "当前容量",
    "Grow to (GB)": "扩容至 (GB)",
    "Grow volume": "扩容",
    "Resize is grow-only. The controller verifies the running Pod actually sees the new size with": "扩容只增不减。controller 会用以下命令验证运行中的 Pod 真正看到新容量：",
    # Outputs tab
    "Collected bytes": "已采集字节",
    "Collection state": "采集状态",
    "Volume retained": "卷已保留",
    "Collection": "采集",
    "Outputs are copied from the network volume through the RunPod S3-compatible API into the local artifacts directory. The volume is only deleted after a successful final collection.": "输出会通过 RunPod 兼容 S3 的 API 从网络卷复制到本地产物目录。只有最终采集成功后才会删除网络卷。",
    "Collect outputs now": "立即采集输出",
    "Open local folder": "打开本地目录",
    "Discard outputs + delete volume": "丢弃输出并删除卷",
    "No collection actions available for this session state.": "当前会话状态没有可用的采集操作。",
    "Last checked:": "最近检查：",
    "Last error:": "最近错误：",
    "Collection runs": "采集记录",
    "Collected files": "已采集文件",
    # Debug tab
    "Session row": "会话原始数据",
    "GPU Acquisition Attempts": "GPU 获取尝试",
    "Watchdog": "看门狗",
    "Pods": "Pod",
    "Tasks": "任务",
    "Artifacts": "产物",
    "Audit": "审计",
    # Confirm dialogs
    "Shutdown session": "关闭会话",
    "This stops GPU compute first, collects ComfyUI outputs through S3, then deletes the network volume only after collection succeeds.": "将先停止 GPU 计算，通过 S3 采集 ComfyUI 输出，只有采集成功后才删除网络卷。",
    "Discard uncollected outputs and delete the retained network volume for session": "丢弃未采集的输出并删除保留的网络卷，会话",
    "This cannot be undone.": "此操作不可撤销。",
    "Terminate this workflow? GPU compute stops first; outputs are collected before the network volume can be deleted.": "终止此工作流？将先停止 GPU 计算；输出采集完成后网络卷才可能被删除。",
    "Delete this workflow record? Active sessions are protected.": "删除此工作流记录？活跃会话会受到保护。",
    # Generic
    "No rows.": "暂无数据",
    "yes": "是",
    "no": "否",
    # Wizard
    "New ComfyUI Session": "新建 ComfyUI 会话",
    "Workflow first: upload JSON, resolve nodes, size models, probe dependencies, then launch.": "工作流优先：上传 JSON、解析节点、确定模型大小、探测依赖，然后启动。",
    "Close": "关闭",
    "Upload a ComfyUI workflow JSON to start, or pick one from the library.": "上传 ComfyUI 工作流 JSON 开始，或从库中选择一个。",
    "Nodes": "节点",
    "Probe / Lock": "探测 / 锁定",
    "Launch": "启动",
    "Step": "步骤",
    "Workflow library": "工作流库",
    "No workflow": "无工作流",
    "Workflow name": "工作流名称",
    "Rename": "重命名",
    "Use selected workflow": "使用所选工作流",
    "Re-analyze": "重新分析",
    "Copy missing-node summary": "复制缺失节点摘要",
    "Delete": "删除",
    "Upload UI workflow JSON": "上传 UI 工作流 JSON",
    "Upload workflow": "上传工作流",
    "Registry matches are suggestions until accepted and locked. Unresolved executable nodes block probe and launch.": "Registry 匹配只是建议，需接受并锁定。未解析的可执行节点会阻止探测和启动。",
    "Needs decision": "待决策",
    "Resolve all with URLs": "解析所有带链接的节点",
    "Locks every unresolved node row that already has a Git repo URL. Rows without URLs stay here for manual input.": "锁定所有已有 Git 仓库链接的未解析节点。没有链接的行保留在此手动填写。",
    "Resolving nodes": "正在解析节点",
    "Preparing...": "准备中...",
    "Resolved custom nodes": "已解析的自定义节点",
    "Built-in / assumed core": "内置 / 视为核心",
    "Workflow-extracted model requirements come first. Extra models can be added below.": "工作流提取的模型需求排在前面。可在下方添加额外模型。",
    "Save all model rows": "保存所有模型行",
    "Add extra model": "添加额外模型",
    "Add": "添加",
    "Refresh metadata": "刷新元数据",
    "The CPU dependency probe is optional here: if it has not run when you launch, the controller runs it automatically before creating paid resources.": "CPU 依赖探测在此为可选：如果启动时尚未运行，controller 会在创建付费资源前自动运行。",
    "Run dependency probe": "运行依赖探测",
    "Lease minutes": "租约分钟数",
    "Max $/hr": "费率上限 $/时",
    "Max total $": "总额上限 $",
    "Min VRAM GB": "最小显存 GB",
    "GPU vendor": "GPU 厂商",
    "Location and GPU candidates are planned automatically during workflow startup.": "机房与 GPU 候选会在工作流启动时自动规划。",
    "Dry run": "试算",
    "Auto-sized from model metadata: max(10GB, ceil(total bytes × 1.20) + 5GB). The computed value is a minimum — you can raise it but not go below. It recalculates whenever model URLs gain metadata.": "根据模型元数据自动计算：max(10GB, ceil(总字节 × 1.20) + 5GB)。计算值是下限——可以调大但不能低于它。模型链接获得元数据后会自动重算。",
    # Wizard JS statuses
    "Upload or select a workflow first.": "请先上传或选择一个工作流。",
    "Select a workflow from the library first.": "请先从库中选择一个工作流。",
    "Workflow ready. Review budget, dry run, then create.": "工作流就绪。检查预算、试算，然后创建。",
    "Workflow selected. Fill model URLs and sizes next.": "已选择工作流。接下来填写模型链接与大小。",
    "Workflow selected. Resolve node decisions next.": "已选择工作流。接下来处理节点决策。",
    "Choose a workflow JSON file first.": "请先选择一个工作流 JSON 文件。",
    "Uploading workflow...": "正在上传工作流...",
    "Workflow uploaded": "工作流已上传",
    "Export package": "导出分享包",
    "Upload workflow JSON or shared package (.zip)": "上传工作流 JSON 或分享包（.zip）",
    "Importing workflow package...": "正在导入分享包...",
    "Workflow package imported": "分享包已导入",
    "Workflow package downloading": "分享包开始下载",
    "Workflow renamed": "工作流已重命名",
    "Workflow deleted": "工作流已删除",
    "Workflow analyzed": "工作流已分析",
    "Missing-node summary copied": "缺失节点摘要已复制",
    "Git repo URL is required for this node.": "此节点需要 Git 仓库链接。",
    "Resolving node lock...": "正在解析节点锁定...",
    "Node lock saved": "节点锁定已保存",
    "Already added. Remove the existing row first.": "已添加过。请先移除已有的行。",
    "Saving row and peeking metadata...": "正在保存并探测元数据...",
    "Saved row from metadata cache": "已保存（命中元数据缓存）",
    "Saved row and metadata.": "已保存行与元数据。",
    "Saved all model rows and metadata.": "已保存全部模型行与元数据。",
    "Model row removed.": "模型行已移除。",
    "Refreshing metadata...": "正在刷新元数据...",
    "Peeking metadata...": "正在探测元数据...",
    "Added from cache": "已添加（来自缓存）",
    "Added": "已添加",
    "Running dependency probe...": "正在运行依赖探测...",
    "Probe cache hit": "探测缓存命中",
    "Probe complete": "探测完成",
    "Creating ComfyUI...": "正在创建 ComfyUI...",
    "no": "否",
    "Resolving": "正在解析",
    # Wizard nav
    "Continue:": "继续：",
    "Optional: launch runs the probe automatically if it has not passed yet.": "可选：若探测尚未通过，启动时会自动运行。",
    "node decision(s) still needed.": "个节点决策待处理。",
    "model asset(s) still need URL or metadata.": "个模型资产仍缺链接或元数据。",
    "Some": "部分",
    # Wizard client-rendered table headers (raw column keys)
    "package": "包",
    "repo_url": "仓库",
    "ref": "提交",
    "source": "来源",
    "node_types": "节点类型",
    "node": "节点",
    "decision": "决策",
    "requested_ref": "请求提交",
    "locked_ref": "锁定提交",
    # Wizard client-rendered
    "No workflow selected.": "未选择工作流。",
    "verified": "已验证",
    "Create request failed": "创建请求失败",
    "Hash": "哈希",
    "Verification": "验证状态",
    "Warnings": "警告",
    "resolved packages": "个已解析包",
    "needs decision": "个待决策",
    "built-in / assumed": "个内置 / 视为核心",
    "Built-in / assumed": "内置 / 视为核心",
    "Upload a workflow first.": "请先上传工作流。",
    "No resolved custom nodes.": "没有已解析的自定义节点。",
    "No built-in nodes recorded.": "没有记录内置节点。",
    "Registry suggestion:": "Registry 建议：",
    "No accepted Registry suggestion yet.": "尚未接受任何 Registry 建议。",
    "Use Registry result": "使用 Registry 结果",
    "Treat as built-in": "视为内置节点",
    "Git repo URL": "Git 仓库链接",
    "tag / branch / commit optional": "标签 / 分支 / 提交（可选）",
    "Resolve and lock Git repo": "解析并锁定 Git 仓库",
    "No active rows.": "暂无条目。",
    "filename": "文件名",
    "folder": "目录",
    "size": "大小",
    "status": "状态",
    "Save": "保存",
    "Remove": "移除",
    "model URL": "模型链接",
    "needs_url": "缺少链接",
    "ready": "就绪",
    "metadata_unknown": "大小未知",
    "needs_metadata": "缺少元数据",
    "metadata unknown": "大小未知",
    "metadata needed": "需要元数据",
    "pending URL": "等待链接",
    "Workflow-extracted models": "工作流提取的模型",
    "Extra models": "额外模型",
    "workflow hash": "工作流哈希",
    "dependency fingerprint": "依赖指纹",
    "launch fingerprint": "启动指纹",
    "base template": "基础模板",
    "probe": "探测",
    "cached": "（缓存）",
    "not run": "未运行",
    "Node locks": "节点锁定",
    "No custom-node locks.": "没有自定义节点锁定。",
    "Probe": "探测",
    "Probe failed:": "探测失败：",
    "Dry run failed:": "试算失败：",
    "Dry run result": "试算结果",
    "Datacenters": "机房数",
    "GPU rows": "GPU 行",
    "Stock rows": "有库存行",
    "datacenter": "机房",
    "eligible GPU rows": "符合条件的 GPU",
    "No matching GPU rows.": "没有匹配的 GPU。",
    "No resources created. Empty datacenters are hidden.": "未创建任何资源。空机房已隐藏。",
    "Saved row. Metadata is still needed.": "已保存行。仍需元数据。",
    "Saved row.": "已保存行。",
    "file(s)": "个文件",
    "Move to ComfyUI folder, e.g. loras / checkpoints / vae": "移动到 ComfyUI 目录，例如 loras / checkpoints / vae",
    "Filename": "文件名",
    "Moving...": "正在移动...",
    "Moved:": "已移动：",
    # Model manager JS
    "Refreshing tree...": "正在刷新模型树...",
    "No model files found.": "未找到模型文件。",
    "URL is required.": "链接为必填。",
    "Move": "移动",
    "Working...": "处理中...",
    "Working": "处理中",
    "Growing volume...": "正在扩容...",
    "Enter a target size in GB.": "请输入目标容量（GB）。",
    "Loaded": "已加载",
    "model row(s).": "条模型记录。",
}
