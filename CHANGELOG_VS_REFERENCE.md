# Changelog vs `reference/` (SUSYUSTC/MathTranslate gốc)

Mọi thay đổi trong `mathtranslate_v2/` so với mã nguồn chuẩn `reference/` đều được ghi lại tại đây kèm lý do cụ thể.

---

## Milestone 1 — Bản chạy tối thiểu (Minimum Runnable Core + CLI)

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/` | Lý do |
|---|---|---|
| `pyproject.toml` | Thay thế `setup.py` bằng cấu hình PEP 621 `pyproject.toml` | `reference/setup.py` thiếu `import os` gây lỗi `NameError` khi build cài đặt trên Python 3.12+/uv. |
| `mathtranslate/translate.py` | Thêm `self.translator = translator` trong `TextTranslator.__init__` cho engine `google` | Sửa bug crash `AttributeError` trong repo gốc (`import mtranslate as translator` nhưng gọi `self.translator.translate(...)`). |
| `mathtranslate/config.py` | Đổi `default_language_to_default` từ `'zh-CN'` sang `'vi'`; dùng `with open(...)` + `os.path.join` | Đặt tiếng Việt làm ngôn ngữ đích mặc định (vẫn giữ đủ 60+ ngôn ngữ); đóng file handle chuẩn trên Windows. |
| `mathtranslate/cache.py` | Bọc các lệnh `open(...)` bằng `with open(...)` | Tránh rò rỉ file descriptor trên Windows khi đọc/ghi cache đa luồng. |
| `mathtranslate/utils.py` | Đổi mặc định `check_update(require_updated=False)` và bọc `try/except Exception` khi gọi PyPI | Tránh crash CLI khi chạy offline hoặc bị chặn truy cập PyPI. |
| `mathtranslate/google.py` | Bọc `ctypes.CDLL("libc.so.6")` bằng điều kiện `if sys.platform != "win32"` và kiểm tra `libc is None` | Giữ lại module theo yêu cầu nhưng tránh lỗi `OSError` khi import trên Windows. |
| `mathtranslate/upload_overleaf.py` | Đưa đoạn code mở Chrome ở top-level vào hàm `upload_to_overleaf()` và `if __name__ == "__main__":` | Giữ nguyên tính năng nhưng tránh tự động khởi chạy Chrome nếu có module nào import file này. |
| `tests/test_m1_smoke.py` | Thêm bộ kiểm thử tự động bằng `pytest` | Đảm bảo Milestone 1 chạy được và kiểm thử được độc lập trên Windows. |

---

## Milestone 2 — Ổn định lõi LaTeX, Bảo toàn Format & ArXiv/ZIP an toàn

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/` | Lý do |
|---|---|---|
| `mathtranslate/process_latex.py` | Dùng raw string `r"""..."""`, `r'\item'`; đưa `format_list` (`textbf`, `textit`, `emph`) vào `command_list` | Loại bỏ `SyntaxWarning` trên Python 3.12+; dịch nội dung bên trong mà vẫn giữ nguyên thẻ in đậm/nghiêng. |
| `mathtranslate/translate.py` | Bỏ `delete_specific_format`; thêm exponential backoff (tối đa 3 lần: `0.5s`, `1.0s`) cho lỗi mạng tạm thời; ghi file `.tmp` + `os.replace` | Bảo toàn `\textbf`/`\textit`/`\emph`; chống lỗi mạng chập chờn và chống hỏng file output nếu crash giữa chừng. |
| `mathtranslate/process_text.py` | Bổ sung `_split_long_paragraph` fallback theo `. `, `, `, khoảng trắng, hoặc cắt cứng `char_limit - 20` | Sửa lỗi đệ quy vô hạn (`RecursionError`) khi đoạn > 2000 ký tự không có dấu `.` viết hoa sau đó. |
| `mathtranslate/utils.py` | Thay `exec(content, var)` bằng `ast.parse` + `ast.literal_eval` và hỗ trợ JSON cho `-commands` | Loại bỏ lỗ hổng thực thi mã tùy ý nhưng vẫn tương thích định dạng `additional_commands = [...]` cũ. |
| `mathtranslate/process_file.py` & `translate_arxiv.py` | Bỏ `os.chdir(temp_dir)`; thêm `safe_extract_zip` & `safe_extract_tar` chống Zip-Slip/Tar-Slip; `zipdir` atomic; thêm `translate_zip_project` | An toàn đa luồng cho Web/GUI, chống path traversal khi giải nén, tái sử dụng dịch `.zip` nhiều file cho cả 3 giao diện. |

