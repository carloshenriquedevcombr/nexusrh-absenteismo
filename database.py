# =====================================================================================
# DATABASE.PY - Camada de acesso ao banco de dados
#
# O arquivo físico do banco é criptografado em disco com AES-256 via SQLCipher
# (extensão do SQLite): mesmo se alguém copiar o .db (ou um backup), os dados
# só são legíveis com a chave mestra (crypto_utils.obter_chave_banco_hex()).
# As senhas de usuário nunca são gravadas em texto puro nem com um hash
# reversível — usam Argon2id (crypto_utils.hash_senha).
#
# Notas de complexidade (Big O):
#   - username, e (colaborador, mes) têm UNIQUE, portanto SQLite mantém um
#     índice B-tree para cada um: buscas e upserts são O(log n), não O(n).
#   - As importações usam executemany (uma única viagem de I/O em lote) em vez
#     de um INSERT por linha em loop Python, reduzindo o overhead constante
#     sem mudar a complexidade assintótica (ainda O(n log n) para n linhas).
# =====================================================================================

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import sqlcipher3.dbapi2 as sqlite3

import crypto_utils as crypto

# ---------------------------------------------------------------------------
# Configuração de caminhos
# ---------------------------------------------------------------------------
DATA_DIR = crypto.DATA_DIR
DB_PATH = DATA_DIR / "absenteismo.db"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)


class ChaveInvalidaError(RuntimeError):
    """A chave de criptografia informada não abre o banco existente."""


# Hierarquia de cargos, do maior para o menor privilégio. "dev" é quem
# instala/mantém o sistema (primeira conta criada); "admin" é delegado pelo
# dev para gerenciar usuários do dia a dia; "usuario" só vê o dashboard.
CARGOS_VALIDOS = ("dev", "admin", "usuario")
CARGOS_COM_PAINEL_ADMIN = ("dev", "admin")


# ---------------------------------------------------------------------------
# Conexão (com a criptografia do banco já configurada)
# ---------------------------------------------------------------------------
def get_connection() -> sqlite3.Connection:
    """Abre uma conexão criptografada com o banco.

    A PRAGMA key precisa ser a primeira instrução executada na conexão: é ela
    que o SQLCipher usa para derivar a cifra AES-256 aplicada a cada página
    do arquivo. Sem a chave certa, qualquer leitura subsequente falha.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    chave_hex = crypto.obter_chave_banco_hex()
    conn.execute(f"PRAGMA key = \"x'{chave_hex}'\"")
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        conn.execute("SELECT count(*) FROM sqlite_master;")
    except sqlite3.DatabaseError as erro:
        conn.close()
        raise ChaveInvalidaError(
            "Não foi possível abrir o banco com a chave atual. Verifique se "
            "db.key (ou a variável de ambiente ABSENTEISMO_DB_KEY) é a mesma "
            "chave usada quando o banco foi criado."
        ) from erro

    return conn


# ---------------------------------------------------------------------------
# Criação do schema
# ---------------------------------------------------------------------------
def init_db() -> None:
    """Cria as tabelas do banco caso ainda não existam. Chamada uma vez no
    início do app."""
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS usuarios (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL UNIQUE,
                nome_completo   TEXT NOT NULL,
                password_hash   TEXT NOT NULL,
                cargo           TEXT NOT NULL DEFAULT 'usuario'
                                    CHECK (cargo IN ('dev', 'admin', 'usuario')),
                ativo           INTEGER NOT NULL DEFAULT 1,
                criado_em       TEXT NOT NULL,
                ultimo_login    TEXT
            );

            CREATE TABLE IF NOT EXISTS absenteismo_mensal (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                colaborador             TEXT NOT NULL,
                mes                     TEXT NOT NULL,
                num_mes                 INTEGER NOT NULL,
                dias_trabalhados        REAL DEFAULT 0,
                horas_previstas         REAL DEFAULT 0,
                horas_trabalhadas       REAL DEFAULT 0,
                horas_ausente           REAL DEFAULT 0,
                percentual_absenteismo  REAL DEFAULT 0,
                atualizado_em           TEXT NOT NULL,
                UNIQUE(colaborador, mes)
            );

            CREATE TABLE IF NOT EXISTS absenteismo_motivos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                colaborador     TEXT NOT NULL,
                data            TEXT NOT NULL,
                mes             TEXT NOT NULL,
                motivo          TEXT NOT NULL,
                atualizado_em   TEXT NOT NULL,
                UNIQUE(colaborador, data, motivo)
            );

            CREATE TABLE IF NOT EXISTS importacoes_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario             TEXT,
                arquivo             TEXT,
                registros_mensal    INTEGER,
                registros_motivos   INTEGER,
                realizado_em        TEXT NOT NULL
            );
            """
        )
        _migrar_is_admin_para_cargo(conn)
        _recalcular_percentuais(conn)
        conn.commit()
    _remover_meses_duplicados()


