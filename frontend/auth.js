// Shared auth helpers used by app.html, login.html, and signup.html.
async function apiJson(path, opts = {}) {
  const res = await fetch(path, {
    method: "GET",
    credentials: "same-origin",
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => null);
  return { ok: res.ok, status: res.status, data };
}

async function getCurrentUser() {
  const { ok, data } = await apiJson("/api/auth/me");
  return ok ? data : null;
}

async function requireLogin() {
  const user = await getCurrentUser();
  if (!user) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.href = `/login.html?next=${next}`;
    return null;
  }
  return user;
}

async function logout() {
  await apiJson("/api/auth/logout", { method: "POST" });
  window.location.href = "/login.html";
}

async function startCheckout() {
  const { ok, data } = await apiJson("/api/billing/checkout", { method: "POST" });
  if (ok && data && data.url) {
    window.location.href = data.url;
  } else {
    alert((data && data.detail) || "Couldn't start checkout. Please try again.");
  }
}

async function openBillingPortal() {
  const { ok, data } = await apiJson("/api/billing/portal", { method: "POST" });
  if (ok && data && data.url) {
    window.location.href = data.url;
  } else {
    alert((data && data.detail) || "Couldn't open billing portal. Please try again.");
  }
}

function renderAccountBar(container, user) {
  const usageText = user.is_pro
    ? "Pro plan — unlimited conversions"
    : `Free plan — ${user.usage_this_month} of ${user.free_monthly_limit} conversions used this month`;

  container.innerHTML = `
    <span class="account-email">${escapeHtmlLocal(user.email)}</span>
    <span class="account-usage">${escapeHtmlLocal(usageText)}</span>
    ${user.is_pro
      ? '<button type="button" id="manage-billing-btn" class="link-button">Manage billing</button>'
      : '<button type="button" id="upgrade-btn" class="link-button link-button-accent">Upgrade to Pro</button>'}
    <button type="button" id="logout-btn" class="link-button">Log out</button>
  `;

  const upgradeBtn = container.querySelector("#upgrade-btn");
  if (upgradeBtn) upgradeBtn.addEventListener("click", startCheckout);
  const manageBtn = container.querySelector("#manage-billing-btn");
  if (manageBtn) manageBtn.addEventListener("click", openBillingPortal);
  container.querySelector("#logout-btn").addEventListener("click", logout);
}

function escapeHtmlLocal(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
