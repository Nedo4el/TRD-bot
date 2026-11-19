"""Дамп параметров робота Trend и скринера Trend в читаемые txt-файлы.

Пишет (перезаписывая) два файла на рабочий стол:

    C:\\Users\\79095\\Desktop\\trd\\Robot\\robot_trend.txt
    C:\\Users\\79095\\Desktop\\trd\\Screener\\bot_screener_trend.txt

Папки назначения можно переопределить переменными окружения
``PARAM_DUMP_ROBOT_DIR`` и ``PARAM_DUMP_SCREENER_DIR``.

Значения берутся не из текста, а из самих конфигов: ``robot_trend/.env`` +
``TrendConfig`` / ``TrendParams`` и ``bot_screener_trend/.env`` + ``ScanConfig``.

Использование::

    python tools/dump_params.py
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if TYPE_CHECKING:
    from bot_screener_trend.autostart import AutoStartConfig
    from bot_screener_trend.scanner import ScanConfig
    from robot_trend.config import TrendConfig
    from robot_trend.guard import GuardParams
    from robot_trend.strategy import TrendParams

ROBOT_ENV = PROJECT_ROOT / "robot_trend" / ".env"
SCREENER_ENV = PROJECT_ROOT / "bot_screener_trend" / ".env"

ROBOT_DIR = Path(os.getenv("PARAM_DUMP_ROBOT_DIR", r"C:\Users\79095\Desktop\trd\Robot"))
SCREENER_DIR = Path(
    os.getenv("PARAM_DUMP_SCREENER_DIR", r"C:\Users\79095\Desktop\trd\Screener")
)
ROBOT_FILE = ROBOT_DIR / "robot_trend.txt"
SCREENER_FILE = SCREENER_DIR / "bot_screener_trend.txt"

MSK = timezone(timedelta(hours=3))
LABEL_WIDTH = 30


def env_keys(path: Path) -> list[str]:
    """Имена переменных из .env файла."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return re.findall(r"^([A-Z0-9_]+)=", text, re.MULTILINE)


def forget_env(paths: list[Path]) -> None:
    """Убрать из окружения значения .env, чтобы конфиг читался заново."""
    for path in paths:
        for key in env_keys(path):
            os.environ.pop(key, None)


def row(label: str, value: str) -> str:
    """Строка «имя ........ значение»."""
    dots = "." * max(LABEL_WIDTH - len(label), 3)
    return f"  {label} {dots} {value}"


def num(value: float) -> str:
    """Число без лишних нулей: 1.0 -> 1, 0.005 -> 0.005."""
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def flag(value: bool) -> str:
    """Булево значение по-человечески."""
    return "да" if value else "нет"


def signed_pct(raw: float, zero: str = "выкл", *, down: bool = False) -> str:
    """Доля -> «0.03 (-3.00%)»; ноль -> «0 (выкл)»."""
    if raw == 0:
        return f"0 ({zero})"
    value = -raw if down else raw
    sign = "+" if value > 0 else "-"
    return f"{num(raw)} ({sign}{abs(value) * 100:.2f}%)"


def header(title: str, source: Path, now: datetime) -> list[str]:
    """Шапка дампа."""
    return [
        "=" * 62,
        f"  {title}",
        f"  Обновлено: {now:%d.%m.%Y %H:%M} МСК",
        f"  Источник: {source.relative_to(PROJECT_ROOT).as_posix()}",
        "=" * 62,
    ]


