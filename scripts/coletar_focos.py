"""
Ambiente Já - focos de calor (INPE, Programa Queimadas). Só biblioteca padrão do Python.

Fonte: https://dataserver-coids.inpe.br/queimadas/queimadas/focos/csv/
  - anual/Brasil_sat_ref/focos_br_ref_AAAA.zip  -> 2012 até o ano passado (satélite de referência)
  - mensal/Brasil/focos_mensal_br_AAAAMM.zip    -> ano atual (filtramos o mesmo satélite de referência)
  - diario/Brasil/focos_diario_br_AAAAMMDD.csv  -> últimos dias (todos os satélites; só para o mapa)

Por que o satélite de referência: o INPE compara anos usando sempre o mesmo satélite (AQUA_M-T),
senão a conta muda quando entram satélites novos. Aqui: mesma regra, comparação honesta.

Saídas (public/data/focos/):
  <código UF>.json  municípios (por mês, por ano, e "mesmo período"), biomas, pontos recentes
  brasil.json       totais por estado e por bioma (ranking)
  meta.json         data da coleta, data da última leitura, avisos
  cache/AAAAMM.json meses fechados do ano atual (não baixa de novo)

Uso: python scripts/coletar_focos.py --uf MT | --uf todas
"""
from __future__ import annotations

import csv
import io
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import comum as c  # noqa: E402

B = "https://dataserver-coids.inpe.br/queimadas/queimadas/focos/csv/"
SAIDA = c.DADOS_PUBLICOS / "focos"
CACHE = SAIDA / "cache"
ANO_INICIAL = 2012
SAT_REF = "AQUA_M-T"
RE_DATA = re.compile(r"(\d{4})\D(\d{2})\D(\d{2})")
BIOMAS = {"AMAZONIA": "Amazônia", "CERRADO": "Cerrado", "PANTANAL": "Pantanal", "MATA ATLANTICA": "Mata Atlântica",
          "CAATINGA": "Caatinga", "PAMPA": "Pampa"}


def bioma_nome(txt: str) -> str:
    return BIOMAS.get(c.sem_acento(txt or "").strip().upper(), (txt or "").strip().title() or "Sem informação")


class Acumulador:
    """Conta focos: por município (ano, mês), por município no 'mesmo período' e por bioma."""

    def __init__(self, casador, mes_ref: int, dia_ref: int):
        self.casador, self.mes_ref, self.dia_ref = casador, mes_ref, dia_ref
        self.mun = defaultdict(lambda: defaultdict(lambda: [0] * 12))            # cod -> ano -> 12 meses
        self.per = defaultdict(lambda: defaultdict(int))                          # cod -> ano -> mesmo período
        self.bio = defaultdict(lambda: defaultdict(lambda: [0, 0]))               # (uf,bioma) -> ano -> [total, período]
        self.uf_ano = defaultdict(lambda: defaultdict(lambda: [0, 0]))            # uf -> ano -> [total, período]
        self.uf_de = {}                                                           # cod -> uf
        self.sem_mun = 0

    def no_periodo(self, mes: int, dia: int) -> bool:
        return (mes, dia) <= (self.mes_ref, self.dia_ref)

    def somar(self, ano, mes, dia, uf, cod, bioma, n=1, periodo=None):
        if periodo is None:
            periodo = self.no_periodo(mes, dia)
        if cod:
            self.mun[cod][ano][mes - 1] += n
            self.uf_de[cod] = uf
            if periodo:
                self.per[cod][ano] += n
        else:
            self.sem_mun += n
        t = self.uf_ano[uf][ano]
        t[0] += n
        b = self.bio[(uf, bioma)][ano]
        b[0] += n
        if periodo:
            t[1] += n
            b[1] += n


def achar_cod(casador, validos, id_txt, nome, uf):
    id_txt = (id_txt or "").strip()
    if id_txt.isdigit() and int(id_txt) in validos:
        return int(id_txt)
    if nome and uf:
        return casador.codigo(nome.strip(), uf.strip())
    return None


def uf_sigla(txt: str) -> str | None:
    t = c.sem_acento(txt or "").strip().upper()
    if t in c.SIGLAS:
        return t
    for sg, nm in c.NOMES_UF.items():
        if c.sem_acento(nm).upper() == t:
            return sg
    return None


def leitor_csv(binario):
    texto = io.TextIOWrapper(binario, encoding="utf-8", errors="replace", newline="")
    for linha in csv.DictReader(texto):
        yield {(k or "").strip().lower(): (v or "").strip() for k, v in linha.items()}


def baixar_zip(url: str) -> bytes:
    with c.abrir(url, tentativas=3, timeout=180) as r:
        return r.read()


# ---------------------------------------------------------------- histórico (anuais)

