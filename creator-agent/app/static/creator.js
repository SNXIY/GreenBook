const AUTH_KEY = "mindflow.creator.auth";
const AUTO_LOGIN_SUPPRESS_KEY = "mindflow.creator.auto-login-suppressed";
const ACTIVE_TASK_KEY = "mindflow.creator.activeTask";
const API_ROOT = "/api/v1/creator";

function importGreenBookToken() {
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const token = fragment.get("zhiguang_token");
  if (!token) return;
  sessionStorage.setItem(AUTH_KEY, JSON.stringify({
    token,
    scheme: "Bearer",
    source: "zhiguang"
  }));
  history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
}

importGreenBookToken();

const state = {
  apiStatus: null,
  tasks: [],
  projects: [],
  materials: [],
  feedbackSummary: null,
  nextCursor: null,
  taskFilter: "",
  projectFilterId: "",
  selectedTaskId: null,
  snapshot: null,
  artifacts: [],
  artifactDetails: new Map(),
  selectedArtifactId: null,
  drafts: [],
  selectedDraft: null,
  draftVersions: [],
  suggestions: [],
  currentSuggestion: null,
  branches: [],
  channelVariants: [],
  outputTab: "artifacts",
  outputText: "",
  outputFilename: "mindflow-creator.md",
  streamController: null,
  streamGeneration: 0,
  lastEventIds: new Map(),
  events: [],
  refreshTimer: null,
  editorSourceArtifactId: null,
  documentEditor: null,
  documentSourceKey: null,
  documentSourceArtifactId: null,
  documentRequestedArtifactId: null,
  documentTitle: "",
  editorDirty: false,
  editorSelectedText: "",
  editorSelectionContext: { prefixContext: "", suffixContext: "" },
  libraryView: "works",
  assistantTab: "collaboration",
  workSearch: "",
  lastDecisionId: null
};

const els = {
  apiState: document.querySelector("#apiState"),
  activeCreator: document.querySelector("#activeCreator"),
  creatorScope: document.querySelector("#creatorScope"),
  logoutButton: document.querySelector("#logoutButton"),
  topDocumentLabel: document.querySelector("#topDocumentLabel"),
  topSaveState: document.querySelector("#topSaveState"),
  mobileSidebarToggle: document.querySelector("#mobileSidebarToggle"),
  openCreateTask: document.querySelector("#openCreateTask"),
  workSearch: document.querySelector("#workSearch"),
  activeProjectFilter: document.querySelector("#activeProjectFilter"),
  worksLibrary: document.querySelector("#worksLibrary"),
  projectsLibrary: document.querySelector("#projectsLibrary"),
  materialsLibrary: document.querySelector("#materialsLibrary"),
  openCreateProject: document.querySelector("#openCreateProject"),
  openCreateMaterial: document.querySelector("#openCreateMaterial"),
  projectList: document.querySelector("#projectList"),
  allProjectCount: document.querySelector("#allProjectCount"),
  pendingProjectCount: document.querySelector("#pendingProjectCount"),
  completedProjectCount: document.querySelector("#completedProjectCount"),
  materialLibrary: document.querySelector("#materialLibrary"),
  taskList: document.querySelector("#taskList"),
  loadMoreTasks: document.querySelector("#loadMoreTasks"),
  workflowEmpty: document.querySelector("#workflowEmpty"),
  workflowContent: document.querySelector("#workflowContent"),
  taskKind: document.querySelector("#taskKind"),
  taskTrace: document.querySelector("#taskTrace"),
  taskBriefSummary: document.querySelector("#taskBriefSummary"),
  taskBriefBar: document.querySelector("#taskBriefBar"),
  taskGoal: document.querySelector("#taskGoal"),
  taskStatus: document.querySelector("#taskStatus"),
  continuePublishing: document.querySelector("#continuePublishing"),
  cancelTask: document.querySelector("#cancelTask"),
  retryTask: document.querySelector("#retryTask"),
  workflowStages: document.querySelector("#workflowStages"),
  documentContextBanner: document.querySelector("#documentContextBanner"),
  documentContextTitle: document.querySelector("#documentContextTitle"),
  documentContextText: document.querySelector("#documentContextText"),
  openAssistantPane: document.querySelector("#openAssistantPane"),
  editorToolbar: document.querySelector("#editorToolbar"),
  documentTitleInput: document.querySelector("#documentTitleInput"),
  documentEditor: document.querySelector("#documentEditor"),
  documentPlaceholder: document.querySelector("#documentPlaceholder"),
  editorWordCount: document.querySelector("#editorWordCount"),
  editorSaveState: document.querySelector("#editorSaveState"),
  editorSourceLabel: document.querySelector("#editorSourceLabel"),
  saveDocumentVersion: document.querySelector("#saveDocumentVersion"),
  decisionSurface: document.querySelector("#decisionSurface"),
  assistantStatusText: document.querySelector("#assistantStatusText"),
  closeAssistantPane: document.querySelector("#closeAssistantPane"),
  selectionAssistant: document.querySelector("#selectionAssistant"),
  selectedTextPreview: document.querySelector("#selectedTextPreview"),
  aiSuggestionPanel: document.querySelector("#aiSuggestionPanel"),
  editorAssistant: document.querySelector("#editorAssistant"),
  editorInstruction: document.querySelector("#editorInstruction"),
  submitEditorInstruction: document.querySelector("#submitEditorInstruction"),
  sourcePanel: document.querySelector("#sourcePanel"),
  qualityPanel: document.querySelector("#qualityPanel"),
  streamState: document.querySelector("#streamState"),
  eventTimeline: document.querySelector("#eventTimeline"),
  outputTitle: document.querySelector("#outputTitle"),
  outputBody: document.querySelector("#outputBody"),
  copyOutput: document.querySelector("#copyOutput"),
  downloadOutput: document.querySelector("#downloadOutput"),
  createTaskDialog: document.querySelector("#createTaskDialog"),
  createTaskForm: document.querySelector("#createTaskForm"),
  taskGoalInput: document.querySelector("#taskGoalInput"),
  taskProject: document.querySelector("#taskProject"),
  taskMaterialPicker: document.querySelector("#taskMaterialPicker"),
  taskAudience: document.querySelector("#taskAudience"),
  taskTakeaway: document.querySelector("#taskTakeaway"),
  taskLanguage: document.querySelector("#taskLanguage"),
  taskFormat: document.querySelector("#taskFormat"),
  taskTone: document.querySelector("#taskTone"),
  taskLength: document.querySelector("#taskLength"),
  taskKeyPoints: document.querySelector("#taskKeyPoints"),
  taskReferences: document.querySelector("#taskReferences"),
  includeHistory: document.querySelector("#includeHistory"),
  includeCommunity: document.querySelector("#includeCommunity"),
  createTaskSubmit: document.querySelector("#createTaskSubmit"),
  draftDialog: document.querySelector("#draftDialog"),
  draftForm: document.querySelector("#draftForm"),
  draftDialogTitle: document.querySelector("#draftDialogTitle"),
  draftTitleInput: document.querySelector("#draftTitleInput"),
  draftBodyInput: document.querySelector("#draftBodyInput"),
  draftVersionLabel: document.querySelector("#draftVersionLabel"),
  draftSubmit: document.querySelector("#draftSubmit"),
  projectDialog: document.querySelector("#projectDialog"),
  projectForm: document.querySelector("#projectForm"),
  projectName: document.querySelector("#projectName"),
  projectDescription: document.querySelector("#projectDescription"),
  projectSubmit: document.querySelector("#projectSubmit"),
  materialDialog: document.querySelector("#materialDialog"),
  materialForm: document.querySelector("#materialForm"),
  materialTitle: document.querySelector("#materialTitle"),
  materialProject: document.querySelector("#materialProject"),
  materialKind: document.querySelector("#materialKind"),
  materialSourceUrl: document.querySelector("#materialSourceUrl"),
  materialFile: document.querySelector("#materialFile"),
  materialContent: document.querySelector("#materialContent"),
  materialTags: document.querySelector("#materialTags"),
  materialSubmit: document.querySelector("#materialSubmit"),
  channelDialog: document.querySelector("#channelDialog"),
  channelForm: document.querySelector("#channelForm"),
  channelTarget: document.querySelector("#channelTarget"),
  channelInstruction: document.querySelector("#channelInstruction"),
  channelSubmit: document.querySelector("#channelSubmit"),
  toast: document.querySelector("#toast")
};

const STATUS_LABELS = {
  CREATED: "已创建",
  QUEUED: "排队中",
  RUNNING: "执行中",
  WAITING_HUMAN: "待确认",
  RETRYING: "重试中",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消"
};

const KIND_LABELS = {
  CREATE_CONTENT: "内容创作",
  ANALYZE_CONTENT: "内容分析",
  BUILD_STRATEGY: "内容策略",
  IMPROVE_DRAFT: "优化草稿",
  RESEARCH_TOPIC: "主题研究"
};

const ARTIFACT_LABELS = {
  SOURCE_DRAFT: "源草稿",
  CREATOR_PROFILE: "创作者画像",
  CONTENT_ANALYSIS: "内容分析",
  EVIDENCE_PACK: "研究证据",
  TOPIC_OPTIONS: "选题方案",
  CONTENT_OUTLINE: "文章大纲",
  DRAFT: "正文草稿",
  CRITIQUE: "质量评审",
  EVALUATION_REPORT: "评估报告",
  DECISION_REQUEST: "决策请求",
  HUMAN_DECISION: "人工决策",
  FINAL_CONTENT: "最终内容"
};

const FORMAT_LABELS = {
  ARTICLE: "深度文章",
  POST: "社区长帖",
  THREAD: "系列短帖"
};

const TONE_LABELS = {
  PRACTICAL: "实用直接",
  PROFESSIONAL: "专业严谨",
  CONVERSATIONAL: "自然亲切",
  SHARP: "鲜明有力"
};

const EVENT_LABELS = {
  "task.created": "已创建创作任务",
  "run.started": "开始创作",
  "run.recovered": "恢复创作",
  "plan.created": "已安排下一步",
  "plan.dispatched": "正在处理",
  "agent.completed": "已完成一个创作步骤",
  "artifact.created": "已生成新内容",
  "artifact.finalized": "成稿已生成",
  "decision.requested": "需要你的选择",
  "decision.required": "等待你的选择",
  "decision.submitted": "已收到你的选择",
  "decision.applied": "已应用你的选择",
  "decision.interrupted": "已暂停等待确认",
  "run.finalizing": "正在整理成稿",
  "run.completed": "创作已完成",
  "run.failed": "创作遇到问题"
};

const METRIC_LABELS = {
  agent_task_success_rate: "任务完成度",
  generation_faithfulness: "事实一致性",
  generation_relevance: "主题相关度",
  generation_style_consistency: "表达一致性",
  retrieval_recall_at_k: "素材覆盖率",
  retrieval_precision_at_k: "素材准确率",
  retrieval_mrr: "首条相关素材排名",
  retrieval_ndcg_at_k: "素材排序质量",
  retrieval_acl_safety: "素材权限安全",
  agent_tool_calling_accuracy: "工具使用准确度",
  agent_planning_quality: "创作规划质量"
};

const CAPABILITY_LABELS = {
  LOAD_CREATOR_MEMORY: "读取创作偏好",
  ANALYZE_CONTENT: "分析历史内容",
  RESEARCH_TOPIC: "查找相关素材",
  PLAN_TOPICS: "生成内容方向",
  BUILD_OUTLINE: "搭建文章结构",
  WRITE_DRAFT: "撰写正文",
  REVISE_DRAFT: "优化正文",
  CRITIQUE_CONTENT: "检查内容质量",
  EVALUATE_RUN: "完成质量评估"
};

const EDITOR_LABELS = {
  HUMAN: "我的编辑",
  AI_ASSISTED: "接受 AI 建议",
  BRANCH: "版本分支"
};

const SUGGESTION_LABELS = {
  REWRITE: "优化表达",
  SHORTEN: "精简内容",
  EXPAND: "补充细节",
  CUSTOM: "按要求修改"
};

const CHANNEL_LABELS = {
  ARTICLE: "深度文章",
  COMMUNITY_POST: "社区长帖",
  THREAD: "系列短帖",
  NEWSLETTER: "邮件通讯"
};

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = text;
  return element;
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function readAuth() {
  try {
    return JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    return null;
  }
}

function authHeader() {
  const auth = readAuth();
  if (!auth?.token) {
    window.location.replace("/");
    return "";
  }
  return `${auth.scheme || "Basic"} ${auth.token}`;
}

function idempotencyKey(prefix) {
  const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `${prefix}-${id}`.slice(0, 128);
}

async function api(path, options = {}) {
  const headers = {
    Accept: "application/json",
    Authorization: authHeader(),
    ...(options.headers || {})
  };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    let code = "";
    try {
      const body = await response.json();
      code = body.error?.code || "";
      message = body.error?.message || message;
    } catch {
      const text = await response.text();
      if (text) message = text;
    }
    const error = new Error(message);
    error.code = code;
    error.status = response.status;
    throw error;
  }
  return response;
}

async function apiJson(path, options = {}) {
  const response = await api(path, options);
  return response.json();
}

function setBadge(element, text, tone = "") {
  element.textContent = text;
  element.className = `status-badge${tone ? ` ${tone}` : ""}`;
}

function taskTone(status) {
  if (status === "COMPLETED") return "is-complete";
  if (status === "WAITING_HUMAN" || status === "RETRYING") return "is-waiting";
  if (status === "RUNNING" || status === "QUEUED") return "is-running";
  if (status === "FAILED" || status === "CANCELLED") return "is-error";
  return "";
}

function showToast(message, error = false) {
  els.toast.textContent = message;
  els.toast.className = `toast${error ? " is-error" : ""}`;
  els.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    els.toast.hidden = true;
  }, 4200);
}

async function loadIdentity() {
  state.apiStatus = await apiJson(`${API_ROOT}/status`);
  els.activeCreator.textContent = state.apiStatus.display_name;
  els.creatorScope.textContent = "GREEN-BOOK 账号";
  setBadge(els.apiState, "创作助手已连接", "is-ready");
  els.apiState.removeAttribute("title");
}

async function loadStudioLibrary() {
  const [projects, materials, feedback] = await Promise.all([
    apiJson(`${API_ROOT}/projects`),
    apiJson(`${API_ROOT}/materials`),
    apiJson(`${API_ROOT}/feedback/summary`)
  ]);
  state.projects = projects;
  state.materials = materials;
  state.feedbackSummary = feedback;
  renderProjectLibrary();
  renderActiveProjectFilter();
  renderMaterialLibrary();
  renderTaskLibraryFields();
}

