// Sign-in and sign-up pages. Sign in with Google is the only way in: no
// passwords to reset, leak or reuse, and no sign-in emails to go missing.
// Google sends the browser back via the API, which lands here with a
// one-time code in the URL fragment (#token=...), never sent to a server.

import { API_BASE, request } from "../../shared/api.js";
import { brandLockup, h, mount } from "../../shared/dom.js";
import { agreementSigner, loadAgreement } from "./agreement.js";
import { getToken, languagePicker, setToken } from "./core.js";
import { reportErrors } from "../../shared/report-errors.js";
import { T } from "./i18n.js";

reportErrors("signin");

const root = document.getElementById("auth");
const page = root.dataset.page;
const hashParams = new URLSearchParams(location.hash.slice(1));

function frame(...children) {
  mount(root,
    h("div", { class: "auth-brand" }, brandLockup({ tagline: true, href: "/" }), languagePicker()),
    ...children);
}

function safeNext(v) {
  return v && v.startsWith("#/") ? v : null;
}

function nextHash() {
  return safeNext(hashParams.get("next")) || safeNext(new URLSearchParams(location.search).get("next")) || "#/";
}

// Login CSRF guard: a random value this tab keeps while it's away at
// Google. The code that comes back only works together with it, so a
// sign-in link someone else made (for *their* account) does nothing here.
const NONCE_KEY = "ar.login_nonce";

function newNonce() {
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  const nonce = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  try {
    sessionStorage.setItem(NONCE_KEY, nonce);
  } catch {
    // Private mode without storage: the sign-in will ask to start again.
  }
  return nonce;
}

function takeNonce() {
  try {
    const n = sessionStorage.getItem(NONCE_KEY);
    sessionStorage.removeItem(NONCE_KEY);
    return n;
  } catch {
    return null;
  }
}

function withNonce(url) {
  return `${url}${url.includes("?") ? "&" : "?"}nonce=${newNonce()}`;
}

function googleButton(label, href) {
  return h("a", {
    class: "btn btn-google btn-lg",
    href,
    onclick: (e) => {
      e.preventDefault();
      location.assign(withNonce(href));
    },
  }, h("img", { src: "/assets/brand/google-g.svg", alt: "", width: "18", height: "18" }), label);
}

// Error codes from the Google callback. Only these texts are shown: the
// page never displays wording taken from its own address, which anyone
// could write ("…your account is locked, call…").
function errorMessage(code, email) {
  const who = email || T("this Google account");
  return {
    cancelled: T("Sign-in was cancelled."),
    state_mismatch: T("That sign-in didn't start in this browser. Start again from the sign-in page."),
    expired: T("That sign-in took too long or was already used. Try again."),
    google_failed: T("Google couldn't complete the sign-in. Try again in a moment."),
    email_unverified: T("Your Google account's email isn't verified yet."),
    account_exists: T("{p0} already has an Autorack account. Sign in instead.", { p0: who }),
    no_account: T("No Autorack account uses {p0}. Ask whoever runs your warehouse to invite this address, or start a free trial.", { p0: who }),
    account_disabled: T("This account is disabled."),
    account_mismatch: T("{p0} is linked to a different Google account. Sign in with that one, or contact us to reset it.", { p0: who }),
    google_not_configured: T("Sign in with Google isn't set up on this server yet."),
  }[code] || T("Sign-in didn't work. Try again.");
}

