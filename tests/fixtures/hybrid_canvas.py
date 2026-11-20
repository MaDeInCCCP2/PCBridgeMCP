"""Window owned by the hybrid-input smoke test."""
import sys
import tkinter as tk

root = tk.Tk()
root.title(sys.argv[1])
root.geometry("460x290+110+110")
root.attributes("-topmost", True)
canvas = tk.Canvas(root, bg="black", borderwidth=0, highlightthickness=0)
canvas.pack(fill=tk.BOTH, expand=True)
button = canvas.create_rectangle(44, 38, 177, 126, outline="white", width=3, fill="black")
canvas.create_text(108, 81, text="Test", fill="white")
canvas.tag_bind(button, "<Button-1>", lambda _: canvas.itemconfigure(button, fill="#ff5500"))
canvas.bind("<Button-1>", lambda _: canvas.itemconfigure(button, fill="#ff5500"))
second = None


def create_second():
    global second
    second = tk.Toplevel(root)
    second.title(sys.argv[1] + " Focus target")
    second.geometry("280x130+230+190")
    second.attributes("-topmost", True)
    second.focus_force()


root.bind("<Key-w>", lambda _: root.after(120, create_second) if second is None else None)
root.after(100, root.focus_force)
root.mainloop()
