# -*- coding: utf-8 -*-
"""main_gui.py — 文献PDF清洗工具 · 图形界面（全程鼠标操作）。

四个标签页：
  ① 导入与清洗   选文件夹 → 勾选清洗规则 → 开始批量清洗
  ② 看结果       结构树（题目/摘要/关键词/标题/表图/参考文献）+ 原文对照
  ③ 标题树汇总   所有文献的标题树（构建综述大纲的参照）
  ④ 质检与导出   质量报告、问题清单、导出/打开结果文件夹

设计原则：原始 PDF 只读；原始文本与清洗文本分开保存；逐步查看、可重新处理。
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

sys.path.insert(0, str(Path(__file__).parent))
from core.clean import CleanConfig, RULE_DESC
from core.pipeline import process_pdf, find_pdfs
from core.extract import engine_status, HAS_FITZ, HAS_PDFMINER
from core.export import export_document, export_summary, headings_markdown, document_markdown
from core.quality import report_markdown

APP_TITLE = "文献PDF清洗工具 v1.0"


def open_folder(path: Path):
    """跨平台打开文件夹。"""
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1100x740")
        self.minsize(900, 620)

        self.pdf_dir = tk.StringVar()
        self.out_dir = tk.StringVar(value=str(Path(__file__).parent / "清洗结果"))
        self.results: dict[str, dict] = {}     # 文献名 → {"result":…, "quality":…}
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()

        # 清洗规则开关
        self.rule_vars: dict[str, tk.BooleanVar] = {}
        for key in RULE_DESC:
            self.rule_vars[key] = tk.BooleanVar(value=True)

        self._build_ui()
        self.after(200, self._poll_queue)
        self._update_engine_label()
        self._load_previous()

    # ------------------------------------------------------------- 界面搭建
    def _build_ui(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="PDF 文件夹：").pack(side="left")
        ttk.Entry(top, textvariable=self.pdf_dir, width=52).pack(side="left", padx=4)
        ttk.Button(top, text="选择…", command=self._pick_dir).pack(side="left")
        ttk.Button(top, text="刷新列表", command=self._refresh_list).pack(side="left", padx=4)

        self.engine_label = ttk.Label(top, text="", foreground="#666")
        self.engine_label.pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=4)
        self.nb = nb
        self.tab1 = ttk.Frame(nb, padding=8)
        self.tab2 = ttk.Frame(nb, padding=8)
        self.tab3 = ttk.Frame(nb, padding=8)
        self.tab4 = ttk.Frame(nb, padding=8)
        nb.add(self.tab1, text=" ① 导入与清洗 ")
        nb.add(self.tab2, text=" ② 看结果 ")
        nb.add(self.tab3, text=" ③ 标题树汇总 ")
        nb.add(self.tab4, text=" ④ 质检与导出 ")

        self._build_tab1()
        self._build_tab2()
        self._build_tab3()
        self._build_tab4()

        # 底部状态栏
        bottom = ttk.Frame(self, padding=(10, 4))
        bottom.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(bottom, length=320, mode="determinate")
        self.progress.pack(side="left")
        self.status = ttk.Label(bottom, text="就绪")
        self.status.pack(side="left", padx=10)

    def _build_tab1(self):
        # 左：文件列表
        left = ttk.Frame(self.tab1)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="待处理 PDF（自动去重，原文件只读）：").pack(anchor="w")
        cols = ("name", "size", "status")
        self.file_tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        self.file_tree.heading("name", text="文件名")
        self.file_tree.heading("size", text="大小")
        self.file_tree.heading("status", text="状态")
        self.file_tree.column("name", width=380)
        self.file_tree.column("size", width=80, anchor="center")
        self.file_tree.column("status", width=120, anchor="center")
        self.file_tree.pack(fill="both", expand=True, pady=4)

        btns = ttk.Frame(left)
        btns.pack(fill="x")
        self.start_btn = ttk.Button(btns, text="▶ 开始清洗", command=self._start_batch)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="停止", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="全部重新处理", command=lambda: self._start_batch(force=True)
                   ).pack(side="left")

        # 右：规则开关
        right = ttk.Frame(self.tab1, width=260)
        right.pack(side="right", fill="y", padx=(10, 0))
        ttk.Label(right, text="清洗规则（可开关）：", font=("", 10, "bold")).pack(anchor="w")
        for key, desc in RULE_DESC.items():
            ttk.Checkbutton(right, text=desc, variable=self.rule_vars[key]).pack(anchor="w", pady=1)
        ttk.Separator(right).pack(fill="x", pady=6)
        ttk.Label(right, text="输出文件夹：", font=("", 10, "bold")).pack(anchor="w")
        of = ttk.Frame(right)
        of.pack(fill="x")
        ttk.Entry(of, textvariable=self.out_dir, width=26).pack(side="left")
        ttk.Button(of, text="…", width=2, command=self._pick_out).pack(side="left")
        ttk.Button(right, text="📁 打开结果文件夹",
                   command=lambda: open_folder(Path(self.out_dir.get()))).pack(anchor="w", pady=6)
        ttk.Separator(right).pack(fill="x", pady=6)
        self.log = tk.Text(right, height=12, width=30, state="disabled",
                           background="#f6f6f6", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    def _build_tab2(self):
        paned = ttk.Panedwindow(self.tab2, orient="horizontal")
        paned.pack(fill="both", expand=True)

        lframe = ttk.Frame(paned)
        paned.add(lframe, weight=1)
        ttk.Label(lframe, text="文献：").pack(anchor="w")
        self.doc_list = tk.Listbox(lframe, exportselection=False, width=34)
        self.doc_list.pack(fill="both", expand=True, pady=2)
        self.doc_list.bind("<<ListboxSelect>>", lambda e: self._show_doc())

        mframe = ttk.Frame(paned)
        paned.add(mframe, weight=2)
        ttk.Label(mframe, text="结构（题目/摘要/关键词/标题/表图/参考文献）：").pack(anchor="w")
        self.struct_tree = ttk.Treeview(mframe, columns=("info",), show="tree headings")
        self.struct_tree.heading("#0", text="结构")
        self.struct_tree.heading("info", text="信息")
        self.struct_tree.column("#0", width=320)
        self.struct_tree.column("info", width=260)
        self.struct_tree.pack(fill="both", expand=True, pady=2)

        rframe = ttk.Frame(paned)
        paned.add(rframe, weight=2)
        bar = ttk.Frame(rframe)
        bar.pack(fill="x")
        ttk.Label(bar, text="文本对照：").pack(side="left")
        self.view_mode = tk.StringVar(value="clean")
        for val, txt in [("clean", "清洗文本(.md)"), ("raw", "原始提取"), ("abstract", "摘要")]:
            ttk.Radiobutton(bar, text=txt, value=val, variable=self.view_mode,
                            command=self._show_doc).pack(side="left", padx=4)
        self.text_view = tk.Text(rframe, wrap="word", font=("Consolas", 10))
        self.text_view.pack(fill="both", expand=True, pady=2)

    def _build_tab3(self):
        bar = ttk.Frame(self.tab3)
        bar.pack(fill="x")
        ttk.Label(bar, text="所有文献的标题树（⚠️ = 层级待人工确认）。"
                            "可整体导出，作为你综述大纲的参照。").pack(side="left")
        ttk.Button(bar, text="导出标题树汇总…", command=self._export_headings).pack(side="right")
        self.head_tree = ttk.Treeview(self.tab3, columns=("page", "status"),
                                      show="tree headings")
        self.head_tree.heading("#0", text="标题层级")
        self.head_tree.heading("page", text="页码")
        self.head_tree.heading("status", text="状态")
        self.head_tree.column("page", width=70, anchor="center")
        self.head_tree.column("status", width=110, anchor="center")
        self.head_tree.pack(fill="both", expand=True, pady=4)

    def _build_tab4(self):
        paned = ttk.Panedwindow(self.tab4, orient="horizontal")
        paned.pack(fill="both", expand=True)
        lframe = ttk.Frame(paned)
        paned.add(lframe, weight=1)
        ttk.Label(lframe, text="文献与质检状态：").pack(anchor="w")
        self.q_list = tk.Listbox(lframe, exportselection=False, width=34)
        self.q_list.pack(fill="both", expand=True, pady=2)
        self.q_list.bind("<<ListboxSelect>>", lambda e: self._show_quality())

        rframe = ttk.Frame(paned)
        paned.add(rframe, weight=3)
        bar = ttk.Frame(rframe)
        bar.pack(fill="x")
        ttk.Button(bar, text="导出全部结果（JSON/TXT/Markdown）", command=self._export_all
                   ).pack(side="left")
        ttk.Button(bar, text="📁 打开结果文件夹",
                   command=lambda: open_folder(Path(self.out_dir.get()))).pack(side="left", padx=6)
        self.q_text = tk.Text(rframe, wrap="word", font=("Consolas", 10))
        self.q_text.pack(fill="both", expand=True, pady=2)

    # ------------------------------------------------------------- 辅助
    def _update_engine_label(self):
        self.engine_label.config(text=f"提取引擎：{engine_status()}")

    def _log(self, msg: str):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _pick_dir(self):
        d = filedialog.askdirectory(title="选择 PDF 所在文件夹")
        if d:
            self.pdf_dir.set(d)
            self._refresh_list()

    def _pick_out(self):
        d = filedialog.askdirectory(title="选择输出文件夹")
        if d:
            self.out_dir.set(d)
            self._load_previous()

    def _refresh_list(self):
        for i in self.file_tree.get_children():
            self.file_tree.delete(i)
        folder = self.pdf_dir.get().strip()
        if not folder or not Path(folder).exists():
            return
        pdfs = find_pdfs(folder)
        seen = set()
        for p in pdfs:
            if p.name in seen:
                continue
            seen.add(p.name)
            status = "已处理" if p.stem in self.results else "待处理"
            self.file_tree.insert("", "end", iid=p.stem,
                                  values=(p.name, f"{p.stat().st_size // 1024} KB", status))
        self.status.config(text=f"找到 {len(seen)} 个 PDF")

    def _cfg(self) -> CleanConfig:
        return CleanConfig(**{k: v.get() for k, v in self.rule_vars.items()})

    # ------------------------------------------------------------- 批处理
    def _start_batch(self, force=False):
        folder = self.pdf_dir.get().strip()
        if not folder or not Path(folder).exists():
            messagebox.showwarning(APP_TITLE, "请先选择 PDF 文件夹！")
            return
        if not (HAS_FITZ or HAS_PDFMINER):
            messagebox.showerror(APP_TITLE, "没有可用的 PDF 引擎！\n请安装 pymupdf 或 pdfminer.six")
            return
        pdfs = [p for p in find_pdfs(folder)]
        # 同名去重
        seen, uniq = set(), []
        for p in pdfs:
            if p.stem not in seen:
                seen.add(p.stem)
                uniq.append(p)
        pdfs = uniq
        if not pdfs:
            messagebox.showwarning(APP_TITLE, "该文件夹下没有 PDF 文件。")
            return
        todo = [p for p in pdfs if force or p.stem not in self.results]
        if not todo:
            messagebox.showinfo(APP_TITLE, "全部文献都已处理过。如需重跑请点『全部重新处理』。")
            return

        self.cancel_flag.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.config(maximum=len(todo), value=0)
        out = Path(self.out_dir.get().strip() or (Path(__file__).parent / "清洗结果"))
        out.mkdir(parents=True, exist_ok=True)

        def work():
            summary = []
            for n, p in enumerate(todo, 1):
                if self.cancel_flag.is_set():
                    self.q.put(("log", "已停止（保留已处理结果）。"))
                    break
                self.q.put(("status", f"[{n}/{len(todo)}] {p.name} …"))
                try:
                    result, quality = process_pdf(p, self._cfg(),
                                                  progress=lambda m: self.q.put(("log", f"  {m}")))
                    self.results[p.stem] = {"result": result, "quality": quality}
                    export_document(result, quality, out, p.stem, str(p))
                    summary.append((p.stem, result, quality))
                    self.q.put(("done_one", (p.stem, quality["status"])))
                except Exception as e:
                    traceback.print_exc()
                    self.q.put(("log", f"  ❌ 处理失败：{e}"))
                    self.q.put(("done_one", (p.stem, f"失败:{e}")))
            # 汇总（包含本次+此前已处理的）
            all_sum = [(k, v["result"], v["quality"]) for k, v in self.results.items()]
            if all_sum:
                try:
                    export_summary(all_sum, out)
                except Exception as e:
                    self.q.put(("log", f"汇总导出失败：{e}"))
            self.q.put(("batch_done", str(out)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self):
        self.cancel_flag.set()
        self.status.config(text="正在停止…")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "status":
                    self.status.config(text=payload)
                elif kind == "done_one":
                    name, st = payload
                    if self.file_tree.exists(name):
                        vals = list(self.file_tree.item(name, "values"))
                        vals[2] = st
                        self.file_tree.item(name, values=vals)
                    self.progress.step()
                elif kind == "batch_done":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.status.config(text=f"完成！结果已输出到：{payload}")
                    self._log("✅ 批量清洗完成")
                    self._refresh_lists()
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    # ------------------------------------------------------------- 结果展示
    def _refresh_lists(self):
        self._refresh_list()
        names = sorted(self.results.keys())
        self.doc_list.delete(0, "end")
        self.q_list.delete(0, "end")
        for n in names:
            self.doc_list.insert("end", n)
            self.q_list.insert("end", n)
        self._fill_head_tree()
        if names:
            self.doc_list.selection_set(0)
            self.q_list.selection_set(0)
            self._show_doc()
            self._show_quality()

    def _selected(self, lst: tk.Listbox) -> str | None:
        sel = lst.curselection()
        if not sel:
            return None
        return lst.get(sel[0])

    def _show_doc(self):
        name = self._selected(self.doc_list)
        self.struct_tree.delete(*self.struct_tree.get_children())
        self.text_view.delete("1.0", "end")
        if not name or name not in self.results:
            return
        res = self.results[name]["result"]

        t = self.struct_tree
        root = t.insert("", "end", text=f"📄 {name}", open=True)
        if res.get("title"):
            t.insert(root, "end", text="题目", values=(res["title"],))
        if res.get("abstract"):
            t.insert(root, "end", text="摘要",
                     values=(res["abstract"][:80] + "…",))
        if res.get("keywords"):
            t.insert(root, "end", text="关键词", values=("; ".join(res["keywords"]),))

        h_root = t.insert(root, "end", text=f"各级标题（{len(res.get('headings', []))}）", open=True)
        nodes = {}
        for h in res.get("headings", []):
            parent = nodes.get(h["parent_id"], h_root) if h["parent_id"] else h_root
            flag = " ⚠️" if h.get("confirmed") == "待确认" else ""
            hid = t.insert(parent, "end",
                           text=f"{'　' * (h['level'] - 1)}{h.get('number', '')} {h['text']}{flag}",
                           values=(f"p{h['page']}",))
            nodes[h["id"]] = hid

        if res.get("tables"):
            tb = t.insert(root, "end", text=f"表格标题（{len(res['tables'])}）")
            for x in res["tables"]:
                t.insert(tb, "end", text=f"{x['id']}", values=(x["caption"][:70],))
        if res.get("figures"):
            fg = t.insert(root, "end", text=f"图片标题（{len(res['figures'])}）")
            for x in res["figures"]:
                t.insert(fg, "end", text=f"{x['id']}", values=(x["caption"][:70],))
        t.insert(root, "end", text="参考文献",
                 values=(f"{len(res.get('references', []))} 条",))

        mode = self.view_mode.get()
        if mode == "clean":
            self.text_view.insert("1.0", document_markdown(res))
        elif mode == "raw":
            self.text_view.insert("1.0", res.get("raw_text", ""))
        else:
            self.text_view.insert("1.0", res.get("abstract", "（未识别到摘要）"))

    def _fill_head_tree(self):
        self.head_tree.delete(*self.head_tree.get_children())
        for name in sorted(self.results.keys()):
            res = self.results[name]["result"]
            top = self.head_tree.insert("", "end", text=f"📄 {name}",
                                        values=("", res.get("title", "")[:40]))
            for h in res.get("headings", []):
                parent = top
                pad = "　" * (h["level"] - 1)
                flag = " ⚠️" if h.get("confirmed") == "待确认" else ""
                self.head_tree.insert(parent, "end",
                                      text=f"{pad}{h.get('number', '')} {h['text']}{flag}",
                                      values=(f"p{h['page']}", h.get("confirmed", "")))

    def _show_quality(self):
        name = self._selected(self.q_list)
        self.q_text.delete("1.0", "end")
        if not name or name not in self.results:
            return
        v = self.results[name]
        self.q_text.insert("1.0", report_markdown(name, v["quality"]))

    # ------------------------------------------------------------- 导出
    def _export_all(self):
        if not self.results:
            messagebox.showinfo(APP_TITLE, "还没有可导出的结果，请先运行清洗。")
            return
        out = Path(self.out_dir.get().strip())
        out.mkdir(parents=True, exist_ok=True)
        for name, v in self.results.items():
            export_document(v["result"], v["quality"], out, name,
                            v["result"].get("source_file", ""))
        export_summary([(k, v["result"], v["quality"]) for k, v in self.results.items()], out)
        messagebox.showinfo(APP_TITLE, f"已导出到：{out}")
        open_folder(out)

    def _export_headings(self):
        if not self.results:
            messagebox.showinfo(APP_TITLE, "还没有结果。")
            return
        path = filedialog.asksaveasfilename(
            title="导出标题树汇总", defaultextension=".md",
            initialfile="汇总_标题树.md",
            filetypes=[("Markdown", "*.md"), ("文本", "*.txt")])
        if not path:
            return
        md = ["# 汇总标题树（所有文献）", ""]
        for name in sorted(self.results.keys()):
            md.append(headings_markdown(self.results[name]["result"], name))
            md.append("\n---\n")
        Path(path).write_text("\n".join(md), encoding="utf-8")
        messagebox.showinfo(APP_TITLE, f"已导出：{path}")

    # ------------------------------------------------------------- 持久化
    def _load_previous(self):
        """重新打开时自动载入上次的清洗结果（断点续跑）。"""
        out = Path(self.out_dir.get().strip())
        if not out.exists():
            return
        loaded = 0
        for d in sorted(out.iterdir()):
            f = d / "clean.json"
            if d.is_dir() and f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    self.results[d.name] = {"result": data, "quality": data.get("quality", {})}
                    loaded += 1
                except Exception:
                    pass
        if loaded:
            self._log(f"载入历史结果 {loaded} 篇（如需重跑请点『全部重新处理』）")
            self._refresh_lists()


if __name__ == "__main__":
    app = App()
    app.mainloop()