function renderProjectLibrary() {
  clear(els.projectList);
  if (!state.projects.length) {
    const empty = node("div", "library-inline-empty");
    empty.append(
      node("strong", "", "还没有项目"),
      node("span", "", "为长期主题建立一个内容系列")
    );
    const button = node("button", "button button-secondary", "新建项目");
    button.type = "button";
    button.addEventListener("click", () => els.projectDialog.showModal());
    empty.append(button);
    els.projectList.append(empty);
    return;
  }
  for (const project of state.projects) {
    const row = node("button", "project-row");
    row.type = "button";
    row.classList.toggle("is-active", state.projectFilterId === project.id);
    const icon = node("span", "collection-icon");
    const iconNode = node("i");
    iconNode.dataset.lucide = "folder-kanban";
    icon.append(iconNode);
    const body = node("span", "project-row-body");
    body.append(
      node("strong", "", project.name),
      node(
        "small",
        "",
        `${project.task_count} 篇作品 · ${project.material_count} 份素材`
      )
    );
    row.append(icon, body);
    row.addEventListener("click", () => filterTasksByProject(project.id));
    els.projectList.append(row);
  }
  refreshIcons(els.projectList);
}

function renderActiveProjectFilter() {
  clear(els.activeProjectFilter);
  const project = state.projects.find(
    (item) => item.id === state.projectFilterId
  );
  els.activeProjectFilter.hidden = !project;
  if (!project) return;
  const icon = node("i");
  icon.dataset.lucide = "folder-kanban";
  const label = node("span", "", project.name);
  const clearButton = node("button", "icon-button");
  clearButton.type = "button";
  clearButton.title = "显示全部项目";
  clearButton.setAttribute("aria-label", "显示全部项目");
  const closeIcon = node("i");
  closeIcon.dataset.lucide = "x";
  clearButton.append(closeIcon);
  clearButton.addEventListener("click", () => filterTasksByProject(""));
  els.activeProjectFilter.append(icon, label, clearButton);
  refreshIcons(els.activeProjectFilter);
}

async function filterTasksByProject(projectId) {
  state.projectFilterId = projectId;
  state.taskFilter = "";
  document.querySelectorAll(".filter-tab").forEach((tab) => {
    tab.classList.toggle("is-active", !tab.dataset.status);
  });
  renderProjectLibrary();
  renderActiveProjectFilter();
  switchLibraryView("works");
  try {
    await loadTasks(true);
  } catch (error) {
    showToast(`项目读取失败：${error.message}`, true);
  }
}

function renderMaterialLibrary() {
  clear(els.materialLibrary);
  if (!state.materials.length) {
    const empty = node("div", "library-inline-empty");
    empty.append(
      node("strong", "", "还没有素材"),
      node("span", "", "添加笔记、文本或链接摘录")
    );
    const button = node("button", "button button-secondary", "添加素材");
    button.type = "button";
    button.addEventListener("click", () => els.materialDialog.showModal());
    empty.append(button);
    els.materialLibrary.append(empty);
    return;
  }
  const projectNames = new Map(
    state.projects.map((project) => [project.id, project.name])
  );
  for (const material of state.materials) {
    const item = node("article", "material-library-row");
    const icon = node("span", "source-item-icon");
    const iconNode = node("i");
    iconNode.dataset.lucide = material.kind === "LINK" ? "link-2" : "file-text";
    icon.append(iconNode);
    const body = node("span", "source-item-body");
    body.append(node("strong", "", material.title));
    body.append(
      node(
        "small",
        "",
        [
          projectNames.get(material.project_id) || "全局素材",
          `${material.chunk_count} 个片段`
        ].join(" · ")
      )
    );
    if (material.tags?.length) {
      body.append(node("span", "material-tags", material.tags.join(" · ")));
    }
    item.append(icon, body);
    els.materialLibrary.append(item);
  }
  refreshIcons(els.materialLibrary);
}

