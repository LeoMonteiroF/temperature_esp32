import os
import datetime
import uvicorn
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional

import alexa_router
import database
from database import FUSO_BR, utc_to_br

app = FastAPI()
templates = Jinja2Templates(directory="templates")

def agora_br():
    return datetime.datetime.now(FUSO_BR)

# --- ESTADO DA TOMADA (Proxy Local) ---
tomadaStatus: str = "off"
ultimaLeituraTimestamp: Optional[datetime.datetime] = None
timestampMudancaEstado: Optional[datetime.datetime] = None
LIMITE_INERCIA_TERMICA = 300  # 5 minutos para detectar dessincronização
TEMPO_PULSO_RESYNC = 30       # 30 segundos de pulso para forçar o Google Home

# --- CONFIGURAÇÕES DE CONTROLE ---
MODO_AQUECIMENTO = 0.0
MODO_RESFRIAMENTO = 1.0

DEFAULT_CONFIG = {
    "modo": MODO_AQUECIMENTO,
    "temp_corte_aquecimento": 13.0,
    "histerese_aquecimento": 2.0,
    "temp_corte_resfriamento": 18.0,
    "histerese_resfriamento": 2.0,
    "temp_max_overshoot": 14.0, # Segurança (usado mais em aquecimento)
    "derivada_critica": -15.0,  # Queda/Subida muito rápida
    "offset_piso": 0.5          # Ajuste antecipatório
}
CONFIG = DEFAULT_CONFIG.copy()

# Variáveis para cálculo da derivada
ultima_temp_derivada = None
ultimo_ts_derivada = None

# Inicializa banco e configurações
database.init_db(DEFAULT_CONFIG)
database.carregar_configuracoes(CONFIG)

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

class ConfigData(BaseModel):
    configs: dict

def registrar_log(mensagem: str):
    """Exibe no console e guarda na memória para o navegador."""
    print(mensagem)
    database.salvar_log_db(mensagem)
    logs_armazenados.append(mensagem)
    if len(logs_armazenados) > LIMITE_HISTORICO:
        logs_armazenados.pop(0)

# # async def trigger_resync_pulse():
# #     global tomadaStatus, em_pulso_resync
# #     if em_pulso_resync:
# #         return # Impede disparos simultâneos
# #
# #     em_pulso_resync = True
# #     registrar_log(">>> [RESYNC] Detectada dessincronização física. Iniciando Pulso de Ressincronização...")
# #     tomadaStatus = "on"
# #     registrar_log(">>> [RESYNC] Estado forçado para 'on' para rearmar gatilho do Google Home.")
# #
# #     await asyncio.sleep(TEMPO_PULSO_RESYNC)
# #
# #     tomadaStatus = "off"
# #     registrar_log(">>> [RESYNC] Pulso finalizado. Estado retornado para 'off'.")
# #     em_pulso_resync = False
# #
@app.post('/boot')
async def rota_boot(data: BootData, request: Request):
    client_host = request.client.host
    msg = f"[{data.horario}] >>> SISTEMA REINICIADO: {data.status.upper()} (IP: {client_host})"
    registrar_log(msg)
    return {"status": "ok"}

