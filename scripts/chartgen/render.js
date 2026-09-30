#!/usr/bin/env node
/**
 * private-copilot 图表渲染器。
 *
 * 输入：chart.py 生成的 JSON（{type, title, subtitle, ...}）
 * 输出：PNG 文件路径（stdout 打印）
 *
 * 用法：node render.js <data.json> <out.png>
 *
 * 为什么用 @napi-rs/canvas：
 *   纯 Python 画图要自己实现字体渲染，中文几乎不可能做好。
 *   系统装了 fonts-noto-cjk 后，cairo 可以直接排版中文。
 *   2x 超采样保证在飞书里放大也清晰。
 */

const { createCanvas, GlobalFonts } = require('@napi-rs/canvas');
const fs = require('fs');

// ---- 字体 ----
const FONT_CANDIDATES = [
  '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
  '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
];
let fontFamily = 'sans-serif';
for (const f of FONT_CANDIDATES) {
  if (fs.existsSync(f)) {
    try {
      GlobalFonts.registerFromPath(f, 'NotoSC');
      fontFamily = 'NotoSC';
    } catch (_) { /* 换下一个 */ }
    break;
  }
}
if (fontFamily === 'sans-serif') {
  console.error('warn: 未找到 Noto CJK 字体，中文可能显示为方块。请 apt install fonts-noto-cjk');
}

const W = 960, H = 620, SCALE = 2;
const PALETTE = ['#4C6EF5', '#F76707', '#12B886', '#BE4BDB', '#FA5252',
                 '#15AABF', '#82C91E', '#7950F2', '#E8590C', '#1098AD'];
const colorOf = i => PALETTE[i % PALETTE.length];
const INK = '#1a1a1a', SUB = '#6b6b6b', MUTE = '#8a8a8a';

const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = process.argv[3] || '/tmp/chart.png';

const canvas = createCanvas(W * SCALE, H * SCALE);
const ctx = canvas.getContext('2d');
ctx.scale(SCALE, SCALE);
ctx.fillStyle = '#ffffff';
ctx.fillRect(0, 0, W, H);

function head(title, subtitle) {
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = INK;
  ctx.font = `34px ${fontFamily}`;
  ctx.fillText(title || '', 60, 76);
  if (subtitle) {
    ctx.font = `19px ${fontFamily}`;
    ctx.fillStyle = SUB;
    ctx.fillText(subtitle, 60, 108);
  }
}

function footer(text) {
  if (!text) return;
  ctx.textAlign = 'left';
  ctx.fillStyle = '#9a9a9a';
  ctx.font = `15px ${fontFamily}`;
  ctx.fillText(text, 60, H - 40);
}

function noData(msg) {
  ctx.textAlign = 'center';
  ctx.fillStyle = '#c0c0c0';
  ctx.font = `26px ${fontFamily}`;
  ctx.fillText(msg || '暂无数据', W / 2, H / 2);
}

/** 金额格式化：大数用「万」，小数保留两位。 */
function money(v) {
  const n = Number(v) || 0;
  if (Math.abs(n) >= 10000) return (n / 10000).toFixed(1) + '万';
  return n.toFixed(2).replace(/\.00$/, '');
}

/** 用真实字体度量文本宽度，避免靠字符数估算。 */
function measure(str, font) {
  ctx.font = font;
  return ctx.measureText(String(str)).width;
}


/**
 * 画 x 轴标签，保证互不重叠、首尾不出界。
 *
 * 关键点：先按间隔挑候选，再从右往左依次放置 —— 这样「最后一个」永远保留，
 * 前面与它冲突的候选被丢弃。从左往右放会先占住位置，导致末尾硬挤。
 */
function drawXLabels(keys, L, pw, step, y) {
  const FONT = `17px ${fontFamily}`;
  const n = keys.length;
  if (!n) return;

  const widths = keys.map(k => measure(k, FONT));
  const widest = Math.max(...widths) + 26;
  const maxLabels = Math.max(2, Math.floor(pw / widest));
  const every = Math.max(1, Math.ceil(n / maxLabels));

  // 候选下标：等间隔 + 强制包含最后一个
  const idx = [];
  for (let i = 0; i < n; i += every) idx.push(i);
  if (idx[idx.length - 1] !== n - 1) idx.push(n - 1);

  // 从右往左放置
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = INK;
  ctx.font = FONT;

  let rightLimit = Infinity;
  for (let j = idx.length - 1; j >= 0; j--) {
    const i = idx[j];
    const w = widths[i];
    let x = L + step * i;
    x = Math.max(L + w / 2, Math.min(x, L + pw - w / 2));
    const right = x + w / 2;
    if (right > rightLimit) continue;      // 与右边已放的标签重叠，丢弃
    ctx.fillText(String(keys[i]), x, y);
    rightLimit = x - w / 2 - 8;
  }
}

