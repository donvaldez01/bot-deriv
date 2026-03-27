import asyncio
import json
import logging
import os
import urllib.request

import websockets
import anthropic

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuração via variáveis de ambiente ────────────────────────────────────
DERIV_TOKEN    = os.environ["DERIV_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT"]

DERIV_WS_URL   = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL         = os.environ.get("SYMBOL", "R_10")
STAKE          = float(os.environ.get("STAKE", "0.35"))
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "5.00"))
STOP_SEQ       = int(os.environ.get("STOP_SEQ", "3"))

TIPOS_VALIDOS  = ("CALL", "PUT", "NOTOUCH", "DIGITEVEN", "DIGITODD", "DIGITOVER", "DIGITUNDER")

state = {
    "ticks": [],
    "balance": 0.0,
    "daily_loss": 0.0,
    "losses_seguidos": 0,
    "trades_hoje": 0,
}

claude  = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
_req_id = 0


def next_req_id() -> int:
    global _req_id
    _req_id += 1
    return _req_id


# ── Telegram ──────────────────────────────────────────────────────────────────
async def telegram(msg: str) -> None:
    url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TELEGRAM_CHAT, "text": msg}).encode()
    req  = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.warning(f"Telegram erro: {e}")


# ── Receptor WS com req_id ────────────────────────────────────────────────────
# Cada mensagem enviada ao Deriv leva um req_id único.
# O receptor usa esse id para entregar a resposta ao Future certo,
# evitando que ticks, respostas de buy e resultados de contrato se misturem.
async def receptor(ws, fila_ticks: asyncio.Queue, pendentes: dict):
    async for raw in ws:
        data   = json.loads(raw)
        req_id = data.get("req_id")
        if req_id and req_id in pendentes:
            fut = pendentes.pop(req_id)
            if not fut.done():
                fut.set_result(data)
        elif "tick" in data:
            await fila_ticks.put(data["tick"])


async def ws_request(ws, payload: dict, pendentes: dict, timeout: int = 15) -> dict:
    """Envia um payload com req_id único e aguarda a resposta correspondente."""
    rid            = next_req_id()
    payload["req_id"] = rid
    loop           = asyncio.get_event_loop()
    fut            = loop.create_future()
    pendentes[rid] = fut
    await ws.send(json.dumps(payload))
    return await asyncio.wait_for(fut, timeout=timeout)


# ── Features ──────────────────────────────────────────────────────────────────
def calcular_features() -> dict | None:
    ticks = state["ticks"]
    if len(ticks) < 20:
        return None

    precos   = [t["quote"] for t in ticks[-50:]]
    retornos = [(precos[i] - precos[i-1]) / precos[i-1] * 100 for i in range(1, len(precos))]
    media    = sum(retornos) / len(retornos)
    vol      = (sum((r - media)**2 for r in retornos) / len(retornos)) ** 0.5
    recentes   = sum(precos[-10:]) / 10
    anteriores = sum(precos[-20:-10]) / 10
    tendencia  = (recentes - anteriores) / anteriores * 100
    direcoes   = "".join("+" if r > 0 else "-" for r in retornos[-5:])

    digitos = [int(str(round(p, 2)).replace(".", "")[-1]) for p in precos[-20:]]
    pares   = sum(1 for d in digitos if d % 2 == 0)
    sobre_5 = sum(1 for d in digitos if d > 5)
    sob_5   = sum(1 for d in digitos if d < 5)

    return {
        "volatilidade":    round(vol, 4),
        "tendencia_pct":   round(tendencia, 4),
        "preco_atual":     precos[-1],
        "direcoes":        direcoes,
        "digitos_pares":   pares,
        "digitos_impares": 20 - pares,
        "digitos_sobre_5": sobre_5,
        "digitos_sob_5":   sob_5,
    }


