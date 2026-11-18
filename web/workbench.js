/* 分析工作台：场景目录 → 一键运行 → 结论与证据 */
(function () {
  "use strict";

  var state = { scenarios: [], filter: "all", pollTimer: null, runId: "" };
  var $ = function (id) { return document.getElementById(id); };
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  /* ------------------------------ 基础工具 ------------------------------ */

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function getJSON(path) {
    return fetch(path, { headers: { "Accept": "application/json" } }).then(function (res) {
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      return res.json();
    });
  }

  function postJSON(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (res) {
      return res.json().then(function (data) {
        if (!res.ok) {
          var msg = (data && data.error && data.error.message) || ("HTTP " + res.status);
          throw new Error(msg);
        }
        return data;
      });
    });
  }

  function fmtTime(ts) {
    if (!ts) { return "—"; }
    var d = new Date(ts * 1000);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return (d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function nowLabel() {
    var d = new Date();
    return d.getMonth() + 1 + "月" + d.getDate() + "日 " + d.getHours() + ":" + (d.getMinutes() < 10 ? "0" : "") + d.getMinutes();
  }

  function statusText(status) {
    return ({ succeeded: "成功", failed: "失败", cancelled: "已取消", running: "运行中", queued: "排队中", paused: "等待确认" })[status] || status || "未知";
  }

  function statusClass(status) {
    if (status === "succeeded") { return "ok"; }
    if (status === "failed" || status === "cancelled") { return "bad"; }
    return "run";
  }

  /* ------------------------------ 场景目录 ------------------------------ */

  function loadScenarios() {
    return getJSON("/api/scenarios").then(function (data) {
      state.scenarios = data.scenarios || [];
      renderFilters();
      renderScenarios();
    }).catch(function (err) {
      $("wb-grid").innerHTML = '<div class="wb-empty">场景加载失败：' + esc(err.message) + '</div>';
    });
  }

  function renderFilters() {
    var defs = [
      ["all", "全部"],
      ["single", "单目标"],
      ["multi", "多目标联合"],
      ["demo", "内置演示"]
    ];
    $("wb-filters").innerHTML = defs.map(function (item) {
      return '<button class="wb-chip' + (state.filter === item[0] ? " on" : "") + '" data-filter="' + item[0] + '">' + item[1] + "</button>";
    }).join("");
    Array.prototype.forEach.call($("wb-filters").querySelectorAll(".wb-chip"), function (btn) {
      btn.addEventListener("click", function () {
        state.filter = btn.getAttribute("data-filter");
        renderFilters();
        renderScenarios();
      });
    });
  }

  function visibleScenarios() {
    return state.scenarios.filter(function (s) {
      if (state.filter === "demo") { return !!s.demo; }
      if (state.filter === "single") { return !s.demo && s.kind !== "multi"; }
      if (state.filter === "multi") { return s.kind === "multi"; }
      return true;
    });
  }

  function renderScenarios() {
    var items = visibleScenarios();
    if (!items.length) {
      $("wb-grid").innerHTML = '<div class="wb-empty">没有符合条件的场景</div>';
      return;
    }
    $("wb-grid").innerHTML = items.map(function (s, index) {
      var targets = (s.targets || []).map(function (t) { return '<span class="wb-tag">' + esc(t) + "</span>"; }).join("");
      var badge = s.kind === "multi"
        ? '<span class="wb-badge multi">' + s.target_count + " 个目标</span>"
        : '<span class="wb-badge">单目标</span>';
      return '' +
        '<article class="wb-card" data-index="' + state.scenarios.indexOf(s) + '">' +
          '<div class="wb-card-top"><h3>' + esc(s.name) + "</h3>" + badge + "</div>" +
          '<p>' + esc(s.description || s.title || "") + "</p>" +
          '<div class="wb-targets">' + targets + "</div>" +
          '<div class="wb-meta"><span>数据 <b>' + (s.rows || "—") + " 行</b></span>" +
            "<span>流程 <b>" + (s.nodes || "—") + " 节点</b></span>" +
            (s.domain ? "<span><b>" + esc(s.domain) + "</b></span>" : "") + "</div>" +
          '<div class="wb-card-foot"><span class="wb-subtle">' + (s.demo ? "内置演示" : "行业场景") + "</span>" +
            '<button class="wb-primary" data-run="' + state.scenarios.indexOf(s) + '">开始分析 →</button></div>' +
        "</article>";
    }).join("");
    Array.prototype.forEach.call($("wb-grid").querySelectorAll("[data-run]"), function (btn) {
      btn.addEventListener("click", function () {
        var index = parseInt(btn.getAttribute("data-run"), 10);
        openScenario(state.scenarios[index]);
      });
    });
  }

  /* ------------------------------ 弹层 ------------------------------ */

  function openModal(html) {
    $("wb-modal-body").innerHTML = html;
    $("wb-modal").hidden = false;
  }

  function closeModal() {
    $("wb-modal").hidden = true;
    $("wb-modal-body").innerHTML = "";
    if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
    state.runId = "";
  }

  function bindClose() {
    $("wb-modal-close").addEventListener("click", closeModal);
    $("wb-modal").addEventListener("click", function (event) {
      if (event.target === $("wb-modal")) { closeModal(); }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") { closeModal(); }
    });
  }

  var NODE_LABELS = {
    start: "启动", plan: "任务规划", scan: "数据扫描", research: "方法调研",
    clean: "数据工程", segment: "分层画像", repair: "修复循环", analyze: "统计建模",
    verify: "五层验证", robustness: "稳健性检验", sensitivity: "敏感性分析",
    joint: "联合对比", critique: "同行评审", gate: "人工闸门", report: "报告撰写", end: "结束"
  };

  function openScenario(s) {
    var targets = (s.targets || []).join("、");
    openModal('' +
      '<div class="wb-m-head"><h3>' + esc(s.name) + "</h3><p>" + esc(s.description || s.title || "") + "</p></div>" +
      '<div class="wb-m-grid">' +
        '<div class="wb-kv"><span>数据规模</span><b>' + (s.rows || "—") + " 行</b></div>" +
        '<div class="wb-kv"><span>分析目标</span><b class="cyan">' + esc(targets || "—") + "</b></div>" +
        '<div class="wb-kv"><span>工作流</span><b>' + (s.nodes || "—") + " 节点</b></div>" +
        '<div class="wb-kv"><span>验证方式</span><b>五层 + 自循环</b></div>' +
      "</div>" +
      '<p class="wb-hint">运行后，平台会自动完成规划、并行分析、五层验证；若验证不通过，会带着结构化反馈重跑，最多 3 轮。' +
      "所有结论都带验证证据，可在「验证中心」逐条查看。</p>" +
      '<div class="wb-m-foot">' +
        '<button class="wb-link-btn" data-close>取消</button>' +
        '<button class="wb-primary" id="btn-run">开始运行</button>' +
      "</div>");
    $("wb-modal-body").querySelector("[data-close]").addEventListener("click", closeModal);
    $("btn-run").addEventListener("click", function () { startScenario(s); });
  }

  function runningHtml(s) {
    return '' +
      '<div class="wb-m-head"><h3>正在分析：' + esc(s.name) + "</h3>" +
      '<p id="wb-run-stage">正在创建任务…</p></div>' +
      '<div class="wb-progress"><div class="wb-bar"><i id="wb-bar"></i></div>' +
      '<div class="wb-progress-line"><span id="wb-progress-text">0 / 0 节点</span><span id="wb-progress-time">0.0s</span></div>' +
      '<div class="wb-progress-line"><span id="wb-node-now">等待调度</span><span id="wb-token-text">Token —</span></div></div>' +
      '<div class="wb-hint">任务在后台执行，关闭窗口不会中断。完成后可在「最近任务」里查看结果。</div>';
  }

  function startScenario(s) {
    openModal(runningHtml(s));
    var label = (s.name || "分析任务") + " · " + nowLabel();
    var body = { workflow_id: s.workflow_id, label: label, auto_approve: true, policy: "adaptive" };
    if (!s.demo) { body.scenario = s.id; }
    postJSON("/api/runs", body).then(function (data) {
      var runId = data.run_id || data.id || (data.run && data.run.run_id);
      if (!runId) { throw new Error("服务未返回运行 ID"); }
      state.runId = runId;
      pollRun(runId, s);
    }).catch(function (err) {
      $("wb-run-stage").textContent = "启动失败：" + err.message;
      $("wb-run-stage").style.color = "#ff6b6b";
    });
  }

  function pollRun(runId, scenario) {
    var startedAt = Date.now();
    function tick() {
      getJSON("/api/runs/" + encodeURIComponent(runId)).then(function (run) {
        if (state.runId !== runId) { return; }
        var nodes = run.nodes || {};
        var ids = Object.keys(nodes);
        var done = 0;
        var active = "";
        var tokens = 0;
        ids.forEach(function (id) {
          var node = nodes[id] || {};
          var st = node.status;
          if (st === "succeeded" || st === "failed" || st === "skipped") { done += 1; }
          if (st === "running" && !active) { active = NODE_LABELS[id] || id; }
          tokens += (node.tokens_in || 0) + (node.tokens_out || 0);
        });
        var total = ids.length || 1;
        var pct = Math.round((done / total) * 100);
        var elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
        if ($("wb-bar")) {
          $("wb-bar").style.width = pct + "%";
          $("wb-progress-text").textContent = done + " / " + ids.length + " 节点";
          $("wb-progress-time").textContent = elapsed + "s";
          $("wb-node-now").textContent = active ? "正在执行：" + active : (run.status === "running" ? "调度中…" : "收尾");
          $("wb-token-text").textContent = "Token " + (tokens || "—");
        }
        if (["succeeded", "failed", "cancelled"].indexOf(run.status) >= 0) {
          showResult(run, scenario);
          return;
        }
        state.pollTimer = setTimeout(tick, 600);
      }).catch(function () {
        state.pollTimer = setTimeout(tick, 1200);
      });
    }
    tick();
  }

  function showResult(run, scenario) {
    var runId = run.run_id;
    var metrics = run.metrics || {};
    var ok = run.status === "succeeded";
    Promise.all([
      getJSON("/api/runs/" + encodeURIComponent(runId) + "/verification").catch(function () { return {}; }),
      getJSON("/api/runs/" + encodeURIComponent(runId) + "/report").catch(function () { return {}; })
    ]).then(function (results) {
      var verification = results[0] || {};
      var reportPayload = results[1] || {};
      var reportData = reportPayload.report || reportPayload;
      var rounds = verification.verification || [];
      if (!rounds.length) {
        rounds = (run.verification || []).map(function (item) { return item; });
      }
      var conf = metrics.avg_confidence != null
        ? metrics.avg_confidence
        : (verification.confidence != null ? verification.confidence : 0);
      var markdown = reportData.markdown || reportData.report || "";
      var excerptSource = markdown;
      var anchors = ["\n## 摘要", "\n## 主要发现", "\n## 结论", "\n## 一、"];
      var at = -1;
      for (var ai = 0; ai < anchors.length && at < 0; ai += 1) {
        at = markdown.indexOf(anchors[ai]);
      }
      if (at < 0) { at = markdown.indexOf("\n## "); }
      if (at > 0) { excerptSource = markdown.slice(at + 1); }
      var excerpt = excerptSource ? excerptSource.slice(0, 1600) : "报告尚未生成。";
      var roundsHtml = rounds.map(function (item, index) {
        var passed = !!item.passed;
        return '<div class="wb-round' + (passed ? " pass" : "") + '">' +
          '<span class="dot"></span><span class="r-num">第 ' + (index + 1) + " 轮</span>" +
          '<span>' + (passed ? "验证通过" : "未通过 · 已触发定向修复") + "</span>" +
          '<span class="r-conf">' + Math.round((item.confidence || 0) * 100) + "%</span></div>";
      }).join("") || '<div class="wb-hint">本次运行没有验证记录。</div>';

      openModal('' +
        '<div class="wb-m-head"><h3>' + esc(scenario ? scenario.name : run.workflow_name || "分析结果") + "</h3>" +
          '<span class="wb-result-badge ' + (ok ? "ok" : "bad") + '">' + (ok ? "✓ 分析完成 · 已验证" : "✕ 运行未完成") + "</span>" +
          '<p style="margin-top:10px">' + (ok ? "结论已通过五层验证；下方是本次分析的量化结果与证据摘要。" : "运行失败或超时，可在「验证中心」查看具体节点与错误。") + "</p></div>" +
        '<div class="wb-m-grid">' +
          '<div class="wb-kv"><span>最终置信度</span><b class="cyan">' + Math.round(conf * 100) + "%</b></div>" +
          '<div class="wb-kv"><span>耗时</span><b>' + (metrics.duration_s != null ? metrics.duration_s + "s" : "—") + "</b></div>" +
          '<div class="wb-kv"><span>Token</span><b>' + (metrics.tokens_total || "—") + "</b></div>" +
          '<div class="wb-kv"><span>验证轮数</span><b class="orange">' + (metrics.verification_count != null ? metrics.verification_count : rounds.length) + "</b></div>" +
          '<div class="wb-kv"><span>自循环</span><b>' + (metrics.self_loop_iterations || 0) + " 轮</b></div>" +
        "</div>" +
        '<h4 style="margin:6px 0 0;font-size:14px">验证过程</h4>' +
        '<div class="wb-rounds">' + roundsHtml + "</div>" +
        '<h4 style="margin:16px 0 6px;font-size:14px">报告摘录</h4>' +
        '<div class="wb-report">' + esc(excerpt) + "</div>" +
        '<div class="wb-m-foot">' +
          '<button class="wb-link-btn" data-again>再做一次</button>' +
          '<a class="wb-link-btn" href="/api/runs/' + encodeURIComponent(runId) + '/artifacts/report.md" target="_blank" rel="noopener">下载报告</a>' +
          '<a class="wb-link-btn" href="#/verify?run=' + encodeURIComponent(runId) + '">查看完整证据 →</a>' +
          '<button class="wb-primary" data-close>完成</button>' +
        "</div>");
      var again = $("wb-modal-body").querySelector("[data-again]");
      if (again) {
        again.addEventListener("click", function () {
          if (scenario) { openScenario(scenario); } else { closeModal(); }
        });
      }
      $("wb-modal-body").querySelector("[data-close]").addEventListener("click", closeModal);
      loadRecent();
    });
  }

  /* ------------------------------ 最近任务 ------------------------------ */

  function loadRecent() {
    return getJSON("/api/runs?limit=8").then(function (data) {
      var runs = data.runs || [];
      if (!runs.length) {
        $("wb-recent").innerHTML = '<div class="wb-empty">还没有运行记录，先选一个场景开始吧</div>';
        return;
      }
      $("wb-recent").innerHTML = runs.map(function (run) {
        return '<div class="wb-row" data-run-id="' + esc(run.run_id) + '">' +
          '<div class="wb-row-main"><div class="wb-row-name">' + esc(run.label || run.workflow_name || run.run_id) + "</div>" +
            '<div class="wb-row-sub">' + esc(run.workflow_name || run.workflow_id) + " · " + fmtTime(run.created_at) + "</div></div>" +
          '<span class="wb-status ' + statusClass(run.status) + '">' + statusText(run.status) + "</span></div>";
      }).join("");
      Array.prototype.forEach.call($("wb-recent").querySelectorAll("[data-run-id]"), function (row) {
        row.addEventListener("click", function () {
          openRun(row.getAttribute("data-run-id"));
        });
      });
    }).catch(function () {
      $("wb-recent").innerHTML = '<div class="wb-empty">运行记录加载失败</div>';
    });
  }

  function openRun(runId) {
    getJSON("/api/runs/" + encodeURIComponent(runId)).then(function (run) {
      if (["succeeded", "failed", "cancelled"].indexOf(run.status) >= 0) {
        state.runId = runId;
        showResult(run, null);
      } else {
        openModal(runningHtml({ name: run.workflow_name || runId }));
        state.runId = runId;
        pollRun(runId, null);
      }
    }).catch(function (err) {
      openModal('<div class="wb-m-head"><h3>无法打开任务</h3><p>' + esc(err.message) + "</p></div>");
    });
  }

  /* ------------------------------ 激活入口 ------------------------------ */

  var activated = false;

  function activate() {
    if (!activated) {
      activated = true;
      var modal = $("wb-modal");
      if (modal && modal.parentNode !== document.body) { document.body.appendChild(modal); }
      bindClose();
      var shared = new URLSearchParams(window.location.search).get("run");
      if (shared) {
        state.runId = shared;
        openRun(shared);
      }
      setInterval(function () {
        if (!state.runId) { loadRecent(); }
      }, 10000);
    }
    loadScenarios();
    loadRecent();
  }

  window.Workbench = { activate: activate, refresh: loadScenarios };
})();
