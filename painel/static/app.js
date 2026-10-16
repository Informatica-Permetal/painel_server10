// Atualiza o painel periodicamente e confirma ações destrutivas. Sem dependências.
(function () {
  "use strict";

  var alvo = document.getElementById("status");
  if (alvo) {
    var intervalo = parseInt(alvo.getAttribute("data-intervalo"), 10) || 60;
    setInterval(function () {
      fetch(alvo.getAttribute("data-url"), { credentials: "same-origin", cache: "no-store" })
        .then(function (resp) {
          if (resp.status === 401) { window.location.reload(); return null; }
          return resp.ok ? resp.text() : null;
        })
        .then(function (html) { if (html) { alvo.innerHTML = html; } })
        .catch(function () { /* rede indisponível: mantém a última tela */ });
    }, intervalo * 1000);
  }

  document.querySelectorAll("form.confirmar").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      if (!window.confirm(form.getAttribute("data-mensagem") || "Confirmar?")) { ev.preventDefault(); }
    });
  });
})();
