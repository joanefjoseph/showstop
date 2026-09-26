let currentPage = 1;
let totalRows = 0;
const PAGE_SIZE = 25;
let pollTimer = null;
function setStatusBadge(status) {
  const badge = document.getElementById("statusBadge");
  badge.style.display = "inline-block";
  badge.textContent = status;
  badge.className = "status-badge status-" + status;
}
async function startScrape() {
  const peopleUrl = document.getElementById("peopleUrl").value.trim();
  const maxPeople = document.getElementById("maxPeople").value || 100;
  const btn = document.getElementById("submitBtn");
  const logDiv = document.getElementById("log");
  if (!peopleUrl) {
    alert("Please enter a People page URL.");
    return;
  }
  btn.disabled = true;
  logDiv.textContent = "Starting scraper...\n";
  setStatusBadge("running");
  const res = await fetch("/api/scrape", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ people_url: peopleUrl, max_people: maxPeople }),
  });
  if (!res.ok) {
    const err = await res.json();
    logDiv.textContent += "Error: " + err.error + "\n";
    btn.disabled = false;
    setStatusBadge("error");
    return;
  }
  const { job_id } = await res.json();
  pollStatus(job_id);
}
function pollStatus(jobId) {
  const logDiv = document.getElementById("log");
  const btn = document.getElementById("submitBtn");
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const res = await fetch(`/api/scrape/${jobId}/status`);
    const data = await res.json();
    logDiv.textContent = data.log.join("\n");
    logDiv.scrollTop = logDiv.scrollHeight;
    setStatusBadge(data.status);
    if (data.status === "finished" || data.status === "error") {
      clearInterval(pollTimer);
      btn.disabled = false;
      loadPeople(); // refresh table with new results
    }
  }, 2000);
}
async function loadPeople() {
  const company = document.getElementById("companyFilter").value.trim();
  const params = new URLSearchParams({ page: currentPage, company });
  const res = await fetch(`/api/people?${params.toString()}`);
  const data = await res.json();
  totalRows = data.total;
  const tbody = document.getElementById("tableBody");
  tbody.innerHTML = "";
  for (const row of data.rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(row.company_name || "")}</td>
      <td>${escapeHtml(row.first_name || "")}</td>
      <td>${escapeHtml(row.last_name || "")}</td>
      <td>${escapeHtml(row.job_title || "")}</td>
      <td><a class="profile-link" href="${row.linkedin_profile_url}" target="_blank">${row.linkedin_profile_url}</a></td>
      <td>${escapeHtml(row.email || "")}</td>
    `;
    tbody.appendChild(tr);
  }
  const totalPages = Math.max(Math.ceil(totalRows / PAGE_SIZE), 1);
  document.getElementById("pageInfo").textContent =
    `Page ${currentPage} of ${totalPages} (${totalRows} total)`;
  document.getElementById("prevBtn").disabled = currentPage <= 1;
  document.getElementById("nextBtn").disabled = currentPage >= totalPages;
}
function prevPage() {
  if (currentPage > 1) {
    currentPage -= 1;
    loadPeople();
  }
}
function nextPage() {
  const totalPages = Math.max(Math.ceil(totalRows / PAGE_SIZE), 1);
  if (currentPage < totalPages) {
    currentPage += 1;
    loadPeople();
  }
}
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
// Initial load
loadPeople();