// The license agreement, read and signed in the browser.
//
// The pages are images of the real PDF. As the signer types, their company,
// name and title appear in the agreement's blanks (the same places the
// server stamps them into the signed PDF). Signing unlocks only after the
// reader has scrolled to the last page and ticked the confirmation.

import { request } from "../../shared/api.js";
import { h, mount } from "../../shared/dom.js";
import { T } from "./i18n.js";

/** Shrink a blank's text until it fits, as the server does in the PDF. */
function fitText(span) {
  if (!span.dataset.base) span.dataset.base = span.style.fontSize;
  const base = parseFloat(span.dataset.base);
  let scale = 1;
  span.style.fontSize = span.dataset.base;
  while (span.scrollWidth > span.clientWidth + 1 && scale > 0.45) {
    scale -= 0.05;
    span.style.fontSize = `${(base * scale).toFixed(3)}cqw`;
  }
}

export function loadAgreement() {
  return request("/api/legal/agreement");
}

function todayIn(timeZone) {
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone, month: "long", day: "numeric", year: "numeric" }).format(new Date());
  } catch {
    return new Intl.DateTimeFormat("en-US", { month: "long", day: "numeric", year: "numeric" }).format(new Date());
  }
}

/**
 * info: GET /api/legal/agreement
 * prefill: {company_name, company_address, signer_name, signer_title}
 * timeZone: for the effective date shown on page 1
 * submitLabel, onSubmit(details) -> Promise (rejections are shown inline)
 */
