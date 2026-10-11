// Dashboard: busca os dados na API, desenha KPIs/gráficos/tabela e cuida
// de importação, backup e usuários. Todo texto vindo do servidor entra na
// página via textContent (nunca innerHTML), para não abrir brecha de XSS.

const fmtNum = (v, casas = 1) => Number(v).toLocaleString("pt-BR", { minimumFractionDigits: casas, maximumFractionDigits: casas });
const graficos = {};
const CORES = ["#8b3dff", "#e0218a", "#ffc857", "#2ecc8f", "#4dabf7", "#ff8a5c"];

function el(tag, props = {}, ...filhos) {
  const n = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  });
  filhos.forEach((f) => n.append(f instanceof Node ? f : document.createTextNode(f ?? "")));
  return n;
}

function valoresSelecionados(select) {
  return Array.from(select.selectedOptions).map((o) => o.value);
}

// ---------------------------------------------------------------- filtros
async function carregarOpcoes() {
  const opcoes = await api("/api/opcoes");
  const selMeses = document.getElementById("filtro-meses");
  const selColab = document.getElementById("filtro-colaboradores");
  const mesesAntes = new Set(valoresSelecionados(selMeses));
  const colabAntes = new Set(valoresSelecionados(selColab));
  selMeses.replaceChildren(...opcoes.meses.map((m) => { const o = el("option", { value: m }, m); o.selected = mesesAntes.has(m); return o; }));
  selColab.replaceChildren(...opcoes.colaboradores.map((c) => { const o = el("option", { value: c }, c); o.selected = colabAntes.has(c); return o; }));
}

// --------------------------------------------------------------- dashboard
async function carregarDashboard() {
  const params = new URLSearchParams();
  valoresSelecionados(document.getElementById("filtro-meses")).forEach((m) => params.append("mes", m));
  valoresSelecionados(document.getElementById("filtro-colaboradores")).forEach((c) => params.append("colaborador", c));

  const dados = await api("/api/dashboard?" + params.toString());
  const vazio = dados.vazio;
  document.getElementById("estado-vazio").classList.toggle("escondido", !vazio);
  document.getElementById("painel-dados").classList.toggle("escondido", vazio);
  if (vazio) return;

  const k = dados.kpis;
  document.getElementById("kpi-percentual").textContent = fmtNum(k.percentual_geral, 2) + "%";
  document.getElementById("kpi-horas").textContent = fmtNum(k.total_horas) + " h";
  document.getElementById("kpi-registros").textContent = k.total_registros;
  document.getElementById("kpi-critico").textContent = k.colaborador_critico;
  document.getElementById("kpi-critico").title = k.colaborador_critico;
  document.getElementById("kpi-critico-valor").textContent = fmtNum(k.indice_critico) + "% de média no período";

  desenharGraficos(dados);
  desenharTabela(dados.tabela);
}

function grafico(id, config) {
  if (typeof Chart === "undefined") return; // CDN indisponível: o resto da tela continua funcionando
  if (graficos[id]) graficos[id].destroy();
  graficos[id] = new Chart(document.getElementById(id), config);
}

function desenharGraficos(dados) {
  if (typeof Chart !== "undefined") {
    Chart.defaults.color = "#b4a6cc";
    Chart.defaults.borderColor = "rgba(255,255,255,0.08)";
  }
  const ev = dados.grafico_evolucao;
  grafico("grafico-evolucao", {
    data: {
      labels: ev.labels,
      datasets: [
        { type: "bar", label: "Horas ausentes", data: ev.valores, backgroundColor: "rgba(139,61,255,0.6)", borderRadius: 6, yAxisID: "y" },
        { type: "line", label: "% absenteísmo", data: ev.percentuais, borderColor: "#e0218a", backgroundColor: "#e0218a", tension: 0.3, yAxisID: "y1" },
      ],
    },
    options: {
      maintainAspectRatio: false,
      scales: {
        y: { beginAtZero: true, title: { display: true, text: "horas" } },
        y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false }, ticks: { callback: (v) => v + "%" } },
      },
    },
  });

  const mot = dados.grafico_motivos;
  grafico("grafico-motivos", {
    type: "doughnut",
    data: { labels: mot.labels, datasets: [{ data: mot.valores, backgroundColor: CORES, borderWidth: 0 }] },
    options: { maintainAspectRatio: false, plugins: { legend: { position: "bottom" } } },
  });

  const col = dados.grafico_colaboradores;
  grafico("grafico-colaboradores", {
    type: "bar",
    data: { labels: col.labels, datasets: [{ label: "Horas ausentes", data: col.valores, backgroundColor: "rgba(224,33,138,0.65)", borderRadius: 5 }] },
    options: { indexAxis: "y", maintainAspectRatio: false, plugins: { legend: { display: false } } },
  });
}

