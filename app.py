import sqlite3
import random
import logging
import os
import secrets
from dotenv import load_dotenv
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, jsonify
from PIL import Image, ImageOps, ExifTags

load_dotenv()
app = Flask(__name__)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=36500)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
app.secret_key = os.environ.get('SECRET_KEY', 'default_fallback_key')


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'my_database.db')
LOG_PATH = os.path.join(BASE_DIR, 'errors.log')
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
DELETED_FOLDER = os.path.join(UPLOAD_FOLDER, 'deleted')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            message TEXT,
            created_at TEXT,
            image_path TEXT,
            exif_data TEXT,
            status TEXT,
            parent_id INTEGER,
            user_id INTEGER
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password_hash TEXT
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
file_handler = logging.FileHandler(LOG_PATH, encoding='utf-8')
file_handler.setLevel(logging.ERROR)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
file_handler.setFormatter(formatter)
app.logger.handlers.clear()
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.ERROR)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def fix_orientation(file_path):
    try:
        with Image.open(file_path) as img:
            img = ImageOps.exif_transpose(img)
            img.save(file_path)
    except Exception as e:
        app.logger.error(f"Ошибка при исправлении ориентации: {e}")

def extract_exif(image_path):
    allowed_tags = ['DateTime', 'Model', 'Make', 'GPSInfo', 'ImageWidth', 'ImageLength', 
                    'DateTimeOriginal', 'Software', 'OffsetTime', 'OffsetTimeOriginal', 'ImageUniqueID']
    ai_markers = []
    
    try:
        if not os.path.exists(image_path) or os.path.getsize(image_path) == 0:
            return "Файл пуст или отсутствует"
            
        with open(image_path, 'rb') as f:
            file_bytes = f.read()
            
        file_content_lower = file_bytes.lower()
        
        if b"c2pa" in file_content_lower or b"dcterms:provenance" in file_content_lower:
            ai_markers.append("ИИ-подпись: Найдена цифровая метка C2PA (Content Credentials)")
        if b"google:ai" in file_content_lower or (b"google" in file_content_lower and b"generative" in file_content_lower):
            ai_markers.append("Генератор: Google (Gemini / Imagen)")
        if b"dall-e" in file_content_lower or b"openai" in file_content_lower:
            ai_markers.append("Генератор: OpenAI (DALL-E)")
        if b"midjourney" in file_content_lower:
            ai_markers.append("Генератор: Midjourney")
        if b"adobe_firefly" in file_content_lower or b"firefly" in file_content_lower:
            ai_markers.append("Генератор: Adobe Firefly")

        extracted = []
        with Image.open(image_path) as img:
            exif_data = img._getexif()
            if exif_data:
                for tag, value in exif_data.items():
                    tag_name = ExifTags.TAGS.get(tag, tag)
                    
                    if tag_name in ['UserComment', 'ImageDescription'] and value:
                        val_str = str(value).strip()
                        if "steps:" in val_str.lower() or "sampler:" in val_str.lower():
                            ai_markers.append("Генератор: Stable Diffusion (Параметры генерации)")
                    
                    if tag_name in allowed_tags:
                        val_str = str(value)[:200]
                        extracted.append(f"{tag_name}: {val_str}")
                        
        if ai_markers:
            ai_info = " | ".join(list(set(ai_markers)))
            if extracted:
                return f"⚠️ {ai_info} -- [Камера]: " + ", ".join(extracted)
            return f"⚠️ {ai_info}"
            
        return ", ".join(extracted) if extracted else "Метаданные отсутствуют"
        
    except Exception as e:
        app.logger.error(f"Не удалось прочитать метаданные: {e}")
        return "Не удалось прочитать метаданные"

def clean_image(image_path):
    try:
        with Image.open(image_path) as img:
            img.seek(0)
            clean_img = img.copy()
            clean_img.save(image_path, img.format, exif=b"")
            
    except Exception as e:
        app.logger.error(f"Ошибка при очистке: {e}")

@app.route('/')
def index():
    num1, num2 = random.randint(1, 9), random.randint(1, 9)
    session['captcha_result'] = num1 + num2
    main_comments, replies = [], {}
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, message, created_at, image_path FROM comments WHERE status='active' AND parent_id IS NULL ORDER BY id DESC")
        main_comments = cursor.fetchall()
        cursor.execute("SELECT id, username, message, created_at, image_path, parent_id FROM comments WHERE status='active' AND parent_id IS NOT NULL ORDER BY id ASC")
        replies_list = cursor.fetchall()
        for r in replies_list:
            p_id = r[5]
            if p_id not in replies: replies[p_id] = []
            replies[p_id].append(r)
        conn.close()
    except Exception as e:
        app.logger.error(f"Ошибка при чтении из БД: {e}")
    captcha_error = session.pop('captcha_error', None)
    return render_template('index.html', main_comments=main_comments, replies=replies, num1=num1, num2=num2, captcha_error=captcha_error)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)
