#!/usr/bin/env python3
"""kb_klines — Kline data access + chart rendering tool.

Calls the Mobius Quant API for real OHLCV, or parses user-pasted data,
then extracts features to give the skill ground truth for analysis.

Network policy: this module makes outbound HTTP requests to exactly one
domain — api.mobiusquant.ai (or an operator-configured MOBIUS_API_BASE) —
a public OHLCV/indicator service operated by MobiusQuant
(https://www.mobiusquant.ai/). No credentials are required or collected by
default; if MOBIUS_API_TOKEN is explicitly set in the environment, it is sent
as a Bearer header for higher rate limits.

Sub-commands:
  resolve     natural name → canonical (exchange, market, symbol)
  fetch       fetch OHLCV (with HTF) — for analyze
  parse       user-pasted text → standard OHLCV
  analyze     OHLCV → feature summary (with panels.items visualization hints)
  chart       fetch K-lines → assemble panels payload
  render      panels JSON → PNG (Playwright + lightweight-charts)
  indicators  fetch indicator output (defaults to the SMC structural
              indicator; pass --inds <name> only when the user explicitly
              named one)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


log = logging.getLogger("kb_klines")

API_BASE = os.environ.get("MOBIUS_API_BASE", "https://api.mobiusquant.ai")
DEFAULT_TIMEOUT = 15

# HTF 自动选择规则（LTF → HTF 一档大）
HTF_LADDER = {
    "1m":  "5m",
    "5m":  "15m",
    "15m": "1h",
    "30m": "1h",
    "1h":  "4h",
    "4h":  "1d",
    "1d":  None,  # 已是最大档
}


# ============================================================================
# HTTP client
# ============================================================================

class MobiusError(RuntimeError):
    """API call error with status_code and detail."""

    def __init__(self, status: int, detail: str, url: str):
        self.status = status
        self.detail = detail
        self.url = url
        super().__init__(f"[{status}] {url}: {detail}")


class MobiusClient:
    """Mobius Quant API lightweight HTTP client.

    Calls api.mobiusquant.ai (public endpoint, no authentication required).

    - 429 backoff retry (per Retry-After)
    - 502/504 exponential backoff
    - 400/422 raise immediately
    """

    def __init__(
        self,
        base_url: str = API_BASE,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    def _headers(self, content_type: Optional[str] = None) -> dict[str, str]:
        h: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "OpenMobius-skill/0.4",
        }
        token = os.environ.get("MOBIUS_API_TOKEN", "").strip()
        if token:
            h["Authorization"] = f"Bearer {token}"
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base}{path}"
        if params:
            # 去掉 None 值
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=self._headers(
                content_type="application/json" if body is not None else None,
            ),
        )

        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    err_body = json.loads(e.read().decode("utf-8"))
                    detail = err_body.get("detail", str(e))
                except Exception:  # noqa: BLE001
                    detail = str(e)

                # 429: 看 Retry-After 退避
                if e.code == 429 and attempt < self.max_retries:
                    retry_after = int(e.headers.get("Retry-After", "5"))
                    log.warning("429 rate limited, sleeping %ds (attempt %d/%d)",
                                retry_after, attempt + 1, self.max_retries)
                    time.sleep(retry_after)
                    continue

                # 502/504: 指数退避
                if e.code in (502, 504) and attempt < self.max_retries:
                    backoff = min(30, 2 ** attempt)
                    log.warning("%d transient error, sleeping %ds (attempt %d/%d)",
                                e.code, backoff, attempt + 1, self.max_retries)
                    time.sleep(backoff)
                    continue

                # 不重试的错误
                raise MobiusError(e.code, detail, url) from None
            except urllib.error.URLError as e:
                # 网络层错误
                if attempt < self.max_retries:
                    backoff = min(30, 2 ** attempt)
                    log.warning("network error %s, sleeping %ds", e.reason, backoff)
                    time.sleep(backoff)
                    continue
                raise MobiusError(0, f"network: {e.reason}", url) from None

        raise MobiusError(0, "max retries exceeded", url)

    # ------------------------- 端点封装 -------------------------

    def health(self) -> dict:
        return self._request("GET", "/api/health")

    def symbols_search(self, query: str) -> dict:
        return self._request("GET", "/api/symbols/search", params={"q": query})

    def symbols_builtin(self) -> dict:
        return self._request("GET", "/api/symbols/builtin")

    def markets(self) -> dict:
        return self._request("GET", "/api/markets")

    def klines(
        self,
        exchange: str,
        market: str,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> dict:
        return self._request(
            "GET",
            "/api/klines",
            params={
                "exchange": exchange,
                "market": market,
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "startTime": start_time,
                "endTime": end_time,
            },
        )

    def indicators(
        self,
        exchange: str,
        market: str,
        symbol: str,
        interval: str,
        calc: list[dict],
        limit: int = 200,
        explain: bool = True,
    ) -> dict:
        """POST /api/indicators — 拉 K 线 + 计算指标。

        返回: {exchange, market, symbol, interval, current_price, count, klines, indicators}
        - klines: [[open_time_ms, o, h, l, c, v, qv], ...]
        - indicators: {<key>: {columns, data, explain?}}
          key 由 name + params 生成（如 "ema:period=20"）
        """
        return self._request(
            "POST",
            "/api/indicators",
            params={"explain": str(explain).lower()},
            body={
                "exchange": exchange,
                "market": market,
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "calc": calc,
            },
        )


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class AssetSpec:
    """资产规格：venue + canonical symbol。"""
    exchange: str
    market: str
    symbol: str
    asset_class: str = ""
    display: str = ""

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "market": self.market,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "display": self.display,
        }


@dataclass
class KlineData:
    """单 timeframe 的 OHLCV + 元数据。"""
    asset: AssetSpec
    interval: str
    candles: list[list[float]]  # [[open_time_ms, open, high, low, close, volume, quote_volume], ...]
    columns: list[str] = field(
        default_factory=lambda: ["open_time", "open", "high", "low", "close", "volume", "quote_volume"],
    )

    @property
    def current_price(self) -> Optional[float]:
        if not self.candles:
            return None
        # close 在 index 4
        return float(self.candles[-1][4])

    @property
    def count(self) -> int:
        return len(self.candles)

    @property
    def time_range_ms(self) -> Optional[tuple[int, int]]:
        if not self.candles:
            return None
        return (int(self.candles[0][0]), int(self.candles[-1][0]))

    def to_dict(self) -> dict:
        return {
            "asset": self.asset.to_dict(),
            "interval": self.interval,
            "columns": self.columns,
            "count": self.count,
            "current_price": self.current_price,
            "time_range_ms": list(self.time_range_ms) if self.time_range_ms else None,
            "candles": self.candles,
        }


# ============================================================================
# resolve 子命令
# ============================================================================

def cmd_resolve(args: argparse.Namespace) -> int:
    """自然名 → canonical 资产规格。"""
    client = MobiusClient()
    query = args.query.strip()
    if not query:
        log.error("--query is required and non-empty")
        return 2

    resp = client.symbols_search(query)
    matches = resp.get("matches") or []
    if not matches:
        log.error("no symbols matched %r", query)
        return 1

    # 默认取第一个匹配（API 已按优先级排序）
    top = matches[0]
    asset = AssetSpec(
        exchange=top["exchange"],
        market=top["market"],
        symbol=top["symbol"],
        asset_class=top.get("asset_class", ""),
        display=top.get("display", ""),
    )

    out = asset.to_dict()
    if args.show_all:
        out = {"top": asset.to_dict(), "all": matches}

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


# ============================================================================
# fetch 子命令
# ============================================================================

def _fetch_one(
    client: MobiusClient,
    asset: AssetSpec,
    interval: str,
    limit: int,
    with_indicators: Optional[list[dict]] = None,
) -> KlineData:
    """单 timeframe 拉数据。"""
    if with_indicators:
        resp = client.indicators(
            exchange=asset.exchange,
            market=asset.market,
            symbol=asset.symbol,
            interval=interval,
            calc=with_indicators,
            limit=limit,
        )
        return KlineData(
            asset=asset,
            interval=interval,
            candles=resp.get("klines") or [],
        )
    resp = client.klines(
        exchange=asset.exchange,
        market=asset.market,
        symbol=asset.symbol,
        interval=interval,
        limit=limit,
    )
    return KlineData(
        asset=asset,
        interval=interval,
        candles=resp.get("data") or [],
    )


HOURLY_DAILY_ONLY_VENUES = frozenset({
    ("stock", "hk"),
    ("stock", "us"),
    ("forex", "spot"),
})


def _htf_for(interval: str, asset: Optional[AssetSpec] = None) -> Optional[str]:
    """LTF → HTF 一档大；返回 None 表示已是最大档。

    stock:hk / stock:us / forex:spot 在 KlineSync Engine v2.0 仅支持 1h 与 1d
    （不支持 4h），因此这些市场的 1h 直接映射到 1d。
    """
    if (
        asset is not None
        and interval == "1h"
        and (asset.exchange.lower(), asset.market.lower()) in HOURLY_DAILY_ONLY_VENUES
    ):
        return "1d"
    return HTF_LADDER.get(interval)


def cmd_fetch(args: argparse.Namespace) -> int:
    """拉 OHLCV，可选自动联拉 HTF。"""
    client = MobiusClient()

    # 1) 解析 asset spec
    if args.exchange and args.market and args.symbol:
        asset = AssetSpec(
            exchange=args.exchange,
            market=args.market,
            symbol=args.symbol,
        )
    elif args.query:
        resp = client.symbols_search(args.query)
        matches = resp.get("matches") or []
        if not matches:
            log.error("no symbols matched %r", args.query)
            return 1
        top = matches[0]
        asset = AssetSpec(
            exchange=top["exchange"],
            market=top["market"],
            symbol=top["symbol"],
            asset_class=top.get("asset_class", ""),
            display=top.get("display", ""),
        )
        log.info("resolved %r → %s:%s:%s", args.query,
                 asset.exchange, asset.market, asset.symbol)
    else:
        log.error("provide either --query, or all of --exchange/--market/--symbol")
        return 2

    # 2) 拉主 timeframe
    ltf = _fetch_one(client, asset, args.interval, args.limit)
    log.info("fetched %s @ %s: %d candles (current %s)",
             asset.symbol, args.interval, ltf.count, ltf.current_price)

    out: dict = {"primary": ltf.to_dict()}

    # 3) 自动联拉 HTF
    if args.with_htf:
        htf_interval = _htf_for(args.interval, asset)
        if htf_interval is None:
            log.info("--with-htf: %s is top, no HTF to fetch", args.interval)
            out["htf"] = None
        else:
            try:
                htf = _fetch_one(client, asset, htf_interval, args.htf_limit)
                log.info("fetched HTF %s @ %s: %d candles",
                         asset.symbol, htf_interval, htf.count)
                out["htf"] = htf.to_dict()
            except MobiusError as exc:
                if exc.status in (400, 422):
                    log.warning(
                        "HTF %s is unsupported for %s:%s:%s (%s); continuing without HTF",
                        htf_interval, asset.exchange, asset.market, asset.symbol, exc.detail,
                    )
                    out["htf"] = None
                else:
                    raise

    # 4) 输出
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("written to %s", out_path)
        print(str(out_path))
    else:
        json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 0


# ============================================================================
# 特征提取算法（纯数值，不用 LLM）
# ============================================================================

@dataclass
class Candle:
    """单根 K 线。"""
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_row(cls, row: list) -> "Candle":
        return cls(
            time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
        )

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low


def calc_atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    """Average True Range（简化版：rolling mean of TR）。"""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        prev_close = candles[i - 1].close
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - prev_close),
            abs(candles[i].low - prev_close),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def find_swings(candles: list[Candle], left: int = 2, right: int = 2) -> list[dict]:
    """Fractal swing points: bar i 是 left 根之前 + right 根之后的极值。"""
    out = []
    for i in range(left, len(candles) - right):
        c = candles[i]
        # high pivot
        if all(c.high >= candles[i - k].high for k in range(1, left + 1)) and \
           all(c.high >= candles[i + k].high for k in range(1, right + 1)):
            out.append({"index": i, "price": c.high, "kind": "high"})
        # low pivot
        if all(c.low <= candles[i - k].low for k in range(1, left + 1)) and \
           all(c.low <= candles[i + k].low for k in range(1, right + 1)):
            out.append({"index": i, "price": c.low, "kind": "low"})
    return out


def _fvg_mitigation_pct(
    top: float, bot: float, fvg_type: str, candles: list[Candle], formed_at: int,
) -> float:
    """从 fvg 形成后的 K 线判断已经被填补的比例。"""
    if formed_at + 1 >= len(candles):
        return 0.0
    subsequent = candles[formed_at + 1:]
    size = top - bot
    if size <= 0:
        return 0.0
    if fvg_type == "bullish_fvg":
        # 自上而下填补
        min_low = min(c.low for c in subsequent)
        if min_low >= top:
            return 0.0
        if min_low <= bot:
            return 100.0
        return (top - min_low) / size * 100.0
    # bearish: 自下而上填补
    max_high = max(c.high for c in subsequent)
    if max_high <= bot:
        return 0.0
    if max_high >= top:
        return 100.0
    return (max_high - bot) / size * 100.0


def find_fvgs(candles: list[Candle], min_size_atr: float = 0.2) -> list[dict]:
    """3-candle non-overlap FVG。

    - bullish: candles[i].high < candles[i+2].low
    - bearish: candles[i].low > candles[i+2].high

    过滤太小的 gap（默认 < 0.2 * ATR）。
    """
    out = []
    n = len(candles)
    if n < 3:
        return out
    atr = calc_atr(candles) or 0
    min_size = min_size_atr * atr if atr else 0

    for i in range(n - 2):
        c0, _c1, c2 = candles[i], candles[i + 1], candles[i + 2]
        # bullish FVG
        if c0.high < c2.low:
            top, bot = c2.low, c0.high
            if top - bot < min_size:
                continue
            out.append({
                "type": "bullish_fvg",
                "top": round(top, 4),
                "bottom": round(bot, 4),
                "formed_at_index": i + 1,
                "age_bars": n - 1 - (i + 1),
                "size": round(top - bot, 4),
                "mitigation_pct": round(
                    _fvg_mitigation_pct(top, bot, "bullish_fvg", candles, i + 1), 1,
                ),
            })
        # bearish FVG
        elif c0.low > c2.high:
            top, bot = c0.low, c2.high
            if top - bot < min_size:
                continue
            out.append({
                "type": "bearish_fvg",
                "top": round(top, 4),
                "bottom": round(bot, 4),
                "formed_at_index": i + 1,
                "age_bars": n - 1 - (i + 1),
                "size": round(top - bot, 4),
                "mitigation_pct": round(
                    _fvg_mitigation_pct(top, bot, "bearish_fvg", candles, i + 1), 1,
                ),
            })
    return out


def find_order_blocks(
    candles: list[Candle], displacement_atr_mult: float = 1.5,
) -> list[dict]:
    """Order Block: 最后一根反向 K 线，紧接强势 displacement。"""
    out = []
    n = len(candles)
    if n < 4:
        return out
    atr = calc_atr(candles)
    if not atr:
        return out
    threshold = displacement_atr_mult * atr

    for i in range(n - 3):
        c = candles[i]
        next3 = candles[i + 1:i + 4]
        if len(next3) < 3:
            continue
        # bullish OB: 当前是 bearish K 线，紧接强势上涨
        if not c.is_bullish:
            move = next3[-1].close - c.open
            cum_up = sum(max(0, x.close - x.open) for x in next3)
            if move > threshold and cum_up > threshold:
                out.append({
                    "type": "bullish_ob",
                    "top": round(c.open, 4),
                    "bottom": round(c.low, 4),
                    "formed_at_index": i,
                    "age_bars": n - 1 - i,
                    "displacement_atr": round(move / atr, 2),
                })
        # bearish OB: 当前是 bullish K 线，紧接强势下跌
        elif c.is_bullish:
            move = c.open - next3[-1].close
            cum_dn = sum(max(0, x.open - x.close) for x in next3)
            if move > threshold and cum_dn > threshold:
                out.append({
                    "type": "bearish_ob",
                    "top": round(c.high, 4),
                    "bottom": round(c.open, 4),
                    "formed_at_index": i,
                    "age_bars": n - 1 - i,
                    "displacement_atr": round(move / atr, 2),
                })
    return out


def find_sweeps(
    candles: list[Candle], swings: list[dict], lookback_bars: int = 15,
) -> list[dict]:
    """Liquidity Sweep: 单 K 线穿越前 swing 后收盘回内侧。"""
    out = []
    n = len(candles)
    swing_highs = [(s["index"], s["price"]) for s in swings if s["kind"] == "high"]
    swing_lows = [(s["index"], s["price"]) for s in swings if s["kind"] == "low"]

    for i in range(1, n):
        c = candles[i]
        # buy-side sweep: 高过前 swing high 后收盘回下
        for sh_idx, sh_price in swing_highs:
            if sh_idx >= i:
                continue
            if i - sh_idx > lookback_bars:
                continue
            if c.high > sh_price and c.close < sh_price:
                out.append({
                    "type": "buy_side_sweep",
                    "swept_level": round(sh_price, 4),
                    "swept_level_index": sh_idx,
                    "sweep_candle_index": i,
                    "age_bars": n - 1 - i,
                    "wick_size": round(c.high - max(c.open, c.close), 4),
                })
                break
        # sell-side sweep: 低过前 swing low 后收盘回上
        for sl_idx, sl_price in swing_lows:
            if sl_idx >= i:
                continue
            if i - sl_idx > lookback_bars:
                continue
            if c.low < sl_price and c.close > sl_price:
                out.append({
                    "type": "sell_side_sweep",
                    "swept_level": round(sl_price, 4),
                    "swept_level_index": sl_idx,
                    "sweep_candle_index": i,
                    "age_bars": n - 1 - i,
                    "wick_size": round(min(c.open, c.close) - c.low, 4),
                })
                break
    return out


def find_displacements(candles: list[Candle], atr_mult: float = 2.0) -> list[dict]:
    """单 K 线 body > atr_mult * ATR → displacement。"""
    atr = calc_atr(candles)
    if not atr:
        return []
    threshold = atr_mult * atr
    n = len(candles)
    out = []
    for i, c in enumerate(candles):
        if c.body >= threshold:
            out.append({
                "direction": "bullish" if c.is_bullish else "bearish",
                "magnitude_pct": round((c.close - c.open) / c.open * 100, 3),
                "magnitude_atr": round(c.body / atr, 2),
                "candle_index": i,
                "age_bars": n - 1 - i,
            })
    return out


def find_volume_anomalies(
    candles: list[Candle], lookback: int = 20, mult: float = 2.0,
) -> list[dict]:
    """volume > mult * rolling avg。"""
    n = len(candles)
    if n < lookback + 1:
        return []
    out = []
    for i in range(lookback, n):
        recent = [c.volume for c in candles[i - lookback:i]]
        avg = sum(recent) / len(recent) if recent else 0
        if avg == 0:
            continue
        ratio = candles[i].volume / avg
        if ratio > mult:
            out.append({
                "candle_index": i,
                "age_bars": n - 1 - i,
                "volume_ratio": round(ratio, 2),
                "direction": "bullish" if candles[i].is_bullish else "bearish",
            })
    return out


def analyze_structure(swings: list[dict]) -> dict:
    """Swing 序列 → HH/HL/LH/LL + BOS/CHoCH 信号。"""
    if len(swings) < 3:
        return {"sequence": [], "events": []}
    # 按 index 排序
    sorted_sw = sorted(swings, key=lambda s: s["index"])
    sequence: list[dict] = []
    last_high = None
    last_low = None
    for s in sorted_sw:
        if s["kind"] == "high":
            if last_high is None:
                label = "H"
            elif s["price"] > last_high:
                label = "HH"
            else:
                label = "LH"
            last_high = s["price"]
        else:
            if last_low is None:
                label = "L"
            elif s["price"] > last_low:
                label = "HL"
            else:
                label = "LL"
            last_low = s["price"]
        sequence.append({
            "label": label,
            "index": s["index"],
            "price": round(s["price"], 4),
        })

    # 简单 BOS / CHoCH 识别（看最后 3-4 个 swing）
    events = []
    if len(sequence) >= 4:
        last4 = [x["label"] for x in sequence[-4:]]
        # bullish BOS: ...HL → HH（trend continuation）
        if last4[-1] == "HH" and "HL" in last4[:-1]:
            events.append({
                "type": "bullish_bos",
                "at_index": sequence[-1]["index"],
                "at_price": sequence[-1]["price"],
            })
        # bearish BOS
        elif last4[-1] == "LL" and "LH" in last4[:-1]:
            events.append({
                "type": "bearish_bos",
                "at_index": sequence[-1]["index"],
                "at_price": sequence[-1]["price"],
            })
        # bullish CHoCH: 之前是下跌结构（LH/LL），现在 HH
        if last4[-1] == "HH" and last4[-2] in ("LH", "LL"):
            events.append({
                "type": "bullish_choch",
                "at_index": sequence[-1]["index"],
                "at_price": sequence[-1]["price"],
            })
        # bearish CHoCH
        elif last4[-1] == "LL" and last4[-2] in ("HL", "HH"):
            events.append({
                "type": "bearish_choch",
                "at_index": sequence[-1]["index"],
                "at_price": sequence[-1]["price"],
            })
    return {"sequence": sequence, "events": events}


# ============================================================================
# 特征摘要格式化（LLM 友好）
# ============================================================================

def _fmt_age(bars: int, interval: str) -> str:
    """把 'age_bars' 转可读时间 (e.g. '12 bars / 12h ago')."""
    if bars == 0:
        return "current bar"
    # interval → 每根 K 线的秒数
    unit_map = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                "1h": 3600, "4h": 14400, "1d": 86400}
    secs = unit_map.get(interval, 0) * bars
    if secs <= 0:
        return f"{bars} bars ago"
    if secs < 3600:
        return f"{bars} bars / {secs // 60}m ago"
    if secs < 86400:
        return f"{bars} bars / {secs // 3600}h ago"
    return f"{bars} bars / {secs // 86400}d ago"


def _fmt_ts(ms: int) -> str:
    """Unix ms → 'YYYY-MM-DD HH:MM UTC'."""
    import datetime  # noqa: PLC0415
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc) \
        .strftime("%Y-%m-%d %H:%M UTC")


def format_summary(
    kdata: dict,
    candles: list[Candle],
    swings: list[dict],
    fvgs: list[dict],
    obs: list[dict],
    sweeps: list[dict],
    displacements: list[dict],
    vols: list[dict],
    structure: dict,
    label: str = "primary",
) -> str:
    """格式化为 LLM 友好的多段文本。"""
    asset = kdata.get("asset", {})
    interval = kdata.get("interval", "?")
    n = len(candles)
    if n == 0:
        return f"== {label} ({asset.get('symbol','?')} {interval}) ==\n(no data)\n"

    current = candles[-1].close
    highest = max(c.high for c in candles)
    lowest = min(c.low for c in candles)
    first_close = candles[0].close
    trend_pct = (current - first_close) / first_close * 100 if first_close else 0
    atr = calc_atr(candles)

    lines: list[str] = []
    lines.append(
        f"== {label}: {asset.get('symbol','?')} "
        f"({asset.get('exchange','?')}:{asset.get('market','?')}) {interval} ==",
    )
    tr = kdata.get("time_range_ms")
    if tr:
        lines.append(f"time range: {_fmt_ts(tr[0])} → {_fmt_ts(tr[1])} ({n} candles)")
    lines.append("")

    # Market state
    lines.append("market state:")
    lines.append(f"  current close: {current:g}")
    lines.append(f"  range: {lowest:g} - {highest:g}")
    lines.append(f"  trend over {n} candles: {trend_pct:+.2f}%")
    if atr:
        lines.append(f"  ATR(14): {atr:.4f}")
    lines.append("")

    # Structure
    lines.append("structure (swing sequence):")
    if structure["sequence"]:
        # 只显示最后 8 个
        recent = structure["sequence"][-8:]
        labels = " → ".join(f"{s['label']}@{s['price']:g}" for s in recent)
        lines.append(f"  {labels}")
    else:
        lines.append("  (no clear swings)")
    if structure["events"]:
        for ev in structure["events"]:
            lines.append(
                f"  ⚡ {ev['type']} at index {ev['at_index']} (price {ev['at_price']:g})",
            )
    lines.append("")

    # FVGs (top 5 most relevant: 未填或部分填、按 age 升序)
    untested_fvgs = [f for f in fvgs if f["mitigation_pct"] < 100]
    untested_fvgs.sort(key=lambda f: f["age_bars"])
    lines.append(f"FVG candidates (untested / partially mitigated, top 5 / total {len(fvgs)}):")
    if not untested_fvgs:
        lines.append("  (none unmitigated)")
    else:
        for f in untested_fvgs[:5]:
            tag = "🟢" if f["type"] == "bullish_fvg" else "🔴"
            lines.append(
                f"  {tag} {f['type']}: {f['bottom']:g} - {f['top']:g} "
                f"({_fmt_age(f['age_bars'], interval)}, "
                f"mitigation {f['mitigation_pct']:g}%, size {f['size']:g})",
            )
    lines.append("")

    # Order Blocks (top 5 by age)
    lines.append(f"Order Block candidates (total {len(obs)}):")
    if not obs:
        lines.append("  (none)")
    else:
        sorted_obs = sorted(obs, key=lambda o: o["age_bars"])
        for ob in sorted_obs[:5]:
            tag = "🟢" if ob["type"] == "bullish_ob" else "🔴"
            lines.append(
                f"  {tag} {ob['type']}: {ob['bottom']:g} - {ob['top']:g} "
                f"({_fmt_age(ob['age_bars'], interval)}, "
                f"next displacement {ob['displacement_atr']}x ATR)",
            )
    lines.append("")

    # Liquidity Sweeps
    lines.append(f"Liquidity Sweep candidates (total {len(sweeps)}):")
    if not sweeps:
        lines.append("  (none)")
    else:
        sorted_sweeps = sorted(sweeps, key=lambda s: s["age_bars"])
        for s in sorted_sweeps[:5]:
            tag = "🔴" if s["type"] == "buy_side_sweep" else "🟢"
            lines.append(
                f"  {tag} {s['type']} at {s['swept_level']:g} "
                f"({_fmt_age(s['age_bars'], interval)}, wick {s['wick_size']:g})",
            )
    lines.append("")

    # Displacements
    lines.append(f"Displacement candles (total {len(displacements)}):")
    if not displacements:
        lines.append("  (none)")
    else:
        sorted_disps = sorted(displacements, key=lambda d: d["age_bars"])
        for d in sorted_disps[:3]:
            tag = "🟢" if d["direction"] == "bullish" else "🔴"
            lines.append(
                f"  {tag} {d['direction']}: {d['magnitude_pct']:+g}% "
                f"({d['magnitude_atr']}x ATR, "
                f"{_fmt_age(d['age_bars'], interval)})",
            )
    lines.append("")

    # Volume anomalies
    lines.append(f"Volume anomalies (total {len(vols)}):")
    if not vols:
        lines.append("  (none)")
    else:
        sorted_vols = sorted(vols, key=lambda v: v["age_bars"])
        for v in sorted_vols[:3]:
            tag = "🟢" if v["direction"] == "bullish" else "🔴"
            lines.append(
                f"  {tag} {v['direction']} candle at "
                f"{v['volume_ratio']:g}x avg volume "
                f"({_fmt_age(v['age_bars'], interval)})",
            )
    lines.append("")

    return "\n".join(lines)


# ============================================================================
# analyze / parse 子命令
# ============================================================================

def _extract_klinedata(d: dict) -> tuple[dict, Optional[dict]]:
    """从 fetch 输出或单 KlineData dict 抽出 (primary, htf)."""
    if "primary" in d:
        return d["primary"], d.get("htf")
    return d, None


def _analyze_one_dict(kdata: dict, label: str) -> dict:
    """对单 KlineData dict 做特征提取，返回结构化 features dict（含 panels.items 建议）。"""
    candles_raw = kdata.get("candles") or []
    candles = [Candle.from_row(c) for c in candles_raw]
    if not candles:
        return {"label": label, "empty": True}

    swings = find_swings(candles)
    fvgs = find_fvgs(candles)
    obs = find_order_blocks(candles)
    sweeps = find_sweeps(candles, swings)
    disps = find_displacements(candles)
    vols = find_volume_anomalies(candles)
    structure = analyze_structure(swings)

    # Panel overlay 建议：把未填补 FVG / 近期 sweep / OB 转成可直接喂 render 的 items
    overlay_items = _build_overlay_items(
        kdata, candles, fvgs, obs, sweeps, structure,
    )

    asset = kdata.get("asset", {})
    return {
        "label": label,
        "asset": asset,
        "interval": kdata.get("interval"),
        "count": len(candles),
        "current_price": candles[-1].close,
        "range": {
            "high": max(c.high for c in candles),
            "low":  min(c.low  for c in candles),
        },
        "trend_pct": (candles[-1].close - candles[0].close) / candles[0].close * 100 if candles[0].close else 0,
        "atr14": calc_atr(candles),
        "structure": structure,
        "fvgs": fvgs,
        "order_blocks": obs,
        "sweeps": sweeps,
        "displacements": disps,
        "volume_anomalies": vols,
        "swings": swings,
        # 建议的 chart overlay items — LLM 可直接挑选加到 chart 输出的 panels[0].items
        "suggested_overlay_items": overlay_items,
    }


def _build_overlay_items(
    kdata: dict,
    candles: list[Candle],
    fvgs: list[dict],
    obs: list[dict],
    sweeps: list[dict],
    structure: dict,
    max_fvgs: int = 2,
    max_obs: int = 2,
    max_sweeps: int = 2,
) -> list[dict]:
    """生成可直接合入主图 panel 的 items 建议。

    数量上限（默认）：FVG 2 / OB 2 / Sweep 2 / Swing 3。视图不挤、信息够用。

    输出图元：
      - FVG / Order Block → rectangle（time 范围 + price 范围 + 语义 role）
      - Liquidity Sweep   → hline（被扫价位）+ markers（穿越 candle）
      - Swing point       → markers

    LLM 在 workflow 里可以挑选若干条加入 chart 输出的 panels[0].items，再喂给 render。
    时间单位：**秒**（lightweight-charts 惯例）。
    """
    items: list[dict] = []
    n = len(candles)
    if n == 0:
        return items

    def _t_sec(ms_or_sec: int) -> int:
        return int(ms_or_sec) // 1000 if ms_or_sec > 1e12 else int(ms_or_sec)

    times_sec = [_t_sec(c.time) for c in candles]

    # ── FVG: bullish 和 bearish 分开各取 max_fvgs/2 个 ──
    untested = [f for f in fvgs if f["mitigation_pct"] < 100]
    bull_fvgs = sorted(
        [f for f in untested if f["type"] == "bullish_fvg"],
        key=lambda f: f["age_bars"],
    )
    bear_fvgs = sorted(
        [f for f in untested if f["type"] == "bearish_fvg"],
        key=lambda f: f["age_bars"],
    )
    n_each = max(1, max_fvgs // 2 if max_fvgs >= 2 else max_fvgs)
    for f in (bull_fvgs[:n_each] + bear_fvgs[:n_each])[:max_fvgs]:
        idx = f["formed_at_index"]
        t_start = times_sec[idx] if 0 <= idx < n else times_sec[0]
        kind = "bull" if f["type"] == "bullish_fvg" else "bear"
        items.append({
            "type":         "rectangle",
            "time_start":   t_start,
            "time_end":     None,
            "price_top":    f["top"],
            "price_bottom": f["bottom"],
            "label":        f"{kind} FVG",
            "style": {
                "role":         "fvg" if kind == "bull" else "fvg_bear",
                "fill_opacity": 0.10,
                "border_width": 0.5,
                "dash":         "dashed",
            },
        })

    # ── OB: bullish 和 bearish 分开各取 ──
    bull_obs = sorted([o for o in obs if o["type"] == "bullish_ob"], key=lambda o: o["age_bars"])
    bear_obs = sorted([o for o in obs if o["type"] == "bearish_ob"], key=lambda o: o["age_bars"])
    n_each = max(1, max_obs // 2 if max_obs >= 2 else max_obs)
    for o in (bull_obs[:n_each] + bear_obs[:n_each])[:max_obs]:
        idx = o["formed_at_index"]
        t_start = times_sec[idx] if 0 <= idx < n else times_sec[0]
        kind = "bull" if o["type"] == "bullish_ob" else "bear"
        items.append({
            "type":         "rectangle",
            "time_start":   t_start,
            "time_end":     None,
            "price_top":    o["top"],
            "price_bottom": o["bottom"],
            "label":        f"{kind} OB",
            "style": {
                "role":         "ob" if kind == "bull" else "ob_bear",
                "fill_opacity": 0.12,
                "border_width": 0.5,
                "dash":         "dotted",
            },
        })

    # ── Liquidity Sweep ──
    sorted_sweeps = sorted(sweeps, key=lambda s: s["age_bars"])
    for s in sorted_sweeps[:max_sweeps]:
        kind = "BSL" if s["type"] == "buy_side_sweep" else "SSL"
        items.append({
            "type":  "hline",
            "value": s["swept_level"],
            "label": kind,
            "style": {"role": "liquidity", "dash": "dashed", "width": 1},
        })
        sweep_idx = s["sweep_candle_index"]
        if 0 <= sweep_idx < n:
            items.append({
                "type": "markers",
                "data": [{
                    "time":     times_sec[sweep_idx],
                    "shape":    "arrowDown" if kind == "BSL" else "arrowUp",
                    "position": "aboveBar" if kind == "BSL" else "belowBar",
                    "text":     kind,
                }],
                "style": {"role": "liquidity"},
            })

    # ── Swing markers（最后 3 个）──
    if structure.get("sequence"):
        for s in structure["sequence"][-3:]:
            idx = s["index"]
            if idx >= n:
                continue
            is_high = s["label"] in ("H", "HH", "LH")
            items.append({
                "type": "markers",
                "data": [{
                    "time":     times_sec[idx],
                    "shape":    "arrowDown" if is_high else "arrowUp",
                    "position": "aboveBar"  if is_high else "belowBar",
                    "text":     s["label"],
                }],
                "style": {"role": "muted"},
            })

    return items


def _analyze_one(kdata: dict, label: str) -> str:
    """文本摘要（向后兼容）。"""
    candles_raw = kdata.get("candles") or []
    candles = [Candle.from_row(c) for c in candles_raw]
    if not candles:
        return f"== {label} ==\n(no candles)\n"

    swings = find_swings(candles)
    fvgs = find_fvgs(candles)
    obs = find_order_blocks(candles)
    sweeps = find_sweeps(candles, swings)
    disps = find_displacements(candles)
    vols = find_volume_anomalies(candles)
    structure = analyze_structure(swings)

    return format_summary(
        kdata, candles, swings, fvgs, obs, sweeps, disps, vols, structure, label=label,
    )


def cmd_analyze(args: argparse.Namespace) -> int:
    # 输入：file 或 stdin
    if args.input:
        raw = Path(args.input).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        log.error("no input")
        return 2
    data = json.loads(raw)

    primary, htf = _extract_klinedata(data)

    # JSON 模式：输出结构化 features（LLM 可用 suggested_overlay_items 直接喂 render）
    if args.format == "json":
        out_dict = {"primary": _analyze_one_dict(primary, label="primary")}
        if htf and htf.get("candles"):
            out_dict["htf"] = _analyze_one_dict(htf, label="HTF")
        text = json.dumps(out_dict, ensure_ascii=False, indent=2, default=str)
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            log.info("features JSON written: %s", out_path)
            print(str(out_path))
        else:
            sys.stdout.write(text)
            sys.stdout.write("\n")
        return 0

    # 默认 text 模式
    out_parts: list[str] = []
    out_parts.append(_analyze_one(primary, label="primary"))
    if htf and htf.get("candles"):
        out_parts.append(_analyze_one(htf, label="HTF"))

    text = "\n".join(out_parts)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        log.info("summary written: %s", out_path)
        print(str(out_path))
    else:
        sys.stdout.write(text)
    return 0


# ============================================================================
# parse: 用户粘贴文本 → 标准 OHLCV JSON
# ============================================================================

import csv  # noqa: E402, PLC0415
import io  # noqa: E402
import re  # noqa: E402
import datetime as _dt  # noqa: E402


# 列名归一化：所有这些都映射到标准 6 列
COLUMN_ALIASES = {
    "time": "time", "timestamp": "time", "open_time": "time",
    "date": "time", "datetime": "time", "t": "time",
    "open": "open", "o": "open",
    "high": "high", "h": "high", "max": "high",
    "low": "low", "l": "low", "min": "low",
    "close": "close", "c": "close", "adj close": "close", "adjclose": "close",
    "volume": "volume", "vol": "volume", "v": "volume",
}


def _normalize_col(name: str) -> Optional[str]:
    return COLUMN_ALIASES.get(name.strip().lower())


def _parse_time(value: Any) -> Optional[int]:
    """各种时间格式 → Unix ms。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # 已经是 ms（>= 10^10 即 ~2001-09-09 ms）
        if v >= 1e12:
            return int(v)
        # 秒级（>= 10^9 即 ~2001-09-09 秒）
        if v >= 1e9:
            return int(v * 1000)
        return int(v)

    s = str(value).strip()
    if not s:
        return None
    # 纯数字
    if s.isdigit():
        return _parse_time(int(s))
    # 尝试 ISO 8601 / 各种日期格式
    fmts = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ]
    # 把 'Z' 替换为 '+00:00' 给 fromisoformat
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    for fmt in fmts:
        try:
            dt = _dt.datetime.strptime(s, fmt).replace(tzinfo=_dt.timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _parse_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _row_to_candle(row: dict) -> Optional[list]:
    """归一化字典 → [time_ms, o, h, l, c, v]; 缺关键字段时返回 None."""
    t = _parse_time(row.get("time"))
    o = _parse_number(row.get("open"))
    h = _parse_number(row.get("high"))
    l = _parse_number(row.get("low"))
    c = _parse_number(row.get("close"))
    v = _parse_number(row.get("volume")) or 0.0
    if None in (t, o, h, l, c):
        return None
    return [t, o, h, l, c, v]


def _parse_as_json(raw: str) -> Optional[list[list]]:
    """尝试当作 JSON 解析（binance 数组格式 或 object 数组）。"""
    raw = raw.strip()
    if not raw or raw[0] not in "[{":
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, list):
        return None
    if not data:
        return []

    # binance 风格: [[time_ms, open_str, high_str, low_str, close_str, vol_str, ...]]
    if isinstance(data[0], list):
        out = []
        for row in data:
            if len(row) < 5:
                continue
            t = _parse_time(row[0])
            o = _parse_number(row[1])
            h = _parse_number(row[2])
            l = _parse_number(row[3])
            c = _parse_number(row[4])
            v = _parse_number(row[5]) if len(row) > 5 else 0.0
            if None in (t, o, h, l, c):
                continue
            out.append([t, o, h, l, c, v or 0.0])
        return out if out else None

    # object 数组
    if isinstance(data[0], dict):
        # 归一化每个 dict 的 key
        out = []
        for row in data:
            norm = {}
            for k, v in row.items():
                std = _normalize_col(k)
                if std:
                    norm[std] = v
            cand = _row_to_candle(norm)
            if cand:
                out.append(cand)
        return out if out else None

    return None