function renderTaskLibraryFields() {
  const selectedProject = els.taskProject.value;
  const selectedMaterialIds = new Set(
    [...els.taskMaterialPicker.querySelectorAll("input:checked")].map(
      (input) => input.value
    )
  );
  clear(els.taskProject);
  els.taskProject.append(new Option("不归入项目", ""));
  clear(els.materialProject);
  els.materialProject.append(new Option("全局素材", ""));
  for (const project of state.projects) {
    els.taskProject.append(new Option(project.name, project.id));
    els.materialProject.append(new Option(project.name, project.id));
  }
  if ([...els.taskProject.options].some((item) => item.value === selectedProject)) {
    els.taskProject.value = selectedProject;
  }
  clear(els.taskMaterialPicker);
  if (!state.materials.length) {
    els.taskMaterialPicker.append(node("span", "field-hint", "暂未添加素材"));
    return;
  }
  for (const material of state.materials) {
    const label = node("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = material.id;
    input.checked = selectedMaterialIds.has(material.id);
    label.append(input, node("span", "", material.title));
    els.taskMaterialPicker.append(label);
  }
}

function refreshIcons(root = document) {
  window.MindFlowEditor?.refreshIcons?.(root);
}

function documentScratchKey(taskId = state.selectedTaskId) {
  return taskId ? `mindflow.creator.scratch.${taskId}` : "";
}

function readDocumentScratch() {
  const key = documentScratchKey();
  if (!key) return null;
  try {
    return JSON.parse(localStorage.getItem(key) || "null");
  } catch {
    return null;
  }
}

function persistDocumentScratch() {
  if (!state.documentEditor || !state.selectedTaskId || !state.documentSourceKey) return;
  try {
    localStorage.setItem(
      documentScratchKey(),
      JSON.stringify({
        source_key: state.documentSourceKey,
        source_artifact_id: state.documentSourceArtifactId,
        title: els.documentTitleInput.value,
        body_markdown: state.documentEditor.getMarkdown(),
        updated_at: new Date().toISOString()
      })
    );
  } catch {
    // Local draft persistence is best effort; server-side version saves still work.
  }
}

function scheduleDocumentScratch() {
  window.clearTimeout(scheduleDocumentScratch.timer);
  scheduleDocumentScratch.timer = window.setTimeout(persistDocumentScratch, 350);
}

function clearDocumentScratch() {
  window.clearTimeout(scheduleDocumentScratch.timer);
  const key = documentScratchKey();
  if (key) localStorage.removeItem(key);
}

function updateEditorOutputState() {
  if (!state.documentEditor) return;
  const title = els.documentTitleInput.value.trim();
  const markdown = state.documentEditor.getMarkdown().trim();
  setOutputActions(
    `${title}${title && markdown ? "\n\n" : ""}${markdown}`.trim(),
    `${safeFilename(title || "mindflow-creator")}.md`
  );
}

function markEditorDirty() {
  if (!state.documentSourceKey) return;
  state.editorDirty = true;
  els.editorSaveState.textContent = "有未保存修改";
  els.editorSaveState.classList.add("is-dirty");
  els.topSaveState.textContent = "修改已暂存在本机";
  scheduleDocumentScratch();
  updateEditorOutputState();
}

function resizeDocumentTitle() {
  els.documentTitleInput.style.height = "auto";
  els.documentTitleInput.style.height = `${els.documentTitleInput.scrollHeight}px`;
}

function renderSelectionAssistant(
  text,
  prefixContext = "",
  suffixContext = ""
) {
  state.editorSelectedText = String(text || "").trim();
  state.editorSelectionContext = {
    prefixContext: String(prefixContext || ""),
    suffixContext: String(suffixContext || "")
  };
  const hasSelection = Boolean(state.editorSelectedText);
  els.selectionAssistant.hidden = !hasSelection;
  els.selectedTextPreview.textContent = hasSelection
    ? state.editorSelectedText.slice(0, 280)
    : "";
}

function initializeDocumentEditor() {
  if (!window.MindFlowEditor) {
    throw new Error("正文编辑器加载失败");
  }
  state.documentEditor = window.MindFlowEditor.create({
    element: els.documentEditor,
    toolbar: els.editorToolbar,
    onSelectionChange: ({ text, prefixContext, suffixContext }) =>
      renderSelectionAssistant(text, prefixContext, suffixContext),
    onUpdate: ({ characters }) => {
      els.editorWordCount.textContent = `${characters} 字`;
      markEditorDirty();
    }
  });
  state.documentEditor.setEditable(false);
  els.documentTitleInput.disabled = true;
}

async function loadTasks(reset = true) {
  const params = new URLSearchParams({ limit: "20" });
  if (state.taskFilter) params.set("status", state.taskFilter);
  if (state.projectFilterId) params.set("project_id", state.projectFilterId);
  if (!reset && state.nextCursor) params.set("cursor", state.nextCursor);
  const page = await apiJson(`${API_ROOT}/tasks?${params.toString()}`);
  state.tasks = reset ? page.items : [...state.tasks, ...page.items];
  state.nextCursor = page.next_cursor;
  renderTasks();
}

function renderTasks() {
  clear(els.taskList);
  const query = state.workSearch.trim().toLowerCase();
  const visibleTasks = query
    ? state.tasks.filter((task) =>
      `${task.goal || ""} ${KIND_LABELS[task.kind] || task.kind}`
        .toLowerCase()
        .includes(query)
    )
    : state.tasks;
  if (!visibleTasks.length) {
    els.taskList.append(
      node("div", "pane-empty", query ? "没有匹配的作品" : "当前筛选下没有作品")
    );
  }
  for (const task of visibleTasks) {
    const row = node("button", "task-row");
    row.type = "button";
    if (task.task_id === state.selectedTaskId) row.classList.add("is-active");

    const head = node("div", "task-row-head");
    head.append(
      node("span", "task-row-kind", KIND_LABELS[task.kind] || task.kind),
      node("span", "task-row-time", formatCompactTime(task.updated_at))
    );
    row.append(head);
    row.append(node("strong", "", task.goal));
    const status = node(
      "span",
      "task-row-status",
      STATUS_LABELS[task.status] || task.status
    );
    if (task.status === "WAITING_HUMAN") status.textContent = "需要你的确认";
    row.append(status);
    row.addEventListener("click", () => {
      switchMobilePane("editor");
      selectTask(task.task_id);
    });
    els.taskList.append(row);
  }
  els.loadMoreTasks.hidden = !state.nextCursor;
  els.allProjectCount.textContent = String(state.tasks.length);
  els.pendingProjectCount.textContent = String(
    state.tasks.filter((task) => task.status === "WAITING_HUMAN").length
  );
  els.completedProjectCount.textContent = String(
    state.tasks.filter((task) => task.status === "COMPLETED").length
  );
}

async function selectTask(taskId) {
  if (!taskId) return;
  state.selectedTaskId = taskId;
  sessionStorage.setItem(ACTIVE_TASK_KEY, taskId);
  state.lastEventIds.delete(taskId);
  state.snapshot = null;
  state.artifacts = [];
  state.artifactDetails.clear();
  state.selectedArtifactId = null;
  state.selectedDraft = null;
  state.draftVersions = [];
  state.suggestions = [];
  state.currentSuggestion = null;
  state.branches = [];
  state.channelVariants = [];
  state.documentSourceKey = null;
  state.documentSourceArtifactId = null;
  state.documentRequestedArtifactId = null;
  state.editorDirty = false;
  state.events = [];
  renderSuggestionPanel();
  stopEventStream();
  renderTasks();
  renderTimeline();
  els.assistantStatusText.textContent = "正在读取作品";
  els.workflowEmpty.hidden = false;
  els.workflowContent.hidden = true;
  await renderDocument();
  await renderContextPanels();
  try {
    await refreshTask(true);
    startEventStream(taskId);
  } catch (error) {
    showToast(`任务读取失败：${error.message}`, true);
  }
}

async function refreshTask(refreshOutputs = false) {
  const taskId = state.selectedTaskId;
  if (!taskId) return;
  const snapshot = await apiJson(`${API_ROOT}/tasks/${taskId}`);
  if (taskId !== state.selectedTaskId) return;
  state.snapshot = snapshot;
  state.artifacts = snapshot.artifacts || [];
  renderTasksFromSnapshot();
  renderWorkflow();
  if (refreshOutputs) {
    await loadDrafts();
  }
  await renderOutput();
  await renderDocument();
  await renderContextPanels();
}

function renderTasksFromSnapshot() {
  if (!state.snapshot) return;
  const index = state.tasks.findIndex(
    (task) => task.task_id === state.snapshot.task_id
  );
  const item = {
    task_id: state.snapshot.task_id,
    run_id: state.snapshot.run_id,
    kind: state.snapshot.kind,
    goal: state.snapshot.goal,
    status: state.snapshot.status,
    version: state.snapshot.version,
    pending_decision_id: state.snapshot.pending_decision?.decision_id || null,
    final_artifact_id: state.snapshot.final_artifact_id,
    error_code: state.snapshot.error_code,
    created_at: state.snapshot.created_at,
    updated_at: state.snapshot.updated_at
  };
  if (index >= 0) state.tasks[index] = item;
  else state.tasks.unshift(item);
  renderTasks();
}

function renderWorkflow() {
  const snapshot = state.snapshot;
  els.workflowEmpty.hidden = Boolean(snapshot);
  els.workflowContent.hidden = !snapshot;
  if (!snapshot) return;

  els.taskKind.textContent = KIND_LABELS[snapshot.kind] || snapshot.kind;
  els.taskTrace.textContent = `任务记录 ${snapshot.trace_id.slice(0, 8)}`;
  const constraints = snapshot.constraints || {};
  els.taskBriefSummary.textContent = [
    FORMAT_LABELS[constraints.format] || constraints.format,
    constraints.audience,
    constraints.target_length ? `${constraints.target_length} 字` : ""
  ].filter(Boolean).join(" · ") || "创作简报";
  els.taskGoal.textContent = snapshot.goal;
  renderTaskBrief(constraints);
  setBadge(
    els.taskStatus,
    STATUS_LABELS[snapshot.status] || snapshot.status,
    taskTone(snapshot.status)
  );
  els.cancelTask.hidden = !["QUEUED", "RUNNING", "WAITING_HUMAN", "RETRYING"].includes(
    snapshot.status
  );
  els.retryTask.hidden = snapshot.status !== "FAILED";
  els.continuePublishing.hidden = snapshot.status !== "COMPLETED";
  renderStages(snapshot);
  renderDecision(snapshot);
}

function renderTaskBrief(constraints) {
  clear(els.taskBriefBar);
  const items = [
    ["写给", constraints.audience],
    ["读者读完", constraints.reader_takeaway],
    ["表达方式", TONE_LABELS[constraints.tone] || constraints.tone]
  ].filter(([, value]) => Boolean(value));
  els.taskBriefBar.hidden = !items.length;
  for (const [label, value] of items) {
    const item = node("span", "task-brief-item");
    item.append(
      node("small", "", label),
      node("strong", "", String(value))
    );
    els.taskBriefBar.append(item);
  }
}

function renderStages(snapshot) {
  clear(els.workflowStages);
  const kinds = new Set(snapshot.artifacts.map((artifact) => artifact.kind));
  const stages = [
    ["定方向", ["TOPIC_OPTIONS"]],
    ["搭结构", ["CONTENT_OUTLINE"]],
    ["写初稿", ["SOURCE_DRAFT", "DRAFT"]],
    ["精修", ["CRITIQUE", "EVALUATION_REPORT"]],
    ["成稿", ["FINAL_CONTENT"]]
  ];
  const pendingStage = {
    TOPIC_SELECTION: 0,
    OUTLINE_APPROVAL: 1,
    DRAFT_REVIEW: 2
  }[snapshot.pending_decision?.kind];
  let activeAssigned = false;
  for (const [index, [label, expectedKinds]] of stages.entries()) {
    const item = node("li", "workflow-stage", label);
    if (pendingStage !== undefined) {
      if (index < pendingStage) item.classList.add("is-done");
      if (index === pendingStage) item.classList.add("is-active");
      els.workflowStages.append(item);
      continue;
    }
    const done =
      snapshot.status === "COMPLETED" ||
      expectedKinds.some((kind) => kinds.has(kind));
    if (done) {
      item.classList.add("is-done");
    } else if (
      !activeAssigned &&
      !["COMPLETED", "FAILED", "CANCELLED"].includes(snapshot.status)
    ) {
      item.classList.add("is-active");
      activeAssigned = true;
    }
    els.workflowStages.append(item);
  }
}

function renderDecision(snapshot) {
  clear(els.decisionSurface);
  const decision = snapshot.pending_decision;
  const hasDecision = Boolean(decision);
  els.editorAssistant.hidden = hasDecision;
  els.decisionSurface.hidden = !hasDecision;
  els.documentContextBanner.hidden = !hasDecision;
  els.assistantStatusText.textContent = hasDecision
    ? "等待你的判断"
    : snapshot.status === "COMPLETED"
      ? "成稿可继续编辑"
      : snapshot.status === "FAILED"
        ? "任务需要处理"
        : "正在协助创作";
  if (!decision) {
    state.lastDecisionId = null;
    return;
  }
  els.documentContextTitle.textContent = {
    TOPIC_SELECTION: "选择这篇内容的切入点",
    OUTLINE_APPROVAL: "确认文章的观点与结构",
    DRAFT_REVIEW: "审阅正文并留下修改意见"
  }[decision.kind] || "助手需要你的判断";
  els.documentContextText.textContent = "处理后 Agent 会从这里继续创作";
  if (state.lastDecisionId !== decision.decision_id) {
    state.lastDecisionId = decision.decision_id;
    switchAssistantTab("collaboration");
  }

  const header = node("div", "decision-header");
  const titles = {
    TOPIC_SELECTION: "选择文章切入点",
    OUTLINE_APPROVAL: "确认文章大纲",
    DRAFT_REVIEW: "审阅正文草稿"
  };
  const steps = {
    TOPIC_SELECTION: "第 1 步 · 定方向",
    OUTLINE_APPROVAL: "第 2 步 · 搭结构",
    DRAFT_REVIEW: "第 3 步 · 看正文"
  };
  const prompts = {
    TOPIC_SELECTION: "同一个主题可以有不同写法。选择最符合你表达目标的一种。",
    OUTLINE_APPROVAL: "确认文章观点和展开顺序，也可以直接调整每一节。",
    DRAFT_REVIEW: "像编辑自己的文章一样审阅正文，只批注真正需要调整的部分。"
  };
  header.append(
    node("span", "decision-kicker", steps[decision.kind] || "需要你确认"),
    node("h3", "", titles[decision.kind] || "需要你确认"),
    node("p", "", prompts[decision.kind] || decision.prompt)
  );
  els.decisionSurface.append(header);
  if (decision.status === "SUBMITTED") {
    const queued = node("div", "decision-idle");
    queued.append(
      node("strong", "", "已经收到你的选择"),
      node("span", "", "创作助手正在继续处理")
    );
    els.decisionSurface.append(queued);
    return;
  }
  if (decision.kind === "TOPIC_SELECTION") {
    renderTopicDecision(decision);
  } else if (decision.kind === "OUTLINE_APPROVAL") {
    renderOutlineDecision(decision);
  } else if (decision.kind === "DRAFT_REVIEW") {
    renderDraftDecision(decision);
  } else {
    els.decisionSurface.append(
      node("div", "decision-idle", "当前内容需要换一种方式处理")
    );
  }
  refreshIcons(els.decisionSurface);
}

function renderTopicDecision(decision) {
  const form = node("form");
  const options = node("div", "topic-options");
  const optionMap = new Map();
  const writableOptions = decision.options.filter(
    (option) => String(option.recommendation || "").toUpperCase() !== "SKIP"
  );
  const advisoryOptions = decision.options.filter(
    (option) => String(option.recommendation || "").toUpperCase() === "SKIP"
  );
  const candidates = writableOptions.length ? writableOptions : decision.options;
  const hasRecommended = candidates.some((option) => option.recommended);

  for (const [index, option] of candidates.entries()) {
    optionMap.set(option.option_id, option);
    const label = node("label", "topic-option");
    if (option.recommended) label.classList.add("is-recommended");
    const radio = node("input");
    radio.type = "radio";
    radio.name = "topic-option";
    radio.value = option.option_id;
    radio.checked = option.recommended || (!hasRecommended && index === 0);
    const body = node("span", "topic-option-body");

    const top = node("span", "topic-option-top");
    const marker = node("span", "topic-direction-marker");
    marker.append(
      node("span", "topic-direction-number", String(index + 1).padStart(2, "0")),
      node("span", "topic-direction-type", topicDirectionLabel(option, index))
    );
    const badges = node("span", "topic-option-badges");
    if (option.recommended) {
      badges.append(node("span", "recommended-label", "助手推荐"));
    }
    top.append(marker, badges);
    body.append(top, node("span", "topic-option-heading", option.title));
    label.append(radio, body);
    options.append(label);
  }
  form.append(options);

  const selectedDetail = node("section", "topic-selected-detail");
  form.append(selectedDetail);

  if (advisoryOptions.length) {
    const advice = node("details", "topic-advice");
    advice.append(node("summary", "", "查看助手排除的低价值写法"));
    const adviceBody = node("div", "topic-advice-body");
    for (const option of advisoryOptions) {
      const item = node("div", "topic-advice-item");
      item.append(
        node("strong", "", option.title),
        node("span", "", option.risk_note || option.angle)
      );
      adviceBody.append(item);
    }
    advice.append(adviceBody);
    form.append(advice);
  }

  const editor = node("details", "human-edit-panel inline-editor");
  editor.append(node("summary", "", "编辑所选方向（可选）"));
  const editorBody = node("div", "inline-editor-body");
  const titleInput = node("input");
  titleInput.type = "text";
  titleInput.placeholder = "文章标题";
  const angleInput = node("textarea");
  angleInput.placeholder = "文章角度和写法";
  angleInput.rows = 2;
  const questionInput = node("textarea");
  questionInput.placeholder = "这篇内容要替读者回答什么问题";
  questionInput.rows = 2;
  const whyInput = node("textarea");
  whyInput.placeholder = "为什么现在值得写";
  whyInput.rows = 2;
  editorBody.append(
    labeledEditorField("文章标题", titleInput),
    labeledEditorField("写作角度", angleInput),
    labeledEditorField("核心读者问题", questionInput),
    labeledEditorField("方向依据", whyInput)
  );
  editor.append(editorBody);
  form.append(editor);

  const syncEditor = () => {
    const selected = form.querySelector('input[name="topic-option"]:checked');
    const option = selected ? optionMap.get(selected.value) : null;
    if (!option) return;
    titleInput.value = option.title || "";
    angleInput.value = option.angle || "";
    questionInput.value = option.reader_question || "";
    whyInput.value = option.why_now || "";
  };
  const renderSelectedDetail = () => {
    clear(selectedDetail);
    const selected = form.querySelector('input[name="topic-option"]:checked');
    const option = selected ? optionMap.get(selected.value) : null;
    if (!option) return;

    const intro = node("div", "topic-detail-intro");
    intro.append(
      node("span", "decision-kicker", "所选方向"),
      node("strong", "", option.title)
    );
    selectedDetail.append(intro);

    if (option.reader_question) {
      const question = node("div", "topic-question");
      question.append(
        node("small", "", "这篇会回答"),
        node("strong", "", option.reader_question)
      );
      selectedDetail.append(question);
    }

    const detailGrid = node("div", "topic-detail-grid");
    if (option.angle) {
      const angle = node("div", "topic-detail-block");
      angle.append(
        node("small", "", "内容怎么展开"),
        node("span", "", option.angle)
      );
      detailGrid.append(angle);
    }
    if (option.audience_value) {
      const value = node("div", "topic-detail-block");
      value.append(
        node("small", "", "读者能带走"),
        node("span", "", option.audience_value)
      );
      detailGrid.append(value);
    }
    if (detailGrid.childNodes.length) selectedDetail.append(detailGrid);

    const rationale = node("details", "topic-rationale");
    rationale.append(node("summary", "", "查看写作依据"));
    const rationaleBody = node("div", "topic-rationale-body");
    if (option.why_now) {
      rationaleBody.append(node("small", "", `方向依据：${option.why_now}`));
    }
    if (option.differentiation) {
      rationaleBody.append(node("small", "", `内容特色：${option.differentiation}`));
    }
    if (option.risk_note) {
      rationaleBody.append(node("small", "", `准备提醒：${option.risk_note}`));
    }
    const cites = new Set([
      ...(option.comment_ids || []),
      ...(option.evidence_ids || [])
    ]);
    if (cites.size) {
      rationaleBody.append(
        node("small", "topic-evidence", `已参考 ${cites.size} 条相关素材`)
      );
    }
    rationale.append(rationaleBody);
    selectedDetail.append(rationale);
  };
  form.querySelectorAll('input[name="topic-option"]').forEach((input) => {
    input.addEventListener("change", () => {
      syncEditor();
      renderSelectedDetail();
    });
  });
  syncEditor();
  renderSelectedDetail();

  const actions = node("div", "decision-actions");
  const regenerateBtn = node("button", "button button-secondary", "换一组方向");
  regenerateBtn.type = "button";
  regenerateBtn.hidden = !decision.allowed_actions?.includes("REQUEST_CHANGES");
  const continueBtn = node(
    "button",
    "button button-primary topic-continue",
    "用这个方向生成大纲"
  );
  continueBtn.type = "button";
  actions.append(regenerateBtn, continueBtn);
  form.append(actions);

  const selectedOptionId = () => {
    const selected = form.querySelector('input[name="topic-option"]:checked');
    return selected ? selected.value : null;
  };

  const setBusy = (busy) => {
    regenerateBtn.disabled = busy;
    continueBtn.disabled = busy;
    form.querySelectorAll('input[name="topic-option"]').forEach((input) => {
      input.disabled = busy;
    });
  };

  editor.addEventListener("toggle", () => {
    continueBtn.textContent = editor.open
      ? "保存修改并生成大纲"
      : "用这个方向生成大纲";
  });

  regenerateBtn.addEventListener("click", async () => {
    setBusy(true);
    try {
      await submitDecision(decision, {
        action: "REQUEST_CHANGES",
        feedback: "请基于创作简报重新生成一组差异更明确、全部可以直接写作的内容方向。"
      });
    } catch (error) {
      showToast(`重新生成失败：${error.message}`, true);
    } finally {
      setBusy(false);
    }
  });

  continueBtn.addEventListener("click", async () => {
    const optionId = selectedOptionId();
    const base = optionMap.get(optionId);
    if (!optionId) {
      showToast("请选择一个内容方向", true);
      return;
    }
    if (!base) {
      showToast("这个方向暂时不可用，请换一个方向", true);
      return;
    }

    let payload = {
      action: "SELECT",
      selected_option_id: optionId
    };
    if (editor.open) {
      const title = titleInput.value.trim();
      const angle = angleInput.value.trim();
      if (!title || !angle) {
        showToast("编辑后的标题和角度不能为空", true);
        return;
      }
      payload = {
        action: "EDIT",
        selected_option_id: optionId,
        edited_payload: {
          option: {
            id: optionId,
            title,
            angle,
            audience_value: base.audience_value || "",
            evidence_ids: base.evidence_ids || [],
            comment_ids: base.comment_ids || [],
            risk_note: base.risk_note || "",
            recommendation: base.recommendation || "WRITE_NOW",
            why_now:
              whyInput.value.trim() || base.why_now || "创作者已调整这个方向。",
            reader_question:
              questionInput.value.trim() || base.reader_question || title,
            differentiation: base.differentiation || ""
          }
        }
      };
    }

    setBusy(true);
    try {
      await submitDecision(decision, payload);
    } catch (error) {
      showToast(`提交失败：${error.message}`, true);
    } finally {
      setBusy(false);
    }
  });
  els.decisionSurface.append(form);
}

function topicDirectionLabel(option, index) {
  const value = `${option.title || ""} ${option.angle || ""}`;
  if (/实战|案例|Bug/i.test(value)) return "案例实战";
  if (/边界|误区|风险|不该/.test(value)) return "边界视角";
  return ["方法指南", "案例实战", "边界视角"][index] || "内容方向";
}

function labeledEditorField(labelText, control) {
  const field = node("label", "human-edit-field");
  field.append(node("span", "", labelText), control);
  return field;
}

function renderOutlineDecision(decision) {
  const content = decision.source?.content || {};
  const editor = node("div", "outline-preview human-edit-panel outline-editor");
  editor.append(node("h4", "", "这篇文章准备这样展开"));

  const titleInput = node("input");
  titleInput.type = "text";
  titleInput.value = content.title || "";
  titleInput.placeholder = "文章标题";
  const thesisInput = node("textarea");
  thesisInput.value = content.thesis || "";
  thesisInput.placeholder = "用一句话说明这篇内容的核心观点";
  thesisInput.rows = 3;
  editor.append(
    labeledEditorField("标题", titleInput),
    labeledEditorField("核心观点", thesisInput)
  );

  const sectionList = node("div", "outline-section-editors");
  const sectionEditors = [];
  const connectedSections = () => sectionEditors.filter((item) => item.root.isConnected);
  const refreshSectionNumbers = () => {
    connectedSections().forEach((item, index) => {
      item.number.textContent = `第 ${index + 1} 节`;
    });
  };
  const addSectionEditor = (section = {}) => {
    const root = node("section", "outline-section outline-section-editor");
    const header = node("div", "outline-section-editor-head");
    const number = node("strong", "", "新章节");
    const remove = node("button", "icon-button", "×");
    remove.type = "button";
    remove.title = "删除这一节";
    remove.setAttribute("aria-label", "删除这一节");
    header.append(number, remove);

    const heading = node("input");
    heading.type = "text";
    heading.value = section.heading || "";
    heading.placeholder = "章节标题";
    const purpose = node("textarea");
    purpose.value = section.purpose || "";
    purpose.placeholder = "这一节要替读者解决什么问题";
    purpose.rows = 2;
    const points = node("textarea");
    points.value = (section.key_points || []).join("\n");
    points.placeholder = "每行一个要点";
    points.rows = 4;
    root.append(
      header,
      labeledEditorField("章节标题", heading),
      labeledEditorField("章节任务", purpose),
      labeledEditorField("核心要点", points)
    );
    const item = {
      root,
      number,
      heading,
      purpose,
      points,
      evidence_ids: section.evidence_ids || []
    };
    sectionEditors.push(item);
    sectionList.append(root);
    remove.addEventListener("click", () => {
      if (connectedSections().length <= 3) {
        showToast("至少保留三个章节", true);
        return;
      }
      root.remove();
      refreshSectionNumbers();
    });
    refreshSectionNumbers();
  };

  for (const section of content.sections || []) addSectionEditor(section);
  const addSection = node("button", "button button-secondary add-section", "添加章节");
  addSection.type = "button";
  addSection.addEventListener("click", () => {
    addSectionEditor();
    connectedSections().at(-1)?.heading.focus();
  });
  editor.append(sectionList, addSection);

  const ctaInput = node("input");
  ctaInput.type = "text";
  ctaInput.value = content.call_to_action || "";
  ctaInput.placeholder = "例如：让读者用一个真实场景验证这套方法";
  editor.append(labeledEditorField("结尾希望读者做什么", ctaInput));
  els.decisionSurface.append(editor);

  const changeArea = node("div", "change-request");
  const feedback = node("textarea");
  feedback.placeholder = "例如：减少概念解释，增加一个完整失败恢复案例";
  feedback.maxLength = 4000;
  changeArea.append(feedback);
  changeArea.hidden = true;
  els.decisionSurface.append(changeArea);

  const actions = node("div", "decision-actions");
  const requestChanges = node("button", "button button-secondary", "让助手重做");
  requestChanges.type = "button";
  const editSave = node("button", "button button-secondary", "保存我的调整");
  editSave.type = "button";
  const approve = node("button", "button button-primary", "就按这个大纲写");
  approve.type = "button";
  actions.append(requestChanges, editSave, approve);
  els.decisionSurface.append(actions);

  const setBusy = (busy) => {
    requestChanges.disabled = busy;
    editSave.disabled = busy;
    approve.disabled = busy;
    addSection.disabled = busy;
  };

  const collectOutline = () => ({
    title: titleInput.value.trim(),
    thesis: thesisInput.value.trim(),
    sections: connectedSections().map((section) => ({
      heading: section.heading.value.trim(),
      purpose: section.purpose.value.trim(),
      key_points: section.points.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean),
      evidence_ids: section.evidence_ids
    })),
    call_to_action: ctaInput.value.trim()
  });

  editSave.addEventListener("click", async () => {
    const outline = collectOutline();
    if (!outline.title || !outline.thesis || outline.sections.length < 3) {
      showToast("请至少保留标题、核心观点和三个章节", true);
      return;
    }
    if (
      outline.sections.some(
        (section) => !section.heading || !section.purpose || !section.key_points.length
      )
    ) {
      showToast("每个章节都需要标题、章节任务和至少一个要点", true);
      return;
    }
    if (!outline.call_to_action) {
      showToast("请填写结尾希望读者完成的行动", true);
      return;
    }
    setBusy(true);
    try {
      await submitDecision(decision, {
        action: "EDIT",
        edited_payload: { outline }
      });
    } finally {
      setBusy(false);
    }
  });

  requestChanges.addEventListener("click", async () => {
    if (changeArea.hidden) {
      changeArea.hidden = false;
      feedback.focus();
      requestChanges.textContent = "提交重做要求";
      return;
    }
    const value = feedback.value.trim();
    if (!value) {
      showToast("请写下具体的重做要求", true);
      return;
    }
    setBusy(true);
    try {
      await submitDecision(decision, {
        action: "REQUEST_CHANGES",
        feedback: value
      });
    } finally {
      setBusy(false);
    }
  });

  approve.addEventListener("click", async () => {
    setBusy(true);
    try {
      await submitDecision(decision, { action: "APPROVE" });
    } finally {
      setBusy(false);
    }
  });
}

