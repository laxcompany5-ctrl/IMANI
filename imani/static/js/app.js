/* IMANI — progressive enhancement for the auth forms. */
(function () {
  "use strict";

  // --- Password reveal toggles ------------------------------------
  document.querySelectorAll(".reveal[data-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var input = document.getElementById(button.dataset.target);
      if (!input) return;
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      button.textContent = show ? "Hide" : "Show";
      button.setAttribute("aria-label", show ? "Hide password" : "Show password");
    });
  });

  // --- Password strength meter ------------------------------------
  function scorePassword(value) {
    var score = 0;
    if (!value) return 0;
    if (value.length >= 8) score++;
    if (value.length >= 12) score++;
    if (/[A-Za-z]/.test(value) && /\d/.test(value)) score++;
    if (/[^A-Za-z0-9]/.test(value)) score++;
    return Math.min(score, 4);
  }

  var LABELS = [
    "Use 8+ characters with letters and numbers.",
    "Weak — add more characters.",
    "Fair — mix letters, numbers and symbols.",
    "Good — a little longer would be stronger.",
    "Strong password."
  ];

  var source = document.querySelector("[data-strength-source]");
  var meter = document.querySelector("[data-strength-for]");
  var label = document.querySelector("[data-strength-label]");

  if (source && meter && label) {
    source.addEventListener("input", function () {
      var score = scorePassword(source.value);
      meter.setAttribute("data-score", String(score));
      label.textContent = LABELS[score];
    });
  }

  // --- Confirm-password live feedback -----------------------------
  var confirm = document.querySelector("[data-confirm-for]");
  if (confirm && source) {
    var confirmLabel = document.querySelector("[data-confirm-label]");
    var check = function () {
      if (!confirm.value) { if (confirmLabel) confirmLabel.textContent = ""; return; }
      var match = confirm.value === source.value;
      if (confirmLabel) confirmLabel.textContent = match ? "Passwords match." : "Passwords do not match yet.";
      if (confirmLabel) confirmLabel.style.color = match ? "var(--success)" : "var(--warning)";
    };
    confirm.addEventListener("input", check);
    source.addEventListener("input", check);
  }
})();