def _migrar_is_admin_para_cargo(conn: sqlite3.Connection) -> None:
    """Compatibilidade com bancos criados pela versão anterior (coluna
    booleana is_admin em vez do cargo dev/admin/usuario).

    'CREATE TABLE IF NOT EXISTS' não adiciona colunas a uma tabela que já
    existe, então um banco antigo continuaria sem a coluna 'cargo' — por
    isso ela é adicionada aqui explicitamente antes de qualquer leitura/
    escrita que dependa dela. Roda a cada início, mas é idempotente (não
    tem efeito depois da primeira vez)."""
    colunas = {linha["name"] for linha in conn.execute("PRAGMA table_info(usuarios)")}

    if "cargo" not in colunas:
        conn.execute("ALTER TABLE usuarios ADD COLUMN cargo TEXT NOT NULL DEFAULT 'usuario'")

    if "is_admin" in colunas:
        conn.execute("UPDATE usuarios SET cargo = 'admin' WHERE is_admin = 1 AND cargo = 'usuario'")


def _recalcular_percentuais(conn: sqlite3.Connection) -> None:
    """O % de absenteísmo é sempre Horas Ausente / Horas Previstas x 100.
    Bancos importados por versões anteriores guardavam o valor da célula da
    planilha, que estava errado em alguns casos (ex.: hora 18:30 virando
    0,4%). Idempotente: depois da primeira vez não muda nada."""
    conn.execute(
        """UPDATE absenteismo_mensal
           SET percentual_absenteismo =
               CASE WHEN horas_previstas > 0 THEN horas_ausente * 100.0 / horas_previstas ELSE 0 END"""
    )


def _remover_meses_duplicados() -> list[str]:
    """Remove meses cujos dados são cópia exata de outro mês (ex.: a aba
    "Setembro 26" da planilha, copiada de "Agosto 26", que versões
    anteriores importavam como se fosse real). Antes de apagar, cria um
    backup automático. Devolve os meses removidos."""
    with get_connection() as conn:
        linhas = conn.execute(
            """SELECT mes, num_mes, colaborador, dias_trabalhados, horas_previstas,
                      horas_trabalhadas, horas_ausente
               FROM absenteismo_mensal ORDER BY num_mes"""
        ).fetchall()

    por_mes: dict[tuple[int, str], list[tuple]] = {}
    for l in linhas:
        por_mes.setdefault((l["num_mes"], l["mes"]), []).append(
            (l["colaborador"], l["dias_trabalhados"], l["horas_previstas"], l["horas_trabalhadas"], l["horas_ausente"])
        )

    vistos: set[tuple] = set()
    duplicados = []
    for (_, mes), dados in sorted(por_mes.items()):
        assinatura = tuple(sorted(dados))
        if assinatura in vistos:
            duplicados.append(mes)
        vistos.add(assinatura)

    if duplicados:
        criar_backup()
        with get_connection() as conn:
            conn.executemany("DELETE FROM absenteismo_mensal WHERE mes = ?", [(m,) for m in duplicados])
            conn.commit()
    return duplicados


# ---------------------------------------------------------------------------
# Usuários / autenticação
# ---------------------------------------------------------------------------
def criar_usuario(username: str, nome_completo: str, senha: str, cargo: str = "usuario") -> tuple[bool, str]:
    """Cria um novo usuário. Retorna (sucesso, mensagem)."""
    username = username.strip().lower()
    if not username or not nome_completo.strip() or not senha:
        return False, "Nome, usuário e senha são obrigatórios."
    if len(senha) < 8:
        return False, "A senha deve ter pelo menos 8 caracteres."
    if cargo not in CARGOS_VALIDOS:
        return False, f"Cargo inválido: {cargo!r}."

    password_hash = crypto.hash_senha(senha)

    try:
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO usuarios (username, nome_completo, password_hash, cargo, ativo, criado_em)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (username, nome_completo.strip(), password_hash, cargo, datetime.now().isoformat()),
            )
            conn.commit()
        return True, "Usuário criado com sucesso."
    except sqlite3.IntegrityError:
        return False, "Já existe um usuário com esse nome de login."