function draftSectionLabels(decision) {
  const outline = (state.snapshot?.artifacts || []).find(
    (item) => item.kind === "CONTENT_OUTLINE"
  );
  const sections = outline?.content?.sections;
  if (Array.isArray(sections) && sections.length) {
    return sections.map((section, index) => ({
      section: index + 1,
      label: section.heading || `第 ${index + 1} 节`
    }));
  }
  const body = String(decision.source?.content?.body_markdown || "");
  const headings = [...body.matchAll(/^##\s+(.+)$/gm)].map((match) => match[1]);
  if (headings.length) {
    return headings.map((heading, index) => ({
      section: index + 1,
      label: heading
    }));
  }
  return [1, 2, 3].map((section) => ({
    section,
    label: `第 ${section} 节`
  }));
}

function renderDraftDecision(decision) {
  const form = node("form");
  const content = decision.source?.content || {};
  const preview = node("article", "draft-review-document");
  preview.append(node("h2", "", content.title || "正文草稿"));
  const markdown = node("div", "markdown-view");
  renderMarkdown(markdown, content.body_markdown || "");
  preview.append(markdown);

  const annotationPanel = node("details", "human-edit-panel draft-annotation-panel");
  annotationPanel.append(
    node("summary", "", "我要逐段批注")
  );
  const annotationBody = node("div", "draft-annotation-body");
  annotationBody.append(
    node("p", "annotation-hint", "只填写需要调整的章节，助手会保留其余内容。")
  );
  const noteInputs = [];
  for (const item of draftSectionLabels(decision)) {
    const row = node("label", "human-edit-field");
    row.append(node("span", "", `${item.section}. ${item.label}`));
    const input = node("textarea");
    input.rows = 2;
    input.placeholder = "例如：补一个 Worker 租约过期后的恢复案例";
    input.dataset.section = String(item.section);
    row.append(input);
    annotationBody.append(row);
    noteInputs.push(input);
  }
  annotationPanel.append(annotationBody);

  const changeArea = node("div", "change-request");
  changeArea.hidden = true;
  const feedback = node("textarea");
  feedback.rows = 3;
  feedback.placeholder = "例如：全文太像技术文档，改得更像面向社区读者的深度文章";
  changeArea.append(feedback);

  const actions = node("div", "decision-actions");
  const editSave = node("button", "button button-secondary", "按批注优化");
  editSave.type = "button";
  const requestChanges = node("button", "button button-secondary", "整体重写");
  requestChanges.type = "button";
  const approve = node("button", "button button-primary", "这版可以，完成创作");
  approve.type = "button";
  actions.append(editSave, requestChanges, approve);

  form.append(preview, annotationPanel, changeArea, actions);
  els.decisionSurface.append(form);

  const setBusy = (busy) => {
    editSave.disabled = busy;
    requestChanges.disabled = busy;
    approve.disabled = busy;
  };

  const submit = async (payload) => {
    setBusy(true);
    try {
      await submitDecision(decision, payload);
    } catch (error) {
      showToast(`提交失败：${error.message}`, true);
    } finally {
      setBusy(false);
    }
  };

  const collectAnnotations = () =>
    noteInputs
      .map((input) => ({
        section: Number(input.dataset.section),
        note: input.value.trim()
      }))
      .filter((item) => item.note);

  editSave.addEventListener("click", async () => {
    const annotations = collectAnnotations();
    if (!annotations.length) {
      annotationPanel.open = true;
      showToast("请至少填写一条分段批注，或选择「整体重写」", true);
      return;
    }
    await submit({
      action: "EDIT",
      edited_payload: { annotations }
    });
  });

  requestChanges.addEventListener("click", async () => {
    if (changeArea.hidden) {
      changeArea.hidden = false;
      feedback.focus();
      requestChanges.textContent = "提交整体重写";
      return;
    }
    const value = feedback.value.trim();
    if (!value) {
      showToast("请填写具体修改意见", true);
      return;
    }
    await submit({
      action: "REQUEST_CHANGES",
      feedback: value
    });
  });

  approve.addEventListener("click", async () => {
    await submit({ action: "APPROVE" });
  });
}

async function submitDecision(decision, payload) {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  setBadge(els.taskStatus, "恢复执行中", "is-running");
  const result = await apiJson(
    `${API_ROOT}/tasks/${snapshot.task_id}/decisions/${decision.decision_id}/responses`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey("decision") },
      body: JSON.stringify({
        ...payload,
        expected_task_version: snapshot.version
      })
    }
  );
  showToast(result.task_status === "COMPLETED" ? "成稿已经准备好" : "已应用你的选择");
  await refreshTask(true);
  await loadTasks(true);
}

function startEventStream(taskId) {
  stopEventStream();
  const controller = new AbortController();
  const generation = ++state.streamGeneration;
  state.streamController = controller;
  connectEventStream(taskId, generation, controller).catch((error) => {
    if (error.name !== "AbortError") {
      setStreamState("连接失败");
      showToast(`事件流失败：${error.message}`, true);
    }
  });
}

function stopEventStream() {
  if (state.streamController) state.streamController.abort();
  state.streamController = null;
  state.streamGeneration += 1;
}

async function connectEventStream(taskId, generation, controller) {
  let retryDelay = 600;
  while (
    state.selectedTaskId === taskId &&
    generation === state.streamGeneration &&
    !controller.signal.aborted
  ) {
    try {
      setStreamState("连接中");
      const headers = {
        Accept: "text/event-stream",
        Authorization: authHeader()
      };
      const lastEventId = state.lastEventIds.get(taskId);
      if (lastEventId) headers["Last-Event-ID"] = lastEventId;
      const response = await fetch(`${API_ROOT}/tasks/${taskId}/events`, {
        headers,
        signal: controller.signal
      });
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.error?.message || `${response.status} ${response.statusText}`);
      }
      if (!response.body) throw new Error("浏览器未提供流式响应");
      setStreamState("实时连接");
      retryDelay = 600;
      const terminal = await consumeEventStream(
        response.body,
        taskId,
        generation,
        controller.signal
      );
      if (terminal) {
        setStreamState("已结束");
        return;
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      setStreamState("正在重连");
      await sleep(retryDelay);
      retryDelay = Math.min(retryDelay * 2, 5000);
    }
  }
}

async function consumeEventStream(body, taskId, generation, signal) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const event = parseEventFrame(frame);
        if (!event) continue;
        if (event.id) state.lastEventIds.set(taskId, event.id);
        const terminal = handleStreamEvent(event, taskId, generation);
        if (terminal) return true;
      }
      if (done) break;
    }
  } finally {
    reader.releaseLock();
  }
  return false;
}

function parseEventFrame(frame) {
  let type = "message";
  let id = "";
  const data = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  let payload;
  try {
    payload = JSON.parse(data.join("\n"));
  } catch {
    payload = { message: data.join("\n") };
  }
  return { type, id, payload };
}

function handleStreamEvent(event, taskId, generation) {
  if (
    taskId !== state.selectedTaskId ||
    generation !== state.streamGeneration
  ) {
    return false;
  }
  if (event.type === "task.snapshot") {
    state.snapshot = event.payload;
    state.artifacts = event.payload.artifacts || [];
    renderTasksFromSnapshot();
    renderWorkflow();
    return false;
  }
  if (event.type === "heartbeat") {
    setStreamState("实时连接");
    return false;
  }
  if (event.type === "stream.closed") {
    scheduleTaskRefresh(true);
    return true;
  }
  const envelope = event.payload;
  if (envelope?.event_id && !state.events.some((item) => item.event_id === envelope.event_id)) {
    state.events.push(envelope);
    if (state.events.length > 100) state.events.shift();
    renderTimeline();
  }
  scheduleTaskRefresh(
    [
      "artifact.created",
      "artifact.finalized",
      "decision.required",
      "decision.requested",
      "decision.submitted",
      "supervisor.plan.created",
      "plan.dispatched",
      "agent.completed",
      "agent.failed",
      "task.completed",
      "run.failed"
    ].includes(event.type)
  );
  return false;
}

function scheduleTaskRefresh(refreshOutputs = false) {
  window.clearTimeout(state.refreshTimer);
  state.refreshTimer = window.setTimeout(async () => {
    try {
      await refreshTask(refreshOutputs);
      if (refreshOutputs) await loadTasks(true);
    } catch (error) {
      showToast(`状态刷新失败：${error.message}`, true);
    }
  }, 120);
}

function setStreamState(text) {
  els.streamState.textContent = text;
}

function renderTimeline() {
  clear(els.eventTimeline);
  if (!state.events.length) {
    els.eventTimeline.append(node("li", "pane-empty", "等待持久化事件"));
    return;
  }
  const events = [...state.events].reverse();
  for (const event of events) {
    const row = node("li", "event-row");
    if (event.type.includes("decision")) row.classList.add("is-decision");
    if (event.type.includes("completed") || event.type.includes("final")) {
      row.classList.add("is-complete");
    }
    if (event.type.includes("failed")) row.classList.add("is-error");
    row.append(
      node("span", "event-type", EVENT_LABELS[event.type] || event.type),
      node("span", "event-summary", eventSummary(event)),
      node("time", "event-time", formatTime(event.timestamp))
    );
    els.eventTimeline.append(row);
  }
}

function eventSummary(event) {
  const payload = event.payload || {};
  if (payload.reason) return String(payload.reason);
  if (payload.capability) {
    return CAPABILITY_LABELS[payload.capability] || "完成一个创作步骤";
  }
  if (payload.kind) return ARTIFACT_LABELS[payload.kind] || String(payload.kind);
  if (payload.error_code) return String(payload.error_code);
  if (payload.artifact_id) return String(payload.artifact_id).slice(0, 18);
  if (payload.status) return String(payload.status);
  if (payload.steps?.length) return `${payload.steps.length} 个计划步骤`;
  return `事件 #${event.sequence}`;
}

async function renderOutput() {
  document.querySelectorAll(".output-tab").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.tab === state.outputTab);
  });
  if (!state.snapshot) {
    clear(els.outputBody);
    els.outputBody.append(node("div", "pane-empty", "选择任务后查看创作内容"));
    setOutputActions("", "");
    return;
  }
  if (state.outputTab === "drafts") {
    await renderDrafts();
    return;
  }
  if (state.outputTab === "evaluation") {
    await renderEvaluation();
    return;
  }
  if (state.outputTab === "channels") {
    await renderChannelVariants();
    return;
  }
  await renderArtifacts();
}

