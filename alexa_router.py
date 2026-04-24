import os
import httpx
import re
import json
import psycopg2
from fastapi import APIRouter, Request, BackgroundTasks
from typing import Dict, List, Any

router = APIRouter()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Mapeamento de personagens para seus respectivos modelos na API
MODELOS = {
    "eremita": "gemini-3-flash-preview",
    "sabio": "gemma-4-31b-it" # Pego do models.json
}

# Em memória: dicionário que mapeia o sessionId da Alexa para o estado da conversa.
# Estrutura: {"sessionId": {"messages": [...], "mode": "eremita" | "sabio"}}
active_sessions: Dict[str, Dict[str, Any]] = {}

SYSTEM_INSTRUCTION = "Você é um assistente conversacional inteligente que opera na Alexa. Seja claro, conciso e natural nas respostas, como se estivesse conversando. Evite formatação Markdown que a Alexa não saiba ler."

def init_db_alexa():
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(dsn=DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alexa_conversas (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                modo TEXT,
                turnos INTEGER,
                conversa_json JSONB,
                conversa_texto TEXT
            )
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Erro ao inicializar tabela de conversas: {e}")

init_db_alexa()

def salvar_conversa_db(session_data: dict):
    if not DATABASE_URL or not session_data.get("messages"):
        return
        
    mensagens = session_data["messages"]
    modo = session_data.get("mode", "desconhecido")
    turnos = len(mensagens) // 2
    
    # Formata texto legível
    texto_legivel = ""
    for msg in mensagens:
        role = "Usuário" if msg["role"] == "user" else "IA"
        texto = msg["parts"][0]["text"]
        texto_legivel += f"{role}: {texto}\n\n"
        
    conversa_json = json.dumps(mensagens)
    
    try:
        conn = psycopg2.connect(dsn=DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO alexa_conversas (modo, turnos, conversa_json, conversa_texto) VALUES (%s, %s, %s, %s)',
            (modo, turnos, conversa_json, texto_legivel)
        )
        conn.commit()
        cursor.close()
        conn.close()
        print(f"Conversa com o {modo} salva no banco de dados!")
    except Exception as e:
        print(f"Erro ao salvar conversa no banco: {e}")

async def call_gemini(messages: List[dict], model_name: str) -> str:
    """Faz a chamada assíncrona para a API da Google com o histórico da conversa."""
    if not GEMINI_API_KEY:
        return "Erro: A chave da API não está configurada no ambiente."
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    
    payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_INSTRUCTION}]
        },
        "contents": messages,
        "generationConfig": {
            "temperature": 0.7
        }
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=payload, timeout=40.0)
            
            # Tratamento caso o modelo especificado não exista ou outro erro de API
            if response.status_code != 200:
                print(f"Erro da API ({response.status_code}): {response.text}")
                return "Desculpe, tive um problema de comunicação com o cérebro deste personagem."

            data = response.json()
            if "candidates" in data and len(data["candidates"]) > 0:
                parts = data["candidates"][0]["content"].get("parts", [])
                if parts:
                    return parts[0].get("text", "Desculpe, não consegui formular uma resposta.")
            return "Desculpe, não entendi a resposta da API."
        except Exception as e:
            print(f"Erro ao chamar API: {e}")
            return "Desculpe, tive um problema ao conectar com a inteligência artificial."

def build_alexa_response(text: str, should_end_session: bool) -> dict:
    """Monta o payload de resposta padrão da Alexa."""
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": text
            },
            "shouldEndSession": should_end_session
        }
    }

