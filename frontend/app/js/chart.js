// Daily column chart: one series, one axis. Two measures of different scale
// (units picked vs. mistakes caught) are drawn as two of these stacked, never
// as a dual-axis chart. Every value is reachable without hover via the table.

import { h } from "../../shared/dom.js";

const NS = "http://www.w3.org/2000/svg";

function s(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, String(v));
  return el;
}

function niceMax(v) {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pow >= v) return m * pow;
  return 10 * pow;
}

function dayLabel(iso) {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" })
    .format(new Date(`${iso}T00:00:00Z`));
}

/**
 * columnChart({title, points: [{date, value}], unit})
 */
export function columnChart({ title, points, unit }) {
  const W = 720;
  const H = 200;
  const pad = { top: 12, right: 8, bottom: 26, left: 40 };
  const innerW = W - pad.left - pad.right;
  const innerH = H - pad.top - pad.bottom;
  const max = niceMax(Math.max(0, ...points.map((p) => p.value)));
  const slot = innerW / Math.max(points.length, 1);
  const barW = Math.min(24, Math.max(2, slot - 2)); // 2px surface gap minimum
  const y = (v) => pad.top + innerH - (v / max) * innerH;

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", "aria-label": title });
  for (const t of [0, max / 2, max]) {
    const yy = y(t);
    svg.appendChild(s("line", { x1: pad.left, x2: W - pad.right, y1: yy, y2: yy, class: "chart-grid" }));
    const label = s("text", { x: pad.left - 6, y: yy + 4, class: "chart-tick", "text-anchor": "end" });
    label.textContent = new Intl.NumberFormat().format(t);
    svg.appendChild(label);
  }

  const tip = h("div", { class: "chart-tip", hidden: true });
  const wrap = h("div", { class: "chart" });

  const showTip = (p, cx) => {
    tip.replaceChildren(
      h("strong", null, `${new Intl.NumberFormat().format(p.value)} ${unit}`),
      h("span", null, dayLabel(p.date)),
    );
    tip.hidden = false;
    const pct = (cx / W) * 100;
    tip.style.left = `${Math.min(88, Math.max(12, pct))}%`;
  };

  points.forEach((p, i) => {
    const cx = pad.left + slot * i + slot / 2;
    const x = cx - barW / 2;
    const top = y(p.value);
    const hgt = pad.top + innerH - top;
    // Hit target is the whole slot, taller and wider than the bar itself.
    const hit = s("rect", {
      x: pad.left + slot * i, y: pad.top, width: slot, height: innerH, class: "chart-hit", tabindex: 0,
      "aria-label": `${dayLabel(p.date)}: ${p.value} ${unit}`,
    });
    if (p.value > 0) {
      const r = Math.min(4, barW / 2, hgt);
      // Rounded data-end, square at the baseline.
      const d = `M${x},${top + hgt} V${top + r} Q${x},${top} ${x + r},${top} H${x + barW - r} ` +
        `Q${x + barW},${top} ${x + barW},${top + r} V${top + hgt} Z`;
      const bar = s("path", { d, class: "chart-bar" });
      svg.appendChild(bar);
      hit.addEventListener("pointerenter", () => bar.classList.add("hover"));
      hit.addEventListener("pointerleave", () => bar.classList.remove("hover"));
      hit.addEventListener("focus", () => bar.classList.add("hover"));
      hit.addEventListener("blur", () => bar.classList.remove("hover"));
    }
    hit.addEventListener("pointerenter", () => showTip(p, cx));
    hit.addEventListener("focus", () => showTip(p, cx));
    hit.addEventListener("pointerleave", () => (tip.hidden = true));
    hit.addEventListener("blur", () => (tip.hidden = true));
    svg.appendChild(hit);
    const every = Math.ceil(points.length / 8);
    // Anchor labels on the most recent day so the last tick never collides.
    if ((points.length - 1 - i) % every === 0) {
      const t = s("text", { x: cx, y: H - 6, class: "chart-tick", "text-anchor": "middle" });
      t.textContent = dayLabel(p.date);
      svg.appendChild(t);
    }
  });

  const total = points.reduce((a, p) => a + p.value, 0);
  const tableEl = h("table", { class: "table table-compact", hidden: true },
    h("thead", null, h("tr", null, h("th", null, "Day"), h("th", { class: "t-right" }, unit))),
    h("tbody", null, ...points.slice().reverse().map((p) =>
      h("tr", null, h("td", null, dayLabel(p.date)), h("td", { class: "t-right num" }, String(p.value))))));
  const toggle = h("button", {
    class: "link-btn small",
    onclick: () => {
      tableEl.hidden = !tableEl.hidden;
      svgHost.hidden = !tableEl.hidden;
      toggle.textContent = tableEl.hidden ? "Show table" : "Show chart";
    },
  }, "Show table");
  const svgHost = h("div", { class: "chart-plot" }, svg, tip);
  wrap.append(
    h("div", { class: "chart-head" },
      h("h3", null, title),
      h("span", { class: "muted small" }, `${new Intl.NumberFormat().format(total)} total`),
      toggle),
    svgHost,
    tableEl,
  );
  return wrap;
}