async function renderArtifacts() {
  els.outputTitle.textContent = "创作内容";
  clear(els.outputBody);
  if (!state.artifacts.length) {
    els.outputBody.append(node("div", "pane-empty", "创作助手正在准备内容"));
    setOutputActions("", "");
    return;
  }
  const contentKinds = new Set([
    "SOURCE_DRAFT",
    "EVIDENCE_PACK",
    "TOPIC_OPTIONS",
    "CONTENT_OUTLINE",
    "DRAFT",
    "FINAL_CONTENT"
  ]);
  const contentArtifacts = state.artifacts.filter((artifact) =>
    contentKinds.has(artifact.kind)
  );
  const runtimeArtifacts = state.artifacts.filter(
    (artifact) => !contentKinds.has(artifact.kind)
  );
  const artifactIds = new Set(state.artifacts.map((artifact) => artifact.artifact_id));
  if (!state.selectedArtifactId || !artifactIds.has(state.selectedArtifactId)) {
    state.selectedArtifactId =
      state.snapshot.final_artifact_id ||
      preferredSupportingArtifact(contentArtifacts)?.artifact_id ||
      contentArtifacts.at(-1)?.artifact_id ||
      state.artifacts.at(-1)?.artifact_id;
  }
  const createArtifactRow = (artifact) => {
    const row = node("button", "artifact-row");
    row.type = "button";
    if (artifact.artifact_id === state.selectedArtifactId) {
      row.classList.add("is-active");
    }
    const label = node("span");
    label.append(
      node("strong", "", ARTIFACT_LABELS[artifact.kind] || artifact.kind),
      node("span", "", `第 ${artifact.revision} 版`)
    );
    row.append(label, node("span", "", formatCompactTime(artifact.created_at)));
    row.addEventListener("click", () => selectArtifact(artifact.artifact_id));
    return row;
  };

  const list = node("div", "artifact-list");
  for (const artifact of [...contentArtifacts].reverse()) {
    list.append(createArtifactRow(artifact));
  }
  els.outputBody.append(list);
  if (runtimeArtifacts.length) {
    const runtimeDetails = node("details", "runtime-artifacts");
    runtimeDetails.append(
      node("summary", "", `查看助手运行记录（${runtimeArtifacts.length}）`)
    );
    const runtimeList = node("div", "artifact-list");
    for (const artifact of [...runtimeArtifacts].reverse()) {
      runtimeList.append(createArtifactRow(artifact));
    }
    runtimeDetails.append(runtimeList);
    els.outputBody.append(runtimeDetails);
  }
  const detail = await getArtifactDetail(state.selectedArtifactId);
  if (detail) renderArtifactPreview(detail);
}

function preferredSupportingArtifact(artifacts) {
  const preferredKind = {
    TOPIC_SELECTION: "EVIDENCE_PACK",
    OUTLINE_APPROVAL: "TOPIC_OPTIONS",
    DRAFT_REVIEW: "CONTENT_OUTLINE"
  }[state.snapshot?.pending_decision?.kind];
  if (!preferredKind) return null;
  return [...artifacts].reverse().find((artifact) => artifact.kind === preferredKind);
}

async function selectArtifact(artifactId) {
  state.selectedArtifactId = artifactId;
  await renderArtifacts();
  const detail = await getArtifactDetail(artifactId);
  if (
    detail &&
    ["FINAL_CONTENT", "DRAFT", "SOURCE_DRAFT", "CONTENT_OUTLINE"].includes(detail.kind)
  ) {
    state.selectedDraft = null;
    state.documentRequestedArtifactId = artifactId;
    state.documentSourceKey = null;
    await renderDocument();
  } else {
    updateEditorOutputState();
  }
}

async function getArtifactDetail(artifactId) {
  if (!artifactId || !state.selectedTaskId) return null;
  if (state.artifactDetails.has(artifactId)) {
    return state.artifactDetails.get(artifactId);
  }
  const detail = await apiJson(
    `${API_ROOT}/tasks/${state.selectedTaskId}/artifacts/${artifactId}`
  );
  state.artifactDetails.set(artifactId, detail);
  return detail;
}

function renderArtifactPreview(artifact) {
  const preview = node("article", "artifact-preview");
  const meta = node("div", "artifact-meta");
  const evidenceCount = artifact.kind === "EVIDENCE_PACK"
    ? (artifact.content?.evidence || []).length
    : null;
  meta.append(
    node("span", "", ARTIFACT_LABELS[artifact.kind] || artifact.kind),
    node("span", "", `第 ${artifact.revision} 版`),
    node(
      "span",
      "",
      evidenceCount === 0
        ? "暂无可引用素材"
        : `参考可信度 ${Math.round(artifact.confidence * 100)}%`
    )
  );
  preview.append(meta);
  const document = artifactDocument(artifact);
  if (document) {
    preview.append(node("h2", "", document.title || "未命名内容"));
    const markdown = node("div", "markdown-view");
    renderMarkdown(markdown, document.body_markdown || "");
    preview.append(markdown);
    setOutputActions(
      `${document.title || ""}\n\n${document.body_markdown || ""}`.trim(),
      `${safeFilename(document.title || "mindflow-creator")}.md`
    );
  } else if (artifact.kind === "TOPIC_OPTIONS") {
    renderTopicArtifact(preview, artifact.content);
    setOutputActions(JSON.stringify(artifact.content, null, 2), "topic-options.json");
  } else if (artifact.kind === "EVIDENCE_PACK") {
    renderEvidenceArtifact(preview, artifact.content);
    setOutputActions(JSON.stringify(artifact.content, null, 2), "reference-material.json");
  } else if (artifact.kind === "CONTENT_OUTLINE") {
    renderOutlineArtifact(preview, artifact.content);
    setOutputActions(JSON.stringify(artifact.content, null, 2), "content-outline.json");
  } else if (artifact.kind === "CRITIQUE") {
    renderCritique(preview, artifact.content);
    setOutputActions(JSON.stringify(artifact.content, null, 2), "critique.json");
  } else if (artifact.kind === "EVALUATION_REPORT") {
    renderMetrics(preview, artifact.content);
    setOutputActions(JSON.stringify(artifact.content, null, 2), "evaluation.json");
  } else {
    const json = node("pre", "json-view");
    json.textContent = JSON.stringify(artifact.content, null, 2);
    preview.append(json);
    setOutputActions(JSON.stringify(artifact.content, null, 2), `${artifact.kind}.json`);
  }
  els.outputBody.append(preview);
}

function artifactDocument(artifact) {
  const content = artifact?.content || {};
  if (artifact?.kind === "FINAL_CONTENT") return content.document || null;
  if (["DRAFT", "SOURCE_DRAFT"].includes(artifact?.kind)) return content;
  return null;
}

function outlineToMarkdown(content) {
  const parts = [];
  if (content.thesis) parts.push(`> ${content.thesis}`);
  for (const section of content.sections || []) {
    parts.push(`## ${section.heading || "未命名章节"}`);
    if (section.purpose) parts.push(section.purpose);
    if (section.key_points?.length) {
      parts.push(section.key_points.map((point) => `- ${point}`).join("\n"));
    }
  }
  if (content.call_to_action) {
    parts.push(`## 下一步\n${content.call_to_action}`);
  }
  return parts.join("\n\n");
}

async function resolveDocumentSource() {
  if (state.selectedDraft) {
    return {
      sourceKey: `draft:${state.selectedDraft.draft_id}:${state.selectedDraft.current_version}`,
      sourceArtifactId: state.selectedDraft.version?.source_artifact_id || null,
      title: state.selectedDraft.title || "",
      markdown: state.selectedDraft.version?.content_markdown || "",
      label: `草稿箱 · 第 ${state.selectedDraft.current_version} 版`
    };
  }

  const preferredKinds = ["FINAL_CONTENT", "DRAFT", "SOURCE_DRAFT", "CONTENT_OUTLINE"];
  let summary = state.documentRequestedArtifactId
    ? state.artifacts.find(
      (item) => item.artifact_id === state.documentRequestedArtifactId
    )
    : null;
  if (state.snapshot?.final_artifact_id) {
    summary = state.artifacts.find(
      (item) => item.artifact_id === state.snapshot.final_artifact_id
    ) || { artifact_id: state.snapshot.final_artifact_id, kind: "FINAL_CONTENT" };
  }
  if (!summary) {
    for (const kind of preferredKinds) {
      summary = [...state.artifacts].reverse().find((item) => item.kind === kind);
      if (summary) break;
    }
  }
  if (!summary) return null;

  const detail = await getArtifactDetail(summary.artifact_id);
  if (!detail) return null;
  const document = artifactDocument(detail);
  if (document) {
    const labels = {
      FINAL_CONTENT: "最终成稿",
      DRAFT: "助手初稿",
      SOURCE_DRAFT: "原始草稿"
    };
    return {
      sourceKey: `artifact:${detail.artifact_id}`,
      sourceArtifactId: detail.artifact_id,
      title: document.title || "",
      markdown: document.body_markdown || "",
      label: `${labels[detail.kind] || "正文"} · 第 ${detail.revision} 版`
    };
  }
  if (detail.kind === "CONTENT_OUTLINE") {
    return {
      sourceKey: `artifact:${detail.artifact_id}`,
      sourceArtifactId: detail.artifact_id,
      title: detail.content?.title || "",
      markdown: outlineToMarkdown(detail.content || {}),
      label: `文章大纲 · 第 ${detail.revision} 版`
    };
  }
  return null;
}

async function renderDocument() {
  if (!state.documentEditor) return;
  const source = await resolveDocumentSource();
  const taskGoal = state.snapshot?.goal || "创作工作台";
  els.topDocumentLabel.textContent = source?.title || taskGoal;
  els.documentPlaceholder.hidden = Boolean(source);
  els.saveDocumentVersion.disabled = !source;
  els.documentTitleInput.disabled = !source;
  state.documentEditor.setEditable(Boolean(source));

  if (!source) {
    state.documentSourceKey = null;
    state.documentSourceArtifactId = null;
    state.editorDirty = false;
    els.documentTitleInput.value = "";
    resizeDocumentTitle();
    state.documentEditor.setMarkdown("");
    els.editorWordCount.textContent = "0 字";
    els.editorSaveState.textContent = "等待正文";
    els.editorSaveState.classList.remove("is-dirty");
    els.editorSourceLabel.textContent = "助手正在准备内容";
    els.topSaveState.textContent = "正文生成后可以直接编辑";
    setOutputActions("", "");
    return;
  }

  if (state.documentSourceKey === source.sourceKey && state.editorDirty) {
    updateEditorOutputState();
    return;
  }

  const scratch = readDocumentScratch();
  const restoreScratch = scratch?.source_key === source.sourceKey;
  state.documentSourceKey = source.sourceKey;
  state.documentSourceArtifactId =
    restoreScratch ? scratch.source_artifact_id : source.sourceArtifactId;
  state.documentTitle = restoreScratch ? scratch.title : source.title;
  els.documentTitleInput.value = state.documentTitle || "";
  resizeDocumentTitle();
  state.documentEditor.setMarkdown(
    restoreScratch ? scratch.body_markdown : source.markdown
  );
  const count = state.documentEditor.wordCount();
  els.editorWordCount.textContent = `${count.characters} 字`;
  els.editorSourceLabel.textContent = source.label;
  state.editorDirty = Boolean(restoreScratch);
  els.editorSaveState.textContent = restoreScratch ? "已恢复本机修改" : "尚未修改";
  els.editorSaveState.classList.toggle("is-dirty", Boolean(restoreScratch));
  els.topSaveState.textContent = restoreScratch
    ? "已恢复尚未保存的修改"
    : "当前版本已同步";
  updateEditorOutputState();
}

function appendSourceItem(container, item, compact = false) {
  const block = node("article", compact ? "material-item" : "source-item");
  const icon = node("span", "source-item-icon");
  const iconNode = node("i");
  iconNode.dataset.lucide = item.url ? "link-2" : "file-text";
  icon.append(iconNode);
  const body = node("span", "source-item-body");
  body.append(node("strong", "", item.title || "参考素材"));
  if (item.summary && !compact) body.append(node("span", "", item.summary));
  if (item.source) body.append(node("small", "", item.source));
  block.append(icon, body);
  container.append(block);
}

async function renderContextPanels() {
  clear(els.sourcePanel);
  clear(els.qualityPanel);
  if (!state.snapshot) {
    els.sourcePanel.append(node("div", "pane-empty", "选择作品后查看素材"));
    els.qualityPanel.append(node("div", "pane-empty", "成稿后会显示质量建议"));
    return;
  }

  const latest = (kind) =>
    [...state.artifacts].reverse().find((artifact) => artifact.kind === kind);
  const evidenceSummary = latest("EVIDENCE_PACK");
  const critiqueSummary = latest("CRITIQUE");
  const evaluationSummary = latest("EVALUATION_REPORT");
  const documentSummary =
    latest("FINAL_CONTENT") || latest("DRAFT") || latest("SOURCE_DRAFT");
  const [evidenceDetail, qualityDetail, documentDetail] = await Promise.all([
    evidenceSummary ? getArtifactDetail(evidenceSummary.artifact_id) : null,
    evaluationSummary
      ? getArtifactDetail(evaluationSummary.artifact_id)
      : critiqueSummary
        ? getArtifactDetail(critiqueSummary.artifact_id)
        : null,
    documentSummary ? getArtifactDetail(documentSummary.artifact_id) : null
  ]);

  const references = String(
    state.snapshot.constraints?.reference_notes || ""
  ).trim();
  const evidence = evidenceDetail?.content?.evidence || [];
  if (references) {
    const item = {
      title: "你提供的创作素材",
      summary: references,
      source: "本次创作简报"
    };
    appendSourceItem(els.sourcePanel, item);
  }
  for (const item of evidence) {
    appendSourceItem(els.sourcePanel, item);
  }
  const citations = artifactDocument(documentDetail)?.citations || [];
  if (citations.length) {
    const citationGroup = node("section", "citation-group");
    citationGroup.append(
      node("strong", "", `${citations.length} 条正文引用`)
    );
    for (const citation of citations) {
      const button = node("button", "citation-item");
      button.type = "button";
      const body = node("span");
      body.append(
        node("strong", "", citation.claim_text),
        node(
          "small",
          "",
          citation.source_title || citation.evidence_id
        )
      );
      const locate = node("i");
      locate.dataset.lucide = "search";
      button.append(body, locate);
      button.addEventListener("click", () => {
        const found = state.documentEditor?.findText(citation.claim_text);
        switchMobilePane("editor");
        showToast(found ? "已定位到引用位置" : "当前版本中未找到这条主张");
      });
      citationGroup.append(button);
    }
    els.sourcePanel.prepend(citationGroup);
  }
  if (!references && !evidence.length && !citations.length) {
    const materialEmpty = node(
      "div",
      "source-empty",
      "这篇内容暂时没有可引用素材。助手会把缺少证据的地方明确标出来。"
    );
    els.sourcePanel.append(materialEmpty);
  } else {
    const sourceSummary = node(
      "p",
      "source-summary",
      `本次创作已连接 ${evidence.length + (references ? 1 : 0)} 组上下文，正文含 ${citations.length} 条可定位引用`
    );
    els.sourcePanel.prepend(sourceSummary);
  }

  if (!qualityDetail) {
    els.qualityPanel.append(
      node(
        "div",
        "source-empty",
        state.snapshot.status === "COMPLETED"
          ? "本次成稿没有单独的质量报告。"
          : "成稿后，助手会在这里给出事实、结构和表达建议。"
      )
    );
  } else if (qualityDetail.kind === "EVALUATION_REPORT") {
    renderMetrics(els.qualityPanel, qualityDetail.content || {});
  } else {
    renderCritique(els.qualityPanel, qualityDetail.content || {});
  }
  renderFeedbackSummary();
  refreshIcons();
}

