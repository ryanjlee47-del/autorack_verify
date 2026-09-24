// Workers (PINs, performance) and phones (linking, revoking).

import { confirmDialog, dialog, fmtAgo, fmtNumber, fmtPercent, h, svg, toast } from "../../../shared/dom.js";
import { api, card, fail, isOwner, layout, pageHeader, table } from "../core.js";

function showPin(name, pin) {
  return dialog(`PIN for ${name}`, (close) => [
    h("p", null, "Give this PIN to ", h("strong", null, name), ". It's shown only once. You can reset it any time."),
    h("div", { class: "pin-reveal", "aria-label": `PIN ${pin.split("").join(" ")}` }, pin),
    h("div", { class: "dialog-actions" }, h("button", { class: "btn btn-primary", onclick: () => close(true) }, "Done")),
  ]);
}

async function addWorker(reload) {
  const result = await dialog("Add worker", (close) => {
    const name = h("input", { class: "input", placeholder: "Name as it should appear on reports", maxlength: "100", required: true });
    const pin = h("input", { class: "input mono", placeholder: "Leave blank to generate", inputmode: "numeric", maxlength: "4", pattern: "[0-9]{4}" });
    return h("form", {
      class: "stack",
      onsubmit: (e) => {
        e.preventDefault();
        close({ name: name.value.trim(), pin: pin.value.trim() || null });
      },
    },
    h("label", null, "Name"), name,
    h("label", null, "4-digit PIN"), pin,
    h("p", { class: "muted small" }, "PINs are unique within this warehouse. A generated PIN avoids easy guesses like 1234."),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
      h("button", { class: "btn btn-primary", type: "submit" }, "Add worker")));
  });
  if (!result) return;
  try {
    const w = await api("/api/workers", { method: "POST", body: result });
    await showPin(w.name, w.pin);
    reload();
  } catch (e) {
    fail(e);
  }
}

export async function workersView(params) {
  const days = Number(params.get("days") || 7);
  const data = await api(`/api/workers?days=${days}`);
  const reload = () => workersView(params).catch(fail);

  const rowActions = (w) => h("div", { class: "row nowrap" },
    w.active ? h("button", {
      class: "btn btn-sm",
      onclick: async (e) => {
        e.stopPropagation();
        if (!(await confirmDialog("Reset PIN?", `${w.name} will be signed out and needs the new PIN.`, { confirmLabel: "Reset PIN" }))) return;
        try {
          const r = await api(`/api/workers/${w.worker_id}/reset-pin`, { method: "POST", body: {} });
          await showPin(r.name, r.pin);
        } catch (err) {
          fail(err);
        }
      },
    }, "Reset PIN") : null,
    h("button", {
      class: "btn btn-sm",
      onclick: async (e) => {
        e.stopPropagation();
        const name = await dialog("Rename worker", (close) => {
          const input = h("input", { class: "input", value: w.name, maxlength: "100" });
          return h("form", { class: "stack", onsubmit: (ev) => { ev.preventDefault(); close(input.value.trim()); } }, input,
            h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
              h("button", { class: "btn btn-primary", type: "submit" }, "Save")));
        });
        if (name) await api(`/api/workers/${w.worker_id}`, { method: "PATCH", body: { name } }).then(reload, fail);
      },
    }, "Rename"),
    h("button", {
      class: "btn btn-sm btn-ghost",
      onclick: async (e) => {
        e.stopPropagation();
        if (w.active) {
          if (!(await confirmDialog("Deactivate worker?", `${w.name} is signed out and their PIN stops working. Their history is kept.`, { confirmLabel: "Deactivate", danger: true }))) return;
          await api(`/api/workers/${w.worker_id}`, { method: "PATCH", body: { active: false } }).then(reload, fail);
        } else {
          try {
            const r = await api(`/api/workers/${w.worker_id}`, { method: "PATCH", body: { active: true } });
            if (r.pin) await showPin(r.name, r.pin);
            reload();
          } catch (err) {
            fail(err);
          }
        }
      },
    }, w.active ? "Deactivate" : "Reactivate"));

  const workers = data.workers.slice().sort((a, b) => (b.active - a.active) || a.name.localeCompare(b.name));
  layout("#/workers", [
    pageHeader("Workers", "Everyone who signs in on a phone with a PIN.",
      h("select", {
        class: "input input-inline",
        onchange: (e) => { location.hash = `#/workers?days=${e.target.value}`; },
      }, ...[7, 30, 90].map((d) => h("option", { value: String(d), selected: d === days }, `Last ${d} days`))),
      h("button", { class: "btn btn-primary", onclick: () => addWorker(reload) }, "Add worker")),
    card(null,
      h("p", { class: "muted small" }, `Warehouse mistake rate over ${days} days: `, h("strong", null, fmtPercent(data.warehouse_error_rate)),
        ". A worker is highlighted when their rate is at least double that (and 3 points higher) over 30+ scans."),
      table([
        {
          label: "Name",
          render: (w) => h("span", { class: "row nowrap" }, h("strong", null, w.name),
            !w.active ? h("span", { class: "badge" }, "inactive") : null,
            w.needs_attention ? h("span", { class: "badge badge-warn" }, "check in") : null),
        },
        { label: "Scans", align: "right", render: (w) => fmtNumber(w.scans) },
        { label: "Units picked", align: "right", render: (w) => fmtNumber(w.units_picked ?? 0) },
        { label: "Mistakes caught", align: "right", render: (w) => fmtNumber((w.mismatches ?? 0) + (w.over_picks ?? 0)) },
        { label: "Mistake rate", align: "right", render: (w) => h("span", { class: w.needs_attention ? "warn-text" : null }, fmtPercent(w.error_rate)) },
        { label: "Undos", align: "right", render: (w) => fmtNumber(w.undos ?? 0) },
        { label: "Flags", align: "right", render: (w) => fmtNumber(w.flags ?? 0) },
        { label: "Last active", render: (w) => h("span", { class: "muted" }, fmtAgo(w.last_active)) },
        { label: "", render: rowActions },
      ], workers, { empty: "No workers yet. Add one, then link a phone so they can sign in." })),
  ]);
}

