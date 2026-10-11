// Menu lateral no celular + utilitários comuns (toast e fetch com CSRF).
(function () {
  const sidebar = document.getElementById("sidebar");
  const fundo = document.getElementById("sidebar-fundo");
  const botao = document.getElementById("btn-menu");
  if (sidebar && fundo && botao) {
    const alternar = (abrir) => {
      sidebar.classList.toggle("aberta", abrir);
      fundo.classList.toggle("aberta", abrir);
    };
    botao.addEventListener("click", () => alternar(!sidebar.classList.contains("aberta")));
    fundo.addEventListener("click", () => alternar(false));
  }
})();

function mostrarToast(mensagem, erro = false) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = mensagem;
  el.classList.toggle("erro", erro);
  el.classList.add("visivel");
  clearTimeout(mostrarToast._t);
  mostrarToast._t = setTimeout(() => el.classList.remove("visivel"), 3500);
}

// fetch que já manda o token CSRF e trata sessão expirada (401 -> login).
async function api(url, opcoes = {}) {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const headers = Object.assign({ "X-CSRF-Token": meta ? meta.content : "" }, opcoes.headers || {});
  const resposta = await fetch(url, Object.assign({}, opcoes, { headers, credentials: "same-origin" }));
  if (resposta.status === 401) {
    window.location.href = "/login";
    throw new Error("Sessão expirada");
  }
  let dados = null;
  try { dados = await resposta.json(); } catch (_) { /* resposta sem JSON */ }
  if (!resposta.ok) {
    throw new Error((dados && (dados.erro || dados.mensagem)) || `Erro ${resposta.status}`);
  }
  return dados;
}
