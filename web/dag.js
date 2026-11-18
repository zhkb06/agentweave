/* ==========================================================================
   织流 AgentWeave · 编排台 DAG 渲染器（原生 SVG，无第三方库）
   - 拓扑分层自动布局：x = layer*220 + 40，y = 层内序号*90 + 40
   - 三次贝塞尔连线、滚轮缩放、拖拽平移、适应画布、节点选中抽屉回调
   ========================================================================== */
(function (global) {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var NODE_W = 120;
  var NODE_H = 56;
  var LAYER_GAP = 220;
  var ROW_GAP = 90;
  var PAD_X = 40;
  var PAD_Y = 40;

  var TYPE_COLORS = {
    agent: "#38bdf8",
    tool: "#22d3ee",
    condition: "#a78bfa",
    loop: "#f59e0b",
    verify: "#34d399",
    human_gate: "#fbbf24",
    human: "#fbbf24",
    start: "#64748b",
    end: "#64748b",
    join: "#94a3b8",
    subworkflow: "#818cf8"
  };

  var TYPE_LABELS = {
    agent: "智能体",
    tool: "工具",
    condition: "条件",
    loop: "循环",
    verify: "验证",
    human_gate: "人工门禁",
    human: "人工门禁",
    start: "开始",
    end: "结束",
    join: "汇聚",
    subworkflow: "子流程"
  };

  function typeColor(type) {
    return TYPE_COLORS[type] || "#8fa3c8";
  }

  function typeLabel(type) {
    return TYPE_LABELS[type] || type || "节点";
  }

  function svgEl(name, attrs) {
    var el = document.createElementNS(SVG_NS, name);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (attrs[key] !== undefined && attrs[key] !== null) {
          el.setAttribute(key, String(attrs[key]));
        }
      });
    }
    return el;
  }

  function truncate(text, max) {
    text = String(text == null ? "" : text);
    if (text.length <= max) return text;
    return text.slice(0, max - 1) + "…";
  }

  /** 计算拓扑层级：先 DFS 去掉回边（自循环/修复环），再最长路径分层。 */
  function computeLayers(nodes, edges) {
    var ids = nodes.map(function (n) { return n.id; });
    var idSet = {};
    ids.forEach(function (id) { idSet[id] = true; });

    var adj = {};
    var radj = {};
    ids.forEach(function (id) { adj[id] = []; radj[id] = []; });

    var seen = {};
    var kept = [];
    edges.forEach(function (edge) {
      var s = edge.source || edge.from;
      var t = edge.target || edge.to;
      if (!idSet[s] || !idSet[t]) return;
      var key = s + "\u0000" + t;
      if (seen[key]) return;
      seen[key] = true;
      adj[s].push(t);
      radj[t].push(s);
      kept.push({ source: s, target: t, edge: edge });
    });

    // DFS 去回边
    var color = {};
    ids.forEach(function (id) { color[id] = 0; });
    var forward = {};
    var back = {};
    function dfs(startId) {
      var stack = [{ id: startId, index: 0 }];
      color[startId] = 1;
      while (stack.length) {
        var top = stack[stack.length - 1];
        var neighbors = adj[top.id];
        if (top.index >= neighbors.length) {
          color[top.id] = 2;
          stack.pop();
          continue;
        }
        var next = neighbors[top.index];
        top.index += 1;
        if (color[next] === 0) {
          forward[top.id + "\u0000" + next] = true;
          color[next] = 1;
          stack.push({ id: next, index: 0 });
        } else if (color[next] === 1) {
          back[top.id + "\u0000" + next] = true; // 回边
        } else {
          forward[top.id + "\u0000" + next] = true;
        }
      }
    }
    ids.forEach(function (id) {
      if (color[id] === 0) dfs(id);
    });

    var preds = {};
    ids.forEach(function (id) { preds[id] = []; });
    kept.forEach(function (item) {
      var key = item.source + "\u0000" + item.target;
      if (forward[key]) preds[item.target].push(item.source);
    });

    // 最长路径分层（前驱图层级 +1），未连通节点为 0
    var layer = {};
    ids.forEach(function (id) { layer[id] = 0; });
    var changed = true;
    var guard = 0;
    while (changed && guard < ids.length + 5) {
      changed = false;
      guard += 1;
      ids.forEach(function (id) {
        preds[id].forEach(function (p) {
          var candidate = layer[p] + 1;
          if (candidate > layer[id] && candidate < ids.length + 2) {
            layer[id] = candidate;
            changed = true;
          }
        });
      });
    }

    var byLayer = {};
    ids.forEach(function (id) {
      var lv = layer[id];
      if (!byLayer[lv]) byLayer[lv] = [];
      byLayer[lv].push(id);
    });
    // 层内按入度/名称排序，保证同一工作流每次布局稳定
    Object.keys(byLayer).forEach(function (lv) {
      byLayer[lv].sort(function (a, b) {
        var da = (radj[a] || []).length;
        var db = (radj[b] || []).length;
        if (da !== db) return da - db;
        return a < b ? -1 : a > b ? 1 : 0;
      });
    });

    var positions = {};
    Object.keys(byLayer)
      .map(Number)
      .sort(function (a, b) { return a - b; })
      .forEach(function (lv) {
        byLayer[lv].forEach(function (id, index) {
          positions[id] = { x: PAD_X + lv * LAYER_GAP, y: PAD_Y + index * ROW_GAP, layer: lv };
        });
      });

    return { positions: positions, forward: forward, kept: kept, back: back };
  }

  function DAGView(svg, options) {
    this.svg = svg;
    this.viewport = svg.querySelector("#dag-viewport") || svg.appendChild(svgEl("g", { id: "dag-viewport" }));
    this.options = options || {};
    this.nodes = [];
    this.edges = [];
    this.positions = {};
    this.nodeEls = {};
    this.edgeEls = [];
    this.edgeMeta = [];
    this.statusMap = {};
    this.view = { x: 0, y: 0, k: 1 };
    this.selectedId = null;
    this._drag = null;
    this._dragged = false;
    this._prepareDefs();
    this._bindEvents();
  }

  DAGView.TYPE_COLORS = TYPE_COLORS;
  DAGView.TYPE_LABELS = TYPE_LABELS;
  DAGView.typeColor = typeColor;
  DAGView.typeLabel = typeLabel;

  DAGView.prototype._prepareDefs = function () {
    var defs = svgEl("defs");
    var marker = svgEl("marker", {
      id: "dag-arrow",
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 7,
      markerHeight: 7,
      orient: "auto-start-reverse"
    });
    marker.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "#33415f" }));
    defs.appendChild(marker);
    var activeMarker = svgEl("marker", {
      id: "dag-arrow-active",
      viewBox: "0 0 10 10",
      refX: 9,
      refY: 5,
      markerWidth: 7,
      markerHeight: 7,
      orient: "auto-start-reverse"
    });
    activeMarker.appendChild(svgEl("path", { d: "M0,0 L10,5 L0,10 z", fill: "#38bdf8" }));
    defs.appendChild(activeMarker);
    this.svg.appendChild(defs);
    this.defs = defs;
    // 保证 viewport 在 defs 之后，避免被覆盖
    if (this.viewport.parentNode !== this.svg) this.svg.appendChild(this.viewport);
  };

  DAGView.prototype._bindEvents = function () {
    var self = this;
    var svg = this.svg;

    svg.addEventListener("wheel", function (event) {
      event.preventDefault();
      var rect = svg.getBoundingClientRect();
      var cx = event.clientX - rect.left;
      var cy = event.clientY - rect.top;
      var factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
      self.zoomTo(self.view.k * factor, cx, cy);
    }, { passive: false });

    svg.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      var nodeEl = event.target.closest ? event.target.closest(".dag-node") : null;
      self._drag = {
        startX: event.clientX,
        startY: event.clientY,
        originX: self.view.x,
        originY: self.view.y,
        nodeId: nodeEl ? nodeEl.getAttribute("data-id") : null
      };
      self._dragged = false;
      svg.classList.add("is-dragging");
      if (svg.setPointerCapture) {
        try { svg.setPointerCapture(event.pointerId); } catch (err) { /* ignore */ }
      }
    });

    svg.addEventListener("pointermove", function (event) {
      if (!self._drag) return;
      var dx = event.clientX - self._drag.startX;
      var dy = event.clientY - self._drag.startY;
      if (!self._dragged && Math.abs(dx) + Math.abs(dy) < 4) return;
      self._dragged = true;
      self.view.x = self._drag.originX + dx;
      self.view.y = self._drag.originY + dy;
      self._applyView();
    });

    function endDrag() {
      if (!self._drag) return;
      var wasDrag = self._dragged;
      var nodeId = self._drag.nodeId;
      self._drag = null;
      svg.classList.remove("is-dragging");
      if (!wasDrag) {
        // 点击空白处：通知外部收起节点详情；点击节点：选中并展示详情
        self.select(nodeId || null, true);
      }
    }
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", function () {
      self._drag = null;
      svg.classList.remove("is-dragging");
    });
    svg.addEventListener("pointerleave", function () {
      if (self._drag && self._dragged) return;
    });
  };

  DAGView.prototype._applyView = function () {
    var v = this.view;
    this.viewport.setAttribute(
      "transform",
      "translate(" + v.x.toFixed(2) + "," + v.y.toFixed(2) + ") scale(" + v.k.toFixed(4) + ")"
    );
    if (this.options.onZoom) this.options.onZoom(v.k);
  };

  DAGView.prototype.setGraph = function (workflow) {
    this.clear();
    var wf = workflow || {};
    this.nodes = Array.isArray(wf.nodes) ? wf.nodes.filter(function (n) { return n && n.id; }) : [];
    this.edges = Array.isArray(wf.edges) ? wf.edges : [];
    var layout = computeLayers(this.nodes, this.edges);
    this.positions = layout.positions;
    this._renderEdges(layout);
    this._renderNodes();
    this.fit(0.62);
    return this;
  };

  DAGView.prototype.clear = function () {
    while (this.viewport.firstChild) this.viewport.removeChild(this.viewport.firstChild);
    this.nodeEls = {};
    this.edgeEls = [];
    this.edgeMeta = [];
    this.selectedId = null;
  };

  DAGView.prototype._renderEdges = function (layout) {
    var self = this;
    var edgeLayer = svgEl("g", { class: "dag-edges" });
    layout.kept.forEach(function (item) {
      var s = self.positions[item.source];
      var t = self.positions[item.target];
      if (!s || !t) return;
      var path = svgEl("path", { class: "dag-edge", "marker-end": "url(#dag-arrow)" });
      path.setAttribute("d", bezier(s, t, !!layout.back[item.source + "\u0000" + item.target]));
      edgeLayer.appendChild(path);
      self.edgeEls.push(path);
      self.edgeMeta.push({ source: item.source, target: item.target, el: path });
    });
    this.viewport.appendChild(edgeLayer);
    this.edgeLayer = edgeLayer;
  };

  function bezier(from, to, isBack) {
    var x1 = from.x + NODE_W;
    var y1 = from.y + NODE_H / 2;
    var x2 = to.x;
    var y2 = to.y + NODE_H / 2;
    if (isBack || x2 <= x1) {
      // 回边（自循环 / 修复循环）：从底部绕回
      var sx = from.x + NODE_W / 2;
      var sy = from.y + NODE_H;
      var tx = to.x + NODE_W / 2;
      var ty = to.y + NODE_H;
      var dip = 46 + Math.abs(ty - sy) * 0.18;
      return (
        "M" + sx + "," + sy +
        " C" + sx + "," + (sy + dip) +
        " " + tx + "," + (ty + dip) +
        " " + tx + "," + ty
      );
    }
    var dx = Math.max(46, (x2 - x1) * 0.5);
    return (
      "M" + x1 + "," + y1 +
      " C" + (x1 + dx) + "," + y1 +
      " " + (x2 - dx) + "," + y2 +
      " " + x2 + "," + y2
    );
  }

  DAGView.prototype._renderNodes = function () {
    var self = this;
    var layer = svgEl("g", { class: "dag-nodes" });
    this.nodes.forEach(function (node) {
      var pos = self.positions[node.id];
      if (!pos) return;
      var color = typeColor(node.type);
      var group = svgEl("g", {
        class: "dag-node",
        "data-id": node.id,
        transform: "translate(" + pos.x + "," + pos.y + ")"
      });
      group.style.setProperty("--node-color", color);

      var halo = svgEl("rect", {
        width: NODE_W, height: NODE_H, rx: 12,
        fill: color, opacity: 0.07
      });
      var rect = svgEl("rect", {
        width: NODE_W, height: NODE_H, rx: 12,
        fill: "#141a2e", stroke: color, "stroke-width": 1.6, "stroke-opacity": 0.85
      });
      var dot = svgEl("circle", { cx: 16, cy: 21, r: 4.2, fill: color });
      var label = svgEl("text", { x: 28, y: 25, class: "node-label" });
      label.textContent = truncate(node.label || node.name || node.id, 9);
      var typeText = svgEl("text", { x: 16, y: 44, class: "node-type-label" });
      typeText.textContent = typeLabel(node.type);

      var agentsText = node.agent ? String(node.agent) : (node.tool ? String(node.tool) : "");
      if (agentsText) {
        var sub = svgEl("text", { x: 56, y: 44, class: "node-type-label", opacity: 0.9 });
        sub.textContent = truncate(agentsText, 9);
        group.appendChild(sub);
      }

      var title = svgEl("title");
      title.textContent =
        (node.label || node.id) + " · " + typeLabel(node.type) +
        (node.agent ? " · agent=" + node.agent : "") +
        (node.tool ? " · tool=" + node.tool : "");

      group.appendChild(halo);
      group.appendChild(rect);
      group.appendChild(dot);
      group.appendChild(label);
      group.appendChild(typeText);
      group.appendChild(title);
      group.dataset.nodeId = node.id;

      layer.appendChild(group);
      self.nodeEls[node.id] = group;
      if (self.statusMap[node.id]) self._applyNodeStatus(node.id, self.statusMap[node.id]);
    });
    this.viewport.appendChild(layer);
    this.nodeLayer = layer;
  };

  DAGView.prototype._applyNodeStatus = function (nodeId, status) {
    var el = this.nodeEls[nodeId];
    if (!el) return;
    el.classList.remove(
      "st-pending", "st-scheduled", "st-running", "st-succeeded",
      "st-failed", "st-skipped", "st-retrying", "st-waiting_human"
    );
    if (status) el.classList.add("st-" + status);
    var rect = el.querySelector("rect:nth-of-type(2)") || el.querySelector("rect");
    if (rect) {
      var strokeMap = {
        succeeded: "#34d399",
        failed: "#f87171",
        running: "#38bdf8",
        skipped: "#46536f"
      };
      if (status === "skipped") {
        el.setAttribute("opacity", "0.55");
      } else {
        el.removeAttribute("opacity");
      }
      if (strokeMap[status] && status !== "running") {
        rect.setAttribute("stroke", strokeMap[status]);
      }
    }
  };

  /** 批量更新节点状态：map = {nodeId: status} */
  DAGView.prototype.setStatuses = function (map) {
    var self = this;
    this.statusMap = map || {};
    Object.keys(this.statusMap).forEach(function (id) {
      self._applyNodeStatus(id, self.statusMap[id]);
    });
    // 连线高亮：目标节点 running 时高亮入边
    this.edgeMeta.forEach(function (meta) {
      var targetStatus = self.statusMap[meta.target];
      var active = targetStatus === "running" || targetStatus === "succeeded";
      meta.el.classList.toggle("is-active", !!active);
      meta.el.setAttribute("marker-end", active ? "url(#dag-arrow-active)" : "url(#dag-arrow)");
    });
  };

  DAGView.prototype.select = function (nodeId, notify) {
    var self = this;
    Object.keys(this.nodeEls).forEach(function (id) {
      self.nodeEls[id].classList.toggle("is-selected", id === nodeId);
    });
    this.selectedId = nodeId;
    if (notify && this.options.onSelect) {
      var node = this.nodes.filter(function (n) { return n.id === nodeId; })[0] || null;
      this.options.onSelect(node);
    }
  };

  DAGView.prototype.getNode = function (nodeId) {
    return this.nodes.filter(function (n) { return n.id === nodeId; })[0] || null;
  };

  DAGView.prototype.zoomTo = function (k, cx, cy) {
    var next = Math.min(2.2, Math.max(0.28, k));
    var v = this.view;
    var px = cx == null ? this.svg.clientWidth / 2 : cx;
    var py = cy == null ? this.svg.clientHeight / 2 : cy;
    var ratio = next / v.k;
    v.x = px - (px - v.x) * ratio;
    v.y = py - (py - v.y) * ratio;
    v.k = next;
    this._applyView();
  };

  DAGView.prototype.zoomBy = function (factor) {
    this.zoomTo(this.view.k * factor);
  };

  DAGView.prototype.fit = function (minScale) {
    if (!this.nodes.length) {
      this.view = { x: 0, y: 0, k: 1 };
      this._applyView();
      return;
    }
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    var positions = this.positions;
    Object.keys(positions).forEach(function (id) {
      var p = positions[id];
      minX = Math.min(minX, p.x);
      minY = Math.min(minY, p.y);
      maxX = Math.max(maxX, p.x + NODE_W);
      maxY = Math.max(maxY, p.y + NODE_H);
    });
    var pad = 38;
    var graphW = Math.max(1, maxX - minX);
    var graphH = Math.max(1, maxY - minY);
    var viewW = this.svg.clientWidth || 900;
    var viewH = this.svg.clientHeight || 560;
    var k = Math.min((viewW - pad * 2) / graphW, (viewH - pad * 2) / graphH);
    var floor = typeof minScale === "number" ? minScale : 0.3;
    // 演示场景工作流较宽：优先保证节点文字可读，必要时允许轻微横向溢出
    floor = Math.min(floor, (viewW - 16) / graphW);
    k = Math.min(1.35, Math.max(floor, k));
    this.view.k = k;
    this.view.x = (viewW - graphW * k) / 2 - minX * k;
    this.view.y = (viewH - graphH * k) / 2 - minY * k;
    this._applyView();
  };

  DAGView.prototype.destroy = function () {
    this.clear();
    this.nodes = [];
    this.edges = [];
  };

  global.AgentWeaveDAG = {
    DAGView: DAGView,
    TYPE_COLORS: TYPE_COLORS,
    TYPE_LABELS: TYPE_LABELS,
    typeColor: typeColor,
    typeLabel: typeLabel
  };
})(window);
