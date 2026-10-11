# =====================================================================================
# EXCEL_IMPORT.PY - Leitura da planilha de origem (.xlsx) e conversão para DataFrames
# prontos para gravar no banco. Não depende do Flask, então pode ser testado
# isoladamente.
#
# A planilha de origem tem alguns problemas conhecidos que este módulo trata
# no momento da leitura, em vez de confiar cegamente nas células:
#   - % de absenteísmo: sempre recalculado como Horas Ausente / Horas
#     Previstas x 100 (a regra escrita na própria planilha). A célula da
#     planilha não é usada: em Julho/Agosto o total usa o denominador errado
#     e em Junho uma célula em formato de hora (18:30) gerava 0,4% em vez de 10%.
#   - Horas no formato "hh.mm": a planilha anota 24.33 querendo dizer
#     24h33min (nenhuma "Horas Ausente" tem parte decimal >= .60, e
#     trabalhadas + ausentes fecha em previstas - 0,40 na maioria das linhas,
#     exatamente o que acontece somando minutos como se fossem centésimos).
#     Esses valores são convertidos para horas decimais (24h33 -> 24,55h).
#   - Aba duplicada: uma aba mensal com exatamente os mesmos dados de outra
#     (ex.: "Setembro 26" copiada de "Agosto 26") é ignorada, com aviso.
#   - Nomes divergentes: o mesmo colaborador escrito de formas diferentes
#     entre abas (ex.: "Maria Aparecida Souza Lima" x "Maria Souza Lima")
#     é unificado num único nome, para os filtros e cruzamentos funcionarem.
# =====================================================================================

from __future__ import annotations

import re
import unicodedata
from datetime import time as dtime, datetime as dt, timedelta
from difflib import SequenceMatcher

import openpyxl
import pandas as pd

ABAS_MENSAIS = {
    "Junho 26": ("Junho", 6),
    "Julho 26": ("Julho", 7),
    "Agosto 26": ("Agosto", 8),
    "Setembro 26": ("Setembro", 9),
}
NOME_ABA_MOTIVOS = "Faltas e Atestados"

MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

# Liga/desliga a leitura de "24.33" como 24h33min (ver cabeçalho do arquivo).
HORAS_NO_FORMATO_HH_MM = True

# Diferença (em horas) entre previstas e trabalhadas + ausentes a partir da
# qual a linha é apontada como inconsistente no aviso da importação.
TOLERANCIA_FECHAMENTO_HORAS = 0.5


# ---------------------------------------------------------------------------
# Conversões de valores de célula
# ---------------------------------------------------------------------------
def _hhmm_para_decimal(valor: float) -> float:
    """24.33 (24h33min) -> 24.55. Valores com parte decimal >= .60 não
    podem ser minutos, então já estão em horas decimais e voltam como estão."""
    if valor < 0:
        return valor
    horas = int(valor)
    minutos = round((valor - horas) * 100)
    if minutos >= 60:
        return valor
    return horas + minutos / 60


def _converter_para_horas(valor, hhmm: bool = False) -> float:
    """Converte o conteúdo de uma célula de horas para horas decimais.
    Aceita número, hora do Excel (18:30 -> 18,5) e duração (timedelta).
    Com hhmm=True, um número como 24.33 é lido como 24h33min."""
    if valor is None or valor == "":
        return 0.0
    if isinstance(valor, dtime):
        return valor.hour + valor.minute / 60 + valor.second / 3600
    if isinstance(valor, timedelta):
        return valor.total_seconds() / 3600
    if isinstance(valor, str):
        valor = valor.strip().replace(",", ".")
        if ":" in valor:
            partes = valor.split(":")
            try:
                return int(partes[0]) + int(partes[1]) / 60
            except (ValueError, IndexError):
                return 0.0
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return _hhmm_para_decimal(numero) if hhmm else numero


def _ler_horas_da_linha(previstas: float, valor_trab, valor_aus) -> tuple[float, float]:
    """Devolve (trabalhadas, ausente) em horas decimais.

    A planilha mistura as duas notações: na maioria das linhas 24.33 quer
    dizer 24h33min, mas em algumas os números foram digitados já em horas
    decimais (178.42 + 6.58 = 185 exatos). Por linha, escolhe a leitura que
    faz trabalhadas + ausentes fechar com as previstas; se nenhuma fechar
    (linha inconsistente na própria planilha), usa a leitura hh.mm, que é a
    predominante."""
    aus_dec = _converter_para_horas(valor_aus)
    trab_dec = _converter_para_horas(valor_trab) if valor_trab is not None else max(previstas - aus_dec, 0.0)
    if not HORAS_NO_FORMATO_HH_MM or abs(previstas - trab_dec - aus_dec) < 0.01:
        return trab_dec, aus_dec

    aus_hhmm = _converter_para_horas(valor_aus, hhmm=True)
    trab_hhmm = _converter_para_horas(valor_trab, hhmm=True) if valor_trab is not None else max(previstas - aus_hhmm, 0.0)
    return trab_hhmm, aus_hhmm


