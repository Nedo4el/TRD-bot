"""HTTP-сервер дашборда (Starlette + uvicorn)."""

from __future__ import annotations

import logging
import threading
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from dashboard import tiger
from dashboard.config import DashboardConfig
from dashboard.positions import close_robot_position, list_open_positions
from dashboard.robots import (
    get_all_robots_status,
    get_robot_symbols,
    set_robot_symbols,
    start_robot,
    stop_all_robots,
    stop_robot,
)
from dashboard.scheduler import Scheduler
from dashboard.settings import AllCoinsSettings, ImpulseSettings
from dashboard.store import SignalStore, now_iso
from dashboard.symbols import SymbolsCache
from dashboard.trade_store import TradeStore

logger = logging.getLogger(__name__)

INDEX_FILE = Path(__file__).resolve().parent / "static" / "index.html"
_NO_CACHE = {"Cache-Control": "no-store"}


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    """Поднять планировщик при старте и остановить при выходе."""
    config = DashboardConfig.from_env()
    specs = config.resolved_specs()
    store = SignalStore(specs)
    scheduler = Scheduler(specs, store, config)
    trade_store = TradeStore()
    app.state.config = config
    app.state.store = store
    app.state.scheduler = scheduler
    app.state.trade_store = trade_store
    app.state.symbols_cache = SymbolsCache()
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


async def homepage(request: Request) -> HTMLResponse:
    """Отдать HTML-страницу дашборда."""
    return HTMLResponse(
        INDEX_FILE.read_text(encoding="utf-8"),
        headers=_NO_CACHE,
    )


async def api_signals(request: Request) -> JSONResponse:
    """Текущий снимок сигналов и статусов."""
    store: SignalStore = request.app.state.store
    return JSONResponse(store.snapshot(), headers=_NO_CACHE)


async def api_scan_all(request: Request) -> JSONResponse:
    """Запустить сканирование всех скринеров сейчас."""
    scheduler: Scheduler = request.app.state.scheduler
    await scheduler.trigger()
    return JSONResponse({"ok": True})


async def api_scan_one(request: Request) -> JSONResponse:
    """Запустить сканирование одного скринера сейчас."""
    scheduler: Scheduler = request.app.state.scheduler
    name = request.path_params["name"]
    if not await scheduler.trigger(name):
        return JSONResponse(
            {"ok": False, "error": "unknown screener"},
            status_code=404,
        )
    return JSONResponse({"ok": True, "screener": name})


async def api_health(request: Request) -> JSONResponse:
    """Проверка живости сервера."""
    return JSONResponse({"ok": True})


async def api_settings_all_get(request: Request) -> JSONResponse:
    """Текущие настройки «Все монеты»."""
    return JSONResponse(AllCoinsSettings.load().to_dict(), headers=_NO_CACHE)


async def api_settings_all_post(request: Request) -> JSONResponse:
    """Сохранить настройки «Все монеты» и пересканировать."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "JSON object required"}, status_code=400
        )
    settings = AllCoinsSettings.from_dict(body)
    settings.save()
    scheduler: Scheduler = request.app.state.scheduler
    await scheduler.trigger("all")
    return JSONResponse({"ok": True, "settings": settings.to_dict()})


def _bad_symbol(symbol: str) -> bool:
    """Тикер должен быть коротким алфанумерическим кодом."""
    return not symbol or len(symbol) > 24 or not symbol.isalnum()


async def api_settings_impulse_get(request: Request) -> JSONResponse:
    """Текущие настройки скринера «Импульс»."""
    return JSONResponse(
        ImpulseSettings.load().to_dict(),
        headers=_NO_CACHE,
    )


async def api_settings_impulse_post(request: Request) -> JSONResponse:
    """Сохранить настройки «Импульс» и пересканировать."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "JSON object required"}, status_code=400
        )
    settings = ImpulseSettings.from_dict(body)
    settings.save()
    scheduler: Scheduler = request.app.state.scheduler
    await scheduler.trigger("impulse")
    return JSONResponse({"ok": True, "settings": settings.to_dict()})


async def api_robots_list(request: Request) -> JSONResponse:
    """Список всех роботов и их статусы."""
    statuses = get_all_robots_status()
    data = [
        {
            "name": s.name,
            "running": s.running,
            "pid": s.pid,
            "error": s.error,
            "symbols": s.symbols,
            "symbol_pids": s.symbol_pids,
        }
        for s in statuses
    ]
    return JSONResponse({"ok": True, "robots": data}, headers=_NO_CACHE)


