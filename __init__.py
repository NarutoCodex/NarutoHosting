import os, sqlite3, zipfile, subprocess, signal, shutil, psutil, time, datetime, random, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
# Global process tracker
running_procs = {}
start_times = {}

# OTP store: {email: {'otp': '123456', 'expires': timestamp}}
otp_store = {}

def get_db():
    storage_dir = os.environ.get('STORAGE_PATH', os.path.join(os.getcwd(), 'storage'))
    db_path = os.path.join(storage_dir, 'nehost.db')
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    storage_dir = os.environ.get('STORAGE_PATH', os.path.join(os.getcwd(), 'storage'))
    if not os.path.exists(storage_dir): os.makedirs(storage_dir)
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        fname TEXT, lname TEXT, username TEXT, email TEXT, password TEXT, pfp TEXT DEFAULT 'default.png',
        role TEXT DEFAULT 'free', 
        status TEXT DEFAULT 'active',
        server_limit INTEGER DEFAULT 1,
        notifications TEXT DEFAULT '',
        expiry_date TEXT DEFAULT NULL
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        user_id INTEGER, name TEXT, folder TEXT, 
        status TEXT, startup TEXT, pid INTEGER,
        server_status TEXT DEFAULT 'active'
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, subject TEXT, message TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS admin_settings (
        id INTEGER PRIMARY KEY, 
        username TEXT, password TEXT,
        popup_title TEXT, popup_msg TEXT, popup_img TEXT, show_popup INTEGER DEFAULT 0
    )''')

    # FORCE set admin credentials every startup
    existing = db.execute('SELECT id FROM admin_settings WHERE id=1').fetchone()
    if existing:
        db.execute('UPDATE admin_settings SET username=?, password=? WHERE id=1',
                   ('narutohost@gamil.com', 'narrutocodex'))
    else:
        db.execute('INSERT INTO admin_settings (id, username, password) VALUES (1, ?, ?)',
                   ('narutohost@gamil.com', 'narrutocodex'))

    # Ensure expiry_date column exists
    cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if 'expiry_date' not in cols:
        db.execute('ALTER TABLE users ADD COLUMN expiry_date TEXT DEFAULT NULL')

    db.commit()
    db.close()

def check_expired_users():
    """Auto-suspend servers of expired users"""
    db = get_db()
    today = datetime.date.today().isoformat()
    # Check if expiry_date column exists first
    cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
    if 'expiry_date' not in cols:
        db.close()
        return
    expired_users = db.execute(
        "SELECT id FROM users WHERE expiry_date IS NOT NULL AND expiry_date <= ? AND status='active'", 
        (today,)
    ).fetchall()
    for u in expired_users:
        # Suspend all servers
        db.execute("UPDATE servers SET server_status='suspended' WHERE user_id=?", (u['id'],))
        # Stop running processes
        srvs = db.execute("SELECT folder, pid FROM servers WHERE user_id=?", (u['id'],)).fetchall()
        for s in srvs:
            folder = s['folder']
            pid = s['pid']
            if folder in running_procs:
                try: os.killpg(os.getpgid(running_procs[folder].pid), signal.SIGKILL)
                except: pass
                del running_procs[folder]
            elif pid and psutil.pid_exists(pid):
                try: os.killpg(os.getpgid(pid), signal.SIGKILL)
                except: pass
    db.commit()
    db.close()

def send_otp_email(to_email, otp):
    try:
        smtp_host = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
        smtp_port = int(os.environ.get('SMTP_PORT', '587'))
        smtp_user = os.environ.get('SMTP_USER', 'narrutohost@gmail.com')
        smtp_pass = os.environ.get('SMTP_PASS', '#gyasuddin##')

        msg = MIMEMultipart('alternative')
        msg['Subject'] = '🔐 NARUTO HOST - Password Reset OTP'
        msg['From'] = f'NARUTO HOST <{smtp_user}>'
        msg['To'] = to_email

        html = f\'\'\'
        <div style="font-family:sans-serif;max-width:480px;margin:auto;background:#111;border-radius:20px;padding:35px;text-align:center;color:#fff;">
          <div style="font-size:40px;margin-bottom:15px;">⚡</div>
          <h2 style="font-weight:800;font-size:22px;margin-bottom:8px;">NARUTO HOST</h2>
          <p style="color:#888;font-size:14px;margin-bottom:25px;">Password Reset Request</p>
          <div style="background:#1c1c1e;border-radius:16px;padding:25px;margin-bottom:20px;">
            <p style="color:#888;font-size:12px;margin-bottom:10px;text-transform:uppercase;font-weight:700;">Your OTP</p>
            <div style="font-size:38px;font-weight:900;letter-spacing:12px;color:#af52de;">{otp}</div>
            <p style="color:#555;font-size:11px;margin-top:10px;">Valid for 10 minutes</p>
          </div>
          <p style="color:#555;font-size:12px;">If you didn\'t request this, ignore this email.</p>
        </div>
        \'\'\'
        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'nehost_naruto_2024_xk99'
    app.config['SESSION_PERMANENT'] = True
    app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = False
    storage_root = os.environ.get('STORAGE_PATH', os.path.join(os.getcwd(), 'storage'))
    app.config['BASE_STORAGE'] = os.path.join(storage_root, 'instances')
    app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static', 'uploads')
    
    if not os.path.exists(app.config['BASE_STORAGE']):
        os.makedirs(app.config['BASE_STORAGE'])
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
        
    init_db()

    def get_precise_uptime(start_timestamp):
        if not start_timestamp: return "Offline"
        diff = int(time.time() - start_timestamp)
        months, rem = divmod(diff, 2592000)
        days, rem = divmod(rem, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        
        parts = []
        if months > 0: parts.append(f"{months}mo")
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)
    
    @app.route('/')
    def home():
      return render_template('index.html')

    # --- FORGOT PASSWORD ROUTES ---
    @app.route('/forgot-password')
    def forgot_password_page():
        return render_template('web/forgot_password.html')

    @app.route('/forgot-password/send-otp', methods=['POST'])
    def send_otp():
        email = request.json.get('email', '').strip().lower()
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE LOWER(email)=?', (email,)).fetchone()
        db.close()
        if not user:
            return jsonify({'status': 'error', 'msg': 'No account with this email!'})

        # Generate 6-digit OTP
        otp = str(random.randint(100000, 999999))
        otp_store[email] = {'otp': otp, 'expires': time.time() + 600}  # 10 min

        # Send email
        sent = send_otp_email(email, otp)
        if sent:
            return jsonify({'status': 'success'})
        else:
            return jsonify({'status': 'error', 'msg': 'Email send failed! Check server config.'})

    @app.route('/forgot-password/verify-otp', methods=['POST'])
    def verify_otp():
        d = request.json
        email = d.get('email', '').strip().lower()
        otp = d.get('otp', '').strip()

        stored = otp_store.get(email)
        if not stored:
            return jsonify({'status': 'error', 'msg': 'OTP expired! Request again.'})
        if time.time() > stored['expires']:
            del otp_store[email]
            return jsonify({'status': 'error', 'msg': 'OTP expired! Request again.'})
        if stored['otp'] != otp:
            return jsonify({'status': 'error', 'msg': 'Wrong OTP! Try again.'})

        # Mark OTP as verified
        otp_store[email]['verified'] = True
        return jsonify({'status': 'success'})

    @app.route('/forgot-password/reset', methods=['POST'])
    def reset_password():
        d = request.json
        email = d.get('email', '').strip().lower()
        new_pass = d.get('password', '').strip()

        stored = otp_store.get(email)
        if not stored or not stored.get('verified'):
            return jsonify({'status': 'error', 'msg': 'Not verified!'})
        if len(new_pass) < 6:
            return jsonify({'status': 'error', 'msg': 'Password too short!'})

        db = get_db()
        db.execute('UPDATE users SET password=? WHERE LOWER(email)=?', (new_pass, email))
        db.commit()
        db.close()

        # Clear OTP
        del otp_store[email]
        return jsonify({'status': 'success'})

    # --- SIGNUP DISABLED - Normal users cannot self-register ---
    @app.route('/signup', methods=['GET', 'POST'])
    def signup():
        # Redirect to login - signup not allowed for normal users
        return redirect(url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form.get('email')
            pwd = request.form.get('password')
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE (email=? OR username=?) AND password=?', (email, email, pwd)).fetchone()
            db.close()
            
            if user:
                if user['status'] == 'banned':
                    return jsonify({'status': 'banned', 'msg': 'Your account is suspended!'}), 403
                
                # Check expiry
                if user['expiry_date']:
                    today = datetime.date.today().isoformat()
                    if user['expiry_date'] <= today:
                        return jsonify({'status': 'error', 'msg': 'Your account has expired! Contact admin.'}), 403
                
                session.permanent = True
                session['user_id'] = user['id']
                return jsonify({'status': 'success', 'url': url_for('dashboard')}), 200
            else:
                return jsonify({'status': 'error', 'msg': 'Invalid credentials!'}), 401
        return render_template('web/login.html')

    @app.route('/dashboard')
    def dashboard():
        if 'user_id' not in session: return redirect(url_for('login'))
        
        # Check expiry on every dashboard load
        check_expired_users()
        
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        db.close()
        if not user or user['status'] != 'active':
            session.clear()
            return redirect(url_for('login'))
        return render_template('web/dashboard.html', user=user)

    @app.route('/profile/update', methods=['POST'])
    def update_profile():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        uid = session['user_id']
        fname = request.form.get('fname')
        lname = request.form.get('lname')
        pwd = request.form.get('password')
        db = get_db()
        if pwd:
            db.execute('UPDATE users SET fname=?, lname=?, password=? WHERE id=?', (fname, lname, pwd, uid))
        else:
            db.execute('UPDATE users SET fname=?, lname=? WHERE id=?', (fname, lname, uid))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/ticket/create', methods=['POST'])
    def create_ticket():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('INSERT INTO tickets (user_id, subject, message) VALUES (?,?,?)', (session['user_id'], d['subject'], d['message']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/api/announcement')
    def get_announcement():
        db = get_db()
        conf = db.execute('SELECT popup_title, popup_msg, popup_img, show_popup FROM admin_settings WHERE id=1').fetchone()
        db.close()
        return jsonify(dict(conf))

    # --- ADMIN LOGIN ---
    @app.route('/admin')
    def admin_redirect():
        return redirect(url_for('admin_login'))

    @app.route('/admin-login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'POST':
            user = (request.form.get('username') or '').strip()
            pwd = (request.form.get('password') or '').strip()
            db = get_db()
            admin = db.execute('SELECT * FROM admin_settings WHERE username=? AND password=?', (user, pwd)).fetchone()
            db.close()
            if admin:
                session.permanent = True
                session['admin_logged'] = True
                session.modified = True
                resp = redirect(url_for('admin_panel'))
                resp.set_cookie('admin_auth', 'naruto_admin_ok', max_age=86400*30, httponly=False)
                return resp
            else:
                return render_template('web/admin_login.html', error=True)
        return render_template('web/admin_login.html')

    @app.route('/admin/panel')
    def admin_panel():
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return redirect(url_for('admin_login'))
        return render_template('web/admin_panel.html')

    @app.route('/admin/stats')
    def admin_stats():
        # Check session OR cookie
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'users': [], 'sys_cpu': '0%', 'sys_ram': '0%'})
        check_expired_users()
        db = get_db()
        users = db.execute('SELECT * FROM users').fetchall()
        user_list = []
        try:
            total_cpu = psutil.cpu_percent()
        except:
            total_cpu = 0
        try:
            total_ram = psutil.virtual_memory().percent
        except:
            total_ram = 0
        today = datetime.date.today().isoformat()
        for u in users:
            srvs = db.execute('SELECT * FROM servers WHERE user_id=?', (u['id'],)).fetchall()
            active_srvs = 0
            for s in srvs:
                is_on = False
                try:
                    if s['pid'] and psutil.pid_exists(s['pid']):
                        proc = psutil.Process(s['pid'])
                        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                            is_on = True
                    elif s['folder'] in running_procs and running_procs[s['folder']].poll() is None:
                        is_on = True
                except: pass
                if is_on: active_srvs += 1
            
            # Calculate days left
            days_left = None
            is_expired = False
            try:
                expiry = u['expiry_date']
            except (IndexError, KeyError):
                expiry = None
            if expiry:
                try:
                    exp = datetime.date.fromisoformat(expiry)
                    diff = (exp - datetime.date.today()).days
                    days_left = diff
                    is_expired = diff <= 0
                except: pass
            
            # Build server list for admin panel
            srv_list = []
            for s in srvs:
                s_online = False
                try:
                    if s['pid'] and psutil.pid_exists(s['pid']):
                        proc = psutil.Process(s['pid'])
                        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                            s_online = True
                    elif s['folder'] in running_procs and running_procs[s['folder']].poll() is None:
                        s_online = True
                except: pass
                srv_list.append({
                    'id': s['id'], 'name': s['name'], 'folder': s['folder'],
                    'online': s_online, 'status': s['server_status']
                })

            try:
                expiry_val = u['expiry_date']
            except (IndexError, KeyError):
                expiry_val = None
            user_list.append({
                'id': u['id'], 'fname': u['fname'] or '', 'email': u['email'] or '',
                'srv_count': len(srvs), 'active_srvs': active_srvs,
                'status': u['status'] or 'active', 'role': u['role'] or 'free',
                'server_limit': u['server_limit'] or 1,
                'expiry_date': expiry_val, 'days_left': days_left, 'is_expired': is_expired,
                'servers': srv_list
            })
        db.close()
        return jsonify({'users': user_list, 'sys_cpu': f"{total_cpu}%", 'sys_ram': f"{total_ram}%"})

    @app.route('/admin/user/update', methods=['POST'])
    def update_user():
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status':'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET role=?, status=?, server_limit=? WHERE id=?', (d['role'], d['status'], d['limit'], d['user_id']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/set-popup', methods=['POST'])
    def set_popup():
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status':'error'})
        title, msg, show = request.form.get('title'), request.form.get('msg'), request.form.get('show')
        img = request.files.get('image')
        db = get_db()
        old_data = db.execute('SELECT popup_img FROM admin_settings WHERE id=1').fetchone()
        img_name = old_data['popup_img'] if old_data else None
        if img:
            img_name = secure_filename(img.filename)
            img.save(os.path.join(app.config['UPLOAD_FOLDER'], img_name))
        show_val = 1 if show == 'true' else 0
        db.execute('UPDATE admin_settings SET popup_title=?, popup_msg=?, popup_img=?, show_popup=? WHERE id=1', 
                   (title or '', msg or '', img_name, show_val))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/send-warning', methods=['POST'])
    def send_warning():
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET notifications=? WHERE id=?', (d['message'], d['user_id']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/login-as/<int:uid>')
    def login_as(uid):
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return redirect(url_for('admin_login'))
        session['user_id'] = uid
        return redirect(url_for('dashboard'))

    @app.route('/admin/manage-user/<int:uid>')
    def admin_manage_user_servers(uid):
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return redirect(url_for('admin_login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (uid,)).fetchall()
        db.close()
        servers = []
        for r in rows:
            f = r['folder']
            online = (f in running_procs and running_procs[f].poll() is None) or (r['pid'] and psutil.pid_exists(r['pid']))
            servers.append({'id': r['id'], 'name': r['name'], 'folder': f, 'online': online, 'status': r['server_status']})
        return render_template('web/admin_manage_user.html', user=user, servers=servers)

    @app.route('/admin/suspend-server/<int:sid>', methods=['POST'])
    def admin_suspend_server(sid):
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status': 'error'})
        status = request.json.get('status')
        db = get_db()
        # If suspending, also stop the server
        if status == 'suspended':
            srv = db.execute('SELECT folder, pid FROM servers WHERE id=?', (sid,)).fetchone()
            if srv:
                folder = srv['folder']
                pid = srv['pid']
                if folder in running_procs:
                    try: os.killpg(os.getpgid(running_procs[folder].pid), signal.SIGKILL)
                    except: pass
                    del running_procs[folder]
                elif pid and psutil.pid_exists(pid):
                    try: os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except: pass
        db.execute('UPDATE servers SET server_status=? WHERE id=?', (status, sid))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-server/<int:sid>', methods=['POST'])
    def admin_delete_server(sid):
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status': 'error'})
        db = get_db()
        srv = db.execute('SELECT folder FROM servers WHERE id=?', (sid,)).fetchone()
        if srv:
            folder = srv['folder']
            if folder in running_procs:
                try: os.killpg(os.getpgid(running_procs[folder].pid), signal.SIGKILL)
                except: pass
                del running_procs[folder]
            db.execute('DELETE FROM servers WHERE id=?', (sid,))
            db.commit()
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path): shutil.rmtree(path)
            db.close()
            return jsonify({'status': 'deleted'})
        db.close()
        return jsonify({'status': 'error', 'msg': 'Server not found'})

    # --- ADMIN CREATE SERVER FOR USER ---
    @app.route('/admin/create-server', methods=['POST'])
    def admin_create_server():
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status': 'error'})
        d = request.json
        uid = d.get('user_id')
        name = d.get('name', 'server')
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not user:
            db.close()
            return jsonify({'status': 'error', 'msg': 'User not found'})
        folder = secure_filename(name).lower() + '_' + str(int(time.time()))
        db.execute('INSERT INTO servers (user_id, name, folder, status, startup) VALUES (?,?,?,?,?)',
                   (uid, name, folder, 'Offline', 'main.py'))
        db.commit()
        db.close()
        os.makedirs(os.path.join(app.config['BASE_STORAGE'], folder), exist_ok=True)
        return jsonify({'status': 'success'})

    # --- ADMIN CREATE USER with expiry days ---
    @app.route('/admin/create-user', methods=['POST'])
    def admin_create_user():
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        limit = d.get('limit', 1)
        days = d.get('days', None)
        
        expiry_date = None
        if days and int(days) > 0:
            expiry_date = (datetime.date.today() + datetime.timedelta(days=int(days))).isoformat()
        
        db.execute(
            'INSERT INTO users (fname, email, password, server_limit, expiry_date) VALUES (?,?,?,?,?)', 
            (d['name'], d['email'], d['pass'], limit, expiry_date)
        )
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    # --- EXTEND USER EXPIRY ---
    @app.route('/admin/extend-expiry', methods=['POST'])
    def admin_extend_expiry():
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status': 'error'})
        d = request.json
        uid = d.get('user_id')
        days = int(d.get('days', 0))
        if days <= 0: return jsonify({'status': 'error', 'msg': 'Invalid days'})
        db = get_db()
        user = db.execute('SELECT expiry_date FROM users WHERE id=?', (uid,)).fetchone()
        if user:
            current_expiry = user['expiry_date']
            if current_expiry:
                try:
                    base = datetime.date.fromisoformat(current_expiry)
                    # If already expired, extend from today
                    if base < datetime.date.today():
                        base = datetime.date.today()
                except:
                    base = datetime.date.today()
            else:
                base = datetime.date.today()
            new_expiry = (base + datetime.timedelta(days=days)).isoformat()
            db.execute('UPDATE users SET expiry_date=? WHERE id=?', (new_expiry, uid))
            db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-user/<int:uid>', methods=['POST'])
    def delete_user(uid):
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status': 'error'})
        db = get_db()
        srvs = db.execute('SELECT folder FROM servers WHERE user_id=?', (uid,)).fetchall()
        for s in srvs:
            path = os.path.join(app.config['BASE_STORAGE'], s['folder'])
            if os.path.exists(path): shutil.rmtree(path)
        db.execute('DELETE FROM servers WHERE user_id=?', (uid,))
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        db.commit()
        db.close()
        return jsonify({'status': 'deleted'})

    # --- ADMIN CHANGE CREDENTIALS ---
    @app.route('/admin/change-credentials', methods=['POST'])
    def admin_change_credentials():
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return jsonify({'status': 'error'})
        d = request.json
        new_email = d.get('email')
        new_pass = d.get('password')
        db = get_db()
        db.execute('UPDATE admin_settings SET username=?, password=? WHERE id=1', (new_email, new_pass))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})
        
    @app.route('/admin/files/<folder>')
    def admin_browse_files(folder):
        admin_ok = session.get('admin_logged') or request.cookies.get('admin_auth') == 'naruto_admin_ok'
        if not admin_ok: return redirect(url_for('admin_login'))
        return render_template('web/dashboard.html', user={'fname': 'Admin'}, is_admin_view=True, admin_folder=folder)

    @app.route('/files/list/<folder>')
    def flist(folder):
        sub_path = request.args.get('path', '')
        full_path = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path))
        if not full_path.startswith(app.config['BASE_STORAGE']): return jsonify([])
        if not os.path.exists(full_path): return jsonify([])
        items = []
        for f in sorted(os.listdir(full_path)):
            if f == 'console.log': continue
            p = os.path.join(full_path, f)
            items.append({'name': f, 'is_dir': os.path.isdir(p), 'is_zip': f.lower().endswith('.zip'), 'rel_path': os.path.join(sub_path, f)})
        return jsonify(items)

    @app.route('/files/content/<folder>/<name>')
    def fcontent(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name)
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f: return jsonify({'content': f.read()})
        except: return jsonify({'content': 'Error reading file'})

    @app.route('/files/save/<folder>/<name>', methods=['POST'])
    def fsave(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name)
        try:
            with open(p, 'w', encoding='utf-8') as f: f.write(request.json.get('content'))
            return jsonify({'status': 'saved'})
        except: return jsonify({'status': 'error'})

    @app.route('/files/delete-bulk/<folder>', methods=['POST'])
    def delete_bulk(folder):
        d = request.json
        sub_path, names = d.get('path', ''), d.get('names', [])
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        for name in names:
            p = os.path.join(base, name)
            if name == 'console.log': continue
            try:
                if os.path.isdir(p): shutil.rmtree(p)
                elif os.path.exists(p): os.remove(p)
            except: pass
        return jsonify({"status": "ok"})

    @app.route('/files/create-file/<folder>', methods=['POST'])
    def create_file(folder):
        d = request.json
        p = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''), secure_filename(d.get('name')))
        with open(p, 'w') as f: f.write("")
        return jsonify({'status': 'success'})

    @app.route('/files/create-folder/<folder>', methods=['POST'])
    def create_folder(folder):
        d = request.json
        p = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''), secure_filename(d.get('name')))
        os.makedirs(p, exist_ok=True)
        return jsonify({'status': 'success'})

    @app.route('/files/upload/<folder>', methods=['POST'])
    def upload_file(folder):
        sub_path = request.form.get('path', '')
        file = request.files['file']
        dest = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not os.path.exists(dest): os.makedirs(dest)
        file.save(os.path.join(dest, secure_filename(file.filename)))
        return jsonify({'status': 'success'})

    @app.route('/files/rename/<folder>', methods=['POST'])
    def rename_file(folder):
        d = request.json
        base = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''))
        os.rename(os.path.join(base, d['old']), os.path.join(base, d['new']))
        return jsonify({'status': 'success'})

    @app.route('/files/download/<folder>/<name>')
    def download_file(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name))
        if not p.startswith(app.config['BASE_STORAGE']): return "Access Denied", 403
        return send_file(p, as_attachment=True)

    @app.route('/files/zip-bulk/<folder>', methods=['POST'])
    def zip_bulk(folder):
        d = request.json
        names, sub_path = d.get('names', []), d.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        zip_name = f"archive_{int(time.time())}.zip"
        zip_path = os.path.join(base, zip_name)
        with zipfile.ZipFile(zip_path, 'w') as z:
            for n in names:
                p = os.path.join(base, n)
                if n == zip_name: continue
                if os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for file in files:
                            full_p = os.path.join(root, file)
                            z.write(full_p, os.path.relpath(full_p, base))
                elif os.path.exists(p): z.write(p, n)
        return jsonify({'status': 'success', 'zip': zip_name})

    @app.route('/files/unzip/<folder>', methods=['POST'])
    def unzip_file(folder):
        d = request.json
        zip_name = d.get('name')
        sub_path = d.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        zip_path = os.path.join(base, zip_name)
        if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
            try:
                with zipfile.ZipFile(zip_path, 'r') as z:
                    z.extractall(base)
                return jsonify({'status': 'success'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': str(e)})
        return jsonify({'status': 'error', 'msg': 'Invalid zip file'})

    @app.route('/server/action/<folder>/<act>', methods=['POST'])
    def server_action(folder, act):
        db = get_db()
        srv_data = db.execute('SELECT server_status FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'This server is suspended by Admin.'})

        path = os.path.join(app.config['BASE_STORAGE'], folder)
        log_file_path = os.path.join(path, 'console.log')
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        if act == 'install':
            req_path = os.path.join(path, 'requirements.txt')
            if os.path.exists(req_path):
                f_log = open(log_file_path, 'a')
                f_log.write(f"\n[{now}] 📦 Package Installation Started...\n")
                f_log.flush()
                subprocess.Popen(['pip', 'install', '-r', 'requirements.txt'], cwd=path, stdout=f_log, stderr=f_log)
                db.close()
                return jsonify({'status': 'installing'})
            db.close()
            return jsonify({'status': 'error', 'msg': 'requirements.txt missing'})

        if act in ['start', 'restart']:
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            old_pid = row['pid'] if row else None
            if folder in running_procs or (old_pid and psutil.pid_exists(old_pid)):
                try: 
                    t_pid = running_procs[folder].pid if folder in running_procs else old_pid
                    os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            srv = db.execute('SELECT startup FROM servers WHERE folder=?', (folder,)).fetchone()
            startup_file = srv['startup'] if srv and srv['startup'] else 'main.py'
            f_log = open(log_file_path, 'a')
            
            # Auto install requirements if exists
            req_path = os.path.join(path, 'requirements.txt')
            if os.path.exists(req_path):
                f_log.write(f"\n[{now}] 📦 Installing packages...\n")
                f_log.flush()
                try:
                    result = subprocess.run(
                        ['pip', 'install', '-r', 'requirements.txt', '--quiet', '--no-warn-script-location'],
                        cwd=path, capture_output=True, text=True, timeout=120
                    )
                    if result.returncode == 0:
                        f_log.write(f"[{now}] ✅ Packages installed!\n")
                    else:
                        f_log.write(f"[{now}] ⚠️ Install warning: {result.stderr[-500:]}\n")
                except subprocess.TimeoutExpired:
                    f_log.write(f"[{now}] ⚠️ Install timeout — starting anyway\n")
                except Exception as e:
                    f_log.write(f"[{now}] ⚠️ Install error: {e}\n")
                f_log.flush()

            f_log.write(f"\n[{now}] 🚀 Instance {act.upper()}ED Successfully\n")
            f_log.flush()
            proc = subprocess.Popen(['python3', startup_file], cwd=path, stdout=f_log, stderr=f_log, preexec_fn=os.setsid)
            running_procs[folder], start_times[folder] = proc, time.time()
            db.execute('UPDATE servers SET pid=? WHERE folder=?', (proc.pid, folder))
            db.commit()
            db.close()
            return jsonify({'status': 'started'})
        elif act == 'stop':
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            t_pid = running_procs[folder].pid if folder in running_procs else (row['pid'] if row else None)
            if t_pid:
                try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            if folder in running_procs: del running_procs[folder]
            db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
            db.commit()
            db.close()
            with open(log_file_path, 'a') as f: f.write(f"\n[{now}] 🛑 Instance STOPPED\n")
            return jsonify({'status': 'stopped'})
        db.close()
        return jsonify({'status': 'ok'})

    @app.route('/server/log/<folder>')
    def server_log(folder):
        path = os.path.join(app.config['BASE_STORAGE'], folder, 'console.log')
        if os.path.exists(path):
            with open(path, 'r') as f: return jsonify({'log': f.read()[-5000:]})
        return jsonify({'log': 'Waiting for logs...'})

    @app.route('/server/set-startup/<folder>', methods=['POST'])
    def set_startup(folder):
        cmd = request.json.get('file')
        db = get_db()
        db.execute('UPDATE servers SET startup=? WHERE folder=?', (cmd, folder))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/server/delete/<folder>', methods=['POST'])
    def delete_server(folder):
        db = get_db()
        srv_data = db.execute('SELECT server_status, pid FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Suspended servers cannot be deleted!'})
        t_pid = running_procs[folder].pid if folder in running_procs else (srv_data['pid'] if srv_data else None)
        if t_pid:
            try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
            except: pass
        if folder in running_procs: del running_procs[folder]
        db.execute('DELETE FROM servers WHERE folder=?', (folder,))
        db.commit()
        db.close()
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        if os.path.exists(path): shutil.rmtree(path)
        return jsonify({'status': 'deleted'})

    @app.route('/servers')
    def list_servers():
        if 'user_id' not in session: return jsonify({'servers': []})
        db = get_db()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (session['user_id'],)).fetchall()
        db.close()
        srvs = []
        for r in rows:
            f, saved_pid = r['folder'], r['pid']
            online = False
            if saved_pid and psutil.pid_exists(saved_pid):
                try:
                    p = psutil.Process(saved_pid)
                    if p.is_running() and p.status() != psutil.STATUS_ZOMBIE: online = True
                except: pass
            elif f in running_procs and running_procs[f].poll() is None: online = True
            uptime = get_precise_uptime(start_times.get(f)) if online and f in start_times else ("Online" if online else "Offline")
            cpu, ram = "0%", "0MB"
            if online:
                try:
                    p_pid = running_procs[f].pid if f in running_procs else saved_pid
                    process = psutil.Process(p_pid)
                    cpu = f"{process.cpu_percent(interval=None)}%"
                    ram = f"{process.memory_info().rss / (1024 * 1024):.1f}MB"
                except: pass
            srvs.append({'name': r['name'], 'folder': f, 'online': online, 'startup': r['startup'], 'uptime': uptime, 'cpu': cpu, 'ram': ram, 'status': r['server_status']})
        return jsonify({'servers': srvs})

    @app.route('/add', methods=['POST'])
    def add_srv():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        count = db.execute('SELECT COUNT(*) as count FROM servers WHERE user_id=?', (session['user_id'],)).fetchone()['count']
        if count >= user['server_limit']:
            db.close()
            return jsonify({'status': 'error', 'msg': f"Limit Reached! Max: {user['server_limit']}"})
        name = request.json.get('name')
        folder = secure_filename(name).lower() + "_" + str(int(time.time()))
        db.execute('INSERT INTO servers (user_id, name, folder, status, startup) VALUES (?,?,?,?,?)', (session['user_id'], name, folder, 'Offline', 'main.py'))
        db.commit()
        db.close()
        os.makedirs(os.path.join(app.config['BASE_STORAGE'], folder), exist_ok=True)
        return jsonify({'status': 'success'})

    return app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
