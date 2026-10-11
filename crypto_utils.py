# =====================================================================================
# CRYPTO_UTILS.PY - Segurança criptográfica do sistema
#
#   1) Hash de senhas com Argon2id (vencedor da Password Hashing Competition,
#      recomendado pela OWASP). Intencionalmente "caro" em tempo e memória:
#      isso é uma escolha de complexidade proposital (custo O(memory_cost) por
#      tentativa) para tornar ataques de força bruta/GPU inviáveis, mesmo que
#      o hash vaze.
#   2) Gestão da chave mestra usada para criptografar o arquivo do banco de
#      dados (SQLCipher, ver database.py) com AES-256.
#   3) Gestão da SECRET_KEY do Flask, usada para assinar o cookie de sessão
#      (é o que impede um usuário de forjar/adulterar seu próprio cookie de
#      login — sem ela, qualquer um poderia se passar por outro usuário só
#      editando o cookie no navegador).
#
# Nenhuma senha ou chave é manipulada fora deste módulo — ponto único de
# responsabilidade sobre criptografia (clean code / SRP).
# =====================================================================================

import os
import stat
import secrets
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHash

BASE_DIR = Path(__file__).resolve().parent
# Pasta dos dados (banco, backups e arquivos de chave). Por padrão é a pasta
# do projeto; no Docker aponta para um volume, para sobreviver a atualizações.
DATA_DIR = Path(os.environ.get("ABSENTEISMO_DATA_DIR") or BASE_DIR).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
ARQUIVO_CHAVE_DB = DATA_DIR / "db.key"
ARQUIVO_CHAVE_SESSAO = DATA_DIR / "secret.key"
VARIAVEL_AMBIENTE_CHAVE_DB = "ABSENTEISMO_DB_KEY"
VARIAVEL_AMBIENTE_CHAVE_SESSAO = "ABSENTEISMO_SECRET_KEY"
TAMANHO_CHAVE_BYTES = 32  # AES-256 / assinatura de sessão de 256 bits

# ---------------------------------------------------------------------------
# Hash de senhas (Argon2id)
# ---------------------------------------------------------------------------
# Parâmetros padrão da biblioteca já seguem a recomendação da OWASP
# (time_cost=3, memory_cost=64 MiB, parallelism=4). O hash resultante já
# guarda algoritmo, versão, custos e salt (gerado aleatoriamente por chamada),
# então não é preciso administrar um campo de "salt" separado no banco.
_hasher = PasswordHasher()


def hash_senha(senha: str) -> str:
    """Gera o hash Argon2id de uma senha em texto plano."""
    return _hasher.hash(senha)


def verificar_senha(hash_armazenado: str, senha: str) -> bool:
    """Compara uma senha em texto plano com um hash já armazenado.
    A verificação interna da biblioteca é resistente a timing attacks."""
    try:
        _hasher.verify(hash_armazenado, senha)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False
    return True


def precisa_rehash(hash_armazenado: str) -> bool:
    """True quando o hash foi gerado com parâmetros mais fracos que os atuais
    (ex.: biblioteca foi atualizada com custos maiores). Permite re-hash
    transparente no próximo login, sem forçar troca de senha."""
    return _hasher.check_needs_rehash(hash_armazenado)


# ---------------------------------------------------------------------------
# Resolução genérica de segredos hexadecimais (chave do banco e secret key
# de sessão seguem exatamente o mesmo padrão: variável de ambiente primeiro,
# depois arquivo local criado automaticamente na primeira execução).
# ---------------------------------------------------------------------------
def _validar_chave_hex(chave_hex: str, rotulo: str) -> None:
    if len(chave_hex) != TAMANHO_CHAVE_BYTES * 2:
        raise ValueError(
            f"{rotulo} inválida: esperado {TAMANHO_CHAVE_BYTES * 2} "
            f"caracteres hexadecimais, recebido {len(chave_hex)}."
        )
    int(chave_hex, 16)  # ValueError se não for hexadecimal


def _resolver_segredo_hex(variavel_ambiente: str, arquivo: Path, rotulo: str) -> str:
    """Resolve um segredo de 256 bits nesta ordem de prioridade:

    1. Variável de ambiente informada (recomendado em produção — mantém o
       segredo fora do disco onde a aplicação roda).
    2. Arquivo local, criado automaticamente no primeiro uso, com permissão
       restrita ao dono (chmod 600 em sistemas POSIX).

    O segredo nunca é derivado de nenhuma senha: é gerado com um CSPRNG
    (secrets.token_hex), independente de qualquer entrada do usuário.
    """
    valor_env = os.environ.get(variavel_ambiente)
    if valor_env:
        _validar_chave_hex(valor_env, rotulo)
        return valor_env

    if arquivo.exists():
        valor = arquivo.read_text(encoding="utf-8").strip()
        _validar_chave_hex(valor, rotulo)
        return valor

    valor = secrets.token_hex(TAMANHO_CHAVE_BYTES)
    arquivo.write_text(valor, encoding="utf-8")
    try:
        os.chmod(arquivo, stat.S_IRUSR | stat.S_IWUSR)  # somente o dono lê/escreve
    except OSError:
        pass  # Windows sem suporte a chmod POSIX: a proteção real fica a cargo do NTFS
    return valor


def obter_chave_banco_hex() -> str:
    """Chave AES-256 usada pelo SQLCipher para criptografar o arquivo do
    banco de dados inteiro (ver database.py)."""
    return _resolver_segredo_hex(VARIAVEL_AMBIENTE_CHAVE_DB, ARQUIVO_CHAVE_DB, "Chave de banco")


def obter_secret_key_sessao() -> str:
    """Chave usada pelo Flask para assinar (HMAC) o cookie de sessão. Sem
    ela ser secreta e estável entre reinícios do processo, um usuário
    poderia forjar o próprio cookie e se passar por outra pessoa, ou seria
    deslogado a cada reinício do servidor."""
    return _resolver_segredo_hex(VARIAVEL_AMBIENTE_CHAVE_SESSAO, ARQUIVO_CHAVE_SESSAO, "Secret key de sessão")
