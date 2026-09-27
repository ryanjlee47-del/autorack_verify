// Sign-in and sign-up pages. Sign in with Google is the only way in: no
// passwords to reset, leak or reuse, and no sign-in emails to go missing.
// Google sends the browser back via the API, which lands here with a
// one-time code in the URL fragment (#token=...), never sent to a server.

import { API_BASE, request } from "../../shared/api.js";
import { brandLockup, h, mount } from "../../shared/dom.js";
import { agreementSigner, loadAgreement } from "./agreement.js";
import { getToken, setToken } from "./core.js";
import { reportErrors } from "../../shared/report-errors.js";

reportErrors("signin");

const root = document.getElementById("auth");
const page = root.dataset.page;
const hashParams = new URLSearchParams(location.hash.slice(1));

function frame(...children) {
  mount(root,
    h("div", { class: "auth-brand" }, brandLockup({ tagline: true, href: "/" })),
    ...children);
}

function safeNext(v) {
  return v && v.startsWith("#/") ? v : null;
}

function nextHash() {
  return safeNext(hashParams.get("next")) || safeNext(new URLSearchParams(location.search).get("next")) || "#/";
}

function googleButton(label, href) {
  return h("a", { class: "btn btn-google btn-lg", href },
    h("img", { src: "/assets/brand/google-g.svg", alt: "", width: "18", height: "18" }), label);
}

function startUrl() {
  const next = safeNext(new URLSearchParams(location.search).get("next"));
  return `${API_BASE}/api/auth/google/start${next ? `?next=${encodeURIComponent(next)}` : ""}`;
}

const NO_GOOGLE_HELP = [
  "Your work email doesn't need to be Gmail. ",
  h("a", { href: "https://accounts.google.com/signup", target: "_blank", rel: "noopener" }, "Create a Google account"),
  " with your existing email (choose “use my current email address instead”); it takes a minute.",
];

async function verify(token) {
  frame(h("h1", null, "Signing you in…"), h("div", { class: "skeleton" }));
  const next = nextHash();
  history.replaceState(null, "", location.pathname); // drop the code from the address bar
  try {
    const r = await request("/api/auth/verify", { method: "POST", body: { token } });
    setToken(r.token);
    location.replace(`/app/${next}`);
  } catch (e) {
    loginForm({ error: e.message });
  }
}

function loginForm({ error = null, email = null } = {}) {
  frame(
    h("h1", null, "Sign in"),
    h("p", { class: "muted" }, "Owners, managers and supervisors sign in with the Google account for their work email."),
    error ? h("div", { class: "banner banner-bad", role: "alert" }, error) : null,
    error && email ? h("p", { class: "muted small" }, "Signed in to Google as ", h("strong", null, email), ". Wrong account? Choose another when Google asks.") : null,
    googleButton("Sign in with Google", startUrl()),
    h("p", { class: "muted small" }, ...NO_GOOGLE_HELP),
    h("p", { class: "muted small" }, "New to Autorack? ", h("a", { href: "/app/signup.html" }, "Start a free trial")),
    h("p", { class: "muted small" }, "Scanning on a phone? Open ", h("a", { href: "/w/" }, "the scanner app"), ": workers use a PIN, not Google."));
}

function signupForm(saved = {}) {
  root.classList.remove("auth-card-wide");
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const name = h("input", { class: "input", id: "wh", required: true, maxlength: "200", placeholder: "e.g. Dockside Distribution", value: saved.warehouse_name || "" });
  const btn = h("button", { class: "btn btn-primary btn-lg", type: "submit" }, "Continue");
  frame(
    h("p", { class: "auth-step" }, "Step 1 of 3"),
    h("h1", null, "Start your free trial"),
    h("p", { class: "muted" }, "14 days free, no card needed. Then $175/month per warehouse, flat."),
    h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        signAgreementStep({ warehouse_name: name.value.trim(), timezone: tz });
      },
    },
    h("label", { for: "wh" }, "Warehouse name"), name,
    h("p", { class: "muted small" }, `Timezone: ${tz} (change it later in Settings).`),
    btn),
    h("p", { class: "muted small" }, "Next you'll read and sign our license agreement, then sign in with Google. ",
      h("a", { href: "/privacy.html", target: "_blank", rel: "noopener" }, "Privacy policy")),
    h("p", { class: "muted small" }, "Already have an account? ", h("a", { href: "/app/login.html" }, "Sign in")));
  name.focus();
}

async function signAgreementStep(account) {
  root.classList.add("auth-card-wide");
  frame(h("h1", null, "Loading the agreement…"), h("div", { class: "skeleton" }));
  let info;
  try {
    info = await loadAgreement();
  } catch (e) {
    frame(h("h1", null, "Couldn't load the agreement"), h("p", { class: "banner banner-bad" }, e.message),
      h("button", { class: "btn", onclick: () => signupForm(account) }, "Back"));
    return;
  }
  frame(
    h("p", { class: "auth-step" }, "Step 2 of 3"),
    h("h1", null, "Read and sign the license agreement"),
    h("p", { class: "muted" }, `Your account for ${account.warehouse_name} is created when you sign and then sign in with Google. `,
      "Take your time; you can open the PDF in a new tab to read it full-size."),
    agreementSigner({
      info,
      timeZone: account.timezone,
      submitLabel: "Sign and continue with Google",
      onBack: () => signupForm(account),
      onSubmit: async (details) => {
        const r = await request("/api/auth/signup", { method: "POST", body: { ...account, ...details } });
        root.classList.remove("auth-card-wide");
        frame(
          h("p", { class: "auth-step" }, "Step 3 of 3"),
          h("h1", null, "Signed. Now sign in with Google"),
          h("p", null, "Use the Google account for the email you'll run ", h("strong", null, account.warehouse_name), " with. That's your sign-in from now on."),
          googleButton("Continue with Google", r.redirect),
          h("p", { class: "muted small" }, ...NO_GOOGLE_HELP));
        location.assign(r.redirect);
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
  loginForm({ error: hashParams.get("message") || "Sign-in didn't work. Try again.", email: hashParams.get("email") });
} else if (getToken() && page === "login") location.replace(`/app/${nextHash()}`);
else if (page === "signup") signupForm();
else loginForm();