# ── Claude ────────────────────────────────────────────────────────────────────
async def perguntar_claude(f: dict) -> dict:
    prompt = f"""Você é o gerenciador de risco de um bot de trading — conta DEMO na Deriv.

MERCADO AGORA:
- Índice: Volatility 10 (sintético)
- Volatilidade: {f['volatilidade']}%
- Tendência (20 ticks): {f['tendencia_pct']:+.4f}%
- Últimas 5 direções: {f['direcoes']} (+ sobe, - cai)
- Preço atual: {f['preco_atual']}

ESTATÍSTICAS DOS ÚLTIMOS 20 DÍGITOS FINAIS:
- Pares: {f['digitos_pares']}/20
- Ímpares: {f['digitos_impares']}/20
- Acima de 5: {f['digitos_sobre_5']}/20
- Abaixo de 5: {f['digitos_sob_5']}/20

ESTADO:
- Saldo: ${state['balance']:.2f}
- Loss hoje: ${state['daily_loss']:.2f}
- Losses seguidos: {state['losses_seguidos']}
- Trades hoje: {state['trades_hoje']}

REGRAS:
1. losses_seguidos >= {STOP_SEQ} → acao = PAUSAR
2. daily_loss >= {MAX_DAILY_LOSS} → acao = PARAR_DIA
3. stake SEMPRE exatamente 1% do saldo = use round(balance * 0.01, 2)

TIPOS DE CONTRATO DISPONÍVEIS E QUANDO USAR:
- CALL / PUT → volatilidade média, tendência clara (tendencia_pct > 0.002)
- NOTOUCH → volatilidade MUITO baixa (< 0.003), mercado calmo
- DIGITEVEN → pares >= 12 nos últimos 20 (desvio favorável a pares)
- DIGITODD → impares >= 12 nos últimos 20 (desvio favorável a ímpares)
- DIGITOVER → sobre_5 >= 12 nos últimos 20 (desvio favorável a altos)
- DIGITUNDER → sob_5 >= 12 nos últimos 20 (desvio favorável a baixos)
- AGUARDAR → nenhuma condição favorável

IMPORTANTE:
- Para NOTOUCH sugerir barrier como percentual do preço atual
- Para CALL/PUT duração: 1 minuto
- Para DIGIT duração: 5 ticks
- Para NOTOUCH duração: 5 minutos

Responda SOMENTE com JSON puro, sem markdown, sem backticks:
{{"acao":"DIGITEVEN","duracao":5,"unidade":"t","stake":0.50,"barrier":null,"confianca":0.7,"raciocinio":"motivo curto"}}

acao pode ser: CALL, PUT, NOTOUCH, DIGITEVEN, DIGITODD, DIGITOVER, DIGITUNDER, AGUARDAR, PAUSAR, PARAR_DIA"""

    try:
        r     = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = r.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(texto)
    except Exception as e:
        log.error(f"Claude erro: {e}")
        return {"acao": "AGUARDAR", "raciocinio": str(e)}


# ── Parâmetros do contrato ────────────────────────────────────────────────────
def montar_parametros(acao: str, stake: float, preco_atual: float) -> dict | None:
    base = {"symbol": SYMBOL, "basis": "stake", "amount": stake, "currency": "USD"}

    if acao in ("CALL", "PUT"):
        return {**base, "contract_type": acao, "duration": 1, "duration_unit": "m"}

    if acao == "NOTOUCH":
        barrier = round(preco_atual * 1.005, 2)
        return {**base, "contract_type": "NOTOUCH", "duration": 5, "duration_unit": "m", "barrier": str(barrier)}

    digit_map = {
        "DIGITEVEN":  {"contract_type": "DIGITEVEN"},
        "DIGITODD":   {"contract_type": "DIGITODD"},
        "DIGITOVER":  {"contract_type": "DIGITOVER",  "barrier": "5"},
        "DIGITUNDER": {"contract_type": "DIGITUNDER", "barrier": "5"},
    }
    if acao in digit_map:
        return {**base, **digit_map[acao], "duration": 5, "duration_unit": "t"}

    log.warning(f"montar_parametros: ação desconhecida '{acao}'")
    return None


def tempo_espera(acao: str) -> int:
    if acao in ("CALL", "PUT"):
        return 75
    if acao == "NOTOUCH":
        return 315
    return 15