function desenharTabela(linhas) {
  const corpo = document.getElementById("tabela-corpo");
  corpo.replaceChildren(...linhas.map((l) => {
    const pct = Number(l.PercentualAbsenteismo);
    const classePct = pct >= 20 ? "num pct-alto" : pct >= 10 ? "num pct-medio" : "num";
    return el("tr", {},
      el("td", {}, l.Colaborador),
      el("td", {}, l.Mes),
      el("td", { class: "num" }, fmtNum(l.DiasTrabalhados, 0)),
      el("td", { class: "num" }, fmtNum(l.HorasPrevistas, 2)),
      el("td", { class: "num" }, fmtNum(l.HorasTrabalhadas, 2)),
      el("td", { class: "num" }, fmtNum(l.HorasAusente, 2)),
      el("td", { class: classePct }, fmtNum(pct, 2) + "%"),
    );
  }));
}

// ------------------------------------------------------------- importação
async function carregarUltimaImportacao() {
  const info = await api("/api/ultima-importacao");
  document.getElementById("ultima-importacao").textContent =
    info ? `Última importação: ${info.data} por ${info.usuario}` : "Nenhuma importação ainda.";
}

function mostrarAvisos(avisos) {
  const caixa = document.getElementById("aviso-importacao");
  if (!avisos || !avisos.length) { caixa.classList.add("escondido"); return; }
  caixa.replaceChildren(
    el("strong", {}, "Ajustes feitos na leitura da planilha:"),
    el("ul", {}, ...avisos.map((a) => el("li", {}, a))),
  );
  caixa.classList.remove("escondido");
}

document.getElementById("form-importar").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const botao = form.querySelector("button");
  const corpo = new FormData(form);
  botao.disabled = true;
  try {
    const r = await api("/api/importar", { method: "POST", body: corpo });
    mostrarToast(`Importado: ${r.registros_mensal} linhas mensais e ${r.registros_motivos} faltas/atestados.`);
    mostrarAvisos(r.avisos);
    form.reset();
    await carregarOpcoes();
    await Promise.all([carregarDashboard(), carregarUltimaImportacao()]);
  } catch (erro) {
    mostrarToast(erro.message, true);
  } finally {
    botao.disabled = false;
  }
});

// ------------------------------------------------------------------ backup
async function carregarBackups() {
  const nomes = await api("/api/backups");
  const lista = document.getElementById("lista-backups");
  lista.replaceChildren(...nomes.slice(0, 5).map((n) =>
    el("li", {}, el("a", { href: "/api/backups/" + encodeURIComponent(n) }, "⬇ " + n))));
}

document.getElementById("btn-backup").addEventListener("click", async (e) => {
  const botao = e.currentTarget; // currentTarget vira null depois do primeiro await
  botao.disabled = true;
  try {
    const r = await api("/api/backup", { method: "POST" });
    mostrarToast("Backup criado: " + r.nome);
    await carregarBackups();
  } catch (erro) {
    mostrarToast(erro.message, true);
  } finally {
    botao.disabled = false;
  }
});

// ---------------------------------------------------------------- usuários
async function carregarUsuarios() {
  const lista = document.getElementById("lista-usuarios");
  if (!lista) return;
  const usuarios = await api("/api/usuarios");
  lista.replaceChildren(...usuarios.map((u) => {
    const info = el("div", {}, u.nome_completo, el("small", {}, `${u.username} · ${u.cargo_rotulo}`));
    const li = el("li", { class: u.ativo ? "" : "inativo" }, info);
    if (u.pode_alterar && u.id !== window.USUARIO_ID) {
      li.append(el("button", {
        class: "btn btn-secundario btn-mini", type: "button",
        onclick: async () => {
          try {
            await api(`/api/usuarios/${u.id}/status`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ ativo: !u.ativo }),
            });
            await carregarUsuarios();
          } catch (erro) { mostrarToast(erro.message, true); }
        },
      }, u.ativo ? "Desativar" : "Ativar"));
    }
    return li;
  }));
}

const formUsuario = document.getElementById("form-usuario");
if (formUsuario) {
  formUsuario.addEventListener("submit", async (e) => {
    e.preventDefault();
    const dados = Object.fromEntries(new FormData(formUsuario));
    try {
      const r = await api("/api/usuarios", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(dados),
      });
      mostrarToast(r.mensagem);
      formUsuario.reset();
      await carregarUsuarios();
    } catch (erro) {
      mostrarToast(erro.message, true);
    }
  });
}

// ----------------------------------------------------------------- início
["filtro-meses", "filtro-colaboradores"].forEach((id) =>
  document.getElementById(id).addEventListener("change", () => carregarDashboard().catch((e) => mostrarToast(e.message, true))));
document.getElementById("btn-limpar-filtros").addEventListener("click", () => {
  document.querySelectorAll("#filtro-meses option, #filtro-colaboradores option").forEach((o) => { o.selected = false; });
  carregarDashboard().catch((e) => mostrarToast(e.message, true));
});

(async function iniciar() {
  try {
    await carregarOpcoes();
    await Promise.all([carregarDashboard(), carregarUltimaImportacao(), carregarBackups(), carregarUsuarios()]);
  } catch (erro) {
    mostrarToast(erro.message, true);
  }
})();
