"""Streamlit dashboard для TRD Bot.

Запуск:
    streamlit run app.py
    streamlit run app.py --server.port 8501
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="TRD Bot Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

PROJECT_ROOT = Path(__file__).resolve().parent


# ============================================================
# Утилиты
# ============================================================


def load_state() -> dict:
    """Загрузить состояние из data/state.json."""
    state_path = PROJECT_ROOT / "data" / "state.json"
    if not state_path.exists():
        return {"positions": {}, "processed_order_ids": []}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError):
        return {"positions": {}, "processed_order_ids": []}


def load_env_file(bot_dir: Path) -> dict[str, str]:
    """Прочитать .env файл и вернуть dict."""
    env_path = bot_dir / ".env"
    if not env_path.exists():
        return {}
    result = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def get_bot_dirs() -> list[Path]:
    """Найти все папки ботов."""
    bots = []
    for d in sorted(PROJECT_ROOT.iterdir()):
        if d.is_dir() and d.name.startswith("bot_"):
            bots.append(d)
    return bots


def format_usd(value: float) -> str:
    """Форматировать число как USD."""
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.2f}"


def format_pnl(pnl: float) -> str:
    """Форматировать PnL с цветом."""
    if pnl > 0:
        return f"+{pnl:.4f}"
    return f"{pnl:.4f}"


# ============================================================
# Боковая панель
# ============================================================


NAV_ITEMS = [
    "📊 Обзор",
    "—— ТОРГОВЫЕ БОТЫ ——",
    "  Flat",
    "  Yrovni",
    "  Trend",
    "  Impulse",
    "  Bot 0",
    "—— СКРИНЕРЫ ——",
    "  Сжатие (uzkiy)",
    "  Накопление (pump)",
    "  Volume Profile",
    "  Круглые числа",
    "  Импульс (impulse)",
    "—— ИНСТРУМЕНТЫ ——",
    "  Бэктест",
    "  Настройки",
]


def sidebar() -> str:
    """Боковая панель навигации."""
    with st.sidebar:
        st.title("📊 TRD Bot")
        st.caption("Bybit v5 Trading Dashboard")
        st.divider()

        page = st.radio(
            "Навигация",
            NAV_ITEMS,
            label_visibility="collapsed",
        )

        st.divider()

        env = load_env_file(PROJECT_ROOT)
        testnet = env.get("TESTNET", "true").lower() == "true"
        api_key_set = bool(env.get("BYBIT_API_KEY"))

        st.markdown("**Статус**")
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Сеть", "Testnet" if testnet else "Mainnet")
        with col2:
            st.metric("API ключ", "✓" if api_key_set else "✗")

        st.divider()
        st.caption(
            f"Обновлено: {datetime.now(MSK).strftime('%H:%M:%S MSK')}"
        )

    return page


# ============================================================
# Страница: Обзор
# ============================================================


def page_overview() -> None:
    """Главная страница:.positions, цены, метрики."""
    st.title("🏠 Обзор")

    # Загружаем состояние
    state = load_state()
    positions = state.get("positions", {})

    # --- Метрики вверху ---
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            "Открытых позиций",
            len(positions),
        )
    with col2:
        total_unrealised = sum(
            float(p.get("unrealised_pnl", 0))
            for p in positions.values()
            if isinstance(p, dict)
        )
        st.metric(
            "Нереализованный PnL",
            format_pnl(total_unrealised),
            delta=None,
        )
    with col3:
        orders_count = len(state.get("processed_order_ids", []))
        st.metric("Обработано ордеров", orders_count)
    with col4:
        st.metric("Скринеров", 4)

    st.divider()

    # --- Открытые позиции ---
    st.subheader("📋 Открытые позиции")

    if not positions:
        st.info("Нет открытых позиций")
    else:
        for symbol, pos in positions.items():
            if not isinstance(pos, dict):
                continue
            with st.expander(f"**{symbol}** — {pos.get('side', '?')}", expanded=True):
                c1, c2, c3, c4, c5 = st.columns(5)
                with c1:
                    st.metric("Сторона", pos.get("side", "-"))
                with c2:
                    st.metric("Размер", pos.get("qty", "-"))
                with c3:
                    st.metric("Цена входа", f"{float(pos.get('entry_price', 0)):.4f}")
                with c4:
                    st.metric("SL", f"{float(pos.get('stop_loss', 0)):.4f}")
                with c5:
                    st.metric("TP", f"{float(pos.get('take_profit', 0)):.4f}")

    st.divider()

    # --- Боты ---
    st.subheader("🤖 Боты")

    bot_dirs = get_bot_dirs()
    cols = st.columns(min(len(bot_dirs), 4))

    for i, bot_dir in enumerate(bot_dirs):
        col = cols[i % len(cols)]
        with col:
            env = load_env_file(bot_dir)
            bot_name = bot_dir.name
            testnet = env.get("TESTNET", "true").lower() == "true"
            simulation = env.get("SIMULATION_MODE", "true").lower() == "true"
            symbol = env.get("SYMBOL", "—")

            status_color = "🟢" if not simulation else "🟡"
            network = "mainnet" if not testnet else "testnet"

            st.markdown(
                f"{status_color} **{bot_name}**  \n"
                f"`: {symbol}` | `{network}` | "
                f"{'simulation' if simulation else 'live'}"
            )

    # --- Быстрый доступ к логам ---
    st.divider()
    st.subheader("📄 Последние логи")

    logs_dir = PROJECT_ROOT / "logs"
    if logs_dir.exists():
        log_files = sorted(
            logs_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True
        )
        if log_files:
            selected_log = st.selectbox(
                "Выберите лог", [f.name for f in log_files[:10]]
            )
            if selected_log:
                log_path = logs_dir / selected_log
                log_content = log_path.read_text(encoding="utf-8", errors="replace")
                lines = log_content.strip().splitlines()
                # Показываем последние 100 строк
                st.code("\n".join(lines[-100:]), language=None)
        else:
            st.info("Логи не найдены")
    else:
        st.info("Папка logs/ не существует")


# ============================================================
# Страница: Скринеры
# ============================================================


def page_screeners() -> None:
    """Страница скринеров: запуск и результаты."""
    st.title("🔍 Скринеры")

    tab1, tab2, tab3, tab4 = st.tabs(
        ["Сжатие (uzkiy)", "Накопление (pump)", "Volume Profile", "Круглые числа"]
    )

    with tab1:
        st.markdown(
            "**Скринер сужения диапазона** — детектирует клины, треугольники, вымпелы."
        )
        _run_screener_tab("bot_screener_klin")

    with tab2:
        st.markdown("**Скринер накопления** — ищет сигналы накопления перед ростом.")
        _run_screener_tab("bot_screener_pump")

    with tab3:
        st.markdown("**Скринер Volume Profile** — анализирует профиль объёма.")
        _run_screener_tab("bot_screener_yrovni")

    with tab4:
        st.markdown("**Скринер круглых чисел** — психологические уровни.")
        _run_screener_tab("bot_screener_krugloe")


def page_bot(bot_name: str) -> None:
    """Страница одного бота."""
    bot_dir = PROJECT_ROOT / bot_name

    if not bot_dir.exists():
        st.warning(f"Папка {bot_name} не найдена")
        return

    env = load_env_file(bot_dir)

    st.title(f"🤖 {bot_name}")
    st.caption(f"Настройки и статус бота {bot_name}")

    col1, col2, col3 = st.columns(3)

    with col1:
        testnet = env.get("TESTNET", "true").lower() == "true"
        st.metric("Сеть", "Testnet" if testnet else "Mainnet")

    with col2:
        simulation = env.get("SIMULATION_MODE", "true").lower() == "true"
        st.metric("Режим", "Simulation" if simulation else "Live")

    with col3:
        st.metric("Пара", env.get("SYMBOL", "—"))

    st.divider()

    # Основные параметры
    st.subheader("⚙️ Параметры стратегии")

    c1, c2, c3, c4 = st.columns(4)

    with c1:
        st.metric("Таймфрейм", env.get("TIMEFRAME", "15"))

    with c2:
        st.metric("Позиция", f"{env.get('POSITION_PCT', '10')}%")

    with c3:
        st.metric("Stop Loss", f"{env.get('STOP_LOSS_PCT', '2')}%")

    with c4:
        st.metric("Take Profit", f"{env.get('TAKE_PROFIT_PCT', '4')}%")

    # Все переменные
    with st.expander("🔑 Все переменные .env"):
        st.json(env)

    # Логи бота
    st.divider()
    st.subheader("📄 Логи")

    logs_dir = PROJECT_ROOT / "logs"
    if logs_dir.exists():
        log_files = sorted(
            logs_dir.glob(f"{bot_name}*.log"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if log_files:
            selected_log = st.selectbox(
                "Выберите лог",
                [f.name for f in log_files[:5]],
                key=f"bot_log_{bot_name}",
            )
            if selected_log:
                log_path = logs_dir / selected_log
                log_content = log_path.read_text(encoding="utf-8", errors="replace")
                lines = log_content.strip().splitlines()
                st.code("\n".join(lines[-100:]), language=None)
        else:
            st.info("Логи не найдены")
    else:
        st.info("Папка logs/ не существует")


def _run_screener_tab(bot_name: str) -> None:
    """Общая логика для вкладки скринера."""
    bot_dir = PROJECT_ROOT / bot_name

    if not bot_dir.exists():
        st.warning(f"Папка {bot_name} не найдена")
        return

    env = load_env_file(bot_dir)

    # Параметры сканирования
    with st.expander("⚙️ Параметры", expanded=False):
        col1, col2, col3 = st.columns(3)
        with col1:
            min_score = st.slider(
                "Минимальный Score", 0, 100,
                value=st.session_state.get(f"{bot_name}_score", 30),
                key=f"{bot_name}_score",
            )
        with col2:
            max_price = st.number_input(
                "Макс. цена ($)",
                value=float(st.session_state.get(
                    f"{bot_name}_price",
                    env.get("MAX_PRICE", "100"),
                )),
                key=f"{bot_name}_price",
            )
        with col3:
            scan_limit = st.number_input(
                "Лимит пар",
                value=int(st.session_state.get(
                    f"{bot_name}_limit",
                    env.get("SCAN_LIMIT", "100"),
                )),
                min_value=10,
                max_value=500,
                key=f"{bot_name}_limit",
            )

        col4, col5, col6 = st.columns(3)
        with col4:
            min_turnover = st.number_input(
                "Мин. оборот 24ч ($)",
                value=float(st.session_state.get(
                    f"{bot_name}_turnover",
                    env.get("MIN_TURNOVER_24H", "1000000"),
                )),
                min_value=0.0,
                max_value=300_000_000.0,
                step=100_000.0,
                format="%.0f",
                key=f"{bot_name}_turnover",
            )
        with col5:
            timeframe_options = ["1", "3", "5", "15", "30", "60", "120", "240", "D"]
            default_tf = st.session_state.get(
                f"{bot_name}_tf",
                env.get("TIMEFRAME", "5"),
            )
            tf_idx = (
                timeframe_options.index(default_tf)
                if default_tf in timeframe_options
                else 2
            )
            timeframe = st.selectbox(
                "Таймфрейм",
                timeframe_options,
                index=tf_idx,
                key=f"{bot_name}_tf",
            )
        with col6:
            lookback = st.number_input(
                "Свечей",
                value=int(st.session_state.get(
                    f"{bot_name}_lookback",
                    env.get("LOOKBACK_BARS", "50"),
                )),
                min_value=20,
                max_value=500,
                key=f"{bot_name}_lookback",
            )

    # Мин. % импульса (только для impulse)
    min_range_pct = 0.0
    freshness = 3
    if bot_name == "bot_screener_impulse":
        min_range_pct = st.slider(
            "Мин. % импульса",
            min_value=0.0,
            max_value=20.0,
            value=float(st.session_state.get(
                f"{bot_name}_range",
                env.get("MIN_RANGE_PCT", "3.0"),
            )),
            step=0.5,
            format="%.1f%%",
            key=f"{bot_name}_range",
        )
        freshness = st.slider(
            "Свежесть (N свечей)",
            min_value=1,
            max_value=20,
            value=int(st.session_state.get(f"{bot_name}_fresh", 3)),
            key=f"{bot_name}_fresh",
        )

    # Live-режим (impulse + uzkiy)
    live_mode = False
    scan_interval = 30
    if bot_name in ("bot_screener_impulse", "bot_screener_uzkiy"):
        st.divider()
        col_live, col_interval = st.columns(2)
        with col_live:
            live_mode = st.toggle(
                "🔴 Live (автосканирование)", key=f"{bot_name}_live"
            )
        with col_interval:
            scan_interval = st.number_input(
                "Интервал (сек)",
                value=30,
                min_value=10,
                max_value=300,
                key=f"{bot_name}_interval",
            )

    # Кнопка запуска
    if st.button(f"▶️ Запустить {bot_name}", key=f"run_{bot_name}"):
        with st.spinner("Сканирование..."):
            results = _run_screener_async(
                bot_name, min_score, max_price, scan_limit,
                min_turnover, timeframe, lookback, min_range_pct,
                freshness,
            )
            if results:
                if bot_name in ("bot_screener_impulse", "bot_screener_uzkiy"):
                    # В режиме live — накапливаем результаты
                    prev = st.session_state.get(f"results_{bot_name}", [])
                    seen = {r["symbol"] for r in prev}
                    for r in results:
                        if r["symbol"] not in seen:
                            prev.append(r)
                            seen.add(r["symbol"])
                    st.session_state[f"results_{bot_name}"] = prev
                else:
                    st.session_state[f"results_{bot_name}"] = results

    # Показать результаты
    results = st.session_state.get(f"results_{bot_name}")
    if results:
        _display_screener_results(results, bot_name)
    else:
        st.info("Нажмите 'Запустить' для сканирования")

    # Live-цикл: ждём и перезапускаем
    if live_mode and bot_name in ("bot_screener_impulse", "bot_screener_uzkiy"):
        import time as _time
        placeholder = st.empty()
        placeholder.info(f"⏳ Сканирование через {scan_interval}сек... (Live ON)")
        _time.sleep(scan_interval)
        placeholder.empty()
        # Сканируем и накапливаем
        with st.spinner("Сканирование..."):
            results = _run_screener_async(
                bot_name, min_score, max_price, scan_limit,
                min_turnover, timeframe, lookback, min_range_pct,
                freshness,
            )
            if results:
                prev = st.session_state.get(f"results_{bot_name}", [])
                seen = {r["symbol"] for r in prev}
                new_count = 0
                for r in results:
                    if r["symbol"] not in seen:
                        prev.append(r)
                        seen.add(r["symbol"])
                        new_count += 1
                st.session_state[f"results_{bot_name}"] = prev
                if new_count:
                    st.toast(f"+{new_count} новых сигналов", icon="🔔")
        st.rerun()


async def _fetch_all_with_turnover(
    max_price: float, scan_limit: int, min_turnover: float = 10_000_000,
) -> list[dict]:
    """Fetch all symbols with price and turnover for krugloe screener."""
    from bot_screener_klin.fetcher import Fetcher
    from core.config import load_bot_env

    bot_dir = PROJECT_ROOT / "bot_screener_krugloe"
    load_bot_env(bot_dir)

    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    testnet = os.getenv("TESTNET", "true").lower() == "true"

    fetcher = Fetcher(api_key=api_key, api_secret=api_secret, testnet=testnet)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = {s.strip() for s in raw_exclude.split(",") if s.strip()}

    filtered = await fetcher.get_filtered_symbols(min_turnover)
    filtered = [(s, t) for s, t in filtered if s not in exclude][:scan_limit]

    results = []
    batch_size = 10
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = []
        for sym, turnover in batch:
            tasks.append(_fetch_one_price(fetcher, sym, turnover))
        batch_results = await asyncio.gather(*tasks)
        for r in batch_results:
            if r and (max_price <= 0 or r["price"] <= max_price):
                results.append(r)

    return results


async def _fetch_one_price(fetcher, symbol: str, turnover: float) -> dict | None:
    """Fetch price for a single symbol."""
    candles = await fetcher.get_klines(symbol=symbol, interval="5", limit=1)
    if not candles:
        return None
    price = candles[-1]["close"]
    return {"symbol": symbol, "price": price, "turnover_24h": turnover}


async def _fetch_all_with_candles(
    max_price: float, scan_limit: int, lookback: int = 50,
    min_turnover: float = 500_000, timeframe: str = "5",
) -> list[dict]:
    """Fetch all symbols with full candle data for impulse/pump screeners."""
    from bot_screener_klin.fetcher import Fetcher
    from core.config import load_bot_env

    bot_dir = PROJECT_ROOT / "bot_screener_impulse"
    load_bot_env(bot_dir)

    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")
    testnet = os.getenv("TESTNET", "true").lower() == "true"

    fetcher = Fetcher(api_key=api_key, api_secret=api_secret, testnet=testnet)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = {s.strip() for s in raw_exclude.split(",") if s.strip()}

    filtered = await fetcher.get_filtered_symbols(min_turnover)
    filtered = [(s, t) for s, t in filtered if s not in exclude][:scan_limit]

    results = []
    batch_size = 10
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = []
        for sym, turnover in batch:
            tasks.append(
                _fetch_one_candles(fetcher, sym, turnover, lookback, timeframe)
            )
        batch_results = await asyncio.gather(*tasks)
        for r in batch_results:
            if r and (max_price <= 0 or r["price"] <= max_price):
                results.append(r)

    return results


async def _fetch_one_candles(
    fetcher, symbol: str, turnover: float, lookback: int, timeframe: str = "5",
) -> dict | None:
    """Fetch candles for a single symbol."""
    candles = await fetcher.get_klines(
        symbol=symbol, interval=timeframe, limit=lookback,
    )
    if not candles or len(candles) < lookback:
        return None
    price = candles[-1]["close"]
    return {
        "symbol": symbol,
        "price": price,
        "candles": candles,
        "turnover_24h": turnover,
    }


def _run_screener_async(
    bot_name: str, min_score: int, max_price: float, scan_limit: int,
    min_turnover: float = 1_000_000, timeframe: str = "5", lookback: int = 50,
    min_range_pct: float = 3.0, freshness: int = 3,
) -> list[dict] | None:
    """Запустить скринер асинхронно."""
    try:
        import sys

        # Добавляем корень проекта в path
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))

        if bot_name == "bot_screener_klin":
            from bot_screener_klin.fetcher import fetch_all_symbols
            from bot_screener_klin.scanner import scan_symbol

            loop = asyncio.new_event_loop()
            try:
                symbols = loop.run_until_complete(
                    fetch_all_symbols(max_price=max_price)
                )
                if not symbols:
                    return []

                results = []
                for sym_data in symbols[:scan_limit]:
                    symbol = sym_data.get("symbol", "")
                    candles = sym_data.get("candles", [])
                    turnover = sym_data.get("turnover_24h", 0)

                    if turnover < min_turnover:
                        continue
                    if len(candles) < 50:
                        continue

                    scan_result = scan_symbol(
                        symbol, timeframe, candles, turnover_24h=turnover
                    )
                    if scan_result.score >= min_score:
                        results.append(
                            {
                                "symbol": scan_result.symbol,
                                "price": scan_result.price,
                                "type": scan_result.squeeze_type.value
                                if scan_result.squeeze_type
                                else "-",
                                "direction": scan_result.direction,
                                "score": scan_result.score,
                                "atr_pct": scan_result.atr_percent,
                                "bb_width": scan_result.bb_width,
                                "adx": scan_result.adx_value,
                                "compression": scan_result.compression_coef,
                                "turnover": scan_result.turnover_24h,
                                "sl": scan_result.stop_loss,
                                "tp": scan_result.take_profit,
                                "signal_time": scan_result.signal_time,
                            }
                        )

                results.sort(key=lambda x: x["score"], reverse=True)
                return results
            finally:
                loop.close()

        elif bot_name == "bot_screener_krugloe":
            from bot_screener_krugloe.main import _load_config
            from bot_screener_krugloe.scanner import scan_symbol

            loop = asyncio.new_event_loop()
            try:
                symbols = loop.run_until_complete(
                    _fetch_all_with_turnover(max_price, scan_limit, min_turnover)
                )
                if not symbols:
                    return []

                env_cfg = _load_config()
                levels = None
                results = []
                for sym_data in symbols:
                    symbol = sym_data.get("symbol", "")
                    price = sym_data.get("price", 0)
                    turnover = sym_data.get("turnover_24h", 0)

                    if price <= 0:
                        continue

                    sig = scan_symbol(
                        symbol=symbol,
                        timeframe=f"{env_cfg['timeframe']}m",
                        price=price,
                        levels=levels,
                        proximity_pct=env_cfg["proximity_pct"],
                        max_price=env_cfg["max_price"],
                        turnover_24h=turnover,
                    )
                    if sig and sig.proximity_pct <= min_score / 10:
                        results.append(
                            {
                                "symbol": sig.symbol,
                                "price": sig.price,
                                "level": sig.level,
                                "proximity_pct": sig.proximity_pct,
                                "dist_up_pct": sig.dist_up_pct,
                                "dist_down_pct": sig.dist_down_pct,
                                "turnover": sig.turnover_24h,
                                "signal_time": sig.signal_time,
                            }
                        )

                results.sort(key=lambda x: x["proximity_pct"])
                return results
            finally:
                loop.close()

        elif bot_name == "bot_screener_impulse":
            from bot_screener_impulse.scanner import analyze_symbol

            loop = asyncio.new_event_loop()
            try:
                symbols = loop.run_until_complete(
                    _fetch_all_with_candles(
                        max_price, scan_limit, lookback, min_turnover, timeframe,
                    )
                )
                if not symbols:
                    return []

                results = []
                for sym_data in symbols:
                    symbol = sym_data.get("symbol", "")
                    candles = sym_data.get("candles", [])
                    turnover = sym_data.get("turnover_24h", 0)

                    if len(candles) < 2:
                        continue

                    sig = analyze_symbol(
                        symbol=symbol,
                        timeframe=f"{timeframe}m",
                        candles=candles,
                        turnover_24h=turnover,
                    )
                    if sig:
                        results.append(
                            {
                                "symbol": sig.symbol,
                                "price": sig.price,
                                "direction": sig.direction,
                                "strength": int(sig.volume_ratio * 10),
                                "strength_label": f"vol={sig.volume_ratio:.1f}x",
                                "range_percent": sig.candle_width,
                                "candles_ago": 0,
                                "range_ratio": sig.volume_ratio,
                                "volume_ratio": sig.volume_ratio,
                                "body_percent": sig.delta_ratio,
                                "turnover": sig.turnover_24h,
                                "signal_time": sig.signal_time,
                            }
                        )

                results.sort(key=lambda x: x["range_percent"], reverse=True)
                return results
            finally:
                loop.close()

        elif bot_name == "bot_screener_uzkiy":
            from bot_screener_uzkiy.scanner import ScanConfig, analyze_symbol

            loop = asyncio.new_event_loop()
            try:
                symbols = loop.run_until_complete(
                    _fetch_all_with_candles(
                        max_price, scan_limit, lookback, min_turnover, timeframe,
                    )
                )
                if not symbols:
                    return []

                cfg = ScanConfig(lookback=lookback, min_turnover_24h=min_turnover)
                results = []
                for sym_data in symbols:
                    symbol = sym_data.get("symbol", "")
                    candles = sym_data.get("candles", [])
                    turnover = sym_data.get("turnover_24h", 0)

                    if len(candles) < lookback:
                        continue

                    sig = analyze_symbol(
                        symbol=symbol,
                        timeframe=f"{timeframe}m",
                        candles=candles,
                        cfg=cfg,
                        turnover_24h=turnover,
                    )
                    if sig and sig.total_candles > 0:
                        results.append(
                            {
                                "symbol": sig.symbol,
                                "price": sig.price,
                                "avg_width": sig.avg_width_pct,
                                "max_width": sig.max_width_pct,
                                "quiet": f"{sig.quiet_candles}/{sig.total_candles}",
                                "turnover": sig.turnover_24h,
                                "status": sig.status,
                                "signal_time": sig.signal_time,
                            }
                        )

                results.sort(key=lambda x: x["avg_width"])
                return results
            finally:
                loop.close()

        else:
            st.warning(f"Скринер {bot_name} пока не подключен к dashboard")
            return None

    except (ValueError, OSError, ImportError) as e:
        st.error(f"Ошибка: {e}")
        return None


def _display_screener_results(results: list[dict], bot_name: str) -> None:
    """Отобразить результаты скринера."""
    if not results:
        st.warning("Сигналов не найдено")
        return

    st.success(f"Найдено {len(results)} сигналов")

    import pandas as pd

    df = pd.DataFrame(results)

    if bot_name == "bot_screener_krugloe":
        df = df.rename(
            columns={
                "symbol": "Монета",
                "price": "Цена",
                "level": "Уровень",
                "proximity_pct": "Проксимити %",
                "dist_up_pct": "До верх %",
                "dist_down_pct": "До низ %",
                "turnover": "Оборот 24ч",
                "signal_time": "Время",
            }
        )
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Цена": st.column_config.NumberColumn(format="%.4f"),
                "Уровень": st.column_config.NumberColumn(format="%.4f"),
                "Проксимити %": st.column_config.NumberColumn(format="%.2f%%"),
                "До верх %": st.column_config.NumberColumn(format="%.2f%%"),
                "До низ %": st.column_config.NumberColumn(format="%.2f%%"),
                "Оборот 24ч": st.column_config.NumberColumn(format="$%.0f"),
                "Время": st.column_config.TextColumn(),
            },
        )
    elif bot_name == "bot_screener_impulse":
        df = df.rename(
            columns={
                "symbol": "Монета",
                "price": "Цена",
                "direction": "Направление",
                "strength": "Сила %",
                "strength_label": "Оценка",
                "range_percent": "Свеча %",
                "candles_ago": "Свежесть",
                "range_ratio": "Размер x",
                "volume_ratio": "Объём x",
                "body_percent": "Body %",
                "turnover": "Оборот 24ч",
                "signal_time": "Время",
            }
        )
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Цена": st.column_config.NumberColumn(format="%.4f"),
                "Сила %": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                ),
                "Свеча %": st.column_config.NumberColumn(format="%.2f%%"),
                "Свежесть": st.column_config.NumberColumn(format="%d св."),
                "Размер x": st.column_config.NumberColumn(format="%.1fx"),
                "Объём x": st.column_config.NumberColumn(format="%.1fx"),
                "Body %": st.column_config.NumberColumn(format="%.1f%%"),
                "Оборот 24ч": st.column_config.NumberColumn(format="$%.0f"),
                "Время": st.column_config.TextColumn(),
            },
        )
    elif bot_name == "bot_screener_uzkiy":
        df = df.rename(
            columns={
                "symbol": "Монета",
                "price": "Цена",
                "avg_width": "Avg W%",
                "max_width": "Max W%",
                "quiet": "Quiet",
                "turnover": "Оборот 24ч",
                "status": "Статус",
                "signal_time": "Время",
            }
        )
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Цена": st.column_config.NumberColumn(format="%.4f"),
                "Avg W%": st.column_config.NumberColumn(format="%.3f%%"),
                "Max W%": st.column_config.NumberColumn(format="%.3f%%"),
                "Оборот 24ч": st.column_config.NumberColumn(format="$%.0f"),
                "Время": st.column_config.TextColumn(),
            },
        )
    else:
        df = df.rename(
            columns={
                "symbol": "Монета",
                "price": "Цена",
                "type": "Тип",
                "direction": "Направление",
                "score": "Score",
                "atr_pct": "ATR%",
                "bb_width": "BB%",
                "adx": "ADX",
                "compression": "Сжатие",
                "turnover": "Оборот 24ч",
                "sl": "SL",
                "tp": "TP",
                "signal_time": "Время",
            }
        )
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Цена": st.column_config.NumberColumn(format="%.4f"),
                "Score": st.column_config.ProgressColumn(
                    min_value=0, max_value=100, format="%d"
                ),
                "ATR%": st.column_config.NumberColumn(format="%.2f%%"),
                "BB%": st.column_config.NumberColumn(format="%.2f%%"),
                "ADX": st.column_config.NumberColumn(format="%.1f"),
                "Сжатие": st.column_config.NumberColumn(format="%.3f"),
                "Оборот 24ч": st.column_config.NumberColumn(format="$%.0f"),
                "SL": st.column_config.NumberColumn(format="%.4f"),
                "TP": st.column_config.NumberColumn(format="%.4f"),
                "Время": st.column_config.TextColumn(),
            },
        )
        if "Score" in df.columns:
            st.bar_chart(df["Score"], height=200)


# ============================================================
# Страница: Бэктест
# ============================================================


def page_backtest() -> None:
    """Страница бэктеста."""
    st.title("📈 Бэктест")

    col1, col2 = st.columns([1, 2])

    with col1:
        st.subheader("Параметры")

        strategy = st.selectbox(
            "Стратегия",
            ["flat", "yrovni", "trend", "impulse", "zero"],
        )

        symbol = st.text_input("Символ", value="XRPUSDT")

        timeframe = st.selectbox(
            "Таймфрейм",
            ["1", "5", "15", "60", "240", "D"],
            index=1,
        )

        limit = st.slider("Кол-во свечей", 100, 2000, 500, step=100)

        st.divider()

        sl_pct = st.number_input(
            "Stop Loss %", value=2.0, min_value=0.1, max_value=20.0, step=0.1
        )
        tp_pct = st.number_input(
            "Take Profit %", value=4.0, min_value=0.1, max_value=20.0, step=0.1
        )

        st.divider()

        run_btn = st.button(
            "🚀 Запустить бэктест", type="primary", use_container_width=True
        )

    with col2:
        st.subheader("Результаты")

        if run_btn:
            with st.spinner("Загрузка свечей и расчёт..."):
                result = _run_backtest(
                    strategy, symbol, timeframe, limit, sl_pct, tp_pct
                )
                if result:
                    st.session_state["backtest_result"] = result
                    st.session_state["backtest_params"] = {
                        "strategy": strategy,
                        "symbol": symbol,
                        "timeframe": timeframe,
                    }

        result = st.session_state.get("backtest_result")
        params = st.session_state.get("backtest_params", {})

        if result:
            _display_backtest_results(result, params)
        else:
            st.info("Настройте параметры и нажмите 'Запустить бэктест'")


def _run_backtest(
    strategy: str, symbol: str, timeframe: str, limit: int, sl_pct: float, tp_pct: float
) -> dict | None:
    """Запустить бэктест."""
    try:
        import sys

        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))

        from core.bybit_client import BybitClient
        from core.config import Config, load_bot_env

        # Загружаем env
        load_bot_env(PROJECT_ROOT)

        config = Config()
        config.symbol = symbol
        config.timeframe = timeframe
        config.validate()

        loop = asyncio.new_event_loop()
        try:
            client = BybitClient(config)
            try:
                candles = loop.run_until_complete(
                    client.get_klines(symbol, interval=timeframe, limit=limit)
                )
            finally:
                client.close()

            if not candles:
                st.error("Не удалось загрузить свечи")
                return None

            # Конвертируем Candle в dict для совместимости
            candle_dicts = [
                {
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                    "open_time": c.open_time,
                }
                for c in candles
            ]

            return {
                "candles": candle_dicts,
                "candle_objects": candles,
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy": strategy,
                "sl_pct": sl_pct,
                "tp_pct": tp_pct,
            }
        finally:
            loop.close()

    except (ValueError, OSError, ImportError) as e:
        st.error(f"Ошибка: {e}")
        return None


def _display_backtest_results(result: dict, params: dict) -> None:
    """Отобразить результаты бэктеста."""
    candles = result["candles"]
    symbol = result["symbol"]
    timeframe = result["timeframe"]
    sl_pct = result["sl_pct"]
    tp_pct = result["tp_pct"]

    if len(candles) < 50:
        st.warning("Недостаточно свечей для анализа")
        return

    # Простой анализ: считаем тренд и волатильность
    closes = [c["close"] for c in candles]
    import pandas as pd

    df = pd.DataFrame(candles)

    # Базовая статистика
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Загружено свечей", len(candles))
    with col2:
        st.metric("Текущая цена", f"{closes[-1]:.4f}")
    with col3:
        change = (closes[-1] - closes[0]) / closes[0] * 100
        st.metric("Изменение", f"{change:+.2f}%")
    with col4:
        volatility = pd.Series(closes).pct_change().std() * 100
        st.metric("Волатильность", f"{volatility:.2f}%")

    st.divider()

    # График цены
    st.subheader(f"📊 {symbol} — TF {timeframe}")

    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("time")

    st.line_chart(df["close"], height=300)

    # Детали
    with st.expander("📋 Детали свечей (последние 20)"):
        st.dataframe(
            df[["open", "high", "low", "close", "volume"]].tail(20),
            use_container_width=True,
        )

    # Info о бэктесте
    st.divider()
    st.info(
        f"**Стратегия**: {params.get('strategy', '?')} | "
        f"**SL**: {sl_pct}% | **TP**: {tp_pct}% | "
        f"**Свечей**: {len(candles)} | **TF**: {timeframe}m"
    )

    st.warning(
        "⚠️ Стратегии ещё не реализованы (make_strategy() "
        "выбрасывает ValueError). Бэктест покажет полные результаты "
        "после написания strategy.py для каждого бота."
    )


# ============================================================
# Страница: Настройки
# ============================================================


def page_settings() -> None:
    """Страница настроек: просмотр и редактирование .env файлов."""
    st.title("⚙️ Настройки")

    bot_dirs = get_bot_dirs()

    tab1, tab2 = st.tabs(["Боты", "Общий .env"])

    with tab1:
        selected_bot = st.selectbox(
            "Выберите бота",
            [d.name for d in bot_dirs],
        )

        if selected_bot:
            bot_dir = PROJECT_ROOT / selected_bot
            env = load_env_file(bot_dir)

            if env:
                st.subheader(f"📋 {selected_bot}/.env")

                # Показываем ключевые параметры
                col1, col2 = st.columns(2)

                with col1:
                    st.markdown("**Сеть и режим**")
                    st.json(
                        {
                            "TESTNET": env.get("TESTNET", "true"),
                            "SIMULATION_MODE": env.get("SIMULATION_MODE", "true"),
                            "CATEGORY": env.get("CATEGORY", "linear"),
                        }
                    )

                with col2:
                    st.markdown("**Торговля**")
                    st.json(
                        {
                            "SYMBOL": env.get("SYMBOL", "—"),
                            "TIMEFRAME": env.get("TIMEFRAME", "15"),
                            "POSITION_PCT": env.get("POSITION_PCT", "10"),
                            "STOP_LOSS_PCT": env.get("STOP_LOSS_PCT", "2"),
                            "TAKE_PROFIT_PCT": env.get("TAKE_PROFIT_PCT", "4"),
                        }
                    )

                # Все переменные
                with st.expander("🔑 Все переменные"):
                    st.json(env)
            else:
                st.info(f"Файл {selected_bot}/.env не найден")

    with tab2:
        st.subheader("📋 Корневой .env")
        root_env = load_env_file(PROJECT_ROOT)
        if root_env:
            st.json(root_env)
        else:
            st.info("Корневой .env не найден")


# ============================================================
# Точка входа
# ============================================================


def main() -> None:
    """Главная функция."""
    page = sidebar()

    if page == "📊 Обзор":
        page_overview()
    elif page == "  Flat":
        page_bot("bot_flat")
    elif page == "  Yrovni":
        page_bot("bot_yrovni")
    elif page == "  Trend":
        page_bot("bot_trend")
    elif page == "  Impulse":
        page_bot("bot_impulse")
    elif page == "  Bot 0":
        page_bot("bot_0")
    elif page == "  Сжатие (uzkiy)":
        st.title("🔍 Сжатие (uzkiy)")
        _run_screener_tab("bot_screener_uzkiy")
    elif page == "  Накопление (pump)":
        st.title("🔍 Накопление (pump)")
        _run_screener_tab("bot_screener_pump")
    elif page == "  Volume Profile":
        st.title("🔍 Volume Profile")
        _run_screener_tab("bot_screener_yrovni")
    elif page == "  Круглые числа":
        st.title("🔍 Круглые числа")
        _run_screener_tab("bot_screener_krugloe")
    elif page == "  Импульс (impulse)":
        st.title("🔍 Импульс (impulse)")
        _run_screener_tab("bot_screener_impulse")
    elif page == "  Бэктест":
        page_backtest()
    elif page == "  Настройки":
        page_settings()


if __name__ == "__main__":
    main()
