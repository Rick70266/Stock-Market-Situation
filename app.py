# -*- coding: utf-8 -*-
"""
股市儀表板 · Stock Dashboard
- 左側功能列表（sidebar）切換三大主題，右側大畫面呈現
- 電腦版：左側選單 + 右側寬版；手機版：Streamlit 原生摺疊選單（漢堡選單）+ 單欄卡片，另加響應式 CSS
- 資料來源：Yahoo Finance（直接 Chart API 取即時/歷史價 + yfinance 取股利）
  Yahoo 報價為延遲報價（約 15 分鐘），僅供參考，非投資建議。
- 走勢圖：K 線（開高低收）+ MA5/10/20/60/120/240 均線，上方為股價、下方為當日成交量；
  滑鼠移到任一交易日，上方提示框只顯示當日收盤價一個數字，下方成交量提示框同時顯示
  該日日期（yyyymmdd）與成交量；均線數值在圖上方資訊列，隨指到的交易日即時變動。
  台股漲紅跌綠。
- 部署：上傳本資料夾到 GitHub，Streamlit Cloud 選 app.py 即可分享。
"""
import concurrent.futures
import datetime as dt
import math

import json
import uuid

import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components

# ---------------------------------------------------------------- 設定
st.set_page_config(
    page_title="股市儀表板",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
YAHOO_HOSTS = ["https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com"]

# ---------------------------------------------------------------- 響應式 CSS
# 電腦版：卡片並排；手機版（<=768px）：字體略縮、表格可橫向捲動、卡片改單欄。
st.markdown(
    """
<style>
  .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
  [data-testid="stSidebar"] { min-width: 240px; }
  .idx-card { background: #fff; border: 1px solid #e6e8eb; border-radius: 12px;
              padding: 12px 14px; margin-bottom: 10px; }
  .idx-card .name { font-weight: 700; font-size: 15px; }
  .idx-card .sub { color: #6b7280; font-size: 12px; }
  .idx-card .price { font-weight: 800; font-size: 22px; line-height: 1.2; }
  .up { color: #d92d20; font-weight: 700; }   /* 台股習慣：漲紅 */
  .down { color: #067647; font-weight: 700; } /* 跌綠 */
  .flat { color: #667085; font-weight: 700; }
  .note { color: #667085; font-size: 12.5px; }
  .combo { background: #f8fafc; border: 1px solid #e6e8eb; border-radius: 12px; padding: 14px 16px; margin-bottom: 12px; }
  @media (max-width: 768px) {
    .block-container { padding-left: 0.8rem; padding-right: 0.8rem; }
    .idx-card .price { font-size: 19px; }
    h1 { font-size: 1.5rem !important; }
    h2 { font-size: 1.25rem !important; }
    [data-testid="stDataFrame"], [data-testid="stTable"] { overflow-x: auto; }
  }
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------- 資料層
@st.cache_data(ttl=900, show_spinner=False)
def fetch_chart(symbol: str, range_: str = "3mo"):
    """直接打 Yahoo Chart API 取價。回傳 dict；失敗時 error 欄位說明原因。

    注意：亞股最後一個交易日的 close 有時為 None（Yahoo 尚未結算），
    此時以 meta.regularMarketPrice 為最新價、前一個有效收盤為前收。
    """
    url_sym = requests.utils.quote(symbol, safe="")
    last_err = ""
    for host in YAHOO_HOSTS:
        try:
            r = requests.get(
                f"{host}/v8/finance/chart/{url_sym}?range={range_}&interval=1d",
                headers=UA, timeout=12,
            )
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            j = r.json()
            res = (j.get("chart") or {}).get("result")
            if not res:
                last_err = str((j.get("chart") or {}).get("error"))
                continue
            res = res[0]
            meta = res.get("meta") or {}
            ts = res.get("timestamp") or []
            quote = (res.get("indicators") or {}).get("quote", [{}])[0] or {}
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            vols = quote.get("volume") or []
            meta_price = meta.get("regularMarketPrice")
            rows = []
            for i, tt in enumerate(ts):
                o = opens[i] if i < len(opens) else None
                h = highs[i] if i < len(highs) else None
                lo = lows[i] if i < len(lows) else None
                c = closes[i] if i < len(closes) else None
                v = vols[i] if i < len(vols) else None
                if c is None:
                    # 亞股最後一日常出現 close 為 None、但開高低量齊全的情形
                    # （如 00878 在 20261113：開 33.35、高 33.84、低 33.34、收缺、量 65,744,702 股），
                    # 且 meta 價（33.6）正好落在當日高低區間內，就以 meta 價補收盤，K 線才完整；
                    # 非最後一日或無開高低資料的缺值列才捨棄。
                    if i == len(ts) - 1 and o is not None and meta_price is not None:
                        lo_f = float(lo) if lo is not None else float(meta_price)
                        hi_f = float(h) if h is not None else float(meta_price)
                        if lo_f - 0.01 <= float(meta_price) <= hi_f + 0.01:
                            c = float(meta_price)
                    if c is None:
                        continue
                d = dt.datetime.fromtimestamp(tt, tz=dt.timezone.utc).date()
                rows.append({
                    "date": pd.to_datetime(d),
                    "open": float(o) if o is not None else float(c),
                    "high": float(h) if h is not None else float(c),
                    "low": float(lo) if lo is not None else float(c),
                    "close": float(c),
                    "volume": float(v) if v is not None else 0.0,
                })
            cols = ["date", "open", "high", "low", "close", "volume"]
            hist = pd.DataFrame(rows).drop_duplicates("date").sort_values("date") if rows else pd.DataFrame(columns=cols)
            price = meta_price
            if price is None and len(hist):
                price = float(hist["close"].iloc[-1])
            prev = None
            if len(hist) >= 1 and price is not None:
                # meta 價只到小數 2 位，歷史收盤是完整精度：兩者差距 <0.5% 即視為同一天，
                # 以歷史收盤為最新價、前收取倒數第二筆；否則（如亞股最後一日 close 尚未結算）
                # 最新價即 meta 價，前收為最後有效收盤。
                last_close = float(hist["close"].iloc[-1])
                if len(hist) < 2:
                    # 只有一個有效收盤日（如創業板 399006.SZ 在 Yahoo 僅回傳最新一日），無法算漲跌
                    prev = None
                elif abs(last_close - float(price)) / float(price) < 0.005:
                    price = last_close
                    prev = float(hist["close"].iloc[-2])
                else:
                    prev = last_close
            last_ohlc = None
            if len(hist):
                r = hist.iloc[-1]
                last_ohlc = {"date": r["date"], "open": float(r["open"]), "high": float(r["high"]),
                             "low": float(r["low"]), "close": float(r["close"]),
                             "volume": float(r["volume"])}
            return {
                "symbol": symbol, "price": (float(price) if price is not None else None),
                "prev_close": prev, "currency": meta.get("currency") or "",
                "last_date": (hist["date"].iloc[-1].date().isoformat() if len(hist) else ""),
                "meta_date": meta.get("regularMarketTime"),
                "history": hist, "last_ohlc": last_ohlc, "error": "",
            }
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:120]
            continue
    return {"symbol": symbol, "price": None, "prev_close": None, "currency": "",
            "last_date": "", "meta_date": None,
            "history": pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]),
            "last_ohlc": None, "error": last_err or "無法取得報價"}


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_dividends(symbol: str):
    """用 yfinance 取股利（除息日、每股配息）。回傳 DataFrame[date, amount]，失敗為空表。"""
    try:
        import yfinance as yf
        d = yf.Ticker(symbol).dividends
        if d is None or len(d) == 0:
            return pd.DataFrame(columns=["date", "amount"])
        df = d.reset_index()
        df.columns = ["date", "amount"]
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df["amount"] = df["amount"].astype(float)
        return df.sort_values("date").reset_index(drop=True)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(columns=["date", "amount"])


def fetch_many(symbols, range_="3mo"):
    """平行抓多檔報價，避免一檔卡住拖慢整頁。"""
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_chart, s, range_): s for s in symbols}
        for f in concurrent.futures.as_completed(futs):
            try:
                out[futs[f]] = f.result()
            except Exception as e:  # noqa: BLE001
                out[futs[f]] = {"symbol": futs[f], "price": None, "prev_close": None,
                                "currency": "", "last_date": "", "last_ohlc": None,
                                "history": pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]),
                                "error": str(e)[:80]}
    return out


def chg_pct(q):
    if q.get("price") is None or q.get("prev_close") in (None, 0):
        return None
    return (q["price"] - q["prev_close"]) / q["prev_close"] * 100


def fmt_price(v, currency=""):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:,.2f}"


def pct_html(p):
    if p is None:
        return '<span class="flat">—</span>'
    cls = "up" if p > 0 else ("down" if p < 0 else "flat")
    arrow = "▲" if p > 0 else ("▼" if p < 0 else "—")
    return f'<span class="{cls}">{arrow} {abs(p):.2f}%</span>'


def price_at(hist: pd.DataFrame, day: pd.Timestamp):
    """取某日（含）之前最近一個交易日的收盤價，用來算當期殖利率的分母。"""
    if hist is None or len(hist) == 0:
        return None
    h = hist[hist["date"] <= day]
    if len(h) == 0:
        return None
    return float(h["close"].iloc[-1])

# ---------------------------------------------------------------- K 線圖（照 Yahoo 股市圖：上方 K 線＋均線、下方成交量）
# 互動方式（依使用者要求）：
#   * 滑鼠移到任一交易日，不再用圖內的提示框顯示數值（提示框會擋住 K 線）：
#     當日收盤價貼在右側股價 y 軸上、成交量貼在右側成交量 y 軸上、
#     交易日期（yyyymmdd）貼在成交量 x 軸正下方，三個數值都跟著懸停的交易日移動，
#     不固定在最右邊。圖內提示框已設為透明隱藏，只留十字對齊線；且提示框旁指向
#     資料點的小箭頭（深灰色，hoverlabel 透明設定蓋不掉）另用 CSS 把 .hovertext
#     整組藏起來，交叉處不再出現箭頭。
#     兩區共用同一 x 軸、hovermode 設為 "x"，所以移到任一區、兩區的數值會同時出現，
#     不用各別移動滑鼠去看。
#   * K 線為實心（漲實心紅、跌實心綠）；x 軸用 rangebreaks 移除非交易日
#     （週末與區間內缺失的平日），交易日之間不留空白間隙。
#   * 十字對齊線：滑鼠移到任一交易日，垂直線用 Plotly 的 x 軸 spike（深黑實線、
#     spikemode "across"、spikesnap "data"），貫穿兩區並吸附到交易日；水平線不用
#     Plotly 的 y 軸 spike（實測它會落在開盤價附近、不是收盤價），改用兩個絕對
#     定位的 div 自己畫：股價區的線對齊當日收盤價、成交量區的線對齊當日成交量，
#     所以交叉點就是 (交易日, 收盤價)。
#   * MA5/MA10/MA20/MA60/MA120/MA240 的數值不在提示框裡，改放在圖上方的資訊列，
#     隨滑鼠指到的交易日即時變動（移開後回到最新一日）。
# Streamlit 內建的 st.plotly_chart 做不到「懸停時更新圖外的文字」，所以這裡用
# st.components.v1.html 包一個 Plotly.js 圖表，在 iframe 內用 plotly_hover 事件更新資訊列。
MA_DEFS = [  # (天數, 顏色) — 顏色對照截圖：MA5 藍、MA10 紫、MA20 橘、MA60 綠、MA120 棕、MA240 灰
    (5, "#1f77b4"), (10, "#9467bd"), (20, "#ff7f0e"),
    (60, "#2ca02c"), (120, "#8c564b"), (240, "#7f7f7f"),
]
UP_COLOR = "#e53935"    # 漲紅（台股習慣）
DOWN_COLOR = "#43a047"  # 跌綠

_CANDLE_HTML_TEMPLATE = """
<style>
.axis-label-__UID__{position:absolute; background:#111111; color:#ffffff; font:12px/1.4 sans-serif; padding:2px 6px; border-radius:2px; pointer-events:none; white-space:nowrap; z-index:5;}
.hline-__UID__{position:absolute; height:1px; background:#000000; pointer-events:none; z-index:4; left:0;}
#wrap-__UID__ .hovertext{display:none !important;}
</style>
<div id="wrap-__UID__" style="position:relative;">
<div id="hdr-__UID__" style="font-size:13px;line-height:1.55;color:#1f2937;font-family:sans-serif;"></div>
<div id="chart-__UID__" style="width:100%;height:__HEIGHT__px;"></div>
<div id="plabel-__UID__" class="axis-label-__UID__" style="right:0;"></div>
<div id="vlabel-__UID__" class="axis-label-__UID__" style="right:0;"></div>
<div id="dlabel-__UID__" class="axis-label-__UID__" style="left:0;"></div>
<div id="hline-__UID__" class="hline-__UID__"></div>
<div id="hlinev-__UID__" class="hline-__UID__"></div>
</div>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<script>
(function(){
  var D = __PAYLOAD__;
  var unit = "__UNIT__";
  var hasVol = __HAS_VOL__;
  var wrap = document.getElementById("wrap-__UID__");
  var hdr = document.getElementById("hdr-__UID__");
  var chartDiv = document.getElementById("chart-__UID__");
  var plabel = document.getElementById("plabel-__UID__");
  var vlabel = document.getElementById("vlabel-__UID__");
  var dlabel = document.getElementById("dlabel-__UID__");
  var hline = document.getElementById("hline-__UID__");
  var hlinev = document.getElementById("hlinev-__UID__");
  var cur = D.dates.length - 1;
  if (!hasVol) { vlabel.style.display = "none"; hlinev.style.display = "none"; }
  function fmt(n){ return (n===null||n===undefined||isNaN(n)) ? "—" : n.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2}); }
  function show(i){
    var chg = D.chg[i];
    var chgStr = (chg===null||chg===undefined) ? "" : " 漲跌"+(chg>0?"+":"")+chg.toFixed(2);
    var ma = ["MA5","MA10","MA20","MA60","MA120","MA240"].map(function(k){ return k+" "+fmt(D.ma[k][i]); }).join("  ");
    hdr.innerHTML = "<b>"+D.ymd[i]+"</b> 開"+fmt(D.open[i])+" 高"+fmt(D.high[i])+" 低"+fmt(D.low[i])+" 收"+fmt(D.close[i])+" 量"+D.vol[i].toLocaleString("en-US")+unit+chgStr+"<br>"+ma;
  }
  show(D.dates.length-1);
  var MA_COLORS = {"MA5":"#1f77b4","MA10":"#9467bd","MA20":"#ff7f0e","MA60":"#2ca02c","MA120":"#8c564b","MA240":"#7f7f7f"};
  var traces = [{type:"candlestick", x:D.dates, open:D.open, high:D.high, low:D.low, close:D.close,
    increasing:{line:{color:"__UP__"}, fillcolor:"__UP__"}, decreasing:{line:{color:"__DOWN__"}, fillcolor:"__DOWN__"},
    name:"K線", hovertemplate:"%{close:.2f}<extra></extra>"}];
  Object.keys(MA_COLORS).forEach(function(k){
    traces.push({type:"scatter", mode:"lines", x:D.dates, y:D.ma[k],
      line:{color:MA_COLORS[k], width:1.2}, name:k, hoverinfo:"skip"});
  });
  if (hasVol) {
    traces.push({type:"bar", x:D.dates, y:D.vol, yaxis:"y2",
      marker:{color: D.close.map(function(c,i){ return c>=D.open[i] ? "__UP__" : "__DOWN__"; })},
      name:"成交量（"+unit+"）",
      hovertemplate:"日期 %{x|%Y%m%d}<br>成交量 %{y:,.0f}"+unit+"<extra></extra>"});
  }
  var layout = {hovermode:"x", showlegend:true, legend:{orientation:"h", y:1.06, x:0},
    hoverlabel:{bgcolor:"rgba(0,0,0,0)", bordercolor:"rgba(0,0,0,0)", font:{color:"rgba(0,0,0,0)", size:1}},
    margin:{l:0, r:100, t:10, b:30},
    xaxis:{tickformat:"%Y%m%d", rangeslider:{visible:false}, showspikes:true, spikemode:"across", spikesnap:"data", spikethickness:1, spikecolor:"#000000", spikedash:"solid", rangebreaks:(function(){ var rb=[{pattern:"day of week", bounds:["sat","mon"]}]; if (D.breaks && D.breaks.length) { rb.push({values:D.breaks}); } return rb; })()},
    yaxis:{domain:[0.28,1], side:"right", title:{text:"股價", standoff:8}, showspikes:false}};
  if (hasVol) { layout.yaxis2 = {domain:[0,0.20], side:"right", title:{text:"成交量（"+unit+"）", standoff:8}, showspikes:false}; }
  else { layout.yaxis.domain = [0,1]; }
  var gd = chartDiv;
  // 軸上數值標籤：懸停的數值不進圖內提示框，改貼在軸上——股價貼右側股價 y 軸、
  // 成交量貼右側成交量 y 軸、交易日期（yyyymmdd）貼在成交量 x 軸正下方，
  // 三個標籤都跟著懸停的交易日移動，不會擋住 K 線。位置用 Plotly 軸的
  // l2p / d2p 換算成像素後以絕對定位的 div 疊在圖上，只改 DOM、不觸發重繪，
  // 所以對齊線與懸停狀態不會閃爍。
  // 注意：l2p() 回傳的是「該軸 domain 頂部起算」的相對像素，不是整張圖的像素
  // （實測：yaxis2.l2p(62900 張)=3.11，是成交量區頂部往下 3px），所以要再加上
  // 該軸 domain 頂部在圖上的像素（axisTop），否則成交量標籤會跑到上方股價區。
  // x 軸的 domain 是整寬、左邊距為 0，d2p() 可直接當圖上 x 座標用。
  // 水平對齊線不用 Plotly 的 y 軸 spike：實測 Plotly 的 K 線水平 spike 會落在
  // 開盤價附近（與收盤價差 9~19px，落在實心中間偏上），不是收盤價；成交量柱
  // 的水平 spike 也一樣不是成交量值。所以水平線改用兩個絕對定位的 div 自己畫：
  // 股價區的線對齊當日收盤價、成交量區的線對齊當日成交量，位置與軸上標籤用
  // 同一組換算，交叉點就是 (交易日, 收盤價)。垂直線仍用 Plotly 的 x 軸 spike
  //（深黑實線、across，正確吸附到交易日）。
  function axisTop(ax){
    // 用 Plotly 自己的 _offset：它是該軸 domain 頂部在圖上的實際像素位置，
    // 已包含 margin autoexpand 的影響（例如圖例在圖上方時，頂邊距會被撐大
    // 約 9px，_fullLayout.margin.t 仍顯示原本的 10，不會跟著變）。
    // 若用 margin.t 手算 domain 頂部，會少算這段展開量，線與標籤就偏高約 9px。
    if (ax._offset !== undefined && ax._offset !== null) return ax._offset;
    var L = gd._fullLayout;
    return L.margin.t + (1 - ax.domain[1]) * (L.height - L.margin.t - L.margin.b);
  }
  function place(i){
    cur = i;
    var L = gd._fullLayout; if(!L || !L.yaxis || !L.yaxis.l2p) return;
    var hdrH = hdr.offsetHeight;
    plabel.textContent = fmt(D.close[i]);
    plabel.style.top = (hdrH + axisTop(L.yaxis) + L.yaxis.l2p(D.close[i]) - plabel.offsetHeight/2) + "px";
    var plotW = (L.xaxis && L.xaxis._length !== undefined) ? L.xaxis._length : (chartDiv.offsetWidth - L.margin.r);
    hline.style.width = plotW + "px";
    hline.style.top = (hdrH + axisTop(L.yaxis) + L.yaxis.l2p(D.close[i]) - 0.5) + "px";
    if (hasVol && L.yaxis2) {
      vlabel.textContent = Math.round(D.vol[i]).toLocaleString("en-US");
      vlabel.style.top = (hdrH + axisTop(L.yaxis2) + L.yaxis2.l2p(D.vol[i]) - vlabel.offsetHeight/2) + "px";
      hlinev.style.width = plotW + "px";
      hlinev.style.top = (hdrH + axisTop(L.yaxis2) + L.yaxis2.l2p(D.vol[i]) - 0.5) + "px";
    }
    dlabel.textContent = D.ymd[i];
    var x = L.xaxis.d2p ? L.xaxis.d2p(D.dates[i]) : NaN;
    if (isFinite(x)) {
      var w = dlabel.offsetWidth;
      var left = x - w/2;
      if (left < 0) left = 0;
      if (left + w > wrap.offsetWidth) left = wrap.offsetWidth - w;
      dlabel.style.left = left + "px";
    }
    dlabel.style.top = (hdrH + plotBottom(L) + 4) + "px";
  }
  function plotBottom(L){
    // 圖表底部（x 軸線）在圖上的實際像素位置：用最下方那個 y 軸的
    // _offset + _length，底邊距若被 autoexpand 撐大也會跟著算進去。
    var botAx = (hasVol && L.yaxis2) ? L.yaxis2 : L.yaxis;
    if (botAx._offset !== undefined && botAx._length !== undefined) return botAx._offset + botAx._length;
    return chartDiv.offsetHeight - L.margin.b;
  }
  Plotly.newPlot(gd, traces, layout, {displayModeBar:false, responsive:true}).then(function(){ place(D.dates.length-1); });
  gd.on("plotly_hover", function(ev){ if (ev.points && ev.points.length) { var i = ev.points[0].pointIndex; show(i); place(i); } });
  gd.on("plotly_unhover", function(){ show(D.dates.length-1); place(D.dates.length-1); });
  window.addEventListener("resize", function(){ place(cur); });
})();
</script>
"""


def make_candle_html(hist: pd.DataFrame, height: int = 320, symbol: str = "", currency: str = ""):
    """回傳 K 線圖的完整 HTML（供 st.components.v1.html 顯示），無資料回傳 None。

    上方 K 線＋MA 均線、下方成交量；滑鼠懸停時數值不進圖內提示框，
    改貼在右側 y 軸（收盤價／成交量）與 x 軸正下方（日期 yyyymmdd），跟著懸停日移動；
    均線數值在圖上方資訊列隨懸停日變動。
    成交量單位：台股（.TW）以「張」（股數/1000）呈現、其餘以原始股數呈現；
    指數若 Yahoo 無成交量資料，就只畫 K 線、不畫量圖。
    """
    if hist is None or len(hist) == 0 or "close" not in hist:
        return None
    df = hist.copy().sort_values("date").reset_index(drop=True)
    for w, _ in MA_DEFS:
        df[f"MA{w}"] = df["close"].rolling(window=w, min_periods=1).mean()
    is_tw = symbol.endswith(".TW")
    vol = df["volume"].fillna(0) if "volume" in df else pd.Series([0.0] * len(df))
    has_vol = bool(vol.sum() > 0)
    vol_y = (vol / 1000.0) if is_tw else vol
    vol_unit = "張" if is_tw else "股"
    prev = df["close"].shift(1)
    chg = df["close"] - prev
    data = {
        "dates": [d.strftime("%Y-%m-%d") for d in df["date"]],
        "ymd": [d.strftime("%Y%m%d") for d in df["date"]],
        "open": [round(float(x), 2) for x in df["open"]],
        "high": [round(float(x), 2) for x in df["high"]],
        "low": [round(float(x), 2) for x in df["low"]],
        "close": [round(float(x), 2) for x in df["close"]],
        "vol": [round(float(x), 1) for x in vol_y],
        "chg": [None if pd.isna(x) else round(float(x), 2) for x in chg],
        # 區間內缺失的平日（國定假日等）：x 軸用 rangebreaks 把這些日期也拿掉，圖上不留空白
        "breaks": [d.strftime("%Y-%m-%d") for d in pd.date_range(df["date"].min(), df["date"].max(), freq="B")
                   if d not in set(df["date"])],
        "ma": {f"MA{w}": [round(float(x), 2) for x in df[f"MA{w}"]] for w, _ in MA_DEFS},
    }
    uid = uuid.uuid4().hex[:8]
    html = (
        _CANDLE_HTML_TEMPLATE
        .replace("__UID__", uid)
        .replace("__HEIGHT__", str(height))
        .replace("__PAYLOAD__", json.dumps(data, ensure_ascii=False))
        .replace("__UNIT__", vol_unit)
        .replace("__HAS_VOL__", "true" if has_vol else "false")
        .replace("__UP__", UP_COLOR)
        .replace("__DOWN__", DOWN_COLOR)
    )
    return html

# ---------------------------------------------------------------- 靜態清單
GLOBAL_INDICES = [
    ("美國", "道瓊工業平均指數", "DJIA", "^DJI", "USD"),
    ("美國", "標普 500 指數", "S&P 500", "^GSPC", "USD"),
    ("美國", "那斯達克綜合指數", "NASDAQ", "^IXIC", "USD"),
    ("美國", "費城半導體指數", "SOX", "^SOX", "USD"),
    ("台灣", "台灣加權指數", "TAIEX", "^TWII", "TWD"),
    ("日本", "日經 225 指數", "Nikkei 225", "^N225", "JPY"),
    ("日本", "東證指數", "TOPIX", "1306.T", "JPY"),  # Yahoo 已無 ^TOPX，以追蹤 TOPIX 的 1306.T ETF 代理，頁面會註明
    ("中國大陸", "上證綜合指數", "SSE Composite", "000001.SS", "CNY"),
    ("中國大陸", "深圳成份指數", "SZSE Component", "399001.SZ", "CNY"),
    ("中國大陸", "創業板指數", "ChiNext", "399006.SZ", "CNY"),
    ("香港", "香港恆生指數", "Hang Seng", "^HSI", "HKD"),
    ("韓國", "韓國綜合股價指數", "KOSPI", "^KS11", "KRW"),
    ("英國", "富時 100 指數", "FTSE 100", "^FTSE", "GBP"),
    ("德國", "德國 DAX 指數", "DAX", "^GDAXI", "EUR"),
]

ETF_CATALOG = [
    # code, name, category, note
    ("0050", "元大台灣50", "台股市值型 / 核心大盤", "追蹤台灣50指數，半年配（1、7月）"),
    ("006208", "富邦台50", "台股市值型 / 核心大盤", "追蹤台灣50指數，年配為主"),
    ("0051", "元大中型100", "台股市值型 / 核心大盤", "追蹤中型100指數，年配為主"),
    ("0056", "元大高股息", "台股高股息型", "季配：1、4、7、10月"),
    ("00878", "國泰永續高股息", "台股高股息型", "季配：2、5、8、11月"),
    ("00919", "群益台灣精選高息", "台股高股息型", "季配：3、6、9、12月"),
    ("00713", "元大台灣高息低波", "台股高股息型", "季配：3、6、9、12月"),
    ("00929", "復華台灣科技優息", "台股高股息型", "月配（每月），科技優息"),
    ("00940", "元大台灣價值高息", "台股高股息型", "月配（每月）"),
    ("0052", "富邦科技", "科技與產業型", "科技類股，年配為主"),
    ("00830", "國泰費城半導體", "科技與產業型", "追蹤費城半導體，年配為主"),
    ("00757", "統一FANG+", "科技與產業型", "美股 FANG+，目前無配息"),
    ("00881", "國泰台灣5G+", "科技與產業型", "5G 通訊，年配為主"),
    ("00631L", "元大台灣50正2", "槓桿與反向型", "2倍槓桿，無配息，不宜長期持有"),
    ("00632R", "元大台灣50反1", "槓桿與反向型", "反向1倍，無配息，避險/短線工具"),
    ("00675L", "富邦臺灣加權正2", "槓桿與反向型", "2倍槓桿，無配息，不宜長期持有"),
]
ETF_BY_CODE = {c[0]: c for c in ETF_CATALOG}
CATEGORIES = ["全部", "台股市值型 / 核心大盤", "台股高股息型", "科技與產業型", "槓桿與反向型"]

# 月配組合：月份以 2025-2026 實際除息月份驗證為準（見下方備註）
COMBOS = [
    {
        "name": "組合 A · 經典季配互補（穩健型）",
        "members": ["0056", "00878", "00919"],
        "idea": "三檔季配 ETF 的配息月剛好錯開：0056 在 1、4、7、10 月；00878 在 2、5、8、11 月；00919 在 3、6、9、12 月，三檔合起來 12 個月每月都有人發。",
        "risk": "三檔都是高股息，產業與選股邏輯重疊度高，成長性低於大盤型；配息金額每年會變，不是保證收益。",
    },
    {
        "name": "組合 B · 月配現金流（每月兩檔）",
        "members": ["00929", "00940"],
        "idea": "00929 與 00940 都是月配，2025-2026 年每月都有除息，兩檔一起每月領兩筆，現金流最平穩。想再補高息可加一檔 0056（季配）。",
        "risk": "月配金額小、每期約 0.03-0.38 元，且 00929 在 2026 年 2 月曾無配息、3 月配兩次，月配不等於每月保證配；兩檔都偏科技/高息，波動不小。",
    },
    {
        "name": "組合 C · 高股息 + 市值成長（平衡型）",
        "members": ["0056", "00878", "00919", "0050"],
        "idea": "在組合 A 的 12 個月全覆蓋基礎上，加 0050（半年配，1、7 月）補進市值型成長，讓組合不只有高股息，也有大盤成長；1、7 月會多領一筆。",
        "risk": "0050 配息低、主要報酬來自價差，組合整體殖利率會被拉低；仍須留意高股息三檔重疊的問題。",
    },
]

# ---------------------------------------------------------------- 側邊欄（左側功能列表）
with st.sidebar:
    st.title("📈 股市儀表板")
    st.caption("全球大盤 · 台灣 ETF · 月月領股息")
    page = st.radio(
        "功能列表",
        ["🌍 全球股市動態", "🇹🇼 台灣 ETF", "🗓️ 月月領股息"],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("資料來源：Yahoo Finance（延遲報價，約 15 分鐘）")
    st.caption(f"現在時間：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    # 版本標記：用來一眼分辨 Streamlit Cloud 跑的是哪一版（舊版沒有這行）。
    # 若部署後看不到這行，代表推上去的不是這一包、或 Cloud 還沒重跑。
    st.caption("版本 20261114-4 · 月月領股息：新增每月投資金額設定")
    if st.button("🔄 重新整理報價", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.markdown(
        '<p class="note">⚠️ 本頁僅供研究參考，非投資建議。'
        "配息金額與殖利率以歷史資料計算，未來配息會變動，"
        "槓桿/反向 ETF 有耗損，不宜長期持有。</p>",
        unsafe_allow_html=True,
    )

# ================================================================ 1. 全球股市動態
def page_global():
    st.header("🌍 全球股市動態")
    st.caption("各國大盤即時狀況（Yahoo Finance 延遲報價）。漲跌以最新價 vs 前一交易日收盤計算。")
    rng_label = st.selectbox("走勢區間", ["1個月", "3個月", "6個月", "1年"], index=2, key="g_rng")
    rng_map = {"1個月": "1mo", "3個月": "3mo", "6個月": "6mo", "1年": "1y"}
    rng = rng_map[rng_label]

    with st.spinner("正在抓取全球大盤報價…"):
        quotes = fetch_many([g[3] for g in GLOBAL_INDICES], range_=rng)

    # 依地區分組呈現
    regions = []
    for g in GLOBAL_INDICES:
        if g[0] not in regions:
            regions.append(g[0])
    # 同一個水平只呈現一個指數（全寬），避免兩欄並排把交易日擠在一起；
    # ETF 個股頁本來就是一次只看一檔，同樣是全寬，不用改。
    for region in regions:
        st.subheader(region)
        items = [g for g in GLOBAL_INDICES if g[0] == region]
        for reg, name, short, sym, curr in items:
            q = quotes.get(sym, {})
            p = chg_pct(q)
            if q.get("price") is None:
                st.markdown(
                    f'<div class="idx-card"><div class="name">{name} <span class="sub">({short} · {sym})</span></div>'
                    f'<div class="note">暫無報價（{q.get("error") or "Yahoo 未回傳"}），請稍後按左側「重新整理報價」。'
                    "</div></div>",
                    unsafe_allow_html=True,
                )
                continue
            delta = f"{q['price'] - q['prev_close']:+,.2f}" if q.get("prev_close") else "—"
            st.markdown(
                f'<div class="idx-card"><div class="name">{name} <span class="sub">({short} · {sym})</span></div>'
                f'<div class="price">{fmt_price(q["price"])} <span style="font-size:13px;color:#667085">{curr}</span></div>'
                f'<div>{pct_html(p)} &nbsp; <span class="note">{delta} 點 · 截至 {q.get("last_date")}</span></div></div>',
                unsafe_allow_html=True,
            )
            hist = q.get("history")
            if hist is not None and len(hist) >= 2:
                html = make_candle_html(hist, height=360, symbol=sym, currency=curr)
                if html is not None:
                    components.html(html, height=420)
                else:
                    st.warning("暫無走勢資料，請稍後重新整理。")

    st.divider()
    st.subheader("漲跌總表")
    rows = []
    for reg, name, short, sym, curr in GLOBAL_INDICES:
        q = quotes.get(sym, {})
        p = chg_pct(q)
        rows.append({
            "地區": reg, "指數": name, "代碼": short,
            "最新價": fmt_price(q.get("price")),
            "漲跌幅": (f"{p:+.2f}%" if p is not None else "—"),
            "漲跌點數": (f"{q['price'] - q['prev_close']:+,.2f}" if q.get("price") is not None and q.get("prev_close") else "—"),
            "幣別": curr, "截至": q.get("last_date") or "—",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, width="stretch", hide_index=True)
    st.markdown(
        '<p class="note">註：東證指數 TOPIX 在 Yahoo 已無 ^TOPX 指數代碼，'
        "本頁以追蹤 TOPIX 的 1306.T ETF 報價代理呈現；"
        "上證綜合指數使用 000001.SS、深圳成份使用 399001.SZ、創業板使用 399006.SZ。</p>",
        unsafe_allow_html=True,
    )


# ================================================================ 2. 台灣 ETF
def dividend_table(code: str, hist2y: pd.DataFrame, current_price):
    """回傳 (2025, 2026) 兩年股利表：除息日、每股配息、除息日收盤價、當期殖利率、年合計與年殖利率。

    當期殖利率 = 每股配息 / 除息日收盤價；年殖利率 = 該年配息合計 / 該年最後收盤價（2025）
    或 / 現價（2026，未完成年度，僅為年初至今殖利率）。
    未發放的期數以空白呈現；下一期以「最近一次 + 頻率推估」呈現，並標明為推估非公告。
    """
    divs = fetch_dividends(f"{code}.TW")
    out = {}
    for year in (2025, 2026):
        sub = divs[(divs["date"].dt.year == year)] if len(divs) else divs
        rows = []
        for _, r in sub.iterrows():
            d = pd.to_datetime(r["date"])
            px_at = price_at(hist2y, d)
            y = (float(r["amount"]) / px_at * 100) if px_at else None
            rows.append({
                "除息日": d.strftime("%Y-%m-%d"),
                "每股配息 (元)": round(float(r["amount"]), 4),
                "除息日收盤價 (元)": (round(px_at, 2) if px_at else None),
                "當期殖利率": (f"{y:.2f}%" if y is not None else "—"),
            })
        total = float(sub["amount"].sum()) if len(sub) else 0.0
        if year == 2025:
            denom = price_at(hist2y, pd.Timestamp("2025-12-31"))
            denom_label = "2025 年底收盤價"
        else:
            denom = current_price
            denom_label = "現價（年初至今）"
        annual_y = (total / denom * 100) if denom and total else None
        out[year] = {
            "rows": rows, "total": total, "annual_yield": annual_y,
            "denom_label": denom_label, "denom": denom, "count": len(rows),
        }
    return out, divs


def next_dividend_hint(code: str, divs: pd.DataFrame):
    """推估下次配息：依最近兩次除息間隔推算；月配約 30 天、季配約 90 天、半年配約 180 天。"""
    if divs is None or len(divs) == 0:
        return "無配息紀錄（此檔目前無配息，或尚未有股利資料）。"
    last = pd.to_datetime(divs["date"].iloc[-1])
    if len(divs) >= 2:
        gaps = divs["date"].diff().dt.days.dropna()
        gap = float(gaps.median()) if len(gaps) else 90
    else:
        gap = 90
    if gap <= 40:
        freq = "月配"
    elif gap <= 110:
        freq = "季配"
    elif gap <= 200:
        freq = "半年配"
    else:
        freq = "年配"
    nxt = last + pd.Timedelta(days=int(round(gap)))
    return (f"最近一次除息：{last.strftime('%Y-%m-%d')}；頻率：{freq}（最近間隔中位數約 {int(round(gap))} 天）；"
            f"依此推估下次約 {nxt.strftime('%Y-%m-%d')}。⚠️ 此為依歷史間隔推估，非官方公告，"
            "實際公告請以投信官網為準。")


def page_etf():
    st.header("🇹🇼 台灣 ETF")
    st.caption("即時股價、近期走勢圖、前一年（2025）至今年（2026）各期股利、當期殖利率與全年度殖利率。未發放的期數留白。")

    cat = st.selectbox("分類", CATEGORIES, index=0, key="etf_cat")
    codes = [c[0] for c in ETF_CATALOG if cat == "全部" or c[2] == cat]

    with st.spinner("正在抓取 ETF 即時股價…"):
        quotes = fetch_many([f"{c}.TW" for c in codes], range_="3mo")

    rows = []
    for code in codes:
        name, cat_name, note = ETF_BY_CODE[code][1], ETF_BY_CODE[code][2], ETF_BY_CODE[code][3]
        q = quotes.get(f"{code}.TW", {})
        p = chg_pct(q)
        hist = q.get("history")
        m1 = m3 = None
        if hist is not None and len(hist) >= 2:
            last = float(hist["close"].iloc[-1])
            h1 = hist[hist["date"] >= hist["date"].max() - pd.Timedelta(days=30)]
            h3 = hist[hist["date"] >= hist["date"].max() - pd.Timedelta(days=90)]
            if len(h1) >= 2:
                m1 = (last / float(h1["close"].iloc[0]) - 1) * 100
            if len(h3) >= 2:
                m3 = (last / float(h3["close"].iloc[0]) - 1) * 100
        rows.append({
            "代碼": code, "名稱": name, "分類": cat_name,
            "即時價": fmt_price(q.get("price")),
            "漲跌幅": (f"{p:+.2f}%" if p is not None else "—"),
            "近1月": (f"{m1:+.2f}%" if m1 is not None else "—"),
            "近3月": (f"{m3:+.2f}%" if m3 is not None else "—"),
            "備註": note,
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    st.divider()
    st.subheader("個股詳細：走勢圖 + 股利明細")
    sel_label = st.selectbox(
        "選擇 ETF",
        [f"{c} {ETF_BY_CODE[c][1]}" for c in codes],
        key="etf_sel",
    )
    code = sel_label.split(" ")[0]
    name = ETF_BY_CODE[code][1]
    rng_label = st.selectbox("走勢區間", ["3個月", "6個月", "1年"], index=1, key="etf_rng")
    rng = {"3個月": "3mo", "6個月": "6mo", "1年": "1y"}[rng_label]

    with st.spinner(f"正在抓取 {code} {name} 的走勢與股利…"):
        q = fetch_chart(f"{code}.TW", range_=rng)
        hist2y = fetch_chart(f"{code}.TW", range_="2y").get("history")
        tables, divs = dividend_table(code, hist2y, q.get("price"))

    p = chg_pct(q)
    c1, c2, c3 = st.columns(3)
    c1.metric("即時價", fmt_price(q.get("price")), (f"{p:+.2f}%" if p is not None else None))
    c2.metric("截至", q.get("last_date") or "—")
    c3.metric("分類", ETF_BY_CODE[code][2])
    st.caption(ETF_BY_CODE[code][3])

    st.markdown(f"**{code} {name} · {rng_label}K 線走勢**（滑鼠移到任一交易日：上方只顯示當日收盤價，下方同時顯示日期與成交量，均線值在圖上方隨指到的日期變動）")
    hist = q.get("history")
    if hist is not None and len(hist) >= 2:
        html = make_candle_html(hist, height=480, symbol=f"{code}.TW", currency="TWD")
        if html is not None:
            components.html(html, height=540)
        else:
            st.warning("暫無走勢資料，請稍後重新整理。")
    else:
        st.warning("暫無走勢資料，請稍後重新整理。")

    for year in (2025, 2026):
        t = tables[year]
        st.markdown(f"**{year} 年股利**")
        if t["count"] == 0:
            st.info(f"{year} 年尚無配息紀錄（未發放的期數以空白呈現；若該檔本來就不配息，如 00757、槓桿/反向型，全年即為空白）。")
        else:
            st.dataframe(pd.DataFrame(t["rows"]), width="stretch", hide_index=True)
            ay = f"{t['annual_yield']:.2f}%" if t["annual_yield"] is not None else "—"
            denom = f"{t['denom']:.2f} 元" if t["denom"] else "—"
            st.markdown(
                f"合計配息 **{t['total']:.4f} 元/股**（{t['count']} 期） · "
                f"全年度殖利率 **{ay}**（分母：{t['denom_label']} {denom}）"
                + (" · ⚠️ 2026 為年初至今，年度尚未結束。" if year == 2026 else "")
            )
    st.info("📅 下次配息：" + next_dividend_hint(code, divs))
    st.markdown(
        '<p class="note">殖利率算法：當期殖利率 = 每股配息 ÷ 除息日收盤價；'
        "年殖利率 = 該年配息合計 ÷ 分母（2025 用年底收盤價，2026 用現價）。"
        "除息日收盤價取自 Yahoo 歷史價（除息日當天或之前最近交易日）。"
        "槓桿/反向型（00631L、00632R、00675L）與 00757 目前無配息，表格為空白屬正常。</p>",
        unsafe_allow_html=True,
    )


# ================================================================ 3. 月月領股息
def page_monthly():
    st.header("🗓️ 月月領股息")
    st.caption("依台灣 ETF 實際配息月份組合，目標是 12 個月每月都有股利可領。以下月份以 2025-2026 年 Yahoo 股利資料驗證，金額為歷史推估、非保證。")

    # 先抓三個組合共 5 檔的股利，算出每月覆蓋與試算
    need = sorted({c for combo in COMBOS for c in combo["members"]})
    with st.spinner("正在抓取組合 ETF 的配息月份…"):
        div_map = {c: fetch_dividends(f"{c}.TW") for c in need}
        price_map = fetch_many([f"{c}.TW" for c in need], range_="5d")

    # 每月投資金額設定：每檔一個下單選單（1,000～20,000 元，間隔 1,000 元）
    st.subheader("每月投資金額")
    st.caption("設定每檔 ETF 每月投入的金額（定期定額概念），下方預估收益會跟著變動。股數以「每月投入 × 12 個月 ÷ 現價」估算（假設股價一年不變、投資滿一年後的持有股數）。")
    inv_order = [c for c in ["0056", "00878", "00919", "00929", "00940", "0050"] if c in need]
    amt_opts = list(range(1000, 20001, 1000))
    inv_map = {}
    for row in range(0, len(inv_order), 3):
        cols = st.columns(3)
        for j, c in enumerate(inv_order[row:row + 3]):
            with cols[j]:
                inv_map[c] = st.selectbox(
                    f"{c} {ETF_BY_CODE[c][1]}",
                    options=amt_opts,
                    index=amt_opts.index(5000),
                    format_func=lambda x: f"NT$ {x:,}",
                    key=f"inv_{c}",
                )
    # 一年累積股數（每月投入 × 12 ÷ 現價；現價抓不到時為 None）
    shares_map = {}
    for c in need:
        price = price_map.get(f"{c}.TW", {}).get("price")
        shares_map[c] = (12 * inv_map[c] / price) if price else None

    # 試算基準：以每月投入、投資滿一年後的持有股數估算，用 2025 全年配息合計推估年領
    st.subheader("試算基準")
    st.markdown("年領 = 一年累積股數 × 2025 年每股配息合計；月領為該月有配息的檔次加總（每檔以該月最新一次配息金額 × 累積股數）。實際配息每年會變，僅供組合比較。")

    for combo in COMBOS:
        st.markdown(f'<div class="combo"><h3>{combo["name"]}</h3><p>{combo["idea"]}</p></div>', unsafe_allow_html=True)
        members = combo["members"]
        # 每月覆蓋：用 2025+2026 實際除息月份
        months = {m: [] for m in range(1, 13)}
        for c in members:
            d = div_map.get(c)
            if d is not None and len(d):
                for _, r in d[d["date"] >= "2025-01-01"].iterrows():
                    months[int(pd.to_datetime(r["date"]).month)].append(c)
        covered = sum(1 for m in range(1, 13) if months[m])
        st.markdown(f"**覆蓋月份：{covered} / 12 個月**" + (" ✅ 每月都有" if covered == 12 else " ⚠️ 有空缺月份"))
        cov_rows = []
        for m in range(1, 13):
            payers = sorted(set(months[m]))
            # 該月試算：每檔以最近一次該月配息金額 × 一年累積股數（取 2025-2026 該月最新一筆）
            amt = 0.0
            for c in payers:
                d = div_map.get(c)
                sub = d[(d["date"].dt.month == m) & (d["date"] >= "2025-01-01")] if d is not None and len(d) else pd.DataFrame()
                if len(sub) and shares_map.get(c):
                    amt += float(sub.sort_values("date")["amount"].iloc[-1]) * shares_map[c]
            cov_rows.append({"月份": f"{m} 月", "配息 ETF": "、".join(payers) if payers else "—（無）",
                             "試算月領": (f"約 NT$ {amt:,.0f}" if amt else "—")})
        st.dataframe(pd.DataFrame(cov_rows), width="stretch", hide_index=True)

        # 成員年領試算（依每月投入換算的累積股數）
        mem_rows = []
        total_year = 0.0
        total_invest = 0
        for c in members:
            d = div_map.get(c)
            y25 = float(d[d["date"].dt.year == 2025]["amount"].sum()) if d is not None and len(d) else 0.0
            y26 = float(d[d["date"].dt.year == 2026]["amount"].sum()) if d is not None and len(d) else 0.0
            sh = shares_map.get(c)
            inv = inv_map[c]
            annual = (sh * y25) if sh else 0.0
            mem_rows.append({
                "ETF": f"{c} {ETF_BY_CODE[c][1]}",
                "每月投入": f"NT$ {inv:,}",
                "一年累積股數": (f"{sh:,.0f} 股" if sh else "—"),
                "2025 每股合計": f"{y25:.3f} 元",
                "2026 年初至今每股": f"{y26:.3f} 元",
                "試算年領（依2025）": f"約 NT$ {annual:,.0f}",
                "一年投入本金": f"NT$ {12 * inv:,}",
            })
            total_year += annual
            total_invest += 12 * inv
        st.dataframe(pd.DataFrame(mem_rows), width="stretch", hide_index=True)
        yld = (total_year / total_invest * 100) if total_invest else 0.0
        st.markdown(f"組合試算年領合計（依 2025 配息）：**約 NT$ {total_year:,.0f}**（一年投入本金合計 NT$ {total_invest:,}，年領約為本金的 {yld:.2f}%）")
        st.warning("留意：" + combo["risk"])

    st.divider()
    st.subheader("怎麼用這三個組合？")
    st.markdown(
        "- **想最簡單**：選組合 A，三檔季配剛好錯開，不用每月操作。\n"
        "- **想每月都有現金進帳**：選組合 B，兩檔月配每月都領，但要接受月配金額小、偶有缺月。\n"
        "- **想兼顧成長**：選組合 C，在 A 的基礎上加 0050，1、7 月多領一筆、長期多一點大盤成長。\n"
        "- 不論選哪個，配息都會因公司獲利、ETF 收益平準金而變動，領息前請以投信官網公告為準；"
        "高股息不等於高報酬，價差虧損可能吃掉股息。"
    )


# ---------------------------------------------------------------- 路由
if page.startswith("🌍"):
    page_global()
elif page.startswith("🇹🇼"):
    page_etf()
else:
    page_monthly()

st.divider()
st.markdown(
    '<p class="note">資料來源：Yahoo Finance（Chart API + yfinance 股利）。'
    "報價為延遲報價，股利為歷史除息紀錄；下次配息日期除非投信已公告，否則本頁僅依歷史間隔推估。"
    "本頁僅供研究參考，非投資建議。</p>",
    unsafe_allow_html=True,
)
