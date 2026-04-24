import os
import datetime
import uvicorn
import asyncio
import pytz
import psycopg2
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI()

import alexa_router

FUSO_BR = pytz.timezone('America/Sao_Paulo')

def agora_br():
    return datetime.datetime.now(FUSO_BR)

def utc_to_br(dt_utc_naive):
    if dt_utc_naive is None:
        return None
    dt_utc_aware = dt_utc_naive.replace(tzinfo=pytz.utc)
    return dt_utc_aware.astimezone(FUSO_BR)

# --- BANCO DE DADOS (Supabase/PostgreSQL) ---
DATABASE_URL = os.getenv("DATABASE_URL")

# --- ESTADO DA TOMADA (Proxy Local) ---
tomadaStatus: str = "off"
ultimaLeituraTimestamp: Optional[datetime.datetime] = None
timestampMudancaEstado: Optional[datetime.datetime] = None
LIMITE_INERCIA_TERMICA = 300  # 5 minutos para detectar dessincronização
TEMPO_PULSO_RESYNC = 30       # 30 segundos de pulso para forçar o Google Home

# --- CONFIGURAÇÕES DE AQUECIMENTO ---
em_pulso_resync: bool = False
DEFAULT_CONFIG = {
    "temp_corte": 13.0,
    "histerese": 2.0,
    "temp_max_overshoot": 14.0,
    "derivada_critica": -15.0,  # Queda muito rápida (desativado por padrão)
    "offset_piso": 0.5          # Quanto subir o piso
}
CONFIG = DEFAULT_CONFIG.copy()

# Variáveis para cálculo da derivada
ultima_temp_derivada = None
ultimo_ts_derivada = None

