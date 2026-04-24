import os
import datetime
import psycopg2
import pytz
from typing import List, Optional

# --- CONFIGURAÇÃO DE FUSO HORÁRIO ---
FUSO_BR = pytz.timezone('America/Sao_Paulo')

def utc_to_br(dt_utc_naive):
    if dt_utc_naive is None:
        return None
    dt_utc_aware = dt_utc_naive.replace(tzinfo=pytz.utc)
    return dt_utc_aware.astimezone(FUSO_BR)

# --- CONEXÃO ---
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(dsn=DATABASE_URL)

def init_db(default_config):
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
    
    # Inicializa configurações padrão se a tabela estiver vazia
    cursor.execute('SELECT COUNT(*) FROM configuracoes')
    if cursor.fetchone()[0] == 0:
        for k, v in default_config.items():
            cursor.execute('INSERT INTO configuracoes (chave, valor) VALUES (%s, %s)', (k, v))
            
    conn.commit()
    cursor.close()
    conn.close()

def carregar_configuracoes(config_dict):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT chave, valor FROM configuracoes')
    rows = cursor.fetchall()
    
    for row in rows:
        config_dict[row[0]] = row[1]
            
    cursor.close()
    conn.close()

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

def atualizar_config_db(configs_para_atualizar, current_config_dict):
    conn = get_db_connection()
    cursor = conn.cursor()
    for k, v in configs_para_atualizar.items():
        if k in current_config_dict:
            try:
                val = float(v)
                cursor.execute('UPDATE configuracoes SET valor = %s WHERE chave = %s', (val, k))
                current_config_dict[k] = val
            except (ValueError, TypeError):
                pass
    conn.commit()
    cursor.close()
    conn.close()

def buscar_logs(limite: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT mensagem FROM logs ORDER BY id DESC LIMIT %s', (limite,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    logs = [row[0] for row in rows]
    logs.reverse()
    return logs

def buscar_dados_grafico(periodo: str, resolucao: int):
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
    
    labels = []
    temp_ds = []
    temp_dht = []
    hum_dht = []
    
    for i in range(0, len(rows), resolucao):
        row = rows[i]
        ts = row[0]
        tipo = row[1]
        valor = row[2]
        sensor = row[3]
        
        dt = utc_to_br(ts)
        if periodo.endswith('h') or periodo == "1":
            label = dt.strftime('%H:%M')
        else:
            label = dt.strftime('%d/%m %H:%M')
        
        labels.append(label)
        
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