---

## Milestone 3 — Pluggable Translation Engines

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/` | Lý do |
|---|---|---|
| `mathtranslate/engines/` (`base.py`, `google_engine.py`, `tencent_engine.py`, `openai_compat.py`, `gemini_engine.py`, `deepl_engine.py`, `__init__.py`) | Tách kiến trúc engine thành `BaseTranslator` + factory `create_engine` hỗ trợ 6 engine (`google`, `tencent`, `gemini`, `openai`, `ollama`, `deepl`) | Dễ mở rộng, tránh hardcode chỉ 2 engine trong `translate.py`. |
| `mathtranslate/engines/google_engine.py` | Ưu tiên endpoint `translate.googleapis.com/translate_a/single?client=gtx` qua `requests`, fallback sang `mtranslate` | Khắc phục lỗi `HTTP 429 Too Many Requests` gặp phải khi dùng endpoint `/m` cũ của `mtranslate`. |
| `mathtranslate/translate.py` & `utils.py` | `TextTranslator` gọi `create_engine(engine)`; cập nhật `-engine` CLI help | Tích hợp đồng bộ 6 engine vào CLI, Web và GUI. |

---

## Milestone 4 — Preamble thông minh & Auto PDF Compiler

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/` | Lý do |
|---|---|---|
| `mathtranslate/pdf_compiler.py` | Tạo `build_language_preamble(l_to)`, `build_default_begin(l_to)` và `compile_tex_to_pdf()` trả về `CompileResult` | Repo gốc luôn hardcode `\usepackage{xeCJK}` (không phù hợp tiếng Việt) và dùng `os.system(f'xelatex {output_path}')` không an toàn với đường dẫn có dấu cách/tiếng Việt. Bản mới tự chèn `fontspec` + `[vietnamese]{babel}` cho `vi`, giữ `xeCJK` cho CJK, chạy `xelatex -> bibtex -> xelatex x2` qua `subprocess.run([...])` và báo rõ khi thiếu `xelatex`. |
| `mathtranslate/translate_tex.py` & `translate_arxiv.py` | Dùng `compile_tex_to_pdf` cho `--compile` và bổ sung cờ `--compile` cho cả `translate_arxiv` | Hỗ trợ tự động biên dịch PDF đồng nhất cho cả `.tex` đơn và arXiv. |

---

## Milestone 5 — Local Web UI (`web/`)

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/web/` | Lý do |
|---|---|---|
| `web/design-tokens.md`, `templates/index.html`, `static/css/style.css`, `static/js/app.js` | Thiết kế lại theo phong cách **Academic Editorial (Warm Paper `#FAF9F5` & Ink `#18181B`, Slate-Navy `#1B3A5C`)**, bỏ Bootstrap CDN + gradient tím-xanh + sửa lỗi nested `<form>` | Tuân thủ `ruleTRANS.md`, hỗ trợ chọn 6 engine, 64 ngôn ngữ (mặc định `en -> vi`), Auto PDF và responsive Desktop/Mobile. |
| `web/app.py` | Chạy bất đồng bộ theo Job ID (`ThreadPoolExecutor` + `/api/jobs/<id>`), bind mặc định `127.0.0.1:5000`, `debug=False`, random `secret_key`, chặn path traversal ở `/download`, bật daemon dọn thư mục cũ | Khắc phục timeout cứng 30s, lỗ hổng bảo mật (`0.0.0.0` + `debug=True`), và scheduler dọn rác không chạy ở repo gốc. |

---