def verificar_login(username: str, senha: str) -> dict[str, Any] | None:
    """Verifica usuário/senha (username indexado -> busca O(log n)).
    Retorna os dados do usuário se válidos, senão None."""
    username = username.strip().lower()
    with get_connection() as conn:
        linha = conn.execute(
            "SELECT * FROM usuarios WHERE username = ? AND ativo = 1", (username,)
        ).fetchone()

        if linha is None or not crypto.verificar_senha(linha["password_hash"], senha):
            return None

        agora = datetime.now().isoformat()
        if crypto.precisa_rehash(linha["password_hash"]):
            # Atualização transparente: se os parâmetros recomendados do Argon2
            # mudaram (ex.: biblioteca mais nova), a senha é re-hasheada no
            # próximo login bem-sucedido, sem exigir troca manual.
            conn.execute(
                "UPDATE usuarios SET password_hash = ?, ultimo_login = ? WHERE id = ?",
                (crypto.hash_senha(senha), agora, linha["id"]),
            )
        else:
            conn.execute("UPDATE usuarios SET ultimo_login = ? WHERE id = ?", (agora, linha["id"]))
        conn.commit()

    return dict(linha)


def existe_algum_usuario() -> bool:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM usuarios").fetchone()["total"]
    return total > 0


def listar_usuarios() -> list[dict[str, Any]]:
    with get_connection() as conn:
        linhas = conn.execute(
            "SELECT id, username, nome_completo, cargo, ativo, criado_em, ultimo_login "
            "FROM usuarios ORDER BY CASE cargo WHEN 'dev' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, nome_completo"
        ).fetchall()
    return [dict(linha) for linha in linhas]


