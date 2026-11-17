/* UniExams single-page app: vanilla JS, no build step.
 * Auth: Cognito managed login (authorization code + PKCE), ID token silently refreshed.
 * API:  same origin (/api/* is routed by CloudFront to API Gateway), so no CORS.
 * Exams: answers are written to localStorage on every change and autosaved to the
 *        server every 30 s with an increasing sequence number (safe retries).
 */
"use strict";

const CFG = window.APP_CONFIG || {};
const app = document.getElementById("app");
const TOKENS_KEY = "uniexams.tokens";

// ------------------------------------------------------------------ helpers
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const mb = (bytes) => `${(Number(bytes) / 1048576).toFixed(1)} MB`;
const num = (v) => (v === null || v === undefined ? null : Number(v));
const view = (html) => { app.innerHTML = html; window.scrollTo(0, 0); };
const field = (form, name) => form.elements.namedItem(name).value;
const pad = (n) => String(n).padStart(2, "0");
const toLocalInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const fromLocalInput = (v) => new Date(v).toISOString();
const ROLE_LABEL = { Students: "Student", Tutors: "Tutor", Admins: "Administrator" };

let bannerTimer;
function banner(message, kind = "err") {
  const el = document.getElementById("banner");
  clearTimeout(bannerTimer);
  if (!message) { el.hidden = true; return; }
  el.textContent = message;
  el.className = `banner ${kind}`;
  el.hidden = false;
  bannerTimer = setTimeout(() => { el.hidden = true; }, 8000);
}
const showError = (e) => banner(e && e.message ? e.message : String(e));

function table(headers, rows) {
  return `<table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

async function fill(id, promise, render) {
  const el = document.getElementById(id);
  try {
    const data = await promise;
    if (el && el.isConnected) { el.classList.remove("muted"); el.innerHTML = render(data); }
  } catch (e) {
    if (el && el.isConnected) el.innerHTML = `<span class="badge err">${esc(e.message)}</span>`;
  }
}

// ------------------------------------------------------------------ auth (Cognito, code + PKCE)
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const randomString = (n) => b64url(crypto.getRandomValues(new Uint8Array(n)));

async function login() {
  const verifier = randomString(64);
  const stateValue = randomString(16);
  sessionStorage.setItem("uniexams.pkce", JSON.stringify({ verifier, state: stateValue, returnTo: location.hash }));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const query = new URLSearchParams({
    response_type: "code", client_id: CFG.clientId, redirect_uri: CFG.redirectUri,
    scope: "openid email profile", code_challenge_method: "S256", code_challenge: challenge, state: stateValue,
  });
  location.assign(`${CFG.cognitoDomain}/oauth2/authorize?${query}`);
}

function logout() {
  sessionStorage.removeItem(TOKENS_KEY);
  const query = new URLSearchParams({ client_id: CFG.clientId, logout_uri: CFG.redirectUri });
  location.assign(`${CFG.cognitoDomain}/logout?${query}`);
}

async function tokenRequest(params) {
  const res = await fetch(`${CFG.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CFG.clientId, ...params }),
  });
  if (!res.ok) throw new Error("Sign-in failed, please try again");
  const t = await res.json();
  return { idToken: t.id_token, refreshToken: t.refresh_token, expiresAt: Date.now() + t.expires_in * 1000 };
}

const loadTokens = () => { try { return JSON.parse(sessionStorage.getItem(TOKENS_KEY)); } catch { return null; } };
const saveTokens = (t) => sessionStorage.setItem(TOKENS_KEY, JSON.stringify(t));

async function handleRedirect() {
  const params = new URLSearchParams(location.search);
  if (!params.has("code")) return;
  const saved = JSON.parse(sessionStorage.getItem("uniexams.pkce") || "{}");
  sessionStorage.removeItem("uniexams.pkce");
  history.replaceState(null, "", location.pathname + (saved.returnTo || ""));
  if (!saved.state || params.get("state") !== saved.state) throw new Error("Sign-in expired, please sign in again");
  saveTokens(await tokenRequest({
    grant_type: "authorization_code", code: params.get("code"), redirect_uri: CFG.redirectUri, code_verifier: saved.verifier,
  }));
}

