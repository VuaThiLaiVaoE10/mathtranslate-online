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
    };

    const form = document.getElementById("translate-form");
    const modeTabs = Array.from(document.querySelectorAll(".mode-tab"));
    const modePanels = {
        arxiv: document.getElementById("panel-mode-arxiv"),
        tex: document.getElementById("panel-mode-tex"),
        zip: document.getElementById("panel-mode-zip"),
    };

    const arxivInput = document.getElementById("arxiv-id");
    const texInput = document.getElementById("tex-file");
    const zipInput = document.getElementById("zip-file");
    const texFilename = document.getElementById("tex-filename");
    const zipFilename = document.getElementById("zip-filename");
    const engineSelect = document.getElementById("engine");
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
        done: document.getElementById("state-done"),
        error: document.getElementById("state-error"),
    };
    const runningSummary = document.getElementById("running-summary");
    const doneSummary = document.getElementById("done-summary");
    const pdfNotice = document.getElementById("pdf-notice");
    const downloadList = document.getElementById("download-list");
    const errorSummary = document.getElementById("error-summary");

    const logContainer = document.getElementById("log-container");
    const jobLog = document.getElementById("job-log");
    const copyLogBtn = document.getElementById("copy-log-btn");

    let activeMode = "arxiv";
    let pollTimer = null;
    let activeJobId = null;

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

    function renderJob(job) {
        jobIdBadge.textContent = `job:${job.id}`;
        jobIdBadge.hidden = false;
        jobMetaCaption.textContent = `Mode: ${job.mode.toUpperCase()} | Engine: ${job.engine} | ${job.l_from} -> ${job.l_to}`;

        const wasAtBottom = jobLog.scrollTop + jobLog.clientHeight >= jobLog.scrollHeight - 24;
        jobLog.textContent = job.log || "";
        if (wasAtBottom || job.status === "running" || job.status === "queued") {
            jobLog.scrollTop = jobLog.scrollHeight;
        }

        if (job.status === "queued" || job.status === "running") {
            setPanelState("running", job.status === "queued" ? "Queued" : "Running");
            runningSummary.textContent =
                job.status === "queued"
                    ? "Job accepted and waiting for an available translation worker."
                    : `Translating via ${job.engine} (${job.l_from} -> ${job.l_to})...`;
            return;
        }

        stopPolling();
        setBusy(false);

        if (job.status === "done") {
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

    setupDropzone("tex", texInput, texFilename, ".tex", "or drop a file here");
    setupDropzone("zip", zipInput, zipFilename, ".zip", "or drop an archive here");
    form.addEventListener("submit", handleSubmit);
    hydrateMeta();
})();
