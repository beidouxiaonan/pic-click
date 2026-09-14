"""Exercise real desktop capture and Tk event flow without changing clipboard."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageGrab
import sys
from app import App, setup_dpi

setup_dpi()
root = tk.Tk()
app = App(root, Path(__file__).parent / "test-output" / "desktop")
app.export_dir = Path(__file__).parent / "test-output" / "configured-exports"
app.auto_save_png = True
auto_document = app.create_document(Image.new("RGB", (32, 24), "white"))
assert app.last_auto_save and app.last_auto_save.exists()
assert auto_document.data["auto_saved_png"] == str(app.last_auto_save.resolve())
app.auto_save_png = False
errors = []
import app as module
def show_error(title, text, **kwargs):
    raise RuntimeError(title + str(text))
module.messagebox.showerror = show_error
synthetic = "--synthetic" in sys.argv
if synthetic:
    module.ImageGrab.grab = lambda **kwargs: Image.new("RGB", (1280, 720), "#edf3fa")

def fail(kind, value, trace):
    import traceback
    traceback.print_exception(kind, value, trace)
    errors.append(str(value))
    app.quit()

root.report_callback_exception = fail

def select():
    overlays = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    overlay = overlays[0]
    canvas = next(w for w in overlay.winfo_children() if isinstance(w, tk.Canvas))
    canvas.event_generate("<ButtonPress-1>", x=40, y=40)
    canvas.event_generate("<B1-Motion>", x=440, y=240)
    canvas.event_generate("<ButtonRelease-1>", x=440, y=240)
    root.update()
    assert len(app.pins) == 0, "selection must wait for a capture Dock action"
    def descendants(widget):
        result = list(widget.winfo_children())
        for child in list(result):
            result.extend(descendants(child))
        return result
    complete = next(widget for widget in descendants(overlay)
                    if isinstance(widget, tk.Button) and "完成" in widget.cget("text"))
    assert any(isinstance(widget, tk.Button) and "滚动截图" in widget.cget("text")
               for widget in descendants(overlay)), "capture Dock must expose scrolling capture"
    assert any(isinstance(widget, tk.Button) and "录制" in widget.cget("text")
               for widget in descendants(overlay)), "capture Dock must expose video recording"
    complete.invoke()
    root.update()
    assert len(app.pins) == 1
    pin = app.pins[0]
    dock_symbols = {getattr(child, "cget", lambda key: "")("text") for child in pin.actions.winfo_children()}
    assert "▣" in dock_symbols, "single Dock must expose copy"
    assert "▤" in dock_symbols, "single Dock must expose pin-to-desktop"
    assert pin.doc.base.size == (400, 200)
    assert not app.capturing and not pin.compact
    assert not overlay.winfo_exists(), "selection overlay must close after mouse release"
    assert pin.state() == "normal", "annotation window must be usable immediately"
    root.update()
    root.update()
    pin.tool.set("矩形")
    pin.canvas.event_generate("<ButtonPress-1>", x=150, y=40)
    pin.canvas.event_generate("<B1-Motion>", x=300, y=100)
    pin.canvas.event_generate("<ButtonRelease-1>", x=300, y=100)
    assert len(pin.doc.data["marks"]) == 1
    pin.undo()
    assert len(pin.doc.data["marks"]) == 0
    pin.tool.set("文字")
    pin.canvas.event_generate("<ButtonPress-1>", x=180, y=65)
    assert pin.text_editor is not None
    assert pin.text_editor_window is not None and pin.text_editor_window.winfo_exists()
    if sys.platform == "win32":
        assert str(pin.text_editor_window.attributes("-transparentcolor")) == pin.text_transparent_color
    pin.text_editor.insert("1.0", "小文本块\n第二行")
    pin.finish_text()
    assert pin.doc.data["marks"][-1]["text"] == "小文本块\n第二行"
    pin.begin_text(50, 40, 0)
    pin.text_editor.delete("1.0", "end")
    pin.text_editor.insert("1.0", "修改文本")
    pin.finish_text()
    assert pin.doc.data["marks"][0]["text"] == "修改文本"
    pin.begin_text(50, 40)
    pin.text_editor.insert("1.0", "取消这段")
    pin.finish_text(cancel=True)
    assert len(pin.doc.data["marks"]) == 1
    pin.undo()
    assert pin.doc.data["marks"][0]["text"] == "小文本块\n第二行"
    pin.redo()
    assert pin.doc.data["marks"][0]["text"] == "修改文本"
    from types import SimpleNamespace
    pin.tool.set("移动")
    assert pin.hit_test(55, 50) == 0
    start_x = pin.offset[0] + 55 * pin.scale
    start_y = pin.offset[1] + 50 * pin.scale
    pin.press(SimpleNamespace(x=start_x, y=start_y, x_root=100, y_root=100))
    pin.motion(SimpleNamespace(x=start_x+40*pin.scale, y=start_y+25*pin.scale, x_root=140, y_root=125))
    pin.release(SimpleNamespace(x=start_x+40*pin.scale, y=start_y+25*pin.scale, x_root=140, y_root=125))
    moved_xy = pin.doc.data["marks"][0]["xy"][:2]
    assert round(moved_xy[0]) == 90 and round(moved_xy[1]) == 65
    pin.undo()
    assert pin.doc.data["marks"][0]["xy"][:2] == [50, 40]
    pin.redo()
    assert round(pin.doc.data["marks"][0]["xy"][0]) == 90
    pin.pin_desktop()
    root.update()
    assert not app.capturing and pin.compact
    assert pin.overrideredirect()
    pin.wheel(SimpleNamespace(state=0, delta=-120))
    assert pin.zoom < 1
    pin.wheel(SimpleNamespace(state=4, delta=-120))
    assert pin.opacity < 1
    pin.toggle_edit()
    root.update()
    assert not pin.compact
    pin.tool.set("画笔")
    pin.canvas.event_generate("<ButtonPress-1>", x=210, y=100)
    pin.canvas.event_generate("<B1-Motion>", x=250, y=140)
    pin.canvas.event_generate("<ButtonRelease-1>", x=260, y=150)
    assert pin.doc.data["marks"][-1]["kind"] == "画笔"
    brush_index = len(pin.doc.data["marks"]) - 1
    brush = pin.doc.data["marks"][brush_index]
    brush_point = brush["points"][0]
    pin.tool.set("移动")
    assert pin.hit_test(*brush_point) == brush_index
    bx = pin.offset[0] + brush_point[0] * pin.scale
    by = pin.offset[1] + brush_point[1] * pin.scale
    pin.press(SimpleNamespace(x=bx, y=by, x_root=100, y_root=100))
    pin.motion(SimpleNamespace(x=bx+15*pin.scale, y=by+10*pin.scale, x_root=115, y_root=110))
    pin.release(SimpleNamespace(x=bx+15*pin.scale, y=by+10*pin.scale, x_root=115, y_root=110))
    moved_point = pin.doc.data["marks"][brush_index]["points"][0]
    assert round(moved_point[0]-brush_point[0]) == 15 and round(moved_point[1]-brush_point[1]) == 10
    number_x, number_y = 320, 120
    pin.tool.set("序号")
    pin.press(SimpleNamespace(x=pin.offset[0]+number_x*pin.scale, y=pin.offset[1]+number_y*pin.scale,
                              x_root=100, y_root=100))
    number_index = len(pin.doc.data["marks"]) - 1
    assert pin.doc.data["marks"][number_index]["kind"] == "序号"
    pin.tool.set("移动")
    assert pin.hit_test(number_x, number_y) == number_index
    nx, ny = pin.offset[0]+number_x*pin.scale, pin.offset[1]+number_y*pin.scale
    pin.press(SimpleNamespace(x=nx, y=ny, x_root=100, y_root=100))
    pin.motion(SimpleNamespace(x=nx-20*pin.scale, y=ny+12*pin.scale, x_root=80, y_root=112))
    pin.release(SimpleNamespace(x=nx-20*pin.scale, y=ny+12*pin.scale, x_root=80, y_root=112))
    assert [round(v) for v in pin.doc.data["marks"][number_index]["xy"][:2]] == [300, 132]
    original_size = pin.doc.base.size
    pin.rotate()
    assert pin.doc.base.size == original_size[::-1]
    pin.flip(True)
    pin.toggle_click_through()
    assert pin.click_through
    app.restore_clicks()
    assert not pin.click_through
    app.capturing = True
    app.begin_scroll_capture((100, 100, 500, 300), Image.new("RGB", (400, 200), "#edf3fa"))
    root.update()
    preview = next(window for window in root.winfo_children()
                   if isinstance(window, tk.Toplevel) and "滚动截图预览" in window.title())
    preview_canvases = [widget for widget in descendants(preview) if isinstance(widget, tk.Canvas)]
    assert preview_canvases, "scrolling capture must show a stitched-image preview canvas"
    assert preview_canvases[0].cget("scrollregion"), "preview must expose the accumulated image"
    cancel = next(widget for widget in descendants(preview)
                  if isinstance(widget, ttk.Button) and widget.cget("text") == "取消")
    cancel.invoke()
    root.update()
    assert not preview.winfo_exists() and not app.capturing
    ImageGrab.grab(all_screens=True).save(Path(__file__).parent / "test-output" / "desktop-verification.png")
    print("DESKTOP_OK: " + ("synthetic screen" if synthetic else "real screen") + ", capture Dock, scrolling preview, pin and annotation workflow", flush=True)
    app.quit()

root.after(150, app.capture)
root.after(2500, select)
root.after(10000, lambda: (errors.append("Timeout"), app.quit()))
root.mainloop()
if errors:
    raise RuntimeError(errors)