async function idToken() {
  let t = loadTokens();
  if (!t) return null;
  if (Date.now() > t.expiresAt - 5 * 60 * 1000 && t.refreshToken) {
    try {   // refresh 5 minutes early so long exams never hit an expired token
      const fresh = await tokenRequest({ grant_type: "refresh_token", refresh_token: t.refreshToken });
      t = { ...fresh, refreshToken: t.refreshToken };
      saveTokens(t);
    } catch {
      if (Date.now() > t.expiresAt) return null;
    }
  }
  return t.idToken;
}

// ------------------------------------------------------------------ API
class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const token = await idToken();
  if (!token) { await login(); throw new ApiError(401, "Signing in…"); }
  let res;
  try {
    res = await fetch(`/api${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Network error, check your connection");
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) sessionStorage.removeItem(TOKENS_KEY);
  if (!res.ok) throw new ApiError(res.status, data.error || `Request failed (${res.status})`);
  return data;
}

// ------------------------------------------------------------------ app shell & routing
const state = { me: null };

function renderUser() {
  const el = document.getElementById("user");
  el.innerHTML = `<span>${esc(state.me.email)}</span><span class="badge">${esc(ROLE_LABEL[state.me.role] || state.me.role)}</span>
    <button class="secondary" id="logout">Sign out</button>`;
  document.getElementById("logout").addEventListener("click", logout);
}

function router() {
  leaveExam();
  const [page, id, sub] = location.hash.replace(/^#\/?/, "").split("/");
  const isStudent = state.me.role === "Students";
  if (page === "exam" && id) return isStudent && sub !== "attempts" ? studentExam(id) : tutorAttempts(id);
  if (page === "assignment" && id && !isStudent) return tutorSubmissions(id);
  return isStudent ? studentDashboard() : staffDashboard();
}

// ------------------------------------------------------------------ student dashboard
function studentDashboard() {
  view(`
    <section class="card"><h2>My exams</h2><div id="exams" class="table-wrap muted">Loading…</div></section>
    <section class="card"><h2>Assignments</h2>
      <p class="muted">Upload a PDF or ZIP (max ${mb(CFG.maxUploadBytes)}). Files go straight to encrypted storage and you get an email receipt.</p>
      <div id="assignments" class="table-wrap muted">Loading…</div></section>
    <section class="card"><h2>My results</h2><div id="results" class="table-wrap muted">Loading…</div></section>`);
  fill("exams", api("GET", "/exams"), renderStudentExams);
  fill("assignments", api("GET", "/assignments"), renderStudentAssignments);
  fill("results", api("GET", "/results"), renderResults);
}

function examStatus(e, now) {
  if (e.attempt_status === "IN_PROGRESS") return { label: "In progress", cls: "warn", action: "Resume" };
  if (e.attempt_status === "SUBMITTED") return { label: "Submitted, being marked", cls: "ok" };
  if (e.attempt_status === "MARKED") return { label: e.results_released ? "Results released" : "Submitted", cls: "ok" };
  if (now < Date.parse(e.opens_at)) return { label: `Opens ${fmt(e.opens_at)}`, cls: "" };
  if (now > Date.parse(e.closes_at)) return { label: "Closed", cls: "" };
  return { label: "Open", cls: "ok", action: "Start" };
}

function renderStudentExams(data) {
  const now = Date.parse(data.serverTime);
  if (!data.exams.length) return "No exams scheduled.";
  return table(["Exam", "Window", "Duration", "Status", ""], data.exams.map((e) => {
    const s = examStatus(e, now);
    return [esc(e.title), `${fmt(e.opens_at)} → ${fmt(e.closes_at)}`, `${e.duration_minutes} min`,
      `<span class="badge ${s.cls}">${esc(s.label)}</span>`,
      s.action ? `<button data-action="open-exam" data-id="${esc(e.id)}">${s.action}</button>` : ""];
  }));
}

function renderStudentAssignments(data) {
  if (!data.assignments.length) return "No assignments yet.";
  return table(["Assignment", "Due", "My submission", "Upload"], data.assignments.map((a) => {
    let mine = '<span class="muted">Nothing submitted</span>';
    if (a.submission_status) {
      const ok = a.submission_status === "RECEIVED";
      mine = `<span class="badge ${ok ? "ok" : "err"}">${ok ? "Received" : "Rejected"}</span> ${esc(a.file_name)} (${mb(a.size_bytes)})
        <br><span class="muted">${fmt(a.received_at)}</span>${a.late ? ' <span class="badge warn">late</span>' : ""}
        ${a.reject_reason ? `<br><span class="muted">${esc(a.reject_reason)}</span>` : ""}`;
    }
    return [esc(a.title), fmt(a.due_at), mine,
      `<div class="upload"><input type="file" accept=".pdf,.zip" data-assignment="${esc(a.id)}"><span class="muted" id="up-${esc(a.id)}"></span></div>`];
  }));
}

function renderResults(data) {
  if (!data.results.length) return "No released results yet.";
  return table(["Exam", "Submitted", "Multiple choice", "Short answers", "Total"], data.results.map((r) => {
    const mcq = num(r.mcq_score) || 0, short = num(r.short_score) || 0;
    const max = (num(r.mcq_total) || 0) + (num(r.short_total) || 0);
    const shortCell = r.short_score === null
      ? (num(r.short_total) ? '<span class="muted">not marked yet</span>' : "—")
      : `${short} / ${num(r.short_total)}`;
    return [esc(r.title), fmt(r.submitted_at) + (r.auto_submitted ? ' <span class="badge">auto-submitted</span>' : ""),
      `${mcq} / ${num(r.mcq_total)}`, shortCell, `<strong>${mcq + short} / ${max}</strong>`];
  }));
}

// Walkthrough 2: presigned POST straight to S3 (the 40 MB file never touches the API).
async function uploadAssignment(input) {
  const file = input.files[0];
  const status = document.getElementById(`up-${input.dataset.assignment}`);
  if (!file) return;
  const ext = file.name.toLowerCase().split(".").pop();
  const contentType = { pdf: "application/pdf", zip: "application/zip" }[ext];
  if (!contentType) { status.textContent = "Only PDF or ZIP files are accepted."; return; }
  if (file.size > CFG.maxUploadBytes) { status.textContent = `File is larger than ${mb(CFG.maxUploadBytes)}.`; return; }
  input.disabled = true;
  try {
    status.textContent = "Requesting a secure upload link…";
    const slot = await api("POST", `/assignments/${input.dataset.assignment}/upload-url`, { fileName: file.name, contentType, size: file.size });
    const form = new FormData();
    Object.entries(slot.fields).forEach(([k, v]) => form.append(k, v));
    form.append("file", file); // must be the last field of an S3 POST
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", slot.url);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) status.textContent = `Uploading… ${Math.round((e.loaded / e.total) * 100)}%`; };
      xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error(`Storage rejected the upload (${xhr.status})`)));
      xhr.onerror = () => reject(new Error("Network error during upload"));
      xhr.send(form);
    });
    status.textContent = "Uploaded ✔ Checking the file; your receipt will arrive by email.";
    setTimeout(() => fill("assignments", api("GET", "/assignments"), renderStudentAssignments), 5000);
  } catch (e) {
    status.textContent = e.message;
    input.disabled = false;
  }
}

// ------------------------------------------------------------------ exam (walkthrough 1)
const exam = { attemptId: null, answers: {}, seq: 0, savedSeq: 0, deadline: 0, offset: 0, timers: [], saving: false, done: true, auto: false };
const localKey = () => `uniexams.attempt.${exam.attemptId}`;
const writeLocal = () => { try { localStorage.setItem(localKey(), JSON.stringify({ answers: exam.answers, seq: exam.seq })); } catch { /* storage full or blocked */ } };
const clearLocal = () => { try { localStorage.removeItem(localKey()); } catch { /* ignore */ } };
function readLocal(id) { try { return JSON.parse(localStorage.getItem(`uniexams.attempt.${id}`)); } catch { return null; } }

function setSave(text, kind) {
  const el = document.getElementById("save-status");
  if (el) { el.textContent = text; el.className = `save ${kind}`; }
}

function stopTimers() { exam.timers.forEach((t) => clearInterval(t)); exam.timers = []; window.onbeforeunload = null; }

function leaveExam() {
  if (exam.attemptId && !exam.done && exam.seq > exam.savedSeq) autosave(); // best effort; also kept locally
  stopTimers();
  exam.done = true;
}

async function studentExam(examId) {
  view('<p class="muted">Opening exam…</p>');
  let data;
  try {
    data = await api("POST", `/exams/${examId}/start`);
  } catch (e) {
    view(`<section class="card"><h2>Can't open this exam</h2><p>${esc(e.message)}</p><a href="#/">← Back to dashboard</a></section>`);
    return;
  }
  const a = data.attempt;
  Object.assign(exam, {
    attemptId: a.id, answers: a.answers || {}, seq: Number(a.client_seq || 0), savedSeq: Number(a.client_seq || 0),
    deadline: Date.parse(a.deadline), offset: Date.parse(data.serverTime) - Date.now(), saving: false, done: false, auto: false,
  });
  const local = readLocal(a.id); // recover anything typed but not saved (tab closed, Wi-Fi drop, failover)
  if (local && local.seq > exam.seq) { exam.answers = local.answers; exam.seq = local.seq; }

  view(`
    <section class="card exam-bar"><h2>${esc(data.exam.title)}</h2>
      <span class="timer" id="timer"></span><span class="save" id="save-status"></span></section>
    <form id="exam-form">${data.questions.map(renderQuestion).join("")}</form>
    <div class="actions"><button data-action="submit-exam">Submit exam</button>
      <span class="muted">Answers save automatically every 30 seconds and stay on this device if the connection drops.</span></div>`);

  if (exam.seq > exam.savedSeq) setSave("Restored unsaved answers from this device, saving…", "pending");
  else setSave(a.saved_at ? `All answers saved ${new Date(a.saved_at).toLocaleTimeString()}` : "Answers will be saved automatically", "ok");
  tick();
  exam.timers.push(setInterval(tick, 1000), setInterval(autosave, 30000));
  window.onbeforeunload = () => (exam.seq > exam.savedSeq ? "You have unsaved answers" : undefined);
  if (exam.seq > exam.savedSeq) autosave();
}

