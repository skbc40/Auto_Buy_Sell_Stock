"""키움 REST 호출(순위·예수금·잔고·주문)과 가상 체결 브로커."""
import itertools
import threading
import time
from dataclasses import dataclass


def _clean(v) -> str:
    return str(v if v is not None else "").strip().replace(",", "")


def to_int(v) -> int:
    """키움 가격/수량 문자열 → 절댓값 정수. 부호(+/-)는 방향 표시일 뿐이다."""
    s = _clean(v)
    return abs(int(float(s))) if s not in ("", "-", "+") else 0


def to_float(v) -> float:
    s = _clean(v)
    return float(s) if s not in ("", "-", "+") else 0.0


def norm_code(v) -> str:
    """'A005930' / '005930_NX' / '005930_AL' → '005930'."""
    s = str(v if v is not None else "").strip()
    if len(s) == 7 and s[0] in "AQJ":
        s = s[1:]
    return s.split("_")[0]


@dataclass(frozen=True)
class RankRow:
    code: str
    name: str
    rank: int
    price: int
    change_pct: float


class RateLimiter:
    """키움 REST 호출 간 최소 간격(1/rate 초)을 보장한다."""

    def __init__(self, rate_per_sec: float) -> None:
        self.interval = 1.0 / rate_per_sec
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class KiwoomBroker:
    """모의투자 계좌에 실제 주문을 낸다. 체결은 웹소켓 00(주문체결)으로 들어온다."""

    simulated = False

    def __init__(self, client, rate_per_sec: float, stex_tp: str, exchange: str) -> None:
        self.client = client
        self.limiter = RateLimiter(rate_per_sec)
        self.stex_tp = stex_tp
        self.exchange = exchange

    def _call(self, api_id: str, path: str, body: dict) -> dict:
        self.limiter.acquire()
        return self.client.request(api_id=api_id, path=path, body=body).body or {}

    def rank(self) -> list[RankRow]:
        body = {"mrkt_tp": "000", "mang_stk_incls": "0", "stex_tp": self.stex_tp}
        rows = self._call("ka10032", "/api/dostk/rkinfo", body).get("trde_prica_upper") or []
        return [RankRow(norm_code(r.get("stk_cd")), str(r.get("stk_nm", "")).strip(),
                        to_int(r.get("now_rank")), to_int(r.get("cur_prc")), to_float(r.get("flu_rt")))
                for r in rows]

    def cash(self) -> int:
        return to_int(self._call("kt00001", "/api/dostk/acnt", {"qry_tp": "3"}).get("ord_alow_amt"))

    def holding_codes(self) -> set[str]:
        b = self._call("kt00018", "/api/dostk/acnt", {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        return {norm_code(r.get("stk_cd")) for r in b.get("acnt_evlt_remn_indv_tot") or []
                if to_int(r.get("rmnd_qty")) > 0}

    def _order(self, api_id: str, code: str, qty: int) -> str:
        if qty <= 0:
            raise ValueError(f"주문 수량은 1주 이상이어야 합니다: {qty}")
        body = {"dmst_stex_tp": self.exchange, "stk_cd": code, "ord_qty": str(qty), "ord_uv": "",
                "trde_tp": "6", "cond_uv": ""}  # 6: 최유리지정가 (상대 최우선호가로 지정가 주문)
        ord_no = _clean(self._call(api_id, "/api/dostk/ordr", body).get("ord_no"))
        if not ord_no:
            raise RuntimeError("주문번호 없음: 주문 결과를 확인할 수 없습니다")
        return ord_no.lstrip("0")

    def buy(self, code: str, qty: int, price: int) -> str:
        return self._order("kt10000", code, qty)

    def sell(self, code: str, qty: int, price: int) -> str:
        return self._order("kt10001", code, qty)

    def cancel(self, ord_no: str, code: str) -> None:
        """미체결 잔량 전부 취소 (cncl_qty 0 = 전량)."""
        body = {"dmst_stex_tp": self.exchange, "orig_ord_no": ord_no.zfill(7), "stk_cd": code, "cncl_qty": "0"}
        self._call("kt10003", "/api/dostk/ordr", body)


class DryBroker:
    """주문을 내지 않고 마지막 체결가로 즉시 가상 체결한다. 순위는 실제 API(있으면)에서 가져온다."""

    simulated = True

    def __init__(self, cash: int, rank_source: KiwoomBroker | None = None) -> None:
        self._cash = cash
        self.rank_source = rank_source
        self._ids = itertools.count(1)

    def rank(self) -> list[RankRow]:
        return self.rank_source.rank() if self.rank_source else []

    def cash(self) -> int:
        return self._cash

    def holding_codes(self) -> set[str]:
        return set()

    def buy(self, code: str, qty: int, price: int) -> str:
        if qty * price > self._cash:
            raise RuntimeError(f"가상 예수금 부족: 필요 {qty * price:,} / 보유 {self._cash:,}")
        self._cash -= qty * price
        return f"D{next(self._ids)}"

    def sell(self, code: str, qty: int, price: int) -> str:
        self._cash += qty * price
        return f"D{next(self._ids)}"