function cleanEmail(v) {
  return v && /^[^\s@<>"']{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$/.test(v) ? v : null;
}

function startUrl() {
  const next = safeNext(new URLSearchParams(location.search).get("next"));
  return `${API_BASE}/api/auth/google/start${next ? `?next=${encodeURIComponent(next)}` : ""}`;
}

const NO_GOOGLE_HELP = [
  T("Your work email doesn't need to be Gmail.") + " ",
  h("a", { href: "https://accounts.google.com/signup", target: "_blank", rel: "noopener" }, T("Create a Google account")),
  " " + T("with your existing email (choose “use my current email address instead”); it takes a minute."),
];

async function verify(token) {
  frame(h("h1", null, T("Signing you in…")), h("div", { class: "skeleton" }));
  const next = nextHash();
  history.replaceState(null, "", location.pathname); // drop the code from the address bar
  try {
    const r = await request("/api/auth/verify", { method: "POST", body: { token, nonce: takeNonce() } });
    setToken(r.token);
    location.replace(`/app/${next}`);
  } catch (e) {
    loginForm({ error: e.message });
  }
}

function loginForm({ error = null, email = null } = {}) {
  frame(
    h("h1", null, T("Sign in")),
    h("p", { class: "muted" }, T("Owners, managers and supervisors sign in with the Google account for their work email.")),
    error ? h("div", { class: "banner banner-bad", role: "alert" }, error) : null,
    error && email ? h("p", { class: "muted small" }, T("Signed in to Google as") + " ", h("strong", null, email), T(". Wrong account? Choose another when Google asks.")) : null,
    googleButton(T("Sign in with Google"), startUrl()),
    h("p", { class: "muted small" }, ...NO_GOOGLE_HELP),
    h("p", { class: "muted small" }, T("New to Autorack?") + " ", h("a", { href: "/app/signup.html" }, T("Start a free trial"))),
    h("p", { class: "muted small" }, T("Scanning on a phone? Open") + " ", h("a", { href: "/w/" }, T("the scanner app")), T(": workers use a PIN, not Google.")));
}

function signupForm(saved = {}) {
  root.classList.remove("auth-card-wide");
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const name = h("input", { class: "input", id: "wh", required: true, maxlength: "200", placeholder: T("e.g. Dockside Distribution"), value: saved.warehouse_name || "" });
  const btn = h("button", { class: "btn btn-primary btn-lg", type: "submit" }, T("Continue"));
  frame(
    h("p", { class: "auth-step" }, T("Step 1 of 3")),
    h("h1", null, T("Start your free trial")),
    h("p", { class: "muted" }, T("14 days free, no card needed. Then $290/year or $29/month per warehouse, flat.")),
    h("p", { class: "founding-callout small" }, h("strong", null, T("Founding customer price.")), " ",
      T("Sign up now and your price is locked in for as long as you stay subscribed.")),
    h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        signAgreementStep({ warehouse_name: name.value.trim(), timezone: tz });
      },
    },
    h("label", { for: "wh" }, T("Warehouse name")), name,
    h("p", { class: "muted small" }, T("Timezone: {tz} (change it later in Settings).", { tz })),
    btn),
    h("p", { class: "muted small" }, T("Next you'll read and sign our license agreement, then sign in with Google.") + " ",
      h("a", { href: "/privacy.html", target: "_blank", rel: "noopener" }, T("Privacy policy"))),
    h("p", { class: "muted small" }, T("Already have an account?") + " ", h("a", { href: "/app/login.html" }, T("Sign in"))));
  name.focus();
}

async function signAgreementStep(account) {
  root.classList.add("auth-card-wide");
  frame(h("h1", null, T("Loading the agreement…")), h("div", { class: "skeleton" }));
  let info;
  try {
    info = await loadAgreement();
  } catch (e) {
    frame(h("h1", null, T("Couldn't load the agreement")), h("p", { class: "banner banner-bad" }, e.message),
      h("button", { class: "btn", onclick: () => signupForm(account) }, T("Back")));
    return;
  }
  frame(
    h("p", { class: "auth-step" }, T("Step 2 of 3")),
    h("h1", null, T("Read and sign the license agreement")),
    h("p", { class: "muted" }, T("Your account for {warehouse_name} is created when you sign and then sign in with Google.", { warehouse_name: account.warehouse_name }) + " ",
      T("Take your time; you can open the PDF in a new tab to read it full-size.")),
    agreementSigner({
      info,
      timeZone: account.timezone,
      submitLabel: T("Sign and continue with Google"),
      onBack: () => signupForm(account),
      onSubmit: async (details) => {
        const r = await request("/api/auth/signup", { method: "POST", body: { ...account, ...details } });
        root.classList.remove("auth-card-wide");
        frame(
          h("p", { class: "auth-step" }, T("Step 3 of 3")),
          h("h1", null, T("Signed. Now sign in with Google")),
          h("p", null, T("Use the Google account for the email you'll run") + " ", h("strong", null, account.warehouse_name), " " + T("with. That's your sign-in from now on.")),
          googleButton(T("Continue with Google"), r.redirect),
          h("p", { class: "muted small" }, ...NO_GOOGLE_HELP));
        location.assign(withNonce(r.redirect));
      },
    }));
  window.scrollTo(0, 0);
}

// A code arriving in a tab that already shows this page only changes the
// #fragment, which doesn't reload it.
window.addEventListener("hashchange", () => {
  const t = new URLSearchParams(location.hash.slice(1)).get("token");
  if (t) verify(t);
});

const token = hashParams.get("token");
const error = hashParams.get("error");
if (token) verify(token);
else if (error) {
  history.replaceState(null, "", location.pathname + location.search);
  const email = cleanEmail(hashParams.get("email"));
  loginForm({ error: errorMessage(error, email), email });
} else if (getToken() && page === "login") location.replace(`/app/${nextHash()}`);
else if (page === "signup") signupForm();
else loginForm();