function renderQuestion(q) {
  const given = exam.answers[q.id];
  const marks = Number(q.marks);
  const body = q.kind === "MCQ"
    ? q.options.map((o, i) => `<label class="option"><input type="radio" name="q-${esc(q.id)}" value="${i}" data-q="${esc(q.id)}"${given === i ? " checked" : ""}><span>${esc(o)}</span></label>`).join("")
    : `<textarea data-q="${esc(q.id)}" maxlength="5000" placeholder="Type your answer">${esc(given ?? "")}</textarea>`;
  return `<fieldset class="q"><legend>Question ${q.position} · ${marks} mark${marks === 1 ? "" : "s"}</legend><p>${esc(q.prompt)}</p>${body}</fieldset>`;
}

function onAnswer(el) {
  if (exam.done) return;
  exam.answers[el.dataset.q] = el.type === "radio" ? Number(el.value) : el.value;
  exam.seq += 1;
  writeLocal();
  setSave("Unsaved changes…", "pending");
}

async function autosave() {
  if (exam.saving || exam.done || exam.seq <= exam.savedSeq) return;
  exam.saving = true;
  const seq = exam.seq;
  try {
    const r = await api("PUT", `/attempts/${exam.attemptId}/answers`, { answers: exam.answers, clientSeq: seq });
    exam.savedSeq = Math.max(exam.savedSeq, seq);
    if (exam.seq > exam.savedSeq) setSave("Saving newer changes…", "pending");
    else setSave(`All answers saved ${new Date(r.savedAt).toLocaleTimeString()}`, "ok");
  } catch (e) {
    if (e.status === 409) lockExam(e.message);
    else setSave(`Not saved yet (${e.message}). Your answers are kept on this device and will retry automatically.`, "error");
  } finally {
    exam.saving = false;
  }
}