def ler_anual(ano: int, acc: Acumulador, validos: set) -> int:
    url = f"{B}anual/Brasil_sat_ref/focos_br_ref_{ano}.zip"
    z = zipfile.ZipFile(io.BytesIO(baixar_zip(url)))
    nome = next(n for n in z.namelist() if n.lower().endswith(".csv"))
    n = 0
    with z.open(nome) as f:
        for r in leitor_csv(f):
            m = RE_DATA.search(r.get("data_pas") or r.get("data_hora_gmt") or "")
            uf = uf_sigla(r.get("estado", ""))
            if not m or not uf or (r.get("pais") and "BRASIL" not in c.sem_acento(r["pais"]).upper()):
                continue
            cod = achar_cod(acc.casador, validos, r.get("municipio_id"), r.get("municipio"), uf)
            acc.somar(int(m.group(1)), int(m.group(2)), int(m.group(3)), uf, cod, bioma_nome(r.get("bioma", "")))
            n += 1
    return n


# ---------------------------------------------------------------- ano atual (mensais)

def abrir_mensal(ano: int, mes: int):
    base = f"{B}mensal/Brasil/focos_mensal_br_{ano}{mes:02d}"
    try:
        z = c.ZipRemoto(base + ".zip")
        return z.abrir(z.procurar(".csv"))
    except Exception:  # noqa: BLE001
        return c.abrir(base + ".csv", tentativas=3, timeout=300)


def ler_mensal(ano: int, mes: int, acc: Acumulador, validos: set):
    """Lê um mês do ano atual (só satélite de referência). Devolve (focos, última data vista)."""
    n, ultima = 0, None
    with abrir_mensal(ano, mes) as f:
        for r in leitor_csv(f):
            if r.get("satelite") != SAT_REF:
                continue
            m = RE_DATA.search(r.get("data_hora_gmt", ""))
            uf = uf_sigla(r.get("estado", ""))
            if not m or not uf:
                continue
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            ultima = max(ultima, d) if ultima else d
            cod = achar_cod(acc.casador, validos, r.get("municipio_id"), r.get("municipio"), uf)
            acc.somar(d.year, d.month, d.day, uf, cod, bioma_nome(r.get("bioma", "")))
            n += 1
    return n, ultima


# ---------------------------------------------------------------- últimos dias (mapa)

def ler_diarios(hoje: date, validos, casador, dias=9):
    pontos, ultima = [], None
    for k in range(dias):
        d = hoje - timedelta(days=k)
        url = f"{B}diario/Brasil/focos_diario_br_{d:%Y%m%d}.csv"
        try:
            with c.abrir(url, tentativas=2, timeout=120) as f:
                linhas = list(leitor_csv(f))
        except Exception as e:  # noqa: BLE001
            c.log(f"   {d}: sem arquivo ({str(e)[:60]})")
            continue
        ultima = ultima or d
        for r in linhas:
            uf = uf_sigla(r.get("estado", ""))
            m = RE_DATA.search(r.get("data_hora_gmt", ""))
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
            except (KeyError, ValueError):
                continue
            if not uf or not m:
                continue
            cod = achar_cod(casador, validos, r.get("municipio_id"), r.get("municipio"), uf)
            risco = c.numero(r.get("risco_fogo"))
            frp = c.numero(r.get("frp"))
            pontos.append([uf, round(lat, 3), round(lon, 3), cod or 0, f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                           None if risco is None or risco < 0 else round(risco, 2),
                           None if frp is None or frp < 0 else round(frp, 1), r.get("satelite", "")])
    return pontos, ultima


