"""기준봉 단타 매매 로직. 시세·체결 이벤트와 현재 시각만 받아 판단하고 브로커로 주문한다."""
import csv
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from broker import RankRow

log = logging.getLogger("trader")
MIN = timedelta(minutes=1)
GRACE = timedelta(seconds=1)   # 분이 바뀐 뒤 늦게 오는 틱을 기다리는 여유
RETRY = timedelta(seconds=5)   # 주문 실패 시 재시도 간격


def hm(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


@dataclass
class Bar:
    start: datetime   # 봉 시작 시각 (09:10봉 = 09:10:00~09:10:59)
    open: int
    high: int
    low: int
    close: int
    volume: int


class BarBuilder:
    """틱을 모아 종목별 1분봉을 만든다."""

    def __init__(self) -> None:
        self.cur: dict[str, Bar] = {}
        self.last_start: dict[str, datetime] = {}

    def on_tick(self, code: str, t: datetime, price: int, qty: int) -> list[Bar]:
        """이 틱 때문에 완성된 봉(직전 분)을 돌려준다."""
        m = t.replace(second=0, microsecond=0)
        if code in self.last_start and m <= self.last_start[code]:
            return []  # 이미 마감한 분의 늦은 틱
        out = []
        cur = self.cur.get(code)
        if cur and m > cur.start:
            out.append(self._close(code))
            cur = None
        if cur is None:
            self.cur[code] = Bar(m, price, price, price, price, qty)
        else:
            cur.high, cur.low, cur.close = max(cur.high, price), min(cur.low, price), price
            cur.volume += qty
        return out

    def close_due(self, now: datetime) -> list[tuple[str, Bar]]:
        """다음 분 틱이 없어도 시간이 지나면 봉을 마감한다."""
        due = [c for c, b in self.cur.items() if now >= b.start + MIN + GRACE]
        return [(c, self._close(c)) for c in due]

    def reset(self, code: str) -> None:
        self.cur.pop(code, None)

    def _close(self, code: str) -> Bar:
        b = self.cur.pop(code)
        self.last_start[code] = b.start
        return b


@dataclass
class Position:
    code: str
    name: str
    base_close: int                 # 기준봉 종가
    ready: datetime                 # 기준봉 완성 시각 (N분 후 계산의 기준)
    bought: bool = False            # 매수 주문을 냈는가 (False 면 매수 대기 중)
    qty: int = 0                    # 체결된 보유 수량
    avg: float = 0.0                # 평균 매수가
    pending_sell: int = 0           # 매도 주문을 냈지만 아직 체결 안 된 수량
    partial_done: bool = False
    judged: bool = False
    close_reason: str | None = None  # 설정되면 남은 수량을 모두 판다
    retry_at: datetime = datetime.min


@dataclass
class Order:
    code: str
    side: str      # buy | sell
    qty: int
    reason: str
    sent_at: datetime
    filled: int = 0
    cancelling: bool = False


class Trader:
    def __init__(self, cfg: dict, broker, trades_path: Path | None = None) -> None:
        self.broker = broker
        self.trades_path = trades_path
        s, u, sig, ex = cfg["session"], cfg["universe"], cfg["signal"], cfg["exit"]
        self.start, self.entry_end, self.force_close = hm(s["start"]), hm(s["entry_end"]), hm(s["force_close"])
        self.skip_first = {hm(x) for x in s["skip_first_bars"]}
        self.rank_top, self.min_change, self.pool_size = u["rank_top"], u["min_change_pct"], u["pool_size"]
        self.vol_ratio, self.close_up = sig["volume_ratio"], sig["close_up_pct"]
        self.buy_delay = timedelta(minutes=sig["buy_delay_min"])
        self.judge_after = timedelta(minutes=sig["judge_after_min"])
        self.judge_up = sig["judge_up_pct"]
        self.exit_after = timedelta(minutes=sig["exit_after_min"])
        self.stop_loss, self.partial_take, self.partial_ratio = ex["stop_loss_pct"], ex["partial_take_pct"], ex["partial_ratio"]
        self.max_positions = cfg["max_positions"]
        self.order_timeout = timedelta(seconds=cfg["order"]["timeout_sec"])
        self.sizing = cfg["sizing"]

        self.bars = BarBuilder()
        self.prev: dict[str, Bar] = {}            # 종목별 마지막 완성 봉
        self.ranks: dict[str, RankRow] = {}
        self.names: dict[str, str] = {}
        self.subscribed_at: dict[str, datetime] = {}
        self.price: dict[str, int] = {}
        self.change: dict[str, float] = {}
        self.entered: set[str] = set()            # 오늘 진입한 종목 (재진입 금지)
        self.excluded: set[str] = set()           # 시작 시 이미 보유 중이던 종목 (건드리지 않음)
        self.pos: dict[str, Position] = {}        # 매수 대기 + 보유 (슬롯 수 = len)
        self.orders: dict[str, Order] = {}
        self.base_cash: int | None = None
        self.ticks = 0

    # ---- 감시 대상 ----
    def update_rank(self, rows: list[RankRow]) -> set[str]:
        """순위를 갱신하고 실시간 시세를 받아야 할 종목 코드를 돌려준다."""
        self.ranks = {r.code: r for r in rows}
        for r in rows:
            self.names[r.code] = r.name
            self.change[r.code] = r.change_pct
            self.price.setdefault(r.code, r.price)
        pool = sorted(rows, key=lambda r: r.rank)[: self.pool_size]
        return {r.code for r in pool} | set(self.pos)

    def mark_subscribed(self, codes, now: datetime) -> None:
        """구독(또는 재연결) 시점 이전의 봉은 불완전하므로 기준봉 비교에 쓰지 않는다."""
        for c in codes:
            self.subscribed_at[c] = now
            self.bars.reset(c)
            self.prev.pop(c, None)

    def mark_unsubscribed(self, codes) -> None:
        for c in codes:
            self.subscribed_at.pop(c, None)
            self.bars.reset(c)
            self.prev.pop(c, None)

    def watching(self, code: str) -> bool:
        r = self.ranks.get(code)
        return r is not None and r.rank <= self.rank_top and self.change.get(code, 0.0) >= self.min_change

    # ---- 이벤트 ----
    def on_tick(self, code: str, t: datetime, price: int, qty: int, change: float, now: datetime) -> None:
        self.ticks += 1
        self.price[code] = price
        self.change[code] = change
        for bar in self.bars.on_tick(code, t, price, qty):
            self._on_bar(code, bar, now)
        p = self.pos.get(code)
        if (p and p.qty > 0 and not p.partial_done and p.close_reason is None and now >= p.retry_at
                and price >= p.avg * (1 + self.partial_take / 100)):
            q = int(p.qty * self.partial_ratio)
            if q < 1:
                p.partial_done = True
            elif self._sell(p, q, f"+{self.partial_take}% 부분익절", now):
                p.partial_done = True

    def on_clock(self, now: datetime) -> None:
        for code, bar in self.bars.close_due(now):
            self._on_bar(code, bar, now)
        for ord_no, o in list(self.orders.items()):
            # 최유리지정가는 일부만 체결되고 잔량이 남을 수 있다 → 시간이 지나면 잔량 취소
            if not o.cancelling and now >= o.sent_at + self.order_timeout:
                try:
                    self.broker.cancel(ord_no, o.code)
                    o.cancelling = True
                    log.info("[%s] 미체결 잔량 취소 요청: %s %s주 중 %s주 미체결",
                             self._label(o.code), o.side, o.qty, o.qty - o.filled)
                except Exception as e:
                    log.error("[%s] 취소 실패 (재시도): %s", self._label(o.code), e)
                    o.sent_at = now
        if now.time() >= self.force_close:
            for p in list(self.pos.values()):
                if not p.bought:
                    log.info("[%s] 매수 대기 취소: 장마감 시각", self._label(p.code))
                    del self.pos[p.code]
                else:
                    self._close(p, "장마감 강제청산", now)
        for p in list(self.pos.values()):
            if not p.bought:
                if now >= p.ready + self.buy_delay:
                    self._buy(p, now)
                continue
            if not p.judged and now >= p.ready + self.judge_after + GRACE:
                p.judged = True
                last = self.prev.get(p.code)
                close = last.close if last else self.price.get(p.code, 0)
                target = p.base_close * (1 + self.judge_up / 100)
                if close < target:
                    self._close(p, f"5분 판정 미달 (종가 {close:,} < 목표 {target:,.0f})", now)
                else:
                    log.info("[%s] 5분 판정 통과 (종가 %s ≥ 목표 %s)", self._label(p.code), f"{close:,}", f"{target:,.0f}")
            if now >= p.ready + self.exit_after:
                self._close(p, "기준봉 15분 시간청산", now)
            self._flush(p, now)

    def on_fill(self, ord_no: str, code: str, side: str, qty: int, price: int, now: datetime) -> None:
        o = self.orders.get(ord_no)
        p = self.pos.get(code)
        if o is None or p is None:
            log.warning("이 프로그램이 내지 않은 주문의 체결은 무시: 주문번호 %s 종목 %s", ord_no, code)
            return
        o.filled += qty
        if o.filled >= o.qty:
            del self.orders[ord_no]
        if side == "buy":
            p.avg = (p.avg * p.qty + price * qty) / (p.qty + qty)
            p.qty += qty
            self._record(now, code, "매수", qty, price, o.reason)
            self._flush(p, now)  # 체결 전에 전량매도 사유가 생겼으면 바로 판다
        else:
            p.qty -= qty
            p.pending_sell -= qty
            self._record(now, code, "매도", qty, price, o.reason, pnl=(price - p.avg) * qty)
            if p.qty <= 0 and p.pending_sell <= 0:
                log.info("[%s] 포지션 종료", self._label(code))
                del self.pos[code]

    def on_reject(self, ord_no: str, now: datetime) -> None:
        self._order_gone(ord_no, now, "주문 거부", now + RETRY)

    def on_cancelled(self, ord_no: str, now: datetime) -> None:
        """잔량 취소 확인. 매도 잔량은 바로 새 최유리지정가로 다시 낸다."""
        self._order_gone(ord_no, now, "잔량 취소됨", now)

    def _order_gone(self, ord_no: str, now: datetime, what: str, retry_at: datetime) -> None:
        o = self.orders.pop(ord_no, None)
        if o is None:
            return
        left = o.qty - o.filled
        log.warning("[%s] %s: %s %s주 (%s)", self._label(o.code), what, o.side, left, o.reason)
        p = self.pos.get(o.code)
        if p is None:
            return
        if o.side == "sell":
            p.pending_sell -= left
            p.retry_at = retry_at  # 부분익절 잔량은 다시 내지 않는다 (절반 초과 매도 방지)
        elif p.qty == 0:
            del self.pos[o.code]

    # ---- 내부 ----
    def _on_bar(self, code: str, bar: Bar, now: datetime) -> None:
        prev = self.prev.get(code)
        self.prev[code] = bar
        p = self.pos.get(code)
        if p and p.qty > 0 and bar.close <= p.avg * (1 - self.stop_loss / 100):
            self._close(p, f"손절 (봉 종가 {bar.close:,} ≤ 매수가 {p.avg:,.0f} -{self.stop_loss}%)", now)
        self._check_signal(code, prev, bar, now)

    def _check_signal(self, code: str, prev: Bar | None, bar: Bar, now: datetime) -> None:
        if code in self.entered or code in self.excluded:
            return
        t = bar.start.time()
        if not (self.start <= t < self.entry_end) or t in self.skip_first:
            return
        if prev is None or prev.start != bar.start - MIN:
            return
        sub = self.subscribed_at.get(code)
        if sub is None or prev.start < sub:
            return  # 직전 봉을 처음부터 보지 못했다
        if not self.watching(code):
            return
        if prev.volume <= 0 or bar.volume < prev.volume * self.vol_ratio:
            return
        if bar.close < prev.close * (1 + self.close_up / 100):
            return
        detail = (f"{bar.start:%H:%M}봉 거래량 {bar.volume:,} (직전 {prev.volume:,}) "
                  f"종가 {bar.close:,} (직전 {prev.close:,})")
        if len(self.pos) >= self.max_positions:
            log.info("[%s] 기준봉 무시 (보유 한도 %s종목): %s", self._label(code), self.max_positions, detail)
            return
        if not self.pos:
            self.base_cash = self.broker.cash()
        self.entered.add(code)
        self.pos[code] = Position(code, self.names.get(code, ""), bar.close, bar.start + MIN)
        log.info("[%s] 기준봉 포착: %s → %s 매수 예정", self._label(code), detail,
                 f"{bar.start + MIN + self.buy_delay:%H:%M:%S}")

    def _amount(self) -> int:
        cash = self.broker.cash()
        if self.sizing["mode"] == "fixed":
            want = self.sizing["fixed_krw"]
        else:
            want = (self.base_cash or cash) * self.sizing["percent"] / 100
        return int(min(want, cash))

    def _buy(self, p: Position, now: datetime) -> None:
        p.bought = True
        price = self.price.get(p.code, 0)
        try:
            amount = self._amount()
            qty = amount // price if price else 0
            if qty < 1:
                log.warning("[%s] 매수 건너뜀: 금액 %s원으로 1주(%s원)도 못 삼", self._label(p.code), f"{amount:,}", f"{price:,}")
                del self.pos[p.code]
                return
            ord_no = self.broker.buy(p.code, qty, price)
        except Exception as e:
            log.error("[%s] 매수 실패: %s", self._label(p.code), e)
            del self.pos[p.code]
            return
        self.orders[ord_no] = Order(p.code, "buy", qty, "기준봉 2분 후 매수", now)
        log.info("[%s] 매수 주문(최유리지정가) %s주 (현재가 %s, 금액 %s)", self._label(p.code), qty, f"{price:,}", f"{qty * price:,}")
        if self.broker.simulated:
            self.on_fill(ord_no, p.code, "buy", qty, price, now)

    def _close(self, p: Position, reason: str, now: datetime) -> None:
        if p.close_reason is None:
            p.close_reason = reason
            log.info("[%s] 전량 매도 결정: %s", self._label(p.code), reason)
        self._flush(p, now)

    def _flush(self, p: Position, now: datetime) -> None:
        if p.close_reason and p.bought and now >= p.retry_at:
            q = p.qty - p.pending_sell
            if q > 0:
                self._sell(p, q, p.close_reason, now)

    def _sell(self, p: Position, qty: int, reason: str, now: datetime) -> bool:
        price = self.price.get(p.code, 0)
        try:
            ord_no = self.broker.sell(p.code, qty, price)
        except Exception as e:
            log.error("[%s] 매도 실패 (%s초 후 재시도): %s", self._label(p.code), RETRY.seconds, e)
            p.retry_at = now + RETRY
            return False
        p.pending_sell += qty
        self.orders[ord_no] = Order(p.code, "sell", qty, reason, now)
        log.info("[%s] 매도 주문 %s주: %s", self._label(p.code), qty, reason)
        if self.broker.simulated:
            self.on_fill(ord_no, p.code, "sell", qty, price, now)
        return True

    def _label(self, code: str) -> str:
        return f"{self.names.get(code, '')}({code})"

    def _record(self, now: datetime, code: str, side: str, qty: int, price: int, reason: str,
                pnl: float | None = None) -> None:
        log.info("[%s] %s 체결 %s주 @ %s%s", self._label(code), side, qty, f"{price:,}",
                 f" 손익 {pnl:+,.0f}원" if pnl is not None else "")
        if self.trades_path is None:
            return
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["시각", "종목코드", "종목명", "구분", "수량", "가격", "사유", "손익(수수료 제외)"])
            w.writerow([f"{now:%H:%M:%S}", code, self.names.get(code, ""), side, qty, price, reason,
                        "" if pnl is None else round(pnl)])