// ---------------------------------------------------------------- 饼图
function drawPie(d) {
  const items = d.items || [];
  head(d.title, d.subtitle);
  if (!items.length) return noData();
  const total = items.reduce((s, x) => s + x.v, 0) || 1;

  const cx = 300, cy = 375, r = 175;
  let start = -Math.PI / 2;
  items.forEach((s, i) => {
    const ang = (s.v / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, start, start + ang);
    ctx.closePath();
    ctx.fillStyle = colorOf(i);
    ctx.fill();
    ctx.lineWidth = 3;
    ctx.strokeStyle = '#fff';
    ctx.stroke();

    const pct = (s.v / total) * 100;
    if (pct >= 8) {                      // 太小就不写字，避免压线
      const mid = start + ang / 2;
      ctx.fillStyle = '#fff';
      ctx.font = `bold 24px ${fontFamily}`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(pct.toFixed(1) + '%',
                   cx + Math.cos(mid) * r * 0.62,
                   cy + Math.sin(mid) * r * 0.62);
    }
    start += ang;
  });

  // 图例（右侧，两行一组）。按可用高度算最多放几项，避免溢出画布。
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  const LEG_TOP = 180, LEG_ROW = 68, LEG_BOTTOM = H - 70;
  const maxRows = Math.max(1, Math.floor((LEG_BOTTOM - LEG_TOP) / LEG_ROW));
  const shown = items.slice(0, maxRows);
  const hidden = items.length - shown.length;
  const lx0 = 600;
  let ly = LEG_TOP;

  shown.forEach((s, i) => {
    const pct = (s.v / total) * 100;
    ctx.fillStyle = colorOf(i);
    ctx.beginPath();
    ctx.roundRect(lx0, ly - 16, 22, 22, 5);
    ctx.fill();

    ctx.fillStyle = INK;
    ctx.font = `23px ${fontFamily}`;
    ctx.fillText(s.k, lx0 + 36, ly + 2);

    ctx.fillStyle = '#444';
    ctx.font = `20px ${fontFamily}`;
    ctx.fillText('¥' + money(s.v), lx0 + 36, ly + 32);

    ctx.fillStyle = MUTE;
    ctx.font = `18px ${fontFamily}`;
    const extra = `${pct.toFixed(1)}%` + (s.n != null ? ` · ${s.n} 笔` : '');
    ctx.fillText(extra, lx0 + 200, ly + 32);
    ly += LEG_ROW;
  });
  if (hidden > 0) {
    ctx.fillStyle = MUTE;
    ctx.font = `18px ${fontFamily}`;
    ctx.fillText(`另有 ${hidden} 项未显示`, lx0, ly - 6);
  }
}

// ---------------------------------------------------------------- 柱状图
function drawBar(d) {
  const items = d.items || [];
  head(d.title, d.subtitle);
  if (!items.length) return noData();

  const L = 110, R = 60, T = 140, B = 120;
  const pw = W - L - R, ph = H - T - B;
  const mx = Math.max(...items.map(x => x.v)) || 1;

  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const y = T + ph - (ph * i) / 4;
    ctx.strokeStyle = '#ececec';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
    ctx.fillStyle = MUTE;
    ctx.font = `16px ${fontFamily}`;
    ctx.fillText(money((mx * i) / 4), L - 14, y);
  }

  const slot = pw / items.length;
  const bw = Math.max(14, Math.min(slot * 0.6, 84));
  ctx.textAlign = 'center';
  items.forEach((s, i) => {
    const x = L + slot * i + (slot - bw) / 2;
    const bh = Math.max(2, (ph * s.v) / mx);
    ctx.fillStyle = colorOf(i);
    ctx.beginPath();
    ctx.roundRect(x, T + ph - bh, bw, bh, [6, 6, 0, 0]);
    ctx.fill();

    ctx.fillStyle = '#5a5a5a';
    ctx.font = `17px ${fontFamily}`;
    ctx.textBaseline = 'alphabetic';
    ctx.fillText(money(s.v), x + bw / 2, T + ph - bh - 10);

    ctx.fillStyle = INK;
    ctx.font = `18px ${fontFamily}`;
    // 柱子太密时隔一个画一个标签，避免底部糊成一片
    const slotW = measure(s.k, `18px ${fontFamily}`) + 14;
    if (slot > slotW || i % 2 === 0) {
      ctx.fillText(String(s.k), x + bw / 2, T + ph + 30);
    }
  });

  ctx.strokeStyle = '#d5d5d5';
  ctx.beginPath(); ctx.moveTo(L, T + ph); ctx.lineTo(L + pw, T + ph); ctx.stroke();
}

