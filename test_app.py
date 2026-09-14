import json
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import struct
import unittest

from PIL import Image
from app import (Document, MjpegAviWriter, ScrollFrameCollector, cloud_target_url, image_difference,
                 load_settings, multipart_body, protect_secret, render, save_settings,
                 stitch_scroll_frames, text_card, unprotect_secret, upload_png)


class DocumentTests(unittest.TestCase):
    def test_mjpeg_avi_writer_builds_indexed_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "recording.avi"
            with MjpegAviWriter(target, (160, 90), fps=10, quality=70) as writer:
                for color in ("#ff0000", "#00ff00", "#0000ff"):
                    writer.write(Image.new("RGB", (160, 90), color))
            payload = target.read_bytes()
            self.assertEqual(payload[:4], b"RIFF")
            self.assertEqual(payload[8:12], b"AVI ")
            self.assertIn(b"MJPG", payload)
            self.assertIn(b"idx1", payload)
            avih = payload.index(b"avih") + 8
            self.assertEqual(struct.unpack_from("<I", payload, avih + 16)[0], 3)

    def test_cloud_secret_is_protected_and_roundtrips(self):
        protected = protect_secret("cloud-token-测试")
        self.assertNotIn("cloud-token", protected)
        self.assertEqual(unprotect_secret(protected), "cloud-token-测试")

    def test_cloud_url_and_multipart_encoding(self):
        target = cloud_target_url({"endpoint": "https://dav.example/root", "remote_folder": "截图/2026"}, "图 1.png")
        self.assertEqual(target, "https://dav.example/root/%E6%88%AA%E5%9B%BE/2026/%E5%9B%BE%201.png")
        body = multipart_body("file", "capture.png", b"PNGDATA", "boundary")
        self.assertIn(b'name="file"', body)
        self.assertIn(b"PNGDATA", body)

    def test_custom_http_api_upload_returns_link_and_authenticates(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received["authorization"] = self.headers.get("Authorization")
                received["user"] = self.headers.get("X-Pic-Click-User")
                received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.dumps({"url": "https://cloud.example/capture.png"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            config = {"provider": "HTTP API", "endpoint": f"http://127.0.0.1:{server.server_port}/upload",
                      "username": "tester", "secret": protect_secret("token-value"),
                      "remote_folder": "", "public_base_url": ""}
            link = upload_png(config, "capture.png", b"PNGDATA")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertEqual(link, "https://cloud.example/capture.png")
        self.assertEqual(received["authorization"], "Bearer token-value")
        self.assertEqual(received["user"], "tester")
        self.assertIn(b"PNGDATA", received["body"])

    def test_webdav_creates_folder_then_uploads_png(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def _record(self, method):
                requests.append((method, self.path, self.headers.get("Authorization")))
                if method == "PUT":
                    self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_MKCOL(self):
                self._record("MKCOL")

            def do_PUT(self):
                self._record("PUT")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_port}/dav"
            config = {"provider": "WebDAV", "endpoint": endpoint, "username": "user",
                      "secret": protect_secret("pass"), "remote_folder": "pic-click/2026",
                      "public_base_url": "https://files.example/"}
            link = upload_png(config, "图 1.png", b"PNGDATA")
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertEqual(link, "https://files.example/pic-click/2026/%E5%9B%BE%201.png")
        self.assertEqual([item[0] for item in requests], ["MKCOL", "MKCOL", "PUT"])
        self.assertEqual(requests[-1][1], "/dav/pic-click/2026/%E5%9B%BE%201.png")
        self.assertEqual(requests[-1][2], "Basic " + base64.b64encode(b"user:pass").decode())

    def test_customer_save_settings_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            defaults = {"capture_dir": "captures", "export_dir": "exports", "auto_save_png": False}
            self.assertEqual(load_settings(path, defaults), defaults)
            configured = {"capture_dir": "D:/captures", "export_dir": "D:/exports", "auto_save_png": True}
            save_settings(path, configured)
            self.assertEqual(load_settings(path, defaults), configured)

    def test_clipboard_text_and_color_cards(self):
        text = text_card("一段可以贴到屏幕上的文字")
        color = text_card("#123456")
        self.assertGreaterEqual(text.width, 260)
        self.assertGreaterEqual(text.height, 120)
        self.assertEqual(color.getpixel((0, 0)), (18, 52, 86))

    def test_scroll_frames_are_stitched_without_duplicate_overlap(self):
        tall = Image.new("RGB", (200, 1200), "white")
        from PIL import ImageDraw
        draw = ImageDraw.Draw(tall)
        for y in range(0, 1200, 40):
            draw.rectangle((0, y, 199, y+39), fill=((y*3) % 255, (y*7) % 255, (y*11) % 255))
            draw.text((10, y+8), str(y), fill="black")
        frames = [tall.crop((0, y, 200, y+400)) for y in (0, 300, 600, 800)]
        result = stitch_scroll_frames(frames)
        self.assertEqual(result.size, tall.size)
        self.assertEqual(image_difference(result, tall), 0)

    def test_scroll_stitch_rejects_empty_input(self):
        with self.assertRaises(ValueError):
            stitch_scroll_frames([])

    def test_manual_scroll_collector_waits_for_stability_and_overlap(self):
        tall = Image.new("RGB", (200, 1000), "white")
        from PIL import ImageDraw
        draw = ImageDraw.Draw(tall)
        for y in range(0, 1000, 25):
            draw.rectangle((0, y, 199, y+24), fill=((y*5) % 255, (y*9) % 255, (y*13) % 255))
            draw.text((8, y+4), f"row-{y}", fill="black")
        frames = [tall.crop((0, y, 200, y+400)) for y in (0, 300, 600)]
        collector = ScrollFrameCollector(frames[0])
        self.assertEqual(collector.observe(frames[1]), "settling")
        self.assertEqual(collector.observe(frames[1]), "captured")
        self.assertEqual(collector.observe(frames[2]), "settling")
        self.assertEqual(collector.observe(frames[2]), "captured")
        self.assertEqual(collector.image().size, (200, 1000))
        self.assertEqual(image_difference(collector.image(), tall), 0)

    def test_manual_scroll_collector_rejects_frame_without_overlap(self):
        collector = ScrollFrameCollector(Image.new("RGB", (160, 300), "#ff0000"))
        self.assertEqual(collector.add(Image.new("RGB", (160, 300), "#0000ff")), "no_overlap")
        self.assertEqual(len(collector.frames), 1)

    def test_manual_scroll_collector_never_appends_same_page_with_dynamic_header(self):
        from PIL import ImageDraw
        page = Image.new("RGB", (240, 400), "white")
        draw = ImageDraw.Draw(page)
        for y in range(0, 400, 32):
            draw.text((12, y + 6), f"content-row-{y}", fill="black")
        changed = page.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 239, 45), fill="#1683ff")
        collector = ScrollFrameCollector(page)
        self.assertEqual(collector.observe(changed), "settling")
        self.assertEqual(collector.observe(changed), "unchanged")
        self.assertEqual(len(collector.frames), 1)
        self.assertEqual(collector.image().size, page.size)

    def test_manual_scroll_collector_only_appends_new_bottom_strip(self):
        tall = Image.new("RGB", (180, 760), "white")
        from PIL import ImageDraw
        draw = ImageDraw.Draw(tall)
        for y in range(0, 760, 20):
            draw.rectangle((0, y, 179, y + 19), fill=((y * 3) % 255, (y * 7) % 255, (y * 11) % 255))
            draw.text((5, y + 2), str(y), fill="black")
        first = tall.crop((0, 0, 180, 400))
        second = tall.crop((0, 240, 180, 640))
        collector = ScrollFrameCollector(first)
        self.assertEqual(collector.add(second), "captured")
        self.assertEqual(collector.parts[-1].height, 240)
        self.assertEqual(collector.image().size, (180, 640))
        self.assertEqual(image_difference(collector.image(), tall.crop((0, 0, 180, 640))), 0)

    def test_wide_sparse_webpage_uses_pixel_exact_seams(self):
        from PIL import ImageDraw
        page = Image.new("RGB", (1743, 1176), "#f7f8fa")
        draw = ImageDraw.Draw(page)
        draw.text((50, 35), "Tool Category", fill="#5b6880")
        draw.text((50, 120), "TIME TOOLS", fill="#111827")
        draw.rectangle((50, 275, 1690, 625), fill="white")
        draw.rectangle((305, 355, 805, 540), fill="#20965d")
        for row_y in (752, 956):
            for column_x in (50, 650, 1250):
                draw.rounded_rectangle((column_x, row_y, column_x + 584, row_y + 188), 12,
                                       fill="white", outline="#d9dee7", width=2)
                draw.text((column_x + 24, row_y + 35), f"card-{column_x}-{row_y}", fill="#172033")
        frames = [page.crop((0, y, page.width, y + 700)) for y in (0, 350, 476)]
        collector = ScrollFrameCollector(frames[0])
        self.assertEqual(collector.add(frames[1]), "captured")
        self.assertEqual(collector.add(frames[2]), "captured")
        self.assertEqual(collector.cuts, [350, 574])
        self.assertEqual(collector.image().size, page.size)
        self.assertEqual(image_difference(collector.image(), page), 0)

    def test_annotations_preserve_original_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Document(Path(tmp) / "capture", Image.new("RGB", (200, 100), "white"))
            doc.data["marks"] = [
                {"kind": "高亮", "xy": [80, 40, 10, 10]},
                {"kind": "矩形", "xy": [100, 10, 180, 60]},
                {"kind": "箭头", "xy": [10, 80, 80, 80]},
                {"kind": "文字", "xy": [90, 65, 90, 65], "text": "重点"},
            ]
            doc.data["notes"] = "解释：第 2 行"
            doc.save()
            loaded = Document(doc.folder)
            self.assertEqual(loaded.data, doc.data)
            self.assertEqual(loaded.base.getpixel((20, 20)), (255, 255, 255))
            self.assertNotEqual(loaded.image().getpixel((20, 20)), (255, 255, 255))
            self.assertEqual(loaded.image().getpixel((199, 99)), (255, 255, 255))
            self.assertEqual(render(loaded.base, []).tobytes(), loaded.base.tobytes())

    def test_attachment_is_independent_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "说明.png"
            Image.new("RGB", (8, 8), "red").save(source)
            doc = Document(Path(tmp) / "capture", Image.new("RGB", (20, 20)))
            doc.attach(source)
            source.unlink()
            loaded = Document(doc.folder)
            attachment = loaded.folder / loaded.data["attachments"][0]["path"]
            self.assertTrue(attachment.exists())
            with Image.open(attachment) as image:
                self.assertEqual(image.size, (8, 8))

    def test_auto_saved_png_tracks_later_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Document(Path(tmp) / "capture", Image.new("RGB", (80, 50), "white"))
            target = Path(tmp) / "exports" / "capture.png"
            doc.data["auto_saved_png"] = str(target)
            doc.save()
            with Image.open(target) as initial:
                before = initial.convert("RGB")
            doc.data["marks"].append({"kind": "矩形", "xy": [5, 5, 60, 35]})
            doc.save()
            with Image.open(target) as saved:
                self.assertGreater(image_difference(before, saved.convert("RGB")), 0)
            self.assertIsNone(doc.auto_save_error)


if __name__ == "__main__":
    unittest.main()
