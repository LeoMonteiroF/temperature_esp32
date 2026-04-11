import os
import datetime
import uvicorn
import pytz
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List

app = FastAPI()

# --- VARIÁVEIS DE AMBIENTE (Configuráveis no Render) ---
# O os.getenv busca o valor lá no painel do Render. Se não achar, usa o segundo texto como padrão.
NOME_USUARIO = os.getenv("NOME_USUARIO", "Leo Monteiro")
TITULO_PAINEL = os.getenv("TITULO_PAINEL", "Controle de Fermentacao")
LIMITE_HISTORICO = int(os.getenv("LIMITE_HISTORICO", "100"))

# Memória temporária para logs (não persiste se o servidor reiniciar)
logs_armazenados: List[str] = []

# Memória focada na Alexa (guarda sempre a última leitura limpa)
ultima_leitura = {
    "temperatura": None,
    "horario": None,
    "horario_fala": None
}

class TemperatureData(BaseModel):
    temperatura: float
    horario: str

class HumidityData(BaseModel):
    umidade: float
    horario: str

class BootData(BaseModel):
    status: str
    horario: str

class LogData(BaseModel):
    log: str

def registrar_log(mensagem: str):
    """Exibe no console e guarda na memória para o navegador."""
    print(mensagem)
    logs_armazenados.append(mensagem)
    if len(logs_armazenados) > LIMITE_HISTORICO:
        logs_armazenados.pop(0)

@app.post('/boot')
async def rota_boot(data: BootData):
    msg = f"[{data.horario}] >>> SISTEMA REINICIADO: {data.status.upper()}"
    registrar_log(msg)
    return {"status": "ok"}

def obter_horario_brasil_extenso():
    # Define o fuso horário de Brasília/São Paulo
    fuso_br = pytz.timezone('America/Sao_Paulo')
    agora = datetime.datetime.now(fuso_br)
    
    # Dicionário para traduzir o mês na mão (mais seguro que locale)
    meses = {
        1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
        5: "maio", 6: "junho", 7: "julho", 8: "agosto",
        9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro"
    }
    
    dia = agora.day
    mes = meses[agora.month]
    hora = agora.hour
    minuto = agora.minute
    
    # Formata a string exatamente como a Alexa deve falar
    return f"{dia} de {mes} às {hora} horas e {minuto} minutos"

@app.post('/temperatura')
async def rota_temperatura(data: TemperatureData):
    # Atualiza o cofre da Alexa com o horário corrigido
    ultima_leitura["temperatura"] = data.temperatura
    ultima_leitura["horario_fala"] = obter_horario_brasil_extenso()
    
    # Para o log visual, usamos apenas o relógio
    fuso_br = pytz.timezone('America/Sao_Paulo')
    ultima_leitura["horario"] = datetime.datetime.now(fuso_br).strftime("%H:%M:%S")
    
    msg = f"[{ultima_leitura['horario']}] Temperatura: {data.temperatura}°C"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/temperatura_dht')
async def rota_temperatura_dht(data: TemperatureData):
    msg = f"[{data.horario}] [ESP32] Temperatura DHT22: {data.temperatura}°C"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/umidade_dht')
async def rota_umidade_dht(data: HumidityData):
    msg = f"[{data.horario}] [ESP32] Umidade DHT22: {data.umidade}%"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/log')
async def rota_log(data: LogData):
    agora = datetime.datetime.now().strftime("[%H:%M:%S]")
    msg = f"[{agora}] [ESP32] {data.log}"
    registrar_log(msg)
    return {"status": "log_registrado"}

@app.post('/alexa')
async def rota_alexa(request: Request):
    req_data = await request.json()
    req_type = req_data.get("request", {}).get("type")
    intent_name = req_data.get("request", {}).get("intent", {}).get("name")
    
    # IMPORTANTE: Garante que pegamos os atributos da sessão corretamente
    session_attrs = req_data.get("session", {}).get("attributes", {})
    if session_attrs is None:
        session_attrs = {}

    # 1. TRATAR O "SIM" (Dando prioridade ao contexto)
    if intent_name == "AMAZON.YesIntent" and session_attrs.get("esperando_horario"):
        horario_extenso = ultima_leitura.get("horario_fala", "momento desconhecido")
        fala = f"O último registro foi feito no dia {horario_extenso}."
        
        return {
            "version": "1.0",
            "response": {
                "outputSpeech": {"type": "PlainText", "text": fala},
                "shouldEndSession": True
            }
        }

    # 2. TRATAR O "NÃO"
    elif intent_name == "AMAZON.NoIntent":
        return {
            "version": "1.0",
            "response": {
                "outputSpeech": {"type": "PlainText", "text": "Ok, estarei monitorando. Até mais!"},
                "shouldEndSession": True
            }
        }

    # 3. PERGUNTA DE TEMPERATURA OU ABERTURA
    elif req_type == "LaunchRequest" or intent_name == "PerguntaTemperaturaIntent":
        temp = ultima_leitura.get("temperatura")
        
        if temp is None:
            fala = "Ainda não recebi dados do sensor."
            encerra = True
            attrs = {}
        else:
            fala = f"A medição atual é de {temp} graus. Gostaria de saber a última atualização?"
            encerra = False
            # O "bilhete" que a Alexa deve devolver no próximo turno
            attrs = {"esperando_horario": True}

        return {
            "version": "1.0",
            "sessionAttributes": attrs,
            "response": {
                "outputSpeech": {"type": "PlainText", "text": fala},
                "shouldEndSession": encerra
            }
        }

    # Fallback para qualquer outra coisa
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": "Não entendi o pedido."},
            "shouldEndSession": True
        }
    }

@app.get('/', response_class=HTMLResponse)
async def pagina_principal():
    html = f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>{TITULO_PAINEL}</title>
            <meta http-equiv="refresh" content="5">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{ font-family: sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 20px; }}
                h1 {{ color: #bb86fc; border-bottom: 1px solid #333; padding-bottom: 10px; }}
                .user-info {{ color: #03dac6; margin-bottom: 20px; font-weight: bold; }}
                .terminal {{ background: #000; border: 1px solid #333; border-radius: 5px; padding: 15px; height: 70vh; overflow-y: auto; font-family: monospace; }}
                .line {{ border-bottom: 1px solid #1a1a1a; padding: 5px 0; color: #00ff41; }}
            </style>
        </head>
        <body>
            <h1>{TITULO_PAINEL}</h1>
            <div class="user-info">Operador: {NOME_USUARIO}</div>
            <div class="terminal">
    """
    if not logs_armazenados:
        html += "<div class='line'>Aguardando dados do ESP32...</div>"
    else:
        for log in reversed(logs_armazenados):
            html += f"<div class='line'>{log}</div>"
            
    html += "</div></body></html>"
    return html

if __name__ == "__main__":
    # O Render define a porta automaticamente na variável PORT
    porta = int(os.getenv("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=porta)