def _parse_as_csv(raw: str) -> Optional[list[list]]:
    """尝试 CSV / TSV（含 header）。"""
    raw = raw.strip()
    if not raw:
        return None
    # 自动检测 dialect
    sample = raw[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        has_header = csv.Sniffer().has_header(sample)
    except csv.Error:
        return None
    if not has_header:
        return None

    reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
    # 归一化 fieldnames
    if not reader.fieldnames:
        return None
    field_map = {f: _normalize_col(f) for f in reader.fieldnames}
    # 至少要有 time/open/high/low/close 5 个映射
    mapped_set = {v for v in field_map.values() if v}
    required = {"time", "open", "high", "low", "close"}
    if not required.issubset(mapped_set):
        return None

    out = []
    for row in reader:
        norm = {}
        for k, v in row.items():
            std = field_map.get(k)
            if std:
                norm[std] = v
        cand = _row_to_candle(norm)
        if cand:
            out.append(cand)
    return out if out else None


def _parse_as_markdown(raw: str) -> Optional[list[list]]:
    """尝试 Markdown table。"""
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    # 找到 `| --- | --- |` 分隔行
    sep_idx = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*\|?[\s\-:|]+\|[\s\-:|]+", ln) and "-" in ln:
            sep_idx = i
            break
    if sep_idx is None or sep_idx == 0:
        return None
    header_line = lines[sep_idx - 1]
    body_lines = lines[sep_idx + 1:]

    def split_row(line: str) -> list[str]:
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    headers = split_row(header_line)
    field_map = [(_normalize_col(h), idx) for idx, h in enumerate(headers)]
    mapped = [(std, idx) for std, idx in field_map if std]
    mapped_set = {std for std, _ in mapped}
    if not {"time", "open", "high", "low", "close"}.issubset(mapped_set):
        return None

    out = []
    for ln in body_lines:
        cells = split_row(ln)
        if len(cells) < len(headers):
            continue
        norm = {std: cells[idx] for std, idx in mapped}
        cand = _row_to_candle(norm)
        if cand:
            out.append(cand)
    return out if out else None


def _parse_as_whitespace(raw: str) -> Optional[list[list]]:
    """尝试空格/Tab 分隔表（有 header）。"""
    lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header_cells = re.split(r"\s+", lines[0].strip())
    field_map = [(_normalize_col(h), idx) for idx, h in enumerate(header_cells)]
    mapped = [(std, idx) for std, idx in field_map if std]
    mapped_set = {std for std, _ in mapped}
    if not {"time", "open", "high", "low", "close"}.issubset(mapped_set):
        return None

    out = []
    for ln in lines[1:]:
        cells = re.split(r"\s+", ln.strip())
        if len(cells) < len(header_cells):
            continue
        norm = {std: cells[idx] for std, idx in mapped if idx < len(cells)}
        cand = _row_to_candle(norm)
        if cand:
            out.append(cand)
    return out if out else None


_PARSERS = [
    ("json", _parse_as_json),
    ("csv", _parse_as_csv),
    ("markdown", _parse_as_markdown),
    ("whitespace", _parse_as_whitespace),
]


def cmd_parse(args: argparse.Namespace) -> int:
    """用户粘贴文本 → 标准 OHLCV JSON。"""
    if args.input:
        raw = Path(args.input).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        log.error("no input (use --input or pipe via stdin)")
        return 2

    candles: Optional[list[list]] = None
    used = None
    for name, parser in _PARSERS:
        try:
            result = parser(raw)
        except Exception as e:  # noqa: BLE001
            log.debug("%s parser raised: %s", name, e)
            continue
        if result:
            candles = result
            used = name
            break

    if not candles:
        log.error(
            "无法识别格式。支持: JSON array, CSV (with header), "
            "Markdown table, whitespace-separated table。\n"
            "建议让 LLM 把数据转成标准 JSON 后再 feed 给 analyze。",
        )
        return 1

    log.info("parsed %d candles via %s parser", len(candles), used)

    # 按 time 升序
    candles.sort(key=lambda c: c[0])

    out = {
        "asset": {
            "exchange": args.exchange or "unknown",
            "market": args.market or "unknown",
            "symbol": args.symbol or "USER_PASTED",
            "asset_class": "user_input",
            "display": args.symbol or "user-pasted",
        },
        "interval": args.interval or "unknown",
        "columns": ["open_time", "open", "high", "low", "close", "volume"],
        "count": len(candles),
        "current_price": candles[-1][4],
        "time_range_ms": [candles[0][0], candles[-1][0]],
        "candles": candles,
    }

    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        log.info("written to %s", out_path)
        print(str(out_path))
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    return 0


# ============================================================================
# chart: 调 Mobius /api/klines 拉纯 K 线 → 组装 panels 协议（无指标）
# ============================================================================

def _ohlcv_to_klines(rows: list[list]) -> list[dict]:
    """[[ms, open, high, low, close, volume, quote_volume?], ...] →
    [{time: sec, open, high, low, close, volume}, ...]

    lightweight-charts 用秒（不是 ms）作为 time。
    """
    out: list[dict] = []
    for r in rows:
        if len(r) < 5:
            continue
        ts_ms = int(r[0])
        out.append({
            "time":   ts_ms // 1000,
            "open":   float(r[1]),
            "high":   float(r[2]),
            "low":    float(r[3]),
            "close":  float(r[4]),
            "volume": float(r[5]) if len(r) > 5 else 0.0,
        })
    return out


# ----------------------------------------------------------------------------
# Freshness metadata — exposed to the LLM so it can NEVER claim data
# freshness from memory. Every API-driven response includes this block.
# ----------------------------------------------------------------------------

INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}