// ---------------------------------------------------------------- 折线图
function drawLine(d) {
  const pts = d.points || [];
  head(d.title, d.subtitle);
  if (!pts.length) return noData();

  const L = 120, R = 70, T = 140, B = 110;
  const pw = W - L - R, ph = H - T - B;
  const vals = pts.map(p => p.v);
  let mx = Math.max(...vals), mn = Math.min(...vals);
  if (mx === mn) { mx += Math.abs(mx) * 0.1 || 1; mn = Math.max(0, mn - 1); }
  const pad = (mx - mn) * 0.14;
  mx += pad; mn = Math.max(0, mn - pad);
  const span = (mx - mn) || 1;

  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const y = T + ph - (ph * i) / 4;
    ctx.strokeStyle = '#ececec';
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
    ctx.fillStyle = MUTE;
    ctx.font = `16px ${fontFamily}`;
    ctx.fillText(money(mn + (span * i) / 4), L - 14, y);
  }

  const step = pts.length > 1 ? pw / (pts.length - 1) : 0;
  const xy = pts.map((p, i) => [
    L + step * i,
    T + ph - (ph * (p.v - mn)) / span,
  ]);

  // 面积填充
  const grad = ctx.createLinearGradient(0, T, 0, T + ph);
  grad.addColorStop(0, 'rgba(76,110,245,0.28)');
  grad.addColorStop(1, 'rgba(76,110,245,0.02)');
  ctx.beginPath();
  ctx.moveTo(xy[0][0], T + ph);
  xy.forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.lineTo(xy[xy.length - 1][0], T + ph);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // 折线
  ctx.beginPath();
  xy.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = '#4C6EF5';
  ctx.lineWidth = 3;
  ctx.lineJoin = 'round';
  ctx.stroke();

  // 数据点
  xy.forEach(([x, y]) => {
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#fff'; ctx.fill();
    ctx.strokeStyle = '#4C6EF5'; ctx.lineWidth = 3; ctx.stroke();
  });

  ctx.strokeStyle = '#d5d5d5';
  ctx.beginPath(); ctx.moveTo(L, T + ph); ctx.lineTo(L + pw, T + ph); ctx.stroke();

  // x 轴标签：先按宽度定间隔，再逐个检查是否与前一个实际位置相撞。
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = INK;
  ctx.font = `17px ${fontFamily}`;
  drawXLabels(pts.map(p => p.k), L, pw, step, T + ph + 32);
}

// ---------------------------------------------------------------- 多折线
function drawMultiLine(d) {
  const labels = d.labels || [];
  const series = d.series || [];
  head(d.title, d.subtitle);
  if (!labels.length || !series.length) return noData();

  const L = 130, R = 210, T = 140, B = 110;
  const pw = W - L - R, ph = H - T - B;
  const all = series.flatMap(s => s.values);
  let mx = Math.max(...all), mn = Math.min(...all);
  if (mx === mn) { mx += 1; mn = Math.max(0, mn - 1); }
  const pad = (mx - mn) * 0.12;
  mx += pad; mn = Math.max(0, mn - pad);
  const span = (mx - mn) || 1;

  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const y = T + ph - (ph * i) / 4;
    ctx.strokeStyle = '#ececec';
    ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke();
    ctx.fillStyle = MUTE;
    ctx.font = `16px ${fontFamily}`;
    ctx.fillText(money(mn + (span * i) / 4), L - 14, y);
  }

  const step = labels.length > 1 ? pw / (labels.length - 1) : 0;
  series.forEach((s, si) => {
    const c = colorOf(si);
    const xy = s.values.map((v, i) => [
      L + step * i,
      T + ph - (ph * (v - mn)) / span,
    ]);
    ctx.beginPath();
    xy.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.strokeStyle = c;
    ctx.lineWidth = 3;
    ctx.lineJoin = 'round';
    ctx.stroke();
    xy.forEach(([x, y]) => {
      ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = '#fff'; ctx.fill();
      ctx.strokeStyle = c; ctx.lineWidth = 3; ctx.stroke();
    });
  });

  ctx.strokeStyle = '#d5d5d5';
  ctx.beginPath(); ctx.moveTo(L, T + ph); ctx.lineTo(L + pw, T + ph); ctx.stroke();

  // x 标签（同样的防重叠逻辑）
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = INK;
  ctx.font = `17px ${fontFamily}`;
  drawXLabels(labels, L, pw, step, T + ph + 32);

  // 右侧图例（同样按高度限制）
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  const LEG_TOP = 180, LEG_ROW = 74, LEG_BOTTOM = H - 70;
  const maxRows = Math.max(1, Math.floor((LEG_BOTTOM - LEG_TOP) / LEG_ROW));
  let ly = LEG_TOP;
  series.slice(0, maxRows).forEach((s, si) => {
    ctx.fillStyle = colorOf(si);
    ctx.beginPath(); ctx.roundRect(L + pw + 30, ly - 16, 22, 22, 5); ctx.fill();
    ctx.fillStyle = INK;
    ctx.font = `22px ${fontFamily}`;
    ctx.fillText(s.name, L + pw + 62, ly + 2);
    ctx.fillStyle = SUB;
    ctx.font = `19px ${fontFamily}`;
    const last = s.values[s.values.length - 1];
    ctx.fillText('¥' + money(last), L + pw + 62, ly + 30);
    ly += LEG_ROW;
  });
}

// ---------------------------------------------------------------- 入口
switch (data.type) {
  case 'pie': drawPie(data); break;
  case 'bar': drawBar(data); break;
  case 'line': drawLine(data); break;
  case 'multi_line': drawMultiLine(data); break;
  default: noData('未知图表类型: ' + data.type);
}
footer(data.footer);

fs.writeFileSync(out, canvas.toBuffer('image/png'));
console.log(out);
