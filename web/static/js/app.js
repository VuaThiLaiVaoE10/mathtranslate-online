(function () {
    "use strict";

    const ENGINE_LABELS = {
        google: "Google Translate (google)",
        tencent: "Tencent Cloud TMT (tencent)",
        gemini: "Google Gemini (gemini)",
        openai: "OpenAI (openai)",
        ollama: "Ollama Local LLM (ollama)",
        deepl: "DeepL (deepl)",
    };

    const MODE_ENDPOINTS = {
        arxiv: "/api/translate/arxiv",
        tex: "/api/translate/tex",
        zip: "/api/translate/zip",
        pdf: "/api/translate/pdf",
    };

    const form = document.getElementById("translate-form");
    const modeTabs = Array.from(document.querySelectorAll(".mode-tab"));
    const modePanels = {
        arxiv: document.getElementById("panel-mode-arxiv"),
        tex: document.getElementById("panel-mode-tex"),
        zip: document.getElementById("panel-mode-zip"),
        pdf: document.getElementById("panel-mode-pdf"),
    };

    const arxivInput = document.getElementById("arxiv-id");
    const texInput = document.getElementById("tex-file");
    const zipInput = document.getElementById("zip-file");
    const pdfInput = document.getElementById("pdf-file");
    const texFilename = document.getElementById("tex-filename");
    const zipFilename = document.getElementById("zip-filename");
    const pdfFilename = document.getElementById("pdf-filename");
    const pdfInspectBox = document.getElementById("pdf-inspect-box");
    const pdfInspectSummary = document.getElementById("pdf-inspect-summary");
    const pdfPageRangeInput = document.getElementById("pdf-page-range");
    const pdfGlossaryInput = document.getElementById("pdf-glossary");
    const engineSelect = document.getElementById("engine");
    const apiKeyField = document.getElementById("api-key-field");
    const apiKeyInput = document.getElementById("api-key");
    const lFromSelect = document.getElementById("l-from");
    const lToSelect = document.getElementById("l-to");
    const swapLangBtn = document.getElementById("swap-lang-btn");
    const compilePdfInput = document.getElementById("compile-pdf");
    const submitBtn = document.getElementById("submit-btn");
    const formError = document.getElementById("form-error");

    const statusPanel = document.getElementById("status-panel");
    const statusPill = document.getElementById("status-pill");
    const statusPillText = document.getElementById("status-pill-text");
    const jobIdBadge = document.getElementById("job-id-badge");
    const jobMetaCaption = document.getElementById("job-meta-caption");

    const stateBlocks = {
        idle: document.getElementById("state-idle"),
        running: document.getElementById("state-running"),
        awaiting_scan: document.getElementById("state-awaiting-scan"),
        cancelled: document.getElementById("state-cancelled"),
        done: document.getElementById("state-done"),
        error: document.getElementById("state-error"),
    };
    const runningSummary = document.getElementById("running-summary");
    const pdfProgressBox = document.getElementById("pdf-progress-box");
    const pdfProgressPages = document.getElementById("pdf-progress-pages");
    const pdfProgressEta = document.getElementById("pdf-progress-eta");
    const pdfProgressBar = document.getElementById("pdf-progress-bar");
    const pdfOverflowLive = document.getElementById("pdf-overflow-live");
    const pdfPartialLink = document.getElementById("pdf-partial-link");
    const cancelJobBtn = document.getElementById("cancel-job-btn");
    const scanPagesList = document.getElementById("scan-pages-list");
    const scanKeepBtn = document.getElementById("scan-keep-btn");
    const scanSkipBtn = document.getElementById("scan-skip-btn");
    const scanCancelBtn = document.getElementById("scan-cancel-btn");
    const resumeJobBtn = document.getElementById("resume-job-btn");
    const pdfCancelledPartialLink = document.getElementById("pdf-cancelled-partial-link");
    const doneSummary = document.getElementById("done-summary");
    const pdfNotice = document.getElementById("pdf-notice");
    const pdfQaSummary = document.getElementById("pdf-qa-summary");
    const pdfOverflowDone = document.getElementById("pdf-overflow-done");
    const downloadList = document.getElementById("download-list");
    const errorSummary = document.getElementById("error-summary");

    const logContainer = document.getElementById("log-container");
    const jobLog = document.getElementById("job-log");
    const copyLogBtn = document.getElementById("copy-log-btn");

    let activeMode = "arxiv";
    let pollTimer = null;
    let activeJobId = null;
    let inspectedUploadId = null;

    function formatBytes(bytes) {
        if (!Number.isFinite(bytes) || bytes < 1024) {
            return `${bytes || 0} B`;
        }
        if (bytes < 1024 * 1024) {
            return `${(bytes / 1024).toFixed(1)} KB`;
        }
        return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
    }

    function showFormError(message) {
        if (!message) {
            formError.hidden = true;
            formError.textContent = "";
            return;
        }
        formError.textContent = message;
        formError.hidden = false;
    }

    function setActiveMode(mode) {
        if (!MODE_ENDPOINTS[mode]) {
            return;
        }
        activeMode = mode;
        showFormError("");

        modeTabs.forEach((tab) => {
            const isSelected = tab.dataset.mode === mode;
            tab.classList.toggle("is-active", isSelected);
            tab.setAttribute("aria-selected", String(isSelected));
        });

        Object.entries(modePanels).forEach(([key, panel]) => {
            panel.hidden = key !== mode;
        });
    }

    function setupDropzone(kind, inputEl, nameEl, expectedExt, emptyText) {
        const zone = document.querySelector(`[data-dropzone="${kind}"]`);
        if (!zone) {
            return;
        }

        function updateLabel() {
            const file = inputEl.files && inputEl.files[0];
            if (file) {
                nameEl.textContent = `${file.name} (${formatBytes(file.size)})`;
                zone.classList.add("has-file");
                showFormError("");
            } else {
                nameEl.textContent = emptyText;
                zone.classList.remove("has-file");
            }
        }

        zone.addEventListener("click", () => inputEl.click());
        zone.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                inputEl.click();
            }
        });

        inputEl.addEventListener("change", updateLabel);

        zone.addEventListener("dragover", (event) => {
            event.preventDefault();
            zone.classList.add("is-dragover");
        });

        zone.addEventListener("dragleave", () => {
            zone.classList.remove("is-dragover");
        });

        zone.addEventListener("drop", (event) => {
            event.preventDefault();
            zone.classList.remove("is-dragover");
            const dropped = event.dataTransfer && event.dataTransfer.files;
            if (!dropped || !dropped.length) {
                return;
            }
            const file = dropped[0];
            if (!file.name.toLowerCase().endsWith(expectedExt)) {
                showFormError(`Please drop a valid ${expectedExt} file.`);
                return;
            }
            const dt = new DataTransfer();
            dt.items.add(file);
            inputEl.files = dt.files;
            updateLabel();
        });
    }

    function setPanelState(state, pillLabel) {
        statusPanel.dataset.state = state;
        Object.entries(stateBlocks).forEach(([key, el]) => {
            el.hidden = key !== state;
        });

        statusPill.className = `status-pill status-${state}`;
        statusPillText.textContent = pillLabel;
        logContainer.hidden = state === "idle";
    }

    function setBusy(isBusy) {
        submitBtn.disabled = isBusy;
        submitBtn.textContent = isBusy ? "Translating..." : "Start Translation";
    }

    function stopPolling() {
        if (pollTimer !== null) {
            window.clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    function renderDownloads(jobId, files) {
        downloadList.innerHTML = "";
        files.forEach((filename) => {
            const li = document.createElement("li");
            li.className = "download-item";

            const nameSpan = document.createElement("span");
            nameSpan.className = "download-filename";
            nameSpan.textContent = filename;

            const link = document.createElement("a");
            link.className = "download-link";
            link.href = `/download/${encodeURIComponent(jobId)}/${encodeURIComponent(filename)}`;
            link.textContent = "Download";
            link.setAttribute("download", filename);

            li.appendChild(nameSpan);
            li.appendChild(link);
            downloadList.appendChild(li);
        });
    }

    function formatEta(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) {
            return "ETA: --";
        }
        const rounded = Math.ceil(seconds);
        if (rounded < 60) {
            return `ETA: ${rounded}s`;
        }
        const mins = Math.floor(rounded / 60);
        const secs = rounded % 60;
        return `ETA: ${mins}m ${secs}s`;
    }

    function parseGlossaryText(raw) {
        const text = (raw || "").trim();
        if (!text) {
            return null;
        }
        if (text.startsWith("{")) {
            return JSON.parse(text);
        }
        const glossary = {};
        text.split(/\r?\n/).forEach((line) => {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith("#")) {
                return;
            }
            const sepIdx = trimmed.includes("=") ? trimmed.indexOf("=") : trimmed.indexOf(":");
            if (sepIdx > 0) {
                const k = trimmed.slice(0, sepIdx).trim();
                const v = trimmed.slice(sepIdx + 1).trim();
                if (k && v) {
                    glossary[k] = v;
                }
            }
        });
        return Object.keys(glossary).length > 0 ? glossary : null;
    }

    async function inspectSelectedPdf(file) {
        inspectedUploadId = null;
        if (!file || !pdfInspectBox || !pdfInspectSummary) {
            return;
        }
        pdfInspectBox.hidden = false;
        pdfInspectSummary.textContent = "Inspecting PDF pages and terminology...";
        const fd = new FormData();
        fd.append("file", file);
        try {
            const resp = await fetch("/api/pdf/inspect", { method: "POST", body: fd });
            const info = await resp.json();
            if (!resp.ok) {
                throw new Error(info.error || "Inspection failed");
            }
            inspectedUploadId = info.upload_id || null;
            const counts = info.counts || {};
            const scanPages = info.scan_pages || [];
            const scanText = scanPages.length > 0
                ? `Scan pages detected (no auto-OCR): ${scanPages.join(", ")}`
                : "No scanned pages detected.";
            pdfInspectSummary.textContent =
                `Pages: ${info.total_pages || 0} (TEXT: ${counts.TEXT || 0}, SCAN: ${counts.SCAN || 0}, MIXED: ${counts.MIXED || 0}). ${scanText}`;
            if (pdfGlossaryInput && !pdfGlossaryInput.value.trim()) {
                const merged = Object.assign({}, info.default_glossary || {});
                if (Object.keys(merged).length > 0) {
                    pdfGlossaryInput.value = JSON.stringify(merged, null, 2);
                }
            }
        } catch (err) {
            pdfInspectSummary.textContent = `Inspection warning: ${err.message}`;
        }
    }

    function renderJob(job) {
        if (!job) {
            return;
        }

        jobIdBadge.textContent = `job:${job.id}`;
        jobIdBadge.hidden = false;
        jobMetaCaption.textContent = `Mode: ${job.mode.toUpperCase()} | Engine: ${job.engine} | ${job.l_from} -> ${job.l_to}`;

        if (job.log) {
            logContainer.hidden = false;
            jobLog.textContent = job.log;
            jobLog.scrollTop = jobLog.scrollHeight;
        }

        if (job.status === "awaiting_scan") {
            stopPolling();
            setBusy(false);
            setPanelState("awaiting_scan", "Awaiting Scan Choice");
            const scanPages = job.scan_pages || [];
            if (scanPagesList) {
                scanPagesList.textContent = `Scanned pages (1-based): ${scanPages.join(", ")}. Automatic OCR is disabled.`;
            }
            return;
        }

        if (job.status === "cancelled") {
            stopPolling();
            setBusy(false);
            setPanelState("cancelled", "Cancelled");
            if (pdfCancelledPartialLink) {
                if (job.partial_available) {
                    pdfCancelledPartialLink.href = `/download/${encodeURIComponent(job.id)}/partial.pdf`;
                    pdfCancelledPartialLink.hidden = false;
                } else {
                    pdfCancelledPartialLink.hidden = true;
                }
            }
            return;
        }

        if (job.status === "queued" || job.status === "running") {
            setPanelState("running", job.status === "queued" ? "Queued" : "Running");
            runningSummary.textContent =
                job.status === "queued"
                    ? "Job queued. Waiting for an available worker..."
                    : `Translating via ${job.engine} (${job.l_from} -> ${job.l_to})...`;

            if (pdfProgressBox && job.mode === "pdf") {
                pdfProgressBox.hidden = false;
                const prog = job.progress || {};
                const completed = prog.completed_pages || 0;
                const total = prog.total_pages || 0;
                const pct = Number.isFinite(prog.percent) ? prog.percent : 0;
                pdfProgressPages.textContent = `${completed} / ${total} pages (${pct.toFixed(1)}%)`;
                pdfProgressEta.textContent = formatEta(prog.eta_seconds);
                pdfProgressBar.style.width = `${Math.min(100, Math.max(0, pct))}%`;

                const overflow = job.overflow_pages || [];
                if (overflow.length > 0) {
                    pdfOverflowLive.hidden = false;
                    pdfOverflowLive.textContent = `Overflow pages needing review: ${overflow.join(", ")}`;
                } else {
                    pdfOverflowLive.hidden = true;
                }

                if (job.partial_available) {
                    pdfPartialLink.href = `/download/${encodeURIComponent(job.id)}/partial.pdf`;
                    pdfPartialLink.hidden = false;
                } else {
                    pdfPartialLink.hidden = true;
                }
            } else if (pdfProgressBox) {
                pdfProgressBox.hidden = true;
            }
            return;
        }

        stopPolling();
        setBusy(false);

        if (job.status === "done" || job.status === "completed") {
            setPanelState("done", "Completed");
            const fileCount = (job.files || []).length;
            doneSummary.textContent =
                fileCount === 1
                    ? "1 output artifact is ready for download."
                    : `${fileCount} output artifacts are ready for download.`;

            if (job.pdf_status && job.pdf_status !== "skipped") {
                pdfNotice.hidden = false;
                pdfNotice.textContent = `PDF status (${job.pdf_status}): ${job.pdf_message || ""}`;
            } else {
                pdfNotice.hidden = true;
                pdfNotice.textContent = "";
            }

            if (pdfQaSummary) {
                if (job.qa_report) {
                    const qa = job.qa_report;
                    pdfQaSummary.hidden = false;
                    pdfQaSummary.textContent =
                        `QA Report: pages=${qa.output_pages || "?"}/${qa.input_pages || "?"}, ` +
                        `placeholders_leaked=${qa.placeholders_leaked || 0}, ` +
                        `overflow_pages=${(qa.overflow_pages || []).length}`;
                } else {
                    pdfQaSummary.hidden = true;
                }
            }

            if (pdfOverflowDone) {
                const overflow = job.overflow_pages || [];
                if (overflow.length > 0) {
                    pdfOverflowDone.hidden = false;
                    pdfOverflowDone.textContent = `Overflow pages flagged for manual check: ${overflow.join(", ")}`;
                } else {
                    pdfOverflowDone.hidden = true;
                }
            }

            renderDownloads(job.id, job.files || []);
            return;
        }

        setPanelState("error", "Failed");
        errorSummary.textContent = job.error || "Translation failed. Review the execution log below.";
    }

    async function pollJob(jobId) {
        if (jobId !== activeJobId) {
            return;
        }
        try {
            const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
                headers: { Accept: "application/json" },
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || `Status request failed (${response.status})`);
            }
            renderJob(data);
            if (data.status === "queued" || data.status === "running") {
                pollTimer = window.setTimeout(() => pollJob(jobId), 800);
            }
        } catch (err) {
            stopPolling();
            setBusy(false);
            setPanelState("error", "Failed");
            errorSummary.textContent = err.message || "Lost connection while polling job status.";
        }
    }

    async function hydrateMeta() {
        try {
            const response = await fetch("/api/meta", { headers: { Accept: "application/json" } });
            if (!response.ok) {
                return;
            }
            const meta = await response.json();

            if (Array.isArray(meta.engines) && meta.engines.length > 0) {
                const currentEngine = engineSelect.value || (meta.defaults && meta.defaults.engine) || "google";
                engineSelect.innerHTML = "";
                meta.engines.forEach((eng) => {
                    const opt = document.createElement("option");
                    opt.value = eng;
                    opt.textContent = ENGINE_LABELS[eng] || eng;
                    if (eng === currentEngine) {
                        opt.selected = true;
                    }
                    engineSelect.appendChild(opt);
                });
            }

            if (Array.isArray(meta.languages) && meta.languages.length > 0) {
                const defaultFrom = (meta.defaults && meta.defaults.l_from) || "en";
                const defaultTo = (meta.defaults && meta.defaults.l_to) || "vi";
                const selectedFrom = lFromSelect.value || defaultFrom;
                const selectedTo = lToSelect.value || defaultTo;

                [
                    [lFromSelect, selectedFrom],
                    [lToSelect, selectedTo],
                ].forEach(([selectEl, targetVal]) => {
                    selectEl.innerHTML = "";
                    meta.languages.forEach((lang) => {
                        const opt = document.createElement("option");
                        opt.value = lang.code;
                        opt.textContent = `${lang.name} (${lang.code})`;
                        if (lang.code === targetVal) {
                            opt.selected = true;
                        }
                        selectEl.appendChild(opt);
                    });
                });
            }
        } catch (_) {
            // Server-rendered options already exist as fallback.
        }
    }

    async function handleSubmit(event) {
        event.preventDefault();
        showFormError("");

        const formData = new FormData();
        formData.append("engine", engineSelect.value);
        if (apiKeyInput && apiKeyInput.value.trim()) {
            formData.append("api_key", apiKeyInput.value.trim());
        }
        formData.append("l_from", lFromSelect.value);
        formData.append("l_to", lToSelect.value);
        formData.append("compile_pdf", compilePdfInput.checked ? "true" : "false");

        if (activeMode === "arxiv") {
            const arxivId = (arxivInput.value || "").trim();
            if (!arxivId) {
                showFormError("Please enter an arXiv ID before starting translation.");
                arxivInput.focus();
                return;
            }
            formData.append("arxiv_id", arxivId);
        } else if (activeMode === "tex") {
            const file = texInput.files && texInput.files[0];
            if (!file) {
                showFormError("Please select a .tex file to translate.");
                return;
            }
            formData.append("file", file);
        } else if (activeMode === "zip") {
            const file = zipInput.files && zipInput.files[0];
            if (!file) {
                showFormError("Please select a .zip project archive to translate.");
                return;
            }
            formData.append("file", file);
        } else if (activeMode === "pdf") {
            const file = pdfInput && pdfInput.files && pdfInput.files[0];
            if (!file && !inspectedUploadId) {
                showFormError("Please select a .pdf file to translate.");
                return;
            }
            if (inspectedUploadId) {
                formData.append("upload_id", inspectedUploadId);
            } else if (file) {
                formData.append("file", file);
            }
            formData.append("scan_policy", "abort");
            if (pdfPageRangeInput && pdfPageRangeInput.value.trim()) {
                formData.append("page_range", pdfPageRangeInput.value.trim());
            }
            try {
                const parsedGlossary = parseGlossaryText(pdfGlossaryInput ? pdfGlossaryInput.value : "");
                if (parsedGlossary) {
                    formData.append("glossary", JSON.stringify(parsedGlossary));
                }
            } catch (gErr) {
                showFormError(`Invalid glossary JSON: ${gErr.message}`);
                return;
            }
        }

        stopPolling();
        setBusy(true);

        try {
            const response = await fetch(MODE_ENDPOINTS[activeMode], {
                method: "POST",
                body: formData,
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || `Request failed (${response.status})`);
            }

            activeJobId = data.job_id;
            jobIdBadge.textContent = `job:${activeJobId}`;
            jobIdBadge.hidden = false;
            setPanelState("running", "Queued");
            runningSummary.textContent = "Job submitted. Waiting for live output...";
            jobLog.textContent = "";
            pollJob(activeJobId);
        } catch (err) {
            setBusy(false);
            showFormError(err.message || "Unable to submit translation request.");
        }
    }

    modeTabs.forEach((tab) => {
        tab.addEventListener("click", () => setActiveMode(tab.dataset.mode));
    });

    swapLangBtn.addEventListener("click", () => {
        const fromVal = lFromSelect.value;
        lFromSelect.value = lToSelect.value;
        lToSelect.value = fromVal;
    });

    copyLogBtn.addEventListener("click", async () => {
        const text = jobLog.textContent || "";
        if (!text) {
            return;
        }
        try {
            await navigator.clipboard.writeText(text);
            copyLogBtn.textContent = "Copied";
            window.setTimeout(() => {
                copyLogBtn.textContent = "Copy Log";
            }, 1500);
        } catch (_) {
            copyLogBtn.textContent = "Copy Failed";
            window.setTimeout(() => {
                copyLogBtn.textContent = "Copy Log";
            }, 1500);
        }
    });

    async function sendScanChoice(choice) {
        if (!activeJobId) {
            return;
        }
        try {
            const resp = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/scan-choice`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ choice }),
            });
            const data = await resp.json();
            if (!resp.ok) {
                throw new Error(data.error || "Failed to submit scan choice");
            }
            if (data.status === "cancelled") {
                pollJob(activeJobId);
            } else {
                setBusy(true);
                pollJob(activeJobId);
            }
        } catch (err) {
            showFormError(err.message);
        }
    }

    if (cancelJobBtn) {
        cancelJobBtn.addEventListener("click", async () => {
            if (!activeJobId) {
                return;
            }
            await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/cancel`, { method: "POST" });
            pollJob(activeJobId);
        });
    }

    if (resumeJobBtn) {
        resumeJobBtn.addEventListener("click", async () => {
            if (!activeJobId) {
                return;
            }
            setBusy(true);
            await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/resume`, { method: "POST" });
            pollJob(activeJobId);
        });
    }

    if (scanKeepBtn) {
        scanKeepBtn.addEventListener("click", () => sendScanChoice("keep_original"));
    }
    if (scanSkipBtn) {
        scanSkipBtn.addEventListener("click", () => sendScanChoice("skip"));
    }
    if (scanCancelBtn) {
        scanCancelBtn.addEventListener("click", () => sendScanChoice("cancel"));
    }

    if (pdfInput) {
        pdfInput.addEventListener("change", () => {
            const file = pdfInput.files && pdfInput.files[0];
            if (file) {
                inspectSelectedPdf(file);
            }
        });
    }

    setupDropzone("tex", texInput, texFilename, ".tex", "or drop a file here");
    setupDropzone("zip", zipInput, zipFilename, ".zip", "or drop an archive here");
    if (pdfInput && pdfFilename) {
        setupDropzone("pdf", pdfInput, pdfFilename, ".pdf", "or drop a PDF here");
    }
    function syncApiKeyVisibility() {
        if (!apiKeyField || !engineSelect) {
            return;
        }
        const needsKey = ["gemini", "openai", "deepl"].includes(engineSelect.value);
        apiKeyField.hidden = !needsKey;
    }

    if (engineSelect) {
        engineSelect.addEventListener("change", syncApiKeyVisibility);
    }

    form.addEventListener("submit", handleSubmit);
    hydrateMeta().then(syncApiKeyVisibility);
    syncApiKeyVisibility();
})();
