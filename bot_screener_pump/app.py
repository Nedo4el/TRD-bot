"""Accumulation Screener — Streamlit Dashboard.

Запуск:  streamlit run bot_screener_pump/app.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR))

from bot_screener_pump.scanner import (
    AccumulationConfig,
    AccumulationSignal,
    analyze_symbol,
)
from core.config import load_bot_env

load_bot_env(_BOT_DIR)


# ============================================================
# Сканер
# ============================================================


def _build_config() -> AccumulationConfig:
    """Построить конфиг из .env + sidebar-параметров."""
    return AccumulationConfig(
        analysis_period=st.session_state.get("analysis_period", 100),
        volume_lookback=st.session_state.get("volume_lookback", 20),
        obv_divergence_lookback=st.session_state.get("obv_lookback", 30),
        bb_period=st.session_state.get("bb_period", 20),
        bb_dev=st.session_state.get("bb_dev", 2.0),
        rsi_period=st.session_state.get("rsi_period", 14),
        smart_money_lookback=st.session_state.get("smart_money_lookback", 7),
        max_price=st.session_state.get("max_price", 0.03),
        min_total_volume_usd=st.session_state.get("min_total_volume_usd", 50_000_000),
        range_max=st.session_state.get("range_max", 999.0),
        range_min=st.session_state.get("range_min", 0.0),
        volume_spike=st.session_state.get("volume_spike", 0.0),
        bb_compression=st.session_state.get("bb_compression", 999.0),
        min_volume_usd=st.session_state.get("min_volume_usd", 0.0),
        min_trades=st.session_state.get("min_trades", 0),
        max_spread=st.session_state.get("max_spread", 999.0),
        min_days_listed=st.session_state.get("min_days_listed", 0),
        max_volatility_3d=st.session_state.get("max_volatility_3d", 999.0),
        max_drop_7d=st.session_state.get("max_drop_7d", 999.0),
        min_pump_probability=st.session_state.get("min_probability", 0.0),
        weight_range=st.session_state.get("w_range", 0.0),
        weight_volume=st.session_state.get("w_volume", 0.0),
        weight_obv=st.session_state.get("w_obv", 0.0),
        weight_bb=st.session_state.get("w_bb", 0.0),
        weight_smart_money=st.session_state.get("w_smart", 0.0),
        weight_outflow=st.session_state.get("w_outflow", 0.0),
        weight_rsi=st.session_state.get("w_rsi", 0.0),
        weight_liquidity=st.session_state.get("w_liquidity", 0.0),
    )


def run_scan(cfg: AccumulationConfig) -> tuple[list[AccumulationSignal], float, int, int]:
    """Запустить сканирование (синхронная обёртка над async)."""
    from bot_screener_klin.fetcher import Fetcher

    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    testnet = os.getenv("TESTNET", "true").lower() == "true"

    fetcher = Fetcher(api_key=api_key, api_secret=api_secret, testnet=testnet)

    async def _scan() -> tuple[list[AccumulationSignal], float, int, int]:
        start = time.monotonic()

        raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
        exclude = {s.strip() for s in raw_exclude.split(",") if s.strip()}
        min_turnover = st.session_state.get("min_turnover", 1_000_000)
        timeframe = st.session_state.get("timeframe", "D")
        lookback = st.session_state.get("lookback", 100)

        filtered = await fetcher.get_filtered_symbols(min_turnover)
        total = len(await fetcher.get_all_linear_symbols())
        filtered = [(s, t) for s, t in filtered if s not in exclude]

        results: list[AccumulationSignal] = []
        spread_cache: dict[str, float] = {}

        batch_size = 10
        for i in range(0, len(filtered), batch_size):
            batch = filtered[i : i + batch_size]
            tasks = []
            for sym, _ in batch:
                tasks.append(_scan_one(fetcher, sym, timeframe, lookback, cfg, spread_cache))
            batch_results = await asyncio.gather(*tasks)
            for r in batch_results:
                if r and r.pump_probability >= cfg.min_pump_probability:
                    results.append(r)

        results.sort(key=lambda r: r.pump_probability, reverse=True)
        elapsed = time.monotonic() - start
        return results, elapsed, total, len(filtered)

    return asyncio.run(_scan())


async def _scan_one(
    fetcher: object,
    symbol: str,
    timeframe: str,
    lookback: int,
    cfg: AccumulationConfig,
    spread_cache: dict[str, float],
) -> AccumulationSignal | None:
    """Просканировать один символ."""
    from bot_screener_klin.fetcher import Fetcher

    assert isinstance(fetcher, Fetcher)
    candles = await fetcher.get_klines(symbol=symbol, interval=timeframe, limit=lookback)
    if not candles:
        return None

    spread = spread_cache.get(symbol, 0.0)
    if spread == 0.0:
        spread = await fetcher.get_spread(symbol)
        spread_cache[symbol] = spread

    return analyze_symbol(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        cfg=cfg,
        spread_pct=spread,
    )


def get_candles(symbol: str, timeframe: str, limit: int) -> list[dict]:
    """Получить свечи для графика."""
    from bot_screener_klin.fetcher import Fetcher

    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    testnet = os.getenv("TESTNET", "true").lower() == "true"

    fetcher = Fetcher(api_key=api_key, api_secret=api_secret, testnet=testnet)
    return asyncio.run(fetcher.get_klines(symbol=symbol, interval=timeframe, limit=limit))


# ============================================================
# Sidebar
# ============================================================

st.set_page_config(
    page_title="Accumulation Screener",
    page_icon=" ",
    layout="wide",
)

st.sidebar.title("⚙️ Настройки")

st.session_state["timeframe"] = st.sidebar.selectbox("Таймфрейм", ["D", "4h", "1h"], index=0)
st.session_state["lookback"] = st.sidebar.slider("Свечей", 50, 200, 100)
st.session_state["min_turnover"] = st.sidebar.number_input(
    "Мин. оборот 24ч ($)", value=1_000_000, step=500_000
)
st.session_state["min_probability"] = st.sidebar.slider(
    "Мин. вероятность (%)", 0, 80, 0
) / 100
st.session_state["max_price"] = st.sidebar.number_input(
    "Макс. цена ($)", value=0.03, step=0.005, format="%.4f"
)
st.session_state["min_total_volume_usd"] = st.sidebar.number_input(
    "Мин. оборот за период ($)", value=50_000_000, step=10_000_000, format="%.0f"
)

st.sidebar.markdown("---")
st.sidebar.subheader("Веса факторов")

col1, col2 = st.sidebar.columns(2)
with col1:
    st.session_state["w_range"] = st.number_input("Диапазон", 0.0, 1.0, 0.0, 0.05)
    st.session_state["w_volume"] = st.number_input("Объем", 0.0, 1.0, 0.0, 0.05)
    st.session_state["w_obv"] = st.number_input("OBV", 0.0, 1.0, 0.0, 0.05)
    st.session_state["w_bb"] = st.number_input("BB", 0.0, 1.0, 0.0, 0.05)
with col2:
    st.session_state["w_smart"] = st.number_input("Smart $", 0.0, 1.0, 0.0, 0.05)
    st.session_state["w_outflow"] = st.number_input("Отток", 0.0, 1.0, 0.0, 0.05)
    st.session_state["w_rsi"] = st.number_input("RSI", 0.0, 1.0, 0.0, 0.05)
    st.session_state["w_liquidity"] = st.number_input("Ликв.", 0.0, 1.0, 0.0, 0.05)

st.sidebar.markdown("---")
auto_refresh = st.sidebar.checkbox("Авто-обновление", value=False)
refresh_interval = st.sidebar.slider("Интервал (сек)", 30, 3600, 60) if auto_refresh else 0


# ============================================================
# Основной контент
# ============================================================

st.title("  Accumulation Screener")
st.caption("Поиск накопления крупных игроков — 8 факторов, вероятность пампа")

# Кнопка сканирования
if st.button("  Запустить сканирование", type="primary", use_container_width=True):
    with st.spinner("Сканирование... (~10 сек)"):
        cfg = _build_config()
        results, elapsed, total, filtered = run_scan(cfg)

    st.session_state["results"] = results
    st.session_state["elapsed"] = elapsed
    st.session_state["total"] = total
    st.session_state["filtered"] = filtered

# Результаты
if "results" in st.session_state:
    results = st.session_state["results"]
    elapsed = st.session_state["elapsed"]
    total = st.session_state["total"]
    filtered = st.session_state["filtered"]

    # Метрики
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Сигналов", len(results))
    c2.metric("Пар просканировано", filtered)
    c3.metric("Всего пар", total)
    c4.metric("Время", f"{elapsed:.1f}с")

    if not results:
        st.info("Сигналов не найдено. Попробуйте снизить MIN_PROBABILITY.")
    else:
        # Таблица результатов
        df = pd.DataFrame(
            [
                {
                    "Монета": r.symbol,
                    "Цена": r.price,
                    "Диапазон %": r.range_pct,
                    "Объем (ср.)": r.avg_volume,
                    "B/S": r.buy_sell_ratio,
                    "F1_range": r.f_range,
                    "F2_volume": r.f_volume,
                    "F3_obv": r.f_obv,
                    "F4_bb": r.f_bb,
                    "F5_smart": r.f_smart_money,
                    "F6_outflow": r.f_outflow,
                    "F7_rsi": r.f_rsi,
                    "F8_liq": r.f_liquidity,
                    "Вероятность %": round(r.pump_probability * 100, 1),
                    "Статус": r.status,
                }
                for r in results
            ]
        )

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Цена": st.column_config.NumberColumn(format="%.4f"),
                "Объем (ср.)": st.column_config.NumberColumn(format="$%.0f"),
                "B/S": st.column_config.NumberColumn(format="%.2f"),
                "Вероятность %": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%.1f%%"
                ),
            },
        )

        # Детализация по каждой монете
        st.markdown("---")
        st.subheader("Детализация сигналов")

        for r in results:
            with st.expander(f"**{r.symbol}** — {r.pump_probability:.1%} — {r.status}"):
                mc1, mc2, mc3, mc4 = st.columns(4)
                mc1.metric("Цена", f"${r.price:.4f}" if r.price < 1 else f"${r.price:.2f}")
                mc2.metric("Диапазон", f"{r.range_pct}%")
                mc3.metric("B/S Ratio", f"{r.buy_sell_ratio:.2f}")
                mc4.metric("Вероятность", f"{r.pump_probability:.1%}")

                # Факторы
                factor_df = pd.DataFrame(
                    [
                        {"Фактор": "1. Диапазон", "Балл": r.f_range, "Вес": "15%"},
                        {"Фактор": "2. Объем", "Балл": r.f_volume, "Вес": "20%"},
                        {"Фактор": "3. OBV", "Балл": r.f_obv, "Вес": "15%"},
                        {"Фактор": "4. BB", "Балл": r.f_bb, "Вес": "10%"},
                        {"Фактор": "5. Smart Money", "Балл": r.f_smart_money, "Вес": "15%"},
                        {"Фактор": "6. Отток", "Балл": r.f_outflow, "Вес": "10%"},
                        {"Фактор": "7. RSI", "Балл": r.f_rsi, "Вес": "5%"},
                        {"Фактор": "8. Ликвидность", "Балл": r.f_liquidity, "Вес": "10%"},
                    ]
                )

                st.dataframe(
                    factor_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Балл": st.column_config.ProgressColumn(
                            min_value=0, max_value=1, format="%.2f"
                        )
                    },
                )

                # График цены
                st.markdown("**Цена (60 дней):**")
                candles = get_candles(r.symbol, st.session_state.get("timeframe", "D"), 60)
                if candles:
                    chart_df = pd.DataFrame(
                        [
                            {
                                "Дата": pd.to_datetime(c["open_time"], unit="ms"),
                                "Close": c["close"],
                                "Volume": c["volume"],
                            }
                            for c in candles
                        ]
                    )
                    chart_df = chart_df.set_index("Дата")
                    st.line_chart(chart_df["Close"])

# Автообновление
if auto_refresh:
    time.sleep(refresh_interval)
    st.rerun()
