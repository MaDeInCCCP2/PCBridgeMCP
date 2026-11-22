"""Local approval window for a command submitted through PC Bridge."""
import json
from pathlib import Path
import sys
import time


ARM_DELAY_MS = 1500


def palette():
    dark = False
    if sys.platform == "win32":
        import winreg

        try:
            path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                dark = winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except OSError:
            pass
    if dark:
        return dict(bg="#171c25", surface="#202733", border="#3a475a", text="#edf2f8",
                    muted="#b0bed0", warning="#f5c67b", highlight="#543743", focus="#7fb3ff")
    return dict(bg="#f6f8fc", surface="#ffffff", border="#c9d2df", text="#162132",
                muted="#4d5d72", warning="#8b430a", highlight="#fce4e8", focus="#1d56ae")


def build_dialog(root, req, done):
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk

    colors = palette()
    bg, surface, border = (colors[name] for name in ("bg", "surface", "border"))
    fg, muted, warning, focus = (colors[name] for name in ("text", "muted", "warning", "focus"))
    timeout = max(10, int(req.get("timeout", 120)))
    root.title("PC Bridge — подтверждение запуска")
    root.configure(bg=bg)
    root.attributes("-topmost", True)
    root.grid_columnconfigure(0, weight=1)
    root.grid_rowconfigure(1, weight=1)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Popup.Vertical.TScrollbar", troughcolor=surface, background=border,
                    bordercolor=surface, lightcolor=border, darkcolor=border, arrowcolor=muted)
    style.configure("Popup.TButton", padding=(10, 6), background=surface, foreground=fg,
                    bordercolor=border, lightcolor=surface, darkcolor=surface, focuscolor=focus)
    style.map("Popup.TButton", background=[("active", bg)], foreground=[("disabled", muted)])

    base = tkfont.nametofont("TkDefaultFont")
    base.configure(family="Segoe UI", size=10)
    bold = base.copy()
    bold.configure(weight="bold")
    heading = bold.copy()
    heading.configure(size=15)
    mono = tkfont.Font(family="Consolas", size=10)

    def label(parent, text, *, color=fg, font=base):
        return tk.Label(parent, text=text, font=font, fg=color, bg=bg, anchor="w", justify="left")

    def reading_box(parent, height, font, name):
        frame = tk.Frame(parent, bg=border, padx=1, pady=1)
        frame.pack(fill="both", expand=name == "command")
        scroll = ttk.Scrollbar(frame, style="Popup.Vertical.TScrollbar")
        scroll.pack(side="right", fill="y")
        text = tk.Text(frame, name=name, wrap="word", height=height, width=48, font=font,
                       bg=surface, fg=fg, relief="flat", padx=10, pady=7, takefocus=True,
                       highlightthickness=1, highlightbackground=surface, highlightcolor=focus,
                       yscrollcommand=scroll.set)
        scroll.configure(command=text.yview)
        text.pack(side="left", fill="both", expand=True)
        return text

    header = tk.Frame(root, bg=bg, padx=20, pady=13)
    header.grid(sticky="ew")
    label(header, "Запуск требует подтверждения", font=heading).pack(fill="x")
    label(header, "Проверьте причину и команду перед запуском.", color=muted).pack(fill="x", pady=(4, 0))

    body = tk.Frame(root, bg=bg, padx=20)
    body.grid(sticky="nsew")
    reasons = req.get("reasons") or ["Команда отмечена как потенциально опасная."]
    local_ai = req.get("local_ai_reason")
    explanation = req.get("risk_explanation")
    details = reading_box(body, 6 if explanation else 4, base, "risk")
    details.tag_configure("heading", font=bold, foreground=warning)
    details.tag_configure("secondary", font=bold, foreground=muted, spacing1=8)
    if local_ai:
        details.insert("end", "Локальная ИИ-проверка\n", "heading")
        details.insert("end", local_ai)
    else:
        details.insert("end", "Проверка PC Bridge\n", "heading")
        details.insert("end", "\n".join("• " + str(r) for r in reasons))
    if explanation:
        details.insert("end", "\nПояснение от агента\n", "secondary")
        details.insert("end", explanation)
    details.configure(state="disabled")

    label(body, "Рабочая папка", font=bold).pack(fill="x", pady=(10, 2))
    folder = tk.Entry(body, name="folder", font=mono, relief="flat", readonlybackground=surface,
                      fg=fg, highlightthickness=1, highlightbackground=border, highlightcolor=focus)
    folder.insert(0, req.get("cwd", ""))
    folder.configure(state="readonly")
    folder.pack(fill="x", ipady=4)
    label(body, "Команда", font=bold).pack(fill="x", pady=(10, 2))
    if req.get("command_truncated"):
        label(body, "Показаны первые 20 000 символов команды.", color=warning).pack(fill="x")
    command = reading_box(body, 5, mono, "command")
    command.insert("1.0", str(req.get("command", "")))
    command.tag_configure("risk", background=colors["highlight"], font=mono)
    for start, end in req.get("spans", [])[:50]:
        command.tag_add("risk", f"1.0+{start}c", f"1.0+{end}c")
    command.configure(state="disabled")

    footer = tk.Frame(root, bg=bg, padx=20, pady=12)
    footer.grid(sticky="ew")
    timer = label(footer, "", color=muted)
    timer.pack(fill="x")
    track = tk.Frame(footer, bg=border, height=4)
    track.pack(fill="x", pady=(5, 10))
    progress = tk.Frame(track, bg=warning)
    progress.place(relwidth=1, relheight=1)
    buttons = tk.Frame(footer, bg=bg)
    buttons.pack(fill="x")
    deny = tk.Button(buttons, name="deny", text="Запретить", font=bold, fg="#ffffff",
                     bg="#2056a5", activeforeground="#ffffff", activebackground="#184783",
                     relief="flat", padx=12, pady=5, highlightbackground=bg, highlightcolor=focus,
                     command=lambda: done("no"))
    deny.pack(side="right")
    once = ttk.Button(buttons, name="allow_once", text="Разрешить один раз", state="disabled", style="Popup.TButton",
                      command=lambda: done("yes"))
    once.pack(side="right", padx=(0, 8))
    allow = [once]
    if req.get("allow_similar"):
        minutes = int(req.get("similar_minutes", 10))
        similar = ttk.Button(buttons, name="allow_similar", text=f"Разрешить похожие на {minutes} мин",
                             state="disabled", style="Popup.TButton", command=lambda: done("similar"))
        similar.pack(side="left")
        allow.append(similar)

    def arm():
        for button in allow:
            button.configure(state="normal")

    deadline = time.monotonic() + timeout

    def tick():
        remaining = max(0, deadline - time.monotonic())
        if not remaining:
            done("timeout")
            return
        timer.configure(text=f"Отмена через {int(remaining + .999)} с · Enter или Esc — запретить")
        progress.place_configure(relwidth=remaining / timeout)
        root.after(250, tick)

    def deny_key(_event):
        done("no")
        return "break"

    root.bind("<Return>", deny_key)
    root.bind("<KP_Enter>", deny_key)
    root.bind("<Escape>", deny_key)
    root.protocol("WM_DELETE_WINDOW", lambda: done("no"))
    root.after(ARM_DELAY_MS, arm)
    tick()
    deny.focus_set()


def main() -> int:
    try:
        filename = sys.argv[1]
        payload = sys.stdin.buffer.read() if filename == "-" else Path(filename).read_bytes()
        req = json.loads(payload.decode("utf-8"))
    except (IndexError, OSError, ValueError):
        return 2
    try:
        import tkinter as tk
    except ImportError:
        return 3
    try:
        root = tk.Tk()
    except tk.TclError:
        return 3

    root.withdraw()
    answer = "no"

    def done(value):
        nonlocal answer
        answer = value
        root.destroy()

    build_dialog(root, req, done)
    root.update_idletasks()
    width = min(root.winfo_screenwidth() - 48, max(640, root.winfo_reqwidth()))
    height = min(root.winfo_screenheight() - 90, root.winfo_reqheight())
    x = (root.winfo_screenwidth() - width) // 2
    y = max(0, (root.winfo_screenheight() - height) // 3)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.minsize(min(width, 500), min(height, 450))
    root.deiconify()
    root.mainloop()
    print(answer, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
