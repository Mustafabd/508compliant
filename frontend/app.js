(() => {
  const form = document.getElementById("upload-form");
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const fileChosen = document.getElementById("file-chosen");
  const submitBtn = document.getElementById("submit-btn");
  const statusEl = document.getElementById("status");
  const resultsEl = document.getElementById("results");
  const resultsSummary = document.getElementById("results-summary");
  const checklistEl = document.getElementById("checklist");
  const downloadLink = document.getElementById("download-link");
  const accountBar = document.getElementById("account-bar");
  const checkoutBanner = document.getElementById("checkout-banner");

  let selectedFile = null;

  (async () => {
    const user = await requireLogin();
    if (!user) return; // requireLogin already redirected to /login.html
    renderAccountBar(accountBar, user);

    const checkoutResult = new URLSearchParams(window.location.search).get("checkout");
    if (checkoutResult === "success") {
      checkoutBanner.hidden = false;
      checkoutBanner.setAttribute("data-state", "done");
      checkoutBanner.textContent = "Thanks for subscribing! It may take a few seconds for your Pro plan to show up below.";
    } else if (checkoutResult === "cancelled") {
      checkoutBanner.hidden = false;
      checkoutBanner.textContent = "Checkout cancelled — you're still on the free plan.";
    }
  })();

  function setStatus(text, state) {
    statusEl.textContent = text;
    if (state) {
      statusEl.setAttribute("data-state", state);
    } else {
      statusEl.removeAttribute("data-state");
    }
  }

  function setSelectedFile(file) {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name)) {
      setStatus("Please choose a PDF file.", "error");
      return;
    }
    selectedFile = file;
    fileChosen.textContent = `Selected: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
    submitBtn.disabled = false;
    setStatus("", null);
  }

  fileInput.addEventListener("change", () => setSelectedFile(fileInput.files[0]));

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files && e.dataTransfer.files[0];
    setSelectedFile(file);
  });

  function badge(status) {
    const label = status === "pass" ? "Pass" : status === "warning" ? "Review" : "Action needed";
    return `<span class="badge ${status}">${label}</span>`;
  }

  function renderReport(payload) {
    const { report, job_id, download_name } = payload;
    resultsSummary.textContent =
      `${report.filename} — ${report.page_count} page(s), title "${report.title}", language ${report.language}.`;

    checklistEl.innerHTML = "";
    for (const item of report.checklist) {
      const li = document.createElement("li");
      li.innerHTML = `
        <div class="item-head">${badge(item.status)} ${escapeHtml(item.label)}</div>
        <p class="detail">${escapeHtml(item.detail)}</p>
      `;
      checklistEl.appendChild(li);
    }

    downloadLink.href = `/api/download/${job_id}?name=${encodeURIComponent(download_name)}`;
    downloadLink.setAttribute("download", download_name);
    resultsEl.hidden = false;
    resultsEl.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!selectedFile) return;

    submitBtn.disabled = true;
    resultsEl.hidden = true;
    setStatus("Converting… this can take a few seconds for larger files.", "working");

    const formData = new FormData();
    formData.append("file", selectedFile);

    try {
      const res = await fetch("/api/convert", { method: "POST", body: formData, credentials: "same-origin" });
      const data = await res.json().catch(() => null);

      if (res.status === 401) {
        window.location.href = "/login.html?next=/app.html";
        return;
      }

      if (res.status === 402) {
        setStatus((data && data.detail) || "You've reached your free plan limit.", "error");
        const upgradeBtn = document.createElement("button");
        upgradeBtn.type = "button";
        upgradeBtn.className = "link-button link-button-accent";
        upgradeBtn.textContent = "Upgrade to Pro";
        upgradeBtn.addEventListener("click", startCheckout);
        statusEl.appendChild(document.createElement("br"));
        statusEl.appendChild(upgradeBtn);
        submitBtn.disabled = false;
        return;
      }

      if (!res.ok) {
        const message = (data && data.detail) || `Conversion failed (${res.status}).`;
        setStatus(message, "error");
        submitBtn.disabled = false;
        return;
      }

      setStatus("Done. Your accessible PDF is ready below.", "done");
      renderReport(data);
      const user = await getCurrentUser();
      if (user) renderAccountBar(accountBar, user);
    } catch (err) {
      setStatus("Network error — please try again.", "error");
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
