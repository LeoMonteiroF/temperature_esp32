import os
import psycopg2
from dotenv import load_dotenv

# Load environment variables if necessary (though DATABASE_URL should be available)
DATABASE_URL = os.getenv("DATABASE_URL")

def check_config():
    if not DATABASE_URL:
        print("DATABASE_URL not found")
        return
    
    conn = psycopg2.connect(dsn=DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute('SELECT chave, valor FROM configuracoes')
    rows = cursor.fetchall()
    print("Keys in database:")
    for row in rows:
        print(f"- {row[0]}: {row[1]}")
    cursor.close()
    conn.close()

if __name__ == "__main__":
    check_config()