function tick() {
  const el = document.getElementById("timer");
  if (!el) return;
  const left = exam.deadline - (Date.now() + exam.offset);
  if (left <= 0) {
    el.textContent = "Time is up";
    if (!exam.done) submitExam(true);
    return;
  }
  const s = Math.floor(left / 1000);
  const h = Math.floor(s / 3600);
  el.textContent = `${h ? `${h}:` : ""}${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)} left`;
  el.classList.toggle("low", left < 5 * 60 * 1000);
}

async function submitExam(auto) {
  if (exam.done) return;
  if (!auto && !confirm("Submit your exam now? You can't change your answers afterwards.")) return;
  exam.auto = exam.auto || auto;
  exam.done = true;
  stopTimers();
  setSave("Submitting…", "pending");
  try {
    await api("POST", `/attempts/${exam.attemptId}/submit`, { answers: exam.answers, clientSeq: exam.seq });
    clearLocal();
    view(`<section class="card"><h2>Exam submitted ✔</h2>
      <p>${exam.auto ? "Time ran out, so your answers were submitted automatically." : "Your answers have been submitted."}
      Multiple-choice questions are marked automatically. You'll get an email when your tutor releases results.</p>
      <a href="#/">← Back to dashboard</a></section>`);
  } catch (e) {
    if (e.status === 409) { lockExam(e.message); return; }
    exam.done = false;
    setSave(`Submit failed (${e.message}). Retrying in 10 s. Your answers are safe on this device.`, "error");
    exam.timers.push(setTimeout(() => submitExam(true), 10000));
  }
}

