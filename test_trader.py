"""가상 틱으로 매매 규칙을 검증한다: uv run pytest -q"""
import copy
from datetime import datetime

import yaml

from broker import DryBroker, RankRow
from trader import Trader

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)
D = datetime(2026, 10, 7)
C = "000001"


def at(h, m, s=0):
    return D.replace(hour=h, minute=m, second=s)


def make(codes=(C,), rank=1, change=8.0, sub=(9, 0), cfg=CFG, broker=None):
    tr = Trader(cfg, broker or DryBroker(10_000_000))
    tr.update_rank([RankRow(c, f"종목{i}", rank + i, 10000, change) for i, c in enumerate(codes)])
    tr.mark_subscribed(codes, at(*sub))
    return tr


def tick(tr, code, h, m, s, price, qty, change=8.0):
    tr.on_tick(code, at(h, m, s), price, qty, change, at(h, m, s))


def signal(tr, code=C, change=8.0):
    """09:01봉 10,000원·100주 → 09:02봉 10,200원(+2%)·300주(3배) = 기준봉, 09:03 완성."""
    tick(tr, code, 9, 1, 0, 10000, 100, change)
    tick(tr, code, 9, 2, 0, 10200, 300, change)
    tr.on_clock(at(9, 3, 1))


def bought(tr, code=C, price=10250):
    tick(tr, code, 9, 4, 0, price, 10)
    tr.on_clock(at(9, 4, 59))
    assert not tr.pos[code].bought, "2분 전에는 사지 않는다"
    tr.on_clock(at(9, 5, 0))
    return tr.pos[code]


def test_judge_fail_sells_all_at_5min():
    tr = make()
    signal(tr)
    p = bought(tr)
    assert p.qty == 5_000_000 // 10250  # 예수금 1천만 × 50%
    tick(tr, C, 9, 7, 0, 10300, 10)     # 09:07봉 종가 10,300 < 기준봉 10,200 × 1.03 = 10,506
    tr.on_clock(at(9, 8, 0))
    assert C in tr.pos, "판정은 기준봉 완성 5분 후"
    tr.on_clock(at(9, 8, 1))
    assert C not in tr.pos
    assert tr.broker.cash() == 10_000_000 + 487 * (10300 - 10250)


def test_partial_take_then_15min_exit():
    tr = make()
    signal(tr)
    p = bought(tr)
    tick(tr, C, 9, 6, 10, 10800, 10)    # +5.4% → 절반 익절
    assert p.qty == 487 - 243 and p.partial_done
    tick(tr, C, 9, 7, 30, 10600, 10)    # 09:07봉 종가 10,600 ≥ 10,506 → 판정 통과
    tr.on_clock(at(9, 8, 1))
    assert tr.pos[C].qty == 244
    tick(tr, C, 9, 10, 0, 10900, 10)    # 다시 +5% 넘어도 부분익절은 1회
    assert tr.pos[C].qty == 244
    tr.on_clock(at(9, 17, 59))
    assert C in tr.pos
    tr.on_clock(at(9, 18, 0))           # 기준봉 완성 15분 후
    assert C not in tr.pos


def test_stop_loss_on_bar_close():
    tr = make()
    signal(tr)
    bought(tr)
    tick(tr, C, 9, 6, 0, 9950, 10)      # 9,950 > 10,250 × 0.97 = 9,942.5 → 유지
    tick(tr, C, 9, 7, 0, 9900, 10)      # 09:06봉 완성(종가 9,950)
    assert C in tr.pos
    tr.on_clock(at(9, 8, 1))            # 09:07봉 종가 9,900 → 손절
    assert C not in tr.pos


def test_filters_block_signal():
    tr = make(rank=31)                  # 30위 밖
    signal(tr)
    assert not tr.pos
    tr = make()
    signal(tr, change=5.0)              # +6% 미만
    assert not tr.pos
    tr = make(sub=(9, 1, 30))           # 09:01봉을 처음부터 보지 못함
    signal(tr)
    assert not tr.pos


def test_first_bar_of_session_is_not_signal():
    tr = make(sub=(8, 58))
    tick(tr, C, 8, 59, 0, 10000, 100)
    tick(tr, C, 9, 0, 0, 10300, 500)    # 09:00봉은 조건을 만족해도 제외
    tick(tr, C, 9, 1, 0, 10300, 100)
    assert not tr.pos
    tick(tr, C, 9, 2, 0, 10500, 300)    # 09:01봉을 직전봉으로 쓴 09:02봉은 기준봉
    tr.on_clock(at(9, 3, 1))
    assert C in tr.pos