def _numero(valor) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Unificação de nomes
# ---------------------------------------------------------------------------
def _normalizar_nome(nome: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", sem_acento.lower())).strip()


def _parecido(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


class _UnificadorNomes:
    """Mantém a lista de nomes "oficiais" (os das abas mensais, priorizando
    o mês mais recente) e resolve variações de grafia para eles."""

    def __init__(self) -> None:
        self.oficiais: list[str] = []
        self._normalizados: dict[str, str] = {}
        self.unificados: dict[str, str] = {}  # variação -> oficial (para o aviso)

    def registrar(self, nome: str) -> str:
        """Registra um nome vindo de uma aba mensal. Variações muito
        próximas (erro de digitação, acento) são fundidas no já existente."""
        norm = _normalizar_nome(nome)
        if norm in self._normalizados:
            return self._normalizados[norm]
        for norm_oficial, oficial in self._normalizados.items():
            if _parecido(norm, norm_oficial) >= 0.92:
                self.unificados[nome] = oficial
                self._normalizados[norm] = oficial
                return oficial
        self.oficiais.append(nome)
        self._normalizados[norm] = nome
        return nome

    def resolver(self, nome: str) -> str:
        """Resolve um nome da aba de faltas/atestados para o nome oficial.
        Aceita a correspondência quando o primeiro nome é praticamente igual
        e o segundo nome ou o último sobrenome também batem, e só se houver
        um único candidato — na dúvida, mantém o nome como veio."""
        norm = _normalizar_nome(nome)
        if norm in self._normalizados:
            return self._normalizados[norm]

        tokens = norm.split()
        candidatos = []
        for norm_oficial, oficial in self._normalizados.items():
            t_of = norm_oficial.split()
            if not tokens or not t_of or _parecido(tokens[0], t_of[0]) < 0.85:
                continue
            mesmo_segundo = len(tokens) > 1 and len(t_of) > 1 and tokens[1] == t_of[1]
            mesmo_ultimo = len(tokens) > 1 and len(t_of) > 1 and tokens[-1] == t_of[-1]
            if mesmo_segundo or mesmo_ultimo:
                candidatos.append((_parecido(norm, norm_oficial), oficial))

        oficiais_distintos = {o for _, o in candidatos}
        if len(oficiais_distintos) != 1:
            return nome
        oficial = candidatos[0][1]
        self.unificados[nome] = oficial
        self._normalizados[norm] = oficial
        return oficial


# ---------------------------------------------------------------------------
# Leitura da planilha
# ---------------------------------------------------------------------------
def _ler_aba_mensal(ws) -> list[dict]:
    linha_cabecalho = next(
        (r for r in range(1, 31) if str(ws.cell(row=r, column=1).value or "").strip() == "Nome do colaborador"),
        None,
    )
    if not linha_cabecalho:
        return []

    linhas = []
    for r in range(linha_cabecalho + 1, ws.max_row + 1):
        nome = ws.cell(row=r, column=1).value
        if not nome or not str(nome).strip():
            # Linha sem nome: se também não tem horas previstas, é o fim
            # da tabela (a aba copiada de Setembro perdeu o rótulo
            # "Somatória", então não dá para depender só dele).
            if linhas and ws.cell(row=r, column=6).value is not None:
                break
            continue
        if isinstance(nome, str) and nome.strip().lower().startswith("somat"):
            break

        previstas = _numero(ws.cell(row=r, column=6).value)
        trabalhadas, ausente = _ler_horas_da_linha(
            previstas, ws.cell(row=r, column=8).value, ws.cell(row=r, column=10).value
        )

        linhas.append({
            "Colaborador": str(nome).strip(),
            "DiasTrabalhados": _numero(ws.cell(row=r, column=4).value),
            "HorasPrevistas": previstas,
            "HorasTrabalhadas": round(trabalhadas, 2),
            "HorasAusente": round(ausente, 2),
            "PercentualAbsenteismo": (ausente / previstas * 100) if previstas else 0.0,
        })
    return linhas


def _assinatura(linhas: list[dict]) -> tuple:
    return tuple(sorted(
        (l["Colaborador"], l["DiasTrabalhados"], l["HorasPrevistas"], l["HorasTrabalhadas"], l["HorasAusente"])
        for l in linhas
    ))


def processar_planilha(arquivo_excel):
    """Lê um arquivo .xlsx (caminho ou arquivo enviado pelo navegador) e
    devolve (df_mensal, df_motivos, avisos), onde avisos é uma lista de
    frases descrevendo o que foi corrigido ou ignorado na leitura."""
    wb = openpyxl.load_workbook(arquivo_excel, data_only=True)
    avisos: list[str] = []

    # 1) Lê as abas mensais. A deduplicação de abas é feita antes de
    #    registrar nomes, para uma aba copiada não influenciar nada.
    abas_lidas: list[tuple[str, int, list[dict]]] = []
    assinaturas: dict[tuple, str] = {}
    for nome_aba, (nome_mes, num_mes) in ABAS_MENSAIS.items():
        if nome_aba not in wb.sheetnames:
            continue
        linhas = _ler_aba_mensal(wb[nome_aba])
        if not linhas:
            continue
        assinatura = _assinatura(linhas)
        if assinatura in assinaturas:
            avisos.append(
                f"A aba \"{nome_aba}\" tem exatamente os mesmos dados de \"{assinaturas[assinatura]}\" "
                f"(cópia não preenchida) e foi ignorada."
            )
            continue
        assinaturas[assinatura] = nome_aba
        abas_lidas.append((nome_mes, num_mes, linhas))

    # 2) Unifica nomes, do mês mais recente para o mais antigo (o nome mais
    #    recente vira o oficial).
    unificador = _UnificadorNomes()
    for _, _, linhas in sorted(abas_lidas, key=lambda a: -a[1]):
        for linha in linhas:
            linha["Colaborador"] = unificador.registrar(linha["Colaborador"])

    linhas_mensal = []
    inconsistentes = []
    for nome_mes, num_mes, linhas in abas_lidas:
        for linha in linhas:
            linha.update({"Mes": nome_mes, "NumMes": num_mes})
            linhas_mensal.append(linha)
            diferenca = linha["HorasPrevistas"] - linha["HorasTrabalhadas"] - linha["HorasAusente"]
            if abs(diferenca) > TOLERANCIA_FECHAMENTO_HORAS:
                inconsistentes.append(f"{linha['Colaborador']} ({nome_mes}, diferença de {diferenca:+.1f}h)")
    if inconsistentes:
        avisos.append(
            "Horas previstas não batem com trabalhadas + ausentes em: " + "; ".join(inconsistentes)
            + ". O % usa Horas Ausente / Horas Previstas; vale conferir essas linhas na planilha."
        )

    colunas_mensal = [
        "Colaborador", "Mes", "NumMes", "DiasTrabalhados", "HorasPrevistas",
        "HorasTrabalhadas", "HorasAusente", "PercentualAbsenteismo",
    ]
    df_mensal = pd.DataFrame(linhas_mensal, columns=colunas_mensal)

    # 3) Aba de faltas/atestados (uma data por célula)
    linhas_motivos = []
    if NOME_ABA_MOTIVOS in wb.sheetnames:
        ws = wb[NOME_ABA_MOTIVOS]
        linha_cabecalho = next(
            (r for r in range(1, 11) if ws.cell(row=r, column=1).value and "Motivo" in str(ws.cell(row=r, column=1).value)),
            None,
        )

        if linha_cabecalho:
            colunas = [
                (c, str(ws.cell(row=linha_cabecalho, column=c).value).strip())
                for c in range(2, ws.max_column + 1)
                if ws.cell(row=linha_cabecalho, column=c).value
            ]
            colaborador_atual = None

            for r in range(linha_cabecalho + 1, ws.max_row + 1):
                nome = ws.cell(row=r, column=1).value
                if nome and str(nome).strip():
                    if str(nome).strip().lower() == "nomes":
                        continue
                    colaborador_atual = unificador.resolver(str(nome).strip())
                if not colaborador_atual:
                    continue

                for col_idx, titulo in colunas:
                    valor = ws.cell(row=r, column=col_idx).value
                    if isinstance(valor, dt):
                        titulo_min = titulo.lower()
                        motivo = "Atestado" if titulo_min.startswith("atest") else "Falta" if titulo_min.startswith("falta") else "Campanha"
                        linhas_motivos.append({
                            "Colaborador": colaborador_atual,
                            "Data": valor.date(),
                            "Mes": MESES_PT[valor.month],
                            "Motivo": motivo,
                        })

    if unificador.unificados:
        avisos.append(
            "Nomes unificados (mesma pessoa escrita de formas diferentes): "
            + "; ".join(f"\"{v}\" → \"{o}\"" for v, o in unificador.unificados.items())
            + "."
        )

    df_motivos = pd.DataFrame(linhas_motivos, columns=["Colaborador", "Data", "Mes", "Motivo"])
    if not df_motivos.empty:
        df_motivos = df_motivos.drop_duplicates(subset=["Colaborador", "Data", "Motivo"])

    return df_mensal, df_motivos, avisos
