#!/usr/bin/env python3
"""
Monitor de preço de passagens aéreas, para várias viagens.

As viagens ficam em viagens.json. Para cada viagem ativa, consulta o Google Flights
via SerpApi, guarda o histórico em data/<id>.csv, calcula tendência e manda
alertas no Telegram.
"""
import csv
import json
import os
import statistics
from datetime import date, datetime, timedelta, timezone

import requests

BRT = timezone(timedelta(hours=-3))
COTA_MENSAL = 250

# horário (Brasília) de cada checagem; precisa bater com o cron do monitor.yml
HORARIOS = {"madrugada": 3, "manha": 8, "tarde": 14, "noite": 20}

# valores usados quando a viagem não define o campo
PADRAO = {
    "nome": None,
    "volta": None,                    # null = só ida
    "janela_ida": [0, 24],            # [hora inicial, hora final] da partida
    "janela_volta": [0, 24],
    "adultos": 1,
    "companhia_preferida": None,      # ex.: "LATAM"; null = sempre a mais barata
    "vantagem_minima_outra_cia": 0.15,
    "max_paradas": None,              # null = qualquer; 0 = só direto; 1 = até 1 conexão
    "alerta_queda_reais": 40,
    "alerta_queda_pct": 0.04,
    "checagens": ["manha", "tarde", "noite"],
    "ativa": True,
}

CAMPOS = [
    "timestamp", "total", "ida_preco", "ida_cia", "ida_horario", "ida_paradas",
    "volta_preco", "volta_cia", "volta_horario", "volta_paradas",
    "total_preferida", "total_mais_barato", "nivel_google_ida", "nivel_google_volta",
    "recomendacao",
]


# ---------------------------------------------------------------- configuração
def carregar_viagens():
    if not os.path.exists("viagens.json"):
        return []
    with open("viagens.json", encoding="utf-8") as f:
        brutas = json.load(f).get("viagens", [])
    viagens = []
    for b in brutas:
        v = {**PADRAO, **b}
        v["nome"] = v["nome"] or f"{v['origem']}-{v['destino']} {v['ida']}"
        viagens.append(v)
    return viagens


def buscas_por_mes(v):
    return (2 if v["volta"] else 1) * len(v["checagens"]) * 31


def link_google(v):
    base = f"https://www.google.com/travel/flights?q=Flights%20from%20{v['origem']}%20to%20{v['destino']}%20on%20{v['ida']}"
    return base + (f"%20through%20{v['volta']}" if v["volta"] else "")


def slot_atual(agora):
    """Qual checagem é esta (o GitHub às vezes atrasa a execução alguns minutos)."""
    minutos = agora.hour * 60 + agora.minute
    def dist(h):
        d = abs(minutos - h * 60)
        return min(d, 1440 - d)
    return min(HORARIOS, key=lambda s: dist(HORARIOS[s]))


# ---------------------------------------------------------------- busca
def buscar(v, origem, destino, data, janela):
    params = {
        "engine": "google_flights",
        "departure_id": origem,
        "arrival_id": destino,
        "outbound_date": data,
        "type": 2,
        "adults": v["adultos"],
        "currency": "BRL",
        "gl": "br",
        "hl": "en",
        "api_key": os.environ["SERPAPI_KEY"],
    }
    if janela != [0, 24]:
        params["outbound_times"] = f"{janela[0]},{max(janela[0], janela[1] - 1)}"
    if v["max_paradas"] is not None:
        params["stops"] = {0: 1, 1: 2, 2: 3}.get(v["max_paradas"], 0)

    r = requests.get("https://serpapi.com/search.json", params=params, timeout=90)
    r.raise_for_status()
    dados = r.json()
    if "error" in dados and not dados.get("best_flights") and not dados.get("other_flights"):
        if "no results" not in dados["error"].lower():
            raise RuntimeError(dados["error"])

    opcoes = []
    for grupo in ("best_flights", "other_flights"):
        for o in dados.get(grupo) or []:
            trechos = o.get("flights") or []
            if not trechos or not o.get("price"):
                continue
            partida = trechos[0]["departure_airport"]["time"]  # "2027-02-18 07:30"
            minutos = int(partida[11:13]) * 60 + int(partida[14:16])
            if not (janela[0] * 60 <= minutos <= janela[1] * 60):
                continue
            opcoes.append({
                "preco": float(o["price"]),
                "cia": " / ".join(sorted({t.get("airline", "?") for t in trechos})),
                "horario": f"{partida[11:16]}-{trechos[-1]['arrival_airport']['time'][11:16]}",
                "paradas": len(trechos) - 1,
            })
    return opcoes, dados.get("price_insights") or {}