def obter_horario_brasil_extenso():
    # Usa o fuso horário de Brasília importado do database
    agora = datetime.datetime.now(FUSO_BR)
    
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
def rota_temperatura(data: TemperatureData, background_tasks: BackgroundTasks):
    global ultimaLeituraTimestamp, tomadaStatus, timestampMudancaEstado
    global ultima_temp_derivada, ultimo_ts_derivada
    
    agora = datetime.datetime.utcnow()
    ultimaLeituraTimestamp = agora
    
    # --- CÁLCULO DA DERIVADA ---
    derivada = 0.0
    if ultima_temp_derivada is not None and ultimo_ts_derivada is not None:
        delta_temp = data.temperatura - ultima_temp_derivada
        delta_time_min = (agora - ultimo_ts_derivada).total_seconds() / 60.0
        if delta_time_min > 0:
            derivada = delta_temp / delta_time_min
            
    ultima_temp_derivada = data.temperatura
    ultimo_ts_derivada = agora

    # --- LÓGICA DE CONTROLE (Dual Mode) ---
    modo = CONFIG.get("modo", MODO_AQUECIMENTO)
    cfg_derivada_critica = CONFIG.get("derivada_critica", DEFAULT_CONFIG["derivada_critica"])
    cfg_offset_piso = CONFIG.get("offset_piso", DEFAULT_CONFIG["offset_piso"])
    
    usando_antecipacao = False
    
    if modo == MODO_AQUECIMENTO:
        corte = CONFIG.get("temp_corte_aquecimento", DEFAULT_CONFIG["temp_corte_aquecimento"])
        histerese = CONFIG.get("histerese_aquecimento", DEFAULT_CONFIG["histerese_aquecimento"])
        
        trigger_on = corte - histerese
        trigger_off = corte
        
        # Antecipação no aquecimento: Se cai muito rápido, sobe o piso para ligar antes
        if tomadaStatus == "off" and derivada <= cfg_derivada_critica:
            trigger_on += cfg_offset_piso
            usando_antecipacao = True

        if data.temperatura <= trigger_on:
            novo_status = "on"
        elif data.temperatura >= trigger_off:
            novo_status = "off"
        else:
            novo_status = tomadaStatus

    else: # MODO_RESFRIAMENTO
        corte = CONFIG.get("temp_corte_resfriamento", DEFAULT_CONFIG["temp_corte_resfriamento"])
        histerese = CONFIG.get("histerese_resfriamento", DEFAULT_CONFIG["histerese_resfriamento"])
        
        trigger_on = corte + histerese
        trigger_off = corte
        
        # Antecipação no resfriamento: Se sobe muito rápido, baixa o teto para ligar antes
        # Nota: derivada positiva significa temperatura subindo
        if tomadaStatus == "off" and derivada >= abs(cfg_derivada_critica):
            trigger_on -= cfg_offset_piso
            usando_antecipacao = True

        if data.temperatura >= trigger_on:
            novo_status = "on"
        elif data.temperatura <= trigger_off:
            novo_status = "off"
        else:
            novo_status = tomadaStatus
        
    if novo_status != tomadaStatus:
        tomadaStatus = novo_status
        timestampMudancaEstado = agora
        tipo_acao = "Aquecimento" if modo == MODO_AQUECIMENTO else "Resfriamento"
        if tomadaStatus == "on" and usando_antecipacao:
            registrar_log(f"Server Hysteresis ({tipo_acao}): Set to 'on' (Anticipatory Trigger! Deriv: {derivada:.2f}°C/min)")
        else:
            registrar_log(f"Server Hysteresis ({tipo_acao}): Set to '{tomadaStatus}' (Temp: {data.temperatura}°C)")

    # Atualiza o cofre da Alexa com o horário corrigido
    ultima_leitura["temperatura"] = data.temperatura
    ultima_leitura["horario_fala"] = obter_horario_brasil_extenso()

    # Para o log visual, usamos apenas o relógio
    ultima_leitura["horario"] = agora_br().strftime("%H:%M:%S")

    database.salvar_leitura("temperatura", data.temperatura, "DS18B20")
    msg = f"[{ultima_leitura['horario']}] Temperatura: {data.temperatura}°C | Tomada Alvo: {tomadaStatus}"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/temperatura_dht')
def rota_temperatura_dht(data: TemperatureData):
    global ultimaLeituraTimestamp
    agora = datetime.datetime.utcnow()
    ultimaLeituraTimestamp = agora
    
    database.salvar_leitura("temperatura", data.temperatura, "DHT22")
    msg = f"[{data.horario}] [ESP32] Temperatura DHT22: {data.temperatura}°C"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/umidade_dht')
def rota_umidade_dht(data: HumidityData):
    global ultimaLeituraTimestamp
    ultimaLeituraTimestamp = datetime.datetime.utcnow()
    
    database.salvar_leitura("umidade", data.umidade, "DHT22")
    msg = f"[{data.horario}] [ESP32] Umidade DHT22: {data.umidade}%"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/log')
async def rota_log(data: LogData):
    agora_str = agora_br().strftime("[%H:%M:%S]")
    msg = f"[{agora_str}] [ESP32] {data.log}"
    registrar_log(msg)
    return {"status": "log_registrado"}

@app.post('/alexa_ai')
async def rota_alexa_ia(request: Request, background_tasks: BackgroundTasks):
    """ Rota EXCLUSIVA para a nova Skill da IA no Amazon Developer Console """
    req_data = await request.json()
    return await alexa_router.processar_alexa_ia(req_data, background_tasks)