export function agreementSigner({ info, prefill = {}, timeZone, submitLabel = T("Sign agreement"), onSubmit, onBack = null }) {
  const started = Date.now();
  const [pw, ph] = info.page_size;
  const overlays = {}; // field key -> [span, ...]
  let reachedEnd = false;

  const pages = info.pages.map((src, i) => {
    const number = i + 1;
    const layer = h("div", { class: "agr-layer", "aria-hidden": "true" });
    for (const [key, spec] of Object.entries(info.fields)) {
      const [page, x0, y0, x1, y1] = spec.box;
      if (page !== number) continue;
      const span = h("span", {
        class: ["agr-field", spec.script && "agr-script", spec.mode === "cover" && "agr-cover"],
        style: {
          left: `${(x0 / pw) * 100}%`,
          top: `${(y0 / ph) * 100}%`,
          width: `${((x1 - x0) / pw) * 100}%`,
          height: `${((y1 - y0) / ph) * 100}%`,
          fontSize: `${((spec.size || 10) / pw) * 100}cqw`,
        },
      });
      (overlays[key] = overlays[key] || []).push(span);
      layer.appendChild(span);
    }
    return h("div", { class: "agr-page", style: { aspectRatio: `${pw} / ${ph}` } },
      h("img", { src, alt: T("Agreement page {number} of {length}", { number, length: info.pages.length }), loading: number <= 2 ? "eager" : "lazy", draggable: "false" }),
      layer);
  });

  const endMarker = h("div", { class: "agr-end" }, T("End of agreement"));
  const doc = h("div", { class: "agr-doc", tabindex: "0", "aria-label": T("License agreement") }, ...pages, endMarker);
  const progress = h("div", { class: "agr-progress" });

  const field = (id, label, value, attrs = {}) => {
    const input = h("input", { class: "input", id, value: value || "", maxlength: "300", autocomplete: "off", ...attrs });
    return [h("label", { for: id }, label), input];
  };
  const [companyL, company] = field("agr-company", T("Company legal name (the Client)"), prefill.company_name, { autocomplete: "organization" });
  const [addressL, address] = field("agr-address", T("Company address"), prefill.company_address, { autocomplete: "street-address", maxlength: "500" });
  const [nameL, name] = field("agr-name", T("Your full legal name — typing it is your signature"), prefill.signer_name, { autocomplete: "name", maxlength: "200" });
  const [titleL, title] = field("agr-title", T("Your title"), prefill.signer_title, { placeholder: T("e.g. Owner, CEO, Operations Manager"), maxlength: "200" });
  const sigPreview = h("div", { class: "agr-sig-preview", "aria-live": "polite" });
  const agree = h("input", { type: "checkbox", id: "agr-agree" });
  const err = h("p", { class: "form-error", role: "alert" });
  const btn = h("button", { class: "btn btn-primary btn-lg", type: "submit", disabled: true }, submitLabel);
  const lockNote = h("p", { class: "agr-lock muted small" }, T("Scroll through the whole agreement above to sign."));

  const countersigner = info.countersigner || {};
  const paint = () => {
    const values = {
      effective_date: todayIn(timeZone),
      company_name: company.value.trim(),
      company_address: address.value.trim(),
      client_signature: name.value.trim(),
      client_name: name.value.trim(),
      client_title: title.value.trim(),
      autorack_signature: countersigner.name || "",
      autorack_name: countersigner.name || "",
      autorack_title: countersigner.title || "",
    };
    for (const [key, spans] of Object.entries(overlays)) {
      for (const s of spans) {
        s.textContent = values[key] || "";
        // Highlight only the blanks the signer fills; Autorack's side is ours.
        s.classList.toggle("agr-empty", !values[key] && !key.startsWith("autorack_"));
        fitText(s);
      }
    }
    sigPreview.textContent = name.value.trim() || T("Your signature");
    sigPreview.classList.toggle("agr-empty", !name.value.trim());
    const ready = reachedEnd && agree.checked && [company, address, name, title].every((i) => i.value.trim());
    btn.disabled = !ready;
    lockNote.hidden = reachedEnd;
    agree.disabled = !reachedEnd;
  };

  const onScroll = () => {
    const max = doc.scrollHeight - doc.clientHeight;
    const frac = max > 0 ? doc.scrollTop / max : 1;
    const page = Math.min(info.pages.length, Math.floor(frac * info.pages.length) + 1);
    if (!reachedEnd && doc.scrollTop + doc.clientHeight >= doc.scrollHeight - 24) {
      reachedEnd = true;
      paint();
    }
    progress.textContent = reachedEnd
      ? T("You've reached the end. Fill in the details below to sign.")
      : T("Page {page} of {length} · scroll to the end to sign", { page, length: info.pages.length });
    progress.classList.toggle("agr-progress-done", reachedEnd);
  };
  doc.addEventListener("scroll", onScroll, { passive: true });
  for (const el of [company, address, name, title]) el.addEventListener("input", paint);
  agree.addEventListener("change", paint);

  const form = h("form", {
    class: "agr-sign",
    onsubmit: async (e) => {
      e.preventDefault();
      if (btn.disabled) return;
      btn.disabled = true;
      err.textContent = "";
      try {
        await onSubmit({
          company_name: company.value.trim(),
          company_address: address.value.trim(),
          signer_name: name.value.trim(),
          signer_title: title.value.trim(),
          agreement_version: info.version,
          accept_agreement: agree.checked,
          viewed_seconds: Math.round((Date.now() - started) / 1000),
        });
      } catch (ex) {
        err.textContent = ex.message || String(ex);
        paint();
      }
    },
  },
  h("div", { class: "agr-grid" }, h("div", null, companyL, company), h("div", null, addressL, address),
    h("div", null, nameL, name), h("div", null, titleL, title)),
  sigPreview,
  lockNote,
  h("label", { class: "row check agr-consent", for: "agr-agree" }, agree, h("span", null, info.consent_text)),
  err,
  h("div", { class: "row agr-actions" },
    onBack ? h("button", { class: "btn btn-lg", type: "button", onclick: onBack }, T("Back")) : null,
    btn));

  const root = h("div", { class: "agr" },
    h("div", { class: "row-between agr-head" },
      h("div", null, h("strong", null, info.title), h("span", { class: "muted small" }, " " + T("· version {version}", { version: info.version }))),
      h("a", { class: "btn btn-sm", href: info.pdf_url, target: "_blank", rel: "noopener" }, T("Open PDF"))),
    progress,
    doc,
    form);
  paint();
  // Widths are only known once the pages are laid out.
  for (const img of doc.querySelectorAll("img")) img.addEventListener("load", paint, { once: true });
  window.addEventListener("resize", paint);
  // A short agreement on a tall screen may not scroll at all.
  requestAnimationFrame(onScroll);
  return root;
}

/** Full-page gate inside the dashboard, for warehouses that haven't signed. */
export function agreementGate(host, { me, info, onSigned, onSignOut, onSwitch, api }) {
  const wh = me.warehouse;
  if (!me.agreement.can_sign) {
    mount(host, h("div", { class: "agr-gate" },
      h("h1", null, T("Waiting on the license agreement")),
      h("p", { class: "muted" }, T("An owner of {name} needs to sign the Autorack license agreement before the dashboard can be used. Ask them to sign in; it takes a few minutes.", { name: wh.name })),
      h("div", { class: "row" },
        ...me.warehouses.filter((w) => w.id !== wh.id).map((w) =>
          h("button", { class: "btn", onclick: () => onSwitch(w.id) }, `Go to ${w.name}`)),
        h("button", { class: "btn", onclick: onSignOut }, T("Sign out")))));
    return;
  }
  mount(host, h("div", { class: "agr-gate" },
    h("h1", null, T("Sign the license agreement for {name}", { name: wh.name })),
    h("p", { class: "muted" }, T("Before you keep using Autorack, please read our license agreement and sign it. Your phones keep scanning in the meantime.")),
    agreementSigner({
      info,
      timeZone: wh.timezone,
      prefill: { signer_name: me.user.name || "" },
      submitLabel: T("Sign and continue"),
      onSubmit: async (details) => {
        await api("/api/agreement/sign", { method: "POST", body: details });
        onSigned();
      },
    })));
}
