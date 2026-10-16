// Painel Server10: tema, atualização do painel e confirmações. Sem dependências.
(function () {
  "use strict";

  var TEMAS = { escuro: "dark", claro: "light", sistema: "system" };

  // Tema: aplica na hora e grava a preferência (cookie não sensível); sem JS o formulário faz POST.
  document.addEventListener("click", function (ev) {
    var botao = ev.target.closest ? ev.target.closest(".tema-op") : null;
    if (!botao) { return; }
    var valor = botao.value;
    if (!TEMAS[valor]) { return; }
    ev.preventDefault();
    document.documentElement.setAttribute("data-theme", TEMAS[valor]);
    var seguro = location.protocol === "https:" ? "; secure" : "";
    document.cookie = "tema=" + valor + "; path=/; max-age=31536000; samesite=lax" + seguro;
    document.querySelectorAll(".tema-op").forEach(function (b) {
      b.setAttribute("aria-pressed", b === botao ? "true" : "false");
    });
  });

  // Confirmação antes de remover (delegada: vale também após atualizar o conteúdo).
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    if (form.classList && form.classList.contains("confirmar")) {
      if (!window.confirm(form.getAttribute("data-mensagem") || "Confirmar?")) { ev.preventDefault(); }
    }
  });

  // Atualização do painel: lê apenas o estado já coletado; pausa com a aba oculta.
  var alvo = document.getElementById("status");
  if (!alvo) { return; }
  var intervalo = (parseInt(alvo.getAttribute("data-intervalo"), 10) || 60) * 1000;
  var ultima = Date.now();
  var emAndamento = false;

  function atualizar() {
    if (emAndamento || document.visibilityState !== "visible") { return; }
    emAndamento = true;
    fetch(alvo.getAttribute("data-url"), { credentials: "same-origin", cache: "no-store" })
      .then(function (resp) {
        if (resp.status === 401) { window.location.reload(); return null; }
        return resp.ok ? resp.text() : null;
      })
      .then(function (html) {
        if (!html) { return; }
        var abertos = [];
        alvo.querySelectorAll("details[open]").forEach(function (d) { if (d.id) { abertos.push(d.id); } });
        alvo.classList.remove("inicial");
        alvo.innerHTML = html;
        abertos.forEach(function (id) {
          var d = document.getElementById(id);
          if (d) { d.open = true; }
        });
      })
      .catch(function () { /* rede indisponível: mantém a última tela */ })
      .then(function () { ultima = Date.now(); emAndamento = false; });
  }

  setInterval(atualizar, intervalo);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && Date.now() - ultima >= intervalo) { atualizar(); }
  });
})();