# ── Loop principal ────────────────────────────────────────────────────────────
async def conectar() -> None:
    tentativa = 0
    while True:
        try:
            tentativa += 1
            log.info(f"Conectando... (tentativa {tentativa})")
            async with websockets.connect(DERIV_WS_URL, ping_interval=30, ping_timeout=10) as ws:
                tentativa = 0

                # Autenticação direta (antes do receptor iniciar)
                await ws.send(json.dumps({"authorize": DERIV_TOKEN, "req_id": 0}))
                auth = json.loads(await ws.recv())
                if "error" in auth:
                    log.error(f"Erro de autenticação: {auth['error']['message']}")
                    return

                state["balance"] = float(auth["authorize"]["balance"])
                log.info(f"Autenticado! Saldo demo: ${state['balance']:.2f}")
                await telegram(f"Bot conectado — saldo demo: ${state['balance']:.2f}")

                # Subscrição de ticks
                await ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
                log.info("Recebendo ticks... aguarde 30 para primeira análise.")

                fila_ticks = asyncio.Queue()
                pendentes  = {}  # req_id → Future

                task_receptor = asyncio.create_task(receptor(ws, fila_ticks, pendentes))

                ciclo = 0
                try:
                    while True:
                        tick = await fila_ticks.get()
                        state["ticks"].append({"quote": float(tick["quote"]), "epoch": tick["epoch"]})
                        if len(state["ticks"]) > 100:
                            state["ticks"] = state["ticks"][-100:]

                        ciclo += 1
                        print(f"\r  tick #{ciclo} — preço: {tick['quote']}", end="", flush=True)

                        if ciclo % 30 != 0:
                            continue

                        print()
                        features = calcular_features()
                        if not features:
                            continue

                        log.info("Consultando Claude...")
                        decisao = await perguntar_claude(features)
                        acao    = decisao.get("acao", "AGUARDAR")
                        razao   = decisao.get("raciocinio", "")
                        log.info(f"Decisão: {acao} — {razao}")

                        if acao == "PARAR_DIA":
                            await telegram(f"Fim do dia. Loss total: ${state['daily_loss']:.2f}")
                            task_receptor.cancel()
                            return

                        if acao == "PAUSAR":
                            await telegram(f"Pausando 5 min após {state['losses_seguidos']} losses.")
                            await asyncio.sleep(300)
                            state["losses_seguidos"] = 0
                            continue

                        if acao == "AGUARDAR" or acao not in TIPOS_VALIDOS:
                            log.info(f"Aguardando... ({razao})")
                            continue

                        # Stake limitado a 1% do saldo
                        stake  = round(state["balance"] * 0.01, 2)
                        params = montar_parametros(acao, stake, features["preco_atual"])
                        if params is None:
                            continue

                        # ── Envia ordem e aguarda confirmação via req_id ──────
                        try:
                            resp_buy = await ws_request(
                                ws, {"buy": 1, "price": stake, "parameters": params},
                                pendentes, timeout=10
                            )
                        except asyncio.TimeoutError:
                            log.error("Timeout aguardando confirmação da ordem.")
                            await telegram(f"Erro: sem confirmação da ordem {acao} em 10s.")
                            continue

                        if "error" in resp_buy:
                            log.error(f"Erro na ordem: {resp_buy['error']['message']}")
                            await telegram(f"Erro na ordem ({acao}): {resp_buy['error']['message']}")
                            continue

                        contract_id  = resp_buy["buy"]["contract_id"]
                        preco_compra = resp_buy["buy"]["buy_price"]
                        espera       = tempo_espera(acao)
                        log.info(f"Ordem aberta — contrato {contract_id} — ${preco_compra:.2f} — aguardando {espera}s...")

                        await asyncio.sleep(espera)

                        # ── Consulta resultado via req_id ─────────────────────
                        lucro  = 0.0
                        status = "desconhecido"
                        for tentativa_res in range(5):
                            try:
                                resp = await ws_request(
                                    ws,
                                    {"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 0},
                                    pendentes, timeout=10
                                )
                            except asyncio.TimeoutError:
                                log.warning(f"Timeout consultando contrato, tentativa {tentativa_res + 1}/5")
                                await asyncio.sleep(3)
                                continue

                            contrato = resp.get("proposal_open_contract", {})
                            status   = contrato.get("status", "desconhecido")
                            if status in ("won", "lost", "sold"):
                                lucro = float(contrato.get("profit", 0))
                                break
                            log.info(f"Resultado pendente, tentativa {tentativa_res + 1}/5...")
                            await asyncio.sleep(3)

                        # ── Atualiza estado ───────────────────────────────────
                        if lucro < 0:
                            state["losses_seguidos"] += 1
                            state["daily_loss"] += abs(lucro)
                        else:
                            state["losses_seguidos"] = 0
                        state["trades_hoje"] += 1

                        emoji = "✅ GANHOU" if lucro >= 0 else "❌ PERDEU"
                        msg = (
                            f"{emoji} | {acao} | {status}\n"
                            f"Lucro: ${lucro:+.2f}\n"
                            f"Razão: {razao}\n"
                            f"Trades hoje: {state['trades_hoje']}\n"
                            f"Losses seguidos: {state['losses_seguidos']}"
                        )
                        log.info(msg)
                        await telegram(msg)

                finally:
                    task_receptor.cancel()

        except Exception as e:
            wait = min(5 * tentativa, 60)
            log.error(f"Conexão perdida: {e}. Reconectando em {wait}s...")
            await asyncio.sleep(wait)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(conectar())