@app.route('/send', methods=['POST'])
def send_message():
    current_user_id = session.get('user_id')
    
    if 'username' in session:
        username = session['username']
    else:
        if 'anon_username' not in session:
            anon_number = random.randint(1000, 9999)
            session['anon_username'] = f"Анон#{anon_number}"
        
        username = session['anon_username']
    
    message = request.form.get('message', '').strip()
    file = request.files.get('image')
    
    filename_to_save = None
    exif_data_str = "Нет файла"
    
    if file and file.filename != '' and allowed_file(file.filename):
        try:
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename_to_save = f"img_{secrets.token_hex(8)}.{ext}"
            
            if not os.path.exists(UPLOAD_FOLDER):
                os.makedirs(UPLOAD_FOLDER)
                
            full_path = os.path.join(UPLOAD_FOLDER, filename_to_save)
            file.save(full_path)
            
            exif_data_str = extract_exif(full_path)
            fix_orientation(full_path)
            clean_image(full_path)
        except Exception as e:
            app.logger.error(f"Ошибка при сохранении файла: {e}")

    if message or filename_to_save:
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO comments 
                              (username, message, created_at, image_path, exif_data, status, user_id) 
                              VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                           (username, message, datetime.now().strftime('%d.%m.%Y %H:%M'), 
                            filename_to_save, exif_data_str, 'active', current_user_id))
            conn.commit()
            conn.close()
        except Exception as e:
            app.logger.error(f"Ошибка записи в БД: {e}")
            
    return redirect(url_for('index'))

@app.route('/admin', methods=['GET', 'POST'])
def admin_panel():
    if request.method == 'POST':
        input_password = request.form.get('password', '')
        
        if ADMIN_PASSWORD_HASH and check_password_hash(ADMIN_PASSWORD_HASH, input_password):
            session['admin_logged_in'] = True
            session.permanent = True
            session['admin_current_tab'] = 'active-tab'
    password_correct = session.get('admin_logged_in', False)
    active_comments, deleted_comments = [], []
    if password_correct:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, message, created_at, image_path, exif_data, user_id FROM comments WHERE status='active' ORDER BY id DESC")
        active_comments = cursor.fetchall()
        cursor.execute("SELECT id, username, message, created_at, image_path, exif_data, user_id FROM comments WHERE status='deleted' ORDER BY id DESC")
        deleted_comments = cursor.fetchall()
        conn.close()
    return render_template('admin.html', active_comments=active_comments, deleted_comments=deleted_comments, password_correct=password_correct)

@app.route('/admin/set_tab', methods=['POST'])
def admin_set_tab():
    if session.get('admin_logged_in'):
        session['admin_current_tab'] = request.json.get('tab', 'active-tab')
    return jsonify({'status': 'ok'})

@app.route('/admin/reply/<int:comment_id>', methods=['POST'])
def admin_reply(comment_id):
    if session.get('admin_logged_in'):
        reply_text = request.form.get('reply_text', '').strip()
        if reply_text:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO comments (username, message, created_at, image_path, exif_data, status, parent_id, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', 
                           ('Админ', reply_text, datetime.now().strftime('%d.%m.%Y %H:%M'), None, 'Нет файла', 'active', comment_id, None))
            conn.commit()
            conn.close()
            session['admin_current_tab'] = 'active-tab'
    return redirect('/admin')

@app.route('/trash/<int:comment_id>', methods=['POST'])
def trash_comment(comment_id):
    if session.get('admin_logged_in'):
        try:
            deleted_folder = os.path.join(UPLOAD_FOLDER, 'deleted')
            if not os.path.exists(deleted_folder):
                os.makedirs(deleted_folder)

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT image_path FROM comments WHERE id=?", (comment_id,))
            row = cursor.fetchone()
            
            if row and row[0]:
                filename = os.path.basename(row[0])
                source_path = os.path.join(UPLOAD_FOLDER, filename)
                target_path = os.path.join(deleted_folder, filename)
                
                if os.path.exists(source_path):
                    os.rename(source_path, target_path)

            cursor.execute("UPDATE comments SET status='deleted' WHERE id=?", (comment_id,))
            conn.commit()
            conn.close()
            session['admin_current_tab'] = 'active-tab'
        except Exception as e:
            app.logger.error(f"Ошибка при переносе в корзину: {e}")
    return redirect('/admin')