@router.post('/alexa_ai')
async def alexa_endpoint(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    req_body = data.get("request", {})
    req_type = req_body.get("type")
    
    session = data.get("session", {})
    session_id = session.get("sessionId", "unknown_session")
    
    # 1. Tratamento do LaunchRequest (quando apenas abrem a skill sem perguntar direto)
    if req_type == "LaunchRequest":
        active_sessions[session_id] = {"messages": [], "mode": "eremita"} # Eremita é o padrão
        return build_alexa_response("Roteamento IA ativado.", False)
        
    # 2. Tratamento de Encerramento
    if req_type == "SessionEndedRequest":
        if session_id in active_sessions:
            if active_sessions[session_id].get("messages"):
                background_tasks.add_task(salvar_conversa_db, active_sessions[session_id])
            del active_sessions[session_id]
        return build_alexa_response("", True)
        
    # 3. Tratamento de Intent (Ação do usuário)
    if req_type == "IntentRequest":
        intent = req_body.get("intent", {})
        intent_name = intent.get("name")
        
        if intent_name in ["AMAZON.StopIntent", "AMAZON.CancelIntent", "SairIntent"]:
            if session_id in active_sessions:
                if active_sessions[session_id].get("messages"):
                    background_tasks.add_task(salvar_conversa_db, active_sessions[session_id])
                del active_sessions[session_id]
            return build_alexa_response("Fim do modo I.A.", True)
            
        slots = intent.get("slots", {})
        user_text = ""
        for slot_name, slot_data in slots.items():
            if slot_data.get("value"):
                user_text = slot_data.get("value")
                break
                
        if not user_text:
            return build_alexa_response("Não escutei direito.", False)
            
        # Recupera ou inicializa a sessão
        if session_id not in active_sessions:
            active_sessions[session_id] = {"messages": [], "mode": "eremita"}
            
        session_data = active_sessions[session_id]
        text_lower = user_text.lower().strip()
        
        # Lógica de roteamento baseada na fala inicial
        # Se a frase contém a invocação, definimos o modo e cortamos essa parte para não sujar o prompt da IA
        if text_lower.startswith("pergunte ao eremita"):
            session_data["mode"] = "eremita"
            user_text = re.sub(r'(?i)^pergunte ao eremita\s*(que|qual|como|onde|por que)?\s*', '', user_text).strip()
            
            # Se ele só disser "pergunte ao eremita", sem query, a gente avisa:
            if not user_text:
                return build_alexa_response("Gemini ativado, pode perguntar.", False)

        elif text_lower.startswith("pergunte ao sabio") or text_lower.startswith("pergunte ao sábio"):
            session_data["mode"] = "sabio"
            user_text = re.sub(r'(?i)^pergunte ao s[áa]bio\s*(que|qual|como|onde|por que)?\s*', '', user_text).strip()
            
            if not user_text:
                return build_alexa_response("Gema ativado, pode perguntar.", False)
        
        # Ou se for logo após abrir a skill e o cara só fala o nome
        elif text_lower in ["ao eremita", "eremita", "o eremita"]:
            session_data["mode"] = "eremita"
            return build_alexa_response("Gemini ativado, pode perguntar.", False)
        elif text_lower in ["ao sabio", "sabio", "o sabio", "ao sábio", "sábio", "o sábio"]:
            session_data["mode"] = "sabio"
            return build_alexa_response("Gema ativado, pode perguntar.", False)
            
        # Caso o usuário já esteja falando no meio da sessão
        messages = session_data["messages"]
        model_to_use = MODELOS[session_data["mode"]]
        
        messages.append({"role": "user", "parts": [{"text": user_text}]})
        
        # Chama a API
        ai_response_text = await call_gemini(messages, model_to_use)
        
        # Anexa a resposta da IA ao contexto
        messages.append({"role": "model", "parts": [{"text": ai_response_text}]})
        
        # Mantém a sessão aberta e devolve a resposta
        return build_alexa_response(ai_response_text, False)
        
    return build_alexa_response("Ação não reconhecida no templo.", True)

@router.get('/health_ai')
def health_check():
    """Rota simples para o Render testar se o roteador de IA está vivo."""
    return {
        "status": "ok", 
        "active_sessions": len(active_sessions)
    }
