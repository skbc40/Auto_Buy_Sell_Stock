"""기준봉 단타 자동매매 GUI: uv run gui.py (또는 실행.bat 더블클릭)"""
import asyncio
import logging
import queue
import re
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk

import yaml

import main
from trader import hm

CONFIG = Path(__file__).with_name("config.yaml")
MODE_NAMES = {"demo": "모의투자", "real": "실제투자"}
MODE_KEYS = {v: k for k, v in MODE_NAMES.items()}

# (섹션, 키, 화면 이름, 형식) — 섹션 None 은 최상위 키
FIELDS = [
    (None, "mode", "투자 모드", "mode"),
    ("session", "start", "기준봉 탐색 시작", "time"),
    ("session", "entry_end", "신규 진입 마감", "time"),
    ("session", "force_close", "강제 청산", "time"),
    ("universe", "rank_top", "거래대금 순위 이내", int),
    ("universe", "min_change_pct", "등락률 하한 (%)", float),
    ("signal", "volume_ratio", "거래량 배수 (직전봉 대비)", float),
    ("signal", "close_up_pct", "종가 상승 (%)", float),
    ("signal", "buy_delay_min", "매수 시점 (기준봉 후 분)", int),
    ("signal", "judge_after_min", "판정 시점 (분)", int),
    ("signal", "judge_up_pct", "판정 상승 기준 (%)", float),
    ("signal", "exit_after_min", "시간 청산 (분)", int),
    ("exit", "stop_loss_pct", "손절 (%)", float),
    ("exit", "partial_take_pct", "부분익절 (%)", float),
    ("exit", "partial_ratio", "부분익절 비율 (0~1)", float),
    (None, "max_positions", "최대 보유 종목", int),
    ("sizing", "mode", "매수 금액 방식", ("percent", "fixed")),
    ("sizing", "percent", "매수가능금액의 (%)", float),
    ("sizing", "fixed_krw", "고정 금액 (원)", int),
    ("order", "timeout_sec", "미체결 취소 (초)", int),
]


