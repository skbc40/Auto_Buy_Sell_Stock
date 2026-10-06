"""기준봉 단타 자동매매 실행: uv run main.py (설정은 config.yaml)"""
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from kiwoom.core.runtime import get_client, get_ws_client
from kiwoom.realtime.packets import build_reg_packet, build_remove_packet

from broker import KiwoomBroker, norm_code, to_float, to_int
from trader import Trader, hm

WS_PATH = "/api/dostk/websocket"
CHUNK = 50  # 실시간 등록 1회 패킷당 종목 수
MODES = {"demo": "모의투자", "real": "실제투자"}
log = logging.getLogger("main")


def setup_logging() -> Path:
    logs = Path("logs")
    logs.mkdir(exist_ok=True)
    day = f"{datetime.now():%Y%m%d}"
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in [h for h in root.handlers if getattr(h, "daytrader", False)]:  # 재시작 시 중복 방지
        root.removeHandler(h)
        h.close()
    handlers = [logging.FileHandler(logs / f"run_{day}.log", encoding="utf-8")]
    if sys.stdout:  # GUI(pythonw)에서는 콘솔이 없다
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.daytrader = True
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("kiwoom").setLevel(logging.WARNING)  # 라이브러리의 메시지별 로그는 끈다
    return logs / f"trades_{day}.csv"


def handle(msg, trader: Trader) -> None:
    if not isinstance(msg, dict):
        return
    trnm = msg.get("trnm")
    if trnm in ("REG", "REMOVE"):
        if str(msg.get("return_code", "0")).strip() not in ("0", ""):
            log.error("실시간 %s 실패: %s", trnm, msg.get("return_msg"))
        return
    if trnm != "REAL":
        return
    now = datetime.now()
    for d in msg.get("data") or []:
        v = d.get("values") or {}
        if d.get("type") == "0B":
            hhmmss = str(v.get("20", "")).strip()
            if len(hhmmss) != 6:
                continue
            t = datetime.combine(now.date(), datetime.strptime(hhmmss, "%H%M%S").time())
            trader.on_tick(norm_code(d.get("item")), t, to_int(v.get("10")), to_int(v.get("15")),
                           to_float(v.get("12")), now)
        elif d.get("type") == "00":
            status = str(v.get("913", "")).strip()
            ord_no = str(v.get("9203", "")).strip().lstrip("0")
            side = "sell" if str(v.get("907", "")).strip() == "1" else "buy"
            if status == "체결" and to_int(v.get("915")) > 0:
                trader.on_fill(ord_no, norm_code(v.get("9001")), side, to_int(v.get("915")), to_int(v.get("914")), now)
            elif status == "거부":
                trader.on_reject(ord_no, now)
            elif status in ("확인", "취소"):  # 취소 주문 확인: 904(원주문번호)가 취소된 주문
                orig = str(v.get("904", "")).strip().lstrip("0")
                trader.on_cancelled(orig or ord_no, now)


async def read_loop(ws, trader: Trader) -> None:
    async for msg in ws.iter_messages():
        handle(msg, trader)


async def send_chunks(ws, codes: list[str], build) -> None:
    for i in range(0, len(codes), CHUNK):
        await ws.send(build(codes[i:i + CHUNK], ["0B"]))