async def api_bybit_symbols(request: Request) -> JSONResponse:
    """Список монет Bybit USDT-M (для окна выбора)."""
    cache: SymbolsCache = request.app.state.symbols_cache
    force = request.query_params.get("refresh", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    symbols = await cache.get(force=force)
    return JSONResponse(
        {"ok": True, "symbols": symbols, "total": len(symbols)},
        headers=_NO_CACHE,
    )


async def api_robot_symbols_get(request: Request) -> JSONResponse:
    """Список торгуемых монет робота."""
    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"ok": False, "error": "robot name required"}, status_code=400
        )
    status = get_all_robots_status()
    current = next((s for s in status if s.name == name), None)
    return JSONResponse(
        {
            "ok": True,
            "robot": name,
            "symbols": get_robot_symbols(name),
            "symbol_pids": current.symbol_pids if current else {},
        },
        headers=_NO_CACHE,
    )


async def api_robot_symbols_post(request: Request) -> JSONResponse:
    """Изменить список монет робота: add / remove / symbols."""
    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"ok": False, "error": "robot name required"}, status_code=400
        )
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "JSON object required"}, status_code=400
        )

    current = get_robot_symbols(name)
    if "symbols" in body:
        raw = body.get("symbols")
        if not isinstance(raw, list):
            return JSONResponse(
                {"ok": False, "error": "symbols: список ожидается"}, status_code=400
            )
        wanted = [str(s).strip().upper() for s in raw if str(s).strip()]
        bad = [s for s in wanted if _bad_symbol(s)]
        if bad:
            return JSONResponse(
                {"ok": False, "error": f"неверные тикеры: {', '.join(bad)}"},
                status_code=400,
            )
        ok, message = set_robot_symbols(name, wanted)
    else:
        add_raw = body.get("add") or []
        remove_raw = body.get("remove") or []
        if not isinstance(add_raw, list) or not isinstance(remove_raw, list):
            return JSONResponse(
                {"ok": False, "error": "add/remove: списки ожидаются"},
                status_code=400,
            )
        add = [str(s).strip().upper() for s in add_raw if str(s).strip()]
        remove = [str(s).strip().upper() for s in remove_raw if str(s).strip()]
        bad = [s for s in [*add, *remove] if _bad_symbol(s)]
        if bad:
            return JSONResponse(
                {"ok": False, "error": f"неверные тикеры: {', '.join(bad)}"},
                status_code=400,
            )
        symbols = [s for s in current if s not in remove]
        for symbol in add:
            if symbol not in symbols:
                symbols.append(symbol)
        ok, message = set_robot_symbols(name, symbols)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": message}, status_code=400, headers=_NO_CACHE
            )
    if not ok:
        return JSONResponse(
            {"ok": False, "error": message}, status_code=400, headers=_NO_CACHE
        )
    logger.info("Монеты робота %s: %s", name, message)
    return JSONResponse(
        {
            "ok": True,
            "robot": name,
            "symbols": get_robot_symbols(name),
            "message": message,
        },
        headers=_NO_CACHE,
    )


async def api_robot_start(request: Request) -> JSONResponse:
    """Запустить робота."""
    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"ok": False, "error": "robot name required"}, status_code=400
        )
    success, message = start_robot(name)
    return JSONResponse(
        {"ok": success, "message": message, "robot": name},
        status_code=200 if success else 400,
        headers=_NO_CACHE,
    )


async def api_robot_stop(request: Request) -> JSONResponse:
    """Остановить робота."""
    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"ok": False, "error": "robot name required"}, status_code=400
        )
    success, message = stop_robot(name)
    return JSONResponse(
        {"ok": success, "message": message, "robot": name},
        status_code=200 if success else 400,
        headers=_NO_CACHE,
    )


async def api_robots_stop_all(request: Request) -> JSONResponse:
    """Остановить все роботы."""
    results = stop_all_robots()
    return JSONResponse(
        {
            "ok": True,
            "results": {
                name: {"success": ok, "message": msg}
                for name, (ok, msg) in results.items()
            },
        },
        headers=_NO_CACHE,
    )


async def api_robot_statistics(request: Request) -> JSONResponse:
    """Статистика сделок робота за период."""
    trade_store: TradeStore = request.app.state.trade_store
    robot = request.query_params.get("robot", "all")
    days_raw = request.query_params.get("days", "30")
    try:
        days = int(days_raw)
    except ValueError:
        days = 30
    if days < 1 or days > 365:
        days = 30
    robot_param: str | None = None if robot == "all" else robot
    stats = trade_store.stats(robot_param, days)
    trades = trade_store.get_trades(robot_param, days)
    return JSONResponse(
        {
            "ok": True,
            "robot": robot or "all",
            "days": days,
            "metrics": asdict(stats),
            "trades": [t.to_dict() for t in trades[:200]],
        },
        headers=_NO_CACHE,
    )


async def api_robot_statistics_clear(request: Request) -> JSONResponse:
    """Очистить историю сделок (всех или одного робота: ?robot=name)."""
    trade_store: TradeStore = request.app.state.trade_store
    robot = request.query_params.get("robot", "").strip() or None
    removed = trade_store.clear(robot)
    logger.info("История сделок очищена (%s): %d записей", robot or "все", removed)
    return JSONResponse(
        {"ok": True, "removed": removed, "robot": robot or "all"},
        headers=_NO_CACHE,
    )


