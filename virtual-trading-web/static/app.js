/**
 * A股虚拟盘 Web 系统 · 前端逻辑
 * 酱酱 vs 小克 双账户对比
 */

// ── Global state ────────────────────────────────────────
let currentDate = "";
let currentTrade = null; // {type, code, name, price}
let compareChart = null;

// ── Init ─────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  loadDates();
  loadStatus();
  loadSignals();
  loadCompare();
  loadL1();
  loadRotation();
});

// ═══════════════════════════════════════════════════════
//  TAB SWITCHING
// ═══════════════════════════════════════════════════════

function initTabs() {
  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("tab-" + btn.dataset.tab).classList.add("active");

      const tabName = btn.dataset.tab;
      if (tabName === "holdings") loadHoldings();
      if (tabName === "trades") loadTrades();
      if (tabName === "replay") loadReplay();
    });
  });
}

// ═══════════════════════════════════════════════════════
//  API HELPERS
// ═══════════════════════════════════════════════════════

async function api(path) {
  const resp = await fetch(path);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function apiPost(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

function fmt(n, decimals = 2) {
  if (n == null || isNaN(n)) return "--";
  return Number(n).toLocaleString("zh-CN", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtPct(n) {
  if (n == null || isNaN(n)) return "--";
  const v = Number(n);
  const sign = v >= 0 ? "+" : "";
  return sign + v.toFixed(2) + "%";
}

function pnlClass(n) {
  if (n == null || isNaN(n)) return "";
  return Number(n) >= 0 ? "up" : "down";
}

// ═══════════════════════════════════════════════════════
//  DATES
// ═══════════════════════════════════════════════════════

async function loadDates() {
  try {
    const data = await api("/api/dates");
    const sel = document.getElementById("date-select");
    sel.innerHTML = "";
    data.dates.forEach(d => {
      const opt = document.createElement("option");
      opt.value = d.date;
      opt.textContent = d.complete ? d.date : d.date + " (不完备)";
      if (!d.complete) opt.style.color = "#555";
      sel.appendChild(opt);
    });
    if (data.latest) {
      currentDate = data.latest;
      sel.value = currentDate;
    }
  } catch (e) {
    console.error("loadDates:", e);
  }
}

function onDateChange() {
  currentDate = document.getElementById("date-select").value;
  loadSignals();
  loadL1();
  loadRotation();
  loadStopLoss();
}

// ═══════════════════════════════════════════════════════
//  STATUS (account cards)
// ═══════════════════════════════════════════════════════

async function loadStatus() {
  try {
    const data = await api("/api/status");
    // 酱酱
    document.getElementById("jj-total").textContent = "¥" + fmt(data.jiangjiang.total_value);
    document.getElementById("jj-cash").textContent = "¥" + fmt(data.jiangjiang.cash);
    document.getElementById("jj-count").textContent = data.jj_holdings_count;
    const jjPnl = document.getElementById("jj-pnl");
    jjPnl.textContent = fmtPct(data.jiangjiang.pnl_pct);
    jjPnl.className = "card-pnl " + pnlClass(data.jiangjiang.pnl_pct);

    // 小克
    document.getElementById("xk-total").textContent = "¥" + fmt(data.xiaoke.total_value);
    document.getElementById("xk-cash").textContent = "¥" + fmt(data.xiaoke.cash);
    document.getElementById("xk-count").textContent = data.xk_holdings_count;
    const xkPnl = document.getElementById("xk-pnl");
    xkPnl.textContent = fmtPct(data.xiaoke.pnl_pct);
    xkPnl.className = "card-pnl " + pnlClass(data.xiaoke.pnl_pct);
  } catch (e) {
    console.error("loadStatus:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  L1 BAR
// ═══════════════════════════════════════════════════════

async function loadL1() {
  try {
    const data = await api("/api/l1?date=" + (currentDate || ""));
    if (data.error) return;

    const regimeEl = document.getElementById("l1-regime");
    regimeEl.textContent = "📊 " + data.regime;
    regimeEl.className = data.regime === "多头趋势" ? "regime-safe"
      : data.regime === "震荡市" ? "regime-shock" : "regime-risk";

    document.getElementById("l1-volume").textContent = "📈 " + (data.volume_analysis || "");

    // Show index details
    const indices = data.indices || {};
    const parts = [];
    for (const [code, info] of Object.entries(indices)) {
      const above = info.above_ma20 ? "↑MA20" : "↓MA20";
      parts.push(`${info.name}: ${info.close.toFixed(0)} ${above} (${fmtPct(info.chg_pct)})`);
    }
    document.getElementById("l1-detail").textContent = parts.join("  ·  ");
  } catch (e) {
    console.error("loadL1:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  ROTATION
// ═══════════════════════════════════════════════════════

async function loadRotation() {
  try {
    const data = await api("/api/rotation?date=" + (currentDate || ""));
    if (data.error) return;
    document.getElementById("rotation-label").textContent = "🔄 " + (data.rotation_label || "--");
    const speed = data.rotation_speed;
    document.getElementById("rotation-speed").textContent =
      speed != null ? `轮动速度: ${(speed * 100).toFixed(0)}%` : "";
    document.getElementById("zb-rate").textContent =
      data.zb_rate != null ? `炸板率: ${data.zb_rate}%` : "";
  } catch (e) {
    console.error("loadRotation:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  SIGNALS TABLE
// ═══════════════════════════════════════════════════════

async function loadSignals() {
  try {
    const data = await api("/api/signals?date=" + (currentDate || ""));
    const tbody = document.getElementById("signals-tbody");
    tbody.innerHTML = "";

    if (!data.stocks || data.stocks.length === 0) {
      tbody.innerHTML = '<tr><td colspan="14" style="text-align:center;color:#8b8fa3;padding:24px;">暂无信号数据</td></tr>';
      return;
    }

    data.stocks.forEach(s => {
      const tr = document.createElement("tr");

      const peText = s.pe != null && s.pe < 0
        ? `<span class="pe-negative">${s.pe.toFixed(1)}</span>`
        : (s.pe != null ? s.pe.toFixed(1) : "--");

      const chgClass = s.chg_pct >= 0 ? "chg-up" : "chg-down";

      // 标签
      let labelHtml = "";
      const label = s.label || "";
      if (label === "强候选") labelHtml = '<span class="label-tag strong">强候选</span>';
      else if (label === "观察") labelHtml = '<span class="label-tag watch">观察</span>';
      else if (label === "风控") labelHtml = '<span class="label-tag risk">风控</span>';
      else labelHtml = '<span class="label-tag weak">弱</span>';

      // 规则列表
      const rules = (s.rules || []).join(", ");

      // 操作按钮：风控不显示买入
      let actionHtml = "";
      if (label === "风控") {
        actionHtml = '<span style="color:#f0a040;font-size:12px;">⛔ 风控</span>';
      } else if (label === "弱") {
        actionHtml = '<span style="color:#8b8fa3;font-size:12px;">—</span>';
      } else {
        actionHtml = `<button class="btn-buy" onclick="openBuy('${s.code}','${s.name}',${s.price})">买入</button>`;
      }

      tr.innerHTML = `
        <td>${s.code}</td>
        <td>${s.name}</td>
        <td>${s.lb}板</td>
        <td>¥${fmt(s.price)}</td>
        <td class="${chgClass}">${fmtPct(s.chg_pct)}</td>
        <td>${peText}</td>
        <td>${s.turnover != null ? s.turnover.toFixed(2) + "%" : "--"}</td>
        <td>${s.vol_ratio != null ? s.vol_ratio.toFixed(2) : "--"}</td>
        <td>${s.market_value != null ? fmt(s.market_value) : "--"}</td>
        <td>${s.hybk || "--"}</td>
        <td>${s.buy_score}/4</td>
        <td>${labelHtml}</td>
        <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;" title="${rules}">${rules}</td>
        <td>${actionHtml}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error("loadSignals:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  HOLDINGS TAB
// ═══════════════════════════════════════════════════════

async function loadHoldings() {
  await Promise.all([loadJJHoldings(), loadXKHoldings(), loadStopLoss()]);
}

async function loadJJHoldings() {
  try {
    const data = await api("/api/holdings/jj");
    const slData = await api("/api/stop-loss/jj");

    // Build stop-loss lookup
    const slMap = {};
    if (slData.holdings) {
      slData.holdings.forEach(h => { slMap[h.code] = h; });
    }

    const tbody = document.getElementById("jj-holdings-tbody");
    tbody.innerHTML = "";

    if (!data.holdings || data.holdings.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#8b8fa3;padding:16px;">酱酱还没有持仓～</td></tr>';
      return;
    }

    data.holdings.forEach(h => {
      const sl = slMap[h.code];
      const curPrice = sl ? sl.current_price : h.cost_price;
      const mktVal = curPrice * h.shares;
      const pnl = (curPrice - h.cost_price) * h.shares;
      const pnlPct = (curPrice - h.cost_price) / h.cost_price * 100;

      const slDot = sl
        ? `<span class="sl-dot ${sl.status_color}" title="${sl.status_text}">${sl.status_text}</span>`
        : "--";

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${h.code}</td>
        <td>${h.name}</td>
        <td>¥${fmt(h.cost_price)}</td>
        <td>¥${fmt(curPrice)}</td>
        <td>${h.shares}</td>
        <td>¥${fmt(mktVal)}</td>
        <td class="${pnlClass(pnl)}">${fmtPct(pnlPct)} (¥${fmt(pnl)})</td>
        <td>${slDot}</td>
        <td><button class="btn-sell" onclick="openSell('${h.code}','${h.name}',${curPrice},${h.shares})">卖出</button></td>
      `;
      tbody.appendChild(tr);
    });

    // Stop-loss banner
    if (slData.any_triggered) {
      const triggered = slData.holdings.filter(h => h.triggered_count > 0);
      const names = triggered.map(h => h.name).join("、");
      showAlert(`🚨 止损预警：${names} 触发止损信号，请及时处理！`);
    }
  } catch (e) {
    console.error("loadJJHoldings:", e);
  }
}

async function loadXKHoldings() {
  try {
    const data = await api("/api/holdings/xk");
    const tbody = document.getElementById("xk-holdings-tbody");
    tbody.innerHTML = "";

    if (!data.holdings || data.holdings.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#8b8fa3;padding:16px;">小克还没有持仓～</td></tr>';
      return;
    }

    data.holdings.forEach(h => {
      const curPrice = h.current_price || h.cost_price || 0;
      const mktVal = curPrice * (h.shares || 0);
      const pnl = (curPrice - (h.cost_price || 0)) * (h.shares || 0);
      const pnlPct = h.cost_price ? (curPrice - h.cost_price) / h.cost_price * 100 : 0;

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${h.code || ""}</td>
        <td>${h.name || ""}</td>
        <td>¥${fmt(h.cost_price)}</td>
        <td>¥${fmt(curPrice)}</td>
        <td>${h.shares || 0}</td>
        <td>¥${fmt(mktVal)}</td>
        <td class="${pnlClass(pnl)}">${fmtPct(pnlPct)}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error("loadXKHoldings:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  STOP LOSS
// ═══════════════════════════════════════════════════════

async function loadStopLoss() {
  try {
    const data = await api("/api/stop-loss/jj");
    // Banner is handled in loadJJHoldings
    return data;
  } catch (e) {
    console.error("loadStopLoss:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  TRADES TAB
// ═══════════════════════════════════════════════════════

async function loadTrades() {
  try {
    // 酱酱 trades
    const jjData = await api("/api/trades/jj");
    const jjTbody = document.getElementById("jj-trades-tbody");
    jjTbody.innerHTML = "";
    if (!jjData.trades || jjData.trades.length === 0) {
      jjTbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#8b8fa3;padding:16px;">暂无交易记录</td></tr>';
    } else {
      jjData.trades.forEach(t => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${t.date}</td>
          <td class="${t.type === 'buy' ? 'trade-buy' : 'trade-sell'}">${t.type === 'buy' ? '买入' : '卖出'}</td>
          <td>${t.code}</td>
          <td>${t.name}</td>
          <td>¥${fmt(t.price)}</td>
          <td>${t.shares}</td>
          <td>¥${fmt(t.amount)}</td>
          <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;">${t.note || ""}</td>
        `;
        jjTbody.appendChild(tr);
      });
    }

    // 小克 trades
    const xkData = await api("/api/trades/xk");
    const xkTbody = document.getElementById("xk-trades-tbody");
    xkTbody.innerHTML = "";
    if (!xkData.trades || xkData.trades.length === 0) {
      xkTbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#8b8fa3;padding:16px;">暂无交易记录</td></tr>';
    } else {
      xkData.trades.forEach(t => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${t.date || ""}</td>
          <td class="${(t.type || '') === 'buy' ? 'trade-buy' : 'trade-sell'}">${t.type === 'buy' ? '买入' : '卖出'}</td>
          <td>${t.code || ""}</td>
          <td>${t.name || ""}</td>
          <td>¥${fmt(t.price)}</td>
          <td>${t.shares || 0}</td>
          <td>¥${fmt(t.amount)}</td>
          <td>${t.note || ""}</td>
        `;
        xkTbody.appendChild(tr);
      });
    }
  } catch (e) {
    console.error("loadTrades:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  REPLAY TAB
// ═══════════════════════════════════════════════════════

async function loadReplay() {
  try {
    const data = await api("/api/signals?date=" + (currentDate || ""));
    const tbody = document.getElementById("replay-tbody");
    tbody.innerHTML = "";

    if (!data.stocks || data.stocks.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#8b8fa3;padding:24px;">该日期无信号数据</td></tr>';
      return;
    }

    data.stocks.forEach(s => {
      const chgClass = s.chg_pct >= 0 ? "chg-up" : "chg-down";
      const peText = s.pe != null && s.pe < 0
        ? `<span class="pe-negative">${s.pe.toFixed(1)}</span>`
        : (s.pe != null ? s.pe.toFixed(1) : "--");

      let labelHtml = "";
      const label = s.label || "";
      if (label === "强候选") labelHtml = '<span class="label-tag strong">强候选</span>';
      else if (label === "观察") labelHtml = '<span class="label-tag watch">观察</span>';
      else if (label === "风控") labelHtml = '<span class="label-tag risk">风控</span>';
      else labelHtml = '<span class="label-tag weak">弱</span>';

      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${s.code}</td>
        <td>${s.name}</td>
        <td>${s.lb}板</td>
        <td>¥${fmt(s.price)}</td>
        <td class="${chgClass}">${fmtPct(s.chg_pct)}</td>
        <td>${peText}</td>
        <td>${s.vol_ratio != null ? s.vol_ratio.toFixed(2) : "--"}</td>
        <td>${s.buy_score}/4</td>
        <td>${labelHtml}</td>
        <td>${(s.rules || []).join(", ")}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    console.error("loadReplay:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  COMPARE CHART
// ═══════════════════════════════════════════════════════

async function loadCompare() {
  try {
    const data = await api("/api/compare");
    const ctx = document.getElementById("compare-chart").getContext("2d");

    const jjDates = data.jiangjiang.map(s => s.date);
    const jjValues = data.jiangjiang.map(s => s.total_value);
    const xkDates = data.xiaoke.map(s => s.date);
    const xkValues = data.xiaoke.map(s => s.total_value);

    // 合并所有日期作为 labels
    const allDates = [...new Set([...jjDates, ...xkDates])].sort();

    // 对齐数据到统一日期轴
    function align(dates, values, allDates) {
      const map = {};
      dates.forEach((d, i) => { map[d] = values[i]; });
      // 向前填充
      let lastVal = 1000000;
      return allDates.map(d => {
        if (map[d] !== undefined) lastVal = map[d];
        return lastVal;
      });
    }

    const jjAligned = align(jjDates, jjValues, allDates);
    const xkAligned = align(xkDates, xkValues, allDates);

    const datasets = [];
    // 小克的线（如果有数据就画，没有也画但可能只画一条）
    if (xkDates.length > 0 || jjDates.length > 0) {
      datasets.push({
        label: "🤖 小克",
        data: xkAligned,
        borderColor: "#4a9eff",
        backgroundColor: "rgba(74,158,255,0.1)",
        fill: false,
        tension: 0.3,
        pointRadius: 2,
      });
    }

    // 酱酱的线（即使空账户也画，从100万开始）
    datasets.push({
      label: "🍑 酱酱",
      data: jjAligned,
      borderColor: "#ff9a76",
      backgroundColor: "rgba(255,154,118,0.1)",
      fill: false,
      tension: 0.3,
      pointRadius: 2,
    });

    if (compareChart) compareChart.destroy();

    compareChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: allDates,
        datasets: datasets,
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            labels: { color: "#e1e4eb", font: { size: 12 } },
          },
          tooltip: {
            callbacks: {
              label: ctx => ctx.dataset.label + ": ¥" + fmt(ctx.raw),
            },
          },
        },
        scales: {
          x: {
            ticks: { color: "#8b8fa3", font: { size: 11 }, maxRotation: 45 },
            grid: { color: "#2a2d38" },
          },
          y: {
            ticks: {
              color: "#8b8fa3",
              font: { size: 11 },
              callback: v => "¥" + (v / 10000).toFixed(0) + "万",
            },
            grid: { color: "#2a2d38" },
          },
        },
      },
    });
  } catch (e) {
    console.error("loadCompare:", e);
  }
}

// ═══════════════════════════════════════════════════════
//  TRADE MODAL
// ═══════════════════════════════════════════════════════

function openBuy(code, name, price) {
  currentTrade = { type: "buy", code, name, price };
  document.getElementById("modal-title").textContent = "🛒 买入";
  document.getElementById("modal-code").textContent = code;
  document.getElementById("modal-name").textContent = name;
  document.getElementById("modal-price").textContent = "¥" + fmt(price);
  document.getElementById("modal-shares").value = 100;
  document.getElementById("modal-amount").textContent = "¥" + fmt(price * 100);
  document.getElementById("btn-confirm-trade").textContent = "确认买入";
  document.getElementById("btn-confirm-trade").className = "btn-confirm";
  document.getElementById("trade-modal").classList.remove("hidden");

  // 实时更新预估金额
  document.getElementById("modal-shares").oninput = function () {
    const s = parseInt(this.value) || 0;
    document.getElementById("modal-amount").textContent = "¥" + fmt(currentTrade.price * s);
  };
}

function openSell(code, name, price, maxShares) {
  currentTrade = { type: "sell", code, name, price, maxShares };
  document.getElementById("modal-title").textContent = "💸 卖出";
  document.getElementById("modal-code").textContent = code;
  document.getElementById("modal-name").textContent = name;
  document.getElementById("modal-price").textContent = "¥" + fmt(price);
  document.getElementById("modal-shares").value = maxShares;
  document.getElementById("modal-amount").textContent = "¥" + fmt(price * maxShares);
  document.getElementById("btn-confirm-trade").textContent = "确认卖出";
  document.getElementById("btn-confirm-trade").className = "btn-confirm";
  document.getElementById("trade-modal").classList.remove("hidden");

  document.getElementById("modal-shares").oninput = function () {
    const s = parseInt(this.value) || 0;
    document.getElementById("modal-amount").textContent = "¥" + fmt(currentTrade.price * s);
  };
}

function closeModal() {
  document.getElementById("trade-modal").classList.add("hidden");
  currentTrade = null;
}

async function confirmTrade() {
  if (!currentTrade) return;
  const shares = parseInt(document.getElementById("modal-shares").value) || 0;
  if (shares <= 0) { alert("请输入有效股数"); return; }
  if (currentTrade.type === "sell" && shares > currentTrade.maxShares) {
    alert(`最多卖出 ${currentTrade.maxShares} 股`); return;
  }

  try {
    const result = await apiPost("/api/trade", {
      type: currentTrade.type,
      code: currentTrade.code,
      name: currentTrade.name,
      price: currentTrade.price,
      shares: shares,
    });

    if (result.ok) {
      closeModal();
      alert(`${currentTrade.type === "buy" ? "买入" : "卖出"}成功！${currentTrade.name} ${shares}股 @ ¥${fmt(currentTrade.price)}`);
      loadStatus();
      loadHoldings();
      loadCompare();
    } else {
      alert("交易失败: " + (result.error || "未知错误"));
    }
  } catch (e) {
    alert("请求失败: " + e.message);
  }
}

// ═══════════════════════════════════════════════════════
//  ALERT BANNER
// ═══════════════════════════════════════════════════════

function showAlert(msg) {
  const banner = document.getElementById("alert-banner");
  document.getElementById("alert-msg").textContent = msg;
  banner.classList.remove("hidden");
}

function dismissAlert() {
  document.getElementById("alert-banner").classList.add("hidden");
}

// ═══════════════════════════════════════════════════════
//  DATA COLLECTION
// ═══════════════════════════════════════════════════════

async function triggerCollect() {
  const btn = document.getElementById("btn-collect");
  btn.disabled = true;
  btn.textContent = "⏳ 采集中...";

  try {
    const result = await apiPost("/api/collect", {});
    if (result.ok) {
      // 轮询直到完成
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        try {
          const status = await api("/api/collect/status");
          if (!status.running) {
            clearInterval(poll);
            btn.disabled = false;
            btn.textContent = "📡 拉取今日数据";
            alert("✅ 数据采集完成！");
            loadDates();
            loadSignals();
            loadL1();
            loadRotation();
            loadStatus();
          }
        } catch (_) {}
        if (attempts > 120) { // 10 分钟超时
          clearInterval(poll);
          btn.disabled = false;
          btn.textContent = "📡 拉取今日数据";
        }
      }, 5000);
    } else {
      btn.disabled = false;
      btn.textContent = "📡 拉取今日数据";
      alert("采集失败: " + (result.reason || result.detail || "未知错误"));
    }
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "📡 拉取今日数据";
    alert("请求失败: " + e.message);
  }
}