@app.post('/alexa')
async def rota_alexa(request: Request, background_tasks: BackgroundTasks):
    req_data = await request.json()
    req_type = req_data.get("request", {}).get("type")
    intent_name = req_data.get("request", {}).get("intent", {}).get("name")
    session_id = req_data.get("session", {}).get("sessionId")
    
    # IMPORTANTE: Garante que pegamos os atributos da sessão corretamente para as rotas normais
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
                "outputSpeech": {"type": "PlainText", "text": "Ok"},
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

    # 4. PERGUNTA DE CONFIGURAÇÃO
    elif intent_name == "PerguntaConfiguracaoIntent":
        modo = CONFIG.get("modo", MODO_AQUECIMENTO)
        
        if modo == MODO_AQUECIMENTO:
            corte = str(CONFIG.get("temp_corte_aquecimento", 13.0)).replace('.', ',')
            histerese = str(CONFIG.get("histerese_aquecimento", 2.0)).replace('.', ',')
            tipo = "aquecimento"
            detalhe_gatilho = f"liga em {str(float(corte.replace(',','.')) - float(histerese.replace(',','.'))).replace('.', ',')} graus"
        else:
            corte = str(CONFIG.get("temp_corte_resfriamento", 18.0)).replace('.', ',')
            histerese = str(CONFIG.get("histerese_resfriamento", 2.0)).replace('.', ',')
            tipo = "resfriamento"
            detalhe_gatilho = f"liga em {str(float(corte.replace(',','.')) + float(histerese.replace(',','.'))).replace('.', ',')} graus"

        temp_max = str(CONFIG.get("temp_max_overshoot", 14.0)).replace('.', ',')
        derivada = CONFIG.get("derivada_critica", -15.0)
        derivada_str = str(abs(derivada)).replace('.', ',')
        offset = str(CONFIG.get("offset_piso", 0.5)).replace('.', ',')
        
        fala = (
            f"O sistema está em modo de {tipo}. A temperatura alvo é {corte} graus, com histerese de {histerese} graus. "
            f"Ou seja, ele {detalhe_gatilho} e desliga ao atingir o alvo. "
            f"A proteção antecipatória está ativa para variações de {derivada_str} graus por minuto."
        )
        
        return {
            "version": "1.0",
            "response": {
                "outputSpeech": {"type": "PlainText", "text": fala},
                "shouldEndSession": True
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

@app.api_route('/', response_class=HTMLResponse, methods=["GET", "HEAD"])
async def pagina_principal(request: Request, periodo: str = "1", resolucao: Optional[int] = None):
    if resolucao is None:
        if periodo in ["1", "3"] or periodo.endswith('h'):
            resolucao = 10
        else:
            resolucao = 100
            
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "TITULO_PAINEL": TITULO_PAINEL,
            "NOME_USUARIO": NOME_USUARIO,
            "periodo": periodo,
            "resolucao": resolucao
        }
    )

@app.get('/api/config')
def get_config():
    return CONFIG

@app.post('/api/config')
def update_config(data: ConfigData):
    database.atualizar_config_db(data.configs, CONFIG)
    registrar_log("Configurações atualizadas via painel.")
    return {"status": "ok"}

@app.post('/api/reload_config')
async def reload_config():
    database.carregar_configuracoes(CONFIG)
    registrar_log("Configurações recarregadas do banco de dados pelo Dashboard.")
    return {"status": "ok", "config": CONFIG}

@app.get('/api/dados')
def api_dados(periodo: str = "1", resolucao: Optional[int] = None):
    if resolucao is None:
        if periodo.endswith('h') or (periodo.isdigit() and int(periodo) <= 3):
            resolucao = 10
        else:
            resolucao = 100
    
    return database.buscar_dados_grafico(periodo, resolucao)

@app.get('/api/logs')
async def api_logs():
    return database.buscar_logs(LIMITE_HISTORICO)

@app.api_route('/check-status', methods=["GET", "HEAD"])
async def check_status():
    global ultimaLeituraTimestamp
    global tomadaStatus

    idade_leitura_segundos = -1
    if ultimaLeituraTimestamp:
        idade_leitura_segundos = (datetime.datetime.utcnow() - ultimaLeituraTimestamp).total_seconds()

    return {
        "status": tomadaStatus,
        "idade_leitura_segundos": int(idade_leitura_segundos)
    }

if __name__ == "__main__":
    # O Render define a porta automaticamente na variável PORT
    porta = int(os.getenv("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=porta)