def main() -> None:
    ufs = c.ufs_escolhidas("MT")
    SAIDA.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    mun = c.ler_json(c.DADOS_PUBLICOS / "municipios.json", {}) or {}
    validos = {m[0] for m in mun.get("m", [])}
    nomes = {m[0]: m[1] for m in mun.get("m", [])}
    casador = c.CasadorMunicipios()
    hoje = datetime.now(timezone.utc).date()
    ano_atual = hoje.year

    # 1) últimos dias (descobre a data da última leitura)
    c.log("== Últimos dias (diários)")
    pontos, ultima_diaria = ler_diarios(hoje, validos, casador)
    c.log(f"   {len(pontos):,} focos, última leitura {ultima_diaria}")
    if not ultima_diaria:
        raise SystemExit("Nenhum arquivo diário respondeu: INPE fora do ar? Mantendo os dados anteriores.")
    # data de referência: o último dia COMPLETO (um dia antes da última leitura)
    ref = ultima_diaria - timedelta(days=1)
    acc = Acumulador(casador, ref.month, ref.day)
    c.log(f"   data de referência da comparação: {ref:%d/%m/%Y}")

    # 2) histórico anual
    c.log("== Histórico (anuais, satélite de referência)")
    for ano in range(ANO_INICIAL, ano_atual):
        try:
            c.log(f"   {ano}: {ler_anual(ano, acc, validos):,} focos")
        except Exception as e:  # noqa: BLE001
            c.log(f"   {ano}: FALHOU ({str(e)[:100]})")

    # 3) ano atual, mês a mês (meses fechados ficam em cache)
    c.log(f"== {ano_atual} (mensais, só {SAT_REF})")
    for mes in range(1, ref.month + 1):
        arq = CACHE / f"{ano_atual}{mes:02d}.json"
        fechado = mes < ref.month
        cache = c.ler_json(arq) if fechado else None
        if cache:
            for cod, n, uf in cache["m"]:
                acc.mun[cod][ano_atual][mes - 1] += n
                acc.per[cod][ano_atual] += n
                acc.uf_de[cod] = uf
            for uf, bio, n in cache["b"]:
                for t in (acc.bio[(uf, bio)][ano_atual], acc.uf_ano[uf][ano_atual]):
                    t[0] += n
                    t[1] += n
            c.log(f"   {mes:02d}: cache ({sum(x[2] for x in cache['b']):,} focos)")
            continue
        parcial = Acumulador(casador, ref.month, ref.day)
        try:
            n, ult = ler_mensal(ano_atual, mes, parcial, validos)
        except Exception as e:  # noqa: BLE001
            c.log(f"   {mes:02d}: FALHOU ({str(e)[:100]})")
            continue
        c.log(f"   {mes:02d}: {n:,} focos (última data {ult})")
        # junta ao acumulador geral; no mês de referência só entram dias até o dia de referência no "período"
        for cod, anos in parcial.mun.items():
            for a, meses in anos.items():
                for i, v in enumerate(meses):
                    acc.mun[cod][a][i] += v
                acc.uf_de[cod] = parcial.uf_de[cod]
        for cod, anos in parcial.per.items():
            for a, v in anos.items():
                acc.per[cod][a] += v
        for k, anos in parcial.bio.items():
            for a, (t, p) in anos.items():
                acc.bio[k][a][0] += t
                acc.bio[k][a][1] += p
        for uf, anos in parcial.uf_ano.items():
            for a, (t, p) in anos.items():
                acc.uf_ano[uf][a][0] += t
                acc.uf_ano[uf][a][1] += p
        acc.sem_mun += parcial.sem_mun
        if fechado:
            lin = [[cod, sum(anos[ano_atual]), parcial.uf_de[cod]] for cod, anos in parcial.mun.items()]
            bios = [[uf, bio, anos[ano_atual][0]] for (uf, bio), anos in parcial.bio.items()]
            if bios:   # nunca grava cache de mês vazio (leitura que falhou)
                c.salvar_json(arq, {"mes": mes, "m": lin, "b": bios})
    c.log(f"   focos sem município identificado: {acc.sem_mun:,}")
    if casador.faltando:
        c.log(f"   nomes sem código IBGE ({len(casador.faltando)}): {sorted(casador.faltando)[:15]}")

    gerado = c.agora_iso()
    # 4) brasil.json: estados e biomas
    brasil = {"uf": {uf: {str(a): v for a, v in anos.items()} for uf, anos in acc.uf_ano.items()}, "bioma": {}}
    for (uf, bio), anos in acc.bio.items():
        g = brasil["bioma"].setdefault(bio, {})
        for a, (t, p) in anos.items():
            x = g.setdefault(str(a), [0, 0])
            x[0] += t
            x[1] += p
    c.salvar_json(SAIDA / "brasil.json", brasil)

    # 5) um arquivo por UF
    for uf in ufs:
        cod_uf = c.SIGLAS[uf]
        muns = {}
        for cod, anos in acc.mun.items():
            if cod // 100000 != cod_uf:
                continue
            muns[str(cod)] = {"n": nomes.get(cod, str(cod)),
                              "a": {str(a): v for a, v in sorted(anos.items())},
                              "p": {str(a): v for a, v in sorted(acc.per[cod].items())}}
        bio = {b: {str(a): v for a, v in sorted(anos.items())} for (u, b), anos in acc.bio.items() if u == uf}
        rec = [[p[1], p[2], p[3], p[4], p[5], p[6], p[7]] for p in pontos if p[0] == uf]
        c.salvar_json(SAIDA / f"{cod_uf}.json", {"uf": uf, "m": muns, "bioma": bio, "rec": rec})
        c.log(f"   {uf}: {len(muns)} municípios, {len(rec)} focos recentes")

    anos_ok = sorted({a for anos in acc.uf_ano.values() for a in anos})
    meta_ant = c.ler_json(SAIDA / "meta.json", {}) or {}
    ufs_disp = sorted(set(meta_ant.get("ufs", [])) | set(ufs))
    c.salvar_json(SAIDA / "meta.json", {
        "coletado_em": gerado, "ultima_leitura": ultima_diaria.isoformat(), "referencia": ref.isoformat(),
        "anos": anos_ok, "ano_atual": ano_atual, "ufs": ufs_disp, "satelite_ref": SAT_REF,
        "fonte": "INPE, Programa Queimadas (dados abertos)"})
    c.resumo_github([f"Focos de calor: referência {ref:%d/%m/%Y}, anos {anos_ok[0]}–{anos_ok[-1]}, UFs {', '.join(ufs)}"])


if __name__ == "__main__":
    main()
