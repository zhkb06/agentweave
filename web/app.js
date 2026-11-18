/* ==========================================================================
   织流 AgentWeave · Web 控制台前端（原生 JS，无第三方依赖）
   页面：概览 / 编排台 / 运行监控 / 验证中心 / Agent 团队 / 工具与 MCP / 模型设置
   ========================================================================== */
(function () {
  "use strict";

  /* ------------------------------ 常量 ------------------------------ */

  var PAGE_TITLES = {
    dashboard: "总览仪表盘",
    flow: "流程编排中心",
    runtime: "运行与验证中心",
    resources: "资源管理中心",
    settings: "系统配置"
  };

  var STATUS_LABELS = {
    pending: "等待",
    scheduled: "已调度",
    running: "运行中",
    succeeded: "成功",
    failed: "失败",
    skipped: "跳过",
    retrying: "重试中",
    waiting_human: "待人工",
    paused: "已暂停",
    cancelled: "已取消",
    rejected: "已拒绝"
  };

  var STATUS_PILL = {
    succeeded: "ok",
    failed: "bad",
    rejected: "bad",
    running: "run",
    pending: "warn",
    paused: "warn",
    waiting_human: "warn",
    scheduled: "run"
  };

  // SSE 具名事件类型（后端 event: <type>）
  var SSE_TYPES = [
    "run.created", "run.started", "run.paused", "run.finished", "run.cancelled",
    "plan.ready", "node.scheduled", "node.started", "node.succeeded", "node.failed",
    "node.retrying", "node.skipped", "agent.thought", "agent.decision",
    "tool.called", "tool.result", "llm.called", "llm.result", "verify.result",
    "loop.iteration", "loop.exit", "human.required", "human.decided",
    "artifact.created", "metric", "log"
  ];

  var EVT_CLASS = [
    ["run.", "evt-run"], ["node.", "evt-node"], ["agent.", "evt-agent"],
    ["tool.", "evt-tool"], ["verify.", "evt-verify"], ["loop.", "evt-loop"],
    ["artifact.", "evt-artifact"], ["human.", "evt-human"], ["llm.", "evt-log"]
  ];

  var LAYER_COLORS = {
    structure: "#38bdf8",
    numeric: "#a78bfa",
    rerun: "#f59e0b",
    cross: "#34d399",
    statistical: "#fbbf24"
  };

  /* ------------------------------ 状态 ------------------------------ */

  var state = {
    page: "overview",
    health: null,
    overview: null,
    runs: [],
    workflows: [],
    selectedWorkflowId: null,
    workflowRun: null,
    workflowStatuses: {},
    dag: null,
    agents: [],
    agentLoad: [],
    tools: [],
    mcpServers: [],
    llm: null,
    verifyRunId: null,
    monitor: {
      runId: null,
      es: null,
      run: null,
      workflowId: "",
      status: "pending",
      duration: null,
      tokens: null,
      successRate: null,
      loopIterations: 0,
      nodes: {},
      nodeOrder: [],
      events: [],
      decisions: [],
      loops: [],
      seenSeq: {},
      finished: false,
      errorToasted: false
    },
    demoBusy: false,
    healthFailed: false
  };

  /* ------------------------------ DOM / 格式化工具 ------------------------------ */

  function $(id) { return document.getElementById(id); }

  function qsa(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function setText(id, value) {
    var el = $(id);
    if (el) el.textContent = value == null || value === "" ? "—" : String(value);
  }

  function setHidden(id, hidden) {
    var el = $(id);
    if (el) el.hidden = !!hidden;
  }

  function toDate(value) {
    if (value == null || value === "") return null;
    if (typeof value === "number") {
      if (!isFinite(value)) return null;
      return new Date(value > 1e12 ? value : value * 1000);
    }
    var date = new Date(value);
    return isNaN(date.getTime()) ? null : date;
  }

  function fmtTime(value) {
    var date = toDate(value);
    if (!date) return "—";
    var pad = function (n) { return n < 10 ? "0" + n : String(n); };
    return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }

  function fmtDateTime(value) {
    var date = toDate(value);
    if (!date) return "—";
    return (date.getMonth() + 1) + "-" + date.getDate() + " " + fmtTime(value);
  }

  function fmtNum(value, digits) {
    if (value == null || value === "" || isNaN(Number(value))) return "—";
    var number = Number(value);
    if (digits === 0) return String(Math.round(number));
    return number.toFixed(digits == null ? 2 : digits);
  }

  function fmtInt(value) {
    if (value == null || isNaN(Number(value))) return "—";
    return String(Math.round(Number(value)));
  }

  function fmtDuration(seconds) {
    if (seconds == null || isNaN(Number(seconds))) return "—";
    var value = Math.max(0, Number(seconds));
    if (value < 60) return value.toFixed(1) + "s";
    if (value < 3600) return Math.floor(value / 60) + "m" + Math.round(value % 60) + "s";
    return Math.floor(value / 3600) + "h" + Math.round((value % 3600) / 60) + "m";
  }

  function normConf(value) {
    if (value == null || value === "" || isNaN(Number(value))) return null;
    var number = Number(value);
    if (number > 1.5) number = number / 100;
    return Math.max(0, Math.min(1, number));
  }

  function fmtConf(value) {
    var ratio = normConf(value);
    return ratio == null ? "—" : (ratio * 100).toFixed(1) + "%";
  }

  function fmtDurationS(value) {
    if (value == null || isNaN(Number(value))) return "—";
    return Math.round(Number(value) * 1000) + " ms";
  }

  function shortId(id) {
    var text = String(id == null ? "" : id);
    if (text.length <= 18) return text;
    return text.slice(0, 10) + "…" + text.slice(-4);
  }

  function statusLabel(status) {
    return STATUS_LABELS[status] || status || "未知";
  }

  function pillClass(status) {
    return STATUS_PILL[status] || "";
  }

  function evtClass(type) {
    var text = String(type || "");
    for (var i = 0; i < EVT_CLASS.length; i += 1) {
      if (text.indexOf(EVT_CLASS[i][0]) === 0) return EVT_CLASS[i][1];
    }
    return "evt-log";
  }

  function kvRow(key, value) {
    return '<div class="kv-row"><span class="k">' + esc(key) + '</span><span class="v">' +
      esc(value == null || value === "" ? "—" : value) + "</span></div>";
  }

  function emptyBlock(title, hint, small) {
    return '<div class="empty' + (small ? " small" : "") + '"><p>' + esc(title) + "</p><span>" +
      esc(hint || "") + "</span></div>";
  }

  function truncate(text, max) {
    var value = String(text == null ? "" : text);
    if (value.length <= max) return value;
    return value.slice(0, max) + "…";
  }

  /* ------------------------------ API ------------------------------ */

  function ApiFailure(message, code, status) {
    this.name = "ApiFailure";
    this.message = message;
    this.code = code;
    this.status = status;
  }
  ApiFailure.prototype = Object.create(Error.prototype);

  function api(path, options) {
    options = options || {};
    var init = { method: options.method || "GET", headers: {} };
    var token = "";
    try {
      token = new URLSearchParams(location.search).get("token") || "";
      if (token) window.localStorage.setItem("agentweave_token", token);
      token = token || window.localStorage.getItem("agentweave_token") || "";
    } catch (err) { token = ""; }
    if (token) init.headers["Authorization"] = "Bearer " + token;
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    return fetch(path, init).then(
      function (response) {
        return response.text().then(function (raw) {
          var data = null;
          if (raw) {
            try { data = JSON.parse(raw); } catch (err) { data = null; }
          }
          if (!response.ok) {
            var error = (data && data.error) || {};
            throw new ApiFailure(
              error.message || ("请求失败（HTTP " + response.status + "）"),
              error.code || "HTTP_" + response.status,
              response.status
            );
          }
          return data;
        });
      },
      function () {
        throw new ApiFailure("无法连接后端服务，请确认服务已启动", "NETWORK_ERROR", 0);
      }
    );
  }

  /* ------------------------------ Toast ------------------------------ */

  function toast(message, type, ttl) {
    var host = $("toasts");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast" + (type ? " is-" + type : "");
    el.textContent = String(message == null ? "" : message);
    host.appendChild(el);
    var lifetime = ttl || (type === "error" ? 5200 : 3200);
    setTimeout(function () {
      el.classList.add("is-leaving");
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, 240);
    }, lifetime);
    while (host.children.length > 4) host.removeChild(host.firstChild);
  }

  function toastError(err) {
    var message = err && err.message ? err.message : String(err || "操作失败");
    toast(message, "error");
    if (window.console && console.warn) console.warn("[AgentWeave]", err);
  }

  /* ------------------------------ 路由 ------------------------------ */

  var PAGE_ENTERS = {};

  function parseHash() {
    var raw = String(location.hash || "").replace(/^#\/?/, "");
    var index = raw.indexOf("?");
    var page = (index >= 0 ? raw.slice(0, index) : raw) || "overview";
    var query = {};
    if (index >= 0) {
      raw.slice(index + 1).split("&").forEach(function (part) {
        if (!part) return;
        var pair = part.split("=");
        query[decodeURIComponent(pair[0])] = decodeURIComponent((pair[1] || "").replace(/\+/g, " "));
      });
    }
    return { page: page, query: query };
  }

  function go(page, query) {
    var search = "";
    if (query) {
      Object.keys(query).forEach(function (key) {
        if (query[key] == null || query[key] === "") return;
        search += (search ? "&" : "") + encodeURIComponent(key) + "=" + encodeURIComponent(query[key]);
      });
    }
    var target = "#/" + page + (search ? "?" + search : "");
    if (location.hash === target) route();
    else location.hash = target;
  }

  function route() {
    var parsed = parseHash();
    var page = Object.prototype.hasOwnProperty.call(PAGE_TITLES, parsed.page) ? parsed.page : "dashboard";
    state.page = page;
    // 支持 #/orchestrator?zoom=0.95 这类截图/演示参数，覆盖自动 fit 的缩放比例
    state.dagZoomOverride = parsed.query && parsed.query.zoom ? parsed.query.zoom : null;
    qsa(".nav-item").forEach(function (item) {
      item.classList.toggle("is-active", item.getAttribute("data-page") === page);
    });
    qsa(".page").forEach(function (section) {
      section.classList.toggle("is-active", section.getAttribute("data-page") === page);
    });
    setText("crumb", PAGE_TITLES[page]);
    if (page === "workbench" && window.Workbench) {
      try { window.Workbench.activate(); } catch (err) { toastError(err); }
      return;
    }
    var enter = PAGE_ENTERS[page];
    if (!enter) return;
    try {
      Promise.resolve(enter(parsed.query)).catch(toastError);
    } catch (err) {
      toastError(err);
    }
  }

  /* ------------------------------ 顶部状态条 ------------------------------ */

  function loadHealth() {
    return api("/api/health").then(function (data) {
      state.health = data;
      state.healthFailed = false;
      var llm = data.llm || {};
      var provider = llm.provider || (data.llm && data.llm.status && data.llm.status.provider) || "mock";
      var providerText = provider + (llm.model ? " · " + llm.model : "");
      if (provider !== "mock" && llm.api_key_set === false) {
        providerText += " · 待填 Key";
      }
      setText("stat-provider", providerText);
      var conn = $("conn-status");
      if (conn) {
        var ready = !(data.platform && data.platform.ready === false);
        conn.classList.toggle("is-ok", ready);
        conn.classList.toggle("is-bad", !ready);
        setText("conn-text", ready ? "系统正常" : "平台未就绪");
        if (!ready && data.platform && data.platform.error) {
          conn.title = String(data.platform.error);
        }
      }
      if (data.uptime_s != null) {
        setText("overview-uptime", "已运行 " + fmtDuration(data.uptime_s));
      }
    }).catch(function (err) {
      if (state.healthFailed) return;
      state.healthFailed = true;
      var conn = $("conn-status");
      if (conn) {
        conn.classList.remove("is-ok");
        conn.classList.add("is-bad");
        setText("conn-text", "服务未连接");
      }
      toastError(err);
    });
  }

  /* ------------------------------ 概览 ------------------------------ */

  function chip(label, value, cls) {
    return '<div class="summary-chip' + (cls ? " " + cls : "") + '"><span>' + esc(label) +
      '</span><b>' + esc(value == null || value === "" ? "—" : value) + "</b></div>";
  }

  function renderRunRow(run) {
    var status = String(run.status || "pending");
    var confidence = fmtConf(run.metrics && run.metrics.avg_confidence != null
      ? run.metrics.avg_confidence
      : run.confidence);
    var name = run.workflow_name || run.workflow_id || run.label || "未命名运行";
    var time = fmtDateTime(run.created_at || run.started_at);
    return '<div class="run-row" data-run-id="' + esc(run.run_id || run.id) + '" role="button" tabindex="0">' +
      '<span class="run-dot ' + esc(status) + '"></span>' +
      '<div class="run-name"><b>' + esc(name) + "</b><span>" +
      esc(shortId(run.run_id || run.id)) + " · " + esc(time) + "</span></div>" +
      '<span class="run-conf">' + esc(confidence) + "</span>" +
      '<span class="pill ' + esc(pillClass(status)) + '">' + esc(statusLabel(status)) + "</span>" +
      "</div>";
  }

  function renderMiniEvent(event) {
    var payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    var summary = event.summary || payload.summary || payload.message || payload.text ||
      (payload.node_id ? "节点 " + payload.node_id : "") || event.type || "";
    return '<div class="mini-event"><time>' + esc(fmtTime(event.ts)) + "</time>" +
      '<span class="mini-type">' + esc(event.type || "event") + "</span>" +
      "<span>" + esc(truncate(summary, 90)) + "</span></div>";
  }

  function loadOverview(silent) {
    return api("/api/overview").then(function (data) {
      state.overview = data;
      setText("metric-agents", fmtInt(data.agent_count != null ? data.agent_count : data.agents));
      setText("metric-workflows", fmtInt(data.workflows));
      setText("metric-runs", fmtInt(data.run_count != null ? data.run_count : data.runs));
      var ratio = normConf(data.avg_confidence);
      setText("metric-confidence", ratio == null || ratio === 0 ? "—" : (ratio * 100).toFixed(1) + "%");
      setText("stat-running", fmtInt(data.running || 0));
      setText("stat-events", fmtInt(data.events || 0));

      var runsSub = $("metric-runs-sub");
      if (runsSub) {
        var finished = Number(data.run_count || 0) - Number(data.running || 0);
        runsSub.textContent = finished > 0
          ? "成功率 " + Math.round((data.success_rate || 0) * 100) + "% · 平均 " + fmtDuration(data.avg_duration_s)
          : "累计执行次数";
      }
      var confSub = $("metric-confidence-sub");
      if (confSub) {
        confSub.textContent = data.avg_tokens ? "平均 " + fmtInt(data.avg_tokens) + " tokens / 次" : "五层验证加权结果";
      }

      var listHost = $("recent-runs");
      if (listHost) {
        var runs = data.recent_runs || [];
        listHost.innerHTML = runs.length
          ? runs.map(renderRunRow).join("")
          : emptyBlock("还没有运行记录", "点击上方「运行演示场景」启动 AI4S 归因分析，或前往编排台查看工作流。", true);
        qsa(".run-row", listHost).forEach(function (row) {
          row.addEventListener("click", function () {
            go("monitor", { run: row.getAttribute("data-run-id") });
          });
          row.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              go("monitor", { run: row.getAttribute("data-run-id") });
            }
          });
        });
      }

      var statusHost = $("overview-status");
      if (statusHost) {
        var scheduler = data.scheduler || {};
        var llm = data.llm || {};
        var platformReady = !(data.health && data.health.platform);
        var rows = [
          kvRow("LLM Provider", (llm.provider || "mock") + (llm.model ? " · " + llm.model : "")),
          kvRow("调度策略", scheduler.policy || scheduler.strategy || "adaptive"),
          kvRow("运行中", fmtInt(data.running || 0) + " / 累计 " + fmtInt(data.run_count || data.runs)),
          kvRow("成功率", Math.round((data.success_rate || 0) * 100) + "%"),
          kvRow("平均耗时", fmtDuration(data.avg_duration_s)),
          kvRow("工具 / 工作流", fmtInt(data.tool_count != null ? data.tool_count : data.tools) + " / " + fmtInt(data.workflows))
        ];
        statusHost.innerHTML = rows.join("");
        void platformReady;
      }

      var eventsHost = $("overview-events");
      if (eventsHost) {
        var events = (data.recent_events || []).slice(0, 8);
        eventsHost.innerHTML = events.length
          ? events.map(renderMiniEvent).join("")
          : '<div class="mini-event"><span>暂无事件 · 运行开始后事件总线将持续推送</span></div>';
      }

      if (data.uptime_s != null) {
        setText("overview-uptime", "已运行 " + fmtDuration(data.uptime_s));
      }
    }).catch(function (err) {
      if (!silent) toastError(err);
      var listHost = $("recent-runs");
      if (listHost && !listHost.children.length) {
        listHost.innerHTML = emptyBlock("概览数据加载失败", "请确认后端服务运行正常后点击重试。", true);
      }
    });
  }

  function startDemo() {
    if (state.demoBusy) return;
    var button = $("btn-demo");
    var original = button ? button.innerHTML : "";
    state.demoBusy = true;
    if (button) {
      button.disabled = true;
      button.textContent = "正在启动演示运行…";
    }
    setText("demo-hint", "正在创建运行，稍后跳转到运行监控…");
    api("/api/demo/run", {
      method: "POST",
      body: { policy: "adaptive", scenario: "ai4s-air-quality" }
    }).then(function (data) {
      var runId = data && data.run_id;
      if (!runId) throw new ApiFailure("后端未返回 run_id", "RUN_START_FAILED", 500);
      toast("演示运行已启动：" + shortId(runId), "success");
      setText("demo-hint", "AI4S 空气质量与气象因子归因分析");
      go("monitor", { run: runId });
      setTimeout(function () { loadOverview(true); loadRuns(); }, 1500);
    }).catch(toastError).then(function () {
      state.demoBusy = false;
      if (button) {
        button.disabled = false;
        button.innerHTML = original;
      }
    });
  }

  function enterOverview() {
    return Promise.all([loadHealth(), loadOverview(), loadOverview(true)]).then(function () {});
  }

  /* ------------------------------ 运行列表（共享） ------------------------------ */

  function loadRuns(limit) {
    return api("/api/runs?limit=" + (limit || 50)).then(function (data) {
      state.runs = (data && data.runs) || [];
      return state.runs;
    });
  }

  /* ------------------------------ 编排台 ------------------------------ */

  function ensureDag() {
    if (state.dag) return state.dag;
    var svg = $("dag-svg");
    if (!svg || !window.AgentWeaveDAG) {
      toast("DAG 渲染器未加载，请刷新页面", "error");
      return null;
    }
    state.dag = new window.AgentWeaveDAG.DAGView(svg, {
      onSelect: function (node) { showNodeDrawer(node); },
      onZoom: function (scale) { setText("dag-zoom", Math.round(scale * 100) + "%"); }
    });
    return state.dag;
  }

  function buildDagLegend(workflow) {
    var host = $("dag-legend");
    if (!host || !window.AgentWeaveDAG) return;
    var colors = window.AgentWeaveDAG.TYPE_COLORS;
    var labels = window.AgentWeaveDAG.TYPE_LABELS;
    var seen = {};
    var order = [];
    (workflow.nodes || []).forEach(function (node) {
      var type = node.type || "agent";
      if (seen[type]) return;
      seen[type] = true;
      order.push(type);
    });
    host.innerHTML = order.map(function (type) {
      return '<span class="legend-item"><span class="legend-dot" style="background:' +
        esc(colors[type] || "#8fa3c8") + '"></span>' + esc(labels[type] || type) + "</span>";
    }).join("");
  }

  function loadWorkflow(workflowId) {
    if (!workflowId) return Promise.resolve();
    state.selectedWorkflowId = workflowId;
    return api("/api/workflows/" + encodeURIComponent(workflowId)).then(function (workflow) {
      var dag = ensureDag();
      if (!dag) return;
      dag.setGraph(workflow);
      buildDagLegend(workflow);
      setHidden("dag-empty", true);
      var stage = $("dag-svg") && $("dag-svg").parentNode;
      if (stage) stage.classList.remove("is-empty");
      requestAnimationFrame(function () {
        dag.fit(0.62);
        applyDagZoomOverride();
      });
      return applyLatestRunStatuses(workflowId);
    }).catch(function (err) {
      var stage = $("dag-svg") && $("dag-svg").parentNode;
      if (stage) stage.classList.add("is-empty");
      setHidden("dag-empty", false);
      toastError(err);
    });
  }

  function applyLatestRunStatuses(workflowId) {
    return loadRuns(50).then(function (runs) {
      var latest = null;
      for (var i = 0; i < runs.length; i += 1) {
        if (runs[i].workflow_id === workflowId) { latest = runs[i]; break; }
      }
      if (!latest) {
        state.workflowRun = null;
        state.workflowStatuses = {};
        if (state.dag) state.dag.setStatuses({});
        return;
      }
      return api("/api/runs/" + encodeURIComponent(latest.run_id || latest.id)).then(function (run) {
        state.workflowRun = run;
        var nodes = run.nodes || run.node_states || {};
        var statuses = {};
        Object.keys(nodes).forEach(function (nodeId) {
          var info = nodes[nodeId];
          if (info && typeof info === "object" && info.status) statuses[nodeId] = info.status;
        });
        state.workflowStatuses = statuses;
        if (state.dag) state.dag.setStatuses(statuses);
      }).catch(function () {
        state.workflowRun = null;
        state.workflowStatuses = {};
        if (state.dag) state.dag.setStatuses({});
      });
    });
  }

  function nodeOutputText(nodeId) {
    var run = state.workflowRun;
    if (!run) return "该节点暂无运行输出。点击概览「运行演示场景」后，回到此处即可查看最近一次输出摘要。";
    var nodes = run.nodes || run.node_states || {};
    var info = nodes[nodeId];
    if (!info || typeof info !== "object") return "最近一次运行（" + shortId(run.run_id) + "）尚未执行到该节点。";
    var parts = [];
    if (info.status) parts.push("状态：" + statusLabel(info.status));
    if (info.agent_id) parts.push("Agent：" + info.agent_id);
    if (info.confidence != null) parts.push("置信度：" + fmtConf(info.confidence));
    var output = info.output;
    if (output != null && output !== "") {
      if (typeof output === "string") parts.push(output);
      else parts.push(JSON.stringify(output, null, 2));
    }
    if (info.error) parts.push("错误：" + info.error);
    return truncate(parts.join("\n"), 2400);
  }

  function showNodeDrawer(node) {
    var drawer = $("dag-drawer");
    if (drawer) drawer.classList.toggle("is-open", !!node);
    var emptyHost = $("drawer-empty");
    var bodyHost = $("drawer-body");
    if (!node) {
      if (emptyHost) emptyHost.hidden = false;
      if (bodyHost) bodyHost.hidden = true;
      return;
    }
    if (emptyHost) emptyHost.hidden = true;
    if (bodyHost) bodyHost.hidden = false;

    var type = node.type || "agent";
    var color = window.AgentWeaveDAG ? window.AgentWeaveDAG.typeColor(type) : "#38bdf8";
    var label = window.AgentWeaveDAG ? window.AgentWeaveDAG.typeLabel(type) : type;
    var badge = $("drawer-type");
    if (badge) {
      badge.textContent = label;
      badge.style.color = color;
    }
    setText("drawer-title", node.label || node.name || node.id);
    var desc = node.description || node.prompt || "该节点未填写描述信息。";
    setText("drawer-desc", desc);

    var configHost = $("drawer-config");
    if (configHost) {
      var rows = [
        kvRow("节点 ID", node.id),
        kvRow("类型", label + "（" + type + "）"),
        kvRow("Agent", node.agent || "—"),
        kvRow("工具", node.tool || "—")
      ];
      if (node.retry) rows.push(kvRow("重试", "最多 " + (node.retry.max_attempts || 2) + " 次"));
      if (node.timeout_s) rows.push(kvRow("超时", node.timeout_s + " 秒"));
      if (Array.isArray(node.outputs) && node.outputs.length) {
        rows.push(kvRow("输出字段", node.outputs.join(", ")));
      } else if (node.outputs && typeof node.outputs === "object") {
        rows.push(kvRow("输出字段", Object.keys(node.outputs).join(", ")));
      }
      if (node.loop) rows.push(kvRow("循环上限", (node.loop.max_iterations || 3) + " 轮"));
      if (node.verify) {
        var checks = node.verify.checks || node.verify.checkers;
        if (Array.isArray(checks) && checks.length) rows.push(kvRow("验证检查器", checks.join(", ")));
        if (node.verify.tolerance != null) rows.push(kvRow("验证容差", (Number(node.verify.tolerance) * 100).toFixed(0) + "%"));
      }
      configHost.innerHTML = rows.join("");
    }

    var outputHost = $("drawer-output");
    if (outputHost) outputHost.textContent = nodeOutputText(node.id);
    var rawHost = $("drawer-raw");
    if (rawHost) rawHost.textContent = truncate(JSON.stringify(node, null, 2), 4000);
  }

  function loadWorkflows() {
    return api("/api/workflows").then(function (data) {
      state.workflows = (data && data.workflows) || [];
      var select = $("wf-select");
      if (!select) return;
      if (!state.workflows.length) {
        select.innerHTML = "";
        select.disabled = true;
        setHidden("dag-empty", false);
        var stage = $("dag-svg") && $("dag-svg").parentNode;
        if (stage) stage.classList.add("is-empty");
        return;
      }
      select.disabled = false;
      select.innerHTML = state.workflows.map(function (wf) {
        var count = wf.node_count != null ? wf.node_count : (wf.nodes || []).length;
        return '<option value="' + esc(wf.id) + '">' + esc(wf.name || wf.id) +
          "（" + esc(count) + " 节点）</option>";
      }).join("");

      var preferred = state.selectedWorkflowId;
      var exists = state.workflows.some(function (wf) { return wf.id === preferred; });
      if (!exists) {
        var demo = state.workflows.filter(function (wf) { return wf.id === "wf-ai4s-air-quality"; })[0];
        preferred = demo ? demo.id : state.workflows[0].id;
      }
      select.value = preferred;
      return loadWorkflow(preferred);
    }).catch(function (err) {
      toastError(err);
    });
  }

  function enterOrchestrator() {
    return loadWorkflows().then(function () {
      if (state.dag) {
        requestAnimationFrame(function () {
          state.dag.fit(0.62);
          applyDagZoomOverride();
        });
      }
    });
  }

  // 演示/截图用：URL 带 ?zoom=0.95 时把 DAG 缩放到指定比例（节点更大、可读性更好）
  function applyDagZoomOverride() {
    var raw = state.dagZoomOverride;
    if (!state.dag || raw == null || raw === "") return;
    var k = Number(raw);
    if (!isFinite(k) || k <= 0) return;
    k = Math.min(1.6, Math.max(0.4, k));
    state.dag.zoomTo(k);
    var badge = $("dag-zoom");
    if (badge) badge.textContent = Math.round(k * 100) + "%";
  }

  /* ------------------------------ 运行监控 ------------------------------ */

  var NODE_SHORT = {
    pending: "等待",
    scheduled: "调度",
    running: "运行",
    succeeded: "成功",
    failed: "失败",
    skipped: "跳过",
    retrying: "重试",
    waiting_human: "待人工",
    paused: "暂停"
  };

  function fillRunSelect(runs) {
    var select = $("monitor-run-select");
    if (!select) return;
    if (!runs.length) {
      select.innerHTML = '<option value="">暂无运行记录</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    select.innerHTML = runs.map(function (run) {
      var id = run.run_id || run.id;
      return '<option value="' + esc(id) + '">' + esc(shortId(id)) + " · " +
        esc(statusLabel(run.status)) + " · " + esc(fmtTime(run.created_at || run.started_at)) + "</option>";
    }).join("");
  }

  function updateLive(text, cls) {
    var host = $("live-indicator");
    if (host) {
      host.classList.remove("is-live", "is-bad");
      if (cls) host.classList.add(cls);
    }
    setText("live-text", text);
  }

  function updateMonitorStatusPill() {
    var pill = $("monitor-status");
    if (!pill) return;
    var m = state.monitor;
    pill.className = "pill " + (m.runId ? pillClass(m.status) : "");
    pill.textContent = m.runId ? statusLabel(m.status) : "未选择";
    var cancel = $("btn-cancel-run");
    if (cancel) {
      var cancellable = m.runId && ["pending", "running", "paused", "waiting_human"].indexOf(m.status) >= 0;
      cancel.disabled = !cancellable;
    }
  }

  function resetMonitor(runId) {
    var m = state.monitor;
    m.runId = runId;
    m.run = null;
    m.workflowId = "";
    m.status = "pending";
    m.duration = null;
    m.tokens = null;
    m.successRate = null;
    m.loopIterations = 0;
    m.nodes = {};
    m.nodeOrder = [];
    m.events = [];
    m.decisions = [];
    m.loops = [];
    m.seenSeq = {};
    m.finished = false;
    m.errorToasted = false;
    renderMonitorAll();
    var eventsHost = $("monitor-events");
    if (eventsHost) eventsHost.innerHTML = '<div class="empty small">正在连接事件流…</div>';
    var scheduleHost = $("monitor-schedule");
    if (scheduleHost) scheduleHost.innerHTML = '<div class="empty small">等待 node.scheduled 事件…</div>';
  }

  function renderMonitorEmpty(title, hint) {
    var m = state.monitor;
    m.runId = null;
    m.run = null;
    m.status = "pending";
    m.nodes = {};
    m.nodeOrder = [];
    m.events = [];
    m.decisions = [];
    m.loops = [];
    m.finished = true;
    var summary = $("monitor-summary");
    if (summary) summary.innerHTML = emptyBlock(title, hint, true);
    var nodesHost = $("monitor-nodes");
    if (nodesHost) nodesHost.innerHTML = '<div class="empty small">选择一次运行后显示节点状态。</div>';
    var eventsHost = $("monitor-events");
    if (eventsHost) eventsHost.innerHTML = '<div class="empty small">选择一次运行后开始接收事件。</div>';
    var scheduleHost = $("monitor-schedule");
    if (scheduleHost) scheduleHost.innerHTML = '<div class="empty small">等待 node.scheduled 事件…</div>';
    var loopsHost = $("monitor-loops");
    if (loopsHost) loopsHost.innerHTML = "";
    setText("monitor-node-count", "0 节点");
    updateLive("未连接", "");
    updateMonitorStatusPill();
  }

  function closeEventSource() {
    var m = state.monitor;
    if (m.es) {
      try { m.es.close(); } catch (err) { /* ignore */ }
      m.es = null;
    }
  }

  function enterMonitor(query) {
    return loadRuns(50).then(function (runs) {
      fillRunSelect(runs);
      var target = query && query.run;
      if (target) return openRun(target);
      if (!runs.length) {
        renderMonitorEmpty("还没有运行记录", "回到概览点击「运行演示场景」，即可实时观看多 Agent 协作与自循环修复过程。");
        return null;
      }
      if (state.monitor.runId) {
        renderMonitorAll();
        return null;
      }
      return openRun(runs[0].run_id || runs[0].id);
    });
  }

  function openRun(runId) {
    if (!runId) return Promise.resolve();
    closeEventSource();
    resetMonitor(runId);
    var select = $("monitor-run-select");
    if (select) select.value = runId;
    updateLive("连接中…", "");
    connectSse(runId);
    return api("/api/runs/" + encodeURIComponent(runId)).then(function (run) {
      applyRunDetail(run);
      return loadRunWorkflowNodes(run);
    }).catch(function (err) {
      if (err && err.status === 404) toast("运行 " + shortId(runId) + " 尚未注册，等待事件流…", "warn");
      else toastError(err);
    });
  }

  // 用工作流拓扑补全尚未执行的节点，节点网格里以 pending 灰色占位，
  // 保证从「只有 8 个已执行节点」也能看到完整 12 节点编排。
  function loadRunWorkflowNodes(run) {
    var m = state.monitor;
    var wfId = run && (run.workflow_id || m.workflowId);
    if (!wfId) return Promise.resolve();
    return api("/api/workflows/" + encodeURIComponent(wfId)).then(function (wf) {
      var nodes = (wf && wf.nodes) || [];
      var order = [];
      nodes.forEach(function (node) {
        var id = node && node.id;
        if (!id) return;
        order.push(id);
        if (!m.nodes[id]) {
          m.nodes[id] = {
            status: "pending",
            agent_id: node.agent || "",
            confidence: null,
            error: ""
          };
        } else if (!m.nodes[id].agent_id && node.agent) {
          m.nodes[id].agent_id = node.agent;
        }
      });
      // 工作流节点优先按拓扑顺序排列，运行中额外出现的节点追加在末尾
      m.nodeOrder.forEach(function (id) {
        if (order.indexOf(id) < 0) order.push(id);
      });
      m.nodeOrder = order;
      renderMonitorAll();
    }).catch(function () {});
  }

  function applyRunDetail(run) {
    if (!run) return;
    var m = state.monitor;
    var runId = run.run_id || run.id;
    if (runId && m.runId && String(runId) !== String(m.runId)) return;
    m.run = run;
    m.workflowId = run.workflow_id || m.workflowId;
    m.status = run.status || m.status;
    var metrics = run.metrics || {};
    m.duration = metrics.duration_s != null ? metrics.duration_s : m.duration;
    m.tokens = metrics.tokens_total != null ? metrics.tokens_total : (metrics.tokens != null ? metrics.tokens : m.tokens);
    m.successRate = metrics.success_rate != null ? metrics.success_rate : m.successRate;
    var selfLoop = run.self_loop || {};
    var history = normalizeLoopRounds(selfLoop);
    if (history.length) m.loopIterations = history[history.length - 1].iteration || history.length;
    if (metrics.self_loop_iterations != null) m.loopIterations = metrics.self_loop_iterations;

    var rawNodes = run.nodes || run.node_states || {};
    Object.keys(rawNodes).forEach(function (nodeId) {
      var info = rawNodes[nodeId];
      if (!info || typeof info !== "object") info = { status: String(info || "pending") };
      if (m.nodeOrder.indexOf(nodeId) < 0) m.nodeOrder.push(nodeId);
      m.nodes[nodeId] = {
        status: info.status || "pending",
        agent_id: info.agent_id || (m.nodes[nodeId] && m.nodes[nodeId].agent_id) || "",
        confidence: info.confidence != null ? info.confidence : (m.nodes[nodeId] && m.nodes[nodeId].confidence),
        error: info.error || ""
      };
    });
    if (!m.decisions.length) {
      var scheduled = run.scheduling || run.decisions || [];
      if (Array.isArray(scheduled)) {
        scheduled.forEach(function (item) {
          if (item && (item.node_id || item.node)) {
            m.decisions.push({
              node_id: item.node_id || item.node,
              agent_id: item.agent_id,
              score: item.score,
              reason: item.reason,
              agent_load: item.agent_load != null ? item.agent_load : item.load
            });
          }
        });
      }
    }
    renderMonitorAll();
    if (m.status === "succeeded" || m.status === "failed" || m.status === "cancelled" || m.status === "rejected") {
      m.finished = true;
      // 保留 SSE：服务端会先回放历史事件，收到 run.finished 后自行关闭连接
      updateLive("运行已结束", m.status === "succeeded" ? "" : "is-bad");
    }
  }

  function normalizeLoopRounds(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw.map(normalizeLoopRound);
    var list = raw.iterations || raw.rounds || raw.history || raw.rounds_history || [];
    if (Array.isArray(list)) return list.map(normalizeLoopRound);
    if (raw.iteration || raw.round || raw.confidence != null) return [normalizeLoopRound(raw)];
    return [];
  }

  function normalizeLoopRound(item, index) {
    if (item == null) return { iteration: index + 1, confidence: null, passed: null, failures: [], feedback: "" };
    if (typeof item !== "object") return { iteration: index + 1, confidence: null, passed: null, failures: [], feedback: String(item) };
    var failures = item.failures || item.failed_checks || item.failed || [];
    if (!Array.isArray(failures)) {
      failures = typeof failures === "string" ? [failures] : Object.keys(failures || {});
    }
    return {
      iteration: item.iteration != null ? item.iteration : index + 1,
      max: item.max_iterations || item.max || null,
      confidence: item.confidence != null ? item.confidence : null,
      passed: item.verify_passed != null ? item.verify_passed : (item.passed != null ? item.passed : null),
      failures: failures,
      feedback: item.feedback_preview || item.feedback || item.action || item.repair || item.repair_action || ""
    };
  }

  function connectSse(runId) {
    closeEventSource();
    var m = state.monitor;
    m.finished = false;
    // 静态截图模式：跳过 SSE 长连接，避免 headless 截图挂起
    if (/[?&]noevents=1/.test(location.search)) {
      m.finished = true;
      return;
    }
    var source;
    try {
      source = new EventSource("/api/runs/" + encodeURIComponent(runId) + "/events");
    } catch (err) {
      updateLive("事件流不可用", "is-bad");
      toastError(err);
      return;
    }
    m.es = source;
    source.onopen = function () {
      m.errorToasted = false;
      if (m.es !== source) return;
      // 注意：不能用 m.finished 判断（运行详情可能先返回「已结束」状态），
      // 否则会把服务端正在回放的历史事件流提前关掉。
      updateLive(m.finished ? "运行已结束 · 事件回放中" : "实时接收中", m.finished ? "" : "is-live");
    };
    source.onerror = function () {
      if (m.finished || m.es !== source) return;
      updateLive("连接中断，正在自动重连…", "is-bad");
      if (!m.errorToasted) {
        m.errorToasted = true;
        toast("事件流连接中断，浏览器将自动重连并携带 Last-Event-ID 续传", "warn");
      }
    };
    SSE_TYPES.forEach(function (type) {
      source.addEventListener(type, function (event) {
        handleSseEvent(type, event);
      });
    });
    source.addEventListener("message", function (event) {
      handleSseEvent("message", event);
    });
  }

  function handleSseEvent(type, eventSourceEvent) {
    var m = state.monitor;
    var raw = eventSourceEvent && eventSourceEvent.data;
    var event = null;
    try {
      event = raw ? JSON.parse(raw) : null;
    } catch (err) {
      event = { type: type, payload: { raw: truncate(raw, 300) } };
    }
    if (!event) return;
    var eventType = type === "message" ? (event.type || "log") : (event.type || type);
    var seq = Number((eventSourceEvent && eventSourceEvent.lastEventId) || event.seq || 0);
    if (seq) {
      if (m.seenSeq[seq]) return;
      m.seenSeq[seq] = 1;
    }
    var payload = (event.payload && typeof event.payload === "object") ? event.payload : {};
    if (!event.payload && event.data && typeof event.data === "object") payload = event.data;
    var nodeId = event.node_id || payload.node_id || payload.nodeId || "";
    var record = {
      type: eventType,
      nodeId: nodeId,
      agentId: event.agent_id || payload.agent_id || "",
      ts: event.ts || Date.now() / 1000,
      payload: payload
    };
    m.events.push(record);
    appendEventRow(record);

    var known = m.runId;
    switch (eventType) {
      case "run.started":
        m.status = "running";
        m.workflowId = payload.workflow_id || m.workflowId;
        break;
      case "plan.ready":
        (payload.candidates || []).forEach(function (candidate) {
          if (candidate && candidate.node_id) {
            m.decisions.push({
              node_id: candidate.node_id,
              agent_id: candidate.agent_id,
              score: candidate.score,
              reason: candidate.reason,
              agent_load: candidate.agent_load
            });
          }
        });
        break;
      case "node.scheduled":
        if (nodeId) {
          ensureNode(nodeId);
          m.nodes[nodeId].status = "scheduled";
          m.nodes[nodeId].agent_id = payload.agent_id || m.nodes[nodeId].agent_id;
        }
        m.decisions.push({
          node_id: payload.node_id || nodeId,
          agent_id: payload.agent_id,
          score: payload.score,
          reason: payload.reason,
          agent_load: payload.agent_load
        });
        break;
      case "node.started":
        if (nodeId) {
          ensureNode(nodeId);
          m.nodes[nodeId].status = "running";
          m.nodes[nodeId].agent_id = payload.agent_id || m.nodes[nodeId].agent_id;
        }
        break;
      case "node.retrying":
        if (nodeId) {
          ensureNode(nodeId);
          m.nodes[nodeId].status = "retrying";
        }
        break;
      case "node.succeeded":
        if (nodeId) {
          ensureNode(nodeId);
          m.nodes[nodeId].status = "succeeded";
          if (payload.confidence != null) m.nodes[nodeId].confidence = payload.confidence;
        }
        break;
      case "node.failed":
        if (nodeId) {
          ensureNode(nodeId);
          m.nodes[nodeId].status = "failed";
          m.nodes[nodeId].error = payload.error || "";
        }
        break;
      case "node.skipped":
        if (nodeId) {
          ensureNode(nodeId);
          m.nodes[nodeId].status = "skipped";
        }
        break;
      case "verify.result":
        if (nodeId) {
          ensureNode(nodeId);
          m.nodes[nodeId].verify = {
            passed: payload.passed,
            confidence: payload.confidence,
            checks: payload.checks || []
          };
          if (payload.confidence != null) m.nodes[nodeId].confidence = payload.confidence;
        }
        break;
      case "loop.iteration":
        m.loops.push(normalizeLoopRound({
          iteration: payload.iteration,
          max_iterations: payload.max_iterations,
          confidence: payload.confidence,
          verify_passed: payload.verify_passed,
          failures: payload.failures,
          feedback_preview: payload.feedback_preview
        }, m.loops.length));
        m.loopIterations = Math.max(m.loopIterations || 0, Number(payload.iteration) || 0);
        break;
      case "run.finished":
        m.status = payload.status || "succeeded";
        if (payload.duration_s != null) m.duration = payload.duration_s;
        if (payload.tokens != null) m.tokens = payload.tokens;
        if (payload.success_rate != null) m.successRate = payload.success_rate;
        if (payload.self_loop_iterations != null) m.loopIterations = payload.self_loop_iterations;
        m.finished = true;
        updateLive("运行已结束", m.status === "succeeded" ? "" : "is-bad");
        // 服务端在 run.finished 帧后即关闭流；浏览器对已结束的流会自动重连，
        // 这里立刻主动关闭，避免「回放→结束→重连→再回放」空转。
        closeEventSource();
        setTimeout(function () {
          if (state.monitor.runId === known) {
            api("/api/runs/" + encodeURIComponent(known)).then(function (run) {
              if (state.monitor.runId === known) applyRunDetail(run);
            }).catch(function () {});
          }
        }, 200);
        toast("运行" + statusLabel(m.status) + " · 耗时 " + fmtDuration(m.duration) +
          " · 自循环 " + fmtInt(m.loopIterations) + " 轮", m.status === "succeeded" ? "success" : "warn");
        break;
      case "run.cancelled":
        m.status = "cancelled";
        m.finished = true;
        updateLive("运行已取消", "is-bad");
        closeEventSource();
        break;
      case "run.paused":
        m.status = "paused";
        break;
      case "human.required":
        m.status = "waiting_human";
        break;
      default:
        break;
    }
    renderMonitorAll();
  }

  function ensureNode(nodeId) {
    var m = state.monitor;
    if (m.nodeOrder.indexOf(nodeId) < 0) m.nodeOrder.push(nodeId);
    if (!m.nodes[nodeId]) m.nodes[nodeId] = { status: "pending", agent_id: "", confidence: null, error: "" };
    return m.nodes[nodeId];
  }

  function describeEvent(record) {
    var payload = record.payload || {};
    var nodeId = record.nodeId || payload.node_id || "";
    switch (record.type) {
      case "run.started":
        return "工作流 " + (payload.workflow_name || payload.workflow_id || "—") +
          " · 策略 " + (payload.policy || "—") + " 开始运行";
      case "run.finished":
        return "运行" + statusLabel(payload.status) + " · 耗时 " + fmtDuration(payload.duration_s) +
          " · 成功节点 " + fmtInt(payload.nodes) + " · self-loop " + fmtInt(payload.self_loop_iterations) + " 轮";
      case "run.cancelled":
        return "运行已取消";
      case "plan.ready":
        return "调度计划就绪 · 候选节点 " + ((payload.candidates || []).length) + " 个";
      case "node.scheduled":
        return nodeId + " → " + (payload.agent_id || "—") + " · score " + fmtNum(payload.score, 3) +
          (payload.agent_load != null ? " · 负载 " + fmtNum(payload.agent_load, 2) : "");
      case "node.started":
        return nodeId + " 开始执行" + (payload.attempt > 1 ? "（第 " + payload.attempt + " 次尝试）" : "");
      case "node.succeeded":
        return nodeId + " 执行成功 · 置信度 " + fmtConf(payload.confidence) + " · " + fmtDuration(payload.latency_s);
      case "node.failed":
        return nodeId + " 执行失败 · " + truncate(payload.error || "未知错误", 70);
      case "node.skipped":
        return nodeId + " 已跳过";
      case "agent.thought":
        return (nodeId ? nodeId + " " : "") + "思考：" + truncate(payload.text, 70);
      case "agent.decision":
        return (nodeId ? nodeId + " " : "") + "决策：" + truncate(payload.text, 60) +
          "（" + fmtConf(payload.confidence) + "）";
      case "tool.called":
        return "调用工具 " + (payload.name || "—") + " " + truncate(safeJson(payload.args), 60);
      case "tool.result":
        return (payload.name || "工具") + (payload.ok === false ? " 失败" : " 成功") +
          " · " + fmtDuration(payload.duration_s);
      case "llm.called":
        return "调用模型 " + (payload.provider || payload.model || "—");
      case "llm.result":
        return "模型返回 · " + fmtInt(payload.tokens_out) + " tokens · " + fmtDuration(payload.latency_s);
      case "verify.result":
        return (nodeId ? nodeId + " " : "") + "验证" + (payload.passed ? "通过" : "未通过") +
          " · 置信度 " + fmtConf(payload.confidence) +
          " · " + ((payload.checks || []).length) + " 项检查";
      case "loop.iteration":
        return (nodeId ? nodeId + " " : "") + "自循环第 " + payload.iteration + "/" + (payload.max_iterations || 3) +
          " 轮 · " + (payload.verify_passed ? "通过" : "未通过") + " · 置信度 " + fmtConf(payload.confidence);
      case "artifact.created":
        return "生成产物 " + (payload.name || "—") + "（" + (payload.kind || "file") + "）";
      case "human.required":
        return "等待人工确认：" + truncate(payload.prompt || payload.message || nodeId, 70);
      case "human.decided":
        return "人工确认结果：" + (payload.approved === false ? "拒绝" : "通过");
      default:
        return truncate(payload.summary || payload.message || payload.text || record.type, 90);
    }
  }

  function safeJson(value) {
    if (value == null) return "{}";
    if (typeof value === "string") return value;
    try { return JSON.stringify(value); } catch (err) { return String(value); }
  }

  function appendEventRow(record) {
    var host = $("monitor-events");
    if (!host) return;
    var placeholder = host.querySelector(".empty");
    if (placeholder && host.children.length === 1) host.innerHTML = "";
    var cls = evtClass(record.type);
    var row = document.createElement("div");
    row.className = "event-row " + cls;
    row.innerHTML = "<time>" + esc(fmtTime(record.ts)) + "</time>" +
      '<span class="event-type ' + cls + '">' + esc(truncate(record.type, 14)) + "</span>" +
      '<span class="event-summary" title="' + esc(describeEvent(record)) + '">' +
      esc(describeEvent(record)) + "</span>";
    host.appendChild(row);
    while (host.children.length > 240) host.removeChild(host.firstChild);
    host.scrollTop = host.scrollHeight;
  }

  function renderSummary() {
    var m = state.monitor;
    var host = $("monitor-summary");
    if (!host) return;
    if (!m.runId) {
      host.innerHTML = emptyBlock("尚未选择运行", "在右上角选择一次运行，或从概览点击「运行演示场景」。", true);
      return;
    }
    var run = m.run || {};
    var workflowName = run.workflow_name || m.workflowId || "—";
    var chips = [
      chip("运行", shortId(m.runId), "cyan"),
      chip("工作流", workflowName),
      chip("状态", statusLabel(m.status), pillClass(m.status) ? "" : ""),
      chip("耗时", fmtDuration(m.duration)),
      chip("Tokens", fmtInt(m.tokens)),
      chip("成功率", m.successRate != null ? Math.round(Number(m.successRate) * 100) + "%" : "—"),
      chip("自循环", fmtInt(m.loopIterations) + " 轮")
    ];
    if (run.policy) chips.push(chip("策略", run.policy));
    host.innerHTML = chips.join("");
  }

  function renderNodes() {
    var m = state.monitor;
    var host = $("monitor-nodes");
    if (!host) return;
    var ids = m.nodeOrder;
    setText("monitor-node-count", (ids.length ? ids.length : 0) + " 节点");
    if (!ids.length) {
      host.innerHTML = '<div class="empty small">暂无节点状态，等待 node.started 事件…</div>';
      return;
    }
    host.innerHTML = ids.map(function (nodeId) {
      var info = m.nodes[nodeId] || {};
      var status = info.status || "pending";
      var title = nodeId + " · " + statusLabel(status) +
        (info.agent_id ? " · agent=" + info.agent_id : "") +
        (info.confidence != null ? " · 置信度 " + fmtConf(info.confidence) : "") +
        (info.error ? " · 错误：" + info.error : "");
      return '<div class="node-chip status-' + esc(status) + '" title="' + esc(title) + '">' +
        '<span class="node-dot"></span>' +
        '<span class="node-name">' + esc(nodeId) + "</span>" +
        '<span class="node-status">' + esc(NODE_SHORT[status] || status) + "</span></div>";
    }).join("");
  }

  function renderSchedule() {
    var m = state.monitor;
    var host = $("monitor-schedule");
    if (!host) return;
    var latest = {};
    m.decisions.forEach(function (item) {
      if (item && item.node_id) latest[item.node_id] = item;
    });
    // 按节点执行顺序展示调度决策，而不是简单倒序：
    // 先展示本轮真正发生调度的节点（含 start/plan/scan 等），超出的折叠成计数。
    var orderIndex = {};
    m.nodeOrder.forEach(function (id, idx) { orderIndex[id] = idx; });
    var ordered = Object.keys(latest).map(function (key) { return latest[key]; });
    ordered.sort(function (a, b) {
      var ia = orderIndex[a.node_id] != null ? orderIndex[a.node_id] : 999;
      var ib = orderIndex[b.node_id] != null ? orderIndex[b.node_id] : 999;
      return ia - ib;
    });
    if (!ordered.length) {
      host.innerHTML = '<div class="empty small">等待 node.scheduled 事件…</div>';
      return;
    }
    var LIMIT = 3;
    var list = ordered.slice(0, LIMIT);
    var more = ordered.length - list.length;
    var cards = list.map(function (item) {
      var meta = [];
      if (item.agent_id) meta.push("agent <i>" + esc(item.agent_id) + "</i>");
      if (item.agent_load != null) meta.push("负载 <i>" + esc(fmtNum(item.agent_load, 2)) + "</i>");
      if (item.reason) meta.push('<span title="' + esc(item.reason) + '">' + esc(truncate(item.reason, 46)) + "</span>");
      return '<div class="decision-card">' +
        '<span class="d-node">' + esc(item.node_id) + "</span>" +
        '<span class="d-score">score ' + esc(fmtNum(item.score, 3)) + "</span>" +
        '<div class="d-meta">' + meta.join("") + "</div></div>";
    }).join("");
    if (more > 0) {
      cards += '<div class="schedule-more">共 ' + ordered.length + " 条调度决策，其余 " + more + " 条随事件流实时更新</div>";
    }
    host.innerHTML = cards;
  }

  function renderLoops() {
    var m = state.monitor;
    var host = $("monitor-loops");
    if (!host) return;
    if (!m.loops.length) {
      host.innerHTML = m.loopIterations
        ? '<span class="loop-badge">自循环 ' + fmtInt(m.loopIterations) + " 轮</span>"
        : "";
      return;
    }
    host.innerHTML = m.loops.map(function (round) {
      var passed = round.passed === true;
      return '<span class="loop-badge' + (passed ? " passed" : "") + '">第 ' + esc(round.iteration) +
        (round.max ? "/" + esc(round.max) : "") + " 轮 · " + esc(fmtConf(round.confidence)) +
        " · " + (passed ? "通过" : "修复") + "</span>";
    }).join("");
  }

  function renderMonitorAll() {
    renderSummary();
    renderNodes();
    renderSchedule();
    renderLoops();
    updateMonitorStatusPill();
  }

  function bindMonitor() {
    var select = $("monitor-run-select");
    if (select) {
      select.addEventListener("change", function () {
        var value = select.value;
        if (!value) return;
        var target = "#/monitor?run=" + encodeURIComponent(value);
        if (location.hash !== target) history.replaceState(null, "", target);
        openRun(value);
      });
    }
    var cancel = $("btn-cancel-run");
    if (cancel) {
      cancel.addEventListener("click", function () {
        var m = state.monitor;
        if (!m.runId) return;
        cancel.disabled = true;
        api("/api/runs/" + encodeURIComponent(m.runId) + "/cancel", { method: "POST", body: {} })
          .then(function (data) {
            toast(data && data.cancelled ? "已发送取消请求" : "取消请求已受理", "warn");
            m.status = "cancelled";
            m.finished = true;
            closeEventSource();
            updateLive("运行已取消", "is-bad");
            renderMonitorAll();
          })
          .catch(toastError)
          .then(function () { updateMonitorStatusPill(); });
      });
    }
  }

  /* ------------------------------ 验证中心 ------------------------------ */

  var LAYER_ORDER = ["structure", "numeric", "rerun", "cross", "statistical"];
  var LAYER_NAMES = {
    structure: "结构层",
    numeric: "数值层",
    rerun: "复算层",
    cross: "交叉层",
    statistical: "统计层"
  };

  function fillVerifyRunSelect(runs) {
    var select = $("verify-run-select");
    if (!select) return;
    if (!runs.length) {
      select.innerHTML = '<option value="">暂无运行记录</option>';
      select.disabled = true;
      return;
    }
    select.disabled = false;
    select.innerHTML = runs.map(function (run) {
      var id = run.run_id || run.id;
      return '<option value="' + esc(id) + '">' + esc(shortId(id)) + " · " +
        esc(statusLabel(run.status)) + " · " + esc(fmtTime(run.created_at || run.started_at)) + "</option>";
    }).join("");
  }

  function loadStrategies() {
    return api("/api/verify/strategies").then(function (data) {
      state.strategies = data;
      renderVerifyLegend((data && data.layers) || [], (data && data.checkers) || []);
      return data;
    }).catch(function () {
      return null;
    });
  }

  var LAYER_ALIAS = { cross_agent: "cross", "L3-复算": "rerun", auto: "" };

  function normalizeLayer(layer) {
    if (!layer) return null;
    if (typeof layer === "string") {
      var id = LAYER_ALIAS[layer] != null ? LAYER_ALIAS[layer] : layer;
      return { id: id, name: LAYER_NAMES[id] || layer };
    }
    var rawId = layer.id || layer.name || "";
    var id2 = LAYER_ALIAS[rawId] != null ? LAYER_ALIAS[rawId] : rawId;
    return { id: id2, name: layer.label || layer.name || LAYER_NAMES[id2] || rawId };
  }

  function renderVerifyLegend(layers, checkers, counts) {
    var host = $("verify-layer-legend");
    if (!host) return;
    var normalized = (layers || []).map(normalizeLayer).filter(Boolean);
    var source = normalized.length ? normalized : LAYER_ORDER.map(function (id) {
      return { id: id, name: LAYER_NAMES[id] };
    });
    host.innerHTML = source.map(function (layer) {
      var id = layer.id || "";
      var color = LAYER_COLORS[id] || "#8fa3c8";
      var count = counts
        ? (counts[id] || 0)
        : checkers.filter(function (checker) {
          var checkerId = LAYER_ALIAS[checker.layer] != null ? LAYER_ALIAS[checker.layer] : checker.layer;
          return checkerId === id;
        }).length;
      return '<span class="layer-chip"><span class="legend-dot" style="background:' + esc(color) + '"></span>' +
        esc(layer.name || LAYER_NAMES[id] || id) + (count ? " · " + count : "") + "</span>";
    }).join("");
  }

  function enterVerify(query) {
    return Promise.all([loadRuns(50), loadStrategies()]).then(function (result) {
      var runs = result[0] || [];
      fillVerifyRunSelect(runs);
      var target = query && query.run;
      if (!target) {
        if (!runs.length) {
          renderVerifyEmpty("还没有运行记录", "运行一次演示场景后，五层验证结果会自动汇总到这里。");
          return null;
        }
        var exists = runs.some(function (run) {
          return String(run.run_id || run.id) === String(state.verifyRunId);
        });
        target = exists ? state.verifyRunId : (runs[0].run_id || runs[0].id);
      }
      state.verifyRunId = target;
      var select = $("verify-run-select");
      if (select) select.value = target;
      return loadVerification(target);
    });
  }

  function setRing(conf) {
    var ratio = normConf(conf);
    var ring = $("verify-ring");
    var percent = ratio == null ? 0 : Math.max(0, Math.min(1, ratio));
    if (ring) {
      ring.style.strokeDashoffset = String(364.42 * (1 - percent));
      ring.classList.remove("is-warn", "is-bad");
      if (ratio != null) {
        if (percent < 0.7) ring.classList.add("is-bad");
        else if (percent < 0.9) ring.classList.add("is-warn");
      }
    }
    setText("verify-confidence", ratio == null ? "–" : (percent * 100).toFixed(1) + "%");
    var bar = $("verify-progress");
    if (bar) bar.style.width = (percent * 100).toFixed(1) + "%";
  }

  function renderVerifyEmpty(title, hint) {
    setRing(null);
    var status = $("verify-status");
    if (status) {
      status.className = "pill";
      status.textContent = "等待运行";
    }
    setText("verify-hint", hint || "选择一次运行查看五层验证结果。");
    var tbody = $("verify-tbody");
    if (tbody) {
      tbody.innerHTML = '<tr><td colspan="5">' + emptyBlock(title || "暂无检查数据", "运行结束后，结构 → 数值 → 复算 → 交叉 → 统计五层结果会逐项列出。", true) + "</td></tr>";
    }
    var loopHost = $("verify-loop");
    if (loopHost) loopHost.innerHTML = '<div class="empty small">暂无自循环记录。</div>';
    setText("verify-loop-count", "0 轮");
  }

  function loadVerification(runId) {
    if (!runId) return Promise.resolve();
    state.verifyRunId = runId;
    var tbody = $("verify-tbody");
    if (tbody) tbody.innerHTML = '<tr><td colspan="5"><div class="empty small">正在加载验证结果…</div></td></tr>';
    return Promise.all([
      api("/api/runs/" + encodeURIComponent(runId) + "/verification"),
      api("/api/runs/" + encodeURIComponent(runId) + "/loop_status").catch(function () { return null; })
    ]).then(function (results) {
      if (String(state.verifyRunId) !== String(runId)) return;
      var data = results[0] || {};
      data.__loopStatus = results[1] || null;
      renderVerification(data);
    }).catch(function (err) {
      if (err && err.status === 404) {
        renderVerifyEmpty("该运行暂无验证结果", "运行仍在进行中？验证结果会在运行结束后自动生成。");
        return;
      }
      toastError(err);
    });
  }

  function normalizeVerification(raw) {
    var items = [];
    if (!raw) return items;
    if (Array.isArray(raw)) {
      raw.forEach(function (entry) {
        if (entry && typeof entry === "object") items.push(entry);
      });
      return items;
    }
    if (typeof raw !== "object") return items;
    if (Array.isArray(raw.checks) || raw.passed != null || raw.confidence != null) {
      items.push(raw);
      return items;
    }
    Object.keys(raw).forEach(function (key) {
      var value = raw[key];
      if (Array.isArray(value)) {
        value.forEach(function (entry) {
          if (entry && typeof entry === "object") {
            var copy = {};
            Object.keys(entry).forEach(function (field) { copy[field] = entry[field]; });
            if (!copy.layer) copy.layer = key;
            items.push(copy);
          }
        });
      } else if (value && typeof value === "object") {
        var one = {};
        Object.keys(value).forEach(function (field) { one[field] = value[field]; });
        if (!one.layer) one.layer = key;
        items.push(one);
      }
    });
    return items;
  }

  function collectChecks(verification, checkers) {
    var layerOf = {};
    (checkers || []).forEach(function (checker) {
      if (checker && checker.name) layerOf[checker.name] = checker.layer;
    });
    var out = [];
    normalizeVerification(verification).forEach(function (result) {
      var layer = result.layer || "";
      var checks = result.checks || result.results || [];
      if (!Array.isArray(checks)) checks = [];
      checks.forEach(function (check) {
        if (!check || typeof check !== "object") check = { name: String(check) };
        var name = check.name || check.checker || "unknown";
        // 引擎可能把整轮结果标成 auto / L3-复算，逐项检查器优先用注册表里的五层归属
        var registered = layerOf[name];
        var checkLayer = registered ||
          (LAYER_ORDER.indexOf(layer) >= 0 ? layer : (check.layer || layer || ""));
        out.push({
          layer: LAYER_ORDER.indexOf(checkLayer) >= 0 ? checkLayer : checkLayer,
          name: name,
          label: check.label || "",
          passed: check.passed != null ? check.passed : (check.ok != null ? check.ok : null),
          detail: check.detail || check.message || (check.evidence ? safeJson(check.evidence) : ""),
          duration: check.duration_s != null ? check.duration_s
            : (check.latency_s != null ? check.latency_s
              : (check.elapsed_s != null ? check.elapsed_s : null))
        });
      });
      if (!checks.length && result.passed != null) {
        out.push({
          layer: layer,
          name: result.name || "verify",
          label: "",
          passed: result.passed,
          detail: result.feedback || "",
          duration: result.duration_s != null ? result.duration_s : null
        });
      }
    });
    return out;
  }

  function renderVerification(data) {
    var checkers = data.checkers || (state.strategies && state.strategies.checkers) || [];
    var entries = normalizeVerification(data.verification != null ? data.verification : data);
    // 同一次运行可能有「第 1 轮未通过 → 第 2 轮修复通过」多条验证结论，
    // 默认只展示最终轮（优先取最后一条 passed=true 的结论）。
    var finalEntry = pickFinalVerification(entries);
    var checks = collectChecks(finalEntry || data.verification || data, checkers);
    var conf = finalEntry && finalEntry.confidence != null ? finalEntry.confidence : data.confidence;
    if (conf == null && checks.length) {
      conf = checks.filter(function (check) { return check.passed === true; }).length / checks.length;
    }
    var passed = finalEntry && finalEntry.passed != null ? finalEntry.passed : data.passed;
    if (passed == null) {
      passed = checks.length ? checks.every(function (check) { return check.passed !== false; }) : null;
    }
    setRing(conf);

    var status = $("verify-status");
    if (status) {
      if (!checks.length) {
        status.className = "pill";
        status.textContent = "等待结果";
      } else if (passed) {
        status.className = "pill ok";
        status.textContent = "最终通过";
      } else {
        status.className = "pill " + (normConf(conf) != null && normConf(conf) >= 0.7 ? "warn" : "bad");
        status.textContent = "验证未通过";
      }
    }
    var passedCount = checks.filter(function (check) { return check.passed === true; }).length;
    var failedCount = checks.filter(function (check) { return check.passed === false; }).length;
    var skippedCount = checks.length - passedCount - failedCount;
    var roundNote = entries.length > 1 ? "共 " + entries.length + " 轮验证 · 最终轮 " : "";
    setText("verify-hint", checks.length
      ? roundNote + checks.length + " 项检查 · 通过 " + passedCount + " · 失败 " + failedCount +
        (skippedCount ? " · 跳过 " + skippedCount : "")
      : "该运行尚未产生检查明细，运行结束后自动汇总五层验证结果。");
    renderVerifyTable(checks, checkers);
    // 图例计数与表格保持一致：按当前展示的最终轮检查项统计各层数量
    var layerCounts = {};
    checks.forEach(function (check) {
      if (check.layer) layerCounts[check.layer] = (layerCounts[check.layer] || 0) + 1;
    });
    var layerList = data.layers || (state.strategies && state.strategies.layers) || [];
    renderVerifyLegend(layerList, checkers, checks.length ? layerCounts : null);
    var loopSource = (data.__loopStatus && data.__loopStatus.self_loop) || data.self_loop || data.loop;
    renderVerifyLoops(loopSource);
  }

  function pickFinalVerification(entries) {
    if (!entries || !entries.length) return null;
    var withResult = entries.filter(function (entry) {
      return entry && (entry.passed != null || entry.confidence != null || (entry.checks && entry.checks.length));
    });
    if (!withResult.length) return entries[entries.length - 1] || null;
    for (var i = withResult.length - 1; i >= 0; i -= 1) {
      if (withResult[i].passed === true) return withResult[i];
    }
    return withResult[withResult.length - 1];
  }

  function renderVerifyTable(checks, checkers) {
    var tbody = $("verify-tbody");
    if (!tbody) return;
    if (!checks.length) {
      tbody.innerHTML = '<tr><td colspan="5">' +
        emptyBlock("暂无检查数据", "运行结束后按结构层 → 数值层 → 复算层 → 交叉层 → 统计层顺序展示。", true) +
        "</td></tr>";
      return;
    }
    var labelOf = {};
    (checkers || []).forEach(function (checker) {
      if (checker && checker.name) labelOf[checker.name] = checker.label || "";
    });
    var sorted = checks.slice().sort(function (a, b) {
      var ai = LAYER_ORDER.indexOf(a.layer);
      var bi = LAYER_ORDER.indexOf(b.layer);
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
    });
    tbody.innerHTML = sorted.map(function (check) {
      var color = LAYER_COLORS[check.layer] || "#8fa3c8";
      var layerName = LAYER_NAMES[check.layer] || check.layer || "通用";
      var statusHtml = check.passed === true
        ? '<span class="status-pass">通过</span>'
        : (check.passed === false ? '<span class="status-fail">失败</span>' : '<span class="status-skip">跳过</span>');
      var label = check.label || labelOf[check.name] || "";
      return "<tr>" +
        '<td><span class="layer-tag" style="color:' + esc(color) + ";border-color:" + esc(color) +
        '">' + esc(layerName) + "</span></td>" +
        '<td><span class="check-name">' + esc(check.name) + "</span>" +
        (label ? ' <small class="check-label muted">' + esc(label) + "</small>" : "") + "</td>" +
        "<td>" + statusHtml + "</td>" +
        '<td class="check-detail">' + esc(truncate(check.detail || "—", 220)) + "</td>" +
        '<td class="mono">' + esc(check.duration != null ? fmtDuration(check.duration) : "—") + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderVerifyLoops(raw) {
    var host = $("verify-loop");
    setText("verify-loop-count", "0 轮");
    if (!host) return;
    var rounds = collectLoopRounds(raw);
    if (!rounds.length) {
      host.innerHTML = '<div class="empty small">本次运行未触发自循环修复：各节点首轮验证即通过，或该运行尚未结束。</div>';
      return;
    }
    setText("verify-loop-count", rounds.length + " 轮");
    host.innerHTML = rounds.map(function (round) {
      var conf = normConf(round.confidence);
      var bad = conf != null && conf < 0.9;
      var failures = (round.failures || []).map(function (failure) {
        if (failure && typeof failure === "object") return String(failure.name || failure.check || failure.detail || "");
        return String(failure);
      }).filter(Boolean);
      var detail = [];
      if (failures.length) detail.push("失败项：<i>" + failures.map(esc).join("、") + "</i>");
      if (round.feedback) detail.push(esc(truncate(round.feedback, 240)));
      if (!detail.length) detail.push("本轮验证" + (round.passed === false ? "未通过，进入修复" : "通过"));
      var nodePrefix = round.node && round.node !== "repair" ? esc(round.node) + " · " : "";
      return '<div class="loop-round">' +
        '<span class="r-index">' + nodePrefix + "第 " + esc(round.iteration) + " 轮" +
        (round.max ? " / " + esc(round.max) : "") + "</span>" +
        '<span class="r-conf' + (bad ? " bad" : "") + '">置信度 ' + esc(fmtConf(round.confidence)) + "</span>" +
        '<div class="r-detail">' + detail.join("<br>") + "</div></div>";
    }).join("");
  }

  // 兼容两种自循环数据结构：
  // 1) /api/runs/{id}/loop_status 的 self_loop: {repair:{iterations,history:[{iteration,confidence,passed,failures,...}]}}
  // 2) 旧结构：直接给 history 数组或单轮对象
  function collectLoopRounds(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) {
      return normalizeLoopRounds(raw).map(function (round) {
        round.node = "repair";
        return round;
      });
    }
    if (typeof raw !== "object") return [];
    var loops = raw.self_loop && typeof raw.self_loop === "object" ? raw.self_loop : raw;
    var rounds = [];
    Object.keys(loops).forEach(function (nodeId) {
      var info = loops[nodeId];
      if (!info || typeof info !== "object") return;
      var history = info.history || info.rounds || info.rounds_history || [];
      if (!Array.isArray(history) || !history.length) return;
      var max = info.max_iterations || info.max || null;
      history.forEach(function (item, index) {
        if (!item || typeof item !== "object") return;
        var failures = item.failures || item.failed_checks || item.failed || [];
        if (!Array.isArray(failures)) {
          failures = typeof failures === "string" ? [failures] : Object.keys(failures || {});
        }
        var improvement = item.improvement;
        var feedback = item.feedback_preview || item.feedback || item.action || "";
        if (!feedback && improvement != null) feedback = "置信度提升 " + fmtNum(improvement, 2);
        rounds.push({
          node: nodeId,
          iteration: item.iteration != null ? item.iteration : index + 1,
          max: max,
          confidence: item.confidence != null ? item.confidence : null,
          passed: item.verify_passed != null ? item.verify_passed : (item.passed != null ? item.passed : null),
          failures: failures,
          feedback: feedback
        });
      });
    });
    return rounds;
  }

  function bindVerify() {
    var refresh = $("btn-verify-refresh");
    if (refresh) {
      refresh.addEventListener("click", function () {
        if (state.verifyRunId) loadVerification(state.verifyRunId);
        else enterVerify();
      });
    }
  }

  /* ------------------------------ Agent 团队 ------------------------------ */

  function computeAgentUsage() {
    var usage = {};
    (state.runs || []).forEach(function (run) {
      var raw = run.nodes || run.node_states || {};
      var list = Array.isArray(raw) ? raw : Object.keys(raw).map(function (key) {
        var info = raw[key];
        if (info && typeof info === "object" && !info.node_id) info.node_id = key;
        return info;
      });
      list.forEach(function (info) {
        if (!info || typeof info !== "object") return;
        // 验证/自循环节点由 verifier 引擎执行，NodeState 里没有 agent_id，这里按验证器归属统计
        var agentId = info.agent_id || (info.verify ? "verifier" : "");
        if (!agentId) return;
        var bucket = usage[agentId] || (usage[agentId] = { calls: 0, success: 0, failed: 0 });
        bucket.calls += 1;
        if (info.status === "succeeded") bucket.success += 1;
        else if (info.status === "failed") bucket.failed += 1;
      });
    });
    return usage;
  }

  var ROLE_NAMES = {
    planner: "规划者",
    researcher: "调研者",
    data_engineer: "数据工程师",
    statistician: "统计建模",
    verifier: "验证者",
    critic: "评审者",
    writer: "报告撰写",
    human: "人工确认"
  };

  function loadAgents() {
    var grid = $("agents-grid");
    return api("/api/agents").then(function (data) {
      var agents = Array.isArray(data) ? data : ((data && data.agents) || []);
      var load = (!Array.isArray(data) && data && data.load) || [];
      renderAgents(agents, load);
    }).catch(function (err) {
      toastError(err);
      if (grid) grid.innerHTML = emptyBlock("Agent 列表加载失败", "请确认后端服务正常后点击右上角刷新。", false);
    });
  }

  function renderAgents(agents, load) {
    var grid = $("agents-grid");
    var usage = computeAgentUsage();
    var loadMap = {};
    (load || []).forEach(function (item) {
      if (item && item.agent_id) loadMap[item.agent_id] = item;
    });
    setText("agents-count", agents.length ? agents.length + " 个 Agent" : "0 个 Agent");
    if (!grid) return;
    if (!agents.length) {
      grid.innerHTML = emptyBlock("尚未加载到 Agent", "请确认 config/agents.json 存在，并检查后端 /api/agents 是否正常返回。", false);
      return;
    }
    grid.innerHTML = agents.map(function (agent) {
      var id = agent.id || agent.name || "agent";
      var name = agent.name || id;
      var role = agent.role || "agent";
      var skills = Array.isArray(agent.skills) ? agent.skills : [];
      var tools = Array.isArray(agent.tools) ? agent.tools : [];
      var tags = skills.slice(0, 5).map(function (skill) {
        return '<span class="tag">' + esc(skill) + "</span>";
      }).join("");
      if (skills.length > 5) tags += '<span class="tag">+' + (skills.length - 5) + "</span>";
      tools.slice(0, 4).forEach(function (tool) {
        tags += '<span class="tag purple">' + esc(tool) + "</span>";
      });
      if (tools.length > 4) tags += '<span class="tag purple">+' + (tools.length - 4) + "</span>";
      var stat = usage[id] || null;
      var calls = stat ? stat.calls : null;
      var rate = stat && stat.calls ? stat.success / stat.calls : null;
      var loadInfo = loadMap[id] || {};
      var avatar = String(name).slice(0, 1);
      var enabled = agent.enabled === false;
      return '<article class="agent-card">' +
        '<div class="agent-head">' +
        '<div class="agent-avatar">' + esc(avatar) + "</div>" +
        '<div class="agent-title"><h3>' + esc(name) + "</h3><span>" + esc(id) +
        (loadInfo.inflight ? " · 执行中 " + loadInfo.inflight : "") + "</span></div>" +
        '<span class="pill agent-role">' + esc(ROLE_NAMES[role] || role) + "</span>" +
        "</div>" +
        '<p class="agent-desc">' + esc(agent.description || agent.system_prompt || "该 Agent 未填写描述。") + "</p>" +
        '<div class="agent-tags">' + (tags || '<span class="tag">通用能力</span>') + "</div>" +
        '<div class="agent-stats">' +
        '<div class="agent-stat"><span>温度</span><b>' + esc(agent.temperature != null ? fmtNum(agent.temperature, 2) : "—") + "</b></div>" +
        '<div class="agent-stat"><span>版本</span><b>' + esc(agent.version || "—") + "</b></div>" +
        '<div class="agent-stat"><span>调用</span><b>' + esc(calls == null ? "—" : fmtInt(calls)) + "</b></div>" +
        '<div class="agent-stat"><span>成功率</span><b class="' + (rate == null ? "" : (rate >= 0.8 ? "good" : "bad")) + '">' +
        esc(rate == null ? "—" : Math.round(rate * 100) + "%") + "</b></div>" +
        "</div>" +
        (enabled ? '<span class="badge">已停用</span>' : "") +
        "</article>";
    }).join("");
  }

  function enterAgents() {
    // 先拉运行记录，才能统计每个 Agent 的调用次数与成功率
    return loadRuns(50).then(function () {
      return loadAgents();
    });
  }

  function bindAgents() {
    var refresh = $("btn-agents-refresh");
    if (refresh) refresh.addEventListener("click", function () { loadAgents(); });
  }

  /* ------------------------------ 工具与 MCP ------------------------------ */

  function loadToolsPage() {
    var toolsHost = $("tools-list");
    if (toolsHost && !toolsHost.children.length) {
      toolsHost.innerHTML = '<div class="empty small">正在加载工具列表…</div>';
    }
    return api("/api/tools").then(function (data) {
      var tools = Array.isArray(data) ? data : ((data && data.tools) || []);
      var servers = (!Array.isArray(data) && (data.mcp_servers || data.servers)) || [];
      state.tools = tools;
      state.mcpServers = servers;
      renderTools(tools);
      renderMcp(servers);
    }).catch(function (err) {
      toastError(err);
      if (toolsHost) toolsHost.innerHTML = emptyBlock("工具列表加载失败", "请确认后端服务正常后点击刷新。", true);
    });
  }

  function renderTools(tools) {
    var host = $("tools-list");
    setText("tools-count", tools.length + " 个工具");
    if (!host) return;
    if (!tools.length) {
      host.innerHTML = emptyBlock("暂无可用工具", "后端尚未注册工具，或 tools 注册表为空。", true);
      return;
    }
    host.innerHTML = tools.map(function (tool) {
      var name = tool.name || "tool";
      var source = tool.source || "builtin";
      var risk = tool.risk_level || tool.risk || "";
      var schema = tool.parameters || tool.schema || {};
      var description = tool.description || "该工具暂无描述。";
      return '<details class="tool-item">' +
        "<summary><span class=\"tool-name\">" + esc(name) + "</span>" +
        '<span class="badge">' + esc(source) + "</span>" +
        (risk ? '<span class="badge">风险 ' + esc(risk) + "</span>" : "") +
        '<span class="muted">' + esc(truncate(description, 46)) + "</span></summary>" +
        '<p class="tool-desc">' + esc(description) + "</p>" +
        "<pre>" + esc(truncate(JSON.stringify(schema, null, 2), 3000)) + "</pre></details>";
    }).join("");
  }

  function renderMcp(servers) {
    var host = $("mcp-list");
    setText("mcp-count", (servers.length || 0) + " 个服务器");
    if (!host) return;
    if (!servers.length) {
      host.innerHTML = emptyBlock("未配置 MCP 服务器", "在 config/platform.json 的 mcp_servers 中添加 stdio 或 http 服务器后重启生效。", true);
      return;
    }
    host.innerHTML = servers.map(function (server) {
      if (typeof server === "string") server = { name: server };
      var name = server.name || server.id || "mcp";
      var status = String(server.status || "").toLowerCase();
      if (!status) status = server.connected === false ? "error" : (server.enabled === false ? "disabled" : "connected");
      var cls = ["connected", "ok", "ready", "running"].indexOf(status) >= 0 ? "connected"
        : (["error", "failed", "offline", "disconnected"].indexOf(status) >= 0 ? "error" : "disabled");
      var label = cls === "connected" ? "已连接" : (cls === "error" ? "连接异常" : "未启用");
      var toolCount = server.tool_count != null ? server.tool_count : (Array.isArray(server.tools) ? server.tools.length : null);
      var detail = label + (toolCount != null ? " · " + toolCount + " 个工具" : "");
      var url = server.url || server.command || server.transport || "";
      if (Array.isArray(url)) url = url.join(" ");
      return '<div class="mcp-item">' +
        '<span class="mcp-status ' + cls + '"></span>' +
        "<b>" + esc(name) + "</b>" +
        '<span class="muted">' + esc(detail) + "</span>" +
        (url ? '<div class="mcp-url">' + esc(truncate(String(url), 160)) + "</div>" : "") +
        "</div>";
    }).join("");
  }

  function enterTools() {
    return loadToolsPage();
  }

  function bindTools() {
    var refresh = $("btn-tools-refresh");
    if (refresh) refresh.addEventListener("click", function () { loadToolsPage(); });
  }

  /* ------------------------------ 模型设置 ------------------------------ */

  function enterSettings() {
    return Promise.all([loadHealth(), loadLlm()]).then(function () {});
  }

  function loadLlm() {
    return api("/api/settings/llm").then(function (data) {
      var llm = (data && (data.llm || data.config)) || data || {};
      state.llm = llm;
      applyLlmToForm(llm);
      renderLlmCurrent(llm);
      renderLlmLatency(llm);
    }).catch(function (err) {
      toastError(err);
    });
  }

  var LLM_PRESETS = {
    zhipu: { base: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4-flash" },
    deepseek: { base: "https://api.deepseek.com/v1", model: "deepseek-chat" },
    dashscope: { base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
    siliconflow: { base: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen2.5-7B-Instruct" },
    moonshot: { base: "https://api.moonshot.cn/v1", model: "moonshot-v1-8k" },
    openai: { base: "https://api.openai.com/v1", model: "gpt-4o-mini" }
  };

  function detectPreset(baseUrl) {
    var url = String(baseUrl || "").replace(/\/+$/, "");
    for (var key in LLM_PRESETS) {
      if (LLM_PRESETS[key].base.replace(/\/+$/, "") === url) return key;
    }
    return "custom";
  }

  function applyLlmToForm(llm) {
    var provider = $("llm-provider");
    if (provider) {
      var value = llm.provider || "mock";
      var exists = false;
      for (var i = 0; i < provider.options.length; i += 1) {
        if (provider.options[i].value === value) exists = true;
      }
      if (!exists) {
        var option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        provider.appendChild(option);
      }
      provider.value = value;
    }
    var base = $("llm-base-url");
    if (base) base.value = llm.base_url || "";
    var model = $("llm-model");
    if (model) model.value = llm.model || "";
    var preset = $("llm-preset");
    if (preset) preset.value = detectPreset(llm.base_url);
    var key = $("llm-api-key");
    if (key) key.value = "";
    var remote = (llm.provider || "mock") !== "mock";
    setText("llm-save-hint", llm.api_key_set
      ? "本次会话已配置 Key（仅内存，服务重启后需重填）"
      : remote
        ? "在线模型尚未配置 Key，当前已临时降级为 mock；填入 Key 后保存即生效。"
        : "尚未配置 API Key：mock 模式无需 Key，切在线模型前请填写。");
  }

  function renderLlmCurrent(llm) {
    var host = $("llm-current");
    if (!host) return;
    var router = llm.router || {};
    var usage = llm.usage || {};
    var calls = usage.calls != null ? usage.calls : llm.calls;
    var avg = usage.avg_latency_s != null ? usage.avg_latency_s : llm.avg_latency_s;
    var rows = [
      kvRow("Provider", llm.provider || "mock"),
      kvRow("Base URL", llm.base_url || "—"),
      kvRow("Model", llm.model || "—"),
      kvRow("API Key", llm.api_key_set ? "已配置（仅本次会话，不落盘）" : "未配置"),
      kvRow("路由", (router.planner || "smart") + " / " + (router.default || "fast"))
    ];
    if (llm.timeout_s != null) rows.push(kvRow("超时", llm.timeout_s + " 秒"));
    if (calls != null) rows.push(kvRow("调用次数", calls + " 次" + (llm.errors ? " · 失败 " + llm.errors : "")));
    if (avg != null) rows.push(kvRow("平均延迟", fmtDurationS(avg)));
    if (usage.total_tokens != null) rows.push(kvRow("累计 Tokens", fmtInt(usage.total_tokens)));
    host.innerHTML = rows.join("");
  }

  function renderLlmLatency(llm) {
    var latency = null;
    var calls = null;
    if (llm) {
      if (llm.last_latency_s != null) latency = llm.last_latency_s;
      else if (llm.avg_latency_s != null) latency = llm.avg_latency_s;
      else if (llm.last_latency_ms != null) latency = llm.last_latency_ms / 1000;
      calls = (llm.usage && llm.usage.calls) || llm.calls || null;
    }
    if (latency == null && state.health && state.health.llm) {
      var healthLlm = state.health.llm;
      if (healthLlm.last_latency_s != null) latency = healthLlm.last_latency_s;
      else if (healthLlm.last_latency_ms != null) latency = healthLlm.last_latency_ms / 1000;
    }
    setText("llm-latency", latency == null
      ? "延迟 –"
      : "平均延迟 " + fmtDurationS(latency) + (calls ? " · " + calls + " 次" : ""));
  }

  function saveLlm(event) {
    if (event) event.preventDefault();
    var providerEl = $("llm-provider");
    var baseEl = $("llm-base-url");
    var modelEl = $("llm-model");
    var keyEl = $("llm-api-key");
    var payload = {
      provider: providerEl ? providerEl.value : "mock",
      base_url: baseEl ? String(baseEl.value || "").trim() : "",
      model: modelEl ? String(modelEl.value || "").trim() : ""
    };
    if (keyEl && String(keyEl.value || "").trim()) payload.api_key = String(keyEl.value).trim();
    var button = $("btn-llm-save");
    if (button) button.disabled = true;
    setText("llm-save-hint", "正在保存…");
    var started = Date.now();
    return api("/api/settings/llm", { method: "POST", body: payload }).then(function (data) {
      var elapsed = Date.now() - started;
      if (keyEl) keyEl.value = "";
      var llm = (data && (data.llm || data.config)) || payload;
      state.llm = llm;
      applyLlmToForm(llm);
      renderLlmCurrent(llm);
      renderLlmLatency(llm);
      setText("llm-save-hint", "已保存 · 本次请求 " + elapsed + " ms · Key 已清空不回显");
      toast("模型配置已更新：" + (payload.provider || "mock"), "success");
      loadHealth();
    }).catch(function (err) {
      setText("llm-save-hint", "保存失败，请检查配置后重试。");
      toastError(err);
    }).then(function () {
      if (button) button.disabled = false;
    });
  }

  function bindSettings() {
    var form = $("llm-form");
    if (form) form.addEventListener("submit", saveLlm);
    var presetEl = $("llm-preset");
    if (presetEl) {
      presetEl.addEventListener("change", function () {
        var preset = LLM_PRESETS[presetEl.value];
        if (!preset) return;
        var providerEl = $("llm-provider");
        var baseEl = $("llm-base-url");
        var modelEl = $("llm-model");
        if (providerEl) providerEl.value = "openai_compat";
        if (baseEl) baseEl.value = preset.base;
        if (modelEl) modelEl.value = preset.model;
        setText("llm-save-hint", "已填入 " + preset.base + "，请填写 Key 后保存。");
      });
    }
    var refresh = $("btn-llm-refresh");
    if (refresh) refresh.addEventListener("click", function () { loadHealth(); loadLlm(); });
  }

  /* ------------------------------ 全局绑定与启动 ------------------------------ */

  function bindDagControls() {
    var zoomIn = $("dag-zoom-in");
    if (zoomIn) {
      zoomIn.addEventListener("click", function () {
        if (state.dag) state.dag.zoomBy(1.2);
      });
    }
    var zoomOut = $("dag-zoom-out");
    if (zoomOut) {
      zoomOut.addEventListener("click", function () {
        if (state.dag) state.dag.zoomBy(1 / 1.2);
      });
    }
    var fit = $("dag-fit");
    if (fit) {
      fit.addEventListener("click", function () {
        if (state.dag) state.dag.fit();
      });
    }
    var drawerClose = $("drawer-close");
    if (drawerClose) {
      drawerClose.addEventListener("click", function () {
        if (state.dag) state.dag.select(null, true);
        else showNodeDrawer(null);
      });
    }
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      if (state.page !== "orchestrator") return;
      if (state.dag && state.dag.selectedId) state.dag.select(null, true);
    });
    var select = $("wf-select");
    if (select) {
      select.addEventListener("change", function () {
        loadWorkflow(select.value);
      });
    }
  }

  function bindGlobal() {
    qsa(".nav-item").forEach(function (item) {
      item.addEventListener("click", function () {
        go(item.getAttribute("data-page"));
      });
    });
    var demo = $("btn-demo");
    if (demo) demo.addEventListener("click", function () { startDemo(); });
    bindDagControls();
    bindMonitor();
    bindVerify();
    bindAgents();
    bindTools();
    bindSettings();
    window.addEventListener("hashchange", route);
  }

  PAGE_ENTERS.dashboard = function (query) {
    return Promise.all([enterOverview(query), enterWorkbench(query)]);
  };
  PAGE_ENTERS.flow = enterOrchestrator;
  PAGE_ENTERS.runtime = function (query) {
    return Promise.all([enterMonitor(query), enterVerify(query)]);
  };
  PAGE_ENTERS.resources = function (query) {
    return Promise.all([enterAgents(query), enterTools(query)]);
  };
  PAGE_ENTERS.settings = enterSettings;

  function enterWorkbench(query) {
    if (window.Workbench && typeof window.Workbench.activate === "function") {
      return Promise.resolve(window.Workbench.activate(query || {}));
    }
    return Promise.resolve();
  }

  function init() {
    bindGlobal();
    if (!location.hash) history.replaceState(null, "", "#/workbench");
    route();
    // 顶栏状态与事件计数在任意页签都需要，先拉一次（失败由各函数自行 toast）
    loadHealth();
    loadOverview(true);
    setInterval(function () {
      if (document.hidden) return;
      loadHealth();
      if (state.page === "overview") loadOverview(true);
    }, 20000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
