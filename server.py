import os
import datetime
import uvicorn
from fastapi import FastAPI
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

class TemperatureData(BaseModel):
    temperatura: float

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

@app.post('/temperatura')
async def rota_temperatura(data: TemperatureData):
    agora = datetime.datetime.now().strftime("[%d/%m %H:%M:%S]")
    msg = f"{agora} Temperatura: {data.temperatura}°C"
    registrar_log(msg)
    return {"status": "recebido"}

@app.post('/log')
async def rota_log(data: LogData):
    agora = datetime.datetime.now().strftime("[%H:%M:%S]")
    msg = f"[{agora}] [ESP32] {data.log}"
    registrar_log(msg)
    return {"status": "log_registrado"}

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