def _utc_iso(ts_seconds: int) -> str:
    """Unix seconds → ISO 8601 UTC string (always with 'Z' suffix)."""
    return datetime.fromtimestamp(int(ts_seconds), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


def _compute_freshness(
    klines: list[dict],
    interval: str,
) -> dict:
    """Build the freshness metadata block.

    Fields:
      fetched_at:             ISO UTC of when this skill fetched from the API
      last_bar_open_time_utc: ISO UTC of the latest bar's open time
      last_bar_age_seconds:   now - last_bar_open_time (positive = older)
      interval_seconds:       expected bar period
      is_stale:               True when bar age exceeds 2 × interval
      data_source:            constant "Mobius Quant API (api.mobiusquant.ai)"
    """
    now_s = int(time.time())
    last_bar_t = int(klines[-1]["time"]) if klines else None
    iv_secs = INTERVAL_SECONDS.get(interval, 0)
    age_s = (now_s - last_bar_t) if last_bar_t is not None else None
    is_stale = (
        age_s is not None and iv_secs > 0 and age_s > 2 * iv_secs
    )
    out: dict = {
        "fetched_at":             _utc_iso(now_s),
        "last_bar_open_time_utc": _utc_iso(last_bar_t) if last_bar_t else None,
        "last_bar_age_seconds":   age_s,
        "interval_seconds":       iv_secs,
        "is_stale":               is_stale,
        "data_source":            "Mobius Quant API (api.mobiusquant.ai)",
    }
    if is_stale:
        log.warning(
            "data staleness: latest %s bar is %ds old (>2× interval=%ds) — "
            "market may be closed or API delayed",
            interval, age_s, iv_secs,
        )
    return out


# ----------------------------------------------------------------------------
# SMC objects sidecar → chart panel items
# ----------------------------------------------------------------------------

def _ms_to_s(ts_ms: Optional[int]) -> Optional[int]:
    """Convert millisecond timestamp (SMC objects) to second timestamp (panels)."""
    if ts_ms is None:
        return None
    try:
        return int(ts_ms) // 1000
    except (TypeError, ValueError):
        return None


def _smc_objects_to_items(
    objects: dict,
    current_price: Optional[float],
    *,
    first_kline_time: Optional[int] = None,
    include_mitigated: bool = True,
    include_zones: bool = False,
    include_internal: bool = True,
    max_items: int = 400,
) -> list[dict]:
    """Convert SMC indicator `objects` sidecar → panels[0].items list.

    TradingView-style: full structural narrative — every BOS/CHoCH event with
    its broken-pivot connector, every active OB / FVG, every equal-highs /
    equal-lows pair, plus the trailing-extreme price labels.
    """
    items: list[dict] = []

    def _add(item: dict) -> bool:
        if len(items) >= max_items:
            return False
        items.append(item)
        return True

    def _clip_left(t_s: Optional[int]) -> Optional[int]:
        if t_s is None:
            return first_kline_time
        if first_kline_time is not None and t_s < first_kline_time:
            return first_kline_time
        return t_s

    # ----- 1. trailing_extremes (Strong High / Weak Low labels) ----------
    te = objects.get("trailing_extremes") or {}
    if te.get("top") is not None:
        _add({
            "type":  "hline",
            "value": te["top"],
            "label": te.get("top_label") or "trailing high",
            "style": {"role": "muted", "width": 1, "dash": "dotted"},
        })
    if te.get("bottom") is not None:
        _add({
            "type":  "hline",
            "value": te["bottom"],
            "label": te.get("bottom_label") or "trailing low",
            "style": {"role": "muted", "width": 1, "dash": "dotted"},
        })

    # ----- 2. ALL BOS/CHoCH events (swing + optionally internal) ---------
    bull_markers: list[dict] = []
    bear_markers: list[dict] = []
    structures_groups: list[list[dict]] = [(objects.get("swing_structures") or [])]
    if include_internal:
        structures_groups.append(objects.get("internal_structures") or [])
    for group in structures_groups:
        for s in group:
            bias = (s.get("bias") or "").lower()
            kind = (s.get("kind") or "").upper()
            pivot_t_raw = _ms_to_s(s.get("pivot_time"))
            confirm_t_raw = _ms_to_s(s.get("confirm_time"))
            pivot_p = s.get("pivot_price")
            if pivot_t_raw is None or confirm_t_raw is None or pivot_p is None:
                continue
            if first_kline_time is not None and confirm_t_raw < first_kline_time:
                continue
            pivot_t = _clip_left(pivot_t_raw)
            confirm_t = _clip_left(confirm_t_raw)
            if pivot_t == confirm_t:
                continue
            _add({
                "type": "line",
                "data": [
                    {"time": pivot_t,   "value": pivot_p},
                    {"time": confirm_t, "value": pivot_p},
                ],
                "style": {
                    "role":  "bullish" if bias == "bull" else "bearish",
                    "dash":  "dashed",
                    "width": 1,
                    "lastValueVisible": False,
                },
            })
            (bull_markers if bias == "bull" else bear_markers).append({
                "time":     confirm_t,
                "shape":    "circle",
                "size":     0,
                "position": "belowBar" if bias == "bull" else "aboveBar",
                "text":     kind,
            })
    if bull_markers:
        _add({"type": "markers", "data": bull_markers, "style": {"role": "bullish"}})
    if bear_markers:
        _add({"type": "markers", "data": bear_markers, "style": {"role": "bearish"}})

    # ----- 3. ALL active swing-level Order Blocks ------------------------
    swing_obs = objects.get("order_blocks_swing") or []
    active_swing_obs = [x for x in swing_obs if (x.get("status") or "").lower() == "active"]
    mitig_swing_obs  = [x for x in swing_obs if (x.get("status") or "").lower() == "mitigated"]
    for ob in active_swing_obs:
        bias = (ob.get("bias") or "").lower()
        _add({
            "type":         "rectangle",
            "time_start":   _clip_left(_ms_to_s(ob.get("anchor_time"))),
            "time_end":     None,
            "price_top":    ob.get("top"),
            "price_bottom": ob.get("bottom"),
            "label":        "",
            "style": {
                "role":         "ob" if bias == "bull" else "ob_bear",
                "fill_opacity": 0.22,
                "border_width": 0,
            },
        })

    # ----- 4. ALL active Fair Value Gaps --------------------------------
    fvgs = objects.get("fair_value_gaps") or []
    active_fvgs = [x for x in fvgs if (x.get("status") or "").lower() == "active"]
    mitig_fvgs  = [x for x in fvgs if (x.get("status") or "").lower() == "mitigated"]
    for fvg in active_fvgs:
        bias = (fvg.get("bias") or "").lower()
        _add({
            "type":         "rectangle",
            "time_start":   _clip_left(_ms_to_s(fvg.get("anchor_time"))),
            "time_end":     None,
            "price_top":    fvg.get("top"),
            "price_bottom": fvg.get("bottom"),
            "label":        "",
            "style": {
                "role":         "fvg" if bias == "bull" else "fvg_bear",
                "fill_opacity": 0.14,
                "border_width": 0,
            },
        })

    # ----- 5. ALL active internal Order Blocks (subtle) -----------------
    if include_internal:
        internal_obs = [
            x for x in (objects.get("order_blocks_internal") or [])
            if (x.get("status") or "").lower() == "active"
        ]
        for ob in internal_obs:
            bias = (ob.get("bias") or "").lower()
            _add({
                "type":         "rectangle",
                "time_start":   _clip_left(_ms_to_s(ob.get("anchor_time"))),
                "time_end":     None,
                "price_top":    ob.get("top"),
                "price_bottom": ob.get("bottom"),
                "label":        "",
                "style": {
                    "role":         "ob" if bias == "bull" else "ob_bear",
                    "fill_opacity": 0.08,
                    "border_width": 0,
                },
            })

    # ----- 6. Equal highs / lows: short dashed connectors with labels ----
    eqh_markers: list[dict] = []
    eql_markers: list[dict] = []
    for eq in (objects.get("equal_highs") or []):
        anchor_t_raw  = _ms_to_s(eq.get("anchor_time"))
        confirm_t_raw = _ms_to_s(eq.get("confirm_time"))
        level = eq.get("level")
        if anchor_t_raw is None or confirm_t_raw is None or level is None:
            continue
        if first_kline_time is not None and confirm_t_raw < first_kline_time:
            continue
        anchor_t  = _clip_left(anchor_t_raw)
        confirm_t = _clip_left(confirm_t_raw)
        if anchor_t == confirm_t:
            continue
        _add({
            "type": "line",
            "data": [
                {"time": anchor_t,  "value": level},
                {"time": confirm_t, "value": level},
            ],
            "style": {"role": "muted", "dash": "dashed", "width": 1, "lastValueVisible": False},
        })
        eqh_markers.append({
            "time": confirm_t, "shape": "circle", "size": 0,
            "position": "aboveBar", "text": "EQH",
        })
    for eq in (objects.get("equal_lows") or []):
        anchor_t_raw  = _ms_to_s(eq.get("anchor_time"))
        confirm_t_raw = _ms_to_s(eq.get("confirm_time"))
        level = eq.get("level")
        if anchor_t_raw is None or confirm_t_raw is None or level is None:
            continue
        if first_kline_time is not None and confirm_t_raw < first_kline_time:
            continue
        anchor_t  = _clip_left(anchor_t_raw)
        confirm_t = _clip_left(confirm_t_raw)
        if anchor_t == confirm_t:
            continue
        _add({
            "type": "line",
            "data": [
                {"time": anchor_t,  "value": level},
                {"time": confirm_t, "value": level},
            ],
            "style": {"role": "muted", "dash": "dashed", "width": 1, "lastValueVisible": False},
        })
        eql_markers.append({
            "time": confirm_t, "shape": "circle", "size": 0,
            "position": "belowBar", "text": "EQL",
        })
    if eqh_markers:
        _add({"type": "markers", "data": eqh_markers, "style": {"role": "muted"}})
    if eql_markers:
        _add({"type": "markers", "data": eql_markers, "style": {"role": "muted"}})

    # ----- 7. Mitigated OBs & FVGs (historical, very muted) -------------
    if include_mitigated:
        for mit_ob in mitig_swing_obs:
            bias = (mit_ob.get("bias") or "").lower()
            _add({
                "type":         "rectangle",
                "time_start":   _clip_left(_ms_to_s(mit_ob.get("anchor_time"))),
                "time_end":     _ms_to_s(mit_ob.get("mitigation_time")),
                "price_top":    mit_ob.get("top"),
                "price_bottom": mit_ob.get("bottom"),
                "label":        "",
                "style": {
                    "role":         "ob" if bias == "bull" else "ob_bear",
                    "fill_opacity": 0.07,
                    "border_width": 0,
                },
            })
        for mit_fvg in mitig_fvgs:
            bias = (mit_fvg.get("bias") or "").lower()
            _add({
                "type":         "rectangle",
                "time_start":   _clip_left(_ms_to_s(mit_fvg.get("anchor_time"))),
                "time_end":     _ms_to_s(mit_fvg.get("mitigation_time")),
                "price_top":    mit_fvg.get("top"),
                "price_bottom": mit_fvg.get("bottom"),
                "label":        "",
                "style": {
                    "role":         "fvg" if bias == "bull" else "fvg_bear",
                    "fill_opacity": 0.05,
                    "border_width": 0,
                },
            })

    # ----- 8. Optional premium / equilibrium / discount zone bands -------
    if include_zones:
        zone_items: list[dict] = []
        for name, role in (
            ("premium_zone",     "premium"),
            ("equilibrium_zone", "equilibrium"),
            ("discount_zone",    "discount"),
        ):
            z = objects.get(name) or {}
            top, bot = z.get("top"), z.get("bottom")
            if top is None or bot is None:
                continue
            zone_items.append({
                "type":         "rectangle",
                "time_start":   first_kline_time if first_kline_time is not None else 0,
                "time_end":     None,
                "price_top":    top,
                "price_bottom": bot,
                "label":        "",
                "style": {
                    "role":         role,
                    "fill_opacity": 0.05,
                    "border_width": 0,
                },
            })
        items = zone_items + items

    _ = current_price  # currently unused — all events shown, no distance ranking
    return items


def cmd_chart(args: argparse.Namespace) -> int:
    """Pull K-lines + SMC structural indicator → panels JSON with auto-filled
    overlay items (BOS/CHoCH connectors, Order Blocks, FVGs, equal H/L,
    trailing extremes labels) plus a volume sub-panel.

    Defaults:
      - auto-overlay: ON (--no-auto-overlay turns it off)
      - include mitigated OBs/FVGs (muted history)
      - include internal Order Blocks
      - include volume panel
      - exclude premium/discount/equilibrium zone bands (--include-zones to add)
    """
    client = MobiusClient()

    # 解析 asset（与 fetch 一致）
    if args.exchange and args.market and args.symbol:
        asset = AssetSpec(
            exchange=args.exchange,
            market=args.market,
            symbol=args.symbol,
        )
    elif args.query:
        resp = client.symbols_search(args.query)
        matches = resp.get("matches") or []
        if not matches:
            log.error("no symbols matched %r", args.query)
            return 1
        top = matches[0]
        asset = AssetSpec(
            exchange=top["exchange"],
            market=top["market"],
            symbol=top["symbol"],
            asset_class=top.get("asset_class", ""),
            display=top.get("display", ""),
        )
        log.info("resolved %r → %s:%s:%s", args.query,
                 asset.exchange, asset.market, asset.symbol)
    else:
        log.error("provide either --query, or all of --exchange/--market/--symbol")
        return 2

    # 拉纯 K 线
    resp = client.klines(
        exchange=asset.exchange,
        market=asset.market,
        symbol=asset.symbol,
        interval=args.interval,
        limit=args.limit,
    )
    rows = resp.get("data") or []
    if not rows:
        log.error("no klines returned")
        return 1
    klines = _ohlcv_to_klines(rows)
    log.info("fetched %s @ %s: %d candles", asset.symbol, args.interval, len(klines))

    # 计算 value_range（K 线 high/low ± buffer，让 hline 不能任意拉伸 priceScale）
    highs = [k["high"] for k in klines]
    lows  = [k["low"]  for k in klines]
    hi, lo = max(highs), min(lows)
    span = hi - lo
    buffer = span * args.value_range_buffer  # 默认 0.08 = 8%（上下各加）
    value_range = [lo - buffer, hi + buffer]

    # Auto-fill items from SMC indicator (default ON; --no-auto-overlay opts out)
    items: list[dict] = []
    if getattr(args, "auto_overlay", True):
        try:
            smc_resp = client.indicators(
                exchange=asset.exchange,
                market=asset.market,
                symbol=asset.symbol,
                interval=args.interval,
                calc=[{
                    "name":   "smc",
                    "params": {
                        "show_swing_points":           True,
                        "show_premium_discount_zones": True,
                    },
                }],
                limit=args.limit,
                explain=False,
            )
            current_price = smc_resp.get("current_price")
            inds = smc_resp.get("indicators") or {}
            objects = {}
            for _key, payload_ind in inds.items():
                if isinstance(payload_ind, dict) and payload_ind.get("objects"):
                    objects = payload_ind["objects"]
                    break
            if objects:
                first_kline_t = klines[0]["time"] if klines else None
                items = _smc_objects_to_items(
                    objects, current_price,
                    first_kline_time=first_kline_t,
                    include_mitigated=args.include_mitigated,
                    include_zones=args.include_zones,
                    include_internal=args.include_internal,
                    max_items=args.max_items,
                )
                log.info(
                    "auto-overlay: %d items from SMC (mitigated=%s zones=%s internal=%s)",
                    len(items), args.include_mitigated, args.include_zones, args.include_internal,
                )
            else:
                log.warning("auto-overlay: SMC response had no `objects` sidecar; items empty")
        except Exception as e:  # noqa: BLE001
            log.warning(
                "auto-overlay: SMC fetch failed (%s); falling back to empty items.", e,
            )

    # Volume panel: per-bar histogram, red/green by close vs open.
    # Placeholder-volume series (e.g. commodity:spot:XAUUSD with volume=0) skip
    # the empty sub-panel so the main chart retains full vertical space.
    volume_items: list[dict] = []
    has_nonzero_volume = any((k.get("volume") or 0) > 0 for k in klines)
    if getattr(args, "volume_panel", True) and has_nonzero_volume:
        vol_color_up   = "rgba(38, 166, 154, 0.6)"
        vol_color_down = "rgba(239, 83, 80, 0.6)"
        vol_data = [
            {
                "time":  k["time"],
                "value": k.get("volume", 0),
                "color": vol_color_up if k["close"] >= k["open"] else vol_color_down,
            }
            for k in klines
        ]
        volume_items = [{
            "type":  "histogram",
            "data":  vol_data,
            "label": "Volume",
            "style": {"role": "muted", "lastValueVisible": False},
        }]
    elif getattr(args, "volume_panel", True) and klines:
        log.info("volume panel skipped: all %d candles report zero volume", len(klines))

    panels = [
        {
            "id":           "main",
            "overlay":      True,
            "height_ratio": 4.0,
            "value_range":  value_range,
            "items":        items,
        },
    ]
    if volume_items:
        panels.append({
            "id":           "volume",
            "overlay":      False,
            "height_ratio": 1.0,
            "items":        volume_items,
        })

    payload = {
        "symbol":    asset.symbol,
        "interval":  args.interval,
        # Freshness meta — LLM MUST quote these in user-facing output instead
        # of making up timestamps. See SKILL.body.md § Freshness Mandate.
        "freshness": _compute_freshness(klines, args.interval),
        "klines":    klines,
        "panels":    panels,
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        log.info("written to %s", out_path)
        print(str(out_path))
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    return 0


# ============================================================================
# render: panels JSON → PNG (Playwright + lightweight-charts)
# ============================================================================

CHART_RENDER_DIR = Path(__file__).resolve().parent / "chart_render"


def _build_render_html(payload: dict, render_dir: Path) -> Path:
    """读模板 index.html → 注入 payload JSON → 写到临时 HTML 文件。

    放在 chart_render/ 同目录下（让相对 <script src="..."> 工作），用唯一临时文件名。
    """
    import tempfile  # noqa: PLC0415

    template_path = render_dir / "index.html"
    if not template_path.is_file():
        raise FileNotFoundError(f"chart_render template not found: {template_path}")
    template = template_path.read_text(encoding="utf-8")

    # 安全注入：JSON 转字符串 + 转义 </script>
    payload_json = json.dumps(payload, ensure_ascii=False)
    payload_json = payload_json.replace("</", "<\\/")  # 防止意外闭合 script

    placeholder = '<script id="payload" type="application/json">{"_placeholder": true}</script>'
    injected = f'<script id="payload" type="application/json">{payload_json}</script>'
    if placeholder not in template:
        raise RuntimeError("template 缺少 #payload placeholder")
    html = template.replace(placeholder, injected)

    # 临时文件在 chart_render/ 同目录（让 <script src="lightweight-charts.standalone.js"> 工作）
    fd, tmp_path_str = tempfile.mkstemp(
        prefix="render-", suffix=".html", dir=str(render_dir),
    )
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    tmp_path.write_text(html, encoding="utf-8")
    return tmp_path


def _playwright_screenshot(
    html_path: Path,
    output_png: Path,
    width: int,
    height: int,
    theme: str,
    timeout_ms: int = 15000,
) -> None:
    """加载 file://html_path → 等 chartReady → 截图 PNG。"""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError(
            "playwright 未安装。请运行：\n"
            "  .venv/bin/pip install playwright\n"
            "  .venv/bin/playwright install chromium",
        ) from e

    url = f"file://{html_path}?theme={theme}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": width, "height": height})
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_function(
                    "document.body.dataset.chartReady === 'true'",
                    timeout=timeout_ms,
                )
            except PlaywrightTimeoutError:
                # 看看页面有没有报错
                err_state = page.evaluate("document.body.dataset.chartReady")
                err_html = page.evaluate("document.body.innerHTML.slice(0, 500)")
                raise RuntimeError(
                    f"等待 chartReady 超时（状态={err_state!r}）。"
                    f"页面前 500 字符：\n{err_html}",
                ) from None
            # 让浏览器多一帧 layout 稳定
            page.wait_for_timeout(150)
            output_png.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output_png), type="png", full_page=False)
        finally:
            browser.close()