def escolher(v, opcoes):
    """Retorna (escolhida, melhor_da_preferida, mais_barata)."""
    if not opcoes:
        return None, None, None
    mais_barata = min(opcoes, key=lambda o: o["preco"])
    pref = v["companhia_preferida"]
    prefs = [o for o in opcoes if pref and pref.lower() in o["cia"].lower()]
    melhor_pref = min(prefs, key=lambda o: o["preco"]) if prefs else None
    if melhor_pref is None:
        return mais_barata, None, mais_barata
    if mais_barata["preco"] <= melhor_pref["preco"] * (1 - v["vantagem_minima_outra_cia"]):
        return mais_barata, melhor_pref, mais_barata
    return melhor_pref, melhor_pref, mais_barata


# ---------------------------------------------------------------- histórico
def arq_hist(v):
    return f"data/{v['id']}.csv"


def ler_historico(v):
    if not os.path.exists(arq_hist(v)):
        return []
    with open(arq_hist(v), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def gravar(v, linha):
    os.makedirs("data", exist_ok=True)
    novo = not os.path.exists(arq_hist(v))
    with open(arq_hist(v), "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS)
        if novo:
            w.writeheader()
        w.writerow(linha)


# ---------------------------------------------------------------- análise
def tendencia(hist, atual, agora):
    """Regressão linear sobre o menor preço de cada dia (últimos 14 dias)."""
    por_dia = {}
    for row in hist:
        d = datetime.fromisoformat(row["timestamp"]).date()
        val = float(row["total"])
        por_dia[d] = min(val, por_dia.get(d, val))
    por_dia[agora.date()] = min(atual, por_dia.get(agora.date(), atual))
    dias = sorted(por_dia)[-14:]
    if len(dias) < 4:
        return "coletando dados", 0.0
    xs = [(d - dias[0]).days for d in dias]
    ys = [por_dia[d] for d in dias]
    inclinacao = statistics.linear_regression(xs, ys).slope
    rel = inclinacao / statistics.mean(ys)
    if rel > 0.005:
        return "subindo", inclinacao
    if rel < -0.005:
        return "caindo", inclinacao
    return "estável", inclinacao


def recomendar(atual, minimo_hist, rotulo, dias_p_voo, niveis):
    niveis = [n for n in niveis if n]
    google_baixo = bool(niveis) and all(n == "low" for n in niveis)
    perto_do_min = minimo_hist is None or atual <= minimo_hist * 1.03
    if dias_p_voo <= 21:
        return "COMPRAR - faltam menos de 3 semanas, tarifas tendem a subir"
    if perto_do_min and google_baixo:
        return "COMPRAR - menor patamar registrado e Google classifica como preço baixo"
    if perto_do_min and rotulo == "subindo":
        return "COMPRAR - perto do mínimo e começando a subir"
    if rotulo == "subindo" and dias_p_voo <= 60:
        return "CONSIDERAR COMPRAR - tendência de alta a menos de 2 meses da viagem"
    if rotulo == "caindo":
        return "AGUARDAR - preço em queda"
    return "AGUARDAR - monitorando"


def melhor_horario(hist):
    grupos = {}
    for r in hist:
        h = datetime.fromisoformat(r["timestamp"]).hour
        faixa = "madrugada" if h < 7 else "manhã" if h < 12 else "tarde" if h < 18 else "noite"
        grupos.setdefault(faixa, []).append(float(r["total"]))
    medias = {f: statistics.mean(v) for f, v in grupos.items() if len(v) >= 5}
    if len(medias) < 2:
        return []
    return ["Média por horário: " + ", ".join(f"{f} {brl(medias[f])}" for f in sorted(medias, key=medias.get))]


def brl(v):
    return f"R$ {v:,.0f}".replace(",", ".")


def fmt_data(iso):
    return date.fromisoformat(iso).strftime("%d/%m")


# ---------------------------------------------------------------- Telegram
def avisar(texto, silencioso=False):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    print(texto, "\n")
    if token and chat:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": texto, "disable_web_page_preview": True,
                  "disable_notification": silencioso},
            timeout=30,
        )


