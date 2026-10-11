# =====================================================================================
# APP.PY - Aplicação Flask (backend) do Dashboard de Absenteísmo - Empresa Exemplo
#
# Substitui a antiga interface Streamlit por um app "tradicional": templates
# Jinja2 (HTML) + CSS/JS estáticos no navegador + esta API em Python/Flask.
# A camada de segurança não muda: Argon2id para senha (crypto_utils.py) e
# banco SQLite criptografado em disco com SQLCipher/AES-256 (database.py) —
# só a interface deixou de ser Streamlit.
#
# Fluxo de autenticação: sessão de servidor via cookie assinado do Flask
# (crypto_utils.obter_secret_key_sessao()). Nenhuma rota de dados responde
# sem sessão válida (decorator login_requerido). Rotas de administração
# (gerenciar usuários) exigem cargo dev/admin (decorator cargo_requerido).
# =====================================================================================

from __future__ import annotations

import secrets
from datetime import datetime as dt
from functools import wraps
from pathlib import Path

import os

import pandas as pd
from werkzeug.middleware.proxy_fix import ProxyFix
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, send_file, abort,
)

import database as db
import crypto_utils as crypto
from excel_import import processar_planilha

BASE_DIR = Path(__file__).resolve().parent
ARQUIVO_PLANILHA_PADRAO = crypto.DATA_DIR / "Relatorio_de_Absenteismo.xlsx"
SENHA_MIN_CARACTERES = 8

CARGO_ROTULO = {"dev": "Dev", "admin": "Admin", "usuario": "Usuário"}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=crypto.obter_secret_key_sessao(),
    SESSION_COOKIE_HTTPONLY=True,       # cookie de sessão inacessível a JS (mitiga XSS)
    SESSION_COOKIE_SAMESITE="Lax",      # mitiga CSRF em navegação cross-site
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,  # 20 MB — limite de upload da planilha
    # Atrás de HTTPS (proxy reverso com certificado), o cookie de sessão só
    # trafega criptografado. Ligado com ABSENTEISMO_HTTPS=1.
    SESSION_COOKIE_SECURE=os.environ.get("ABSENTEISMO_HTTPS") == "1",
)