@app.route('/admin/edit/<int:comment_id>', methods=['POST'])
def edit_comment(comment_id):
    if session.get('admin_logged_in'):
        new_text = request.form.get('edit_text', '').strip()
        if new_text:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("UPDATE comments SET message=? WHERE id=?", (new_text, comment_id))
            conn.commit()
            conn.close()
            session['admin_current_tab'] = 'active-tab'
    return redirect('/admin')
@app.route('/restore/<int:comment_id>', methods=['POST'])
def restore_comment(comment_id):
    if session.get('admin_logged_in'):
        try:
            deleted_folder = os.path.join(UPLOAD_FOLDER, 'deleted')
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT image_path FROM comments WHERE id=?", (comment_id,))
            row = cursor.fetchone()
            
            if row and row[0]:
                filename = os.path.basename(row[0])
                source_path = os.path.join(deleted_folder, filename)
                target_path = os.path.join(UPLOAD_FOLDER, filename)
               
                if os.path.exists(source_path):
                    os.rename(source_path, target_path)
                    
            cursor.execute("UPDATE comments SET status='active' WHERE id=?", (comment_id,))
            conn.commit()
            conn.close()
            session['admin_current_tab'] = 'deleted-tab'
        except Exception as e:
            app.logger.error(f"Ошибка при восстановлении файла из корзины: {e}")
    return redirect('/admin')

@app.route('/delete/<int:comment_id>', methods=['POST'])
def delete_comment_complete(comment_id):
    if session.get('admin_logged_in'):
        try:
            deleted_folder = os.path.join(UPLOAD_FOLDER, 'deleted')
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT image_path FROM comments WHERE id=?", (comment_id,))
            row = cursor.fetchone()
            
            if row and row[0]:
                filename = os.path.basename(row[0])
                file_in_trash = os.path.join(deleted_folder, filename)
                file_in_uploads = os.path.join(UPLOAD_FOLDER, filename)
                
                if os.path.exists(file_in_trash): os.remove(file_in_trash)
                if os.path.exists(file_in_uploads): os.remove(file_in_uploads)
                    
            cursor.execute("DELETE FROM comments WHERE id=?", (comment_id,))
            conn.commit()
            conn.close()
            session['admin_current_tab'] = 'deleted-tab'
        except Exception as e:
            app.logger.error(f"Ошибка при окончательном удалении: {e}")
    return redirect('/admin')


@app.route('/admin/clear_trash', methods=['POST'])
def clear_trash():
    if session.get('admin_logged_in'):
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM comments WHERE status='deleted'")
            conn.commit()
            conn.close()
            
            if os.path.exists(DELETED_FOLDER):
                for filename in os.listdir(DELETED_FOLDER):
                    file_path = os.path.join(DELETED_FOLDER, filename)
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        
            session['admin_current_tab'] = 'deleted-tab'
        except Exception as e:
            app.logger.error(f"Ошибка при полной очистке корзины: {e}")
    return redirect('/admin')

@app.route('/admin/clear_log', methods=['POST'])
def clear_log():
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        log_file_path = os.path.join(base_dir, 'errors.log')
        if os.path.exists(log_file_path):
            with open(log_file_path, 'w', encoding='utf-8') as f:
                f.truncate(0)
        return redirect('/admin')
    except Exception as e:
        return f"Не удалось очистить лог: {str(e)}", 500

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_current_tab', None)
    return redirect('/admin')

@app.route('/profile')
def profile():
    return render_template('profile.html')

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')
    
    if not username or not password:
        return redirect('/profile')
        
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, password_hash FROM users WHERE username=?", (username,))
    user = cursor.fetchone()
    
    if user:
        if check_password_hash(user[2], password):
            session.permanent = True
            session['user_id'] = user[0]
            session['username'] = user[1]
    else:
        hashed = generate_password_hash(password)
        cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (username, hashed))
        conn.commit()
        new_user_id = cursor.lastrowid
        session.permanent = True
        session['user_id'] = new_user_id
        session['username'] = username
        
    conn.close()
    return redirect('/profile')

@app.route('/update_username', methods=['POST'])
def update_username():
    if 'user_id' not in session:
        return redirect('/profile')
    
    new_name = request.form.get('new_username', '').strip()
    if new_name:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE users SET username=? WHERE id=?", (new_name, session['user_id']))
            cursor.execute("UPDATE comments SET username=? WHERE username=?", (new_name, session['username']))
            conn.commit()
            session['username'] = new_name
        except Exception as e:
            app.logger.error(f"Ошибка смены ника: {e}")
        conn.close()
    return redirect('/profile')

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, threaded=True, debug=False)
