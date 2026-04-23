import os, sqlite3, json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
app = Flask(__name__)
CORS(app)

def init_db():
    conn = sqlite3.connect('database.db')
    conn.execute('CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, role TEXT, content TEXT)')
    conn.commit()
    conn.close()

init_db()

@app.route("/")
def home(): return render_template("index.html")

@app.route("/get_chats")
def get_chats():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    chats = conn.execute('SELECT * FROM chats ORDER BY id DESC').fetchall()
    conn.close()
    return jsonify([dict(c) for c in chats])

@app.route("/get_chat/<int:chat_id>")
def get_chat(chat_id):
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    msgs = conn.execute('SELECT * FROM messages WHERE chat_id = ? ORDER BY id ASC', (chat_id,)).fetchall()
    conn.close()
    return jsonify([dict(m) for m in msgs])

@app.route("/rename_chat/<int:chat_id>", methods=["POST"])
def rename_chat(chat_id):
    new_title = request.json.get("title")
    conn = sqlite3.connect('database.db')
    conn.execute('UPDATE chats SET title = ? WHERE id = ?', (new_title, chat_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/delete_chat/<int:chat_id>", methods=["DELETE"])
def delete_chat(chat_id):
    conn = sqlite3.connect('database.db')
    conn.execute('DELETE FROM chats WHERE id = ?', (chat_id,))
    conn.execute('DELETE FROM messages WHERE chat_id = ?', (chat_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/chat", methods=["POST"])
def chat():
    data = request.json
    prompt = data.get("message")
    chat_id = data.get("chat_id")
    
    today = datetime.now().strftime("%A, %B %d, %Y")
    full_prompt = f"System: Knowledge Cutoff ignored. Today is {today}. Context: Paul Somendra. Always provide info relative to this date.\n\nUser: {prompt}"

    if chat_id != "temp":
        conn = sqlite3.connect('database.db')
        if not chat_id:
            cursor = conn.execute('INSERT INTO chats (title) VALUES (?)', (prompt[:25],))
            chat_id = cursor.lastrowid
        conn.execute('INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)', (chat_id, 'User', prompt))
        conn.commit()
        conn.close()

    try:
        client = OpenAI(api_key=os.getenv("OPENROUTER_KEY"), base_url="https://openrouter.ai/api/v1")
        res = client.chat.completions.create(
            model="google/gemma-3-27b-it:free",
            messages=[{"role": "user", "content": full_prompt}]
        )
        ans = res.choices[0].message.content
    except: ans = "Connection failed. Check API key."

    if chat_id != "temp":
        conn = sqlite3.connect('database.db')
        conn.execute('INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)', (chat_id, 'One AI', ans))
        conn.commit()
        conn.close()

    return jsonify({"master_answer": ans, "chat_id": chat_id})

if __name__ == "__main__":
    app.run(debug=True, port=5000)