async def run(cfg: dict, halt=None, state: dict | None = None) -> None:
    """halt(threading.Event)가 설정되면 멈춘다. state 에는 GUI 가 읽을 trader/broker/연결 상태를 넣는다."""
    mode = cfg.get("mode")
    if mode not in MODES:
        sys.exit("config.yaml 의 mode 는 demo(모의투자) 또는 real(실제투자) 이어야 합니다.")
    state = {} if state is None else state
    halted = lambda: halt is not None and halt.is_set()  # noqa: E731
    trades = setup_logging()
    u = cfg["universe"]
    broker = KiwoomBroker(get_client(mode=mode), cfg["api"]["rate_per_sec"], u["stex_tp"], cfg["order"]["exchange"])
    trader = Trader(cfg, broker, trades)
    trader.excluded = broker.holding_codes()
    state.update(trader=trader, broker=broker, mode=mode, connected=False, subscribed=0)
    log.info("시작: %s / 예수금 %s원 / 시작 시 보유 종목(건드리지 않음): %s",
             MODES[mode], f"{state.setdefault('cash', broker.cash()):,}",
             sorted(trader.excluded) or "없음")

    ws = get_ws_client(mode=mode)
    stop = hm(cfg["session"]["stop"])
    suffix = u["tick_suffix"]
    while datetime.now().time() < stop and not halted():
        subscribed: set[str] = set()
        reader = None
        try:
            await ws.connect(api_url=WS_PATH)
            log.info("웹소켓 연결됨")
            state["connected"] = True
            await ws.send(build_reg_packet([""], ["00"]))  # 내 계좌 주문체결
            reader = asyncio.create_task(read_loop(ws, trader))
            next_rank, next_status = 0.0, 0.0
            while not reader.done() and datetime.now().time() < stop and not halted():
                now = datetime.now()
                if time.monotonic() >= next_rank:
                    next_rank = time.monotonic() + u["rank_interval_sec"]
                    try:
                        wanted = trader.update_rank(await asyncio.to_thread(broker.rank))
                    except Exception as e:
                        log.error("순위 조회 실패: %s", e)
                        wanted = subscribed | set(trader.pos)  # 보유 종목 시세는 놓치지 않는다
                    add, remove = sorted(wanted - subscribed), sorted(subscribed - wanted)
                    await send_chunks(ws, [c + suffix for c in add], build_reg_packet)
                    trader.mark_subscribed(add, now)
                    await send_chunks(ws, [c + suffix for c in remove], build_remove_packet)
                    trader.mark_unsubscribed(remove)
                    subscribed = wanted
                    state["subscribed"] = len(subscribed)
                trader.on_clock(now)
                if time.monotonic() >= next_status:
                    next_status = time.monotonic() + 60
                    watch = [trader._label(c) for c in subscribed if trader.watching(c)]
                    log.info("상태: 구독 %s종목 / 최근 1분 틱 %s건 / 조건충족 %s / 포지션 %s",
                             len(subscribed), trader.ticks, watch or "없음",
                             {trader._label(c): p.qty for c, p in trader.pos.items()} or "없음")
                    trader.ticks = 0
                await asyncio.sleep(0.2)
            if reader.done():
                reader.result()  # 읽기 중 예외가 있었다면 여기서 올라온다
                log.warning("웹소켓 연결이 끊김")
        except Exception as e:
            log.error("웹소켓 오류: %s", e)
        finally:
            state["connected"] = False
            if reader:
                reader.cancel()
            try:
                await ws.close()
            except Exception:
                pass
        if datetime.now().time() < stop and not halted():
            log.info("3초 후 재연결")
            await asyncio.sleep(3)
    trader.on_clock(datetime.now())
    log.info("종료: 남은 포지션 %s / 예수금 %s원",
             {trader._label(c): p.qty for c, p in trader.pos.items()} or "없음", f"{broker.cash():,}")


def wait_for_session(cfg: dict, halt=None) -> bool:
    """장 시간이 지났거나 주말이면 다음 평일 시작 5분 전까지 기다린다. halt 로 중단되면 False."""
    now = datetime.now()
    if now.weekday() < 5 and now.time() < hm(cfg["session"]["stop"]):
        return True
    day = now.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    # ponytail: 공휴일은 모름 — 휴장일엔 틱 없이 돌다가 종료된다
    target = datetime.combine(day, hm(cfg["session"]["start"])) - timedelta(minutes=5)
    msg = f"장 시간이 아닙니다. {target:%m/%d(%a) %H:%M} 까지 기다렸다가 시작합니다."
    log.info(msg)
    if sys.stdout:
        print(msg, flush=True)
    wait = (target - now).total_seconds()
    if halt is None:
        time.sleep(wait)
        return True
    return not halt.wait(wait)


if __name__ == "__main__":
    with open("config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if config.get("mode") == "real" and input("실제투자 모드입니다. 실제 돈으로 주문합니다. 진행하려면 '실제투자' 입력: ").strip() != "실제투자":
        sys.exit("취소했습니다.")
    wait_for_session(config)
    asyncio.run(run(config))