async def api_positions(request: Request) -> JSONResponse:
    """Открытые позиции всех роботов (биржа + state-файлы)."""
    positions, errors = await list_open_positions()
    return JSONResponse(
        {
            "ok": True,
            "generated_at": now_iso(),
            "positions": positions,
            "errors": errors,
        },
        headers=_NO_CACHE,
    )


async def api_positions_close(request: Request) -> JSONResponse:
    """Закрыть позицию робота по рынку: {robot, symbol, pct?}."""
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "JSON object required"}, status_code=400
        )
    robot = str(body.get("robot") or "").strip()
    symbol = str(body.get("symbol") or "").strip().upper()
    if not robot:
        return JSONResponse({"ok": False, "error": "robot обязателен"}, status_code=400)
    if _bad_symbol(symbol):
        return JSONResponse({"ok": False, "error": "неверный тикер"}, status_code=400)
    try:
        pct = float(body.get("pct", 100))
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "pct: число ожидается"}, status_code=400
        )
    ok, message = await close_robot_position(robot, symbol, pct)
    return JSONResponse(
        {"ok": ok, "message": message, "robot": robot, "symbol": symbol},
        status_code=200 if ok else 400,
        headers=_NO_CACHE,
    )


async def api_open_tiger(request: Request) -> JSONResponse:
    """Открыть инструмент в терминале Tiger Trade через его локальный API."""
    config: DashboardConfig = request.app.state.config
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    symbol = str(body.get("symbol") or "").strip().upper()
    mode = str(body.get("mode") or config.tiger_mode).strip().lower()
    if _bad_symbol(symbol):
        return JSONResponse(
            {"ok": False, "error": "неверный тикер"},
            status_code=400,
        )
    if mode not in tiger.MODES:
        return JSONResponse(
            {"ok": False, "error": "неизвестный режим"},
            status_code=400,
        )
    if mode == "combo":
        ok, message = await tiger.open_combo(
            symbol,
            link_group=config.tiger_link_group,
            url=config.tiger_url,
        )
    else:
        ok, message = await tiger.send(
            tiger.build_request(
                symbol,
                mode=mode,
                link_group=config.tiger_link_group,
            ),
            url=config.tiger_url,
        )
    if not ok:
        logger.info("Tiger: %s (%s) — %s", symbol, mode, message)
    return JSONResponse(
        {"ok": ok, "message": message, "symbol": symbol, "mode": mode},
        headers=_NO_CACHE,
    )


def create_app() -> Starlette:
    """Собрать ASGI-приложение."""
    routes = [
        Route("/", homepage),
        Route("/api/signals", api_signals),
        Route("/api/scan", api_scan_all, methods=["POST"]),
        Route("/api/scan/{name}", api_scan_one, methods=["POST"]),
        Route("/api/open-tiger", api_open_tiger, methods=["POST"]),
        Route("/api/positions", api_positions),
        Route("/api/positions/close", api_positions_close, methods=["POST"]),
        Route("/api/health", api_health),
        Route("/api/settings/all", api_settings_all_get),
        Route("/api/settings/all", api_settings_all_post, methods=["POST"]),
        Route("/api/settings/impulse", api_settings_impulse_get),
        Route(
            "/api/settings/impulse",
            api_settings_impulse_post,
            methods=["POST"],
        ),
        Route("/api/robots", api_robots_list),
        Route("/api/bybit/symbols", api_bybit_symbols),
        Route("/api/robots/{name}/start", api_robot_start, methods=["POST"]),
        Route("/api/robots/{name}/stop", api_robot_stop, methods=["POST"]),
        Route("/api/robots/{name}/symbols", api_robot_symbols_get),
        Route(
            "/api/robots/{name}/symbols",
            api_robot_symbols_post,
            methods=["POST"],
        ),
        Route("/api/robots/stop-all", api_robots_stop_all, methods=["POST"]),
        Route("/api/robots/statistics", api_robot_statistics, methods=["GET"]),
        Route(
            "/api/robots/statistics/clear",
            api_robot_statistics_clear,
            methods=["POST"],
        ),
        Route("/favicon.ico", lambda request: Response(status_code=204)),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def _open_browser(url: str) -> None:
    """Открыть дашборд в браузере по умолчанию."""
    try:
        webbrowser.open(url)
    except OSError as exc:
        logger.warning("Не удалось открыть браузер: %s", exc)


def run(config: DashboardConfig | None = None) -> None:
    """Запустить сервер одной командой."""
    settings = config or DashboardConfig.from_env()
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    url = f"http://{host}:{settings.port}"
    logger.info("Дашборд: %s (скринеры: %s)", url, ", ".join(settings.enabled))
    if settings.open_browser:
        timer = threading.Timer(1.5, _open_browser, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )
