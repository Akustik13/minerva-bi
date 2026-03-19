/**
 * strategy/static/strategy/canvas.js
 * Phase 1 — SVG-based canvas for strategy visualization.
 * Loaded only when user clicks "Показати canvas" button.
 */

(function () {
  'use strict';

  var NODE_W = 160;
  var NODE_H = 60;
  var COLORS = {
    email:    { bg: 'rgba(88,166,255,.18)',  border: '#58a6ff' },
    call:     { bg: 'rgba(255,165,0,.18)',   border: '#e3a030' },
    pause:    { bg: 'rgba(96,125,139,.18)',  border: '#607d8b' },
    decision: { bg: 'rgba(227,179,65,.18)',  border: '#e3b341' },
  };
  var OUTCOME_COLORS = {
    done_pos:    '#3fb950',
    done_neg:    '#f85149',
    skipped:     '#607d8b',
    no_response: '#e3b341',
    pending:     null,
  };

  function init(strategyPk, containerId) {
    var container = document.getElementById(containerId);
    if (!container) return;

    fetch('/strategy/' + strategyPk + '/canvas/data/')
      .then(function (r) { return r.json(); })
      .then(function (data) { render(data, container); })
      .catch(function (e) {
        container.innerHTML = '<p style="color:var(--err)">Помилка завантаження даних canvas.</p>';
      });
  }

  function render(data, container) {
    var nodes = data.nodes;
    if (!nodes || nodes.length === 0) {
      container.innerHTML = '<p style="color:var(--text-muted)">Кроки відсутні.</p>';
      return;
    }

    // Auto-layout if all coords are 0
    var allZero = nodes.every(function (n) { return n.x === 0 && n.y === 0; });
    if (allZero) {
      nodes.forEach(function (n, i) {
        n.x = 40;
        n.y = 40 + i * (NODE_H + 40);
      });
    }

    var maxX = Math.max.apply(null, nodes.map(function (n) { return n.x; })) + NODE_W + 60;
    var maxY = Math.max.apply(null, nodes.map(function (n) { return n.y; })) + NODE_H + 60;

    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', maxX);
    svg.setAttribute('height', maxY);
    svg.style.display = 'block';
    svg.style.maxWidth = '100%';
    svg.style.overflow = 'auto';

    // Build id → node map
    var nodeMap = {};
    nodes.forEach(function (n) { nodeMap[n.id] = n; });

    // Draw edges
    nodes.forEach(function (n) {
      if (n.next_yes_id && nodeMap[n.next_yes_id]) {
        drawEdge(svg, n, nodeMap[n.next_yes_id], '#3fb950', n.step_type === 'decision' ? 'Так' : '');
      }
      if (n.next_no_id && nodeMap[n.next_no_id]) {
        drawEdge(svg, n, nodeMap[n.next_no_id], '#f85149', 'Ні');
      }
    });

    // Draw nodes
    nodes.forEach(function (n) {
      drawNode(svg, n);
    });

    container.innerHTML = '';
    container.appendChild(svg);
  }

  function drawEdge(svg, from, to, color, label) {
    var x1 = from.x + NODE_W / 2;
    var y1 = from.y + NODE_H;
    var x2 = to.x + NODE_W / 2;
    var y2 = to.y;

    var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    var cy1  = y1 + 20;
    var cy2  = y2 - 20;
    path.setAttribute('d', 'M' + x1 + ',' + y1 + ' C' + x1 + ',' + cy1 + ' ' + x2 + ',' + cy2 + ' ' + x2 + ',' + y2);
    path.setAttribute('stroke', color || 'var(--border-strong)');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('fill', 'none');
    path.setAttribute('marker-end', 'url(#arrow)');
    svg.appendChild(path);

    if (label) {
      var mx = (x1 + x2) / 2;
      var my = (y1 + y2) / 2;
      var text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', mx + 4);
      text.setAttribute('y', my);
      text.setAttribute('fill', color);
      text.setAttribute('font-size', '11');
      text.textContent = label;
      svg.appendChild(text);
    }
  }

  function drawNode(svg, n) {
    var c = COLORS[n.step_type] || COLORS.email;
    var outlineColor = OUTCOME_COLORS[n.outcome] || c.border;

    var g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('transform', 'translate(' + n.x + ',' + n.y + ')');

    var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('width', NODE_W);
    rect.setAttribute('height', NODE_H);
    rect.setAttribute('rx', '8');
    rect.setAttribute('fill', c.bg);
    rect.setAttribute('stroke', outlineColor);
    rect.setAttribute('stroke-width', '2');
    g.appendChild(rect);

    // Icon + title text
    var icon = { email: '📧', call: '📞', pause: '⏸', decision: '🔀' }[n.step_type] || '•';
    var t1 = svgText(icon + ' ' + truncate(n.title, 16), 10, 22, 13, '#e6edf3', 600);
    g.appendChild(t1);

    // Outcome label
    if (n.outcome && n.outcome !== 'pending') {
      var outLabel = outcomeLabel(n.outcome);
      var t2 = svgText(outLabel, 10, 42, 11, OUTCOME_COLORS[n.outcome] || '#9aafbe', 400);
      g.appendChild(t2);
    } else if (n.scheduled_at) {
      var datePart = n.scheduled_at.substring(0, 10);
      var t3 = svgText('📅 ' + datePart, 10, 42, 10, '#9aafbe', 400);
      g.appendChild(t3);
    }

    svg.appendChild(g);
  }

  function svgText(content, x, y, size, fill, weight) {
    var t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', x);
    t.setAttribute('y', y);
    t.setAttribute('fill', fill);
    t.setAttribute('font-size', size);
    t.setAttribute('font-weight', weight);
    t.setAttribute('font-family', 'system-ui, sans-serif');
    t.textContent = content;
    return t;
  }

  function truncate(s, n) {
    return s && s.length > n ? s.substring(0, n) + '…' : s;
  }

  function outcomeLabel(outcome) {
    var labels = {
      done_pos: '✅ Виконано (+)',
      done_neg: '⚠️ Виконано (−)',
      skipped: '⏭ Пропущено',
      no_response: '🔇 Без відповіді',
    };
    return labels[outcome] || outcome;
  }

  // Expose to global scope
  window.StrategyCanvas = { init: init };
})();