function renderFeedbackSummary() {
  if (!state.snapshot || !state.feedbackSummary) return;
  const feedback = state.feedbackSummary;
  const panel = node("section", "feedback-summary");
  const decided =
    Number(feedback.accepted_suggestions || 0) +
    Number(feedback.rejected_suggestions || 0);
  panel.append(
    node("strong", "", "你的协作偏好"),
    node(
      "p",
      "",
      decided
        ? `已采纳 ${feedback.accepted_suggestions} 条建议，拒绝 ${feedback.rejected_suggestions} 条`
        : "接受或拒绝建议后，助手会据此调整后续协作"
    )
  );
  const rating = node("div", "rating-actions");
  rating.append(node("span", "", "这次结果有帮助吗？"));
  for (const [score, iconName, label] of [
    [1, "thumbs-up", "有帮助"],
    [0, "thumbs-down", "没帮助"]
  ]) {
    const button = node("button", "icon-button");
    button.type = "button";
    button.title = label;
    button.setAttribute("aria-label", label);
    const icon = node("i");
    icon.dataset.lucide = iconName;
    button.append(icon);
    button.addEventListener("click", () => submitRating(score, button));
    rating.append(button);
  }
  panel.append(rating);
  els.qualityPanel.append(panel);
}

async function submitRating(score, button) {
  if (!state.snapshot) return;
  button.disabled = true;
  try {
    await apiJson(`${API_ROOT}/feedback`, {
      method: "POST",
      body: JSON.stringify({
        task_id: state.snapshot.task_id,
        draft_id: state.selectedDraft?.draft_id || null,
        score,
        reason: ""
      })
    });
    state.feedbackSummary = await apiJson(`${API_ROOT}/feedback/summary`);
    showToast("已记录这次反馈");
    await renderContextPanels();
  } catch (error) {
    showToast(`反馈提交失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

function renderMarkdown(container, markdown) {
  let list = null;
  const flushList = () => {
    if (list) {
      container.append(list);
      list = null;
    }
  };
  for (const rawLine of String(markdown).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) {
      flushList();
      continue;
    }
    if (line.startsWith("### ")) {
      flushList();
      container.append(node("h3", "", line.slice(4)));
    } else if (line.startsWith("## ")) {
      flushList();
      container.append(node("h2", "", line.slice(3)));
    } else if (line.startsWith("# ")) {
      flushList();
      container.append(node("h2", "", line.slice(2)));
    } else if (/^[-*]\s+/.test(line)) {
      if (!list) list = node("ul");
      list.append(node("li", "", line.replace(/^[-*]\s+/, "")));
    } else if (/^\d+[.)]\s+/.test(line)) {
      if (!list || list.tagName !== "OL") {
        flushList();
        list = node("ol");
      }
      list.append(node("li", "", line.replace(/^\d+[.)]\s+/, "")));
    } else {
      flushList();
      container.append(node("p", "", line));
    }
  }
  flushList();
}

function renderTopicArtifact(container, content) {
  container.append(node("h2", "", "选题方案"));
  if (content.recommendation_reason) {
    container.append(node("p", "", content.recommendation_reason));
  }
  for (const option of content.options || []) {
    const block = node("section", "outline-section");
    const titleBits = [option.title];
    if (option.recommendation) {
      titleBits.push(recommendationLabel(option.recommendation));
    }
    if (option.id === content.recommended_option_id) {
      titleBits.push("推荐");
    }
    block.append(node("strong", "", titleBits.join(" · ")));
    if (option.reader_question) {
      block.append(node("p", "", `读者问：${option.reader_question}`));
    }
    if (option.angle) block.append(node("p", "", option.angle));
    if (option.why_now) block.append(node("p", "", `为何现在：${option.why_now}`));
    if (option.differentiation) {
      block.append(node("p", "", `差异化：${option.differentiation}`));
    }
    if (option.audience_value) block.append(node("p", "", option.audience_value));
    container.append(block);
  }
}

function renderEvidenceArtifact(container, content) {
  container.append(node("h2", "", "参考素材"));
  if (content.research_question) {
    container.append(node("p", "artifact-lead", content.research_question));
  }
  const evidence = content.evidence || [];
  for (const item of evidence) {
    const block = node("section", "outline-section");
    block.append(node("strong", "", item.title || "参考内容"));
    if (item.summary) block.append(node("p", "", item.summary));
    if (item.source) {
      block.append(node("small", "topic-evidence", `来源：${item.source}`));
    }
    container.append(block);
  }
  if (!evidence.length) {
    container.append(
      node(
        "p",
        "pane-empty",
        "素材库里暂时没有与这个主题直接相关、可放心引用的内容。助手不会拿热门但无关的文章凑数。"
      )
    );
  }
  appendStringList(
    container,
    evidence.length ? "建议补充" : "写作前可补充",
    consumerEvidenceGaps(content)
  );
}

function consumerEvidenceGaps(content) {
  const question = String(content.research_question || "").trim();
  const rawGaps = content.search_gaps || [];
  const useful = [];
  let sourceUnavailable = false;
  for (const value of rawGaps) {
    const gap = String(value || "").trim();
    if (!gap) continue;
    if (
      /QDRANT|retrieval budget|retrieval source|source query/i.test(gap)
    ) {
      sourceUnavailable = true;
      continue;
    }
    if (question && (gap === question || gap.startsWith(`${question} `))) {
      continue;
    }
    useful.push(gap);
  }
  if (!(content.evidence || []).length) {
    useful.push(
      "一段亲自使用或团队落地的真实案例",
      "产品能力、数据和外部结论对应的可核验来源"
    );
  } else if (sourceUnavailable) {
    useful.push("部分素材渠道暂不可用，发布前请核验关键外部事实");
  }
  return [...new Set(useful)];
}

function recommendationLabel(value) {
  switch (String(value || "").toUpperCase()) {
    case "WRITE_NOW":
      return "方向成熟";
    case "WRITE_LATER":
      return "建议补充素材";
    case "SKIP":
      return "不建议采用";
    default:
      return value;
  }
}

function renderOutlineArtifact(container, content) {
  container.append(node("h2", "", content.title || "文章大纲"));
  if (content.thesis) container.append(node("p", "", content.thesis));
  for (const section of content.sections || []) {
    const block = node("section", "outline-section");
    block.append(node("strong", "", section.heading || "未命名章节"));
    if (section.purpose) block.append(node("p", "", section.purpose));
    const list = node("ul");
    for (const point of section.key_points || []) list.append(node("li", "", point));
    if (list.childNodes.length) block.append(list);
    container.append(block);
  }
}

function renderCritique(container, content) {
  const verdict = String(content.verdict || "").toUpperCase() === "ACCEPT"
    ? "可以成稿"
    : "建议继续优化";
  container.append(node("h2", "", `内容检查 · ${verdict}`));
  const scores = content.scores || {};
  const table = metricTable(
    Object.entries(scores).map(([metric, score]) => ({
      metric: {
        relevance: "主题相关度",
        structure: "结构完整度",
        evidence: "证据支撑",
        style: "表达质量",
        overall: "综合质量"
      }[metric] || metric,
      score,
      status: Number(score) >= 0.75 ? "通过" : "待优化"
    }))
  );
  container.append(table);
  appendStringList(container, "优势", content.strengths);
  appendStringList(container, "问题", content.issues);
  appendStringList(container, "修订要求", content.revision_instructions);
}

function renderMetrics(container, content) {
  container.append(
    node(
      "h2",
      "",
      `成稿质量 · ${Math.round(Number(content.quality_score || 0) * 100)} 分`
    )
  );
  container.append(metricTable(content.metrics || []));
  appendStringList(container, "生成观察", content.generation_observations);
  appendStringList(container, "规划观察", content.planning_observations);
  appendStringList(container, "未评估指标", content.unevaluated_metrics);
}

function metricTable(metrics) {
  const table = node("table", "metric-table");
  const head = node("thead");
  const headRow = node("tr");
  for (const label of ["指标", "得分", "状态"]) {
    headRow.append(node("th", "", label));
  }
  head.append(headRow);
  table.append(head);
  const body = node("tbody");
  for (const metric of metrics) {
    const row = node("tr");
    const rawScore = metric.score;
    const score = rawScore === null || rawScore === undefined
      ? "—"
      : Number(rawScore).toFixed(2);
    row.append(
      node("td", "", METRIC_LABELS[metric.metric] || metric.metric || "质量指标"),
      node("td", "metric-score", score),
      node(
        "td",
        "",
        metric.passed === true
          ? "通过"
          : metric.passed === false
            ? "待优化"
            : metric.status === "SCORED"
              ? "已评估"
              : metric.status || "—"
      )
    );
    body.append(row);
  }
  table.append(body);
  return table;
}

function appendStringList(container, title, values) {
  if (!values?.length) return;
  const block = node("section", "outline-section");
  block.append(node("strong", "", title));
  const list = node("ul");
  for (const value of values) list.append(node("li", "", String(value)));
  block.append(list);
  container.append(block);
}

async function loadDrafts() {
  if (!state.selectedTaskId) {
    state.drafts = [];
    return;
  }
  state.drafts = await apiJson(
    `${API_ROOT}/tasks/${state.selectedTaskId}/drafts`
  );
}

async function handoffPublication() {
  if (!state.selectedTaskId) return;
  try {
    const result = await apiJson(
      `${API_ROOT}/tasks/${state.selectedTaskId}/publication-handoffs`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey("publication-handoff") },
        body: JSON.stringify({})
      }
    );
    showToast(
      result.replayed
        ? "发布草稿已经存在，已为你打开"
        : "已创建发布草稿"
    );
    if (
      result.external_draft_id &&
      window.parent !== window
    ) {
      let parentOrigin = "*";
      try {
        parentOrigin = new URL(document.referrer).origin;
      } catch {
        // Embedded deployments without a referrer still rely on host-side origin checks.
      }
      window.parent.postMessage(
        {
          type: "greenbook.creator.handoff",
          draftId: String(result.external_draft_id)
        },
        parentOrigin
      );
    }
    state.outputTab = "drafts";
    await loadDrafts();
    await renderOutput();
  } catch (error) {
    showToast(`创建发布草稿失败：${error.message}`, true);
  }
}

async function renderDrafts() {
  els.outputTitle.textContent = "草稿箱";
  clear(els.outputBody);
  setOutputActions("", "");
  const finalArtifact = await currentFinalArtifact();
  if (finalArtifact) {
    const actions = node("div", "draft-actions");
    const handoffButton = node(
      "button",
      "button button-primary draft-primary-action",
      "创建发布草稿"
    );
    handoffButton.type = "button";
    handoffButton.addEventListener("click", () => handoffPublication());
    const createButton = node(
      "button",
      "button button-secondary draft-primary-action",
      "保存可编辑副本"
    );
    createButton.type = "button";
    createButton.addEventListener("click", () => openDraftEditor(null, finalArtifact));
    actions.append(handoffButton, createButton);
    els.outputBody.append(actions);
    try {
      const handoffs = await apiJson(
        `${API_ROOT}/tasks/${state.selectedTaskId}/publication-handoffs`
      );
      if (handoffs.length) {
        const panel = node("details", "runtime-artifacts publication-details");
        panel.append(node("summary", "", "查看发布来源"));
        const panelBody = node("div", "publication-details-body");
        for (const item of handoffs) {
          panelBody.append(
            node("small", "", `发布草稿 ${item.external_draft_id}`),
            node("small", "topic-evidence", `来源版本 ${item.source_artifact_revision}`)
          );
        }
        panel.append(panelBody);
        els.outputBody.append(panel);
      }
    } catch (_error) {
      // ignore handoff listing failures in draft pane
    }
  }
  if (!state.drafts.length) {
    els.outputBody.append(node("div", "pane-empty", "还没有保存到草稿箱的内容"));
    return;
  }
  const list = node("div", "draft-list");
  for (const draft of state.drafts) {
    const row = node("button", "draft-row");
    row.type = "button";
    if (state.selectedDraft?.draft_id === draft.draft_id) {
      row.classList.add("is-active");
    }
    const label = node("span");
    label.append(
      node("strong", "", draft.title),
      node("span", "", `第 ${draft.current_version} 版`)
    );
    row.append(label, node("span", "", formatCompactTime(draft.updated_at)));
    row.addEventListener("click", () => selectDraft(draft.draft_id));
    list.append(row);
  }
  els.outputBody.append(list);
  if (state.selectedDraft) renderDraftPreview();
}

async function selectDraft(draftId) {
  const [draft, versions, suggestions, branches, channelVariants] = await Promise.all([
    apiJson(`${API_ROOT}/drafts/${draftId}`),
    apiJson(`${API_ROOT}/drafts/${draftId}/versions`),
    apiJson(`${API_ROOT}/drafts/${draftId}/suggestions`),
    apiJson(`${API_ROOT}/drafts/${draftId}/branches`),
    apiJson(`${API_ROOT}/drafts/${draftId}/channel-variants`)
  ]);
  state.selectedDraft = draft;
  state.draftVersions = versions;
  state.suggestions = suggestions;
  state.currentSuggestion =
    suggestions.find((item) => item.status === "PENDING") || null;
  state.branches = branches;
  state.channelVariants = channelVariants;
  state.documentRequestedArtifactId = null;
  state.documentSourceKey = null;
  renderSuggestionPanel();
  await renderDrafts();
  await renderDocument();
}

function renderDraftPreview() {
  const draft = state.selectedDraft;
  if (!draft) return;
  const view = node("article", "draft-view");
  const meta = node("div", "artifact-meta");
  meta.append(
    node("span", "", `第 ${draft.current_version} 版`),
    node("span", "", draft.status === "DRAFT" ? "草稿" : draft.status),
    node("span", "", formatTime(draft.updated_at))
  );
  view.append(meta, node("h2", "", draft.title));
  const markdown = node("div", "markdown-view");
  renderMarkdown(markdown, draft.version.content_markdown);
  view.append(markdown);
  const actions = node("div", "draft-preview-actions");
  const edit = node("button", "button button-secondary", "编辑新版本");
  edit.type = "button";
  edit.addEventListener("click", () => openDraftEditor(draft, null));
  const branch = node("button", "button button-secondary", "创建分支");
  branch.type = "button";
  branch.addEventListener("click", () => createDraftBranch());
  const channel = node("button", "button button-primary", "生成渠道稿");
  channel.type = "button";
  channel.addEventListener("click", () => els.channelDialog.showModal());
  actions.append(edit, branch, channel);
  view.append(actions);
  const versions = node("ul", "version-list");
  for (const version of state.draftVersions) {
    const item = node("li");
    item.append(
      node(
        "span",
        "",
        `第 ${version.version} 版 · ${EDITOR_LABELS[version.editor_type] || "助手生成"}`
      ),
      node("time", "", formatCompactTime(version.created_at))
    );
    versions.append(item);
  }
  view.append(versions);
  if (state.branches.length) {
    const branchList = node("section", "branch-list");
    branchList.append(node("strong", "", "内容分支"));
    for (const item of state.branches) {
      const button = node("button", "branch-row");
      button.type = "button";
      const icon = node("i");
      icon.dataset.lucide = "git-branch";
      button.append(
        icon,
        node("span", "", item.name),
        node("small", "", `源自第 ${item.source_version} 版`)
      );
      button.addEventListener("click", () => selectDraft(item.draft_id));
      branchList.append(button);
    }
    view.append(branchList);
  }
  els.outputBody.append(view);
  setOutputActions(
    `${draft.title}\n\n${draft.version.content_markdown}`,
    `${safeFilename(draft.title)}.md`
  );
}

async function createDraftBranch() {
  const draft = state.selectedDraft;
  if (!draft) {
    showToast("请先保存正文版本", true);
    return;
  }
  const name = `方案分支 ${new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date())}`;
  try {
    const result = await apiJson(
      `${API_ROOT}/drafts/${draft.draft_id}/branches`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey("draft-branch") },
        body: JSON.stringify({
          source_version: draft.current_version,
          name
        })
      }
    );
    await loadDrafts();
    await selectDraft(result.draft.draft_id);
    showToast("已创建独立分支，原版本保持不变");
  } catch (error) {
    showToast(`创建分支失败：${error.message}`, true);
  }
}

async function renderChannelVariants() {
  els.outputTitle.textContent = "渠道稿";
  clear(els.outputBody);
  if (!state.selectedDraft) {
    els.outputBody.append(
      node("div", "pane-empty", "先把正文保存为草稿版本，再生成渠道稿")
    );
    setOutputActions("", "");
    return;
  }
  const create = node("button", "button button-primary channel-create-button");
  create.type = "button";
  create.textContent = "生成渠道稿";
  create.addEventListener("click", () => els.channelDialog.showModal());
  els.outputBody.append(create);
  if (!state.channelVariants.length) {
    els.outputBody.append(node("div", "pane-empty", "还没有渠道版本"));
    setOutputActions("", "");
    return;
  }
  for (const variant of state.channelVariants) {
    const view = node("article", "channel-variant");
    const header = node("header");
    header.append(
      node("strong", "", CHANNEL_LABELS[variant.channel] || variant.channel),
      node("small", "", `基于正文第 ${variant.draft_version} 版`)
    );
    const copy = node("button", "icon-button");
    copy.type = "button";
    copy.title = "复制渠道稿";
    copy.setAttribute("aria-label", "复制渠道稿");
    const icon = node("i");
    icon.dataset.lucide = "copy";
    copy.append(icon);
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(
        `${variant.title}\n\n${variant.content_markdown}`
      );
      showToast("渠道稿已复制");
    });
    header.append(copy);
    view.append(header, node("h3", "", variant.title));
    const markdown = node("div", "markdown-view");
    renderMarkdown(markdown, variant.content_markdown);
    view.append(markdown, node("p", "adaptation-note", variant.adaptation_note));
    els.outputBody.append(view);
  }
  const latest = state.channelVariants[0];
  setOutputActions(
    `${latest.title}\n\n${latest.content_markdown}`,
    `${safeFilename(latest.title)}-${latest.channel}.md`
  );
  refreshIcons(els.outputBody);
}

async function createChannelVariant(event) {
  event.preventDefault();
  const draft = state.selectedDraft;
  if (!draft) return;
  els.channelSubmit.disabled = true;
  try {
    const variant = await apiJson(
      `${API_ROOT}/drafts/${draft.draft_id}/channel-variants`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey("channel-variant") },
        body: JSON.stringify({
          expected_version: draft.current_version,
          channel: els.channelTarget.value,
          instruction: els.channelInstruction.value.trim()
        })
      }
    );
    state.channelVariants.unshift(variant);
    els.channelDialog.close();
    els.channelForm.reset();
    state.outputTab = "channels";
    await renderOutput();
    showToast("渠道稿已生成，原稿没有被改动");
  } catch (error) {
    showToast(`渠道稿生成失败：${error.message}`, true);
  } finally {
    els.channelSubmit.disabled = false;
  }
}

async function currentFinalArtifact() {
  const id = state.snapshot?.final_artifact_id;
  return id ? getArtifactDetail(id) : null;
}

async function renderEvaluation() {
  els.outputTitle.textContent = "内容质量";
  clear(els.outputBody);
  const evaluations = state.artifacts.filter(
    (artifact) => artifact.kind === "EVALUATION_REPORT"
  );
  if (!evaluations.length) {
    els.outputBody.append(node("div", "pane-empty", "成稿后会在这里显示质量检查结果"));
    setOutputActions("", "");
    return;
  }
  const latest = evaluations[evaluations.length - 1];
  const detail = await getArtifactDetail(latest.artifact_id);
  const view = node("article", "evaluation-view");
  renderMetrics(view, detail.content);
  els.outputBody.append(view);
  setOutputActions(JSON.stringify(detail.content, null, 2), "evaluation.json");
}

function setOutputActions(text, filename) {
  state.outputText = text;
  state.outputFilename = filename || "mindflow-creator.txt";
  els.copyOutput.hidden = !text;
  els.downloadOutput.hidden = !text;
}

function openDraftEditor(draft, artifact) {
  const document = artifact ? artifactDocument(artifact) : null;
  state.selectedDraft = draft;
  state.editorSourceArtifactId =
    draft?.version?.source_artifact_id || artifact?.artifact_id || null;
  els.draftDialogTitle.textContent = draft ? "编辑草稿" : "保存到草稿箱";
  els.draftTitleInput.value = draft?.title || document?.title || "";
  els.draftBodyInput.value =
    draft?.version?.content_markdown || document?.body_markdown || "";
  els.draftVersionLabel.textContent = draft
    ? `当前第 ${draft.current_version} 版`
    : "新草稿";
  els.draftDialog.showModal();
}

async function saveDraft(event) {
  event.preventDefault();
  if (!state.selectedTaskId) return;
  const title = els.draftTitleInput.value.trim();
  const content = els.draftBodyInput.value.trim();
  if (!title || !content) {
    showToast("标题和正文不能为空", true);
    return;
  }
  els.draftSubmit.disabled = true;
  try {
    if (state.selectedDraft) {
      state.selectedDraft = await apiJson(
        `${API_ROOT}/drafts/${state.selectedDraft.draft_id}/versions`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey("draft-update") },
          body: JSON.stringify({
            expected_version: state.selectedDraft.current_version,
            title,
            content_markdown: content,
            source_artifact_id: state.editorSourceArtifactId
          })
        }
      );
    } else {
      state.selectedDraft = await apiJson(
        `${API_ROOT}/tasks/${state.selectedTaskId}/drafts`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey("draft-create") },
          body: JSON.stringify({
            title,
            content_markdown: content,
            source_artifact_id: state.editorSourceArtifactId
          })
        }
      );
    }
    els.draftDialog.close();
    await loadDrafts();
    await selectDraft(state.selectedDraft.draft_id);
    showToast("草稿版本已保存");
  } catch (error) {
    showToast(`草稿保存失败：${error.message}`, true);
  } finally {
    els.draftSubmit.disabled = false;
  }
}

async function saveDocumentVersion(options = {}) {
  if (!state.selectedTaskId || !state.documentEditor) return;
  const title = els.documentTitleInput.value.trim();
  const content = state.documentEditor.getMarkdown().trim();
  if (!title || !content) {
    showToast("标题和正文不能为空", true);
    return;
  }

  els.saveDocumentVersion.disabled = true;
  els.editorSaveState.textContent = "正在保存";
  try {
    const editingSelectedDraft =
      state.selectedDraft &&
      state.documentSourceKey?.startsWith(`draft:${state.selectedDraft.draft_id}:`);
    if (editingSelectedDraft) {
      state.selectedDraft = await apiJson(
        `${API_ROOT}/drafts/${state.selectedDraft.draft_id}/versions`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey("editor-draft-update") },
          body: JSON.stringify({
            expected_version: state.selectedDraft.current_version,
            title,
            content_markdown: content,
            source_artifact_id: state.documentSourceArtifactId
          })
        }
      );
    } else {
      state.selectedDraft = await apiJson(
        `${API_ROOT}/tasks/${state.selectedTaskId}/drafts`,
        {
          method: "POST",
          headers: { "Idempotency-Key": idempotencyKey("editor-draft-create") },
          body: JSON.stringify({
            title,
            content_markdown: content,
            source_artifact_id: state.documentSourceArtifactId
          })
        }
      );
    }

    clearDocumentScratch();
    const draftId = state.selectedDraft.draft_id;
    const [draft, versions, suggestions, branches, channelVariants] = await Promise.all([
      apiJson(`${API_ROOT}/drafts/${draftId}`),
      apiJson(`${API_ROOT}/drafts/${draftId}/versions`),
      apiJson(`${API_ROOT}/drafts/${draftId}/suggestions`),
      apiJson(`${API_ROOT}/drafts/${draftId}/branches`),
      apiJson(`${API_ROOT}/drafts/${draftId}/channel-variants`)
    ]);
    state.selectedDraft = draft;
    state.draftVersions = versions;
    state.suggestions = suggestions;
    state.currentSuggestion =
      suggestions.find((item) => item.status === "PENDING") || null;
    state.branches = branches;
    state.channelVariants = channelVariants;
    renderSuggestionPanel();
    state.documentRequestedArtifactId = null;
    state.documentSourceKey = null;
    state.editorDirty = false;
    await loadDrafts();
    await renderDocument();
    if (state.outputTab === "drafts") await renderDrafts();
    els.editorSaveState.textContent = `已保存第 ${draft.current_version} 版`;
    els.editorSaveState.classList.remove("is-dirty");
    els.topSaveState.textContent = "当前版本已同步";
    if (!options.silent) showToast(`已保存第 ${draft.current_version} 版`);
    return draft;
  } catch (error) {
    els.editorSaveState.textContent = "保存失败，本机修改仍保留";
    if (!options.silent) showToast(`版本保存失败：${error.message}`, true);
    if (options.rethrow) throw error;
    return null;
  } finally {
    els.saveDocumentVersion.disabled = false;
  }
}

function improvementInstruction(action) {
  const instructions = {
    rewrite: "优化表达，使内容更清晰、自然、具体，避免空泛术语。",
    shorten: "精简表达，删除重复和低信息密度内容，但不要损失关键结论。",
    expand: "补充必要的解释、案例或执行细节，不要编造无法验证的事实。"
  };
  return instructions[action] || "按要求优化正文";
}

function renderSuggestionPanel() {
  clear(els.aiSuggestionPanel);
  const suggestion = state.currentSuggestion;
  els.aiSuggestionPanel.hidden = !suggestion;
  if (!suggestion) return;
  els.assistantStatusText.textContent = "修改建议待你决定";

  const header = node("header", "suggestion-header");
  const title = node("span");
  title.append(
    node(
      "strong",
      "",
      SUGGESTION_LABELS[suggestion.kind] || "AI 修改建议"
    ),
    node("small", "", `基于正文第 ${suggestion.base_version} 版`)
  );
  const status = node("span", "suggestion-status", "待处理");
  header.append(title, status);

  const diff = node("div", "suggestion-diff");
  const before = node("section", "diff-side is-before");
  before.append(
    node("small", "", "原文"),
    node("del", "", suggestion.original_text)
  );
  const after = node("section", "diff-side is-after");
  after.append(
    node("small", "", "建议"),
    node("ins", "", suggestion.replacement_text)
  );
  diff.append(before, after);

  const rationale = node("p", "suggestion-rationale", suggestion.rationale);
  const actions = node("div", "suggestion-actions");
  const reject = node("button", "button button-secondary", "不采用");
  reject.type = "button";
  reject.addEventListener("click", () => rejectSuggestion(suggestion.id));
  const accept = node("button", "button button-primary", "采用并保存新版本");
  accept.type = "button";
  accept.addEventListener("click", () => acceptSuggestion(suggestion.id));
  actions.append(reject, accept);
  els.aiSuggestionPanel.append(header, diff, rationale);
  if (suggestion.evidence_ids?.length) {
    const evidence = node("div", "suggestion-evidence");
    evidence.append(node("small", "", "依据"));
    for (const id of suggestion.evidence_ids) {
      evidence.append(node("span", "", id));
    }
    els.aiSuggestionPanel.append(evidence);
  }
  if (suggestion.risk_note) {
    els.aiSuggestionPanel.append(
      node("p", "suggestion-risk", suggestion.risk_note)
    );
  }
  els.aiSuggestionPanel.append(actions);
}

async function acceptSuggestion(suggestionId) {
  const buttons = els.aiSuggestionPanel.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const response = await apiJson(
      `${API_ROOT}/suggestions/${suggestionId}/accept`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey("suggestion-accept") }
      }
    );
    state.selectedDraft = response.draft;
    state.currentSuggestion = null;
    state.documentSourceKey = null;
    clearDocumentScratch();
    await loadDrafts();
    await selectDraft(response.draft.draft_id);
    state.feedbackSummary = await apiJson(`${API_ROOT}/feedback/summary`);
    els.assistantStatusText.textContent = "建议已采用，可继续编辑";
    showToast(`已采用建议并保存第 ${response.draft.current_version} 版`);
  } catch (error) {
    showToast(`采用建议失败：${error.message}`, true);
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function rejectSuggestion(suggestionId) {
  const buttons = els.aiSuggestionPanel.querySelectorAll("button");
  buttons.forEach((button) => { button.disabled = true; });
  try {
    await apiJson(`${API_ROOT}/suggestions/${suggestionId}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason: "" })
    });
    state.currentSuggestion = null;
    state.suggestions = state.suggestions.map((item) =>
      item.id === suggestionId ? { ...item, status: "REJECTED" } : item
    );
    renderSuggestionPanel();
    state.feedbackSummary = await apiJson(`${API_ROOT}/feedback/summary`);
    els.assistantStatusText.textContent = "已忽略建议，可继续编辑";
    showToast("已忽略这条建议");
  } catch (error) {
    showToast(`忽略建议失败：${error.message}`, true);
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

async function createEditorImprovement(instruction, action = "custom") {
  if (!state.selectedTaskId || !state.documentEditor) return;
  const title = els.documentTitleInput.value.trim();
  const markdown = state.documentEditor.getMarkdown().trim();
  const goal = String(instruction || "").trim();
  const selectedText = state.editorSelectedText;
  const selectedContext = { ...state.editorSelectionContext };
  if (!title || !markdown) {
    showToast("正文准备好后才能交给助手修改", true);
    return;
  }
  if (goal.length < 3) {
    showToast("请写清楚希望如何修改", true);
    return;
  }

  document.querySelectorAll("[data-editor-assist]").forEach((button) => {
    button.disabled = true;
  });
  els.submitEditorInstruction.disabled = true;
  els.assistantStatusText.textContent = "正在生成修改建议";
  try {
    let draft = state.selectedDraft;
    const editingCurrentDraft =
      draft &&
      state.documentSourceKey?.startsWith(`draft:${draft.draft_id}:`) &&
      !state.editorDirty;
    if (!editingCurrentDraft) {
      draft = await saveDocumentVersion({ silent: true, rethrow: true });
    }
    if (!draft) throw new Error("正文版本尚未保存");
    const originalText = selectedText || markdown;
    const suggestion = await apiJson(
      `${API_ROOT}/drafts/${draft.draft_id}/suggestions`,
      {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey("editor-suggestion") },
      body: JSON.stringify({
        expected_version: draft.current_version,
        kind: {
          rewrite: "REWRITE",
          shorten: "SHORTEN",
          expand: "EXPAND"
        }[action] || "CUSTOM",
        instruction: goal,
        original_text: originalText,
        prefix_context: selectedText ? selectedContext.prefixContext : "",
        suffix_context: selectedText ? selectedContext.suffixContext : ""
      })
      }
    );
    state.currentSuggestion = suggestion;
    state.suggestions.unshift(suggestion);
    els.editorInstruction.value = "";
    renderSelectionAssistant("");
    renderSuggestionPanel();
    switchAssistantTab("collaboration");
    switchMobilePane("assistant");
    els.assistantStatusText.textContent = "修改建议待你决定";
    showToast("修改建议已生成，请查看差异");
  } catch (error) {
    els.assistantStatusText.textContent = "修改建议生成失败";
    showToast(`生成建议失败：${error.message}`, true);
  } finally {
    document.querySelectorAll("[data-editor-assist]").forEach((button) => {
      button.disabled = false;
    });
    els.submitEditorInstruction.disabled = false;
  }
}