# ---------------------------------------------------------------- uma viagem
def checar(v, agora):
    dias_p_voo = (date.fromisoformat(v["ida"]) - agora.date()).days
    hist = ler_historico(v)
    madrugada = agora.hour < 7
    primeira_do_dia = not madrugada and not any(
        datetime.fromisoformat(r["timestamp"]).date() == agora.date()
        and datetime.fromisoformat(r["timestamp"]).hour >= 7
        for r in hist
    )

    ops_i, ins_i = buscar(v, v["origem"], v["destino"], v["ida"], v["janela_ida"])
    esc_i, pref_i, bar_i = escolher(v, ops_i)
    if v["volta"]:
        ops_v, ins_v = buscar(v, v["destino"], v["origem"], v["volta"], v["janela_volta"])
        esc_v, pref_v, bar_v = escolher(v, ops_v)
    else:
        ins_v, esc_v, pref_v, bar_v = {}, None, None, None

    if not esc_i or (v["volta"] and not esc_v):
        if primeira_do_dia:
            avisar(f"⚠️ {v['nome']}: nenhum voo encontrado nas janelas de horário hoje.")
        return

    def soma(a, b):
        return a["preco"] + (b["preco"] if b else 0)

    total = soma(esc_i, esc_v)
    total_pref = soma(pref_i, pref_v) if pref_i and (pref_v or not v["volta"]) else None
    total_barato = soma(bar_i, bar_v)

    anterior = float(hist[-1]["total"]) if hist else None
    minimo_hist = min((float(r["total"]) for r in hist), default=None)
    rotulo, incl = tendencia(hist, total, agora)
    niveis = [ins_i.get("price_level"), ins_v.get("price_level")]
    rec = recomendar(total, minimo_hist, rotulo, dias_p_voo, niveis)
    rec_anterior = hist[-1]["recomendacao"] if hist else ""

    gravar(v, {
        "timestamp": agora.isoformat(timespec="minutes"),
        "total": total,
        "ida_preco": esc_i["preco"], "ida_cia": esc_i["cia"],
        "ida_horario": esc_i["horario"], "ida_paradas": esc_i["paradas"],
        "volta_preco": esc_v["preco"] if esc_v else "", "volta_cia": esc_v["cia"] if esc_v else "",
        "volta_horario": esc_v["horario"] if esc_v else "", "volta_paradas": esc_v["paradas"] if esc_v else "",
        "total_preferida": total_pref or "", "total_mais_barato": total_barato,
        "nivel_google_ida": niveis[0] or "", "nivel_google_volta": niveis[1] or "",
        "recomendacao": rec,
    })

    motivos = []
    if anterior is not None:
        queda = anterior - total
        if queda >= v["alerta_queda_reais"] or queda >= anterior * v["alerta_queda_pct"]:
            motivos.append(f"📉 Queda de {brl(queda)} desde a última checagem ({brl(anterior)})")
    if minimo_hist is not None and total < minimo_hist:
        motivos.append(f"🏆 Novo menor preço registrado (antes: {brl(minimo_hist)})")
    if rec.startswith("COMPRAR") and not rec_anterior.startswith("COMPRAR"):
        motivos.append("🛒 Sinal de compra")
    if not hist:
        motivos.append("✅ Monitor iniciado")
    if not motivos and not primeira_do_dia:
        print(f"{v['nome']}: sem novidades.")
        return

    def parada(n):
        return "direto" if n == 0 else f"{n} parada(s)"

    def trecho(rotulo_t, data, e):
        return f"{rotulo_t} {fmt_data(data)}: {e['cia']} {e['horario']} ({parada(e['paradas'])}) - {brl(e['preco'])}"

    faixa = ins_i.get("typical_price_range")
    linhas = [
        f"✈️ {v['nome']}",
        *(motivos or ["☀️ Resumo diário"]),
        "",
        f"{v['origem']} → {v['destino']}  |  total {brl(total)}" + ("" if v["volta"] else " (só ida)"),
        trecho("Ida", v["ida"], esc_i),
    ]
    if esc_v:
        linhas.append(trecho("Volta", v["volta"], esc_v))
    if total_pref and total_pref != total:
        linhas.append(f"Só {v['companhia_preferida']}: {brl(total_pref)}")
    if total_barato < total:
        linhas.append(f"Mais barato de qualquer cia: {brl(total_barato)}")
    linhas += [
        "",
        f"Tendência (14 dias): {rotulo}" + (f" ({brl(incl)}/dia)" if incl else ""),
        f"Menor já visto: {brl(min(total, minimo_hist) if minimo_hist else total)}",
        f"Google: ida '{niveis[0] or '?'}'" + (f", volta '{niveis[1] or '?'}'" if v["volta"] else "")
        + (f" | faixa típica ida {brl(faixa[0])}-{brl(faixa[1])}" if faixa else ""),
        f"Faltam {dias_p_voo} dias",
        *melhor_horario(hist),
        "",
        f"👉 {rec}",
        link_google(v),
    ]
    avisar("\n".join(linhas), silencioso=madrugada)