def set_yaml_value(text: str, section: str | None, key: str, value: str) -> str:
    """주석을 지우지 않고 config.yaml 의 값 하나만 바꾼다."""
    lines = text.splitlines(keepends=True)
    in_sec = section is None
    pat = re.compile(rf"^({'  ' if section else ''}{key}:\s*)(\S.*?)(\s+#.*)?$")
    for i, line in enumerate(lines):
        if section and re.match(rf"^{section}:", line):
            in_sec = True
            continue
        if section and in_sec and re.match(r"^\S", line):
            in_sec = False
        m = pat.match(line.rstrip("\r\n"))
        if in_sec and m:
            if yaml.safe_load(m.group(2)) != yaml.safe_load(value):  # 같은 값이면 원래 표기(6.0 등) 유지
                lines[i] = m.group(1) + value + (m.group(3) or "") + line[len(line.rstrip("\r\n")):]
            return "".join(lines)
    raise KeyError(f"{section}.{key}")


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record) -> None:
        self.q.put(self.format(record))


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("기준봉 단타 자동매매 (키움 모의투자)")
        root.geometry("1280x800")
        self.halt = threading.Event()
        self.state: dict = {}
        self.thread: threading.Thread | None = None
        self.logq: queue.Queue = queue.Queue()
        logging.getLogger().addHandler(QueueHandler(self.logq))
        logging.getLogger().setLevel(logging.INFO)

        top = ttk.Frame(root, padding=6)
        top.pack(fill="x")
        self.btn_start = ttk.Button(top, text="▶ 시작", command=self.start)
        self.btn_stop = ttk.Button(top, text="■ 중지", command=self.stop, state="disabled")
        self.btn_save = ttk.Button(top, text="설정 저장", command=self.save)
        for b in (self.btn_start, self.btn_stop, self.btn_save):
            b.pack(side="left", padx=3)
        self.lbl_status = ttk.Label(top, text="중지됨", font=("맑은 고딕", 11, "bold"))
        self.lbl_status.pack(side="left", padx=15)
        self.lbl_info = ttk.Label(top, text="")
        self.lbl_info.pack(side="left")

        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=6)
        body.add(self._settings_panel(body), weight=0)
        right = ttk.Panedwindow(body, orient="vertical")
        body.add(right, weight=1)

        self.tv_pos = self._table(right, "포지션",
                                  ("종목", "상태", "수량", "평균가", "현재가", "수익률", "기준봉 종가", "판정"), 5)
        self.tv_watch = self._table(right, "조건 충족 종목 (거래대금 순위·등락률)",
                                    ("순위", "종목", "등락률", "현재가", "진입여부"), 6)
        self.tv_trades = self._table(right, "체결 내역",
                                     ("시각", "종목", "구분", "수량", "가격", "사유", "손익"), 6)
        logf = ttk.LabelFrame(right, text="로그")
        self.txt_log = scrolledtext.ScrolledText(logf, height=10, font=("Consolas", 9), state="disabled")
        self.txt_log.pack(fill="both", expand=True)
        right.add(logf, weight=2)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.load()
        self.tick()

    # ---- 화면 구성 ----
    def _settings_panel(self, parent) -> ttk.LabelFrame:
        f = ttk.LabelFrame(parent, text="설정 (config.yaml)", padding=6)
        self.vars = {}
        for i, (sec, key, label, kind) in enumerate(FIELDS):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=2)
            if kind == "mode" or isinstance(kind, tuple):
                v = tk.StringVar()
                values = list(MODE_KEYS) if kind == "mode" else kind
                ttk.Combobox(f, textvariable=v, values=values, state="readonly", width=12).grid(row=i, column=1, sticky="w")
            else:
                v = tk.StringVar()
                ttk.Entry(f, textvariable=v, width=14).grid(row=i, column=1, sticky="w")
            self.vars[(sec, key)] = v
        ttk.Label(f, text="※ 저장한 설정은 다음 시작부터 적용", foreground="gray").grid(
            row=len(FIELDS), column=0, columnspan=2, sticky="w", pady=(8, 0))
        return f

    def _table(self, parent, title: str, cols: tuple, height: int) -> ttk.Treeview:
        f = ttk.LabelFrame(parent, text=title)
        tv = ttk.Treeview(f, columns=cols, show="headings", height=height)
        for c in cols:
            tv.heading(c, text=c)
            tv.column(c, width=90 if c not in ("종목", "사유") else 170, anchor="center")
        tv.pack(fill="both", expand=True)
        parent.add(f, weight=1)
        return tv

    # ---- 설정 ----
    def load(self) -> None:
        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        for (sec, key), v in self.vars.items():
            val = (cfg[sec] if sec else cfg)[key]
            v.set(MODE_NAMES[val] if (sec, key) == (None, "mode") else val)

    def save(self) -> bool:
        text = CONFIG.read_bytes().decode("utf-8")  # 줄바꿈(CRLF/LF) 그대로 유지
        try:
            for sec, key, label, kind in FIELDS:
                raw = self.vars[(sec, key)].get()
                if kind == "mode":
                    val = MODE_KEYS[raw]
                elif kind == "time":
                    hm(str(raw).strip())
                    val = f'"{str(raw).strip()}"'
                elif isinstance(kind, tuple):
                    val = raw
                else:
                    num = kind(str(raw).strip().replace(",", ""))
                    val = str(int(num)) if float(num).is_integer() else str(num)  # 50.0 → 50
                text = set_yaml_value(text, sec, key, val)
        except (ValueError, KeyError) as e:
            messagebox.showerror("설정 오류", f"'{label}' 값이 올바르지 않습니다: {raw}\n({e})")
            return False
        CONFIG.write_bytes(text.encode("utf-8"))
        logging.getLogger("gui").info("설정 저장됨")
        return True

    # ---- 실행 ----
    def start(self) -> None:
        if not self.save():
            return
        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        if cfg["mode"] == "real":
            answer = simpledialog.askstring(
                "실제투자 확인", "실제투자 모드입니다. 실제 돈으로 주문합니다.\n진행하려면 '실제투자' 라고 입력하세요.", parent=self.root)
            if (answer or "").strip() != "실제투자":
                messagebox.showinfo("취소", "시작하지 않았습니다.")
                return
        self.halt.clear()
        self.state.clear()
        self.thread = threading.Thread(target=self._worker, args=(cfg,), daemon=True)
        self.thread.start()
        self.btn_start["state"], self.btn_stop["state"] = "disabled", "normal"

    def _worker(self, cfg: dict) -> None:
        try:
            if main.wait_for_session(cfg, self.halt):
                asyncio.run(main.run(cfg, self.halt, self.state))
        except BaseException as e:  # sys.exit 포함
            logging.getLogger("gui").error("실행 중 오류: %s", e)
        logging.getLogger("gui").info("자동매매 중지됨")

    def stop(self) -> None:
        tr = self.state.get("trader")
        if tr and tr.pos and not messagebox.askyesno(
                "중지 확인", "보유 중이거나 매수 대기 중인 종목이 있습니다.\n중지하면 더 이상 관리(손절·청산)하지 않습니다. 중지할까요?"):
            return
        self.halt.set()
        self.btn_stop["state"] = "disabled"

    def on_close(self) -> None:
        if self.thread and self.thread.is_alive():
            self.stop()
            if not self.halt.is_set():
                return
            self.thread.join(timeout=5)
        self.root.destroy()

    # ---- 1초마다 화면 갱신 ----
    def tick(self) -> None:
        while not self.logq.empty():
            self.txt_log["state"] = "normal"
            self.txt_log.insert("end", self.logq.get() + "\n")
            self.txt_log.see("end")
            self.txt_log["state"] = "disabled"

        running = self.thread is not None and self.thread.is_alive()
        if not running and self.btn_start["state"] == "disabled":
            self.btn_start["state"], self.btn_stop["state"] = "normal", "disabled"
        tr = self.state.get("trader")
        if not running:
            self.lbl_status["text"] = "중지됨"
        elif tr is None:
            self.lbl_status["text"] = "장 시작 대기 중"
        else:
            self.lbl_status["text"] = "실행 중 · 연결됨" if self.state.get("connected") else "실행 중 · 연결 중…"
        if tr:
            mode = self.state.get("mode", "demo")
            self.lbl_status["foreground"] = "red" if mode == "real" else "black"
            self.lbl_info["text"] = (f"{MODE_NAMES[mode]} | 시작 시 주문가능금액 {self.state.get('cash', 0):,}원 | "
                                     f"구독 {self.state.get('subscribed', 0)}종목 | {datetime.now():%H:%M:%S}")
            self._refresh(tr)
        self.root.after(1000, self.tick)

    def _refresh(self, tr) -> None:
        def fill(tv, rows):
            tv.delete(*tv.get_children())
            for r in rows:
                tv.insert("", "end", values=r)

        pos = []
        for p in list(tr.pos.values()):
            price = tr.price.get(p.code, 0)
            status = "매수 대기" if not p.bought else "매도 중" if p.close_reason else "보유"
            rate = f"{(price / p.avg - 1) * 100:+.2f}%" if p.avg else ""
            pos.append((tr._label(p.code), status, p.qty, f"{p.avg:,.0f}", f"{price:,}", rate,
                        f"{p.base_close:,}", "통과" if p.judged and not p.close_reason else ("완료" if p.judged else "대기")))
        fill(self.tv_pos, pos)
        watch = sorted((r for r in list(tr.ranks.values()) if tr.watching(r.code)), key=lambda r: r.rank)
        fill(self.tv_watch, [(r.rank, tr._label(r.code), f"{tr.change.get(r.code, 0):+.2f}%",
                              f"{tr.price.get(r.code, 0):,}", "진입함" if r.code in tr.entered else "")
                             for r in watch])
        fill(self.tv_trades, [(t[0], f"{t[2]}({t[1]})", t[3], t[4], f"{t[5]:,}", t[6],
                               "" if t[7] == "" else f"{t[7]:+,}") for t in reversed(tr.trades)])


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
