(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.ReviewSessionUI = api;
  if (root.window === root) api.installDecisionActor(root);
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function installDecisionActor(window) {
    const params = new URLSearchParams(window.location.search);
    const simulated = params.get("review_actor") === "simulated_researcher_agent";

    window.reviewDecisionActor = () => simulated
      ? {
          actor_type: "simulated_researcher_agent",
          actor_label: "dashboard-playwright-reviewer",
        }
      : {};
  }

  function canonicalProjectVisibleLabel(value) {
    if (typeof value !== "string") return "";
    let normalized;
    try {
      normalized = value.normalize("NFC");
    } catch (_) {
      return "";
    }
    if (/\p{C}/u.test(normalized)) return "";
    normalized = normalized.replace(/\p{Z}+/gu, " ").replace(/ +/g, " ").trim();
    if (!normalized || [...normalized].length > 200) return "";
    return normalized;
  }

  function createProjectSelectionRegistry() {
    let optionIds = new Map();
    let optionLabels = new Map();
    let projectKeys = new Map();

    function replace(projects) {
      const rows = Array.isArray(projects) ? projects : [];
      const visibleLabels = rows.map(project => canonicalProjectVisibleLabel(project?.visible_label));
      const labelCounts = new Map();
      visibleLabels.forEach(visibleLabel => {
        const label = visibleLabel || "项目显示名称不可用";
        labelCounts.set(label, (labelCounts.get(label) || 0) + 1);
      });
      const nextOptionIds = new Map();
      const nextOptionLabels = new Map();
      const nextProjectKeys = new Map();
      const choices = rows.map((project, index) => {
        const key = `project-option-${index + 1}`;
        const visibleLabel = visibleLabels[index];
        const hasValidLabel = visibleLabel !== "";
        const label = visibleLabel || "项目显示名称不可用";
        const projectId = typeof project?.project_id === "string" ? project.project_id : "";
        const selectable = project?.selectable === true
          && hasValidLabel
          && projectId !== ""
          && labelCounts.get(label) === 1;
        nextOptionLabels.set(key, label);
        if (selectable) {
          nextOptionIds.set(key, projectId);
          nextProjectKeys.set(projectId, key);
        }
        const message = canonicalProjectVisibleLabel(project?.selection_message)
          || (hasValidLabel
            ? "请在 QoderWork 中设置唯一项目显示名称。"
            : "请在 QoderWork 中设置唯一有效项目显示名称。");
        return {key, label: selectable ? label : `${label}（${message}）`, selectable};
      });
      optionIds = nextOptionIds;
      optionLabels = nextOptionLabels;
      projectKeys = nextProjectKeys;
      return choices;
    }

    return {
      replace,
      getProjectId: key => optionIds.get(key) || "",
      getVisibleLabel: key => optionLabels.get(key) || "",
      getOptionKey: projectId => projectKeys.get(projectId) || "",
    };
  }

  function createProjectRefreshScheduler(options) {
    const refresh = options?.refresh;
    const getProjectId = options?.getProjectId;
    const setTimer = options?.setTimer;
    const clearTimer = options?.clearTimer;
    const emptyDelay = options?.emptyDelay;
    const selectedDelay = options?.selectedDelay;
    if (typeof refresh !== "function" || typeof getProjectId !== "function") {
      throw new Error("refresh and project reader required");
    }
    if (typeof setTimer !== "function" || typeof clearTimer !== "function") {
      throw new Error("timer functions required");
    }
    if (!Number.isInteger(emptyDelay) || emptyDelay < 1000) throw new Error("empty delay required");
    if (!Number.isInteger(selectedDelay) || selectedDelay < emptyDelay) throw new Error("selected delay required");

    let timer = null;
    let running = false;
    let stopped = true;

    function schedule() {
      if (stopped || timer !== null || running) return;
      timer = setTimer(tick, getProjectId() ? selectedDelay : emptyDelay);
    }

    async function tick() {
      timer = null;
      if (stopped || running) return;
      running = true;
      try {
        await refresh();
      } catch (_) {
        // Project discovery is recoverable; the next bounded tick retries once.
      } finally {
        running = false;
        schedule();
      }
    }

    return {
      start(settings = {}) {
        if (!stopped) return;
        stopped = false;
        if (settings.immediate === true) return tick();
        schedule();
      },
      stop() {
        stopped = true;
        if (timer !== null) clearTimer(timer);
        timer = null;
      },
      isActive() {
        return !stopped;
      },
    };
  }

  function installProjectRefreshLifecycle(window, getController) {
    if (!window || typeof window.addEventListener !== "function" || typeof getController !== "function") {
      throw new Error("window and refresh controller reader required");
    }
    window.addEventListener("pagehide", () => getController()?.stop());
    window.addEventListener("pageshow", event => {
      const controller = getController();
      if (event?.persisted === true || controller?.isActive() === false) controller?.start();
    });
  }

  function createProjectSurfaceCoordinator(options) {
    const getProjectId = options?.getProjectId;
    const getProjectLabel = options?.getProjectLabel;
    const load = options?.load;
    const render = options?.render;
    if (typeof getProjectId !== "function" || typeof load !== "function" || typeof render !== "function") {
      throw new Error("project reader, loader, and renderer required");
    }

    let projectId = String(getProjectId() || "");
    let projectLabel = typeof getProjectLabel === "function" ? String(getProjectLabel() || "") : "";
    let generation = 0;
    let operationEpoch = 0;
    let refreshRunning = false;
    let refreshQueued = false;
    let mutationRunning = false;

    function syncProject() {
      const nextProjectId = String(getProjectId() || "");
      const nextProjectLabel = typeof getProjectLabel === "function" ? String(getProjectLabel() || "") : "";
      projectLabel = nextProjectLabel;
      if (nextProjectId !== projectId) {
        projectId = nextProjectId;
        generation += 1;
        if (refreshRunning || mutationRunning) refreshQueued = true;
        options?.onProjectChange?.({projectId, projectLabel, generation, operationEpoch});
      }
      return {projectId, projectLabel, generation, operationEpoch};
    }

    function isCurrent(context) {
      const current = syncProject();
      return current.projectId === context.projectId
        && current.generation === context.generation
        && current.operationEpoch === context.operationEpoch;
    }

    async function drainRefresh() {
      if (!refreshQueued || refreshRunning || mutationRunning) return;
      refreshQueued = false;
      await refresh();
    }

    async function refresh() {
      const context = syncProject();
      if (!context.projectId) return {status: "empty"};
      if (refreshRunning || mutationRunning) {
        refreshQueued = true;
        return {status: "queued"};
      }
      refreshRunning = true;
      try {
        const value = await load(context.projectId, context);
        if (!isCurrent(context)) return {status: "stale"};
        render(value, context);
        return {status: "rendered"};
      } catch (error) {
        if (isCurrent(context)) options?.onLoadError?.(error, context);
        return {status: "error"};
      } finally {
        refreshRunning = false;
        await drainRefresh();
      }
    }

    async function mutate(run, settings) {
      if (typeof run !== "function") throw new Error("mutation required");
      const projectContext = syncProject();
      if (!projectContext.projectId) return {status: "empty"};
      if (mutationRunning) return {status: "busy"};
      operationEpoch += 1;
      const context = {...projectContext, operationEpoch};
      mutationRunning = true;
      try {
        const value = await run(context.projectId, context);
        const current = isCurrent(context);
        if (current && typeof settings?.renderResult === "function") {
          settings.renderResult(value, context);
        }
        return {status: current ? "saved" : "stale"};
      } catch (error) {
        if (isCurrent(context)) settings?.onError?.(error, context);
        return {status: "error"};
      } finally {
        if (settings?.refreshAfterMutation === true) refreshQueued = true;
        mutationRunning = false;
        await drainRefresh();
      }
    }

    function projectChanged() {
      syncProject();
      return refresh();
    }

    return {mutate, projectChanged, refresh};
  }

  return {
    createProjectRefreshScheduler,
    createProjectSelectionRegistry,
    createProjectSurfaceCoordinator,
    installDecisionActor,
    installProjectRefreshLifecycle,
  };
}));