function lockExam(message) {
  stopTimers();
  exam.done = true;
  document.querySelectorAll("#exam-form input, #exam-form textarea, [data-action='submit-exam']").forEach((el) => { el.disabled = true; });
  setSave(message, "error");
}

// ------------------------------------------------------------------ tutor / admin
const SAMPLE_QUESTIONS = JSON.stringify([
  { kind: "MCQ", prompt: "Which AWS service stores the assignment files?", options: ["Amazon EBS", "Amazon S3", "Amazon SQS"], correctOption: 1, marks: 1 },
  { kind: "MCQ", prompt: "Which component pools database connections for Lambda?", options: ["RDS Proxy", "NAT gateway", "CloudFront"], correctOption: 0, marks: 1 },
  { kind: "SHORT", prompt: "Explain why the database is deployed Multi-AZ.", marks: 4 },
], null, 2);

function staffDashboard() {
  const isAdmin = state.me.role === "Admins";
  const now = new Date();
  view(`
    ${isAdmin ? `<section class="card"><h2>Users</h2>
      <form data-form="create-user" class="grid">
        <div><label for="u-email">Email</label><input id="u-email" name="email" type="email" required></div>
        <div><label for="u-role">Role</label><select id="u-role" name="role"><option>Students</option><option>Tutors</option><option>Admins</option></select></div>
        <div class="actions"><button>Create user</button></div>
      </form>
      <div id="users" class="table-wrap muted">Loading…</div></section>` : ""}
    <section class="card"><h2>Exams</h2><div id="exams" class="table-wrap muted">Loading…</div>
      <details><summary>Create an exam</summary>
        <form data-form="create-exam">
          <label for="e-title">Title</label><input id="e-title" name="title" required minlength="3">
          <div class="grid">
            <div><label for="e-open">Opens</label><input id="e-open" name="opensAt" type="datetime-local" required value="${toLocalInput(now)}"></div>
            <div><label for="e-close">Closes</label><input id="e-close" name="closesAt" type="datetime-local" required value="${toLocalInput(new Date(now.getTime() + 2 * 3600e3))}"></div>
            <div><label for="e-dur">Duration (minutes)</label><input id="e-dur" name="durationMinutes" type="number" min="5" max="600" value="30" required></div>
          </div>
          <label for="e-q">Questions (JSON: kind MCQ with options + correctOption index, or SHORT)</label>
          <textarea id="e-q" name="questions" rows="14">${esc(SAMPLE_QUESTIONS)}</textarea>
          <div class="actions"><button>Create exam</button></div>
        </form></details></section>
    <section class="card"><h2>Assignments</h2><div id="assignments" class="table-wrap muted">Loading…</div>
      <details><summary>Create an assignment</summary>
        <form data-form="create-assignment" class="grid">
          <div><label for="a-title">Title</label><input id="a-title" name="title" required minlength="3"></div>
          <div><label for="a-due">Due</label><input id="a-due" name="dueAt" type="datetime-local" required value="${toLocalInput(new Date(now.getTime() + 7 * 86400e3))}"></div>
          <div class="actions"><button>Create assignment</button></div>
        </form></details></section>`);
  fill("exams", api("GET", "/exams"), renderStaffExams);
  fill("assignments", api("GET", "/assignments"), renderStaffAssignments);
  if (isAdmin) fill("users", api("GET", "/admin/users"), renderUsers);
}