def migrar_historico_antigo(viagens):
    """Aproveita o data/historico.csv da versão anterior (uma viagem só)."""
    antigo = "data/historico.csv"
    if not os.path.exists(antigo) or not viagens or os.path.exists(arq_hist(viagens[0])):
        return
    with open(antigo, encoding="utf-8") as f:
        conteudo = f.read().replace("total_latam", "total_preferida", 1)
    with open(arq_hist(viagens[0]), "w", encoding="utf-8") as f:
        f.write(conteudo)
    os.remove(antigo)
    print(f"Histórico antigo movido para {arq_hist(viagens[0])}")


# ---------------------------------------------------------------- main
def main():
    agora = datetime.now(BRT)
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    slot = slot_atual(agora)
    viagens = carregar_viagens()

    ativas = [v for v in viagens if v["ativa"] and date.fromisoformat(v["ida"]) >= agora.date()]
    uso = sum(buscas_por_mes(v) for v in ativas)
    print(f"Checagem '{slot}'{' (manual)' if manual else ''} | uso estimado da cota: {uso}/{COTA_MENSAL} buscas/mês")
    if uso > COTA_MENSAL:
        print("ATENÇÃO: a soma das viagens passa da cota gratuita; reduza 'checagens' em alguma viagem.")

    migrar_historico_antigo(viagens)

    so_esta = os.environ.get("VIAGEM", "").strip()  # checagem manual de uma viagem só
    for v in ativas:
        if manual and so_esta and v["id"] != so_esta:
            continue
        if not manual and slot not in v["checagens"]:
            continue
        try:
            checar(v, agora)
        except Exception as e:  # uma viagem com erro não derruba as outras
            print(f"Erro em {v['nome']}: {e}")


if __name__ == "__main__":
    main()
