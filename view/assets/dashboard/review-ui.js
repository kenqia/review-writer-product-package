(function () {
  "use strict";

  const navigationGroups = [
    {
      id: "home",
      label: "首页",
      href: "/review",
      matches: ({page, hash}) => page === "review" && hash !== "#evidence",
      children: [],
    },
    {
      id: "sources",
      label: "来源与证据",
      href: "/library",
      matches: ({page, hash}) => page === "library" || page === "matrix" || (page === "review" && hash === "#evidence"),
      children: [
        {id: "corpus", label: "Corpus", href: "/library", matches: ({page}) => page === "library"},
        {id: "evidence", label: "Evidence", href: "/review#evidence", matches: ({page, hash}) => page === "review" && hash === "#evidence"},
        {id: "matrix", label: "Matrix", href: "/matrix", matches: ({page}) => page === "matrix"},
      ],
    },
    {
      id: "manuscript",
      label: "正文",
      href: "/blueprint",
      matches: ({page}) => ["blueprint", "sections", "draft"].includes(page),
      children: [
        {id: "blueprint", label: "写作大纲", href: "/blueprint", matches: ({page}) => page === "blueprint"},
        {id: "sections", label: "Sections", href: "/sections", matches: ({page}) => page === "sections"},
        {id: "draft", label: "Draft", href: "/draft", matches: ({page}) => page === "draft"},
      ],
    },
    {
      id: "figures",
      label: "图表",
      href: "/figures",
      matches: ({page}) => page === "figures",
      children: [
        {id: "figure-attribution", label: "Figures attribution", href: "/figures", matches: ({hash}) => !hash || hash === "#attribution"},
        {id: "figure-license", label: "许可", href: "/figures#license", matches: ({hash}) => hash === "#license"},
        {id: "figure-binding", label: "正文绑定", href: "/figures#binding", matches: ({hash}) => hash === "#binding"},
      ],
    },
    {
      id: "release",
      label: "发布与历史",
      href: "/final#release",
      matches: ({page}) => page === "final",
      children: [
        {id: "quality", label: "Quality", href: "/final#quality", matches: ({hash}) => hash === "#quality"},
        {id: "release", label: "Release", href: "/final#release", matches: ({hash}) => !hash || hash === "#release"},
        {id: "markdown", label: "Markdown", href: "/final#markdown", matches: ({hash}) => hash === "#markdown"},
        {id: "docx", label: "DOCX", href: "/final#docx", matches: ({hash}) => hash === "#docx"},
        {id: "history", label: "History", href: "/final#history", matches: ({hash}) => hash === "#history"},
      ],
    },
  ];

  const statusLabels = {
    approved: "已核对",
    approve: "已核对",
    rejected: "已拒绝",
    reject: "已拒绝",
    pending: "待人工核对",
    needs_review: "需要人工核对",
    needs_human_edit: "待研究者编辑",
    in_progress: "进行中",
    complete: "已完成",
    final_review: "终稿核对",
    drafting: "正文编辑",
    evidence_review: "证据核对",
    review_brief: "研究范围",
    ready_for_discovery: "语料准备",
    not_started: "尚未开始",
    unavailable: "暂未提供",
    stale: "来源已过期",
    in_progress: "进行中",
    IN_PROGRESS: "进行中",
    SELF_REVIEWED_DRAFT: "自评审草稿",
    EXPERT_REVIEWED_RELEASE: "专家评审发布稿",
    PAPER_EVIDENCE_APPROVED: "证据已核对",
    PAPER_EVIDENCE_READY: "证据可核对",
    MANUSCRIPT_APPROVED: "正文已批准",
  };

  function text(value, fallback = "—") {
    const valueText = value == null ? "" : String(value).trim();
    return valueText || fallback;
  }

  function humanStatus(value, fallback = "待核对") {
    const raw = text(value, "");
    return statusLabels[raw]
      || statusLabels[raw.toLowerCase()]
      || statusLabels[raw.toUpperCase()]
      || (raw ? raw.replaceAll("_", " ") : fallback);
  }

  function humanActor(value) {
    const actor = value && typeof value === "object" ? value : {};
    if (actor.actor_type === "human_researcher") return "研究者决定";
    if (actor.actor_type === "simulated_researcher_agent") return "Agent 代理动作";
    return actor.actor_label ? "Dashboard 决定" : "未记录决定者";
  }

  function currentNavigation() {
    const locationState = {
      page: location.pathname.replace(/^\/+/, "") || "review",
      hash: location.hash.toLowerCase(),
    };
    const group = navigationGroups.find(candidate => candidate.matches(locationState)) || navigationGroups[0];
    const child = group.children.find(candidate => candidate.matches(locationState)) || group.children[0] || null;
    return {group, child};
  }

  function currentStage() {
    return currentNavigation().child?.id || currentNavigation().group.id;
  }

  function legacyProjectIdFromLocation() {
    const params = new URLSearchParams(window.location.search);
    return String(params.get("project_id") || params.get("project") || "").trim();
  }

  function projectIdFromLocation() {
    return legacyProjectIdFromLocation();
  }

  function projectHref(href, projectId = projectIdFromLocation()) {
    const value = String(projectId || "").trim();
    if (!value) return href;
    const url = new URL(href, window.location.origin);
    url.searchParams.set("project_id", value);
    return `${url.pathname}${url.search}${url.hash}`;
  }

  function setProjectIdInLocation(projectId) {
    const value = String(projectId || "").trim();
    const url = new URL(window.location.href);
    if (value) url.searchParams.set("project_id", value);
    else url.searchParams.delete("project_id");
    url.searchParams.delete("project");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    document.querySelectorAll("#rw-workflow-nav a[data-base-href]").forEach(link => {
      link.href = projectHref(link.dataset.baseHref, value);
    });
    return value;
  }

  function makeWorkflowNav() {
    const nav = document.querySelector(".nav");
    if (!nav) return;
    const title = nav.querySelector(".nav-title");
    if (title) {
      title.textContent = "Review Writer";
      title.setAttribute("aria-label", "Review Writer");
    }
    const oldStrip = document.getElementById("rw-workflow-nav");
    if (oldStrip) oldStrip.remove();
    const strip = document.createElement("section");
    strip.id = "rw-workflow-nav";
    strip.className = "stage-strip";
    strip.setAttribute("aria-label", "Research workflow");
    const {group, child} = currentNavigation();
    const currentCopy = document.createElement("div");
    currentCopy.className = "stage-current";
    currentCopy.innerHTML = `
      <div class="stage-kicker">研究工作台</div>
      <div class="stage-name"></div>
      <div class="stage-hint"></div>
    `;
    currentCopy.querySelector(".stage-name").textContent = group.label;
    currentCopy.querySelector(".stage-hint").textContent = child
      ? `当前位置：${group.label} / ${child.label}`
      : "阅读当前项目、核证证据并决定下一步。";
    const navigation = document.createElement("div");
    navigation.className = "rw-navigation";
    const primary = document.createElement("nav");
    primary.className = "rw-primary-nav";
    primary.setAttribute("aria-label", "顶级导航");
    navigationGroups.forEach(candidate => {
      const link = document.createElement("a");
      link.dataset.baseHref = candidate.href;
      link.href = projectHref(candidate.href);
      link.textContent = candidate.label;
      link.className = `rw-primary-link${candidate.id === group.id ? " active" : ""}`;
      link.setAttribute("aria-current", candidate.id === group.id ? "page" : "false");
      primary.append(link);
    });
    navigation.append(primary);
    if (group.children.length) {
      const secondary = document.createElement("nav");
      secondary.className = "rw-secondary-nav";
      secondary.setAttribute("aria-label", `${group.label}页面`);
      group.children.forEach(candidate => {
        const link = document.createElement("a");
        link.dataset.baseHref = candidate.href;
        link.href = projectHref(candidate.href);
        link.textContent = candidate.label;
        link.className = `rw-secondary-link${candidate.id === child?.id ? " active" : ""}`;
        link.setAttribute("aria-current", candidate.id === child?.id ? "page" : "false");
        secondary.append(link);
      });
      navigation.append(secondary);
    }
    strip.append(currentCopy, navigation);
    nav.insertAdjacentElement("afterend", strip);
  }

  function getProjectSelects() {
    return [...document.querySelectorAll("#project, #projectSelect, #globalProjectSelect")];
  }

  function selectedProjectId(select, projects) {
    if (!select || !select.value) return "";
    const direct = projects.find(project => project.project_id === select.value);
    if (direct) return direct.project_id;
    const optionText = select.selectedOptions?.[0]?.textContent?.trim() || "";
    const byLabel = projects.find(project => project.visible_label === optionText || project.project_id === optionText);
    return byLabel?.project_id || "";
  }

  function ensureContextBar() {
    let bar = document.getElementById("rw-context-bar");
    if (bar) return bar;
    const nav = document.querySelector(".nav");
    if (!nav) return null;
    bar = document.createElement("section");
    bar.id = "rw-context-bar";
    bar.className = "rw-context-bar";
    bar.setAttribute("aria-label", "Current project context");
    bar.innerHTML = `
      <div class="rw-context-identity">
        <span class="rw-context-product">Research workbench</span>
        <span class="rw-context-divider" aria-hidden="true">·</span>
        <span id="rw-context-project">未选择项目</span>
      </div>
      <div class="rw-context-meta">
        <span id="rw-context-version" title="公开投影未提供内部版本 ID">版本：待读取</span>
        <span id="rw-context-status" class="rw-status rw-status-pending"><span aria-hidden="true">○</span> 待读取</span>
      </div>
    `;
    const strip = document.getElementById("rw-workflow-nav");
    if (strip) strip.insertAdjacentElement("afterend", bar);
    else nav.insertAdjacentElement("afterend", bar);
    return bar;
  }

  function ensureFeedback() {
    let feedback = document.getElementById("rw-global-feedback");
    if (feedback) return feedback;
    feedback = document.createElement("div");
    feedback.id = "rw-global-feedback";
    feedback.className = "rw-global-feedback";
    feedback.setAttribute("role", "status");
    feedback.setAttribute("aria-live", "polite");
    feedback.hidden = true;
    document.body.append(feedback);
    return feedback;
  }

  function setContext(project, reviewState, finalPayload) {
    const projectNode = document.getElementById("rw-context-project");
    const versionNode = document.getElementById("rw-context-version");
    const statusNode = document.getElementById("rw-context-status");
    if (!projectNode || !versionNode || !statusNode) return;
    const projectId = project?.project_id || reviewState?.project_id;
    projectNode.textContent = projectId ? text(project?.visible_label, projectId) : "未选择项目";
    const snapshot = finalPayload?.release_snapshot || {};
    const versionLabel = snapshot.exists
      ? (snapshot.matches_authoritative ? "当前发布快照" : "发布快照已过期")
      : "当前权威正文";
    versionNode.textContent = `版本：${versionLabel}`;
    versionNode.title = finalPayload?.release_status || "公开投影未提供内部版本 ID";
    const rawStatus = snapshot.exists && snapshot.matches_authoritative
      ? "approved"
      : reviewState?.status || finalPayload?.release_status || "pending";
    const isGood = rawStatus === "approved" || rawStatus === "complete";
    const isWarn = rawStatus === "stale" || snapshot.exists && !snapshot.matches_authoritative;
    statusNode.className = `rw-status ${isGood ? "rw-status-ok" : isWarn ? "rw-status-warn" : "rw-status-pending"}`;
    statusNode.innerHTML = `<span aria-hidden="true">${isGood ? "✓" : isWarn ? "!" : "○"}</span> ${humanStatus(isWarn ? "stale" : rawStatus)}`;
    statusNode.title = rawStatus;
  }

  async function refreshContext(projects) {
    const selects = getProjectSelects();
    const selected = selects.map(select => selectedProjectId(select, projects)).find(Boolean) || "";
    const project = projects.find(row => row.project_id === selected);
    if (!selected) {
      setContext(null, null, null);
      return;
    }
    const encoded = encodeURIComponent(selected);
    const reviewState = await fetch(`/api/project/${encoded}/review-state`)
      .then(response => response.ok ? response.json() : null)
      .catch(() => null);
    const finalDataReady = ["final", "complete"].includes(reviewState?.current_stage)
      || Boolean(reviewState?.draft?.final_draft_exists || reviewState?.draft?.docx_exists);
    const finalPayload = reviewState?.status === "AWAITING_BRIEF_CONFIRMATION" || !finalDataReady
      ? null
      : await fetch(`/api/project/${encoded}/final`)
        .then(response => response.ok ? response.json() : null)
        .catch(() => null);
    setContext(project, reviewState, finalPayload);
  }

  async function installContext() {
    ensureContextBar();
    ensureFeedback();
    let projects = [];
    try {
      const response = await fetch("/api/projects");
      if (response.ok) projects = await response.json();
    } catch (_) {
      projects = [];
    }
    await refreshContext(projects);
    const refresh = () => window.setTimeout(() => refreshContext(projects), 0);
    document.addEventListener("change", event => {
      if (event.target.matches("#project, #projectSelect, #globalProjectSelect")) refresh();
    });
    getProjectSelects().forEach(select => {
      const observer = new MutationObserver(() => refresh());
      observer.observe(select, {childList: true, subtree: true, attributes: true});
    });
  }

  function installPublicAPI() {
    window.ReviewPresentation = {
      navigationGroups,
      humanStatus,
      humanActor,
      currentStage,
      currentNavigation,
      legacyProjectIdFromLocation,
      projectIdFromLocation,
      projectHref,
      setProjectIdInLocation,
      notify(message, tone = "info") {
        const feedback = ensureFeedback();
        feedback.textContent = text(message, "");
        feedback.dataset.tone = tone;
        feedback.hidden = !feedback.textContent;
        if (feedback.textContent) {
          window.clearTimeout(feedback._hideTimer);
          feedback._hideTimer = window.setTimeout(() => { feedback.hidden = true; }, 5000);
        }
      },
    };
  }

  function normalizeLegacyProjectLocation() {
    const params = new URLSearchParams(window.location.search);
    if (params.has("project") && !params.has("project_id")) {
      return setProjectIdInLocation(params.get("project"));
    }
    return projectIdFromLocation();
  }

  function syncReviewFocus() {
    if (!document.body || !document.body.classList.contains("page-review")) return;
    const evidenceFocused = location.hash.toLowerCase() === "#evidence";
    document.body.classList.toggle("rw-overview-focus", !evidenceFocused);
    document.body.classList.toggle("rw-evidence-focus", evidenceFocused);
    document.body.dataset.reviewFocus = evidenceFocused ? "evidence" : "overview";
    if (evidenceFocused) {
      window.requestAnimationFrame(() => {
        document.getElementById("evidence-synthesis-workspace")?.scrollIntoView({block: "start"});
      });
    }
  }

  function init() {
    normalizeLegacyProjectLocation();
    syncReviewFocus();
    const page = currentStage();
    document.body.classList.add(`rw-page-${page}`);
    makeWorkflowNav();
    installPublicAPI();
    installContext();
    window.addEventListener("hashchange", () => { syncReviewFocus(); makeWorkflowNav(); });
  }

  normalizeLegacyProjectLocation();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}());