// ---------------------------------------------------------------------------
// Phones
// ---------------------------------------------------------------------------

export async function devicesView() {
  const [link, devices] = await Promise.all([api("/api/warehouse/device-link"), api("/api/devices")]);
  const reload = () => devicesView().catch(fail);
  const active = devices.filter((d) => !d.revoked_at);
  const revoked = devices.filter((d) => d.revoked_at);

  layout("#/devices", [
    pageHeader("Phones", "Any phone works: workers' own, or shared ones kept at the dock."),
    h("div", { class: "grid-main" },
      card("Link a phone",
        h("ol", { class: "steps" },
          h("li", null, "Open the phone's camera and point it at this code."),
          h("li", null, "Tap the link. The Autorack scanner opens and links itself."),
          h("li", null, "Add it to the home screen when prompted. It works offline from then on."),
          h("li", null, "The worker signs in with their PIN.")),
        h("p", { class: "muted small" }, "No camera handy? Open ", h("span", { class: "mono" }, link.url.replace(/\?.*$/, "")), " on the phone and type the setup code."),
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: () => window.open(`/app/print.html?setup=1`, "_blank", "noopener") }, "Print setup poster"),
          isOwner() ? h("button", {
            class: "btn btn-ghost",
            onclick: async () => {
              if (!(await confirmDialog("Change the setup code?", "The old code and QR stop working for new phones. Phones already linked keep working.", { confirmLabel: "Change code" }))) return;
              await api("/api/warehouse/device-link/rotate", { method: "POST" }).then(reload, fail);
            },
          }, "Change setup code") : null)),
      card(null,
        h("div", { class: "qr-box qr-large" }, svg(link.qr_svg, "qr")),
        h("div", { class: "setup-code mono" }, link.join_code))),
    card(`Linked phones (${active.length})`,
      table([
        { label: "Phone", render: (d) => h("strong", null, d.label) },
        { label: "Signed in", render: (d) => d.current_worker || h("span", { class: "muted" }, "Nobody") },
        { label: "Last seen", render: (d) => h("span", { class: "muted" }, fmtAgo(d.last_seen_at)) },
        { label: "Linked", render: (d) => h("span", { class: "muted" }, fmtAgo(d.created_at)) },
        {
          label: "",
          render: (d) => h("div", { class: "row nowrap" },
            h("button", {
              class: "btn btn-sm",
              onclick: async () => {
                const label = await dialog("Rename phone", (close) => {
                  const input = h("input", { class: "input", value: d.label, maxlength: "100" });
                  return h("form", { class: "stack", onsubmit: (e) => { e.preventDefault(); close(input.value.trim()); } }, input,
                    h("div", { class: "dialog-actions" }, h("button", { class: "btn", type: "button", onclick: () => close(null) }, "Cancel"),
                      h("button", { class: "btn btn-primary", type: "submit" }, "Save")));
                });
                if (label) await api(`/api/devices/${d.id}`, { method: "PATCH", body: { label } }).then(reload, fail);
              },
            }, "Rename"),
            h("button", {
              class: "btn btn-sm btn-ghost",
              onclick: async () => {
                if (!(await confirmDialog("Unlink this phone?", `${d.label} is signed out and can't be used until it's linked again. Scans that already synced are kept. Scans still waiting on the phone (made offline) will not sync, so let it reconnect first if you can.`, { confirmLabel: "Unlink", danger: true }))) return;
                await api(`/api/devices/${d.id}/revoke`, { method: "POST" }).then(() => { toast("Phone unlinked", "ok"); reload(); }, fail);
              },
            }, "Unlink")),
        },
      ], active, { empty: "No phones linked yet." })),
    revoked.length ? h("details", { class: "card" }, h("summary", null, `Unlinked phones (${revoked.length})`),
      table([{ label: "Phone", key: "label" }, { label: "Unlinked", render: (d) => fmtAgo(d.revoked_at) }], revoked)) : null,
  ]);
}
