"""Grid Flat — нейтральная сетка вокруг POC (Volume Profile за 3 часа).

Чистая логика без I/O: профиль объёма (POC/VAH/VAL), построение сетки,
риск-проверки и стейт-машина решений. Исполнение ордеров — в main.py.

Полное ТЗ с формулами: robot_grid_flat/TZ.md.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field

from core.bybit_client import Candle
from core.indicators import atr as atr_series
from core.indicators import ema as ema_series
from core.logger import get_logger

logger = get_logger(__name__)

EQUITY_WINDOW_S = 24 * 3600
EQUITY_SAMPLES_MAX = 4000
BUILD_RETRY_NOTE = "рекомендация: уменьшите LEVELS_MIN или увеличьте депозит"


# ============================= Конфигурация ==============================


@dataclass
class GridConfig:
    """Параметры сетки (дефолты и диапазоны — в TZ.md)."""

    # --- POC / профиль ---
    poc_window_min: int = 180
    poc_bins: int = 100
    value_area_pct: float = 70.0

    # --- ATR и границы ---
    atr_period: int = 14
    atr_extend_mult: float = 1.0

    # --- Стабильность POC (гейт построения) ---
    poc_unstable_pct: float = 0.5
    poc_unstable_bars: int = 3

    # --- Шаг сетки ---
    grid_step_atr_mult: float = 0.3
    min_tick_mult: int = 3
    fee_taker: float = 0.00055
    fee_maker: float = 0.0002
    step_fee_mult: float = 3.0

    # --- Количество уровней ---
    levels_min: int = 10
    levels_max: int = 50

    # --- Риск ---
    risk_per_trade_pct: float = 1.0
    max_exposure_pct: float = 20.0
    leverage_cap: float = 3.0
    max_open_levels: int = 10
    grid_sl_atr_mult: float = 1.0
    trail_trigger_pct: float = 3.0
    trail_giveback_pct: float = 1.0
    kill_24h_pct: float = 5.0

    # --- Гарды входа ---
    funding_max_pct: float = 0.05
    max_spread_pct: float = 0.05
    depth_mult: float = 5.0
    impulse_pct: float = 5.0
    impulse_window: int = 5
    impulse_cooldown_bars: int = 30

    # --- Динамика сетки ---
    recenter_interval_s: float = 1800.0
    poc_recenter_pct: float = 0.5
    trend_ema_fast: int = 50
    trend_ema_slow: int = 200
    trend_threshold_pct: float = 2.0

    # --- Размер ордера ---
    order_size_cap_usd: float = 0.0


# =============================== Модели ===================================


@dataclass(frozen=True)
class InstrumentInfo:
    """Торговые фильтры инструмента."""

    tick_size: float
    qty_step: float
    min_qty: float
    min_notional: float


@dataclass(frozen=True)
class VolumeProfile:
    """Volume Profile окна: POC и границы Value Area (70% объёма)."""

    poc: float
    vah: float
    val: float
    total_volume: float


@dataclass(frozen=True)
class GridPlan:
    """Построенная сетка: центр POC, границы, шаг, уровни, размер уровня."""

    poc: float
    vah: float
    val: float
    lower: float
    upper: float
    step: float
    atr: float
    levels_long: tuple[float, ...]
    levels_short: tuple[float, ...]
    per_level_usd: float
    built_at: float

    @property
    def n_levels(self) -> int:
        """Общее количество уровней сетки."""
        return len(self.levels_long) + len(self.levels_short)


@dataclass
class PlannedEntry:
    """Уровень, на который нужно выставить входящий ордер."""

    level_price: float
    side: str  # "Buy" | "Sell"
    usd: float


@dataclass
class LevelState:
    """Состояние одного уровня сетки."""

    side: str
    level_price: float
    status: str  # "entry" (ордер стоит) | "position" (исполнена, ждёт/держит TP)
    entry_price: float = 0.0
    qty: float = 0.0
    order_id: str = ""
    tp_order_id: str = ""
    step: float = 0.0


@dataclass
class MarketSnapshot:
    """Срез рынка за один цикл (собирает main.py)."""

    now: float
    candles: list[Candle]
    price: float
    bid: float
    ask: float
    equity: float
    long_qty: float
    short_qty: float
    funding_pct: float | None
    spread_pct: float | None
    depth_notional: float | None
    news_ok: bool
    manual_pause: bool
    tf_minutes: int
    instrument: InstrumentInfo


@dataclass
class Decision:
    """Решение стейт-машины на цикл — main.py только исполняет."""

    kind: str  # wait | build | rebuild | run | pause | reset | halt
    reason: str
    plan: GridPlan | None = None
    cancel_entries: bool = False
    close_all: bool = False
    cancel_sides: tuple[str, ...] = ()
    entries: list[PlannedEntry] = field(default_factory=list)


# ========================= Чистые функции сетки ===========================


def volume_profile(
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    n_bins: int = 100,
    value_area_pct: float = 70.0,
) -> VolumeProfile | None:
    """Volume Profile окна: POC + VAH/VAL (Value Area, доля объёма).

    Каждая свеча распределяет свой объём поровну по бинам, которые
    пересекает её диапазон [low, high]. Value Area расширяется от бина
    POC в сторону бо́льшего объёма, пока не наберёт value_area_pct.

    Args:
        highs: максимумы свечей (старые → новые).
        lows: минимумы свечей.
        volumes: объёмы свечей.
        n_bins: количество бинов профиля.
        value_area_pct: доля объёма Value Area (70%).

    Returns:
        VolumeProfile или None, если данных нет.
    """
    if not highs or len(highs) != len(lows) or len(highs) != len(volumes):
        return None
    g_low = min(lows)
    g_high = max(highs)
    total = float(sum(volumes))
    if total <= 0:
        return None
    if g_high <= g_low:
        return VolumeProfile(g_low, g_low, g_low, total)

    bucket = (g_high - g_low) / n_bins
    profile = [0.0] * n_bins
    for high, low, vol in zip(highs, lows, volumes):
        if vol <= 0 or high <= low:
            continue
        start = max(0, int((low - g_low) / bucket))
        end = min(n_bins - 1, int((high - g_low) / bucket))
        span = end - start + 1
        share = vol / span
        for b in range(start, end + 1):
            profile[b] += share

    poc_idx = max(range(n_bins), key=profile.__getitem__)
    poc = g_low + (poc_idx + 0.5) * bucket

    target = total * (value_area_pct / 100.0)
    lo = poc_idx
    hi = poc_idx
    area = profile[poc_idx]
    while area < target and (lo > 0 or hi < n_bins - 1):
        left = profile[lo - 1] if lo > 0 else -1.0
        right = profile[hi + 1] if hi < n_bins - 1 else -1.0
        if right >= left:
            hi += 1
            area += profile[hi]
        else:
            lo -= 1
            area += profile[lo]

    val = g_low + lo * bucket
    vah = g_low + (hi + 1) * bucket
    return VolumeProfile(poc=poc, vah=vah, val=val, total_volume=total)


def compute_step(atr_val: float, price: float, tick: float, cfg: GridConfig) -> float:
    """Базовый шаг сетки: max(0.3·ATR, N·tick, 3·taker·price), округлён вверх до тика.

    Args:
        atr_val: ATR(14, 5m).
        price: опорная цена (POC).
        tick: tickSize инструмента.
        cfg: конфиг сетки.

    Returns:
        Шаг сетки (кратный tick, не меньше минимальных ограничений).
    """
    floor_step = max(
        cfg.grid_step_atr_mult * atr_val,
        cfg.min_tick_mult * tick,
        cfg.step_fee_mult * cfg.fee_taker * price,
    )
    return ceil_to_tick(floor_step, tick)


def make_levels(
    poc: float, lower: float, upper: float, step: float, tick: float
) -> list[float]:
    """Уровни сетки: POC ± k·step внутри [lower, upper], без уровня на самом POC.

    Args:
        poc: центр сетки.
        lower: нижняя граница.
        upper: верхняя граница.
        step: шаг сетки.
        tick: tickSize (для выравнивания и пропуска POC).

    Returns:
        Цены уровней, отсортированные по удалённости от POC.
    """
    if step <= 0 or lower >= upper:
        return []
    k_min = math.ceil((lower - poc) / step)
    k_max = math.floor((upper - poc) / step)
    prices: list[float] = []
    seen: set[float] = set()
    for k in range(k_min, k_max + 1):
        if k == 0:
            continue
        price = round_to_tick(poc + k * step, tick)
        if price < lower - tick / 2 or price > upper + tick / 2:
            continue
        if abs(price - poc) < tick / 2:
            continue
        if price in seen:
            continue
        seen.add(price)
        prices.append(price)
    prices.sort(key=lambda p: abs(p - poc))
    return prices


def size_levels(
    equity: float,
    poc: float,
    lower: float,
    upper: float,
    atr_val: float,
    n_levels: int,
    cfg: GridConfig,
) -> float | None:
    """Размер одного уровня в USD: min(риск-кап, экспозиция/кол-во, явный кап).

    Риск-кап: убыток уровня при пробое сеточного стопа ≤ risk_per_trade_pct%
    депозита. Сеточный стоп: lower − grid_sl_atr_mult·ATR (и зеркально сверху).

    Returns:
        USD на уровень или None, если депозита не хватает.
    """
    if equity <= 0 or n_levels <= 0:
        return None
    sl_long = lower - cfg.grid_sl_atr_mult * atr_val
    sl_short = upper + cfg.grid_sl_atr_mult * atr_val
    worst_dist = max((poc - sl_long) / poc, (sl_short - poc) / poc)
    if worst_dist <= 0:
        return None

    risk_usd = equity * (cfg.risk_per_trade_pct / 100.0)
    per_risk = risk_usd / worst_dist
    exposure_budget = equity * (cfg.max_exposure_pct / 100.0)
    per_exposure = exposure_budget / n_levels
    per_cap = cfg.order_size_cap_usd if cfg.order_size_cap_usd > 0 else math.inf

    leverage_budget = equity * cfg.leverage_cap / max(n_levels, 1)
    per_level = min(per_risk, per_exposure, per_cap, leverage_budget)
    return per_level if per_level > 0 else None


def level_qty(usd: float, price: float, instrument: InstrumentInfo) -> float:
    """Количество актива для уровня: floor(usd/price / qtyStep)·qtyStep."""
    if usd <= 0 or price <= 0 or instrument.qty_step <= 0:
        return 0.0
    raw = usd / price
    return math.floor(raw / instrument.qty_step) * instrument.qty_step


def tp_price_for(side: str, entry_price: float, step: float, fee_maker: float) -> float:
    """TP уровня: соседний уровень, сдвинутый на суммарные maker-комиссии к POC.

    Гарантирует положительный нетто-результат сделки (шаг ≥ 3·taker·price).

    Args:
        side: сторона входа ("Buy"/"Sell").
        entry_price: фактическая цена входа.
        step: шаг сетки уровня.
        fee_maker: maker-комиссия дробью (0.0002).

    Returns:
        Цена TP (округление до тика делает place_order).
    """
    fee_buffer = entry_price * fee_maker * 2
    if side == "Buy":
        return entry_price + step - fee_buffer
    return entry_price - step + fee_buffer


def round_to_tick(price: float, tick: float) -> float:
    """Округлить цену до ближайшего тика."""
    if tick <= 0:
        return price
    return round(price / tick) * tick


def ceil_to_tick(value: float, tick: float) -> float:
    """Округлить значение вверх до кратного тика (не нарушает минимумы)."""
    if tick <= 0:
        return value
    return math.ceil(value / tick - 1e-9) * tick


def floor_to_tick(value: float, tick: float) -> float:
    """Округлить значение вниз до кратного тика (увеличивает число уровней)."""
    if tick <= 0:
        return value
    return math.floor(value / tick + 1e-9) * tick


def level_key(side: str, price: float) -> str:
    """Уникальный ключ уровня: сторона + цена."""
    return f"{side}:{price:.10f}"


# ============================ Стратегия ===================================


class GridStrategy:
    """Стейт-машина нейтральной сетки вокруг POC."""

    name = "grid_flat"

    def __init__(self, cfg: GridConfig | None = None) -> None:
        self.cfg = cfg or GridConfig()
        self.plan: GridPlan | None = None
        self.levels: dict[str, LevelState] = {}
        self.halted = False
        self.halt_reason = ""

        self._poc_history: deque[float] = deque(maxlen=self.cfg.poc_unstable_bars + 1)
        self._equity_samples: deque[tuple[float, float]] = deque()
        self._session_base: float = 0.0
        self._peak_equity: float = 0.0
        self._trailing_armed = False
        self._impulse_until: float = 0.0
        self._last_impulse_bar: int = -1
        self._last_poc_bar: int = -1
        self._last_recenter: float = 0.0
        self._sl_orders: dict[str, str] = {}
        self._sl_qty: dict[str, float] = {}
        self._sl_trigger: dict[str, float] = {}

    # --------------------------- Главный вход ----------------------------

    def next(self, snap: MarketSnapshot) -> Decision:
        """Принять решение на текущий цикл по срезу рынка.

        Приоритеты: kill-switch → трейлинг → SL сетки (выходы) →
        гарды входа → построение/перестроение → новые уровни.

        Args:
            snap: срез рынка от main.py.

        Returns:
            Decision, который main.py исполняет без доп. логики.
        """
        if self.halted:
            return Decision(
                "halt",
                f"ОСТАНОВЛЕН: {self.halt_reason}",
                cancel_entries=True,
                close_all=True,
            )

        self._record_equity(snap)

        kill = self._check_kill_switch(snap)
        if kill:
            return self._halt(kill)

        trail = self._check_trailing(snap)
        if trail:
            return self._reset(trail, snap)

        self._repair(snap)

        if self.plan is not None:
            sl = self._check_grid_sl(snap)
            if sl:
                return self._reset(sl, snap)

        self._note_impulse(snap)

        guard = self._entry_guard(snap)
        if guard is not None:
            if self.plan is None:
                return Decision("wait", f"нет сетки, пауза: {guard}")
            return Decision("pause", guard, cancel_entries=True)

        profile, atr_val = self._profile_and_atr(snap)
        if profile is None or atr_val is None:
            if self.plan is None:
                return Decision("wait", "разогрев: мало свечей для POC/ATR")
            return self._run(snap, "профиль недоступен — держим сетку")

        self._push_poc(snap, profile.poc)
        unstable = self._poc_unstable()

        if self.plan is None:
            if unstable:
                return Decision(
                    "wait",
                    f"POC нестабилен: сдвиг > {self.cfg.poc_unstable_pct}% "
                    f"за {self.cfg.poc_unstable_bars} бара",
                )
            return self._try_build(snap, profile, atr_val)

        if unstable:
            return self._run(snap, "POC нестабилен — держим текущую сетку")

        rebuild = self._rebuild_reason(snap, profile)
        if rebuild:
            decision = self._try_build(snap, profile, atr_val)
            if decision.kind == "build":
                return Decision(
                    "rebuild",
                    f"{rebuild} → {decision.reason}",
                    plan=decision.plan,
                    cancel_entries=True,
                    entries=decision.entries,
                )
            return self._run(snap, f"перестроение отложено: {decision.reason}")

        return self._run(snap, "ок")

    # ------------------- Переходы состояний (из main.py) ------------------

    def mark_entry_filled(self, key: str, price: float, qty: float) -> None:
        """Входящий ордер исполнен — перевести уровень в позицию."""
        level = self.levels.get(key)
        if level is None or qty <= 0:
            return
        level.status = "position"
        level.entry_price = price
        level.qty = qty
        level.order_id = ""
        level.tp_order_id = ""
        logger.info(
            "Исполнен вход %s @ %.8g qty=%.8g (уровень %.8g)",
            level.side,
            price,
            qty,
            level.level_price,
        )

    def mark_entry_cancelled(self, key: str) -> None:
        """Входящий ордер отменён без исполнения — освободить уровень."""
        level = self.levels.get(key)
        if level is None or level.status != "entry":
            return
        logger.debug("Отменён вход %s (уровень %.8g)", key, level.level_price)
        del self.levels[key]

    def mark_tp_placed(self, key: str, order_id: str) -> None:
        """Зафиксировать, что TP-ордер уровня выставлен."""
        level = self.levels.get(key)
        if level is not None:
            level.tp_order_id = order_id

    def mark_tp_filled(self, key: str) -> None:
        """TP исполнен — уровень закрыт, слот свободен."""
        level = self.levels.get(key)
        if level is None:
            return
        logger.info(
            "TP исполнен %s @ %.8g qty=%.8g (вход %.8g)",
            level.side,
            level.entry_price,
            level.qty,
            level.entry_price,
        )
        del self.levels[key]

    def clear_tp(self, key: str) -> None:
        """TP-ордер пропал/отменён — поставить его заново на следующем цикле."""
        level = self.levels.get(key)
        if level is not None:
            level.tp_order_id = ""

    def register_entry(
        self, side: str, price: float, usd: float, order_id: str
    ) -> None:
        """Записать выставленный входящий ордер в состояние."""
        self.levels[level_key(side, price)] = LevelState(
            side=side,
            level_price=price,
            status="entry",
            order_id=order_id,
            step=self.plan.step if self.plan else 0.0,
        )

    def register_sl(self, side: str, order_id: str, qty: float, trigger: float) -> None:
        """Зафиксировать выставленный сеточный stop-market ордер."""
        self._sl_orders[side] = order_id
        self._sl_qty[side] = qty
        self._sl_trigger[side] = trigger

    def clear_sl(self, side: str) -> None:
        """Забыть сеточный стоп стороны (ордер отменён/исполнён)."""
        self._sl_orders.pop(side, None)
        self._sl_qty.pop(side, None)
        self._sl_trigger.pop(side, None)

    def sl_info(self, side: str) -> tuple[str, float, float]:
        """Текущий сеточный стоп стороны: (orderId, qty, triggerPrice)."""
        return (
            self._sl_orders.get(side, ""),
            self._sl_qty.get(side, 0.0),
            self._sl_trigger.get(side, 0.0),
        )

    # ------------------------ Kill-switch / трейлинг ----------------------

    def _record_equity(self, snap: MarketSnapshot) -> None:
        """Добавить сэмпл эквити и выкинуть старше 24 часов."""
        eq = snap.equity
        self._equity_samples.append((snap.now, eq))
        while (
            len(self._equity_samples) > 1
            and self._equity_samples[0][0] < snap.now - EQUITY_WINDOW_S
        ):
            self._equity_samples.popleft()
        if len(self._equity_samples) > EQUITY_SAMPLES_MAX:
            for _ in range(len(self._equity_samples) - EQUITY_SAMPLES_MAX):
                self._equity_samples.popleft()
        if self._session_base <= 0:
            self._session_base = eq
            self._peak_equity = eq

    def _check_kill_switch(self, snap: MarketSnapshot) -> str | None:
        """−kill_24h_pct% эквити за 24ч → стоп бота."""
        if len(self._equity_samples) < 2:
            return None
        base = self._equity_samples[0][1]
        if base <= 0:
            return None
        pnl_pct = (snap.equity - base) / base * 100.0
        if pnl_pct <= -self.cfg.kill_24h_pct:
            return (
                f"KILL-SWITCH: эквити {pnl_pct:+.2f}% за 24ч "
                f"≤ −{self.cfg.kill_24h_pct}%"
            )
        return None

    def _check_trailing(self, snap: MarketSnapshot) -> str | None:
        """После +trail_trigger_pct к депо — трейлинг по пиковой эквити."""
        eq = snap.equity
        if self._session_base <= 0:
            return None
        gain_pct = (eq - self._session_base) / self._session_base * 100.0
        self._peak_equity = max(self._peak_equity, eq)
        if not self._trailing_armed and gain_pct >= self.cfg.trail_trigger_pct:
            self._trailing_armed = True
            logger.info("ТРЕЙЛИНГ ВКЛЮЧЁН: %+0.2f%% к депо (пик %.2f)", gain_pct, eq)
        if self._trailing_armed and self._peak_equity > 0:
            giveback = (self._peak_equity - eq) / self._peak_equity * 100.0
            if giveback >= self.cfg.trail_giveback_pct:
                return (
                    f"ТРЕЙЛИНГ: пик {self._peak_equity:.2f}, "
                    f"откат {giveback:.2f}% ≥ {self.cfg.trail_giveback_pct}% "
                    f"(профит {gain_pct:+.2f}%)"
                )
        return None

    def _reset(self, reason: str, snap: MarketSnapshot) -> Decision:
        """Полный сброс: закрыть всё, отменить ордера, новая сессия."""
        logger.warning("СБРОС СЕТКИ: %s", reason)
        self.plan = None
        self.levels.clear()
        self._sl_orders.clear()
        self._sl_qty.clear()
        self._sl_trigger.clear()
        self._trailing_armed = False
        self._session_base = snap.equity
        self._peak_equity = snap.equity
        return Decision("reset", reason, cancel_entries=True, close_all=True)

    def _halt(self, reason: str) -> Decision:
        """Kill-switch: остановить бота (main.py завершает цикл)."""
        logger.error("ОСТАНОВКА БОТА: %s", reason)
        self.halted = True
        self.halt_reason = reason
        return Decision("halt", reason, cancel_entries=True, close_all=True)

    # --------------------------- Выходы и гарды ---------------------------

    def _check_grid_sl(self, snap: MarketSnapshot) -> str | None:
        """Пробой границы диапазона + 1 ATR → закрыть всю сетку."""
        plan = self.plan
        if plan is None:
            return None
        sl_long = plan.lower - self.cfg.grid_sl_atr_mult * plan.atr
        sl_short = plan.upper + self.cfg.grid_sl_atr_mult * plan.atr
        if snap.price <= sl_long:
            return (
                f"SL СЕТКИ: цена {snap.price:.8g} ≤ {sl_long:.8g} "
                f"(граница {plan.lower:.8g} − {self.cfg.grid_sl_atr_mult} ATR)"
            )
        if snap.price >= sl_short:
            return (
                f"SL СЕТКИ: цена {snap.price:.8g} ≥ {sl_short:.8g} "
                f"(граница {plan.upper:.8g} + {self.cfg.grid_sl_atr_mult} ATR)"
            )
        return None

    def _note_impulse(self, snap: MarketSnapshot) -> None:
        """Зафиксировать импульс на новом баре → пауза входов."""
        candles = snap.candles
        window = self.cfg.impulse_window
        if len(candles) <= window:
            return
        bar_time = candles[-1].open_time
        if bar_time == self._last_impulse_bar:
            return
        self._last_impulse_bar = bar_time
        base = candles[-1 - window].close
        if base <= 0:
            return
        change = abs(candles[-1].close - base) / base * 100.0
        if change >= self.cfg.impulse_pct:
            cooldown = self.cfg.impulse_cooldown_bars * snap.tf_minutes * 60
            self._impulse_until = snap.now + cooldown
            logger.warning(
                "ИМПУЛЬС %.1f%% за %d баров — пауза %d баров",
                change,
                window,
                self.cfg.impulse_cooldown_bars,
            )

    def _entry_guard(self, snap: MarketSnapshot) -> str | None:
        """Гарды, запрещающие НОВЫЕ входы (выходы не блокируются)."""
        if snap.manual_pause:
            return "РУЧНАЯ ПАУЗА (файл PAUSE)"
        if not snap.news_ok:
            return "НОВОСТИ: окно ±15 мин"
        if snap.now < self._impulse_until:
            left = (self._impulse_until - snap.now) / 60
            return f"ИМПУЛЬС: пауза ещё {left:.0f} мин"
        if snap.funding_pct is not None and (
            abs(snap.funding_pct) > self.cfg.funding_max_pct
        ):
            return f"FUNDING {snap.funding_pct:+.4f}% > ±{self.cfg.funding_max_pct}%"
        if snap.spread_pct is not None and snap.spread_pct > self.cfg.max_spread_pct:
            return f"СПРЕД {snap.spread_pct:.4f}% > {self.cfg.max_spread_pct}%"
        if self.plan is not None and snap.depth_notional is not None:
            need = self.cfg.depth_mult * self.plan.per_level_usd
            if snap.depth_notional < need:
                return (
                    f"ЛИКВИДНОСТЬ: глубина ${snap.depth_notional:.0f} "
                    f"< {self.cfg.depth_mult}×позиции ${need:.0f}"
                )
        return None

    def _repair(self, snap: MarketSnapshot) -> None:
        """Сверка локальных позиций с биржей (ручные закрытия, ликвидации)."""
        for side, actual in (("Buy", snap.long_qty), ("Sell", snap.short_qty)):
            keys = [
                k
                for k, lvl in self.levels.items()
                if lvl.side == side and lvl.status == "position"
            ]
            if not keys:
                continue
            claimed = sum(self.levels[k].qty for k in keys)
            step = snap.instrument.qty_step
            if actual <= 0:
                for key in keys:
                    del self.levels[key]
                logger.warning(
                    "Биржа закрыла позиции %s — сброшено %d уровней",
                    side,
                    len(keys),
                )
            elif actual < claimed - step / 2:
                logger.warning(
                    "Десинхрон %s: на бирже %.8g < учтённых %.8g",
                    side,
                    actual,
                    claimed,
                )

    # ---------------------- Профиль, ATR, стабильность --------------------

    def _profile_and_atr(
        self, snap: MarketSnapshot
    ) -> tuple[VolumeProfile | None, float | None]:
        """POC/VA за окно + последний ATR; None — не хватает свечей."""
        tf = max(snap.tf_minutes, 1)
        bars = max(int(self.cfg.poc_window_min / tf), 2)
        if len(snap.candles) < bars + 2:
            return None, None
        window = snap.candles[-bars:]
        profile = volume_profile(
            [c.high for c in window],
            [c.low for c in window],
            [c.volume for c in window],
            n_bins=self.cfg.poc_bins,
            value_area_pct=self.cfg.value_area_pct,
        )
        atrs = atr_series(
            [c.high for c in snap.candles],
            [c.low for c in snap.candles],
            [c.close for c in snap.candles],
            self.cfg.atr_period,
        )
        if profile is None or not atrs or atrs[-1] <= 0:
            return None, None
        return profile, atrs[-1]

    def _push_poc(self, snap: MarketSnapshot, poc: float) -> None:
        """Копить POC по одному значению на закрытый бар (для стабильности)."""
        if not snap.candles:
            return
        bar_time = snap.candles[-1].open_time
        if bar_time == self._last_poc_bar:
            return
        self._last_poc_bar = bar_time
        self._poc_history.append(poc)

    def _poc_unstable(self) -> bool:
        """POC сдвинулся больше poc_unstable_pct за poc_unstable_bars баров."""
        hist = self._poc_history
        need = self.cfg.poc_unstable_bars + 1
        if len(hist) < need or hist[0] <= 0:
            return False
        shift = abs(hist[-1] - hist[0]) / hist[0] * 100.0
        return shift > self.cfg.poc_unstable_pct

    # --------------------------- Построение -------------------------------

    def _try_build(
        self, snap: MarketSnapshot, profile: VolumeProfile, atr_val: float
    ) -> Decision:
        """Построить план сетки или вернуть wait с причиной отказа."""
        plan, reason = self._build_plan(snap, profile, atr_val)
        if plan is None:
            return Decision("wait", f"СЕТКА НЕ ПОСТРОЕНА: {reason}")
        self.plan = plan
        self._last_recenter = snap.now
        entries = self._select_entries(snap)
        logger.info(
            "СЕТКА: POC=%.8g [%s] шаг=%.8g уровней=%d ($%.2f/уровень)",
            plan.poc,
            reason,
            plan.step,
            plan.n_levels,
            plan.per_level_usd,
        )
        return Decision(
            "build",
            f"POC={plan.poc:.8g} границы=[{plan.lower:.8g}, {plan.upper:.8g}] "
            f"шаг={plan.step:.8g} уровней={plan.n_levels} "
            f"$на уровень={plan.per_level_usd:.2f}",
            plan=plan,
            entries=entries,
        )

    def _build_plan(
        self, snap: MarketSnapshot, profile: VolumeProfile, atr_val: float
    ) -> tuple[GridPlan | None, str]:
        """Рассчитать границы, шаг, уровни и размер уровня. None + причина."""
        inst = snap.instrument
        cfg = self.cfg
        if inst.tick_size <= 0 or inst.qty_step <= 0:
            return None, "нет фильтров инструмента"

        lower = profile.val - cfg.atr_extend_mult * atr_val
        upper = profile.vah + cfg.atr_extend_mult * atr_val
        if not (0 < lower < upper):
            return None, f"некорректные границы [{lower:.8g}, {upper:.8g}]"

        tick = inst.tick_size
        step = compute_step(atr_val, profile.poc, tick, cfg)
        prices = make_levels(profile.poc, lower, upper, step, tick)
        n = len(prices)
        if n == 0:
            return None, "нет уровней: диапазон уже шага"

        if n > cfg.levels_max:
            step = ceil_to_tick((upper - lower) / (cfg.levels_max - 1), tick)
            prices = make_levels(profile.poc, lower, upper, step, tick)
            while len(prices) > cfg.levels_max:
                step = ceil_to_tick(step + tick, tick)
                prices = make_levels(profile.poc, lower, upper, step, tick)
            n = len(prices)
        elif n < cfg.levels_min:
            floor_step = ceil_to_tick(
                max(
                    cfg.min_tick_mult * tick,
                    cfg.step_fee_mult * cfg.fee_taker * profile.poc,
                ),
                tick,
            )
            want = floor_to_tick((upper - lower) / max(cfg.levels_min - 1, 1), tick)
            relaxed = max(want, floor_step)
            if relaxed < step:
                step = relaxed
                prices = make_levels(profile.poc, lower, upper, step, tick)
                n = len(prices)

        if n <= 0:
            return None, "нет уровней после коррекции шага"

        needed = max(inst.min_notional, inst.min_qty * upper)
        exposure_budget = snap.equity * cfg.max_exposure_pct / 100.0
        max_n = int(exposure_budget // needed) if needed > 0 else cfg.levels_max
        if max_n < cfg.levels_min:
            return None, (
                f"экспозиция {cfg.max_exposure_pct}% от ${snap.equity:.2f} "
                f"не вмещает {cfg.levels_min} уровней × ${needed:.2f} мин. {BUILD_RETRY_NOTE}"
            )
        if n > max_n:
            prices = prices[:max_n]
            n = max_n

        per_level = size_levels(snap.equity, profile.poc, lower, upper, atr_val, n, cfg)
        if per_level is None or per_level < needed:
            got = per_level or 0.0
            return None, (
                f"размер уровня ${got:.2f} < минимума ${needed:.2f} "
                f"(депозит ${snap.equity:.2f})"
            )

        longs = tuple(p for p in sorted(prices) if p < profile.poc)
        shorts = tuple(p for p in sorted(prices) if p > profile.poc)
        note = "ок"
        if n < cfg.levels_min:
            note = f"уровней {n} < {cfg.levels_min}: минимальный шаг/комиссия не дают больше"
        plan = GridPlan(
            poc=profile.poc,
            vah=profile.vah,
            val=profile.val,
            lower=lower,
            upper=upper,
            step=step,
            atr=atr_val,
            levels_long=longs,
            levels_short=shorts,
            per_level_usd=per_level,
            built_at=snap.now,
        )
        return plan, note

    def _rebuild_reason(
        self, snap: MarketSnapshot, profile: VolumeProfile
    ) -> str | None:
        """Триггеры перестроения: выход за VA, сдвиг POC, плановый интервал."""
        plan = self.plan
        if plan is None:
            return None
        if snap.price > profile.vah or snap.price < profile.val:
            side = "VAH" if snap.price > profile.vah else "VAL"
            return f"выход цены за {side}"
        if plan.poc > 0:
            shift = abs(profile.poc - plan.poc) / plan.poc * 100.0
            if shift > self.cfg.poc_recenter_pct:
                return f"POC сместился на {shift:.2f}% > {self.cfg.poc_recenter_pct}%"
        if snap.now - self._last_recenter >= self.cfg.recenter_interval_s:
            return f"плановый re-center каждые {self.cfg.recenter_interval_s:.0f}с"
        return None

    # --------------------------- Выбор входов -----------------------------

    def _blocked_sides(self, snap: MarketSnapshot) -> set[str]:
        """Anti-martingale: тренд против открытых позиций → доборные отключены."""
        blocked: set[str] = set()
        has_long = any(
            l.side == "Buy" and l.status == "position" for l in self.levels.values()
        )
        has_short = any(
            l.side == "Sell" and l.status == "position" for l in self.levels.values()
        )
        if not (has_long or has_short):
            return blocked
        closes = [c.close for c in snap.candles]
        if len(closes) < self.cfg.trend_ema_slow + 1:
            return blocked
        fast = ema_series(closes, self.cfg.trend_ema_fast)
        slow = ema_series(closes, self.cfg.trend_ema_slow)
        if not fast or not slow or slow[-1] <= 0:
            return blocked
        trend = (fast[-1] - slow[-1]) / slow[-1] * 100.0
        if has_long and trend < -self.cfg.trend_threshold_pct:
            blocked.add("Buy")
        if has_short and trend > self.cfg.trend_threshold_pct:
            blocked.add("Sell")
        return blocked

    def _full_sides(self) -> set[str]:
        """Стороны, где открыто max_open_levels позиций."""
        counts = {"Buy": 0, "Sell": 0}
        for level in self.levels.values():
            if level.status == "position":
                counts[level.side] += 1
        return {side for side, n in counts.items() if n >= self.cfg.max_open_levels}

    def _select_entries(self, snap: MarketSnapshot) -> list[PlannedEntry]:
        """Уровни для входа: свободные, не заблокированные, без пересечения стакана."""
        plan = self.plan
        if plan is None:
            return []
        blocked = self._blocked_sides(snap) | self._full_sides()
        entries: list[PlannedEntry] = []
        sides = (("Buy", plan.levels_long), ("Sell", plan.levels_short))
        for side, prices in sides:
            if side in blocked:
                continue
            for price in prices:
                if level_key(side, price) in self.levels:
                    continue
                if side == "Buy" and price >= snap.ask:
                    continue
                if side == "Sell" and price <= snap.bid:
                    continue
                entries.append(
                    PlannedEntry(level_price=price, side=side, usd=plan.per_level_usd)
                )
        return entries

    def _run(self, snap: MarketSnapshot, note: str) -> Decision:
        """Обычный цикл активной сетки: входы + снятие запрещённых сторон."""
        plan = self.plan
        blocked = self._blocked_sides(snap)
        full = self._full_sides()
        cancel = tuple(sorted(blocked | full))
        entries = self._select_entries(snap)
        status = note
        if plan is not None:
            longs = sum(
                1
                for l in self.levels.values()
                if l.side == "Buy" and l.status == "position"
            )
            shorts = sum(
                1
                for l in self.levels.values()
                if l.side == "Sell" and l.status == "position"
            )
            status = (
                f"POC={plan.poc:.8g} цена={snap.price:.8g} "
                f"коридор=[{plan.lower:.8g}, {plan.upper:.8g}] "
                f"long={longs} short={shorts} новых={len(entries)} | {note}"
            )
        return Decision("run", status, entries=entries, cancel_sides=cancel)

    # --------------------------- Сериализация -----------------------------

    def to_dict(self) -> dict:
        """Сериализовать состояние для grid_state.json."""
        return {
            "version": 1,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "plan": asdict(self.plan) if self.plan else None,
            "levels": {k: asdict(v) for k, v in self.levels.items()},
            "poc_history": list(self._poc_history),
            "equity_samples": [list(s) for s in self._equity_samples],
            "session_base": self._session_base,
            "peak_equity": self._peak_equity,
            "trailing_armed": self._trailing_armed,
            "impulse_until": self._impulse_until,
            "last_impulse_bar": self._last_impulse_bar,
            "last_poc_bar": self._last_poc_bar,
            "last_recenter": self._last_recenter,
            "sl_orders": dict(self._sl_orders),
            "sl_qty": dict(self._sl_qty),
            "sl_trigger": dict(self._sl_trigger),
        }

    def restore(self, data: dict) -> None:
        """Восстановить состояние из grid_state.json (после рестарта)."""
        if data.get("version") != 1:
            logger.warning("Неизвестная версия grid_state — состояние сброшено")
            return
        self.halted = bool(data.get("halted", False))
        self.halt_reason = str(data.get("halt_reason", ""))
        plan_data = data.get("plan")
        if plan_data:
            plan_data = dict(plan_data)
            plan_data["levels_long"] = tuple(plan_data.get("levels_long") or ())
            plan_data["levels_short"] = tuple(plan_data.get("levels_short") or ())
            self.plan = GridPlan(**plan_data)
        self.levels = {
            key: LevelState(**value)
            for key, value in (data.get("levels") or {}).items()
        }
        self._poc_history = deque(
            data.get("poc_history") or (), maxlen=self.cfg.poc_unstable_bars + 1
        )
        self._equity_samples = deque(
            (float(ts), float(eq)) for ts, eq in (data.get("equity_samples") or [])
        )
        self._session_base = float(data.get("session_base") or 0.0)
        self._peak_equity = float(data.get("peak_equity") or 0.0)
        self._trailing_armed = bool(data.get("trailing_armed", False))
        self._impulse_until = float(data.get("impulse_until") or 0.0)
        self._last_impulse_bar = int(data.get("last_impulse_bar") or -1)
        self._last_poc_bar = int(data.get("last_poc_bar") or -1)
        self._last_recenter = float(data.get("last_recenter") or 0.0)
        self._sl_orders = dict(data.get("sl_orders") or {})
        self._sl_qty = {k: float(v) for k, v in (data.get("sl_qty") or {}).items()}
        self._sl_trigger = {
            k: float(v) for k, v in (data.get("sl_trigger") or {}).items()
        }
        logger.info(
            "Состояние восстановлено: сетка=%s уровней=%d halted=%s",
            "есть" if self.plan else "нет",
            len(self.levels),
            self.halted,
        )