def alterar_status_usuario(user_id: int, ativo: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE usuarios SET ativo = ? WHERE id = ?", (int(ativo), user_id))
        conn.commit()


def alterar_cargo_usuario(user_id: int, novo_cargo: str) -> tuple[bool, str]:
    if novo_cargo not in CARGOS_VALIDOS:
        return False, f"Cargo inválido: {novo_cargo!r}."
    with get_connection() as conn:
        conn.execute("UPDATE usuarios SET cargo = ? WHERE id = ?", (novo_cargo, user_id))
        conn.commit()
    return True, "Cargo atualizado."


def redefinir_senha(user_id: int, nova_senha: str) -> tuple[bool, str]:
    if len(nova_senha) < 8:
        return False, "A senha deve ter pelo menos 8 caracteres."
    with get_connection() as conn:
        conn.execute(
            "UPDATE usuarios SET password_hash = ? WHERE id = ?",
            (crypto.hash_senha(nova_senha), user_id),
        )
        conn.commit()
    return True, "Senha redefinida com sucesso."


# ---------------------------------------------------------------------------
# Dados de absenteísmo: importação (Excel -> banco) e leitura (banco -> app)
# ---------------------------------------------------------------------------
def importar_dados(df_mensal, df_motivos, usuario: str = "sistema", nome_arquivo: str = "") -> tuple[int, int]:
    """Grava/atualiza no banco os DataFrames já processados a partir do Excel.
    Tudo numa única transação: se algo falhar no meio, nada é gravado.

    Upsert em lote (executemany) sobre colunas indexadas (UNIQUE): O(n log n)
    para n linhas, com uma única viagem de I/O por tabela em vez de uma
    instrução por linha.
    """
    agora = datetime.now().isoformat()

    linhas_mensal = [
        (
            row["Colaborador"], row["Mes"], int(row["NumMes"]),
            float(row["DiasTrabalhados"] or 0), float(row["HorasPrevistas"] or 0),
            float(row["HorasTrabalhadas"] or 0), float(row["HorasAusente"] or 0),
            float(row["PercentualAbsenteismo"] or 0), agora,
        )
        for _, row in df_mensal.iterrows()
    ]
    linhas_motivos = [
        (row["Colaborador"], str(row["Data"]), row["Mes"], row["Motivo"], agora)
        for _, row in df_motivos.iterrows()
    ]

    # Os meses presentes na planilha são substituídos por completo: quem saiu
    # da planilha (ou um nome corrigido) não fica para trás como linha velha
    # no banco. Meses que não vieram na planilha continuam intactos.
    meses_mensal = sorted({linha[1] for linha in linhas_mensal})
    meses_motivos = sorted({linha[2] for linha in linhas_motivos} | set(meses_mensal))

    with get_connection() as conn:
        conn.executemany("DELETE FROM absenteismo_mensal WHERE mes = ?", [(m,) for m in meses_mensal])
        conn.executemany("DELETE FROM absenteismo_motivos WHERE mes = ?", [(m,) for m in meses_motivos])
        conn.executemany(
            """
            INSERT INTO absenteismo_mensal
                (colaborador, mes, num_mes, dias_trabalhados, horas_previstas,
                 horas_trabalhadas, horas_ausente, percentual_absenteismo, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(colaborador, mes) DO UPDATE SET
                num_mes = excluded.num_mes,
                dias_trabalhados = excluded.dias_trabalhados,
                horas_previstas = excluded.horas_previstas,
                horas_trabalhadas = excluded.horas_trabalhadas,
                horas_ausente = excluded.horas_ausente,
                percentual_absenteismo = excluded.percentual_absenteismo,
                atualizado_em = excluded.atualizado_em
            """,
            linhas_mensal,
        )

        if linhas_motivos:
            conn.executemany(
                """
                INSERT INTO absenteismo_motivos (colaborador, data, mes, motivo, atualizado_em)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(colaborador, data, motivo) DO UPDATE SET
                    mes = excluded.mes,
                    atualizado_em = excluded.atualizado_em
                """,
                linhas_motivos,
            )

        conn.execute(
            """INSERT INTO importacoes_log (usuario, arquivo, registros_mensal, registros_motivos, realizado_em)
               VALUES (?, ?, ?, ?, ?)""",
            (usuario, nome_arquivo, len(linhas_mensal), len(linhas_motivos), agora),
        )
        conn.commit()

    return len(linhas_mensal), len(linhas_motivos)


def carregar_dados_do_banco():
    """Lê os dados atuais do banco e devolve (df_mensal, df_motivos) no
    formato usado pelo restante do dashboard."""
    import pandas as pd

    colunas_mensal = [
        "Colaborador", "Mes", "NumMes", "DiasTrabalhados", "HorasPrevistas",
        "HorasTrabalhadas", "HorasAusente", "PercentualAbsenteismo",
    ]
    colunas_motivos = ["Colaborador", "Data", "Mes", "Motivo"]

    # pd.read_sql_query só é testado com o sqlite3 padrão (gera um aviso com
    # a conexão do SQLCipher), então o DataFrame é montado direto do cursor.
    with get_connection() as conn:
        linhas_mensal = conn.execute(
            """SELECT colaborador, mes, num_mes, dias_trabalhados, horas_previstas,
                      horas_trabalhadas, horas_ausente, percentual_absenteismo
               FROM absenteismo_mensal"""
        ).fetchall()
        linhas_motivos = conn.execute(
            "SELECT colaborador, data, mes, motivo FROM absenteismo_motivos"
        ).fetchall()

    df_mensal = pd.DataFrame([tuple(l) for l in linhas_mensal], columns=colunas_mensal)
    df_motivos = pd.DataFrame([tuple(l) for l in linhas_motivos], columns=colunas_motivos)

    if not df_motivos.empty:
        df_motivos["Data"] = pd.to_datetime(df_motivos["Data"]).dt.date

    return df_mensal, df_motivos


def banco_tem_dados() -> bool:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM absenteismo_mensal").fetchone()["total"]
    return total > 0


def ultima_importacao() -> dict[str, Any] | None:
    with get_connection() as conn:
        linha = conn.execute("SELECT * FROM importacoes_log ORDER BY id DESC LIMIT 1").fetchone()
    return dict(linha) if linha else None


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
def criar_backup() -> Path:
    """Copia o arquivo .db atual (já criptografado em disco pelo SQLCipher)
    para a pasta de backups com timestamp no nome. Como o arquivo de origem
    já está cifrado, a cópia também está — nenhum passo extra é necessário."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destino = BACKUP_DIR / f"absenteismo_backup_{timestamp}.db"
    shutil.copy2(DB_PATH, destino)
    return destino


def listar_backups() -> list[Path]:
    return sorted(BACKUP_DIR.glob("absenteismo_backup_*.db"), reverse=True)
