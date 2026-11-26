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

---

## Giai đoạn PDF-M2 — Dịch PDF theo Lô, Translation Memory SQLite, Checkpoint & Tiếp tục (Resume)

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/` | Lý do |
|---|---|---|
| `mathtranslate/pdf_batch.py` | Bổ sung bộ điều phối dịch PDF theo lô (`PdfBatchTranslator`): chia lô 5–10 trang theo mật độ chữ (`compute_density_batches`), hàng đợi giới hạn luồng (`max_workers`, tự hạ về `1` khi `supports_multithread=False`), retry exponential backoff kèm `cancel_event.wait` cho HTTP 429/timeout và fail-fast cho `ValueError`/4xx qua `_RetryingTranslatorProxy`, bộ nhớ dịch `SQLiteTranslationMemory` (`pdf_tm.sqlite3`, WAL + `RLock`, khoá SHA-256 `[text, glossary_hash, engine, l_from, l_to]`), ghi `checkpoint.json` + lắp ghép `partial.pdf` đọc được ngay sau mỗi lô (atomic `tmp` + `os.replace`), hỗ trợ `cancel()` / `from_checkpoint()` / `resume()` không dịch lại lô đã xong, và xuất `qa_report.json` | Repo `reference/` gốc không có cơ chế dịch PDF chia lô hay checkpoint/resume; tránh mất tiến độ khi dịch tài liệu dài hàng trăm trang (như `linear.pdf` 386 trang) hoặc gặp giới hạn tốc độ API. |
| `mathtranslate/pdf_doc.py` | Bổ sung `_FONT_CACHE_LOCK` cho đa luồng, `scan_pages_all=True` + regex biên dịch sẵn cho `extract_ambiguous_terms`, gom cụm `redact_rects` theo y-band + kiểm tra khoảng hở ngang đối xứng `<= 24pt` để tránh cắt qua công thức, ghi đè glossary không phân biệt hoa/thường & khớp ranh giới từ (tối đa 24 thuật ngữ trong prompt), ngữ cảnh đoạn liền kề xuyên trang (`_extract_edge_prose_snippet` <1 ms) kèm bộ lọc chống rò rỉ prompt LLM kể cả định dạng Markdown (`_strip_llm_prompt_leaks`) mà vẫn giữ cache key thuần trên đoạn đích, chuyển tiếp `cache_lookup`/`cache_store` + `cancel_check`, và tách biệt rõ `scan_policy="skip"` (xoá trang scan & chuẩn hoá phân cấp TOC `_normalize_toc_levels`) khỏi `"keep_original"` | Đảm bảo an toàn đa luồng khi chạy song song nhiều lô, nâng cao chất lượng nhất quán thuật ngữ/ngữ cảnh cho PDF dài, tránh xoá nhầm công thức giữa dòng và giữ nguyên mục lục hợp lệ khi bỏ trang scan. |
| `mathtranslate/pdf_qa.py` | Đưa lỗi lệch kích thước trang (`> 0.5pt`) vào `hard_fail`, thêm tuỳ chọn `require_page_count_match` & `page_pairs` cho chế độ `skip`/kiểm định từng phần, bổ sung `.to_dict()` và hàm `save_qa_report_json()` ghi JSON atomic | Phục vụ kiểm định tự động chặt chẽ cho cả PDF đầy đủ và PDF bỏ trang scan, đồng thời cung cấp báo cáo JSON chuẩn cho Web/GUI. |
| `tests/test_pdf_m2_batch.py` | Bổ sung bộ kiểm thử đơn vị & tích hợp PDF-M2 (chia lô mật độ trên `linear.pdf`, độ nhạy SHA-256 & đa luồng SQLite TM, backoff/fast-fail/huỷ ngay, vòng đời đa lô với `partial.pdf`/`qa_report.json`, kịch bản 386 trang `linear.pdf` chạy 2 lô -> `cancel()` -> đọc `partial.pdf` -> `from_checkpoint()`/`resume()` tiếp tục tại lô 3 với `0` lượt dịch lại, các bài kiểm tra R2 và kiểm thử hồi quy `redact_rects` y-band, Markdown prompt leak, chuẩn hoá TOC khi `skip`) | Kiểm chứng đầy đủ yêu cầu R1/R2 và chống hồi quy tự động dưới `python -W error -m pytest`. |

---

## Giai đoạn PDF-M3 — Tích hợp PDF (EN -> VI) vào Local Web UI & Desktop GUI

| File trong `mathtranslate_v2/` | Điểm khác biệt so với `reference/` | Lý do |
|---|---|---|
| `web/app.py` | Bổ sung `/api/pdf/inspect`, `/api/translate/pdf`, `/api/jobs/<id>/scan-choice`, `/api/jobs/<id>/cancel`, `/api/jobs/<id>/resume`; cập nhật `_serialize_job` trả về `progress` (trang, %, batch, tốc độ, ETA), `scan_pages`, `overflow_pages`, `partial_available`, `qa_report`, cùng artifact `partial.pdf` & `qa_report.json` | Cho phép kiểm tra trang scan trước khi dịch (không tự chạy OCR), theo dõi tiến độ/ETA theo thời gian thực, tải bản xuất một phần và hủy/tiếp tục qua checkpoint. |
| `web/templates/index.html`, `web/static/css/style.css`, `web/static/js/app.js` | Thêm tab thứ 4 `PDF Document (EN -> VI)` (`repeat(4, 1fr)` trên desktop, gộp 1 cột trên mobile `<= 768px`), hộp kiểm tra PDF + bảng thuật ngữ `Glossary` chỉnh sửa được, hộp chọn xử lý trang scan (`awaiting_scan`), thanh tiến độ + ETA + danh sách trang tràn chữ + nút `Cancel`/`Resume` theo đúng `design-tokens.md` | Đồng bộ trải nghiệm người dùng trên trình duyệt mà không phá vỡ quy tắc thiết kế Academic Editorial (0 emoji, 0 gradient, 0 CDN ngoài). |
| `gui/controller.py`, `gui/__init__.py`, `gui/app.py` | Bổ sung `PdfTranslationController` (headless-testable: `inspect`, `start`, `cancel`, `resume`, `wait`, `progress_queue`, `status_queue`) và tab `PDF Document (EN -> VI)` trong `MathTranslateApp` với Glossary, Scan policy, Progress/ETA/Overflow, Cancel/Resume | Mang toàn bộ pipeline dịch PDF theo lô có checkpoint lên Desktop GUI `tkinter`. |
| `tests/test_pdf_m3_ui.py` | Bổ sung kiểm thử tích hợp cho Web API PDF (`/api/pdf/inspect`, `/api/translate/pdf`, `scan-choice`, `cancel`/`resume`, `partial.pdf`, `qa_report`) và `PdfTranslationController` | Đảm bảo hoạt động ổn định cho cả Web UI và Desktop GUI dưới `python -W error -m pytest`. |