## Milestone 6 — Desktop GUI (`gui/`)

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/gui/` | Lý do |
|---|---|---|
| `gui/controller.py`, `gui/app.py`, `gui/__main__.py` | Thay Kivy (thiếu `guipage/file.kv`, nặng OpenGL, lỗi font tiếng Việt) bằng `tkinter` + `ttk` (stdlib) + `QueueWriter` + `self.after(100, ...)` | Chạy mượt ngay trên Windows không cần cài thêm thư viện ngoài, hiển thị tiếng Việt có dấu chuẩn, sửa lỗi thiếu `Redirect` ở luồng dịch `.tex`, bổ sung tab `Project .zip`, 6 engine và Auto PDF. |
| `pyproject.toml` | Thêm optional dep `web = ["flask>=3.0"]` và console scripts `mathtranslate-web`, `mathtranslate-gui` | Cho phép khởi chạy nhanh Web UI và Desktop GUI từ dòng lệnh. |

---

## Post-Review Hardening (Ponytail & Teamwork Preview Protocol)

| File trong `mathtranslate_v2/` | Điểm khác biệt so với trước | Lý do |
|---|---|---|
| `mathtranslate/utils.py`, `mathtranslate/pdf_compiler.py`, `web/app.py`, `gui/controller.py` | Gộp chung `parse_language_options`, `normalize_arxiv_id`, `compile_zip_to_pdf` về lõi dùng chung; Web UI nhận được cả link arXiv đầy đủ (`https://arxiv.org/abs/...`) và ép `threads=1` cho `tencent`; `cleanup_expired_jobs` bỏ qua job đang `running`/`queued` | Xóa ~80 dòng code trùng lặp giữa Web/GUI, đồng nhất hành vi nhận link arXiv và bảo vệ job chạy lâu. |
| `mathtranslate/engines/gemini_engine.py`, `google_engine.py`, `translate.py` | Bỏ `params={"key": ...}` trên URL Gemini (chỉ gửi header `x-goog-api-key`); nối chuỗi lỗi fallback `raise ... from primary_exc`; đóng `HTTPError.close()` (RAII) | Chặn lộ API key trong log lỗi HTTP, giữ nguyên nguyên nhân lỗi gốc và đạt `0 Warning` dưới `python -W error` trên Python 3.14. |

---

## Giai đoạn PDF-M1 — Dịch PDF Giữ Nguyên Định Dạng, Bảo vệ Công thức & Kiểm định Ảnh

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/` | Lý do |
|---|---|---|
| `mathtranslate/pdf_doc.py` | Bổ sung pipeline dịch trực tiếp file `.pdf` (`EN -> VI`): phân loại trang `TEXT/SCAN/MIXED` (cấm tự chạy OCR trên `SCAN`), sắp xếp thứ tự đọc đa cột, bảo vệ công thức 3 lớp (font toán/MathType/PUA `0xE000..0xF8FF`/vector), gộp request NMT bằng `XMATHXSEP`, xoá chữ gốc không đụng hình/vector (`PDF_REDACT_IMAGE_NONE` + `PDF_REDACT_LINE_ART_NONE`), nhúng subset font `Times New Roman`/`Arial`/`Consolas` qua `insert_htmlbox`, tự co chữ chống tràn + gắn cờ trang, giữ nguyên TOC/links/metadata | Repo `reference/` gốc không có tính năng dịch trực tiếp PDF (phải qua Mathpix trả phí); bản `pdf2zh` trước đó làm vỡ ký tự MathType PUA và trộn font tiếng Việt. |
| `mathtranslate/pdf_qa.py` | Bổ sung bộ kiểm định bắt buộc sau dịch: khớp số trang, khớp/bảo toàn số ảnh, 0 rò rỉ placeholder (`{v0}`, `XMATHX`), kiểm tra dấu tiếng Việt, đo độ tương đồng pixel (`1 - MAE`) vùng công thức độc lập và xuất ảnh PNG đối chiếu song song (Trái: Gốc / Phải: Dịch) | Đảm bảo kiểm chứng tự động + trực quan trước khi báo hoàn thành. |
| `pyproject.toml` & `tests/test_pdf_m1.py` | Thêm optional dep `pdf = ["pymupdf>=1.25"]`, CLI `translate_pdf` và 5 bài kiểm thử tự động `test_pdf_m1.py` | Cho phép chạy độc lập và kiểm thử đầy đủ `PDF-M1`. |