# Atrás de um proxy reverso (Nginx Proxy Manager, Caddy...), confia nos
# cabeçalhos X-Forwarded-* de UM salto para saber o protocolo/host reais.
if os.environ.get("ABSENTEISMO_ATRAS_DE_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---------------------------------------------------------------------------
# Bootstrap: schema do banco + carga inicial da planilha padrão (se existir
# e o banco ainda estiver vazio) — mesmo comportamento da versão Streamlit.
# ---------------------------------------------------------------------------
def _inicializar() -> None:
    db.init_db()
    if not db.banco_tem_dados() and ARQUIVO_PLANILHA_PADRAO.exists():
        df_inicial_mensal, df_inicial_motivos, _ = processar_planilha(str(ARQUIVO_PLANILHA_PADRAO))
        if not df_inicial_mensal.empty:
            db.importar_dados(
                df_inicial_mensal, df_inicial_motivos,
                usuario="sistema (carga inicial)", nome_arquivo=ARQUIVO_PLANILHA_PADRAO.name,
            )


try:
    _inicializar()
except db.ChaveInvalidaError as erro:
    # Erro fatal de configuração (chave errada) — melhor falhar alto e claro
    # na inicialização do processo do que deixar toda rota quebrando depois.
    raise SystemExit(f"Erro ao iniciar o banco de dados: {erro}") from erro


# ---------------------------------------------------------------------------
# Autenticação / autorização
# ---------------------------------------------------------------------------
def login_requerido(view):
    """Bloqueia a rota para quem não tem sessão autenticada. Rotas de API
    devolvem 401 JSON; rotas de página redirecionam para /login — é a
    mesma ideia do st.stop() da versão Streamlit, só que por rota."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "usuario_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"erro": "Sessão expirada ou inválida. Faça login novamente."}), 401
            return redirect(url_for("login", proximo=request.path))
        return view(*args, **kwargs)
    return wrapper


def cargo_requerido(*cargos_permitidos):
    """Bloqueia a rota para quem não tem um dos cargos informados. Usado
    nas rotas de administração de usuários (só dev/admin)."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if session.get("cargo") not in cargos_permitidos:
                return jsonify({"erro": "Você não tem permissão para esta ação."}), 403
            return view(*args, **kwargs)
        return wrapper
    return decorator


def _csrf_valido() -> bool:
    """Confere o token CSRF enviado no cabeçalho X-CSRF-Token contra o
    guardado na sessão. Todo POST/DELETE que muda estado passa por aqui —
    sem isso, um site malicioso poderia disparar essas ações usando o
    cookie de sessão da vítima sem o usuário perceber."""
    token_sessao = session.get("csrf_token")
    token_requisicao = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    return bool(token_sessao) and token_sessao == token_requisicao


def csrf_requerido(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _csrf_valido():
            return jsonify({"erro": "Token de segurança inválido ou ausente. Recarregue a página."}), 400
        return view(*args, **kwargs)
    return wrapper


def _iniciar_sessao(usuario: dict) -> None:
    session.clear()
    session["usuario_id"] = usuario["id"]
    session["username"] = usuario["username"]
    session["nome_completo"] = usuario["nome_completo"]
    session["cargo"] = usuario["cargo"]
    session["csrf_token"] = secrets.token_urlsafe(32)


def _usuario_logado() -> dict:
    return {
        "id": session["usuario_id"],
        "username": session["username"],
        "nome_completo": session["nome_completo"],
        "cargo": session["cargo"],
        "cargo_rotulo": CARGO_ROTULO[session["cargo"]],
    }


def _destino_seguro(proximo: str | None) -> str:
    """Só redireciona depois do login para um caminho interno do próprio
    sistema. Sem isso, um link como /login?proximo=https://site-falso.com
    mandaria o usuário, já logado, para fora do sistema (open redirect)."""
    if proximo and proximo.startswith("/") and not proximo.startswith("//") and "\\" not in proximo:
        return proximo
    return url_for("dashboard")


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if "usuario_id" in session:
        return redirect(url_for("dashboard"))

    primeiro_acesso = not db.existe_algum_usuario()
    erro = None

    if request.method == "POST":
        if primeiro_acesso:
            nome_completo = request.form.get("nome_completo", "").strip()
            username = request.form.get("username", "").strip()
            senha = request.form.get("password", "")
            confirmar_senha = request.form.get("password_confirm", "")

            if not nome_completo or not username or not senha:
                erro = "Preencha todos os campos."
            elif senha != confirmar_senha:
                erro = "As senhas não coincidem."
            elif len(senha) < SENHA_MIN_CARACTERES:
                erro = f"A senha deve ter pelo menos {SENHA_MIN_CARACTERES} caracteres."
            else:
                sucesso, mensagem = db.criar_usuario(username, nome_completo, senha, cargo="dev")
                if sucesso:
                    return redirect(url_for("login", criado="1"))
                erro = mensagem
        else:
            username = request.form.get("username", "")
            senha = request.form.get("password", "")
            usuario = db.verificar_login(username, senha)
            if usuario is None:
                erro = "Usuário ou senha inválidos."
            else:
                _iniciar_sessao(usuario)
                return redirect(_destino_seguro(request.args.get("proximo")))

    return render_template(
        "login.html",
        primeiro_acesso=primeiro_acesso,
        erro=erro,
        conta_criada=request.args.get("criado") == "1",
        senha_min_caracteres=SENHA_MIN_CARACTERES,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_requerido
def dashboard():
    usuario = _usuario_logado()
    return render_template(
        "dashboard.html",
        usuario=usuario,
        pode_administrar=usuario["cargo"] in db.CARGOS_COM_PAINEL_ADMIN,
        cargos_criaveis=list(db.CARGOS_VALIDOS) if usuario["cargo"] == "dev" else ["admin", "usuario"],
        cargo_rotulo=CARGO_ROTULO,
        senha_min_caracteres=SENHA_MIN_CARACTERES,
    )


# ---------------------------------------------------------------------------
# API: dados do dashboard (filtros, KPIs, gráficos, tabela)
# ---------------------------------------------------------------------------
def _carregar_dataframes():
    return db.carregar_dados_do_banco()


@app.route("/api/opcoes")
@login_requerido
def api_opcoes():
    df_mensal, _ = _carregar_dataframes()
    if df_mensal.empty:
        return jsonify({"meses": [], "colaboradores": []})
    ordem_meses = df_mensal[["Mes", "NumMes"]].drop_duplicates().sort_values("NumMes")["Mes"].tolist()
    colaboradores = sorted(df_mensal["Colaborador"].unique().tolist())
    return jsonify({"meses": ordem_meses, "colaboradores": colaboradores})


@app.route("/api/dashboard")
@login_requerido
def api_dashboard():
    df_mensal, df_motivos = _carregar_dataframes()

    if df_mensal.empty:
        return jsonify({"vazio": True})

    meses_selecionados = request.args.getlist("mes") or df_mensal["Mes"].unique().tolist()
    colaboradores_selecionados = request.args.getlist("colaborador")

    df_filtrado = df_mensal[df_mensal["Mes"].isin(meses_selecionados)]
    if colaboradores_selecionados:
        df_filtrado = df_filtrado[df_filtrado["Colaborador"].isin(colaboradores_selecionados)]

    df_motivos_filtrado = df_motivos[df_motivos["Mes"].isin(meses_selecionados)] if not df_motivos.empty else df_motivos
    if colaboradores_selecionados and not df_motivos_filtrado.empty:
        df_motivos_filtrado = df_motivos_filtrado[df_motivos_filtrado["Colaborador"].isin(colaboradores_selecionados)]

    if df_filtrado.empty:
        return jsonify({"vazio": True})

    # --- KPIs -----------------------------------------------------------
    total_registros = int(len(df_motivos_filtrado)) if not df_motivos_filtrado.empty else 0
    total_horas = float(df_filtrado["HorasAusente"].sum())
    total_previstas = float(df_filtrado["HorasPrevistas"].sum())
    # Indicador geral = soma das horas ausentes / soma das horas previstas
    # (mesma regra da planilha). Não é a média dos % individuais, que daria
    # o mesmo peso a quem tem 18h previstas e a quem tem 193h.
    percentual_geral = (total_horas / total_previstas * 100) if total_previstas else 0.0
    ranking = df_filtrado.groupby("Colaborador")["PercentualAbsenteismo"].mean().sort_values(ascending=False)
    colab_critico = ranking.index[0] if not ranking.empty else "—"
    ind_critico = float(ranking.iloc[0]) if not ranking.empty else 0.0

    # --- Gráfico: evolução mensal (linha) -------------------------------
    evolucao = (
        df_filtrado.groupby(["NumMes", "Mes"], as_index=False)[["HorasAusente", "HorasPrevistas"]]
        .sum().sort_values("NumMes")
    )
    evolucao["Percentual"] = (evolucao["HorasAusente"] / evolucao["HorasPrevistas"].where(evolucao["HorasPrevistas"] > 0) * 100).fillna(0)

    # --- Gráfico: motivos de ausência (rosca) ---------------------------
    if not df_motivos_filtrado.empty:
        por_motivo = df_motivos_filtrado["Motivo"].value_counts()
        grafico_motivos = {"labels": por_motivo.index.tolist(), "valores": por_motivo.values.tolist()}
    else:
        grafico_motivos = {"labels": [], "valores": []}

    # --- Gráfico: impacto por colaborador (barras, top 15) --------------
    por_colaborador = (
        df_filtrado.groupby("Colaborador", as_index=False)["HorasAusente"]
        .sum().sort_values("HorasAusente", ascending=False).head(15)
        .sort_values("HorasAusente")
    )

    # --- Tabela detalhada -------------------------------------------------
    colunas_tabela = [
        "Colaborador", "Mes", "DiasTrabalhados", "HorasPrevistas",
        "HorasTrabalhadas", "HorasAusente", "PercentualAbsenteismo",
    ]
    tabela = (
        df_filtrado.sort_values(["NumMes", "Colaborador"])[colunas_tabela]
        .to_dict(orient="records")
    )

    return jsonify({
        "vazio": False,
        "kpis": {
            "total_registros": total_registros,
            "total_horas": round(total_horas, 1),
            "percentual_geral": round(percentual_geral, 2),
            "colaborador_critico": colab_critico,
            "indice_critico": round(ind_critico, 1),
        },
        "grafico_evolucao": {
            "labels": evolucao["Mes"].tolist(),
            "valores": evolucao["HorasAusente"].round(1).tolist(),
            "percentuais": evolucao["Percentual"].round(2).tolist(),
        },
        "grafico_motivos": grafico_motivos,
        "grafico_colaboradores": {
            "labels": por_colaborador["Colaborador"].tolist(),
            "valores": por_colaborador["HorasAusente"].round(1).tolist(),
        },
        "tabela": tabela,
    })


# ---------------------------------------------------------------------------
# API: importação de planilha
# ---------------------------------------------------------------------------
@app.route("/api/importar", methods=["POST"])
@login_requerido
@csrf_requerido
def api_importar():
    arquivo = request.files.get("arquivo")
    if arquivo is None or not arquivo.filename:
        return jsonify({"erro": "Nenhum arquivo enviado."}), 400
    if not arquivo.filename.lower().endswith(".xlsx"):
        return jsonify({"erro": "Envie um arquivo .xlsx."}), 400

    try:
        df_novo_mensal, df_novo_motivos, avisos = processar_planilha(arquivo)
    except Exception:
        return jsonify({"erro": "Não foi possível ler essa planilha. Verifique o formato/modelo."}), 400

    if df_novo_mensal.empty:
        return jsonify({"erro": "Nenhum dado válido encontrado nessa planilha."}), 400

    qtd_mensal, qtd_motivos = db.importar_dados(
        df_novo_mensal, df_novo_motivos,
        usuario=session["username"], nome_arquivo=arquivo.filename,
    )
    return jsonify({
        "ok": True, "registros_mensal": qtd_mensal, "registros_motivos": qtd_motivos, "avisos": avisos,
    })


@app.route("/api/ultima-importacao")
@login_requerido
def api_ultima_importacao():
    info = db.ultima_importacao()
    if not info:
        return jsonify(None)
    data_fmt = dt.fromisoformat(info["realizado_em"]).strftime("%d/%m/%Y %H:%M")
    return jsonify({"data": data_fmt, "usuario": info["usuario"]})


# ---------------------------------------------------------------------------
# API: backup
# ---------------------------------------------------------------------------
@app.route("/api/backup", methods=["POST"])
@login_requerido
@csrf_requerido
def api_criar_backup():
    caminho = db.criar_backup()
    return jsonify({"ok": True, "nome": caminho.name})


@app.route("/api/backups")
@login_requerido
def api_listar_backups():
    return jsonify([p.name for p in db.listar_backups()])


@app.route("/api/backups/<path:nome_arquivo>")
@login_requerido
def api_baixar_backup(nome_arquivo):
    # Evita path traversal: só aceita o nome puro do arquivo, e só de dentro
    # da pasta de backups real (nunca monta o caminho a partir de entrada
    # livre do usuário sem checar que o resultado continua dentro dela).
    caminho = (db.BACKUP_DIR / Path(nome_arquivo).name).resolve()
    if db.BACKUP_DIR.resolve() not in caminho.parents or not caminho.exists():
        abort(404)
    return send_file(caminho, as_attachment=True, download_name=caminho.name)


# ---------------------------------------------------------------------------
# API: gerenciamento de usuários (dev/admin)
# ---------------------------------------------------------------------------
@app.route("/api/usuarios", methods=["GET"])
@login_requerido
@cargo_requerido(*db.CARGOS_COM_PAINEL_ADMIN)
def api_listar_usuarios():
    usuarios = db.listar_usuarios()
    cargo_de_quem_pede = session["cargo"]
    for u in usuarios:
        u["cargo_rotulo"] = CARGO_ROTULO[u["cargo"]]
        # Só um "dev" pode ativar/desativar outro "dev" — um admin comum vê
        # o status dessa conta, mas não consegue alterá-lo.
        u["pode_alterar"] = u["cargo"] != "dev" or cargo_de_quem_pede == "dev"
    return jsonify(usuarios)


@app.route("/api/usuarios", methods=["POST"])
@login_requerido
@cargo_requerido(*db.CARGOS_COM_PAINEL_ADMIN)
@csrf_requerido
def api_criar_usuario():
    dados = request.get_json(silent=True) or {}
    nome_completo = (dados.get("nome_completo") or "").strip()
    username = (dados.get("username") or "").strip()
    senha = dados.get("password") or ""
    cargo = dados.get("cargo") or "usuario"

    # Um admin delegado não pode criar (nem se autopromover a) outro dev —
    # só quem já é dev cria contas dev. Reforçado aqui no servidor, não só
    # escondendo a opção na tela.
    cargos_permitidos = db.CARGOS_VALIDOS if session["cargo"] == "dev" else ("admin", "usuario")
    if cargo not in cargos_permitidos:
        return jsonify({"erro": "Você não tem permissão para criar um usuário com esse cargo."}), 403

    sucesso, mensagem = db.criar_usuario(username, nome_completo, senha, cargo=cargo)
    return jsonify({"ok": sucesso, "mensagem": mensagem}), (200 if sucesso else 400)


@app.route("/api/usuarios/<int:usuario_id>/status", methods=["POST"])
@login_requerido
@cargo_requerido(*db.CARGOS_COM_PAINEL_ADMIN)
@csrf_requerido
def api_alterar_status_usuario(usuario_id):
    alvo = next((u for u in db.listar_usuarios() if u["id"] == usuario_id), None)
    if alvo is None:
        return jsonify({"erro": "Usuário não encontrado."}), 404
    if alvo["cargo"] == "dev" and session["cargo"] != "dev":
        return jsonify({"erro": "Só um Dev pode ativar/desativar outra conta Dev."}), 403

    dados = request.get_json(silent=True) or {}
    db.alterar_status_usuario(usuario_id, bool(dados.get("ativo")))
    return jsonify({"ok": True})


if __name__ == "__main__":
    # debug=False mesmo em desenvolvimento local: o debugger interativo do
    # Flask permite execução arbitrária de código a partir do navegador —
    # inaceitável para um app que já lida com senhas e dados de RH.
    app.run(host="127.0.0.1", port=5000, debug=False)