function renderStaffExams(data) {
  if (!data.exams.length) return "No exams yet.";
  const isAdmin = state.me.role === "Admins";
  return table(["Exam", "Window", "Duration", "Questions", "Submitted", "Results", ""], data.exams.map((e) => [
    esc(e.title), `${fmt(e.opens_at)} → ${fmt(e.closes_at)}`, `${e.duration_minutes} min`, e.question_count,
    `${e.submitted_count} / ${e.attempt_count}`,
    e.results_released ? '<span class="badge ok">Released</span>' : '<span class="badge">Not released</span>',
    `<div class="actions"><a class="button secondary" href="#/exam/${esc(e.id)}/attempts">Attempts</a>
      ${e.results_released ? "" : `<button data-action="release" data-id="${esc(e.id)}">Release results</button>`}
      ${isAdmin ? `<button class="secondary" data-action="reschedule" data-id="${esc(e.id)}" data-opens="${esc(e.opens_at)}" data-closes="${esc(e.closes_at)}" data-duration="${e.duration_minutes}">Reschedule</button>` : ""}</div>`,
  ]));
}

function renderStaffAssignments(data) {
  if (!data.assignments.length) return "No assignments yet.";
  return table(["Assignment", "Due", "Students submitted", ""], data.assignments.map((a) => [
    esc(a.title), fmt(a.due_at), a.submission_count,
    `<a class="button secondary" href="#/assignment/${esc(a.id)}">Submissions</a>`,
  ]));
}

function renderUsers(data) {
  if (!data.users.length) return "No users yet.";
  return table(["Email", "Role", "Status"], data.users.map((u) => [
    esc(u.email), esc(ROLE_LABEL[u.role] || u.role),
    `<span class="badge ${u.status === "CONFIRMED" ? "ok" : "warn"}">${esc(u.status)}</span>${u.enabled ? "" : ' <span class="badge err">disabled</span>'}`,
  ]));
}

function tutorAttempts(examId) {
  view(`<p><a href="#/">← Back</a></p>
    <section class="card"><h2>Attempts</h2>
      <p class="muted">Autosaved answers appear here while students sit the exam. <button class="secondary" data-action="refresh">Refresh</button></p>
      <div id="attempts" class="table-wrap muted">Loading…</div></section>`);
  fill("attempts", api("GET", `/exams/${examId}/attempts`), (d) => {
    if (!d.attempts.length) return "No attempts yet.";
    const shortQs = d.questions.filter((q) => q.kind === "SHORT");
    const shortMax = shortQs.reduce((sum, q) => sum + Number(q.marks), 0);
    return table(["Student", "Status", "Last autosave", "MCQ (auto)", "Short answers", `Short score / ${shortMax}`], d.attempts.map((a) => [
      esc(a.student_email),
      `<span class="badge ${a.status === "IN_PROGRESS" ? "warn" : "ok"}">${esc(a.status)}</span>${a.auto_submitted ? ' <span class="badge">auto-submitted</span>' : ""}`,
      fmt(a.saved_at),
      a.mcq_score === null ? "—" : `${num(a.mcq_score)} / ${num(a.mcq_total)}`,
      shortQs.map((q) => `<div class="muted">Q${q.position}. ${esc(q.prompt)}</div><div class="answer">${esc((a.answers || {})[q.id]) || "<em>no answer</em>"}</div>`).join("") || "—",
      a.status === "IN_PROGRESS" || !shortQs.length ? "—"
        : `<input class="score-input" type="number" min="0" max="${shortMax}" step="0.5" value="${a.short_score ?? ""}" id="score-${esc(a.id)}" aria-label="Short answer score">
           <button data-action="save-score" data-id="${esc(a.id)}">Save</button>`,
    ]));
  });
}

function tutorSubmissions(assignmentId) {
  view(`<p><a href="#/">← Back</a></p>
    <section class="card"><h2>Submissions</h2>
      <p class="muted">Download links are valid for 5 minutes. <button class="secondary" data-action="refresh">Refresh</button></p>
      <div id="subs" class="table-wrap muted">Loading…</div></section>`);
  fill("subs", api("GET", `/assignments/${assignmentId}/submissions`), (d) => {
    if (!d.submissions.length) return "No submissions yet.";
    return table(["Student", "File", "Size", "Received", "Status", ""], d.submissions.map((s) => [
      esc(s.student_email), esc(s.file_name), mb(s.size_bytes),
      fmt(s.received_at) + (s.late ? ' <span class="badge warn">late</span>' : ""),
      s.status === "RECEIVED" ? '<span class="badge ok">Received</span>' : `<span class="badge err">Rejected</span> ${esc(s.reject_reason)}`,
      s.downloadUrl ? `<a class="button secondary" href="${esc(s.downloadUrl)}">Download</a>` : "",
    ]));
  });
}