def robot_text(
    cfg: TrendConfig, p: TrendParams, g: GuardParams, now: datetime
) -> str:
    """Текст дампа параметров robot_trend."""
    notional = cfg.order_notional()
    size = f"${notional:.2f} (от депозита)" if notional else "по QTY"
    long_rule = (
        f"  LONG : {cfg.htf_timeframe}м тренд вверх (EMA {p.ema_fast} > "
        f"{p.ema_slow}, цена > EMA {p.ema_macro}, ADX >= {num(p.adx_threshold)}, "
        f"+DI > -DI) + откат к EMA и отскок, зазор EMA >= "
        f"{num(p.ema_min_spread_pct)}%, объём >= {num(p.vol_mult)}x SMA, "
        f"ER({p.chop_period}) >= {num(p.chop_min_er)}, "
        f"цена <= {num(p.max_ext_atr)} ATR от EMA(HTF)"
    )
    lines = header("ROBOT TREND — ПАРАМЕТРЫ", ROBOT_ENV, now)
    lines += [
        "",
        "ИНСТРУМЕНТ И ТАЙМФРЕЙМЫ",
        row("SYMBOL", cfg.symbol),
        row("CATEGORY", cfg.category),
        row("TESTNET (тестовые деньги)", flag(cfg.testnet)),
        row("TIMEFRAME (рабочий, мин)", cfg.timeframe),
        row("HTF_TIMEFRAME (старший, мин)", cfg.htf_timeframe),
        row("CANDLE_WARMUP (свечей LTF)", str(cfg.candles_warmup)),
        row("HTF_WARMUP (свечей HTF)", str(cfg.htf_candle_warmup)),
        row("POLL_SEC (опрос, сек)", num(cfg.poll_sec)),
        row("WAKE_JITTER_MS (разброс просыпания, мс)", str(cfg.wake_jitter_ms)),
        "",
        "РАЗМЕР ПОЗИЦИИ",
        row("DEPOSIT_USD", num(cfg.deposit_usd)),
        row("POSITION_PCT", f"{num(cfg.position_pct)}%"),
        row("QTY (фикс. лот, 0 = выкл)", num(cfg.fixed_qty)),
        row("Размер сделки", size),
        "",
        "СТРАТЕГИЯ — ИНДИКАТОРЫ",
        row("EMA_FAST", str(p.ema_fast)),
        row("EMA_SLOW", str(p.ema_slow)),
        row("EMA_MACRO", str(p.ema_macro)),
        row("EMA_MIN_SPREAD_PCT", f"{num(p.ema_min_spread_pct)}%"),
        row("ADX_PERIOD", str(p.adx_period)),
        row("ADX_THRESHOLD", num(p.adx_threshold)),
        row("CHOP_PERIOD (свечей LTF)", str(p.chop_period)),
        row("CHOP_MIN_ER (боковик)", num(p.chop_min_er)),
        row("MAX_EXT_ATR (перегрев, ATR HTF)", num(p.max_ext_atr)),
        row("VOLUME_SMA_PERIOD", str(p.vol_period)),
        row("VOLUME_MULTIPLIER", f"{num(p.vol_mult)}x SMA"),
        row("ENTRY_ON_PULLBACK", flag(p.entry_on_pullback)),
        "",
        "ВХОД (все условия сразу)",
        long_rule,
        "  SHORT: зеркально (тренд вниз, откат сверху)",
        "  Выход: только серверный SL/TP биржи — стратегия не закрывает по сигналу",
        "",
        "РИСК И ВЫХОД",
        row("SL_PCT (стоп)", signed_pct(p.sl_pct, down=True)),
        row("TAKE_PCT (тейк)", signed_pct(p.take_pct)),
        row("BE_TRIGGER", signed_pct(p.be_trigger_pct)),
        row("BE_OFFSET", signed_pct(p.be_offset_pct)),
        row("TRAIL_PCT (трейлинг)", signed_pct(p.trail_pct)),
        row("TRAIL_TRIGGER (старт трейлинга)", signed_pct(p.trail_trigger_pct)),
        "",
        "ЛИМИТЫ И ЗАЩИТА",
        row("COOLDOWN_SEC (пауза)", num(cfg.cooldown_sec)),
        row("DAILY_LOSS_LIMIT (USD/день)", num(cfg.daily_loss_limit)),
        row("MAX_TRADES_PER_DAY", str(cfg.max_trades_per_day)),
        row("MAX_LOSS_USD (kill)", num(cfg.max_loss_usd)),
        row("MAX_DRAWDOWN_PCT (kill)", signed_pct(cfg.max_drawdown_pct, down=True)),
        row("MAX_ENTRY_DEVIATION_PCT", f"{num(cfg.max_entry_deviation_pct * 100)}%"),
        row("REENTRY_LOCK_STOPS", str(cfg.reentry_lock_stops)),
        row("REENTRY_LOCK_SEC (сек)", num(cfg.reentry_lock_sec)),
        row("SLIPPAGE_PCT", f"{num(cfg.slippage_pct)} (доля)"),
        row("FUNDING_AWARE", flag(cfg.funding_aware)),
        row("FUNDING_WINDOW_SEC", num(cfg.funding_window_sec)),
        row("KILL_SWITCH (файл)", cfg.kill_switch_file or "выкл"),
        "",
        "ЗАЩИТА ПОЗИЦИИ (GUARD)",
        row("GUARD_ENABLED", flag(g.enabled)),
        row("GUARD_DRY_RUN (без ордеров)", flag(g.dry_run)),
        row("CVD_ENABLED", flag(g.cvd_enabled)),
        row("CVD_WINDOW (свечей 5м)", str(g.cvd_window)),
        row("CVD_EXIT_MODE", g.cvd_exit_mode),
        row("CVD_PARTIAL_PCT", f"{num(g.partial_pct * 100)}%"),
        row("PARTIAL_MODE", g.partial_mode),
        row("PARTIAL_MIN_PROFIT", signed_pct(g.partial_min_profit)),
        row("PARTIAL_FALLBACK_PCT (уровень)", signed_pct(g.partial_fallback_pct)),
        row("PARTIAL_SIGNAL", g.partial_signal),
        row("ABSORPTION_ENABLED", flag(g.absorption_enabled)),
        row("ABSORPTION_VOL_MULT", f"{num(g.absorption_vol_mult)}x SMA20"),
        row("ABSORPTION_BODY_MAX", f"{num(g.absorption_body_max)} ATR(14)"),
        row("OI_ENABLED", flag(g.oi_enabled)),
        row("OI_WINDOW (свечей 5м)", str(g.oi_window)),
        row("OI_DROP_PCT (от пика)", f"{num(g.oi_drop_pct * 100)}%"),
        row("FUNDING_HIGH_THRESHOLD", f"{num(g.funding_high * 100)}%"),
        row("MIN_VOLUME_USD (оборот свечи)", f"${num(g.min_volume_usd)}"),
        (
            f"  CVD: цена обновила экстремум, а CVD — нет -> "
            f"{'выход целиком' if g.cvd_exit_mode == 'full' else 'закрыть ' + num(g.partial_pct * 100) + '%'}"
        ),
        (
            f"  Absorption: объём >= {num(g.absorption_vol_mult)}x SMA20 при теле "
            f"<= {num(g.absorption_body_max)} ATR -> WARN, при пробое соседней свечи — частичный выход"
        ),
        (
            f"  OI: новый экстремум цены без роста OI -> частичный выход; "
            f"фандинг >= {num(g.funding_high * 100)}% -> выход целиком"
        ),
        "",
        "СРЕЗ 50% — КАК РАБОТАЕТ СЕЙЧАС",
        (
            f"  GUARD_DRY_RUN={flag(g.dry_run)}: "
            + (
                "сигналы только в guard_log.jsonl, реально работает "
                f"уровень {num(g.partial_fallback_pct * 100)}% + трейлинг"
                if g.dry_run
                else "сигналы режут позицию по-настоящему"
            )
        ),
        (
            "  резать по дивергенции (CVD/OI) -> GUARD_DRY_RUN=false; "
            "при PARTIAL_MODE=both сработает первое из двух событий"
        ),
        "  PARTIAL_FALLBACK_PCT должен быть ниже TAKE_PCT, иначе тейк сработает раньше",
        "",
        "СЛУЖЕБНОЕ",
        row("WS_ENABLED", flag(cfg.ws_enabled)),
        row("RECONNECT_SEC", num(cfg.reconnect_sec)),
        row("HEARTBEAT_SEC", num(cfg.heartbeat_sec)),
        row("RECV_WINDOW (мс)", str(cfg.recv_window)),
        row("TIME_SYNC", flag(cfg.time_sync)),
        row("REQUESTS_PER_SECOND", num(cfg.requests_per_second)),
        row("STATE_FILE", cfg.state_file),
        row("LOG_FILE", cfg.log_file),
        row("LOG_LEVEL", cfg.log_level),
        "",
    ]
    return "\n".join(lines)