def cmd_render(args: argparse.Namespace) -> int:
    """panels JSON → PNG。

    输入支持：
      --input <path>  从 chart 的输出读（LLM 可能已往 panels[0].items 注入标注）
      stdin           不指定 --input 时
    """
    # 读 payload
    if args.input:
        raw = Path(args.input).read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()
    if not raw.strip():
        log.error("no payload input (use --input or pipe stdin)")
        return 2

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("JSON 解析失败: %s", e)
        return 1

    # 验证最小 schema
    if not isinstance(payload, dict):
        log.error("payload must be a JSON object")
        return 1
    if "panels" not in payload:
        log.error("payload 缺 'panels' 字段。请用 chart 子命令的输出（而非 fetch）")
        return 1

    # Merge trade-setup items (LLM-authored entry / SL / target hlines)
    if getattr(args, "trade_setup", None):
        setup_path = Path(args.trade_setup)
        if not setup_path.is_file():
            log.error("--trade-setup file not found: %s", setup_path)
            return 1
        try:
            setup = json.loads(setup_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log.error("--trade-setup JSON parse failed: %s", e)
            return 1
        extra_items = setup.get("items") if isinstance(setup, dict) else None
        if not isinstance(extra_items, list):
            log.error("--trade-setup must be a JSON object with an 'items' array")
            return 1
        panels = payload.get("panels") or []
        if not panels:
            log.error("--trade-setup: payload has no panels to merge into")
            return 1
        existing = panels[0].setdefault("items", [])
        existing.extend(extra_items)
        log.info("merged %d trade-setup items into panels[0]", len(extra_items))

    # 写临时 HTML
    render_dir = CHART_RENDER_DIR
    if not render_dir.is_dir():
        log.error("chart_render/ 目录不存在: %s", render_dir)
        return 1

    html_path = _build_render_html(payload, render_dir)
    log.info("temp html: %s", html_path)

    output_png = Path(args.output)
    try:
        _playwright_screenshot(
            html_path=html_path,
            output_png=output_png,
            width=args.width,
            height=args.height,
            theme=args.theme,
            timeout_ms=args.timeout_ms,
        )
    except RuntimeError as e:
        log.error("render 失败: %s", e)
        if not args.keep_html:
            html_path.unlink(missing_ok=True)
        return 1

    if not args.keep_html:
        html_path.unlink(missing_ok=True)
    else:
        log.info("保留 HTML 文件（--keep-html）: %s", html_path)

    log.info("rendered: %s (%dx%d, %s theme)",
             output_png, args.width, args.height, args.theme)
    print(str(output_png))
    return 0


# ============================================================================
# indicators: 拉技术指标数值（文本/JSON 输出，不画到图上）
# ============================================================================

# 紧凑 indicator 串 → calc list
# 'ema:50,rsi:14,macd:12:26:9,bollinger:20:2,atr:14' → list[dict]
INDS_SPECS = {
    # name: (positional_param_keys, single_int_fallback_key)
    # SMC: structural indicator (BOS/CHoCH, Order Block, FVG, equal H/L,
    # premium/discount, trailing extremes). No positional params; server
    # uses sensible defaults.
    "smc":       ([], None),
    "ema":       (["period"], "period"),
    "sma":       (["period"], "period"),
    "wma":       (["period"], "period"),
    "rsi":       (["period"], "period"),
    "atr":       (["period"], "period"),
    "stoch":     (["period", "smooth_k", "smooth_d"], "period"),
    "macd":      (["fast", "slow", "signal"], None),
    "bollinger": (["period", "std_dev"], "period"),
    "cci":       (["period"], "period"),
    "adx":       (["period"], "period"),
    "vwap":      ([], None),
    "obv":       ([], None),
    "cvd":       ([], None),
}

# When --inds is empty / not passed, this is what we fetch.
DEFAULT_INDS = "smc"


def _parse_inds(inds_str: str) -> list[dict]:
    """'ema:50,rsi:14,macd:12:26:9' → [{name, params}, ...].

    未知指标按 single-int fallback（{name: ..., params: {period: <first_num>}}）。
    """
    out: list[dict] = []
    if not inds_str:
        return out
    for tok in inds_str.split(","):
        tok = tok.strip()
        if not tok:
            continue
        parts = tok.split(":")
        name = parts[0].strip().lower()
        try:
            nums = [float(p) if "." in p else int(p) for p in parts[1:] if p.strip()]
        except ValueError:
            log.warning("indicator %r: 参数非数字，跳过", tok)
            continue
        spec = INDS_SPECS.get(name)
        params: dict = {}
        if spec:
            keys, _fallback = spec
            for i, k in enumerate(keys):
                if i < len(nums):
                    params[k] = nums[i]
        else:
            # 未知指标：单数字按 period 兜底
            if nums:
                params["period"] = nums[0]
        # SMC: enable the boolean toggles that produce the richest objects
        # sidecar. Server defaults skip these.
        if name == "smc":
            params.setdefault("show_swing_points", True)
            params.setdefault("show_premium_discount_zones", True)
        out.append({"name": name, "params": params})
    return out


def _format_objects_summary(objects: dict) -> list[str]:
    """Render the `objects` sidecar compactly (SMC indicator etc.)."""
    lines: list[str] = ["  Objects:"]

    te = objects.get("trailing_extremes") or {}
    if te:
        top = te.get("top"); bot = te.get("bottom")
        tlbl = te.get("top_label") or ""; blbl = te.get("bottom_label") or ""
        lines.append(f"    trailing_extremes: top={top} ({tlbl}) / bottom={bot} ({blbl})")

    zones = []
    for name in ("premium_zone", "equilibrium_zone", "discount_zone"):
        z = objects.get(name) or {}
        if z and z.get("top") is not None and z.get("bottom") is not None:
            zones.append(f"{name.replace('_zone','')}={z['bottom']}-{z['top']}")
    if zones:
        lines.append("    zones: " + " | ".join(zones))

    for key in (
        "swing_pivots", "swing_structures", "internal_structures",
        "equal_highs", "equal_lows",
    ):
        v = objects.get(key) or []
        if v:
            lines.append(f"    {key}: {len(v)} events")

    def _show_active(name: str, max_n: int = 3) -> None:
        items = [
            x for x in (objects.get(name) or [])
            if (x.get("status") or "").lower() == "active"
        ]
        if not items:
            return
        recent = items[-max_n:]
        line = f"    {name} (active, recent {len(recent)}/{len(items)}): " + " | ".join(
            f"[{x.get('bias','?')}] {x.get('bottom')}-{x.get('top')}"
            for x in recent
        )
        lines.append(line)

    _show_active("order_blocks_swing")
    _show_active("order_blocks_internal")
    _show_active("fair_value_gaps", max_n=5)

    alerts = objects.get("alerts_last_bar") or {}
    fired = [k for k, v in alerts.items() if v]
    if fired:
        lines.append("    alerts_last_bar (fired): " + ", ".join(fired))

    return lines


def _format_indicators_compact(resp: dict) -> str:
    """LLM 友好的简短文本摘要。

    每个指标展示：
    - 当前值（最后一根 K 线的输出列）
    - explain.category + desc（一句话概括）
    - explain.summary_focus（分析维度列表 — 这是 Mobius 的指标知识库，
      告诉 LLM 看这个指标时要按哪些维度展开）
    - explain.signals（信号判读规则，如有）
    - explain.outputs（各输出列含义，如有）

    完整数据（多根 K 线时序）请用默认 JSON 模式。
    """
    lines: list[str] = []
    sym = resp.get("symbol", "?")
    iv  = resp.get("interval", "?")
    cnt = resp.get("count", 0)
    cur = resp.get("current_price")
    lines.append(f"== {sym} @ {iv} ({cnt} candles) ==")
    # Freshness header — LLM must cite these in user-facing output.
    fr = resp.get("freshness") or {}
    if fr:
        lines.append(f"data_source:           {fr.get('data_source','?')}")
        lines.append(f"fetched_at (UTC):      {fr.get('fetched_at','?')}")
        lines.append(f"last_bar_open (UTC):   {fr.get('last_bar_open_time_utc','?')}")
        lines.append(
            f"last_bar_age:          {fr.get('last_bar_age_seconds','?')}s"
            f" (interval={fr.get('interval_seconds','?')}s, "
            f"is_stale={fr.get('is_stale','?')})"
        )
    if cur is not None:
        lines.append(f"current_price:         {cur}")
    lines.append("")

    inds = resp.get("indicators") or {}
    for key, payload in inds.items():
        cols    = payload.get("columns") or []
        data    = payload.get("data") or []
        explain = payload.get("explain") or {}
        objects = payload.get("objects") or {}
        if not data or not cols:
            lines.append(f"{key}: (no data)")
            continue
        last = data[-1]
        # 跳过第一列（open_time）
        value_pairs = [f"{c}={last[i]}" for i, c in enumerate(cols) if i > 0]
        lines.append(f"{key}:  " + ", ".join(value_pairs))

        # objects sidecar (SMC structural events etc.)
        if objects:
            lines.extend(_format_objects_summary(objects))

        # explain: category + desc
        if explain:
            cat = explain.get("category", "")
            desc = explain.get("desc") or explain.get("summary") or ""
            if cat or desc:
                # desc 第一行（避免超长）
                desc_line = (desc.splitlines()[0] if desc else "")
                head = f"  [{cat}]" if cat else "  "
                if desc_line:
                    head += f" {desc_line}"
                lines.append(head)

            # summary_focus: 分析维度（关键 — Mobius 的指标知识库）
            sf = explain.get("summary_focus") or []
            if sf:
                lines.append("  分析维度 / Analysis dimensions:")
                for item in sf:
                    # 单条不截断（这是分析指引，完整保留）
                    lines.append(f"    • {item}")

            # outputs: 各列含义（如果非空且有内容）
            outs = explain.get("outputs") or {}
            if outs:
                lines.append("  输出列含义 / Output columns:")
                for col, meaning in outs.items():
                    if meaning:
                        # 单行截断 200 字
                        m = str(meaning).splitlines()[0][:200]
                        lines.append(f"    • {col}: {m}")

            # signals: 信号规则（如果非空且有内容）
            sigs = explain.get("signals") or {}
            if sigs:
                lines.append("  信号规则 / Signal rules:")
                for sig_name, sig_def in sigs.items():
                    s = str(sig_def).splitlines()[0][:200]
                    lines.append(f"    • {sig_name}: {s}")

        lines.append("")  # 指标之间留空行分隔

    return "\n".join(lines)


def cmd_indicators(args: argparse.Namespace) -> int:
    """拉技术指标数值。默认 JSON 输出；--format compact 给文本摘要。"""
    client = MobiusClient()

    # 解析 asset
    if args.exchange and args.market and args.symbol:
        asset = AssetSpec(
            exchange=args.exchange, market=args.market, symbol=args.symbol,
        )
    elif args.query:
        sresp = client.symbols_search(args.query)
        matches = sresp.get("matches") or []
        if not matches:
            log.error("no symbols matched %r", args.query)
            return 1
        top = matches[0]
        asset = AssetSpec(
            exchange=top["exchange"], market=top["market"], symbol=top["symbol"],
            asset_class=top.get("asset_class", ""), display=top.get("display", ""),
        )
        log.info("resolved %r → %s:%s:%s", args.query,
                 asset.exchange, asset.market, asset.symbol)
    else:
        log.error("provide either --query, or all of --exchange/--market/--symbol")
        return 2

    # 解析 inds —— 默认 SMC 结构指标
    inds_str = (args.inds or "").strip() or DEFAULT_INDS
    calc = _parse_inds(inds_str)
    if not calc:
        log.error("could not parse --inds %r", inds_str)
        return 2
    log.info("indicators: %s", [c["name"] for c in calc])

    # explain block is always suppressed; field semantics live in SKILL.body.md
    volume_required_inds = frozenset({"vwap", "obv", "cvd"})
    requested_names = {c["name"] for c in calc}
    try:
        resp = client.indicators(
            exchange=asset.exchange,
            market=asset.market,
            symbol=asset.symbol,
            interval=args.interval,
            calc=calc,
            limit=args.limit,
            explain=False,
        )
    except MobiusError as exc:
        if exc.status == 400 and (requested_names & volume_required_inds):
            log.error(
                "volume-based indicator(s) %s rejected by API (%s) — "
                "placeholder-volume series such as commodity:spot:XAUUSD do not "
                "carry real traded volume",
                sorted(requested_names & volume_required_inds),
                exc.detail,
            )
        raise
    log.info("got %s @ %s: %d candles, %d indicators",
             asset.symbol, args.interval,
             resp.get("count", 0), len(resp.get("indicators") or {}))

    # Inject freshness metadata — LLM MUST quote these instead of inventing
    # timestamps. See SKILL.body.md § Freshness Mandate.
    klines_for_meta = []
    for row in (resp.get("klines") or []):
        if isinstance(row, list) and len(row) >= 1:
            klines_for_meta.append({"time": int(row[0]) // 1000})  # ms→s
    resp["freshness"] = _compute_freshness(klines_for_meta, args.interval)

    # 输出
    if args.format == "compact":
        text = _format_indicators_compact(resp)
    else:
        text = json.dumps(resp, ensure_ascii=False, indent=2)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        log.info("written to %s", out_path)
        print(str(out_path))
    else:
        sys.stdout.write(text)
        if args.format != "compact":
            sys.stdout.write("\n")
    return 0


# ============================================================================
# CLI
# ============================================================================

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-5s %(message)s",
    )

    p = argparse.ArgumentParser(
        prog="kb_klines",
        description="Mobius Quant Engine Kline 接入工具",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # resolve
    p_resolve = sub.add_parser("resolve", help="自然名 → canonical asset spec")
    p_resolve.add_argument("query", help="自然名（如 '比特币' / 'BTC' / '茅台'）")
    p_resolve.add_argument("--show-all", action="store_true",
                           help="同时输出所有匹配项")
    p_resolve.set_defaults(func=cmd_resolve)

    # fetch
    p_fetch = sub.add_parser("fetch", help="拉 OHLCV + 可选 HTF/指标")
    grp = p_fetch.add_mutually_exclusive_group()
    grp.add_argument("--query", help="自然名（与 --symbol 二选一）")
    p_fetch.add_argument("--exchange", help="venue exchange")
    p_fetch.add_argument("--market", help="venue market")
    p_fetch.add_argument("--symbol", help="canonical symbol")
    p_fetch.add_argument("--interval", required=True,
                         choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"])
    p_fetch.add_argument("--limit", type=int, default=300,
                          help="主 timeframe 数量（默认 300，给 SMC ATR(200) 留暖机）")
    p_fetch.add_argument("--with-htf", action="store_true",
                         help="自动联拉上一档 HTF")
    p_fetch.add_argument("--htf-limit", type=int, default=100,
                         help="HTF 数量 (默认 100)")
    p_fetch.add_argument("--output", "-o", help="输出文件；不指定则 stdout")
    p_fetch.set_defaults(func=cmd_fetch)

    # parse
    p_parse = sub.add_parser("parse", help="用户粘贴文本 → 标准 OHLCV JSON")
    p_parse.add_argument("--input", "-i", help="输入文件；省略时读 stdin")
    p_parse.add_argument("--output", "-o", help="输出 JSON 文件；省略时 stdout")
    p_parse.add_argument("--symbol", help="标注资产 symbol（可选元数据）")
    p_parse.add_argument("--exchange", help="venue exchange（可选）")
    p_parse.add_argument("--market", help="venue market（可选）")
    p_parse.add_argument("--interval", help="K 线周期（可选元数据）")
    p_parse.set_defaults(func=cmd_parse)

    # analyze
    p_analyze = sub.add_parser("analyze", help="OHLCV → 特征摘要")
    p_analyze.add_argument("--input", "-i",
                           help="输入 JSON 文件（fetch 输出格式或单 KlineData dict）；省略时读 stdin")
    p_analyze.add_argument("--output", "-o", help="输出文件；省略时 stdout")
    p_analyze.add_argument(
        "--format", default="text", choices=["text", "json"],
        help="text: LLM 友好的文本摘要（默认）；json: 结构化 features (含 suggested_overlay_items)",
    )
    p_analyze.set_defaults(func=cmd_analyze)

    # chart
    p_chart = sub.add_parser(
        "chart",
        help="K-lines + auto-filled SMC structural overlay + volume sub-panel "
             "→ panels JSON. Append trade-setup hlines at render time via --trade-setup.",
    )
    p_chart.add_argument("--query", help="自然名（与 --symbol 二选一）")
    p_chart.add_argument("--exchange", help="venue exchange")
    p_chart.add_argument("--market", help="venue market")
    p_chart.add_argument("--symbol", help="canonical symbol")
    p_chart.add_argument("--interval", required=True,
                         choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"])
    p_chart.add_argument("--limit", type=int, default=300,
                         help="K 线数量（默认 300，给 SMC ATR(200) 留暖机）")
    p_chart.add_argument(
        "--value-range-buffer", type=float, default=0.08,
        help="value_range 在 K 线 high/low 基础上加的 buffer（默认 0.08 = 8%%；"
             "trade setup hline 在此范围内不会拉伸 priceScale，K 线保持稠密）",
    )
    # Auto-overlay flags
    p_chart.add_argument(
        "--no-auto-overlay", dest="auto_overlay", action="store_false",
        default=True,
        help="Disable auto-population of overlay items from the SMC indicator.",
    )
    p_chart.add_argument(
        "--max-items", type=int, default=400,
        help="Safety cap on auto-filled overlay items (default 400 — generous).",
    )
    p_chart.add_argument(
        "--no-include-mitigated", dest="include_mitigated", action="store_false",
        default=True,
        help="Exclude mitigated Order Blocks / FVGs (default: included with muted style).",
    )
    p_chart.add_argument(
        "--include-zones", dest="include_zones", action="store_true",
        default=False,
        help="Include premium / equilibrium / discount background bands "
             "(default OFF — TV-style chart doesn't draw these).",
    )
    p_chart.add_argument(
        "--no-include-internal", dest="include_internal", action="store_false",
        default=True,
        help="Exclude internal-structure events (BOS/CHoCH connectors + "
             "internal OBs). Default: included.",
    )
    p_chart.add_argument(
        "--no-volume-panel", dest="volume_panel", action="store_false",
        default=True,
        help="Disable the volume sub-panel (default: ON, TV-style).",
    )
    p_chart.add_argument("--output", "-o", help="输出 JSON 文件；省略 stdout")
    p_chart.set_defaults(func=cmd_chart)

    # render
    p_render = sub.add_parser(
        "render",
        help="panels JSON → PNG (Playwright + lightweight-charts)",
    )
    p_render.add_argument("--input", "-i",
                          help="输入 panels JSON 文件（chart 子命令输出，可由 LLM 注入 items）；省略读 stdin")
    p_render.add_argument("--output", "-o", required=True, help="输出 PNG 路径")
    p_render.add_argument("--width", type=int, default=1280, help="图像宽度（默认 1280）")
    p_render.add_argument("--height", type=int, default=800, help="图像高度（默认 800）")
    p_render.add_argument("--theme", default="light", choices=["dark", "light"])
    p_render.add_argument("--timeout-ms", type=int, default=15000,
                          help="等待 chartReady 的超时（默认 15000ms）")
    p_render.add_argument("--keep-html", action="store_true",
                          help="保留临时 HTML 文件（debug 用）")
    p_render.add_argument(
        "--trade-setup", default=None,
        help="Optional JSON file with extra panels[0].items to merge in "
             "(e.g. entry / SL / target hlines). Schema: "
             '{"items": [{"type": "hline", "value": ..., "label": ..., '
             '"style": {"role": "entry_short", "width": 2}}, ...]}',
    )
    p_render.set_defaults(func=cmd_render)

    # indicators (default SMC; user-named indicators pass through)
    p_inds = sub.add_parser(
        "indicators",
        help="Fetch indicator output. Default: SMC structural indicator. "
             "Pass --inds <name[:params]> only when the user named one.",
    )
    p_inds.add_argument("--query", help="自然名（与 --symbol 二选一）")
    p_inds.add_argument("--exchange", help="venue exchange")
    p_inds.add_argument("--market", help="venue market")
    p_inds.add_argument("--symbol", help="canonical symbol")
    p_inds.add_argument("--interval", required=True,
                        choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d"])
    p_inds.add_argument(
        "--inds", default="",
        help="Indicator spec. Default empty → SMC structural indicator.",
    )
    p_inds.add_argument("--limit", type=int, default=300,
                        help="K 线数量（默认 300，给 ATR(200) 等长周期指标暖机）")
    p_inds.add_argument(
        "--format", default="json", choices=["json", "compact"],
        help="json: 完整原始响应（默认）；compact: LLM 友好的当前值摘要",
    )
    p_inds.add_argument("--output", "-o", help="output file; omit for stdout")
    p_inds.set_defaults(func=cmd_indicators)

    args = p.parse_args()
    try:
        return args.func(args)
    except MobiusError as e:
        log.error("API error: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