async function createTask(event) {
  event.preventDefault();
  const kind = document.querySelector('input[name="taskKind"]:checked')?.value;
  const interactionMode = document.querySelector(
    'input[name="interactionMode"]:checked'
  )?.value;
  const goal = els.taskGoalInput.value.trim();
  const audience = els.taskAudience.value.trim();
  const readerTakeaway = els.taskTakeaway.value.trim();
  if (!goal) {
    showToast("请填写你想创作的主题", true);
    return;
  }
  const keyPoints = els.taskKeyPoints.value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 20);
  const materialIds = [
    ...els.taskMaterialPicker.querySelectorAll("input:checked")
  ].map((input) => input.value);
  const projectId = els.taskProject.value || null;
  els.createTaskSubmit.disabled = true;
  try {
    const created = await apiJson(`${API_ROOT}/tasks`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey("task-create") },
      body: JSON.stringify({
        kind,
        goal,
        project_id: projectId,
        material_ids: materialIds,
        constraints: {
          language: els.taskLanguage.value,
          format: els.taskFormat.value,
          target_length: Number(els.taskLength.value),
          interaction_mode: interactionMode,
          audience,
          reader_takeaway: readerTakeaway,
          tone: els.taskTone.value,
          key_points: keyPoints,
          reference_notes: els.taskReferences.value.trim()
        },
        source_scope: {
          include_creator_profile: true,
          include_creator_history: els.includeHistory.checked,
          include_community_posts: els.includeCommunity.checked
        }
      })
    });
    state.projectFilterId = projectId || "";
    els.createTaskDialog.close();
    els.createTaskForm.reset();
    await loadStudioLibrary();
    await loadTasks(true);
    await selectTask(created.task_id);
    showToast("创作简报已提交，正在准备内容方向");
  } catch (error) {
    showToast(`任务创建失败：${error.message}`, true);
  } finally {
    els.createTaskSubmit.disabled = false;
  }
}

