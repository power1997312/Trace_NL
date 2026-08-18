"""pdfparser 图形界面：选择 PDF 文件/文件夹 → 选择结果路径 → 一键解析。

启动：
  cd /Users/plankg/PDF识别
  source .venv/bin/activate
  python tools/gui_app.py

功能：
- 添加 PDF 文件（多选）或包含 PDF 的文件夹（递归），列表可增删；
- 任意选择结果保存目录；
- 后台线程解析，进度条 + 实时日志，不卡界面；
- 完成后一键打开结果文件夹。
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from batch_parse import parse_one  # noqa: E402  （复用批量解析核心）


def collect_pdf_files(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """把用户选择展开为 [(绝对路径, 相对路径)]，文件夹递归收集，按绝对路径去重。"""
    out: dict[str, tuple[str, str]] = {}
    for path, rel in entries:
        if os.path.isfile(path):
            key = os.path.normpath(path)
            out.setdefault(key, (key, rel or os.path.basename(path)))
        elif os.path.isdir(path):
            for root, _, files in os.walk(path):
                for f in sorted(files):
                    if f.lower().endswith(".pdf"):
                        abs_path = os.path.normpath(os.path.join(root, f))
                        rel_path = os.path.relpath(abs_path, path)
                        out.setdefault(abs_path, (abs_path, os.path.join(rel, rel_path)))
    return sorted(out.values())


def unique_out_dir(out_root: str, rel_base: str) -> str:
    """输出子目录：<out_root>/<rel_base>/，已存在则自动加 _2、_3（不覆盖旧结果）。"""
    target = os.path.join(out_root, rel_base)
    n = 1
    while os.path.exists(target):
        n += 1
        target = os.path.join(out_root, f"{rel_base}_{n}")
    return target


class ParserApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("pdfparser · PDF 结构化提取")
        root.geometry("760x600")
        root.minsize(680, 520)

        self.entries: list[tuple[str, str]] = []   # [(abs_path, rel_label)]
        self.q: queue.Queue = queue.Queue()
        self.busy = False

        self._build_ui()
        self.after_poll()

    # ------------------------------------------------------------------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        pad = {"padx": 12, "pady": 6}

        # ===== 输入区 =====
        frm_in = ttk.LabelFrame(self.root, text="① 选择输入（可多选 PDF 文件 / 整个文件夹）")
        frm_in.pack(fill="x", **pad)
        row1 = ttk.Frame(frm_in)
        row1.pack(fill="x", padx=8, pady=8)
        ttk.Button(row1, text="添加 PDF 文件...", command=self.add_files).pack(side="left")
        ttk.Button(row1, text="添加文件夹...", command=self.add_folder).pack(side="left", padx=6)
        ttk.Button(row1, text="移除选中", command=self.remove_selected).pack(side="left")
        ttk.Button(row1, text="清空列表", command=self.clear_entries).pack(side="left", padx=6)
        self.lbl_count = ttk.Label(row1, text="已选 0 项")
        self.lbl_count.pack(side="right")

        self.listbox = tk.Listbox(frm_in, height=8, selectmode="extended",
                                  activestyle="dotbox")
        self.listbox.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # ===== 输出区 =====
        frm_out = ttk.LabelFrame(self.root, text="② 结果保存位置与格式")
        frm_out.pack(fill="x", **pad)
        row2 = ttk.Frame(frm_out)
        row2.pack(fill="x", padx=8, pady=8)
        self.out_var = tk.StringVar(value=os.path.join(ROOT, "output"))
        ttk.Entry(row2, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="选择目录...", command=self.choose_out).pack(side="left", padx=6)
        row3 = ttk.Frame(frm_out)
        row3.pack(fill="x", padx=8, pady=(0, 8))
        self.fmt_var = tk.StringVar(value="both")
        for text, val in (("Markdown + JSON（推荐）", "both"),
                          ("仅 Markdown", "md"),
                          ("仅 JSON", "json")):
            ttk.Radiobutton(row3, text=text, value=val,
                            variable=self.fmt_var).pack(side="left", padx=(0, 12))

        # ===== 处理区 =====
        frm_run = ttk.LabelFrame(self.root, text="③ 开始处理")
        frm_run.pack(fill="both", expand=True, **pad)
        self.btn_run = ttk.Button(frm_run, text="开始解析", command=self.start_parse)
        self.btn_run.pack(padx=8, pady=8)
        self.progress = ttk.Progressbar(frm_run, mode="determinate")
        self.progress.pack(fill="x", padx=8)
        self.lbl_status = ttk.Label(frm_run, text="就绪", anchor="w")
        self.lbl_status.pack(fill="x", padx=8, pady=(4, 0))
        self.log = scrolledtext.ScrolledText(frm_run, height=12, state="disabled",
                                             font=("Menlo", 10))
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        self.btn_open = ttk.Button(frm_run, text="打开结果文件夹", command=self.open_out,
                                   state="disabled")
        self.btn_open.pack(padx=8, pady=(0, 8))

    # ------------------------------------------------------------------
    # 输入操作
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择 PDF 文件（可多选）",
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*")])
        added = 0
        for p in paths:
            if p not in {e[0] for e in self.entries}:
                self.entries.append((p, os.path.basename(p)))
                added += 1
        self.refresh_list()

    def add_folder(self):
        d = filedialog.askdirectory(title="选择包含 PDF 的文件夹")
        if d:
            self.entries.append((d, os.path.basename(d) or d))
            self.refresh_list()

    def remove_selected(self):
        sel = self.listbox.curselection()
        for i in reversed(sel):
            self.entries.pop(i)
        self.refresh_list()

    def clear_entries(self):
        self.entries.clear()
        self.refresh_list()

    def refresh_list(self):
        self.listbox.delete(0, "end")
        for path, rel in self.entries:
            kind = "文件夹" if os.path.isdir(path) else "PDF"
            self.listbox.insert("end", f"[{kind}] {path}")
        self.lbl_count.config(text=f"已选 {len(self.entries)} 项")

    def choose_out(self):
        d = filedialog.askdirectory(title="选择结果保存目录")
        if d:
            self.out_var.set(d)

    # ------------------------------------------------------------------
    # 处理
    def start_parse(self):
        if self.busy:
            return
        pdfs = collect_pdf_files(self.entries)
        if not pdfs:
            messagebox.showwarning("提示", "请先添加 PDF 文件或包含 PDF 的文件夹")
            return
        out_root = self.out_var.get().strip()
        if not out_root:
            messagebox.showwarning("提示", "请选择结果保存目录")
            return
        os.makedirs(out_root, exist_ok=True)
        self.busy = True
        self.btn_run.config(state="disabled")
        self.btn_open.config(state="disabled")
        self.progress.config(maximum=len(pdfs), value=0)
        self.log_clear()
        self.log_append(f"共 {len(pdfs)} 份 PDF，输出到：{out_root}\n")
        threading.Thread(target=self._worker, args=(pdfs, out_root),
                         daemon=True).start()

    def _worker(self, pdfs, out_root):
        t_all = time.time()
        ok = scanned = failed = 0
        for i, (abs_path, rel) in enumerate(pdfs, 1):
            base = os.path.splitext(os.path.basename(abs_path))[0]
            rel_dir = os.path.dirname(rel)
            rel_out = os.path.join(rel_dir, base) if rel_dir else base
            # 对最终输出目录去重（parse_one 直接写入该目录）
            final = unique_out_dir(out_root, rel_out)
            try:
                r = parse_one(abs_path, final, self.fmt_var.get(),
                              prefer_camelot=True)
            except Exception as e:  # noqa: BLE001
                failed += 1
                self.q.put(("log", f"[{i}/{len(pdfs)}] ✗ {rel}\n    错误：{e}\n"))
                self.q.put(("progress", i))
                continue
            if not r.get("ok"):
                failed += 1
                self.q.put(("log", f"[{i}/{len(pdfs)}] ✗ {rel}\n    错误：{r.get('error')}\n"))
            elif r.get("scanned"):
                scanned += 1
                self.q.put(("log", f"[{i}/{len(pdfs)}] ⚠ {rel} —— 扫描件，无文本层，需 OCR\n"))
            else:
                ok += 1
                self.q.put(("log",
                            f"[{i}/{len(pdfs)}] ✓ {rel}\n"
                            f"    页 {r['pages']} · 标题 {r['n_headings']} · "
                            f"表格 {r['n_tables']} · 图片 {r['n_assets']} · "
                            f"耗时 {r['elapsed']}s\n"))
            self.q.put(("progress", i))
        self.q.put(("done", ok, scanned, failed, time.time() - t_all))

    # ------------------------------------------------------------------
    # UI 轮询
    def after_poll(self):
        self.root.after(100, self.poll)

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log_append(item[1])
                elif kind == "progress":
                    self.progress.config(value=item[1])
                    self.lbl_status.config(
                        text=f"处理中 {item[1]}/{self.progress['maximum']}")
                elif kind == "done":
                    _, ok, scanned, failed, elapsed = item
                    self.busy = False
                    self.btn_run.config(state="normal")
                    self.btn_open.config(state="normal")
                    self.lbl_status.config(
                        text=f"完成：成功 {ok} · 扫描件 {scanned} · 失败 {failed} · 共 {elapsed:.1f}s")
                    self.log_append(
                        f"\n====== 完成：成功 {ok} | 扫描件 {scanned} | "
                        f"失败 {failed} | 总耗时 {elapsed:.1f}s ======\n")
        except queue.Empty:
            pass
        if self.busy or not self.q.empty():
            self.after_poll()
        else:
            self.after_poll()

    # ------------------------------------------------------------------
    # 辅助
    def log_append(self, text: str):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def log_clear(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def open_out(self):
        d = self.out_var.get().strip()
        if not os.path.isdir(d):
            messagebox.showinfo("提示", f"结果目录不存在：{d}")
            return
        if sys.platform == "darwin":
            os.system(f'open "{d}"')
        elif sys.platform.startswith("win"):
            os.startfile(d)  # noqa: S606
        else:
            os.system(f'xdg-open "{d}"')


def main():
    root = tk.Tk()
    ParserApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