function promptDate(label, iso) {
  const answer = prompt(`${label} (YYYY-MM-DD HH:MM, your local time)`, toLocalInput(new Date(iso)).replace("T", " "));
  if (answer === null) return null;
  const d = new Date(answer.trim().replace(" ", "T"));
  if (Number.isNaN(d.getTime())) throw new Error("Invalid date");
  return d.toISOString();
}

// ------------------------------------------------------------------ event wiring
const actions = {
  login: () => login(),
  refresh: () => router(),
  "open-exam": (el) => { location.hash = `#/exam/${el.dataset.id}`; },
  "submit-exam": () => submitExam(false),
  release: async (el) => {
    if (!confirm("Release results and email every student who sat this exam?")) return;
    const r = await api("POST", `/exams/${el.dataset.id}/release`);
    banner(`Results released: ${r.notified} email(s) queued${r.stillMarking ? `, ${r.stillMarking} attempt(s) still being marked` : ""}.`, "ok");
    router();
  },
  reschedule: async (el) => {
    const opensAt = promptDate("Opens", el.dataset.opens);
    if (!opensAt) return;
    const closesAt = promptDate("Closes", el.dataset.closes);
    if (!closesAt) return;
    const durationMinutes = Number(prompt("Duration in minutes", el.dataset.duration));
    await api("PUT", `/exams/${el.dataset.id}/schedule`, { opensAt, closesAt, durationMinutes });
    banner("Schedule updated", "ok");
    router();
  },
  "save-score": async (el) => {
    const value = document.getElementById(`score-${el.dataset.id}`).value;
    await api("PUT", `/attempts/${el.dataset.id}/short-score`, { shortScore: Number(value) });
    banner("Score saved", "ok");
  },
};

const forms = {
  "create-exam": async (f) => {
    let questions;
    try { questions = JSON.parse(field(f, "questions")); } catch { throw new Error("Questions must be valid JSON"); }
    await api("POST", "/exams", {
      title: field(f, "title"), opensAt: fromLocalInput(field(f, "opensAt")), closesAt: fromLocalInput(field(f, "closesAt")),
      durationMinutes: Number(field(f, "durationMinutes")), questions,
    });
    banner("Exam created", "ok");
    router();
  },
  "create-assignment": async (f) => {
    await api("POST", "/assignments", { title: field(f, "title"), dueAt: fromLocalInput(field(f, "dueAt")) });
    banner("Assignment created", "ok");
    router();
  },
  "create-user": async (f) => {
    const r = await api("POST", "/admin/users", { email: field(f, "email"), role: field(f, "role") });
    banner(`Created ${r.email}. Cognito has emailed them a temporary password.`, "ok");
    router();
  },
};

app.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-action]");
  if (!el || el.disabled) return;
  ev.preventDefault();
  const handler = actions[el.dataset.action];
  if (handler) Promise.resolve().then(() => handler(el)).catch(showError);
});

app.addEventListener("submit", (ev) => {
  const handler = forms[ev.target.dataset.form];
  if (!handler) return;
  ev.preventDefault();
  const button = ev.target.querySelector("button");
  if (button) button.disabled = true;
  Promise.resolve().then(() => handler(ev.target)).catch(showError).finally(() => { if (button && button.isConnected) button.disabled = false; });
});

app.addEventListener("input", (ev) => { if (ev.target.dataset && ev.target.dataset.q) onAnswer(ev.target); });
app.addEventListener("change", (ev) => { if (ev.target.matches("input[type=file][data-assignment]")) uploadAssignment(ev.target); });

// ------------------------------------------------------------------ start
async function main() {
  if (!CFG.clientId) {
    view('<section class="card"><h2>Not configured</h2><p>config.js is missing. Run <code>scripts/deploy-frontend.sh</code>.</p></section>');
    return;
  }
  try { await handleRedirect(); } catch (e) { showError(e); }
  if (!loadTokens()) {
    view(`<section class="card"><h2>Online exams &amp; assignment submissions</h2>
      <p>Sign in with your university account to sit exams, upload assignments and see your results.</p>
      <button data-action="login">Sign in</button></section>`);
    return;
  }
  try {
    state.me = await api("GET", "/me");
  } catch (e) {
    view(`<section class="card"><h2>Couldn't load your profile</h2><p>${esc(e.message)}</p><button data-action="login">Sign in again</button></section>`);
    return;
  }
  renderUser();
  window.addEventListener("hashchange", router);
  window.addEventListener("online", () => autosave());
  router();
}

main();