async function cancelTask() {
  const snapshot = state.snapshot;
  if (!snapshot || !window.confirm("确认取消当前任务？")) return;
  els.cancelTask.disabled = true;
  try {
    await apiJson(`${API_ROOT}/tasks/${snapshot.task_id}/cancel`, {
      method: "POST",
      body: JSON.stringify({ expected_task_version: snapshot.version })
    });
    await refreshTask(true);
    await loadTasks(true);
    showToast("任务已取消");
  } catch (error) {
    showToast(`取消失败：${error.message}`, true);
  } finally {
    els.cancelTask.disabled = false;
  }
}

async function retryTask() {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  els.retryTask.disabled = true;
  try {
    await apiJson(`${API_ROOT}/tasks/${snapshot.task_id}/retry`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey("task-retry") },
      body: JSON.stringify({ expected_task_version: snapshot.version })
    });
    await refreshTask(true);
    startEventStream(snapshot.task_id);
    showToast("任务已进入重试队列");
  } catch (error) {
    showToast(`重试失败：${error.message}`, true);
  } finally {
    els.retryTask.disabled = false;
  }
}

async function switchOutputTab(tab) {
  state.outputTab = tab;
  await renderOutput();
  updateEditorOutputState();
}

function switchLibraryView(view) {
  state.libraryView = view;
  document.querySelectorAll("[data-library-view]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.libraryView === view);
  });
  document.querySelectorAll("[data-library-panel]").forEach((panel) => {
    const active = panel.dataset.libraryPanel === view;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });
}

function switchAssistantTab(tab) {
  state.assistantTab = tab;
  document.querySelectorAll("[data-assistant-tab]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.assistantTab === tab);
  });
  document.querySelectorAll("[data-assistant-panel]").forEach((panel) => {
    const active = panel.dataset.assistantPanel === tab;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });
}

function switchMobilePane(pane) {
  const target = ["library", "editor", "assistant"].includes(pane)
    ? pane
    : "editor";
  document.body.dataset.mobilePane = target;
  document.querySelectorAll("[data-mobile-target]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.mobileTarget === target);
  });
  if (target === "editor") {
    window.requestAnimationFrame(resizeDocumentTitle);
  }
}

function openCreateDialog(projectId = "") {
  if (typeof projectId !== "string") projectId = "";
  if (!projectId) projectId = state.projectFilterId;
  renderTaskLibraryFields();
  if (
    projectId &&
    [...els.taskProject.options].some((item) => item.value === projectId)
  ) {
    els.taskProject.value = projectId;
  }
  els.createTaskDialog.showModal();
  window.setTimeout(() => els.taskGoalInput.focus(), 0);
}

async function createProject(event) {
  event.preventDefault();
  const name = els.projectName.value.trim();
  if (!name) return;
  els.projectSubmit.disabled = true;
  try {
    const project = await apiJson(`${API_ROOT}/projects`, {
      method: "POST",
      body: JSON.stringify({
        name,
        description: els.projectDescription.value.trim()
      })
    });
    state.projects.unshift(project);
    els.projectDialog.close();
    els.projectForm.reset();
    renderProjectLibrary();
    renderTaskLibraryFields();
    showToast("项目已创建");
  } catch (error) {
    showToast(`项目创建失败：${error.message}`, true);
  } finally {
    els.projectSubmit.disabled = false;
  }
}

async function createMaterial(event) {
  event.preventDefault();
  const title = els.materialTitle.value.trim();
  const content = els.materialContent.value.trim();
  if (!title || !content) {
    showToast("请填写素材标题和正文", true);
    return;
  }
  els.materialSubmit.disabled = true;
  try {
    await apiJson(`${API_ROOT}/materials`, {
      method: "POST",
      body: JSON.stringify({
        project_id: els.materialProject.value || null,
        title,
        kind: els.materialKind.value,
        source_url: els.materialSourceUrl.value.trim() || null,
        content_text: content,
        tags: els.materialTags.value
          .split(/[,，]/)
          .map((item) => item.trim())
          .filter(Boolean)
          .slice(0, 20)
      })
    });
    els.materialDialog.close();
    els.materialForm.reset();
    await loadStudioLibrary();
    showToast("素材已保存");
  } catch (error) {
    showToast(`素材保存失败：${error.message}`, true);
  } finally {
    els.materialSubmit.disabled = false;
  }
}

async function readMaterialFile() {
  const file = els.materialFile.files?.[0];
  if (!file) return;
  if (file.size > 500_000) {
    showToast("文本文件不能超过 500 KB", true);
    els.materialFile.value = "";
    return;
  }
  try {
    els.materialContent.value = await file.text();
    if (!els.materialTitle.value.trim()) {
      els.materialTitle.value = file.name.replace(/\.[^.]+$/, "");
    }
    els.materialKind.value = "FILE";
  } catch (error) {
    showToast(`读取文件失败：${error.message}`, true);
  }
}

function copyOutput() {
  if (!state.outputText) return;
  if (!navigator.clipboard?.writeText) {
    showToast("当前浏览器不支持剪贴板访问", true);
    return;
  }
  navigator.clipboard.writeText(state.outputText)
    .then(() => showToast("内容已复制"))
    .catch(() => showToast("浏览器拒绝了剪贴板访问", true));
}

function downloadOutput() {
  if (!state.outputText) return;
  const blob = new Blob([state.outputText], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = state.outputFilename;
  link.click();
  URL.revokeObjectURL(url);
}

function safeFilename(value) {
  return String(value || "mindflow-creator")
    .replace(/[\\/:*?"<>|]/g, "-")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80) || "mindflow-creator";
}

function formatTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).format(new Date(value));
}

function formatCompactTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function sleep(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function bindEvents() {
  els.logoutButton.addEventListener("click", () => {
    stopEventStream();
    sessionStorage.removeItem(AUTH_KEY);
    sessionStorage.removeItem(ACTIVE_TASK_KEY);
    sessionStorage.setItem(AUTO_LOGIN_SUPPRESS_KEY, "true");
    window.location.replace("/");
  });
  els.openCreateTask.addEventListener("click", openCreateDialog);
  els.openCreateProject.addEventListener("click", () => els.projectDialog.showModal());
  els.openCreateMaterial.addEventListener("click", () => {
    renderTaskLibraryFields();
    els.materialDialog.showModal();
  });
  document.querySelectorAll("[data-open-create]").forEach((button) => {
    button.addEventListener("click", openCreateDialog);
  });
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => els.createTaskDialog.close());
  });
  document.querySelectorAll("[data-close-draft]").forEach((button) => {
    button.addEventListener("click", () => els.draftDialog.close());
  });
  document.querySelectorAll("[data-close-project]").forEach((button) => {
    button.addEventListener("click", () => els.projectDialog.close());
  });
  document.querySelectorAll("[data-close-material]").forEach((button) => {
    button.addEventListener("click", () => els.materialDialog.close());
  });
  document.querySelectorAll("[data-close-channel]").forEach((button) => {
    button.addEventListener("click", () => els.channelDialog.close());
  });
  document.querySelectorAll(".filter-tab").forEach((tab) => {
    tab.addEventListener("click", async () => {
      document.querySelectorAll(".filter-tab").forEach((item) => {
        item.classList.toggle("is-active", item === tab);
      });
      state.taskFilter = tab.dataset.status || "";
      try {
        await loadTasks(true);
      } catch (error) {
        showToast(`任务筛选失败：${error.message}`, true);
      }
    });
  });
  document.querySelectorAll("[data-library-view]").forEach((button) => {
    button.addEventListener("click", () => switchLibraryView(button.dataset.libraryView));
  });
  document.querySelectorAll("[data-project-filter]").forEach((button) => {
    button.addEventListener("click", async () => {
      document.querySelectorAll("[data-project-filter]").forEach((item) => {
        item.classList.toggle("is-active", item === button);
      });
      state.taskFilter = button.dataset.projectFilter || "";
      state.projectFilterId = "";
      renderProjectLibrary();
      renderActiveProjectFilter();
      document.querySelectorAll(".filter-tab").forEach((tab) => {
        tab.classList.toggle(
          "is-active",
          (tab.dataset.status || "") === state.taskFilter
        );
      });
      switchLibraryView("works");
      try {
        await loadTasks(true);
      } catch (error) {
        showToast(`项目读取失败：${error.message}`, true);
      }
    });
  });
  els.workSearch.addEventListener("input", () => {
    state.workSearch = els.workSearch.value;
    renderTasks();
  });
  document.querySelectorAll("[data-assistant-tab]").forEach((button) => {
    button.addEventListener("click", () => switchAssistantTab(button.dataset.assistantTab));
  });
  document.querySelectorAll("[data-mobile-target]").forEach((button) => {
    button.addEventListener("click", () => switchMobilePane(button.dataset.mobileTarget));
  });
  els.mobileSidebarToggle.addEventListener("click", () => switchMobilePane("library"));
  els.closeAssistantPane.addEventListener("click", () => switchMobilePane("editor"));
  els.openAssistantPane.addEventListener("click", () => {
    switchAssistantTab("collaboration");
    switchMobilePane("assistant");
  });
  document.querySelectorAll(".output-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchOutputTab(tab.dataset.tab));
  });
  els.loadMoreTasks.addEventListener("click", () => loadTasks(false));
  els.createTaskForm.addEventListener("submit", createTask);
  els.projectForm.addEventListener("submit", createProject);
  els.materialForm.addEventListener("submit", createMaterial);
  els.materialFile.addEventListener("change", readMaterialFile);
  els.channelForm.addEventListener("submit", createChannelVariant);
  els.draftForm.addEventListener("submit", saveDraft);
  els.cancelTask.addEventListener("click", cancelTask);
  els.retryTask.addEventListener("click", retryTask);
  els.continuePublishing.addEventListener("click", handoffPublication);
  els.saveDocumentVersion.addEventListener("click", saveDocumentVersion);
  els.documentTitleInput.addEventListener("input", () => {
    state.documentTitle = els.documentTitleInput.value;
    resizeDocumentTitle();
    els.topDocumentLabel.textContent =
      els.documentTitleInput.value.trim() || state.snapshot?.goal || "无标题内容";
    markEditorDirty();
  });
  document.querySelectorAll("[data-editor-assist]").forEach((button) => {
    button.addEventListener("click", () => {
      createEditorImprovement(
        improvementInstruction(button.dataset.editorAssist),
        button.dataset.editorAssist
      );
    });
  });
  els.submitEditorInstruction.addEventListener("click", () => {
    const custom = els.editorInstruction.value.trim();
    createEditorImprovement(custom, "custom");
  });
  els.editorInstruction.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      els.submitEditorInstruction.click();
    }
  });
  els.copyOutput.addEventListener("click", copyOutput);
  els.downloadOutput.addEventListener("click", downloadOutput);
  window.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!els.saveDocumentVersion.disabled) saveDocumentVersion();
    }
  });
  window.addEventListener("resize", resizeDocumentTitle);
  window.addEventListener("beforeunload", () => {
    if (state.editorDirty) persistDocumentScratch();
    stopEventStream();
  });
}

async function initialize() {
  try {
    initializeDocumentEditor();
    bindEvents();
    refreshIcons();
    await loadIdentity();
    await loadStudioLibrary();
    await loadTasks(true);
    const preferred = sessionStorage.getItem(ACTIVE_TASK_KEY);
    const initial = state.tasks.find((task) => task.task_id === preferred)
      ? preferred
      : state.tasks[0]?.task_id;
    if (initial) {
      await selectTask(initial);
    } else {
      els.assistantStatusText.textContent = "等待选择作品";
      await renderContextPanels();
    }
  } catch (error) {
    if (error.status === 401) {
      sessionStorage.removeItem(AUTH_KEY);
      window.location.replace("/");
      return;
    }
    setBadge(els.apiState, "创作助手暂不可用", "is-error");
    showToast(`工作台初始化失败：${error.message}`, true);
  }
}

initialize();
