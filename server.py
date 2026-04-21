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
TEMP_CORTE = 9.75             # Ponto de desligamento alvo (meio da faixa 9.0-10.5)
HISTERESE = 0.75              # Metade da faixa de histerese (0.75 = 1.5 / 2)
TEMP_MAX_OVERSHOOT = 11.0     # Teto de segurança pós-corte (mantido)

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
    conn.commit()
    cursor.close()
    conn.close()

init_db()

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
# @app.post('/boot')
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
def rota_temperatura(data: TemperatureData, background_tasks: BackgroundTasks):
    global ultimaLeituraTimestamp, tomadaStatus, timestampMudancaEstado
    agora = datetime.datetime.now()
    ultimaLeituraTimestamp = agora
    
    # Lógica de Aquecimento
    if data.temperatura <= (TEMP_CORTE - HISTERESE):
        novo_status = "on"
    elif data.temperatura >= TEMP_CORTE:
        novo_status = "off"
    else:
        novo_status = tomadaStatus # Mantém estado atual dentro da histerese
        
    if novo_status != tomadaStatus:
        tomadaStatus = novo_status
        timestampMudancaEstado = agora
        registrar_log(f"Server-side Hysteresis: Set to '{tomadaStatus}' (Temp: {data.temperatura}°C)")

    # Atualiza o cofre da Alexa com o horário corrigido
    # ultima_leitura["temperatura"] = data.temperatura
    # ultima_leitura["horario_fala"] = obter_horario_brasil_extenso()

    # Para o log visual, usamos apenas o relógio
    # fuso_br = pytz.timezone('America/Sao_Paulo')
    # ultima_leitura["horario"] = agora.strftime("%H:%M:%S")

    salvar_leitura("temperatura", data.temperatura, "DS18B20")
    msg = f"[{ultima_leitura['horario']}] Temperatura: {data.temperatura}°C | Tomada Alvo: {tomadaStatus}"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/temperatura_dht')
def rota_temperatura_dht(data: TemperatureData):
    global ultimaLeituraTimestamp
    agora = datetime.datetime.now()
    ultimaLeituraTimestamp = agora
    
    salvar_leitura("temperatura", data.temperatura, "DHT22")
    msg = f"[{data.horario}] [ESP32] Temperatura DHT22: {data.temperatura}°C"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/umidade_dht')
def rota_umidade_dht(data: HumidityData):
    global ultimaLeituraTimestamp
    ultimaLeituraTimestamp = datetime.datetime.now()
    
    salvar_leitura("umidade", data.umidade, "DHT22")
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