def get_db_connection():
    # Se DATABASE_URL for uma string de conexão completa, psycopg2.connect(DATABASE_URL) deveria funcionar.
    # O erro "invalid port number" sugere que o psycopg2 está interpretando parte da string como porta.
    # Vamos tentar usar o parâmetro dsn explicitamente.
    return psycopg2.connect(dsn=DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    # Tabela para leituras (temperatura, umidade)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS leituras (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tipo TEXT,
            valor REAL,
            sensor TEXT
        )
    ''')
    # Tabela para logs do sistema
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            mensagem TEXT
        )
    ''')
    # Tabela para configurações
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS configuracoes (
            chave TEXT PRIMARY KEY,
            valor REAL
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()

def carregar_configuracoes():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT chave, valor FROM configuracoes')
    rows = cursor.fetchall()
    
    if not rows:
        # Tabela vazia, vamos inserir os valores padrão
        for k, v in DEFAULT_CONFIG.items():
            cursor.execute('INSERT INTO configuracoes (chave, valor) VALUES (%s, %s)', (k, v))
        conn.commit()
    else:
        # Carregar do banco para a memória
        for row in rows:
            CONFIG[row[0]] = row[1]
            
    cursor.close()
    conn.close()

init_db()
carregar_configuracoes()

def salvar_leitura(tipo: str, valor: float, sensor: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO leituras (tipo, valor, sensor) VALUES (%s, %s, %s)', (tipo, valor, sensor))
    conn.commit()
    cursor.close()
    conn.close()

def salvar_log_db(mensagem: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO logs (mensagem) VALUES (%s)', (mensagem,))
    conn.commit()
    cursor.close()
    conn.close()

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
    salvar_log_db(mensagem)
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

    # --- LÓGICA DE AQUECIMENTO (Piso Dinâmico) ---
    cfg_temp_corte = CONFIG.get("temp_corte", DEFAULT_CONFIG["temp_corte"])
    cfg_histerese = CONFIG.get("histerese", DEFAULT_CONFIG["histerese"])
    cfg_derivada_critica = CONFIG.get("derivada_critica", DEFAULT_CONFIG["derivada_critica"])
    cfg_offset_piso = CONFIG.get("offset_piso", DEFAULT_CONFIG["offset_piso"])
    
    piso_histerese = cfg_temp_corte - cfg_histerese
    usando_piso_dinamico = False
    
    if tomadaStatus == "off" and derivada <= cfg_derivada_critica:
        piso_histerese += cfg_offset_piso
        usando_piso_dinamico = True

    if data.temperatura <= piso_histerese:
        novo_status = "on"
    elif data.temperatura >= cfg_temp_corte:
        novo_status = "off"
    else:
        novo_status = tomadaStatus # Mantém estado atual dentro da histerese
        
    if novo_status != tomadaStatus:
        tomadaStatus = novo_status
        timestampMudancaEstado = agora
        if tomadaStatus == "on" and usando_piso_dinamico:
            registrar_log(f"Server-side Hysteresis: Set to 'on' (Anticipatory Trigger! Deriv: {derivada:.2f}°C/min, Floor: {piso_histerese:.1f}°C)")
        else:
            registrar_log(f"Server-side Hysteresis: Set to '{tomadaStatus}' (Temp: {data.temperatura}°C)")

    # Atualiza o cofre da Alexa com o horário corrigido
    ultima_leitura["temperatura"] = data.temperatura
    ultima_leitura["horario_fala"] = obter_horario_brasil_extenso()

    # Para o log visual, usamos apenas o relógio
    ultima_leitura["horario"] = agora_br().strftime("%H:%M:%S")

    salvar_leitura("temperatura", data.temperatura, "DS18B20")
    msg = f"[{ultima_leitura['horario']}] Temperatura: {data.temperatura}°C | Tomada Alvo: {tomadaStatus}"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/temperatura_dht')
def rota_temperatura_dht(data: TemperatureData):
    global ultimaLeituraTimestamp
    agora = datetime.datetime.utcnow()
    ultimaLeituraTimestamp = agora
    
    salvar_leitura("temperatura", data.temperatura, "DHT22")
    msg = f"[{data.horario}] [ESP32] Temperatura DHT22: {data.temperatura}°C"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/umidade_dht')
def rota_umidade_dht(data: HumidityData):
    global ultimaLeituraTimestamp
    ultimaLeituraTimestamp = datetime.datetime.utcnow()
    
    salvar_leitura("umidade", data.umidade, "DHT22")
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
        temp_corte = str(CONFIG.get("temp_corte", 13.0)).replace('.', ',')
        histerese = str(CONFIG.get("histerese", 2.0)).replace('.', ',')
        temp_max = str(CONFIG.get("temp_max_overshoot", 14.0)).replace('.', ',')
        
        derivada = CONFIG.get("derivada_critica", -15.0)
        derivada_str = str(abs(derivada)).replace('.', ',')
        
        offset = str(CONFIG.get("offset_piso", 0.5)).replace('.', ',')
        
        fala = (
            f"Configurações vigentes: Temperatura alvo de {temp_corte} graus, com faixa de variação permitida de {histerese} graus. "
            f"O limite de segurança máximo é de {temp_max} graus. "
            f"A proteção anti-compressor está configurada para identificar queda maior que {derivada_str} graus por minuto, "
            f"subindo o piso temporariamente em {offset} graus para previnir undershoot."
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
async def pagina_principal(periodo: str = "1", resolucao: Optional[int] = None):
    if resolucao is None:
        if periodo in ["1", "3"] or periodo.endswith('h'):
            resolucao = 10
        else:
            resolucao = 100
            
    html = f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>{TITULO_PAINEL}</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <style>
                body {{ font-family: sans-serif; background: #121212; color: #e0e0e0; margin: 0; display: flex; min-height: 100vh; }}
                /* Sidebar Styles */
                .sidebar {{ width: 250px; background: #1e1e1e; padding: 20px; border-right: 1px solid #333; display: flex; flex-direction: column; }}
                .sidebar h2 {{ color: #bb86fc; margin-top: 0; font-size: 1.2rem; }}
                .user-info {{ color: #03dac6; margin-bottom: 20px; font-weight: bold; font-size: 0.9rem; }}
                .endpoint-list {{ list-style: none; padding: 0; margin: 0; flex-grow: 1; }}
                .endpoint-list li {{ margin-bottom: 10px; }}
                .endpoint-list a {{ color: #e0e0e0; text-decoration: none; display: block; padding: 10px; border-radius: 4px; background: #333; transition: background 0.2s; }}
                .endpoint-list a:hover {{ background: #444; }}
                .method-badge {{ display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 0.7rem; font-weight: bold; margin-right: 8px; }}
                .method-get {{ background: #03dac6; color: #000; }}
                .method-post {{ background: #bb86fc; color: #000; }}
                
                /* Main Content Styles */
                .main-content {{ flex-grow: 1; padding: 20px; display: flex; flex-direction: column; height: 100vh; box-sizing: border-box; overflow-y: auto; }}
                .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 20px; }}
                .header h1 {{ color: #bb86fc; margin: 0; font-size: 1.5rem; }}
                
                /* Controls */
                .controls {{ background: #1e1e1e; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }}
                select, button, input {{ padding: 8px; border-radius: 4px; background: #333; color: #fff; border: 1px solid #555; }}
                button {{ cursor: pointer; background: #03dac6; color: #000; font-weight: bold; border: none; }}
                button:hover {{ background: #01b8a5; }}
                
                /* Charts Grid */
                .charts-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
                @media (max-width: 1000px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
                .chart-container {{ background: #1e1e1e; padding: 20px; border-radius: 8px; }}
                canvas {{ max-height: 300px; }}
                
                /* Terminal */
                .terminal-container {{ flex-grow: 1; display: flex; flex-direction: column; min-height: 250px; }}
                .terminal-container h3 {{ color: #bb86fc; margin-top: 0; margin-bottom: 10px; font-size: 1.2rem; }}
                .terminal {{ background: #000; border: 1px solid #333; border-radius: 5px; padding: 15px; flex-grow: 1; overflow-y: auto; font-family: monospace; }}
                .line {{ border-bottom: 1px solid #1a1a1a; padding: 5px 0; color: #00ff41; }}
                
                /* Modal Styles */
                .modal-overlay {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 1000; justify-content: center; align-items: center; }}
                .modal {{ background: #1e1e1e; padding: 20px; border-radius: 8px; width: 400px; max-width: 90%; border: 1px solid #333; }}
                .modal h2 {{ color: #bb86fc; margin-top: 0; }}
                .modal-form-group {{ margin-bottom: 15px; }}
                .modal-form-group label {{ display: block; margin-bottom: 5px; color: #aaa; font-size: 0.9rem; }}
                .modal-form-group input {{ width: 100%; box-sizing: border-box; padding: 8px; border-radius: 4px; background: #333; color: #fff; border: 1px solid #555; }}
                .modal-buttons {{ display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }}
                .btn-cancel {{ background: #333; color: #fff; }}
                .btn-cancel:hover {{ background: #444; }}
            </style>
            <script>
                // Settings Modal Logic
                async function openSettings() {{
                    const resp = await fetch('/api/config');
                    const config = await resp.json();
                    
                    const container = document.getElementById('configFields');
                    container.innerHTML = '';
                    
                    for (const [key, value] of Object.entries(config)) {{
                        container.innerHTML += `
                            <div class="modal-form-group">
                                <label>${{key}}</label>
                                <input type="number" step="0.1" id="cfg_${{key}}" value="${{value}}">
                            </div>
                        `;
                    }}
                    
                    document.getElementById('settingsModal').style.display = 'flex';
                }}
                
                function closeSettings() {{
                    document.getElementById('settingsModal').style.display = 'none';
                }}
                
                async function saveSettings() {{
                    const inputs = document.querySelectorAll('input[id^="cfg_"]');
                    const newConfig = {{}};
                    inputs.forEach(input => {{
                        const key = input.id.replace('cfg_', '');
                        newConfig[key] = parseFloat(input.value);
                    }});
                    
                    const resp = await fetch('/api/config', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ configs: newConfig }})
                    }});
                    
                    if (resp.ok) {{
                        alert('Configurações salvas com sucesso!');
                        closeSettings();
                    }} else {{
                        alert('Erro ao salvar configurações.');
                    }}
                }}

                // Terminal Logic
                let autoScroll = true;
                function updateLogs() {{
                    fetch('/api/logs')
                        .then(response => response.json())
                        .then(data => {{
                            const terminal = document.getElementById('terminal');
                            const wasAtBottom = terminal.scrollHeight - terminal.clientHeight <= terminal.scrollTop + 1;
                            
                            terminal.innerHTML = data.map(log => `<div class='line'>${{log}}</div>`).join('');
                            
                            if (autoScroll && wasAtBottom) {{
                                terminal.scrollTop = terminal.scrollHeight;
                            }}
                        }});
                }}
                
                // Charts Logic
                async function loadData() {{
                    const resp = await fetch(`/api/dados?periodo={periodo}&resolucao={resolucao}`);
                    const data = await resp.json();
                    
                    const ctxTemp = document.getElementById('tempChart').getContext('2d');
                    new Chart(ctxTemp, {{
                        type: 'line',
                        data: {{
                            labels: data.labels,
                            datasets: [
                                {{ label: 'DS18B20 (°C)', data: data.temp_ds, borderColor: '#bb86fc', tension: 0.1, spanGaps: true }},
                                {{ label: 'DHT22 (°C)', data: data.temp_dht, borderColor: '#03dac6', tension: 0.1, spanGaps: true }}
                            ]
                        }},
                        options: {{ responsive: true, plugins: {{ title: {{ display: true, text: 'Temperatura', color: '#e0e0e0' }} }}, scales: {{ x: {{ ticks: {{ color: '#aaa' }} }}, y: {{ ticks: {{ color: '#aaa' }} }} }} }}
                    }});

                    const ctxHum = document.getElementById('humChart').getContext('2d');
                    new Chart(ctxHum, {{
                        type: 'line',
                        data: {{
                            labels: data.labels,
                            datasets: [{{ label: 'Umidade DHT22 (%)', data: data.hum_dht, borderColor: '#cf6679', tension: 0.1, spanGaps: true }}]
                        }},
                        options: {{ responsive: true, plugins: {{ title: {{ display: true, text: 'Umidade', color: '#e0e0e0' }} }}, scales: {{ x: {{ ticks: {{ color: '#aaa' }} }}, y: {{ ticks: {{ color: '#aaa' }} }} }} }}
                    }});
                }}

                window.onload = () => {{
                    const terminal = document.getElementById('terminal');
                    terminal.addEventListener('scroll', () => {{
                        autoScroll = (terminal.scrollHeight - terminal.clientHeight <= terminal.scrollTop + 50);
                    }});
                    updateLogs();
                    setInterval(updateLogs, 3000);
                    loadData();
                }};
            </script>
        </head>
        <body>
            <!-- Sidebar -->
            <div class="sidebar">
                <h2>{TITULO_PAINEL}</h2>
                <div class="user-info">Operador: {NOME_USUARIO}</div>
                
                <h3 style="color: #888; font-size: 0.8rem; margin-top: 20px;">ENDPOINTS</h3>
                <ul class="endpoint-list">
                    <li><a href="/"><span class="method-badge method-get">GET</span> / (Dashboard)</a></li>
                    <li><a href="/check-status" target="_blank"><span class="method-badge method-get">GET</span> /check-status</a></li>
                    <li><a href="/api/logs" target="_blank"><span class="method-badge method-get">GET</span> /api/logs</a></li>
                    <li><a href="/api/dados?periodo=1" target="_blank"><span class="method-badge method-get">GET</span> /api/dados</a></li>
                    <li><a href="#" style="pointer-events: none; opacity: 0.7;"><span class="method-badge method-post">POST</span> /temperatura</a></li>
                    <li><a href="#" style="pointer-events: none; opacity: 0.7;"><span class="method-badge method-post">POST</span> /temperatura_dht</a></li>
                    <li><a href="#" style="pointer-events: none; opacity: 0.7;"><span class="method-badge method-post">POST</span> /umidade_dht</a></li>
                    <li><a href="#" style="pointer-events: none; opacity: 0.7;"><span class="method-badge method-post">POST</span> /log</a></li>
                    <li><a href="#" style="pointer-events: none; opacity: 0.7;"><span class="method-badge method-post">POST</span> /alexa</a></li>
                    <li><a href="#" style="pointer-events: none; opacity: 0.7;"><span class="method-badge method-post">POST</span> /alexa_ai</a></li>
                </ul>
                
                <div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid #333;">
                    <button onclick="openSettings()" style="width: 100%; background: #03dac6; color: #000; padding: 10px; margin-bottom: 10px;">Editar Configurações</button>
                    <button onclick="reloadConfig()" style="width: 100%; background: #bb86fc; color: #000; padding: 10px;">Recarregar Configurações</button>
                    <script>
                        async function reloadConfig() {{
                            try {{
                                const resp = await fetch('/api/reload_config', {{ method: 'POST' }});
                                if(resp.ok) {{
                                    alert('Configurações recarregadas com sucesso!');
                                }} else {{
                                    alert('Erro ao recarregar configurações.');
                                }}
                            }} catch (e) {{
                                alert('Erro de conexão ao recarregar.');
                            }}
                        }}
                    </script>
                </div>
            </div>
            
            <!-- Main Content -->
            <div class="main-content">
                <div class="header">
                    <h1>Dashboard de Controle</h1>
                </div>
                
                <!-- Chart Controls -->
                <form class="controls" method="get">
                    <div>
                        <label>Período:</label>
                        <select name="periodo">
                            <option value="6h" {"selected" if periodo=="6h" else ""}>Últimas 6 Horas</option>
                            <option value="12h" {"selected" if periodo=="12h" else ""}>Últimas 12 Horas</option>
                            <option value="1" {"selected" if periodo=="1" else ""}>Último Dia</option>
                            <option value="3" {"selected" if periodo=="3" else ""}>Últimos 3 Dias</option>
                            <option value="7" {"selected" if periodo=="7" else ""}>Última Semana</option>
                            <option value="14" {"selected" if periodo=="14" else ""}>Últimos 14 Dias</option>
                        </select>
                    </div>
                    <div>
                        <label>Resolução (cada N medições):</label>
                        <input type="number" name="resolucao" value="{resolucao}" style="width: 60px;">
                    </div>
                    <button type="submit">Atualizar Gráficos</button>
                </form>
                
                <!-- Charts -->
                <div class="charts-grid">
                    <div class="chart-container"><canvas id="tempChart"></canvas></div>
                    <div class="chart-container"><canvas id="humChart"></canvas></div>
                </div>
                
                <!-- Terminal -->
                <div class="terminal-container">
                    <h3>Console / Logs</h3>
                    <div id="terminal" class="terminal">
                        <div class='line'>Carregando logs...</div>
                    </div>
                </div>
            </div>
            
            <!-- Settings Modal -->
            <div id="settingsModal" class="modal-overlay">
                <div class="modal">
                    <h2>Configurações do Sistema</h2>
                    <div id="configFields"></div>
                    <div class="modal-buttons">
                        <button class="btn-cancel" onclick="closeSettings()">Cancelar</button>
                        <button onclick="saveSettings()">Salvar</button>
                    </div>
                </div>
            </div>
        </body>
    </html>
    """
    return html

@app.get('/api/config')
def get_config():
    return CONFIG

@app.post('/api/config')
def update_config(data: ConfigData):
    conn = get_db_connection()
    cursor = conn.cursor()
    for k, v in data.configs.items():
        if k in CONFIG:
            try:
                val = float(v)
                cursor.execute('UPDATE configuracoes SET valor = %s WHERE chave = %s', (val, k))
                CONFIG[k] = val
            except (ValueError, TypeError):
                pass
    conn.commit()
    cursor.close()
    conn.close()
    registrar_log("Configurações atualizadas via painel.")
    return {"status": "ok"}

@app.post('/api/reload_config')
async def reload_config():
    carregar_configuracoes()
    registrar_log("Configurações recarregadas do banco de dados pelo Dashboard.")
    return {"status": "ok", "config": CONFIG}

@app.get('/api/dados')
def api_dados(periodo: str = "1", resolucao: Optional[int] = None):
    if resolucao is None:
        # Se for horas (6h, 12h) ou até 3 dias, resolução 10. Senão 100.
        if periodo.endswith('h') or (periodo.isdigit() and int(periodo) <= 3):
            resolucao = 10
        else:
            resolucao = 100
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Filtro de tempo
    if periodo.endswith('h'):
        horas = int(periodo[:-1])
        data_limite = (datetime.datetime.utcnow() - datetime.timedelta(hours=horas))
    else:
        dias = int(periodo)
        data_limite = (datetime.datetime.utcnow() - datetime.timedelta(days=dias))
    
    # Busca dados
    cursor.execute('''
        SELECT timestamp, tipo, valor, sensor
        FROM leituras
        WHERE timestamp >= %s
        ORDER BY timestamp ASC
    ''', (data_limite,))
    
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    # Processamento com resolução (downsampling simples)
    # Agrupamos por timestamp aproximado ou apenas pulamos N registros
    # Para simplificar e atender o pedido: "a cada N medições"
    
    labels = []
    temp_ds = []
    temp_dht = []
    hum_dht = []
    
    # Dicionário para alinhar por tempo (simplificado)
    # Como as medições não são síncronas, vamos apenas pegar os pontos na resolução pedida
    
    for i in range(0, len(rows), resolucao):
        row = rows[i]
        ts = row[0]
        tipo = row[1]
        valor = row[2]
        sensor = row[3]
        
        # Formata timestamp para o gráfico (HH:mm se for 1 dia, DD/MM HH:mm se for mais)
        # ts já vem como objeto datetime do psycopg2
        dt = utc_to_br(ts)
        # Formata timestamp para o gráfico (HH:mm se for < 1 dia ou exatamente 1 dia, DD/MM HH:mm se for mais)
        if periodo.endswith('h') or periodo == "1":
            label = dt.strftime('%H:%M')
        else:
            label = dt.strftime('%d/%m %H:%M')
        
        labels.append(label)
        
        # Aqui a lógica de alinhamento é falha se os sensores postarem em tempos muito diferentes,
        # mas para um gráfico de tendência funciona.
        if tipo == "temperatura":
            if sensor == "DS18B20":
                temp_ds.append(valor)
                temp_dht.append(None)
                hum_dht.append(None)
            else:
                temp_dht.append(valor)
                temp_ds.append(None)
                hum_dht.append(None)
        else:
            hum_dht.append(valor)
            temp_ds.append(None)
            temp_dht.append(None)

    return {
        "labels": labels,
        "temp_ds": temp_ds,
        "temp_dht": temp_dht,
        "hum_dht": hum_dht
    }

@app.get('/api/logs')
async def api_logs():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT mensagem FROM logs ORDER BY id DESC LIMIT %s', (LIMITE_HISTORICO,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    logs = [row[0] for row in rows]
    logs.reverse() # Mais antigos primeiro, para que o mais recente fique no fundo do terminal
    return logs

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