def test_no_reentry_and_max_two_positions():
    codes = ("000001", "000002", "000003")
    tr = make(codes=codes)
    for c in codes:
        tick(tr, c, 9, 1, 0, 10000, 100)
        tick(tr, c, 9, 2, 0, 10200, 300)
    tr.on_clock(at(9, 3, 1))
    assert set(tr.pos) == {"000001", "000002"}
    assert "000003" not in tr.entered, "한도 때문에 못 산 종목은 나중에 다시 기회가 있다"
    for c in codes:
        tick(tr, c, 9, 4, 0, 10250, 10)
    tr.on_clock(at(9, 5, 0))
    assert tr.pos["000001"].qty == tr.pos["000002"].qty == 487, "두 종목 같은 금액"
    for c in codes[:2]:
        tick(tr, c, 9, 7, 0, 10250, 10)
    tr.on_clock(at(9, 8, 1))            # 둘 다 판정 미달로 청산
    assert not tr.pos
    tick(tr, C, 9, 20, 0, 10000, 100)
    tick(tr, C, 9, 21, 0, 10500, 500)
    tr.on_clock(at(9, 22, 1))
    assert not tr.pos, "한 번 진입한 종목은 재진입하지 않는다"


def test_fixed_sizing():
    cfg = copy.deepcopy(CFG)
    cfg["sizing"]["mode"] = "fixed"
    tr = make(cfg=cfg)
    signal(tr)
    assert bought(tr).qty == 1_000_000 // 10250


class FakeRealBroker(DryBroker):
    """주문만 기록하고 체결은 테스트가 직접 넣는다 (실제 모의계좌 흐름)."""
    simulated = False

    def __init__(self):
        super().__init__(10_000_000)
        self.sent = []

    def buy(self, code, qty, price):
        self.sent.append(("buy", qty))
        return f"B{len(self.sent)}"

    def sell(self, code, qty, price):
        self.sent.append(("sell", qty))
        return f"S{len(self.sent)}"

    def cancel(self, ord_no, code):
        self.sent.append(("cancel", ord_no))


def test_unfilled_remainder_is_cancelled_and_sell_reissued():
    br = FakeRealBroker()
    tr = make(broker=br)
    signal(tr)
    p = bought(tr)                      # 09:05:00 매수 487주 주문
    tr.on_fill("B1", C, "buy", 300, 10250, at(9, 5, 1))   # 300주만 체결
    tr.on_clock(at(9, 5, 9))
    assert br.sent[-1] == ("buy", 487)
    tr.on_clock(at(9, 5, 10))           # 10초 경과 → 잔량 취소
    assert br.sent[-1] == ("cancel", "B1")
    tr.on_cancelled("B1", at(9, 5, 11))
    assert p.qty == 300 and "B1" not in tr.orders
    tick(tr, C, 9, 7, 0, 9900, 10)
    tr.on_clock(at(9, 8, 1))            # 손절 → 300주 매도
    assert br.sent[-1] == ("sell", 300)
    tr.on_fill("S3", C, "sell", 100, 9890, at(9, 8, 2))   # 100주만 체결
    tr.on_clock(at(9, 8, 11))
    assert br.sent[-1] == ("cancel", "S3")
    tr.on_cancelled("S3", at(9, 8, 12))
    tr.on_clock(at(9, 8, 12))           # 남은 200주 즉시 다시 매도
    assert br.sent[-1] == ("sell", 200)
    tr.on_fill("S5", C, "sell", 200, 9880, at(9, 8, 13))
    assert C not in tr.pos


def test_real_flow_fill_and_rejected_sell_retries():
    br = FakeRealBroker()
    tr = make(broker=br)
    signal(tr)
    p = bought(tr)
    assert p.qty == 0 and br.sent == [("buy", 487)]
    tr.on_fill("B1", C, "buy", 487, 10260, at(9, 5, 1))
    assert p.qty == 487 and p.avg == 10260
    tick(tr, C, 9, 7, 0, 9900, 10)
    tr.on_clock(at(9, 8, 1))            # 손절 결정 → 매도 주문
    assert br.sent[-1] == ("sell", 487) and p.pending_sell == 487
    tr.on_reject("S2", at(9, 8, 2))     # 주문 거부 → 5초 후 재시도
    tr.on_clock(at(9, 8, 3))
    assert len(br.sent) == 2
    tr.on_clock(at(9, 8, 7))
    assert br.sent[-1] == ("sell", 487)
    tr.on_fill("S3", C, "sell", 487, 9890, at(9, 8, 8))
    assert C not in tr.pos


def test_force_close():
    tr = make()
    signal(tr)
    bought(tr)
    tick(tr, C, 9, 7, 30, 10600, 10)    # 판정 통과
    tr.on_clock(at(9, 8, 1))
    tr.force_close = at(9, 10).time()   # 장마감 시각을 앞당겨 확인
    tr.on_clock(at(9, 10, 0))
    assert C not in tr.pos