@app.api_route('/', response_class=HTMLResponse, methods=["GET", "HEAD"])
async def pagina_principal():
    html = f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>{TITULO_PAINEL}</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{ font-family: sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 20px; }}
                h1 {{ color: #bb86fc; border-bottom: 1px solid #333; padding-bottom: 10px; }}
                .user-info {{ color: #03dac6; margin-bottom: 20px; font-weight: bold; }}
                .nav {{ margin-bottom: 20px; }}
                .nav a {{ color: #bb86fc; text-decoration: none; margin-right: 15px; border: 1px solid #bb86fc; padding: 5px 10px; border-radius: 4px; }}
                .terminal {{ background: #000; border: 1px solid #333; border-radius: 5px; padding: 15px; height: 70vh; overflow-y: auto; font-family: monospace; }}
                .line {{ border-bottom: 1px solid #1a1a1a; padding: 5px 0; color: #00ff41; }}
            </style>
            <script>
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
                setInterval(updateLogs, 3000);
                
                window.onload = () => {{
                    const terminal = document.getElementById('terminal');
                    terminal.addEventListener('scroll', () => {{
                        // Se o usuário subir mais de 50px do fundo, desativa auto-scroll
                        autoScroll = (terminal.scrollHeight - terminal.clientHeight <= terminal.scrollTop + 50);
                    }});
                    updateLogs();
                }};
            </script>
        </head>
        <body>
            <h1>{TITULO_PAINEL}</h1>
            <div class="user-info">Operador: {NOME_USUARIO}</div>
            <div class="nav">
                <a href="/graficos">Ver Gráficos</a>
            </div>
            <div id="terminal" class="terminal">
                <div class='line'>Carregando logs...</div>
            </div>
        </body>
    </html>
    """
    return html

@app.get('/api/logs')
async def api_logs():
    return list(reversed(logs_armazenados))

@app.api_route('/check-status', methods=["GET", "HEAD"])
async def check_status():
    global ultimaLeituraTimestamp
    global tomadaStatus

    idade_leitura_segundos = -1
    if ultimaLeituraTimestamp:
        idade_leitura_segundos = (datetime.datetime.now() - ultimaLeituraTimestamp).total_seconds()

    return {
        "status": tomadaStatus,
        "idade_leitura_segundos": int(idade_leitura_segundos)
    }

@app.get('/graficos', response_class=HTMLResponse)
async def pagina_graficos(periodo: str = "1", resolucao: Optional[int] = None):
    # periodo em dias: 1, 3, 7, 14
    # resolucao: a cada N medições
    
    if resolucao is None:
        if periodo in ["1", "3"]:
            resolucao = 10
        else:
            resolucao = 100
    
    html = f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>Gráficos - {TITULO_PAINEL}</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <style>
                body {{ font-family: sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 20px; }}
                h1 {{ color: #bb86fc; }}
                .nav {{ margin-bottom: 20px; }}
                .nav a {{ color: #bb86fc; text-decoration: none; margin-right: 15px; border: 1px solid #bb86fc; padding: 5px 10px; border-radius: 4px; }}
                .controls {{ background: #1e1e1e; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; gap: 20px; align-items: center; flex-wrap: wrap; }}
                select, button {{ padding: 8px; border-radius: 4px; background: #333; color: #fff; border: 1px solid #555; }}
                .chart-container {{ background: #1e1e1e; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
                canvas {{ max-height: 400px; }}
            </style>
        </head>
        <body>
            <h1>Gráficos de Fermentação</h1>
            <div class="nav">
                <a href="/">Voltar ao Terminal</a>
            </div>
            
            <form class="controls" method="get">
                <div>
                    <label>Período:</label>
                    <select name="periodo">
                        <option value="1" {"selected" if periodo=="1" else ""}>Último Dia</option>
                        <option value="3" {"selected" if periodo=="3" else ""}>Últimos 3 Dias</option>
                        <option value="7" {"selected" if periodo=="7" else ""}>Última Semana</option>
                        <option value="14" {"selected" if periodo=="14" else ""}>Últimos 14 Dias</option>
                    </select>
                </div>
                <div>
                    <label>Resolução (cada N medições):</label>
                    <input type="number" name="resolucao" value="{resolucao}" style="width: 60px; padding: 8px; border-radius: 4px; background: #333; color: #fff; border: 1px solid #555;">
                    <small style="color: #888; display: block;">Padrão: 10 (1-3 dias) ou 100 (7-14 dias)</small>
                </div>
                <button type="submit">Atualizar</button>
            </form>

            <div class="chart-container">
                <canvas id="tempChart"></canvas>
            </div>
            <div class="chart-container">
                <canvas id="humChart"></canvas>
            </div>

            <script>
                async function loadData() {{
                    const resp = await fetch(`/api/dados?periodo={periodo}&resolucao={resolucao}`);
                    const data = await resp.json();
                    
                    const ctxTemp = document.getElementById('tempChart').getContext('2d');
                    new Chart(ctxTemp, {{
                        type: 'line',
                        data: {{
                            labels: data.labels,
                            datasets: [
                                {{
                                    label: 'DS18B20 (°C)',
                                    data: data.temp_ds,
                                    borderColor: '#bb86fc',
                                    tension: 0.1,
                                    spanGaps: true
                                }},
                                {{
                                    label: 'DHT22 (°C)',
                                    data: data.temp_dht,
                                    borderColor: '#03dac6',
                                    tension: 0.1,
                                    spanGaps: true
                                }}
                            ]
                        }},
                        options: {{
                            responsive: true,
                            plugins: {{ title: {{ display: true, text: 'Temperatura', color: '#e0e0e0' }} }},
                            scales: {{
                                x: {{ ticks: {{ color: '#aaa' }} }},
                                y: {{ ticks: {{ color: '#aaa' }} }}
                            }}
                        }}
                    }});

                    const ctxHum = document.getElementById('humChart').getContext('2d');
                    new Chart(ctxHum, {{
                        type: 'line',
                        data: {{
                            labels: data.labels,
                            datasets: [{{
                                label: 'Umidade DHT22 (%)',
                                data: data.hum_dht,
                                borderColor: '#cf6679',
                                tension: 0.1,
                                spanGaps: true
                            }}]
                        }},
                        options: {{
                            responsive: true,
                            plugins: {{ title: {{ display: true, text: 'Umidade', color: '#e0e0e0' }} }},
                            scales: {{
                                x: {{ ticks: {{ color: '#aaa' }} }},
                                y: {{ ticks: {{ color: '#aaa' }} }}
                            }}
                        }}
                    }});
                }}
                loadData();
            </script>
        </body>
    </html>
    """
    return html

@app.get('/api/dados')
def api_dados(periodo: int = 1, resolucao: Optional[int] = None):
    if resolucao is None:
        resolucao = 10 if periodo <= 3 else 100
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Filtro de tempo
    data_limite = (datetime.datetime.now() - datetime.timedelta(days=periodo))
    
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
        dt = ts
        label = dt.strftime('%H:%M') if periodo <= 1 else dt.strftime('%d/%m %H:%M')
        
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

if __name__ == "__main__":
    # O Render define a porta automaticamente na variável PORT
    porta = int(os.getenv("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=porta)