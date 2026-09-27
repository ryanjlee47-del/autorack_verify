// Help & support: quick answers from the help center, and a message to us.

import { h, mount, toast } from "../../../shared/dom.js";
import { api, card, ctx, fail, layout, pageHeader } from "../core.js";
import { T } from "../i18n.js";

const HELP = "/help.html";

function quickAnswers() {
  return [
    ["first-steps", T("What should I do first?")],
    ["link-phone", T("How do I link a phone?")],
    ["csv", T("What should my CSV look like?")],
    ["barcode-mismatch", T("A barcode scans as wrong, but it's the right item.")],
    ["pin", T("A worker forgot their PIN.")],
    ["switch-plan", T("Can I switch between yearly and monthly?")],
    ["export", T("Can I get my data out?")],
  ];
}

export function helpView(params) {
  const topic = h("select", { class: "input", id: "support-topic" },
    ...[["question", T("A question")], ["problem", T("Something isn't working")], ["billing", T("Billing")], ["idea", T("A feature idea")]]
      .map(([v, label]) => h("option", { value: v, selected: v === params.get("topic") }, label)));
  const message = h("textarea", {
    class: "input", id: "support-message", rows: "7", maxlength: "5000", required: true,
    placeholder: T("What happened, and what did you expect? If it's about an order, include its number."),
  });
  const send = h("button", { class: "btn btn-primary", type: "submit" }, T("Send message"));
  const formBox = h("div");
  const form = h("form", {
    class: "stack",
    onsubmit: async (e) => {
      e.preventDefault();
      if (message.value.trim().length < 5) {
        toast(T("Tell us a little more first."), "bad");
        return;
      }
      send.disabled = true;
      try {
        await api("/api/support", {
          method: "POST",
          body: { topic: topic.value, message: message.value.trim(), page: ctx.lastRoute ? `#${ctx.lastRoute}` : null },
        });
        mount(formBox, h("div", { class: "banner banner-ok", role: "status" },
          h("strong", null, T("Message sent.")), " ",
          T("We'll reply to {p0} by email, usually within one business day.", { p0: ctx.me.user.email })));
      } catch (err) {
        send.disabled = false;
        fail(err);
      }
    },
  },
  h("label", { for: "support-topic" }, T("What's it about?")), topic,
  h("label", { for: "support-message" }, T("Your message")), message,
  h("p", { class: "muted small" }, T("We'll see which warehouse and page you're on, so there's no need to explain your setup.")),
  h("div", { class: "row" }, send));
  mount(formBox, form);

  layout("#/help", [
    pageHeader(T("Help & support"), T("Quick answers, or send us a message and we'll reply by email.")),
    h("div", { class: "grid-main" },
      card(T("Contact support"), formBox),
      card(T("Quick answers"),
        h("ul", { class: "plain-list help-links" },
          ...quickAnswers().map(([id, q]) => h("li", null, h("a", { href: `${HELP}#${id}`, target: "_blank", rel: "noopener" }, q)))),
        h("p", null, h("a", { class: "btn", href: HELP, target: "_blank", rel: "noopener" }, T("Open the help center"))))),
  ]);
  message.focus();
}