def screener_text(
    env_cfg: dict,
    cfg: ScanConfig,
    auto_cfg: AutoStartConfig,
    now: datetime,
) -> str:
    """Текст дампа параметров bot_screener_trend."""
    excluded = env_cfg["exclude_symbols"]
    exclude_note = f"{len(excluded)} шт: {', '.join(excluded)}" if excluded else "нет"
    turnover = env_cfg["min_turnover_24h"]
    trend_tf = env_cfg["htf_timeframe"]
    signal_tf = env_cfg["timeframe"]
    up_rule = (
        f"  UP  : EMA {cfg.ema_fast} > EMA {cfg.ema_slow}, цена > "
        f"EMA {cfg.ema_macro}, ADX >= {num(cfg.adx_threshold)}, +DI > -DI"
    )
    down_rule = (
        f"  DOWN: EMA {cfg.ema_fast} < EMA {cfg.ema_slow}, цена < "
        f"EMA {cfg.ema_macro}, ADX >= {num(cfg.adx_threshold)}, -DI > +DI"
    )
    entry_note = (
        f"  Вход (выбирает робот): откат к EMA {cfg.ema_fast}, объём, зазор EMA"
    )
    lines = header("SCREENER TREND — ПАРАМЕТРЫ", SCREENER_ENV, now)
    lines += [
        "",
        "СКАНИРОВАНИЕ",
        row("TESTNET (тестовые данные)", flag(env_cfg["testnet"])),
        row("TIMEFRAME (сигнал, мин)", signal_tf),
        row("HTF_TIMEFRAME (тренд, мин)", trend_tf),
        row("SCAN_INTERVAL (сек)", str(env_cfg["scan_interval"])),
        row("MIN_TURNOVER_24H", f"${turnover / 1_000_000:.0f}M"),
        row("LOOKBACK_BARS (свечей)", str(env_cfg["lookback_bars"])),
        row("HTF_LOOKBACK_BARS (свечей)", str(env_cfg["htf_lookback_bars"])),
        row("EXCLUDE_SYMBOLS", exclude_note),
        "",
        "ТРЕНД — ИНДИКАТОРЫ",
        row("EMA_FAST", str(cfg.ema_fast)),
        row("EMA_SLOW", str(cfg.ema_slow)),
        row("EMA_MACRO", str(cfg.ema_macro)),
        row("ADX_PERIOD", str(cfg.adx_period)),
        row("ADX_THRESHOLD", num(cfg.adx_threshold)),
        "",
        "СПРАВОЧНО В ТАБЛИЦЕ (на отбор не влияет)",
        row("VOLUME_PERIOD", str(cfg.vol_period)),
        "",
        "УСЛОВИЕ СИГНАЛА (только тренд)",
        f"  Тренд считается на {trend_tf}m, робот входит на {signal_tf}m",
        up_rule,
        down_rule,
        "  Score: ADX x 2 + зазор EMA_FAST/EMA_SLOW x 5 (макс. 100)",
        "  Скринер отдаёт монету роботу, точку входа робот выбирает сам",
        entry_note,
        "",
        "АВТО-ЗАПУСК ROBOT_TREND",
        row("AUTO_START", flag(auto_cfg.enabled)),
        row("AUTO_START_ROBOT", auto_cfg.robot_name),
        row("AUTO_START_MAX (монет за скан)", str(auto_cfg.max_per_scan)),
        "  Монеты сигналов добавляются в список робота и запускаются",
        "  как кнопкой в дашборде (отдельный процесс на монету)",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """Перезаписать оба txt-дампа и напечатать пути."""
    from bot_screener_trend.main import _load_config
    from robot_trend.config import load_config
    from robot_trend.guard import GuardParams
    from robot_trend.strategy import TrendParams

    now = datetime.now(MSK)

    forget_env([ROBOT_ENV, SCREENER_ENV])
    robot_cfg = load_config()
    robot_params = TrendParams.from_env()
    guard_params = GuardParams.from_env()
    robot_out = robot_text(robot_cfg, robot_params, guard_params, now)

    forget_env([ROBOT_ENV, SCREENER_ENV])
    env_cfg, scan_cfg, auto_cfg = _load_config()
    screener_out = screener_text(env_cfg, scan_cfg, auto_cfg, now)

    for path, text in ((ROBOT_FILE, robot_out), (SCREENER_FILE, screener_out)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\r\n")
        print(f"обновлено: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
