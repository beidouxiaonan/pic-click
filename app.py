"""pic-click: a native Windows screenshot, annotation and pinboard application."""
from __future__ import annotations

import argparse
import base64
import copy
import ctypes
from ctypes import wintypes
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageGrab, ImageStat, ImageTk

VENDOR_DIR = Path(__file__).resolve().parent / ".vendor"
if VENDOR_DIR.exists() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))


def setup_dpi():
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except OSError:
            ctypes.windll.user32.SetProcessDPIAware()


def font(size=22):
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/arial.ttf"):
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default()


def render(base, marks):
    result = base.convert("RGBA")
    for mark in marks:
        layer = Image.new("RGBA", result.size)
        draw = ImageDraw.Draw(layer)
        x1, y1, x2, y2 = mark["xy"]
        box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        color = mark.get("color", "#ff3b30")
        kind = mark["kind"]
        width = mark.get("width", 3)
        if kind == "矩形":
            draw.rectangle(box, outline=color, width=width)
        elif kind == "椭圆":
            draw.ellipse(box, outline=color, width=width)
        elif kind == "画笔":
            points = [tuple(p) for p in mark.get("points", [(x1, y1), (x2, y2)])]
            if len(points) > 1:
                draw.line(points, fill=color, width=width, joint="curve")
        elif kind == "高亮":
            draw.rectangle(box, fill=(255, 222, 0, 85))
        elif kind == "马赛克":
            left, top, right, bottom = map(round, box)
            if right > left and bottom > top:
                piece = result.crop((left, top, right, bottom))
                small = piece.resize((max(1, piece.width//12), max(1, piece.height//12)), Image.Resampling.BILINEAR)
                result.paste(small.resize(piece.size, Image.Resampling.NEAREST), (left, top))
                continue
        elif kind == "序号":
            radius = max(12, width * 4)
            draw.ellipse((x1-radius, y1-radius, x1+radius, y1+radius), fill=color, outline="white", width=2)
            label = str(mark.get("number", 1))
            face = font(max(14, radius))
            bounds = draw.textbbox((0, 0), label, font=face)
            draw.text((x1-(bounds[2]-bounds[0])/2, y1-(bounds[3]-bounds[1])/2-2), label, font=face, fill="white")
        elif kind == "箭头":
            import math
            draw.line((x1, y1, x2, y2), fill=color, width=width)
            angle = math.atan2(y2-y1, x2-x1)
            points = [(x2, y2)] + [(x2-17*math.cos(angle+a), y2-17*math.sin(angle+a)) for a in (-0.5, 0.5)]
            draw.polygon(points, fill=color)
        elif kind == "文字":
            draw.text((x1, y1), mark["text"], font=font(mark.get("size", 22)), fill=color,
                      stroke_width=1, stroke_fill="white")
        result = Image.alpha_composite(result, layer)
    return result.convert("RGB")


def text_card(value, background="#ffffff", foreground="#202124"):
    """Turn plain text or a color value into a readable floating image."""
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        background = value
        rgb = tuple(int(value[i:i+2], 16) for i in (1, 3, 5))
        foreground = "#ffffff" if sum(rgb) < 360 else "#111111"
        lines = [value.upper(), f"RGB({rgb[0]}, {rgb[1]}, {rgb[2]})"]
    else:
        lines = []
        for paragraph in value.splitlines() or [""]:
            lines.extend(textwrap.wrap(paragraph, width=46, replace_whitespace=False) or [""])
    face = font(22)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    widths = [probe.textbbox((0, 0), line, font=face)[2] for line in lines]
    width = min(900, max(260, max(widths, default=0) + 48))
    height = max(120, len(lines) * 34 + 48)
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text((24, 22 + i * 34), line, font=face, fill=foreground)
    return image


def image_difference(first, second):
    """Return a mean per-channel difference in the 0..255 range."""
    if first.size != second.size:
        return 255.0
    stat = ImageStat.Stat(ImageChops.difference(first.convert("RGB"), second.convert("RGB")))
    return sum(stat.mean) / len(stat.mean)


def find_scroll_overlap(previous, current):
    """Find where the previous viewport's bottom continues in the current viewport."""
    if previous.size != current.size:
        return 0, 0, 255.0
    width, height = previous.size
    sample_width = min(192, width)
    sample_height = min(height, max(96, round(height * sample_width / max(1, width))))
    prev = previous.resize((sample_width, sample_height), Image.Resampling.BILINEAR).convert("L")
    curr = current.resize((sample_width, sample_height), Image.Resampling.BILINEAR).convert("L")
    min_overlap = max(8, sample_height // 8)
    max_header = min(20, sample_height // 4)
    best = (0, min_overlap, 255.0)
    for start in range(0, max_header + 1, 2):
        max_overlap = sample_height - start - 3
        for overlap in range(min_overlap, max_overlap + 1, 2):
            a = prev.crop((0, sample_height-overlap, sample_width, sample_height))
            b = curr.crop((0, start, sample_width, start+overlap))
            score = ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
            # Prefer a longer overlap when two matches are visually equivalent.
            adjusted = score - overlap * 0.002
            if adjusted < best[2] - best[1] * 0.002:
                best = (start, overlap, score)
    scale = height / sample_height
    coarse_start, coarse_overlap = round(best[0] * scale), round(best[1] * scale)
    # Refine around the downsampled estimate at original vertical resolution.
    # This removes the 1–3 px seams otherwise introduced by resize rounding.
    prev_detail = previous.resize((sample_width, height), Image.Resampling.BILINEAR).convert("L")
    curr_detail = current.resize((sample_width, height), Image.Resampling.BILINEAR).convert("L")
    refined = (coarse_start, coarse_overlap, 255.0)
    search_radius = max(8, round(scale * 3))
    for start in range(max(0, coarse_start-search_radius),
                       min(height//4, coarse_start+search_radius) + 1):
        for overlap in range(max(8, coarse_overlap-search_radius),
                             min(height-start-2, coarse_overlap+search_radius) + 1):
            a = prev_detail.crop((0, height-overlap, sample_width, height))
            b = curr_detail.crop((0, start, sample_width, start+overlap))
            score = ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
            if score - overlap * 0.002 < refined[2] - refined[1] * 0.002:
                refined = (start, overlap, score)
    return refined


def stitch_scroll_frames(frames):
    """Stitch viewport captures, removing detected overlaps and repeated headers."""
    if not frames:
        raise ValueError("至少需要一帧截图")
    parts = [frames[0].convert("RGB")]
    for previous, current in zip(frames, frames[1:]):
        start, overlap, score = find_scroll_overlap(previous, current)
        cut = start + overlap if score < 28 else current.height // 4
        if cut < current.height - 2:
            parts.append(current.convert("RGB").crop((0, cut, current.width, current.height)))
    output = Image.new("RGB", (max(part.width for part in parts), sum(part.height for part in parts)), "white")
    y = 0
    for part in parts:
        output.paste(part, (0, y))
        y += part.height
    return output


class ScrollFrameCollector:
    """Collect stable, downward-scrolled frames only when a reliable overlap exists."""
    def __init__(self, first_frame, overlap_threshold=28):
        first_frame = first_frame.convert("RGB")
        self.frames = [first_frame]
        self.parts = [first_frame]
        self.cuts = []
        self.overlap_threshold = overlap_threshold
        self.candidate = None
        self.stable_polls = 0

    def add(self, frame):
        frame = frame.convert("RGB")
        if image_difference(self.frames[-1], frame) < 1.0:
            self.candidate = None
            self.stable_polls = 0
            return "unchanged"
        # Ignore banners, clocks, cursors and video controls changing near the
        # top when the page body itself has not moved.
        body_top = frame.height // 5
        previous_body = self.frames[-1].crop((0, body_top, frame.width, frame.height))
        current_body = frame.crop((0, body_top, frame.width, frame.height))
        if image_difference(previous_body, current_body) < 1.0:
            self.candidate = None
            self.stable_polls = 0
            return "unchanged"
        start, overlap, score = find_scroll_overlap(self.frames[-1], frame)
        if score >= self.overlap_threshold:
            self.candidate = None
            self.stable_polls = 0
            return "no_overlap"
        cut = start + overlap
        # Cursor blinking, video, ads and hover effects can make an otherwise
        # unchanged viewport look different. A real downward scroll must expose
        # a meaningful strip of new pixels at the bottom.
        minimum_advance = max(16, frame.height // 50)
        if frame.height - cut < minimum_advance:
            self.candidate = None
            self.stable_polls = 0
            return "unchanged"
        self.frames.append(frame)
        self.cuts.append(cut)
        self.parts.append(frame.crop((0, cut, frame.width, frame.height)))
        self.candidate = None
        self.stable_polls = 0
        return "captured"

    def observe(self, frame):
        frame = frame.convert("RGB")
        if image_difference(self.frames[-1], frame) < 1.0:
            self.candidate = None
            self.stable_polls = 0
            return "unchanged"
        if self.candidate is None or image_difference(self.candidate, frame) >= 1.0:
            self.candidate = frame
            self.stable_polls = 0
            return "settling"
        self.stable_polls += 1
        if self.stable_polls < 1:
            return "settling"
        return self.add(frame)

    def image(self):
        output = Image.new("RGB", (max(part.width for part in self.parts),
                                   sum(part.height for part in self.parts)), "white")
        y = 0
        for part in self.parts:
            output.paste(part, (0, y))
            y += part.height
        return output


def point_segment_distance(px, py, x1, y1, x2, y2):
    dx, dy = x2-x1, y2-y1
    if dx == 0 and dy == 0:
        return ((px-x1)**2 + (py-y1)**2) ** 0.5
    amount = max(0, min(1, ((px-x1)*dx + (py-y1)*dy) / (dx*dx + dy*dy)))
    x, y = x1 + amount*dx, y1 + amount*dy
    return ((px-x)**2 + (py-y)**2) ** 0.5


def load_settings(path, defaults):
    result = dict(defaults)
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(value, dict):
            for key in defaults:
                if key in value and isinstance(value[key], type(defaults[key])):
                    result[key] = value[key]
    except (OSError, ValueError):
        pass
    return result


def save_settings(path, settings):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def protect_secret(value):
    if not value:
        return ""
    raw = value.encode("utf-8")
    if sys.platform != "win32":
        return "plain:" + base64.b64encode(raw).decode("ascii")
    buffer = ctypes.create_string_buffer(raw)
    source = DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "pic-click", None, None, None, 0,
                                                  ctypes.byref(output)):
        raise OSError("Windows 无法加密云端凭据")
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def unprotect_secret(value):
    if not value:
        return ""
    prefix, _, encoded = value.partition(":")
    if prefix == "plain":
        return base64.b64decode(encoded).decode("utf-8")
    if prefix != "dpapi" or sys.platform != "win32":
        return ""
    raw = base64.b64decode(encoded)
    buffer = ctypes.create_string_buffer(raw)
    source = DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0,
                                                    ctypes.byref(output)):
        raise OSError("Windows 无法解密云端凭据")
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def cloud_target_url(config, filename):
    endpoint = config["endpoint"].rstrip("/") + "/"
    folder = config.get("remote_folder", "").strip("/")
    relative = "/".join(filter(None, (folder, filename)))
    return urllib.parse.urljoin(endpoint, urllib.parse.quote(relative, safe="/"))


def multipart_body(field, filename, payload, boundary):
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: image/png\r\n\r\n").encode("utf-8")
    return head + payload + f"\r\n--{boundary}--\r\n".encode("ascii")


def ensure_webdav_folder(config, headers, timeout):
    """Create the configured WebDAV folder one level at a time when needed."""
    current = config["endpoint"].rstrip("/") + "/"
    for segment in filter(None, config.get("remote_folder", "").strip("/").split("/")):
        current = urllib.parse.urljoin(current, urllib.parse.quote(segment, safe="") + "/")
        request = urllib.request.Request(current, headers=headers, method="MKCOL")
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                pass
        except urllib.error.HTTPError as error:
            if error.code not in {301, 302, 405}:
                raise


def upload_png(config, filename, payload, timeout=30):
    """Upload PNG bytes through WebDAV PUT or a multipart HTTP API and return a link."""
    provider = config.get("provider", "WebDAV")
    secret = unprotect_secret(config.get("secret", ""))
    headers = {"User-Agent": "pic-click/1.0"}
    if provider == "WebDAV":
        if config.get("username") or secret:
            token = base64.b64encode(f"{config.get('username', '')}:{secret}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = "Basic " + token
        ensure_webdav_folder(config, headers, timeout)
        target = cloud_target_url(config, filename)
        headers["Content-Type"] = "image/png"
        request = urllib.request.Request(target, data=payload, headers=headers, method="PUT")
    else:
        target = config["endpoint"]
        boundary = "picclick" + uuid.uuid4().hex
        body = multipart_body("file", filename, payload, boundary)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        if secret:
            headers["Authorization"] = "Bearer " + secret
        if config.get("username"):
            headers["X-Pic-Click-User"] = config["username"]
        request = urllib.request.Request(target, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content = response.read().decode("utf-8", errors="replace").strip()
    link = ""
    if content:
        try:
            result = json.loads(content)
            if isinstance(result, dict):
                link = next((result[key] for key in ("url", "link", "download_url")
                             if isinstance(result.get(key), str)), "")
        except ValueError:
            if content.startswith(("http://", "https://")):
                link = content
    if not link and config.get("public_base_url"):
        folder = config.get("remote_folder", "").strip("/")
        relative = "/".join(filter(None, (folder, filename)))
        link = urllib.parse.urljoin(config["public_base_url"].rstrip("/") + "/",
                                    urllib.parse.quote(relative, safe="/"))
    if provider != "WebDAV" and not link:
        raise ValueError("HTTP API 上传成功，但响应中没有 url、link 或 download_url，也未配置公开链接基础 URL")
    return link or target


class Document:
    def __init__(self, folder, base=None):
        self.folder = Path(folder)
        if base is not None:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.base = base.convert("RGB")
            self.base.save(self.folder / "original.png")
            self.data = {"version": 1, "marks": [], "notes": "", "attachments": []}
            self.save()
        else:
            self.base = Image.open(self.folder / "original.png").convert("RGB")
            self.data = json.loads((self.folder / "metadata.json").read_text(encoding="utf-8"))

    def save(self):
        temp = self.folder / "metadata.tmp"
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.folder / "metadata.json")
        self.auto_save_error = None
        target_value = self.data.get("auto_saved_png")
        if target_value:
            target = Path(target_value)
            temporary_image = target.with_suffix(target.suffix + ".tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.image().save(temporary_image, format="PNG")
                temporary_image.replace(target)
            except OSError as error:
                self.auto_save_error = str(error)

    def attach(self, path):
        source = Path(path)
        folder = self.folder / "attachments"
        folder.mkdir(exist_ok=True)
        relative = Path("attachments") / (uuid.uuid4().hex[:8] + "_" + source.name)
        shutil.copy2(source, self.folder / relative)
        self.data["attachments"].append({"name": source.name, "path": relative.as_posix()})
        self.save()

    def image(self):
        return render(self.base, self.data["marks"])


def copy_image(image):
    """Transfer an owned CF_DIB allocation to the Windows clipboard."""
    stream = io.BytesIO()
    image.convert("RGB").save(stream, "BMP")
    payload = stream.getvalue()[14:]
    kernel, user = ctypes.windll.kernel32, ctypes.windll.user32
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE
    handle = kernel.GlobalAlloc(0x0002, len(payload))
    if not handle:
        raise OSError("无法分配剪贴板内存")
    transferred = False
    try:
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise OSError("无法锁定剪贴板内存")
        ctypes.memmove(pointer, payload, len(payload))
        kernel.GlobalUnlock(handle)
        if not user.OpenClipboard(None):
            raise OSError("剪贴板正忙，请重试")
        try:
            user.EmptyClipboard()
            if not user.SetClipboardData(8, handle):
                raise OSError("写入剪贴板失败")
            transferred = True
        finally:
            user.CloseClipboard()
    finally:
        if not transferred:
            kernel.GlobalFree(handle)


class Pin(tk.Toplevel):
    def __init__(self, app, document):
        super().__init__(app.root)
        self.app, self.doc = app, document
        self.title("pic-click · 截图标注与贴图")
        self.attributes("-topmost", True)
        self.tool = tk.StringVar(value="移动")
        self.top = tk.BooleanVar(value=True)
        self.scale = 1.0
        self.draft = None
        self.notes_window = None
        self.text_editor = None
        self.text_editor_window = None
        self.text_transparent_color = "#010203"
        self.selected_index = None
        self.move_origin = None
        self.move_original = None
        self.move_checkpointed = False
        self.undo_stack, self.redo_stack = [], []
        self.compact = False
        self.capture_done = None
        self.zoom = 1.0
        self.opacity = 1.0
        self.click_through = False
        self.color = tk.StringVar(value="#ff3b30")
        self.stroke = tk.IntVar(value=3)
        self.text_size = tk.IntVar(value=22)
        bar = ttk.Frame(self, padding=5)
        self.bar = bar
        bar.pack(fill="x")
        for name in ("移动", "矩形", "椭圆", "箭头", "画笔", "高亮", "马赛克", "序号", "文字"):
            ttk.Radiobutton(bar, text="移动标注" if name == "移动" else name,
                            value=name, variable=self.tool).pack(side="left")
        actions = ttk.Frame(self, padding=(5, 0, 5, 5))
        self.actions = actions
        actions.pack(fill="x")
        for text, command in (("撤销", self.undo), ("重做", self.redo), ("保存 PNG", self.export),
                              ("上传云端", self.upload_cloud), ("保存设置", self.app.settings_dialog),
                              ("复制", self.copy), ("关联说明", self.notes), ("贴到桌面", self.pin_desktop)):
            ttk.Button(actions, text=text, command=command).pack(side="left", padx=2)
        ttk.Checkbutton(actions, text="置顶", variable=self.top,
                        command=lambda: self.attributes("-topmost", self.top.get())).pack(side="left")
        self.options = ttk.Frame(self, padding=5)
        self.options.pack(fill="x")
        for color in ("#ff3b30", "#ffcc00", "#00aa66", "#1683ff", "#222222", "#ffffff"):
            tk.Button(self.options, bg=color, width=2, command=lambda c=color: self.color.set(c)).pack(side="left", padx=2)
        ttk.Label(self.options, text="线宽").pack(side="left")
        ttk.Combobox(self.options, textvariable=self.stroke, values=(2, 3, 5, 8, 12), width=3, state="readonly").pack(side="left")
        ttk.Label(self.options, text="字号").pack(side="left")
        ttk.Combobox(self.options, textvariable=self.text_size, values=(14, 18, 22, 28, 36, 48), width=3, state="readonly").pack(side="left")
        self.canvas = tk.Canvas(self, bg="#e8e8e8", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.status = ttk.Label(self, text="移动标注可拖动文字、序号、画笔和形状 · Ctrl+Z 撤销", padding=5)
        self.status.pack(fill="x")
        w, h = self.doc.base.size
        w = min(max(w, 600), int(self.winfo_screenwidth() * .8))
        h = min(max(h + 110, 250), int(self.winfo_screenheight() * .8))
        self.geometry(f"{w}x{h}")
        self.canvas.bind("<Configure>", lambda event: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.canvas.bind("<Double-Button-1>", self.edit_text)
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<Button-3>", self.menu)
        self.bind("<space>", self.toggle_edit)
        self.bind("<Escape>", lambda event: self.withdraw() if self.compact else self.close())
        self.bind("<Control-y>", lambda event: self.redo())
        self.bind("<Delete>", lambda event: self.delete_selected())
        self.bind("<Control-w>", lambda event: self.close())
        self.bind("<Key-1>", lambda event: self.rotate(True) if self.compact else None)
        self.bind("<Key-2>", lambda event: self.rotate(False) if self.compact else None)
        self.bind("<Key-3>", lambda event: self.flip(True) if self.compact else None)
        self.bind("<Key-4>", lambda event: self.flip(False) if self.compact else None)
        self.bind("<plus>", lambda event: self.keyboard_zoom(1))
        self.bind("<minus>", lambda event: self.keyboard_zoom(-1))
        self.bind("<Left>", lambda event: self.nudge(-1, 0))
        self.bind("<Right>", lambda event: self.nudge(1, 0))
        self.bind("<Up>", lambda event: self.nudge(0, -1))
        self.bind("<Down>", lambda event: self.nudge(0, 1))
        self.canvas.bind("<Button-2>", lambda event: self.reset_zoom())
        self.bind("<Control-z>", lambda event: self.undo())
        self.bind("<Control-s>", lambda event: self.export())
        self.bind("<Control-c>", lambda event: self.copy())
        self.protocol("WM_DELETE_WINDOW", self.close)
        app.pins.append(self)

    def checkpoint(self):
        self.undo_stack.append(copy.deepcopy(self.doc.data["marks"]))
        self.redo_stack.clear()

    def pin_desktop(self):
        self.finish_text()
        if self.capture_done:
            callback, self.capture_done = self.capture_done, None
            callback()
        self.compact = True
        self.selected_index = None
        self.zoom = min(self.zoom, self.winfo_screenwidth()/self.doc.base.width, self.winfo_screenheight()/self.doc.base.height)
        self.tool.set("移动")
        for widget in (self.bar, self.actions, self.options, self.status):
            widget.pack_forget()
        self.overrideredirect(True)
        self.resize_pin()
        self.focus_force()

    def resize_pin(self):
        self.geometry(f"{max(40, round(self.doc.base.width*self.zoom))}x{max(30, round(self.doc.base.height*self.zoom))}")

    def toggle_edit(self, event=None):
        if self.text_editor:
            return
        if not self.compact:
            self.pin_desktop()
        else:
            self.compact = False
            self.overrideredirect(False)
            for widget in (self.bar, self.actions, self.options):
                widget.pack(fill="x", before=self.canvas)
            self.status.pack(fill="x")
            self.geometry(f"{max(680, self.doc.base.width)}x{min(self.winfo_screenheight()-100, self.doc.base.height+160)}")
        return "break"

    def wheel(self, event):
        if event.state & 4:
            self.opacity = max(.2, min(1.0, self.opacity + (.05 if event.delta > 0 else -.05)))
            self.attributes("-alpha", self.opacity)
        elif self.compact:
            limit = min(4, self.winfo_screenwidth()/self.doc.base.width, self.winfo_screenheight()/self.doc.base.height)
            self.zoom = max(.1, min(limit, self.zoom * (1.1 if event.delta > 0 else 1/1.1)))
            self.resize_pin()
        return "break"

    def menu(self, event):
        menu = tk.Menu(self, tearoff=False)
        for label, command in (("编辑标注 / 贴图（Space）", self.toggle_edit), ("复制图片", self.copy),
                               ("保存 PNG", self.export), ("上传云端", self.upload_cloud),
                               ("关联说明", self.notes), ("顺时针旋转 90°（1）", lambda: self.rotate(True)),
                               ("逆时针旋转 90°（2）", lambda: self.rotate(False)),
                               ("水平翻转（3）", lambda: self.flip(True)), ("垂直翻转（4）", lambda: self.flip(False)),
                               ("鼠标穿透", self.toggle_click_through), ("恢复原尺寸", self.reset_zoom),
                               ("隐藏（Esc）", self.withdraw), ("关闭贴图", self.close)):
            menu.add_command(label=label, command=command)
        menu.tk_popup(event.x_root, event.y_root)

    def flatten_transform(self, operation):
        self.finish_text()
        image = operation(self.doc.image())
        self.doc.base = image.convert("RGB")
        self.doc.data["marks"] = []
        self.doc.base.save(self.doc.folder / "original.png")
        self.doc.save()
        self.undo_stack.clear()
        self.redo_stack.clear()
        if self.compact:
            self.resize_pin()
        self.redraw()

    def rotate(self, clockwise=True):
        self.flatten_transform(lambda image: image.transpose(Image.Transpose.ROTATE_270 if clockwise else Image.Transpose.ROTATE_90))

    def flip(self, horizontal=True):
        self.flatten_transform(lambda image: image.transpose(Image.Transpose.FLIP_LEFT_RIGHT if horizontal else Image.Transpose.FLIP_TOP_BOTTOM))

    def toggle_click_through(self):
        if sys.platform != "win32":
            return
        self.click_through = not self.click_through
        hwnd = self.winfo_id()
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        transparent = 0x00000020
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style | transparent if self.click_through else style & ~transparent)
        self.status.config(text="鼠标穿透已开启；按 F4 恢复所有贴图交互" if self.click_through else "鼠标穿透已关闭")

    def keyboard_zoom(self, direction):
        if self.compact:
            self.zoom = max(.1, min(4, self.zoom * (1.1 if direction > 0 else 1/1.1)))
            self.resize_pin()
        return "break"

    def nudge(self, dx, dy):
        if self.compact:
            self.geometry(f"+{self.winfo_x()+dx}+{self.winfo_y()+dy}")
        return "break"

    def reset_zoom(self):
        self.zoom = 1.0
        self.opacity = 1.0
        self.attributes("-alpha", 1.0)
        if self.compact:
            self.resize_pin()

    def redraw(self):
        image = self.doc.image() if not self.draft else render(self.doc.base, self.doc.data["marks"] + [self.draft])
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        self.scale = min(cw / image.width, ch / image.height, 4.0 if self.compact else 1.0)
        size = (max(1, round(image.width*self.scale)), max(1, round(image.height*self.scale)))
        self.offset = ((cw-size[0])//2, (ch-size[1])//2)
        self.photo = ImageTk.PhotoImage(image.resize(size, Image.Resampling.LANCZOS))
        self.canvas.delete("picture")
        self.canvas.delete("selection_box")
        self.canvas.create_image(*self.offset, image=self.photo, anchor="nw", tags="picture")
        self.canvas.tag_lower("picture")
        if self.selected_index is not None and self.selected_index < len(self.doc.data["marks"]):
            x1, y1, x2, y2 = self.mark_bounds(self.doc.data["marks"][self.selected_index])
            self.canvas.create_rectangle(self.offset[0]+x1*self.scale-4, self.offset[1]+y1*self.scale-4,
                                         self.offset[0]+x2*self.scale+4, self.offset[1]+y2*self.scale+4,
                                         outline="#00aaff", width=2, dash=(4, 3), tags="selection_box")
        if self.text_editor:
            self.position_editor()

    def mark_bounds(self, mark):
        x1, y1, x2, y2 = mark["xy"]
        kind = mark["kind"]
        if kind == "文字":
            draw = ImageDraw.Draw(self.doc.base)
            return draw.multiline_textbbox((x1, y1), mark["text"], font=font(mark.get("size", 22)))
        if kind == "序号":
            radius = max(12, mark.get("width", 3) * 4)
            return x1-radius, y1-radius, x1+radius, y1+radius
        if kind == "画笔" and mark.get("points"):
            xs = [point[0] for point in mark["points"]]
            ys = [point[1] for point in mark["points"]]
            return min(xs), min(ys), max(xs), max(ys)
        return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)

    def hit_test(self, x, y):
        for index in range(len(self.doc.data["marks"])-1, -1, -1):
            mark = self.doc.data["marks"][index]
            kind = mark["kind"]
            tolerance = max(8, mark.get("width", 3) + 5)
            if kind in {"箭头"}:
                x1, y1, x2, y2 = mark["xy"]
                if point_segment_distance(x, y, x1, y1, x2, y2) <= tolerance:
                    return index
            elif kind == "画笔":
                points = mark.get("points", [])
                if any(point_segment_distance(x, y, *first, *second) <= tolerance
                       for first, second in zip(points, points[1:])):
                    return index
            else:
                x1, y1, x2, y2 = self.mark_bounds(mark)
                if x1-tolerance <= x <= x2+tolerance and y1-tolerance <= y <= y2+tolerance:
                    return index
        return None

    def translate_mark(self, mark, dx, dy):
        mark["xy"] = [value + (dx if i % 2 == 0 else dy) for i, value in enumerate(mark["xy"])]
        if mark.get("points"):
            mark["points"] = [[x+dx, y+dy] for x, y in mark["points"]]

    def delete_selected(self):
        self.finish_text()
        if self.selected_index is not None and self.selected_index < len(self.doc.data["marks"]):
            self.checkpoint()
            self.doc.data["marks"].pop(self.selected_index)
            self.selected_index = None
            self.doc.save()
            self.redraw()
        return "break"

    def position_editor(self):
        if not self.text_editor_window:
            return
        x, y = self.text_position
        screen_x = round(self.canvas.winfo_rootx() + self.offset[0] + x*self.scale)
        screen_y = round(self.canvas.winfo_rooty() + self.offset[1] + y*self.scale)
        self.text_editor_window.geometry(f"360x100{screen_x:+d}{screen_y:+d}")

    def begin_text(self, x, y, index=None):
        self.finish_text()
        self.text_position = (x, y)
        self.text_index = index
        self.text_style = ({"color": self.color.get(), "size": self.text_size.get()} if index is None else
                           {k: self.doc.data["marks"][index].get(k, v) for k, v in (("color", "#ff3b30"), ("size", 22))})
        editor_window = self.text_editor_window = tk.Toplevel(self)
        editor_window.overrideredirect(True)
        editor_window.attributes("-topmost", True)
        if sys.platform == "win32":
            editor_window.attributes("-transparentcolor", self.text_transparent_color)
        editor = self.text_editor = tk.Text(editor_window, width=24, height=3, wrap="none",
                                           font=("Microsoft YaHei UI", -max(8, round(self.text_style["size"]*self.scale))),
                                           fg=self.text_style["color"], bg=self.text_transparent_color,
                                           insertbackground=self.text_style["color"], undo=True,
                                           borderwidth=0, highlightthickness=0, padx=0, pady=0)
        editor.pack(fill="both", expand=True)
        self.position_editor()
        if index is not None:
            editor.insert("1.0", self.doc.data["marks"][index]["text"])
        editor.bind("<Control-Return>", lambda event: self.finish_text())
        editor.bind("<Escape>", lambda event: self.finish_text(cancel=True))
        editor.bind("<Control-z>", lambda event: self.text_undo())
        editor.bind("<Control-s>", lambda event: self.finish_text())
        editor_window.bind("<Escape>", lambda event: self.finish_text(cancel=True))
        editor.focus_set()
        self.status.config(text="透明文本层 · Enter 换行 · Ctrl+Enter 完成 · Esc 取消 · 双击文字可修改")

    def text_undo(self):
        try:
            self.text_editor.edit_undo()
        except tk.TclError:
            pass
        return "break"

    def finish_text(self, cancel=False):
        if not self.text_editor:
            return "break"
        value = self.text_editor.get("1.0", "end-1c")
        if self.text_editor_window:
            self.text_editor_window.destroy()
        self.text_editor = None
        self.text_editor_window = None
        if not cancel:
            if value.strip() or self.text_index is not None:
                self.checkpoint()
            x, y = self.text_position
            mark = {"kind": "文字", "xy": [x, y, x, y], "text": value, **self.text_style}
            if self.text_index is not None:
                if value.strip():
                    self.doc.data["marks"][self.text_index] = mark
                else:
                    self.doc.data["marks"].pop(self.text_index)
            elif value.strip():
                self.doc.data["marks"].append(mark)
            self.doc.save()
        self.redraw()
        self.status.config(text="文字已保存" if not cancel else "已取消文字编辑")
        return "break"

    def edit_text(self, event):
        if self.compact:
            self.toggle_edit()
            return "break"
        self.finish_text()
        x, y = self.point(event)
        draw = ImageDraw.Draw(self.doc.base)
        for index in range(len(self.doc.data["marks"])-1, -1, -1):
            mark = self.doc.data["marks"][index]
            if mark["kind"] != "文字":
                continue
            mx, my = mark["xy"][:2]
            a, b, c, d = draw.multiline_textbbox((mx, my), mark["text"], font=font(mark.get("size", 22)))
            if a-5 <= x <= c+5 and b-5 <= y <= d+5:
                self.begin_text(mx, my, index)
                break
        return "break"

    def point(self, event):
        return [max(0, min(self.doc.base.width-1, (event.x-self.offset[0])/self.scale)),
                max(0, min(self.doc.base.height-1, (event.y-self.offset[1])/self.scale))]

    def press(self, event):
        self.finish_text()
        self.drag = (event.x_root, event.y_root, self.winfo_x(), self.winfo_y())
        if self.tool.get() == "移动":
            if not self.compact:
                x, y = self.point(event)
                self.selected_index = self.hit_test(x, y)
                self.move_origin = (x, y) if self.selected_index is not None else None
                self.move_original = (copy.deepcopy(self.doc.data["marks"][self.selected_index])
                                      if self.selected_index is not None else None)
                self.move_checkpointed = False
                self.redraw()
            return
        x, y = self.point(event)
        if self.tool.get() == "文字":
            self.begin_text(x, y)
            return
        if self.tool.get() == "序号":
            self.checkpoint()
            number = 1 + sum(mark["kind"] == "序号" for mark in self.doc.data["marks"])
            self.doc.data["marks"].append({"kind": "序号", "xy": [x, y, x, y], "number": number,
                                           "color": self.color.get(), "width": self.stroke.get()})
            self.doc.save()
            self.redraw()
            return
        self.draft = {"kind": self.tool.get(), "xy": [x, y, x, y], "color": self.color.get(), "width": self.stroke.get()}
        if self.tool.get() == "画笔":
            self.draft["points"] = [[x, y]]

    def motion(self, event):
        if self.tool.get() == "移动" and self.compact:
            sx, sy, wx, wy = self.drag
            self.geometry(f"{wx+event.x_root-sx:+d}{wy+event.y_root-sy:+d}")
        elif self.tool.get() == "移动" and self.selected_index is not None and self.move_origin:
            x, y = self.point(event)
            dx, dy = x-self.move_origin[0], y-self.move_origin[1]
            if not self.move_checkpointed and abs(dx)+abs(dy) > 0.5:
                self.checkpoint()
                self.move_checkpointed = True
            if self.move_checkpointed:
                moved = copy.deepcopy(self.move_original)
                self.translate_mark(moved, dx, dy)
                self.doc.data["marks"][self.selected_index] = moved
                self.redraw()
        elif self.draft:
            self.draft["xy"][2:] = self.point(event)
            if self.draft["kind"] == "画笔":
                self.draft["points"].append(self.point(event))
            self.redraw()

    def release(self, event):
        if self.tool.get() == "移动" and not self.compact:
            if self.move_checkpointed:
                self.doc.save()
            self.move_origin = None
            self.move_original = None
            self.move_checkpointed = False
            self.redraw()
            return
        if self.draft:
            self.draft["xy"][2:] = self.point(event)
            if self.draft["kind"] == "画笔":
                self.draft["points"].append(self.point(event))
            x1, y1, x2, y2 = self.draft["xy"]
            if abs(x2-x1)+abs(y2-y1) > 3 or len(self.draft.get("points", [])) > 2:
                self.checkpoint()
                self.doc.data["marks"].append(self.draft)
                self.doc.save()
            self.draft = None
            self.redraw()

    def undo(self):
        self.finish_text()
        if self.undo_stack:
            self.redo_stack.append(copy.deepcopy(self.doc.data["marks"]))
            self.doc.data["marks"] = self.undo_stack.pop()
            self.doc.save()
            self.redraw()

    def redo(self):
        self.finish_text()
        if self.redo_stack:
            self.undo_stack.append(copy.deepcopy(self.doc.data["marks"]))
            self.doc.data["marks"] = self.redo_stack.pop()
            self.doc.save()
            self.redraw()

    def export(self):
        self.finish_text()
        self.app.export_dir.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(parent=self, initialdir=str(self.app.export_dir),
                                            initialfile=datetime.now().strftime("pic-click_%Y%m%d_%H%M%S.png"),
                                            defaultextension=".png", filetypes=[("PNG 图片", "*.png")])
        if path:
            self.doc.image().save(path)
            self.status.config(text="已保存：" + path)

    def copy(self):
        self.finish_text()
        try:
            copy_image(self.doc.image())
            self.status.config(text="已复制含标注的图片，可粘贴到聊天或文档")
        except OSError as error:
            messagebox.showerror("复制失败", str(error), parent=self)

    def upload_cloud(self):
        self.finish_text()
        self.app.upload_document(self.doc, status_widget=self.status)

    def notes(self):
        if self.notes_window and self.notes_window.winfo_exists():
            self.notes_window.lift()
            return
        win = self.notes_window = tk.Toplevel(self)
        win.title("关联说明 · 自动保存")
        win.geometry("500x430")
        ttk.Label(win, text="文字解释（自动保存）", padding=8).pack(anchor="w")
        editor = tk.Text(win, height=8, wrap="word", undo=True)
        editor.pack(fill="both", expand=True, padx=8)
        editor.insert("1.0", self.doc.data["notes"])
        editor.edit_modified(False)
        def save_notes(event=None):
            if editor.edit_modified():
                self.doc.data["notes"] = editor.get("1.0", "end-1c")
                self.doc.save()
                editor.edit_modified(False)
        editor.bind("<<Modified>>", save_notes)
        ttk.Label(win, text="图片 / 视频附件（复制到截图资料夹，双击打开）", padding=8).pack(anchor="w")
        listing = tk.Listbox(win, height=6)
        listing.pack(fill="both", expand=True, padx=8)
        def refresh():
            listing.delete(0, "end")
            for item in self.doc.data["attachments"]:
                listing.insert("end", item["name"])
        def add():
            paths = filedialog.askopenfilenames(parent=win, filetypes=[("图片与视频", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.mp4 *.mov *.mkv *.avi *.webm"), ("所有文件", "*.*")])
            for path in paths:
                try:
                    self.doc.attach(path)
                except OSError as error:
                    messagebox.showerror("附件复制失败", str(error), parent=win)
            refresh()
        def open_item(event=None):
            if listing.curselection():
                item = self.doc.data["attachments"][listing.curselection()[0]]
                try:
                    os.startfile(str((self.doc.folder / item["path"]).resolve()))
                except OSError as error:
                    messagebox.showerror("无法打开附件", str(error), parent=win)
        listing.bind("<Double-Button-1>", open_item)
        row = ttk.Frame(win, padding=8)
        row.pack(fill="x")
        ttk.Button(row, text="添加本地附件", command=add).pack(side="left")
        ttk.Button(row, text="打开附件", command=open_item).pack(side="left", padx=5)
        ttk.Button(row, text="打开资料夹", command=lambda: os.startfile(str(self.doc.folder.resolve()))).pack(side="left")
        refresh()

    def close(self):
        self.finish_text()
        if self.capture_done:
            callback, self.capture_done = self.capture_done, None
            callback()
        self.app.closed_documents.append(self.doc.folder)
        self.app.closed_documents[:] = self.app.closed_documents[-10:]
        self.app.pins.remove(self)
        self.destroy()


class App:
    def __init__(self, root, data_dir, enable_tray=True):
        self.root = root
        local_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "pic-click"
        self.settings_file = local_root / "settings.json"
        default_capture_dir = str(Path(data_dir) if data_dir else local_root / "captures")
        default_export_dir = str(Path.home() / "Pictures" / "pic-click")
        self.settings = load_settings(self.settings_file, {
            "capture_dir": default_capture_dir,
            "export_dir": default_export_dir,
            "auto_save_png": False,
            "cloud": {
                "provider": "WebDAV", "endpoint": "", "username": "", "secret": "",
                "remote_folder": "pic-click", "public_base_url": "", "auto_upload": False,
            },
        })
        # A command-line data directory is an explicit per-run override.
        self.data_dir = Path(data_dir) if data_dir else Path(self.settings["capture_dir"])
        self.export_dir = Path(self.settings["export_dir"])
        self.auto_save_png = self.settings["auto_save_png"]
        self.cloud_settings = self.settings["cloud"]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pins = []
        self.closed_documents = []
        self.last_rect = None
        self.suggested_rect = None
        self.capture_mode = "normal"
        self.capture_target_hwnd = None
        self.tray = None
        self.capturing = False
        root.title("pic-click · 截图、标注、贴图")
        root.geometry("540x310")
        ttk.Label(root, text="pic-click", font=("Microsoft YaHei UI", 24, "bold"), padding=12).pack()
        ttk.Label(root, text="截图 → 标注 → 贴在屏幕上", font=("Microsoft YaHei UI", 13)).pack()
        ttk.Label(root, text="原屏幕保持清晰；矩形、箭头、高亮、文字\n每张截图可关联自己的文字、图片和视频", justify="center").pack()
        row = ttk.Frame(root, padding=15)
        row.pack()
        ttk.Button(row, text="开始框选截图", command=self.capture).pack(side="left", padx=5)
        ttk.Button(row, text="历史截图", command=self.history).pack(side="left", padx=5)
        ttk.Button(row, text="剪贴板贴图", command=self.paste).pack(side="left", padx=5)
        controls = ttk.Frame(root)
        controls.pack()
        ttk.Button(controls, text="显示 / 隐藏全部贴图", command=self.toggle_pins).pack(side="left", padx=4)
        ttk.Button(controls, text="保存设置", command=self.settings_dialog).pack(side="left", padx=4)
        ttk.Button(controls, text="云端设置", command=self.cloud_dialog).pack(side="left", padx=4)
        self.status = ttk.Label(root, text="截图时拖动鼠标框选，Esc 取消", padding=8)
        self.status.pack()
        ttk.Button(root, text="打开截图资料目录", command=lambda: os.startfile(str(self.data_dir.resolve()))).pack()
        root.bind("<Control-Shift-s>", lambda event: self.capture())
        self.hotkey = False
        self.extra_hotkeys = []
        if sys.platform == "win32":
            self.hotkey = bool(ctypes.windll.user32.RegisterHotKey(None, 1, 0x4000 | 0x0002 | 0x0004, 0x53))
            self.status.config(text="全局快捷键 Ctrl+Shift+S · Esc 取消框选" if self.hotkey else "全局快捷键被占用，请点击“开始框选截图”")
            root.after(100, self.poll_hotkey)
            for ident, key in ((2, 0x70), (3, 0x72), (4, 0x73), (5, 0x72)):
                modifiers = 0x4000 | (0x0004 if ident == 5 else 0)
                if ctypes.windll.user32.RegisterHotKey(None, ident, modifiers, key):
                    self.extra_hotkeys.append(ident)
            self.status.config(text="F1 截图 · F3 贴图 · Shift+F3 隐藏/显示 · F4 关闭穿透")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        if enable_tray:
            self.start_tray()

    def start_tray(self):
        try:
            import pystray
            icon_image = Image.new("RGBA", (64, 64), "#1976d2")
            draw = ImageDraw.Draw(icon_image)
            draw.rounded_rectangle((8, 8, 56, 56), 10, fill="white")
            draw.line((20, 22, 44, 22, 44, 42, 20, 42, 20, 22), fill="#1976d2", width=5)
            schedule = lambda action: (lambda icon=None, item=None: self.root.after(0, action))
            menu = pystray.Menu(
                pystray.MenuItem("截图（F1）", schedule(self.capture), default=True),
                pystray.MenuItem("剪贴板贴图（F3）", schedule(self.paste)),
                pystray.MenuItem("显示 / 隐藏贴图", schedule(self.toggle_pins)),
                pystray.MenuItem("打开主窗口", schedule(self.show_main)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出 pic-click", schedule(self.quit)),
            )
            self.tray = pystray.Icon("pic-click", icon_image, "pic-click", menu)
            self.tray.run_detached()
        except ImportError:
            self.status.config(text=self.status.cget("text") + " · 未安装 pystray，托盘不可用")

    def show_main(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def settings_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("pic-click · 保存设置")
        win.geometry("650x260")
        win.transient(self.root)
        win.attributes("-topmost", True)
        capture_value = tk.StringVar(value=str(self.data_dir))
        export_value = tk.StringVar(value=str(self.export_dir))
        auto_value = tk.BooleanVar(value=self.auto_save_png)

        def path_row(label, variable):
            row = ttk.Frame(win, padding=(12, 10, 12, 0))
            row.pack(fill="x")
            ttk.Label(row, text=label, width=16).pack(side="left")
            ttk.Entry(row, textvariable=variable).pack(side="left", fill="x", expand=True, padx=6)
            def choose():
                selected = filedialog.askdirectory(parent=win, initialdir=variable.get() or str(Path.home()))
                if selected:
                    variable.set(selected)
            ttk.Button(row, text="浏览…", command=choose).pack(side="left")

        path_row("截图工作资料目录", capture_value)
        path_row("默认 PNG 保存目录", export_value)
        ttk.Checkbutton(win, text="截图或剪贴板贴图创建后自动保存一份 PNG",
                        variable=auto_value).pack(anchor="w", padx=28, pady=14)
        ttk.Label(win, text="目录修改只影响之后创建的截图；已有截图仍保留在原目录。",
                  foreground="#666666").pack(anchor="w", padx=28)

        def apply():
            try:
                capture_dir = Path(os.path.expandvars(capture_value.get().strip())).expanduser()
                export_dir = Path(os.path.expandvars(export_value.get().strip())).expanduser()
                if not capture_value.get().strip() or not export_value.get().strip():
                    raise ValueError("保存目录不能为空")
                capture_dir.mkdir(parents=True, exist_ok=True)
                export_dir.mkdir(parents=True, exist_ok=True)
                self.data_dir, self.export_dir = capture_dir, export_dir
                self.auto_save_png = auto_value.get()
                self.settings = {
                    "capture_dir": str(capture_dir.resolve()),
                    "export_dir": str(export_dir.resolve()),
                    "auto_save_png": self.auto_save_png,
                    "cloud": self.cloud_settings,
                }
                save_settings(self.settings_file, self.settings)
            except (OSError, ValueError) as error:
                messagebox.showerror("设置无法保存", str(error), parent=win)
                return
            self.status.config(text=f"保存设置已更新 · PNG：{self.export_dir}")
            win.destroy()

        buttons = ttk.Frame(win, padding=12)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="保存设置", command=apply).pack(side="right")
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right", padx=8)
        win.grab_set()

    def cloud_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("pic-click · 云端上传设置")
        win.geometry("700x470")
        win.transient(self.root)
        win.attributes("-topmost", True)
        current = self.cloud_settings
        provider = tk.StringVar(value=current.get("provider", "WebDAV"))
        endpoint = tk.StringVar(value=current.get("endpoint", ""))
        username = tk.StringVar(value=current.get("username", ""))
        try:
            secret_value = unprotect_secret(current.get("secret", ""))
        except (OSError, ValueError):
            secret_value = ""
        secret = tk.StringVar(value=secret_value)
        remote_folder = tk.StringVar(value=current.get("remote_folder", "pic-click"))
        public_base_url = tk.StringVar(value=current.get("public_base_url", ""))
        auto_upload = tk.BooleanVar(value=current.get("auto_upload", False))
        status = ttk.Label(win, text="", foreground="#555555")

        def field(label, variable, password=False):
            row = ttk.Frame(win, padding=(14, 8, 14, 0))
            row.pack(fill="x")
            ttk.Label(row, text=label, width=18).pack(side="left")
            ttk.Entry(row, textvariable=variable, show="•" if password else "").pack(side="left", fill="x", expand=True)

        row = ttk.Frame(win, padding=(14, 12, 14, 0))
        row.pack(fill="x")
        ttk.Label(row, text="上传方式", width=18).pack(side="left")
        ttk.Combobox(row, textvariable=provider, values=("WebDAV", "HTTP API"), state="readonly").pack(side="left", fill="x", expand=True)
        field("服务 URL / API 地址", endpoint)
        field("账号 / 用户标识", username)
        field("密码 / API Token", secret, password=True)
        field("远端目录", remote_folder)
        field("公开链接基础 URL", public_base_url)
        ttk.Checkbutton(win, text="每次创建截图后自动上传", variable=auto_upload).pack(anchor="w", padx=32, pady=(14, 4))
        ttk.Label(win, text=("WebDAV 使用 PUT 与账号密码；HTTP API 使用 multipart POST，文件字段名为 file，"
                            "Token 通过 Bearer Authorization 发送。\n百度网盘需先在其开放平台完成 OAuth，"
                            "再把上传接口或中转服务填入 HTTP API。公开链接基础 URL 可不填。"),
                  foreground="#666666", wraplength=650, justify="left").pack(anchor="w", padx=32, pady=4)
        status.pack(anchor="w", padx=32, pady=5)

        def collect():
            address = endpoint.get().strip()
            parsed = urllib.parse.urlparse(address)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("服务 URL 必须是完整的 http:// 或 https:// 地址")
            return {
                "provider": provider.get(), "endpoint": address,
                "username": username.get().strip(), "secret": protect_secret(secret.get()),
                "remote_folder": remote_folder.get().strip(" /\\"),
                "public_base_url": public_base_url.get().strip(), "auto_upload": auto_upload.get(),
            }

        def save(close=True):
            try:
                config = collect()
            except (OSError, ValueError) as error:
                messagebox.showerror("云端设置无法保存", str(error), parent=win)
                return None
            self.cloud_settings = config
            self.settings["cloud"] = config
            save_settings(self.settings_file, self.settings)
            if close:
                self.status.config(text="云端上传设置已保存")
                win.destroy()
            return config

        def test_and_save():
            config = save(close=False)
            if not config:
                return
            sample = io.BytesIO()
            Image.new("RGB", (16, 16), "#1976d2").save(sample, "PNG")
            status.config(text="正在上传连接测试图片…")
            def complete(link, error):
                if error:
                    status.config(text="连接测试失败：" + str(error), foreground="#b00020")
                else:
                    status.config(text="连接测试成功：" + link, foreground="#087f23")
            self._upload_bytes(config, "pic-click-connection-test.png", sample.getvalue(), complete)

        buttons = ttk.Frame(win, padding=14)
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text="保存", command=save).pack(side="right")
        ttk.Button(buttons, text="测试并保存", command=test_and_save).pack(side="right", padx=8)
        ttk.Button(buttons, text="取消", command=win.destroy).pack(side="right")
        win.grab_set()

    def _upload_bytes(self, config, filename, payload, callback):
        def worker():
            try:
                link, error = upload_png(config, filename, payload), None
            except (OSError, ValueError, urllib.error.URLError) as caught:
                link, error = "", caught
            try:
                self.root.after(0, lambda: callback(link, error))
            except tk.TclError:
                pass
        threading.Thread(target=worker, name="pic-click-cloud-upload", daemon=True).start()

    def upload_document(self, document, status_widget=None, silent=False):
        config = dict(self.cloud_settings)
        if not config.get("endpoint"):
            if not silent:
                self.cloud_dialog()
            return
        stream = io.BytesIO()
        document.image().save(stream, "PNG")
        existing = document.data.get("auto_saved_png")
        filename = Path(existing).name if existing else datetime.now().strftime("pic-click_%Y%m%d_%H%M%S_%f.png")
        if status_widget:
            status_widget.config(text="正在上传云端…")
        elif not silent:
            self.status.config(text="正在上传云端…")

        def complete(link, error):
            if error:
                message = "云端上传失败：" + str(error)
                if status_widget:
                    status_widget.config(text=message)
                else:
                    self.status.config(text=message)
                if not silent:
                    messagebox.showerror("云端上传失败", str(error), parent=status_widget.winfo_toplevel() if status_widget else self.root)
                return
            document.data.setdefault("cloud_uploads", []).append({
                "provider": config.get("provider"), "url": link,
                "uploaded_at": datetime.now().isoformat(timespec="seconds"),
            })
            document.save()
            message = "已上传云端：" + link
            if status_widget:
                status_widget.config(text=message)
            else:
                self.status.config(text=message)
            if not silent:
                self.root.clipboard_clear()
                self.root.clipboard_append(link)
                messagebox.showinfo("上传完成", "图片链接已复制：\n" + link,
                                    parent=status_widget.winfo_toplevel() if status_widget else self.root)

        self._upload_bytes(config, filename, stream.getvalue(), complete)

    def create_document(self, image):
        document = Document(self.data_dir / uuid.uuid4().hex, image)
        self.last_auto_save = None
        if self.auto_save_png:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            target = self.export_dir / (datetime.now().strftime("pic-click_%Y%m%d_%H%M%S_%f")[:-3] + ".png")
            document.image().save(target)
            document.data["auto_saved_png"] = str(target.resolve())
            document.save()
            self.last_auto_save = target
        if self.cloud_settings.get("auto_upload") and self.cloud_settings.get("endpoint"):
            self.upload_document(document, silent=True)
        return document

    def poll_hotkey(self):
        msg = wintypes.MSG()
        while ctypes.windll.user32.PeekMessageW(ctypes.byref(msg), None, 0x0312, 0x0312, 1):
            if msg.wParam == 3:
                self.paste()
            elif msg.wParam == 4:
                self.restore_clicks()
            elif msg.wParam == 5:
                self.toggle_pins()
            else:
                self.capture()
        self.root.after(100, self.poll_hotkey)

    def quit(self):
        for pin in self.pins:
            pin.finish_text()
        if self.hotkey:
            ctypes.windll.user32.UnregisterHotKey(None, 1)
        for ident in self.extra_hotkeys:
            ctypes.windll.user32.UnregisterHotKey(None, ident)
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def toggle_pins(self):
        if self.capturing:
            return
        show = any(pin.state() == "withdrawn" for pin in self.pins)
        for pin in self.pins:
            pin.deiconify() if show else pin.withdraw()

    def restore_clicks(self):
        for pin in self.pins:
            if pin.click_through:
                pin.toggle_click_through()

    def open_pin(self, image):
        pin = Pin(self, self.create_document(image))
        pin.pin_desktop()
        return pin

    def paste(self):
        if self.capturing:
            return
        try:
            image = ImageGrab.grabclipboard()
            if isinstance(image, list):
                image_path = next((path for path in image if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}), None)
                image = Image.open(image_path).convert("RGB") if image_path else text_card("\n".join(image))
            if not isinstance(image, Image.Image):
                try:
                    value = self.root.clipboard_get()
                except tk.TclError:
                    value = ""
                if value.strip():
                    image = text_card(value)
                elif self.closed_documents:
                    folder = self.closed_documents.pop()
                    if Path(folder).exists():
                        pin = Pin(self, Document(folder))
                        pin.pin_desktop()
                        return
                else:
                    messagebox.showinfo("剪贴板贴图", "请先复制图片、文字、颜色值或文件路径，再按 F3。", parent=self.root)
                    return
            self.open_pin(image)
        except Exception as error:
            messagebox.showerror("贴图失败", str(error), parent=self.root)

    def capture_scroll(self):
        self.capture()

    def capture(self, mode="normal"):
        if self.capturing:
            return
        self.capture_mode = mode
        self.capturing = True
        if sys.platform == "win32":
            user = ctypes.windll.user32
            hwnd = user.GetForegroundWindow()
            self.capture_target_hwnd = hwnd
            rect = wintypes.RECT()
            if hwnd and hwnd != self.root.winfo_id() and user.GetWindowRect(hwnd, ctypes.byref(rect)):
                self.suggested_rect = (rect.left, rect.top, rect.right, rect.bottom)
        self.hidden = [self.root] + [p for p in self.pins if p.state() != "withdrawn"]
        self.hidden += [p.notes_window for p in self.pins if p.notes_window and p.notes_window.winfo_exists()]
        for win in self.hidden:
            win.withdraw()
        self.root.after(250, self.overlay)

    def begin_scroll_capture(self, rect, first_frame):
        """Watch a manually scrolled target and collect stable, overlapping viewports."""
        collector = ScrollFrameCollector(first_frame)
        state = {"active": True, "after": None}
        x1, y1, x2, y2 = rect
        user = ctypes.windll.user32
        center_x, center_y = (x1+x2)//2, (y1+y2)//2
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        # The window that was foreground before the capture overlay is the most
        # reliable scroll recipient. WindowFromPoint is only a fallback because
        # overlays can remain in DWM for a moment after being destroyed.
        target = self.capture_target_hwnd or user.WindowFromPoint(wintypes.POINT(center_x, center_y))
        if target:
            root_target = user.GetAncestor(target, 2)  # GA_ROOT: browser/document top-level window
            target = root_target or target

        panel = tk.Toplevel(self.root)
        panel.title("pic-click · 滚动截图预览")
        panel.attributes("-topmost", True)
        panel.resizable(False, False)
        virtual_left = user.GetSystemMetrics(76)
        virtual_top = user.GetSystemMetrics(77)
        virtual_width = user.GetSystemMetrics(78)
        virtual_height = user.GetSystemMetrics(79)
        panel_width = 350
        preview_height = max(180, min(360, virtual_height - 280))
        panel_height = preview_height + 205
        panel_x = x2 + 12 if x2 + panel_width + 12 <= virtual_left + virtual_width else max(virtual_left, x1 - panel_width - 12)
        panel_y = max(virtual_top, min(y1, virtual_top + virtual_height - panel_height))
        panel.geometry(f"{panel_width}x{panel_height}{panel_x:+d}{panel_y:+d}")
        ttk.Label(panel, text="实时拼接预览", font=("Microsoft YaHei UI", 12, "bold"),
                  padding=(12, 10, 12, 4)).pack(anchor="w")
        preview_shell = ttk.Frame(panel, padding=(10, 0, 10, 0))
        preview_shell.pack(fill="both", expand=True)
        preview_canvas = tk.Canvas(preview_shell, width=310, height=preview_height,
                                   bg="#15171b", highlightthickness=1, highlightbackground="#3b414d")
        preview_scroll = ttk.Scrollbar(preview_shell, orient="vertical", command=preview_canvas.yview)
        preview_canvas.configure(yscrollcommand=preview_scroll.set)
        preview_scroll.pack(side="right", fill="y")
        preview_canvas.pack(side="left", fill="both", expand=True)
        preview_photo = [None]
        preview_size = ttk.Label(panel, text="", foreground="#666666", padding=(12, 3, 12, 0))
        preview_size.pack(anchor="w")
        status = ttk.Label(panel, text="已采集 1 帧 · 请在原页面内手动向下滚动",
                           justify="left", padding=(12, 5, 12, 2))
        status.pack(fill="x")
        ttk.Label(panel, text="预览会自动跟随到底部；每次滚动请保留部分上一屏内容",
                  foreground="#666666", padding=(12, 0, 12, 2)).pack(anchor="w")
        buttons = ttk.Frame(panel, padding=(10, 4, 10, 10))
        buttons.pack(fill="x")
        panel.update_idletasks()
        # Keep the controller out of captures when Windows supports display affinity.
        user.SetWindowDisplayAffinity(wintypes.HWND(panel.winfo_id()), 0x00000011)

        def activate_target():
            if target:
                user.SetForegroundWindow(target)
            user.SetCursorPos(center_x, center_y)

        activate_target()

        def read_frame():
            desktop = ImageGrab.grab(all_screens=True)
            return desktop.crop((x1-virtual_left, y1-virtual_top,
                                 x2-virtual_left, y2-virtual_top)).convert("RGB")

        def refresh_preview():
            image = collector.image()
            canvas_width = max(80, preview_canvas.winfo_width() - 12)
            scale = min(1.0, canvas_width / image.width)
            size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            preview_photo[0] = ImageTk.PhotoImage(image.resize(size, Image.Resampling.LANCZOS))
            preview_canvas.delete("all")
            preview_canvas.create_image(max(0, (canvas_width-size[0])//2), 4,
                                        image=preview_photo[0], anchor="nw")
            preview_canvas.configure(scrollregion=(0, 0, canvas_width, size[1] + 8))
            preview_canvas.yview_moveto(1.0)
            preview_size.config(text=f"当前结果：{image.width} × {image.height} · {len(collector.frames)} 帧")

        def show_result(result):
            count = len(collector.frames)
            if result == "captured":
                added = collector.parts[-1].height
                status.config(text=f"已采集 {count} 帧，本次新增 {added} 像素 · 可继续滚动")
                refresh_preview()
            elif result == "no_overlap":
                status.config(text=f"已采集 {count} 帧 · 滚动过快，请向回滚一点")
            elif result == "unchanged":
                status.config(text=f"已采集 {count} 帧 · 等待页面滚动，相同画面不会重复采集")

        def collect_now():
            try:
                show_result(collector.add(read_frame()))
                activate_target()
            except Exception as error:
                finish_scroll(cancelled=True)
                messagebox.showerror("滚动截图失败", str(error), parent=self.root)

        def finish_scroll(cancelled=False):
            if not state["active"]:
                return
            state["active"] = False
            if state["after"]:
                try:
                    self.root.after_cancel(state["after"])
                except tk.TclError:
                    pass
            if panel.winfo_exists():
                panel.destroy()
            self.restore()
            if cancelled:
                self.status.config(text="滚动截图已取消")
                return
            image = collector.image()
            pin = Pin(self, self.create_document(image))
            saved = f" · 已自动保存：{self.last_auto_save}" if self.last_auto_save else ""
            pin.status.config(text=f"滚动截图完成：{len(collector.frames)} 帧，{image.width} × {image.height}{saved} · 可继续标注")
            pin.lift()
            pin.focus_force()

        def watch_manual_scroll():
            if not state["active"]:
                return
            if user.GetAsyncKeyState(0x1B) & 0x8000:
                finish_scroll()
                return
            try:
                result = collector.observe(read_frame())
            except Exception as error:
                finish_scroll(cancelled=True)
                messagebox.showerror("滚动截图失败", str(error), parent=self.root)
                return
            show_result(result)
            if len(collector.frames) >= 50:
                finish_scroll()
                return
            state["after"] = self.root.after(250, watch_manual_scroll)

        ttk.Button(buttons, text="捕获当前画面", command=collect_now).pack(side="left")
        ttk.Button(buttons, text="取消", command=lambda: finish_scroll(cancelled=True)).pack(side="right")
        ttk.Button(buttons, text="完成", command=finish_scroll).pack(side="right", padx=5)
        panel.protocol("WM_DELETE_WINDOW", finish_scroll)
        refresh_preview()
        state["after"] = self.root.after(350, watch_manual_scroll)

    def restore(self):
        for win in self.hidden:
            if win.winfo_exists():
                win.deiconify()
        self.capturing = False

    def overlay(self):
        try:
            desktop = ImageGrab.grab(all_screens=True)
            user = ctypes.windll.user32
            left, top = user.GetSystemMetrics(76), user.GetSystemMetrics(77)
            overlay = tk.Toplevel(self.root)
            overlay.overrideredirect(True)
            overlay.attributes("-topmost", True)
            overlay.geometry(f"{desktop.width}x{desktop.height}{left:+d}{top:+d}")
            # SetWindowPos supports negative virtual desktop coordinates reliably.
            overlay.update_idletasks()
            user.SetWindowPos(wintypes.HWND(overlay.winfo_id()), None, left, top, desktop.width, desktop.height, 0x0014)
            canvas = tk.Canvas(overlay, highlightthickness=0, cursor="crosshair")
            canvas.pack(fill="both", expand=True)
            photo = ImageTk.PhotoImage(desktop)
            canvas.photo = photo
            canvas.create_image(0, 0, image=photo, anchor="nw")
            guide_text = "拖动框选区域 · 松开后在底部工具栏选择完成、滚动截图、复制或贴图"
            canvas.create_text(desktop.width//2, 24, text=guide_text,
                               fill="white", font=("Microsoft YaHei UI", 12, "bold"), tags="guide")
            canvas.create_rectangle(desktop.width//2-330, 5, desktop.width//2+330, 43,
                                    fill="#202124", outline="#202124", stipple="gray50", tags="guide_bg")
            canvas.tag_lower("guide_bg", "guide")
            start = []
            selection = [0, 0, desktop.width, desktop.height]
            if self.suggested_rect:
                l, t, r, b = self.suggested_rect
                selection[:] = [max(0, l-left), max(0, t-top), min(desktop.width, r-left), min(desktop.height, b-top)]
            rectangle = canvas.create_rectangle(*selection, outline="#00aaff", width=2, tags="selection")
            size_label = canvas.create_text(selection[0]+4, max(12, selection[1]-12), text="", anchor="w", fill="#00aaff", font=("Segoe UI", 10, "bold"))
            current_color = ["#000000"]
            accepted = [False]
            dock = [None]
            dock_window = [None]
            def show_selection():
                canvas.coords(rectangle, *selection)
                canvas.coords(size_label, selection[0]+4, max(12, selection[1]-12))
                canvas.itemconfigure(size_label, text=f"{selection[2]-selection[0]} × {selection[3]-selection[1]}")
            show_selection()
            def finish(event=None):
                if overlay.winfo_exists():
                    try:
                        overlay.grab_release()
                    except tk.TclError:
                        pass
                    overlay.destroy()
                self.restore()
            def press(event):
                if dock_window[0] is not None:
                    canvas.itemconfigure(dock_window[0], state="hidden")
                start[:] = [event.x, event.y]
                selection[:] = [event.x, event.y, event.x, event.y]
                show_selection()
            def move(event):
                if start:
                    selection[:] = [start[0], start[1], max(0, min(desktop.width, event.x)), max(0, min(desktop.height, event.y))]
                    show_selection()
            def magnify(event):
                x, y = max(0, min(desktop.width-1, event.x)), max(0, min(desktop.height-1, event.y))
                pixel = desktop.getpixel((x, y))[:3]
                current_color[0] = "#%02X%02X%02X" % pixel
                l, t = max(0, x-7), max(0, y-7)
                crop = desktop.crop((l, t, min(desktop.width, l+15), min(desktop.height, t+15))).resize((150, 150), Image.Resampling.NEAREST)
                canvas.magnifier = ImageTk.PhotoImage(crop)
                px = x+24 if x+185 < desktop.width else x-174
                py = y+24 if y+205 < desktop.height else y-204
                canvas.delete("magnifier")
                canvas.create_rectangle(px-2, py-2, px+152, py+180, fill="#202124", outline="white", tags="magnifier")
                canvas.create_image(px, py, image=canvas.magnifier, anchor="nw", tags="magnifier")
                canvas.create_line(px+75, py, px+75, py+150, fill="#ff3b30", tags="magnifier")
                canvas.create_line(px, py+75, px+150, py+75, fill="#ff3b30", tags="magnifier")
                canvas.create_text(px+75, py+165, text=f"{current_color[0]}  ({x+left}, {y+top})", fill="white", tags="magnifier")
            def copy_color(event=None):
                self.root.clipboard_clear()
                self.root.clipboard_append(current_color[0])
                canvas.itemconfigure("guide", text=f"已复制颜色 {current_color[0]} · F3 可贴成颜色卡")
            def accept(pin_direct=False, copy_direct=False, scroll_capture=False):
                if accepted[0]:
                    return
                x1, x2 = sorted((selection[0], selection[2]))
                y1, y2 = sorted((selection[1], selection[3]))
                if x2-x1 < 4 or y2-y1 < 4:
                    return
                accepted[0] = True
                self.last_rect = tuple(selection)
                crop = desktop.crop((x1, y1, x2, y2))
                if scroll_capture:
                    try:
                        overlay.grab_release()
                    except tk.TclError:
                        pass
                    overlay.destroy()
                    self.begin_scroll_capture((left+x1, top+y1, left+x2, top+y2), crop)
                    return
                if copy_direct:
                    copy_image(crop)
                    finish()
                    return
                finish()
                pin = Pin(self, self.create_document(crop))
                pin.geometry(f"{max(680, crop.width)}x{min(crop.height+170, pin.winfo_screenheight()-80)}{left+x1:+d}{top+y1:+d}")
                saved = f" · 已自动保存：{self.last_auto_save}" if self.last_auto_save else ""
                pin.status.config(text=f"框选完成{saved} · 直接标注，然后点击“贴到桌面”")
                pin.lift()
                pin.focus_force()
                if pin_direct:
                    pin.pin_desktop()
            def show_dock():
                if dock[0] is None:
                    bar = dock[0] = tk.Frame(canvas, bg="#252932", bd=0, padx=6, pady=5)
                    buttons = (
                        ("✓  完成", lambda: accept()),
                        ("↕  滚动截图", lambda: accept(scroll_capture=True)),
                        ("▣  复制", lambda: accept(copy_direct=True)),
                        ("▤  贴图", lambda: accept(pin_direct=True)),
                        ("×  取消", finish),
                    )
                    for label, command in buttons:
                        tk.Button(bar, text=label, command=command, bg="#252932", fg="#f1f3f4",
                                  activebackground="#3b414d", activeforeground="white", relief="flat",
                                  bd=0, padx=10, pady=5, font=("Microsoft YaHei UI", 10)).pack(side="left")
                    bar.update_idletasks()
                    dock_window[0] = canvas.create_window(0, 0, window=bar, anchor="n", tags="capture_dock")
                width = dock[0].winfo_reqwidth()
                height = dock[0].winfo_reqheight()
                center = max(width//2 + 8, min(desktop.width-width//2-8, (selection[0]+selection[2])//2))
                below = max(selection[1], selection[3]) + 8
                y = below if below + height < desktop.height-8 else max(8, min(selection[1], selection[3])-height-8)
                canvas.coords(dock_window[0], center, y)
                canvas.itemconfigure(dock_window[0], state="normal")
            def release(event):
                if not start:
                    return
                move(event)
                start.clear()
                x1, x2 = sorted((selection[0], selection[2]))
                y1, y2 = sorted((selection[1], selection[3]))
                if x2-x1 >= 4 and y2-y1 >= 4:
                    show_dock()
            def adjust(event):
                key = event.keysym
                dx = -1 if key == "Left" else 1 if key == "Right" else 0
                dy = -1 if key == "Up" else 1 if key == "Down" else 0
                if event.state & 4:
                    selection[2] = max(selection[0]+4, min(desktop.width, selection[2]+dx))
                    selection[3] = max(selection[1]+4, min(desktop.height, selection[3]+dy))
                elif event.state & 1:
                    selection[2] = max(selection[0]+4, min(desktop.width, selection[2]-dx))
                    selection[3] = max(selection[1]+4, min(desktop.height, selection[3]-dy))
                else:
                    selection[:] = [v + (dx if i % 2 == 0 else dy) for i, v in enumerate(selection)]
                show_selection()
                return "break"
            canvas.bind("<ButtonPress-1>", press)
            canvas.bind("<B1-Motion>", move)
            canvas.bind("<ButtonRelease-1>", release)
            canvas.bind("<Motion>", magnify)
            canvas.bind("<Button-2>", lambda event: accept(pin_direct=True))
            overlay.bind("<Escape>", finish)
            overlay.bind("<Return>", lambda event: accept())
            overlay.bind("<Control-c>", lambda event: accept(copy_direct=True))
            overlay.bind("<Control-t>", lambda event: accept(pin_direct=True))
            overlay.bind("<Control-a>", lambda event: (selection.__setitem__(slice(None), [0, 0, desktop.width, desktop.height]), show_selection()))
            overlay.bind("r", lambda event: (selection.__setitem__(slice(None), list(self.last_rect or selection)), show_selection()))
            overlay.bind("c", copy_color)
            for key in ("<Left>", "<Right>", "<Up>", "<Down>"):
                overlay.bind(key, adjust)
            overlay.focus_force()
            overlay.grab_set()
        except Exception as error:
            self.restore()
            messagebox.showerror("截图失败", "请在已登录、未锁屏的 Windows 桌面运行；远程会话需要保持连接。\n\n系统返回：" + str(error), parent=self.root)

    def history(self):
        win = tk.Toplevel(self.root)
        win.title("历史截图 · 双击打开")
        win.geometry("480x320")
        listing = tk.Listbox(win)
        listing.pack(fill="both", expand=True, padx=8, pady=8)
        files = sorted(self.data_dir.glob("*/metadata.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        from datetime import datetime
        for path in files:
            listing.insert("end", datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") + "  " + path.parent.name[:8])
        def open_doc(event=None):
            if listing.curselection():
                try:
                    Pin(self, Document(files[listing.curselection()[0]].parent))
                except Exception as error:
                    messagebox.showerror("读取失败", str(error), parent=win)
        listing.bind("<Double-Button-1>", open_doc)
        ttk.Button(win, text="打开所选截图", command=open_doc).pack(pady=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=None, help="本次运行覆盖截图工作资料目录")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()
    setup_dpi()
    root = tk.Tk()
    app = App(root, args.data_dir, enable_tray=not args.smoke_test)
    if args.smoke_test:
        doc = Document(Path(args.data_dir or app.data_dir) / "smoke", Image.new("RGB", (640, 320), "white"))
        doc.data["marks"] = [{"kind": "高亮", "xy": [20, 40, 300, 75]}, {"kind": "文字", "xy": [20, 40, 20, 40], "text": "重点说明 / pic-click"}]
        doc.save()
        pin = Pin(app, doc)
        pin.notes()
        root.after(700, lambda: (root.update_idletasks(), print("SMOKE_OK: Tk, pin, notes and image rendering", flush=True), app.quit()))
    root.mainloop()


if __name__ == "__main__":
    main()
