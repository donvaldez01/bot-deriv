import asyncio
import json
import urllib.request
from datetime import datetime
import websockets
import anthropic

import os

DERIV_TOKEN    = os.environ.get("DERIV_TOKEN")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT")

DERIV_WS_URL   = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL         = "R_10"
STAKE          = 0.50
MAX_DAILY_LOSS = 5.00
STOP_SEQ       = 3

state = {
    "ticks": [],
    "balance": 0,
    "daily_loss": 0,
    "losses_seguidos": 0,
    "trades_hoje": 0,
}

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

async def telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[Telegram erro] {e}")

def calcular_features():
    ticks = state["ticks"]
    if len(ticks) < 20:
        return None
    precos = [t["quote"] for t in ticks[-50:]]
    retornos = [(precos[i] - precos[i-1]) / precos[i-1] * 100 for i in range(1, len(precos))]
    media = sum(retornos) / len(retornos)
    volatilidade = (sum((r - media)**2 for r in retornos) / len(retornos)) ** 0.5
    recentes   = sum(precos[-10:]) / 10
    anteriores = sum(precos[-20:-10]) / 10
    tendencia  = (recentes - anteriores) / anteriores * 100
    direcoes   = "".join("+" if r > 0 else "-" for r in retornos[-5:])
    return {
        "volatilidade":  round(volatilidade, 4),
        "tendencia_pct": round(tendencia, 4),
        "preco_atual":   precos[-1],
        "direcoes":      direcoes,
    }

async def perguntar_claude(f):
    prompt = f"""Você é o gerenciador de risco de um bot de trading — conta DEMO.

MERCADO AGORA:
- Índice: Volatility 10 (sintético, sem notícias)
- Volatilidade: {f['volatilidade']}%
- Tendência (20 ticks): {f['tendencia_pct']:+.4f}%
- Últimas 5 direções: {f['direcoes']} (+ sobe, - cai)
- Preço: {f['preco_atual']}

ESTADO:
- Saldo: ${state['balance']:.2f}
- Loss hoje: ${state['daily_loss']:.2f}
- Losses seguidos: {state['losses_seguidos']}
- Trades hoje: {state['trades_hoje']}

REGRAS:
1. losses_seguidos >= {STOP_SEQ} → acao = PAUSAR
2. daily_loss >= {MAX_DAILY_LOSS} → acao = PARAR_DIA
3. volatilidade > 0.08 → acao = AGUARDAR
4. stake máximo: 5% do saldo

Responda SOMENTE com JSON puro, sem texto antes ou depois, sem markdown, sem backticks:
{{"acao":"CALL","stake":0.50,"confianca":0.7,"raciocinio":"motivo curto"}}

acao pode ser: CALL, PUT, AGUARDAR, PAUSAR, PARAR_DIA"""

    try:
        r = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        texto = r.content[0].text.strip()
        texto = texto.replace("```json", "").replace("```", "").strip()
        return json.loads(texto)
    except Exception as e:
        print(f"[Claude erro] {e}")
        return {"acao": "AGUARDAR", "raciocinio": str(e)}

async def conectar():
    while True:
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Conectando...")
            async with websockets.connect(DERIV_WS_URL, ping_interval=30, ping_timeout=10) as ws:

                await ws.send(json.dumps({"authorize": DERIV_TOKEN}))
                auth = json.loads(await ws.recv())

                if "error" in auth:
                    print(f"Erro de autenticação: {auth['error']['message']}")
                    return

                state["balance"] = float(auth["authorize"]["balance"])
                print(f"Autenticado! Saldo demo: ${state['balance']:.2f}")
                await telegram(f"Bot conectado — saldo demo: ${state['balance']:.2f}")

                await ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
                print("Recebendo ticks... aguarde 30 para primeira análise.")

                ciclo = 0
                async for mensagem in ws:
                    data = json.loads(mensagem)
                    if "tick" not in data:
                        continue

                    tick = data["tick"]
                    state["ticks"].append({"quote": float(tick["quote"]), "epoch": tick["epoch"]})
                    if len(state["ticks"]) > 100:
                        state["ticks"] = state["ticks"][-100:]

                    ciclo += 1
                    print(f"  tick #{ciclo} — preço: {tick['quote']}", end="\r")

                    if ciclo % 30 != 0:
                        continue

                    features = calcular_features()
                    if not features:
                        continue

                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Consultando Claude...")
                    decisao = await perguntar_claude(features)
                    acao    = decisao.get("acao", "AGUARDAR")
                    razao   = decisao.get("raciocinio", "")
                    print(f"  Decisão: {acao} — {razao}")

                    if acao == "PARAR_DIA":
                        await telegram(f"Fim do dia. Loss total: ${state['daily_loss']:.2f}")
                        return

                    if acao == "PAUSAR":
                        await telegram(f"Pausando 5 min após {state['losses_seguidos']} losses.")
                        await asyncio.sleep(300)
                        state["losses_seguidos"] = 0
                        continue

                    if acao not in ("CALL", "PUT"):
                        print(f"  Aguardando... ({razao})")
                        continue

                    stake = min(decisao.get("stake", STAKE), round(state["balance"] * 0.05, 2))
                    await ws.send(json.dumps({
                        "buy": 1,
                        "price": stake,
                        "parameters": {
                            "contract_type": acao,
                            "symbol": SYMBOL,
                            "duration": 1,
                            "duration_unit": "m",
                            "basis": "stake",
                            "amount": stake,
                            "currency": "USD",
                        }
                    }))
                    resp_ordem = json.loads(await ws.recv())

                    if "error" in resp_ordem:
                        print(f"  Erro na ordem: {resp_ordem['error']['message']}")
                        await telegram(f"Erro na ordem: {resp_ordem['error']['message']}")
                        continue

                    contract_id  = resp_ordem["buy"]["contract_id"]
                    preco_compra = resp_ordem["buy"]["buy_price"]
                    print(f"  Ordem aberta — contrato {contract_id} — ${preco_compra:.2f}")
                    print(f"  Aguardando 1 minuto para resultado...")

                    # Espera o contrato de 1 minuto resolver
                    await asyncio.sleep(75)

                    # Tenta buscar resultado até 5 vezes
                    lucro = 0
                    status = "desconhecido"
                    for tentativa in range(5):
                        await ws.send(json.dumps({
                            "proposal_open_contract": 1,
                            "contract_id": contract_id,
                            "subscribe": 0
                        }))
                        resp_status = json.loads(await ws.recv())
                        contrato = resp_status.get("proposal_open_contract", {})
                        status = contrato.get("status", "desconhecido")
                        if status in ("won", "lost", "sold"):
                            lucro = float(contrato.get("profit", 0))
                            break
                        print(f"  Resultado ainda não disponível, tentativa {tentativa+1}/5...")
                        await asyncio.sleep(3)

                    if lucro < 0:
                        state["losses_seguidos"] += 1
                        state["daily_loss"] += abs(lucro)
                    else:
                        state["losses_seguidos"] = 0
                    state["trades_hoje"] += 1

                    emoji = "GANHOU" if lucro >= 0 else "PERDEU"
                    msg = (f"{emoji} | {acao} | {status}\n"
                           f"Lucro: ${lucro:+.2f}\n"
                           f"Razão: {razao}\n"
                           f"Trades hoje: {state['trades_hoje']}\n"
                           f"Losses seguidos: {state['losses_seguidos']}")
                    print(f"\n  {msg}")
                    await telegram(msg)

        except Exception as e:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Conexão perdida: {e}")
            print("Reconectando em 5 segundos...")
            await asyncio.sleep(5)

await conectar()
