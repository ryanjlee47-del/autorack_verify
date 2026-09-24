// Sign-in and sign-up pages. Sign-in is by emailed link only: no passwords
// to reset, leak, or reuse. The link's token arrives in the URL fragment
// (#token=...), which browsers never send to any server.

import { request } from "../../shared/api.js";
import { brandMark, h, mount } from "../../shared/dom.js";
import { getToken, setToken } from "./core.js";

const root = document.getElementById("auth");
const page = root.dataset.page;

function frame(...children) {
  mount(root,
    h("a", { class: "auth-brand", href: "/" }, brandMark(), h("span", null, "Autorack")),
    ...children);
}

function sent(email) {
  frame(
    h("h1", null, "Check your email"),
    h("p", null, "We sent a sign-in link to ", h("strong", null, email), ". It works once and expires in 15 minutes."),
    h("p", { class: "muted small" }, "Nothing arrived? Check spam, or ", h("a", { href: "/app/login.html" }, "request another link"), "."));
}

function nextHash() {
  const next = new URLSearchParams(location.search).get("next");
  return next && next.startsWith("#/") ? next : "#/";
}

async function verify(token) {
  frame(h("h1", null, "Signing you in…"), h("div", { class: "skeleton" }));
  history.replaceState(null, "", location.pathname); // drop the token from the address bar
  try {
    const r = await request("/api/auth/verify", { method: "POST", body: { token } });
    setToken(r.token);
    location.replace(`/app/${nextHash()}`);
  } catch (e) {
    frame(
      h("h1", null, "That link didn't work"),
      h("p", { class: "banner banner-bad" }, e.message),
      h("a", { class: "btn btn-primary", href: "/app/login.html" }, "Get a new link"));
  }
}

function loginForm() {
  const email = h("input", { class: "input", type: "email", id: "email", required: true, autocomplete: "email", placeholder: "you@company.com" });
  const err = h("p", { class: "form-error", role: "alert" });
  const btn = h("button", { class: "btn btn-primary btn-lg", type: "submit" }, "Email me a sign-in link");
  frame(
    h("h1", null, "Sign in"),
    h("p", { class: "muted" }, "We'll email you a link. No password needed."),
    h("form", {
      class: "stack",
      onsubmit: async (e) => {
        e.preventDefault();
        btn.disabled = true;
        err.textContent = "";
        try {
          await request("/api/auth/magic-link", { method: "POST", body: { email: email.value } });
          sent(email.value);
        } catch (ex) {
          err.textContent = ex.message;
          btn.disabled = false;
        }
      },
    }, h("label", { for: "email" }, "Work email"), email, err, btn),
    h("p", { class: "muted small" }, "New to Autorack? ", h("a", { href: "/app/signup.html" }, "Start a free trial")),
    h("p", { class: "muted small" }, "Setting up a phone for scanning? Open ", h("a", { href: "/w/" }, "the scanner app"), " instead."));
  email.focus();
}

function signupForm() {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const name = h("input", { class: "input", id: "wh", required: true, maxlength: "200", placeholder: "e.g. Dockside Distribution" });
  const email = h("input", { class: "input", type: "email", id: "email", required: true, autocomplete: "email", placeholder: "you@company.com" });
  const err = h("p", { class: "form-error", role: "alert" });
  const btn = h("button", { class: "btn btn-primary btn-lg", type: "submit" }, "Start free trial");
  frame(
    h("h1", null, "Start your free trial"),
    h("p", { class: "muted" }, "14 days free, no card needed. Then $175/month per warehouse, flat."),
    h("form", {
      class: "stack",
      onsubmit: async (e) => {
        e.preventDefault();
        btn.disabled = true;
        err.textContent = "";
        try {
          await request("/api/auth/signup", { method: "POST", body: { warehouse_name: name.value, email: email.value, timezone: tz } });
          sent(email.value);
        } catch (ex) {
          err.textContent = ex.message;
          btn.disabled = false;
        }
      },
    },
    h("label", { for: "wh" }, "Warehouse name"), name,
    h("label", { for: "email" }, "Your work email"), email,
    h("p", { class: "muted small" }, `Timezone: ${tz} (change it later in Settings).`),
    err, btn),
    h("p", { class: "muted small" }, "Already have an account? ", h("a", { href: "/app/login.html" }, "Sign in")));
  name.focus();
}

const token = new URLSearchParams(location.hash.slice(1)).get("token");
if (token) verify(token);
else if (getToken() && page === "login") location.replace(`/app/${nextHash()}`);
else if (page === "signup") signupForm();
else loginForm();
