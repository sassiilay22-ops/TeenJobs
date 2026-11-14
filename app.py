# -*- coding: utf-8 -*-
# TeenJob – פלטפורמת עבודה לבני נוער (Flask). גרסת Production: Render + PostgreSQL (Neon) + Resend.
# הגדרות דרך משתני סביבה – ראו README.md
import os, sqlite3, time, secrets, io, re, json, hashlib, urllib.request
from datetime import timedelta
from functools import wraps
from flask import (Flask, g, request, session, redirect, render_template as _rt,
                   flash, abort)
from jinja2 import DictLoader
from werkzeug.security import generate_password_hash, check_password_hash

# ===================== הגדרות =====================
ADMIN_CODE = os.environ.get("ADMIN_CODE", "")      # קוד כניסה לדף /admin (חובה להגדיר, אחרת הדף נעול)
BRAND = os.environ.get("BRAND", "TeenJob")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "TeenJob <onboarding@resend.dev>")
DEV_MODE = not RESEND_API_KEY                      # בלי מפתח – הקוד מודפס ללוגים (לבדיקות בלבד)
DATABASE_URL = os.environ.get("DATABASE_URL", "")  # PostgreSQL (Neon). ריק = SQLite מקומי לפיתוח
PG = DATABASE_URL.startswith("postgres")
DB = os.environ.get("SQLITE_PATH", "teenjob.db")
PROD = bool(os.environ.get("RENDER") or os.environ.get("PRODUCTION"))
if PG:
    import psycopg2, psycopg2.extras
MIN_WAGE = 34.3           # שכר מינימום לשעה (בדקו ועדכנו לפי החוק העדכני)
CITIES = ["תל אביב-יפו", "ירושלים", "חיפה", "ראשון לציון", "פתח תקווה", "אשדוד", "נתניה",
          "באר שבע", "בני ברק", "רמת גן", "הרצליה", "רעננה", "כפר סבא", "מודיעין", "אור יהודה", "אחר"]
TYPES = ["בייביסיטר ועזרה לילדים", "דוג ווקר וטיפול בחיות", "שיעורים פרטיים (מתמטיקה, אנגלית...)",
         "הדרכה וחוגים", "קמעונאות וחנויות", "מסעדות וקפה – הגשה", "מטבח ופיצה", "שליחויות",
         "משרד ושירות לקוחות", "גינה ועבודות בית", "אחר"]
# גיל מינימלי באתר לסוגי עבודה רגישים (מדיניות בטיחות של הפלטפורמה – אמתו מול החוק)
TYPE_MIN_AGE = {"מטבח ופיצה": 16, "שליחויות": 16, "מסעדות וקפה – הגשה": 15, "קמעונאות וחנויות": 15}
DURS = ["קבועה", "חד-פעמית"]
STATUSES = ["התקבלה", "נצפתה", "זימון לראיון", "התקבלת!", "לא התקדמנו"]

# ===================== שכבת נתונים =====================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE, pw TEXT, name TEXT,
  age INTEGER, city TEXT, skills TEXT, experience TEXT, bio TEXT);
CREATE TABLE IF NOT EXISTS employers(user_id INTEGER PRIMARY KEY, business TEXT, city TEXT,
  about TEXT, reg_no TEXT, verified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, employer_id INTEGER, title TEXT, city TEXT,
  jtype TEXT, wage REAL, min_age INTEGER, start_h INTEGER, end_h INTEGER, days TEXT, duration TEXT,
  no_exp INTEGER, weekend INTEGER, description TEXT, requirements TEXT, wage_period TEXT DEFAULT 'hour', days_per_week INTEGER DEFAULT 3, active INTEGER DEFAULT 1,
  created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS favs(user_id INTEGER, job_id INTEGER, PRIMARY KEY(user_id, job_id));
CREATE TABLE IF NOT EXISTS apps(id INTEGER PRIMARY KEY, user_id INTEGER, job_id INTEGER, message TEXT,
  status TEXT DEFAULT 'התקבלה', created TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, job_id));
CREATE TABLE IF NOT EXISTS ratings(user_id INTEGER, job_id INTEGER, stars INTEGER, comment TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, job_id));
CREATE TABLE IF NOT EXISTS otps(uid INTEGER PRIMARY KEY, h TEXT, exp REAL, tries INTEGER DEFAULT 0, sent REAL);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, job_id INTEGER, user_id INTEGER, reason TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP);
"""
SCHEMA_PG = (SCHEMA.replace("(id INTEGER PRIMARY KEY", "(id SERIAL PRIMARY KEY")
             .replace("TEXT DEFAULT CURRENT_TIMESTAMP", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
             .replace(" REAL", " DOUBLE PRECISION"))

def init_db():
    if PG:
        c = psycopg2.connect(DATABASE_URL); c.cursor().execute(SCHEMA_PG); c.commit(); c.close()
    else:
        c = sqlite3.connect(DB); c.executescript(SCHEMA); c.commit(); c.close()
init_db()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("tj" + DATABASE_URL + DB).encode()).hexdigest()
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=PROD, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=8))

def db():
    if "db" not in g:
        if PG: g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        else: g.db = sqlite3.connect(DB); g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def _close(e):
    d = g.pop("db", None)
    if d: d.close()

def _run(sql, a=()):
    if PG:
        c = db().cursor(); c.execute(sql.replace("?", "%s").replace(" LIKE ", " ILIKE "), a); return c
    return db().execute(sql, a)

def q(sql, a=(), one=False):
    r = _run(sql, a).fetchall()
    return (r[0] if r else None) if one else r

def ex(sql, a=()):
    if PG and sql.lstrip().startswith("INSERT INTO") and sql.split()[2].split("(")[0] in ("users", "jobs", "apps", "reports"):
        c = _run(sql + " RETURNING id", a); i = c.fetchone()["id"]; db().commit(); return i
    c = _run(sql, a); db().commit(); return None if PG else c.lastrowid

JOB_SQL = """SELECT j.*, e.business, e.verified,
 (SELECT AVG(stars) FROM ratings WHERE job_id=j.id) avg,
 (SELECT COUNT(*) FROM ratings WHERE job_id=j.id) cnt
 FROM jobs j JOIN employers e ON e.user_id=j.employer_id WHERE j.active=1 """

# ===================== לוגיקה: חוקי עבודת נוער =====================
def wage_floor(age):
    f = 0.7 if age < 16 else 0.75 if age == 16 else 0.82 if age == 17 else 1
    return round(MIN_WAGE * f, 2)

def dpw_of(f):
    d = f.get("days_per_week", "")
    return min(max(int(d), 1), 6) if d.isdigit() else 3

def validate_job(f):
    err = []
    try:
        age, wage, s, e = int(f["min_age"]), float(f["wage"]), int(f["start_h"]), int(f["end_h"])
    except (ValueError, KeyError):
        return ["יש למלא מספרים תקינים בגיל, שכר ושעות."]
    if age < 14: err.append("גיל העסקה מינימלי בישראל הוא 14.")
    if not (0 <= s < e <= 24): err.append("שעות העבודה אינן תקינות.")
    elif e - s > 8: err.append("משמרת של נוער לא יכולה לעלות על 8 שעות ביום.")
    elif age < 16 and (s < 6 or e > 20): err.append("מתחת לגיל 16 אסור לעבוד בין 20:00 ל-06:00.")
    elif s < 6 or e > 22: err.append("עד גיל 18 אסור לעבוד בין 22:00 ל-06:00.")
    tmin = TYPE_MIN_AGE.get(f.get("jtype"), 14)
    if age < tmin: err.append(f"בעבודה מסוג זה הגיל המינימלי באתר הוא {tmin}.")
    hrs, floor = max(e - s, 0), wage_floor(max(age, 14))
    if f.get("wage_period") == "month":
        need_m = round(floor * hrs * dpw_of(f) * 4.33)
        if wage < need_m: err.append(f"השכר החודשי נמוך מהמינימום החוקי לשעות שהוגדרו (כ-{need_m} ₪ לחודש).")
    elif wage < floor: err.append(f"השכר נמוך מהמינימום החוקי לגיל זה (כ-{floor} ₪ לשעה).")
    if len(f.get("description", "").strip()) < 20: err.append("יש לכתוב תיאור משרה של לפחות 20 תווים.")
    return err

# ===================== עזרים =====================
def need(mode=None):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            if not session.get("uid"):
                return redirect("/login?next=" + request.path)
            if not session.get("mode"): return redirect("/choose")
            if mode and session["mode"] != mode:
                flash("הפעולה הזו זמינה במצב אחר – אפשר להחליף מצב בכל רגע מהתפריט."); return redirect("/choose")
            return fn(*a, **k)
        return w
    return deco

@app.context_processor
def inject():
    me = q("SELECT * FROM users WHERE id=?", (session.get("uid"),), True) if session.get("uid") else None
    return dict(me=me, mode=session.get("mode"), BRAND=BRAND, lang=session.get("lang", "he"), TMA=TYPE_MIN_AGE, CITIES=CITIES, TYPES=TYPES, DURS=DURS,
                STATUSES=STATUSES, CSS=CSS, wage_floor=wage_floor,
                stars=lambda v: "★" * round(v or 0) + "☆" * (5 - round(v or 0)))

# ===================== אבטחה, שפה וברקוד =====================
def render_template(name, **kw):
    return _rt(("en/" if session.get("lang") == "en" else "") + name, **kw)

def en(s):
    global _K
    if "_K" not in globals(): globals()["_K"] = sorted(EN, key=len, reverse=True)
    for k in _K: s = s.replace(k, EN[k])
    return s

@app.template_filter("tr")
def tr(s):
    if session.get("lang") != "en" or not isinstance(s, str): return s
    return EX.get(s) or en(s)

@app.before_request
def _csrf():                       # הגנת CSRF לכל טופס POST
    if "t" not in session: session["t"] = secrets.token_hex(16)
    if request.method == "POST" and not secrets.compare_digest(request.form.get("_t", "").encode(), session["t"].encode()):
        flash("הדף פג תוקף – נסו שוב.")
        return redirect(request.referrer or "/")

@app.after_request
def _headers(r):                   # כותרות אבטחה
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "same-origin"
    r.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'self'")
    if session.get("uid"): r.headers["Cache-Control"] = "no-store"
    return r

@app.route("/lang/<l>")
def set_lang(l):
    session["lang"] = "en" if l == "en" else "he"
    ref = request.referrer or ""
    return redirect(ref if ref.startswith(request.host_url) else "/")

def make_qr(u):
    import qrcode.image.svg
    img = qrcode.make(u, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    b = io.BytesIO(); img.save(b); t = b.getvalue().decode()
    return t[t.index("<svg"):]

@app.route("/qr")
def qr_page():
    u = (request.args.get("u") or PUBLIC_URL).strip()[:300]
    if not u: return render_template("qr", svg="", url="", auto=True)   # הדפדפן ישלים את הכתובת האמיתית
    if not u.startswith(("http://", "https://")): u = "https://" + u
    return render_template("qr", svg=make_qr(u), url=u, auto=False)

@app.route("/health")
def health():
    return "ok"      # לבדיקת זמינות (UptimeRobot)

# ===================== נתיבים: כללי =====================
@app.route("/")
def home():
    pop = q(JOB_SQL + "ORDER BY (SELECT COUNT(*) FROM apps WHERE job_id=j.id) DESC, j.id DESC LIMIT 6")
    st = dict(jobs=q("SELECT COUNT(*) c FROM jobs WHERE active=1", one=True)["c"],
              biz=q("SELECT COUNT(*) c FROM employers WHERE verified=1", one=True)["c"],
              teens=q("SELECT COUNT(*) c FROM users WHERE age IS NOT NULL", one=True)["c"])
    return render_template("home", pop=pop, st=st)

@app.route("/jobs")
def jobs():
    a = request.args; sql = JOB_SQL; p = []
    if a.get("q"):
        sql += "AND (j.title LIKE ? OR e.business LIKE ? OR j.description LIKE ?) "; p += [f"%{a['q']}%"] * 3
    if a.get("city"): sql += "AND j.city=? "; p.append(a["city"])
    if a.get("age", "").isdigit(): sql += "AND j.min_age<=? "; p.append(int(a["age"]))
    if a.get("jtype"): sql += "AND j.jtype=? "; p.append(a["jtype"])
    if a.get("wage"):
        try: sql += "AND (j.wage_period='month' OR j.wage>=?) "; p.append(float(a["wage"]))
        except ValueError: p.pop(); sql = sql[:-len("AND (j.wage_period='month' OR j.wage>=?) ")]
    if a.get("hours") == "morning": sql += "AND j.start_h<12 "
    if a.get("hours") == "afternoon": sql += "AND j.start_h>=12 AND j.start_h<17 "
    if a.get("hours") == "evening": sql += "AND j.end_h>=17 "
    if a.get("duration"): sql += "AND j.duration=? "; p.append(a["duration"])
    if a.get("no_exp"): sql += "AND j.no_exp=1 "
    if a.get("weekend"): sql += "AND j.weekend=1 "
    res = q(sql + "ORDER BY j.id DESC", p)
    return render_template("jobs", res=res, a=a)

@app.route("/job/<int:jid>")
def job(jid):
    j = q(JOB_SQL + "AND j.id=?", (jid,), True) or abort(404)
    rv = q("SELECT r.*, u.name FROM ratings r JOIN users u ON u.id=r.user_id WHERE job_id=? ORDER BY r.created DESC", (jid,))
    uid = session.get("uid")
    fav = uid and q("SELECT 1 FROM favs WHERE user_id=? AND job_id=?", (uid, jid), True)
    ap = uid and q("SELECT * FROM apps WHERE user_id=? AND job_id=?", (uid, jid), True)
    emp = q("SELECT * FROM employers WHERE user_id=?", (j["employer_id"],), True)
    return render_template("job", j=j, rv=rv, fav=fav, ap=ap, emp=emp)

# ===================== נתיבים: משתמשים =====================
# ===================== התחברות עם קוד אימות במייל =====================
FAILS = {}
def locked(k):
    n, t = FAILS.get(k, (0, 0)); return n >= 5 and time.time() - t < 600
def failed(k):
    n, t = FAILS.get(k, (0, 0)); FAILS[k] = (n + 1, t if n else time.time())

def send_code(uid):
    u = q("SELECT email FROM users WHERE id=?", (uid,), True)
    code = f"{secrets.randbelow(10**6):06d}"
    ex("INSERT INTO otps(uid,h,exp,tries,sent) VALUES(?,?,?,0,?) ON CONFLICT(uid) DO UPDATE SET h=excluded.h, exp=excluded.exp, tries=0, sent=excluded.sent",
       (uid, generate_password_hash(code), time.time() + 600, time.time()))
    session["pending"] = uid
    if not DEV_MODE:
        payload = json.dumps({"from": EMAIL_FROM, "to": [u["email"]], "subject": f"{BRAND} – קוד הכניסה שלך: {code}",
            "text": f"שלום!\nקוד האימות שלך ל-{BRAND} הוא: {code}\nהקוד תקף ל-10 דקות. אם לא ביקשת להתחבר – התעלמו מההודעה."}).encode()
        req = urllib.request.Request("https://api.resend.com/emails", payload, {"Authorization": "Bearer " + RESEND_API_KEY,
            "Content-Type": "application/json", "User-Agent": "teenjob/1.0"})
        try:
            urllib.request.urlopen(req, timeout=15); return
        except Exception as err:
            print("⚠ שליחת האימייל נכשלה:", err)
    print(f"🔑 [מצב פיתוח] קוד האימות עבור {u['email']}: {code}")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form; email = f["email"].strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email): flash("כתובת האימייל אינה תקינה.")
        elif len(f["pw"]) < 8: flash("הסיסמה חייבת להכיל לפחות 8 תווים.")
        elif q("SELECT 1 FROM users WHERE email=?", (email,), True): flash("האימייל כבר רשום – נסו להתחבר.")
        else:
            uid = ex("INSERT INTO users(email,pw,name) VALUES(?,?,?)", (email, generate_password_hash(f["pw"]), f["name"].strip()[:40]))
            send_code(uid); return redirect("/verify")
    return render_template("auth", reg=True)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower(); k = email + str(request.remote_addr)
        if locked(k): flash("יותר מדי ניסיונות. נסו שוב בעוד כמה דקות.")
        else:
            u = q("SELECT * FROM users WHERE email=?", (email,), True)
            if u and check_password_hash(u["pw"], request.form["pw"]):
                FAILS.pop(k, None); send_code(u["id"]); return redirect("/verify")
            failed(k); flash("אימייל או סיסמה שגויים.")
    return render_template("auth", reg=False)

@app.route("/verify", methods=["GET", "POST"])
def verify():
    uid = session.get("pending")
    o = uid and q("SELECT * FROM otps WHERE uid=?", (uid,), True)
    if not o: return redirect("/login")
    if request.method == "POST":
        if time.time() > o["exp"] or o["tries"] >= 5:
            ex("DELETE FROM otps WHERE uid=?", (uid,)); session.pop("pending", None)
            flash("הקוד פג תוקף. התחברו מחדש."); return redirect("/login")
        if check_password_hash(o["h"], request.form.get("code", "").strip()):
            ex("DELETE FROM otps WHERE uid=?", (uid,)); session.pop("pending", None)
            session["uid"] = uid; session["t"] = secrets.token_hex(16); session.permanent = True
            session.pop("mode", None); return redirect("/choose")
        ex("UPDATE otps SET tries=tries+1 WHERE uid=?", (uid,)); flash("קוד שגוי.")
    mail = q("SELECT email FROM users WHERE id=?", (uid,), True)["email"]
    return render_template("verify", dev=DEV_MODE, email=mail[0] + "***" + mail[mail.index("@"):])

@app.route("/resend", methods=["POST"])
def resend():
    uid = session.get("pending"); o = uid and q("SELECT sent FROM otps WHERE uid=?", (uid,), True)
    if not o: return redirect("/login")
    if time.time() - o["sent"] < 30: flash("אפשר לבקש קוד חדש רק אחרי 30 שניות.")
    else: send_code(uid); flash("נשלח קוד חדש.")
    return redirect("/verify")

@app.route("/logout", methods=["POST"])
def logout():
    l = session.get("lang"); session.clear()
    if l: session["lang"] = l
    return redirect("/")

@app.route("/choose")
def choose():
    if not session.get("uid"): return redirect("/login")
    return render_template("choose")

@app.route("/switch/<m>")
def switch(m):
    if not session.get("uid") or m not in ("teen", "employer"): return redirect("/login")
    session["mode"] = m
    return redirect("/me" if m == "teen" else "/employer")

# ===================== אזור אישי – נוער =====================
@app.route("/me", methods=["GET", "POST"])
@need("teen")
def me_page():
    uid = session["uid"]
    if request.method == "POST":
        f = request.form
        try: age = int(f["age"]); assert 14 <= age <= 21
        except Exception: flash("האתר מיועד לגילאי 14–21."); return redirect("/me")
        ex("UPDATE users SET name=?,age=?,city=?,skills=?,experience=?,bio=? WHERE id=?",
           (f["name"], age, f["city"], f["skills"], f["experience"], f["bio"], uid))
        flash("הפרופיל נשמר ✔")
        return redirect("/me")
    favs = q(JOB_SQL + "AND j.id IN (SELECT job_id FROM favs WHERE user_id=?)", (uid,))
    aps = q("""SELECT a.*, j.title, e.business FROM apps a JOIN jobs j ON j.id=a.job_id
               JOIN employers e ON e.user_id=j.employer_id WHERE a.user_id=? ORDER BY a.id DESC""", (uid,))
    return render_template("me", favs=favs, aps=aps)

@app.route("/fav/<int:jid>")
@need("teen")
def fav(jid):
    uid = session["uid"]
    if q("SELECT 1 FROM favs WHERE user_id=? AND job_id=?", (uid, jid), True):
        ex("DELETE FROM favs WHERE user_id=? AND job_id=?", (uid, jid))
    else: ex("INSERT INTO favs VALUES(?,?)", (uid, jid))
    return redirect(f"/job/{jid}")

@app.route("/apply/<int:jid>", methods=["POST"])
@need("teen")
def apply(jid):
    u = q("SELECT * FROM users WHERE id=?", (session["uid"],), True)
    j = q("SELECT * FROM jobs WHERE id=? AND active=1", (jid,), True) or abort(404)
    if not u["age"]: flash("כדי להגיש מועמדות יש להשלים גיל בפרופיל."); return redirect("/me")
    if u["age"] < j["min_age"]: flash(f"המשרה מיועדת מגיל {j['min_age']}.")
    elif q("SELECT 1 FROM apps WHERE user_id=? AND job_id=?", (u["id"], jid), True): flash("כבר הגשת מועמדות למשרה הזו.")
    else:
        ex("INSERT INTO apps(user_id,job_id,message) VALUES(?,?,?)", (u["id"], jid, request.form.get("message", "")))
        flash("המועמדות נשלחה! 🎉 אפשר לעקוב אחרי הסטטוס באזור האישי.")
    return redirect(f"/job/{jid}")

@app.route("/rate/<int:jid>", methods=["POST"])
@need()
def rate(jid):
    j = q("SELECT * FROM jobs WHERE id=?", (jid,), True) or abort(404)
    if j["employer_id"] == session["uid"]: flash("אי אפשר לדרג משרה של עצמך.")
    else:
        s = min(5, max(1, int(request.form.get("stars", 5))))
        ex("INSERT INTO ratings(user_id,job_id,stars,comment) VALUES(?,?,?,?) ON CONFLICT(user_id,job_id) DO UPDATE SET stars=excluded.stars, comment=excluded.comment",
           (session["uid"], jid, s, request.form.get("comment", "")[:500]))
        flash("תודה על הדירוג!")
    return redirect(f"/job/{jid}")

@app.route("/report/<int:jid>", methods=["POST"])
@need()
def report(jid):
    ex("INSERT INTO reports(job_id,user_id,reason) VALUES(?,?,?)", (jid, session["uid"], request.form.get("reason", "")))
    flash("הדיווח התקבל ויבדק על ידי הצוות. תודה ששומרים על הקהילה 💙")
    return redirect(f"/job/{jid}")

# ===================== אזור מעסיקים =====================
@app.route("/employer", methods=["GET", "POST"])
@need("employer")
def employer():
    uid = session["uid"]
    emp = q("SELECT * FROM employers WHERE user_id=?", (uid,), True)
    if request.method == "POST":
        f = request.form
        if emp: ex("UPDATE employers SET business=?,city=?,about=?,reg_no=? WHERE user_id=?",
                   (f["business"], f["city"], f["about"], f["reg_no"], uid))
        else: ex("INSERT INTO employers(user_id,business,city,about,reg_no) VALUES(?,?,?,?,?)",
                 (uid, f["business"], f["city"], f["about"], f["reg_no"]))
        flash("פרופיל העסק נשמר ✔ – הבקשה לאימות הועברה לצוות."); return redirect("/employer")
    js = q("""SELECT j.*, (SELECT COUNT(*) FROM apps WHERE job_id=j.id) napps FROM jobs j
              WHERE employer_id=? ORDER BY id DESC""", (uid,))
    return render_template("emp", emp=emp, js=js)

@app.route("/employer/job/<int:jid>", methods=["GET", "POST"])
@need("employer")
def job_form(jid):
    uid = session["uid"]
    if not q("SELECT 1 FROM employers WHERE user_id=?", (uid,), True):
        flash("קודם יוצרים פרופיל עסקי."); return redirect("/employer")
    j = q("SELECT * FROM jobs WHERE id=? AND employer_id=?", (jid, uid), True) if jid else None
    if jid and not j: abort(404)
    if request.method == "POST":
        f = request.form; err = validate_job(f)
        if err:
            for e in err: flash("⚠ " + e)
            return render_template("jobform", j=f, jid=jid)
        v = (f["title"], f["city"], f["jtype"], float(f["wage"]), int(f["min_age"]), int(f["start_h"]),
             int(f["end_h"]), f["days"], f["duration"], int(bool(f.get("no_exp"))), int(bool(f.get("weekend"))),
             f["description"], f["requirements"], "month" if f.get("wage_period") == "month" else "hour", dpw_of(f))
        cols = "title=?,city=?,jtype=?,wage=?,min_age=?,start_h=?,end_h=?,days=?,duration=?,no_exp=?,weekend=?,description=?,requirements=?,wage_period=?,days_per_week=?"
        if jid: ex(f"UPDATE jobs SET {cols} WHERE id=?", v + (jid,))
        else: ex("INSERT INTO jobs(title,city,jtype,wage,min_age,start_h,end_h,days,duration,no_exp,weekend,description,requirements,wage_period,days_per_week,employer_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", v + (uid,))
        flash("המשרה נשמרה ✔"); return redirect("/employer")
    return render_template("jobform", j=j or {}, jid=jid)

@app.route("/employer/job/<int:jid>/toggle")
@need("employer")
def toggle(jid):
    ex("UPDATE jobs SET active=1-active WHERE id=? AND employer_id=?", (jid, session["uid"])); return redirect("/employer")

@app.route("/employer/job/<int:jid>/applicants")
@need("employer")
def applicants(jid):
    j = q("SELECT * FROM jobs WHERE id=? AND employer_id=?", (jid, session["uid"]), True) or abort(404)
    aps = q("""SELECT a.*, u.name,u.age,u.city,u.skills,u.experience,u.bio FROM apps a
               JOIN users u ON u.id=a.user_id WHERE a.job_id=? ORDER BY a.id DESC""", (jid,))
    return render_template("applicants", j=j, aps=aps)

@app.route("/employer/app/<int:aid>", methods=["POST"])
@need("employer")
def app_status(aid):
    a = q("SELECT a.*, j.employer_id FROM apps a JOIN jobs j ON j.id=a.job_id WHERE a.id=?", (aid,), True) or abort(404)
    if a["employer_id"] != session["uid"]: abort(403)
    ex("UPDATE apps SET status=? WHERE id=?", (request.form["status"], aid))
    return redirect(f"/employer/job/{a['job_id']}/applicants")

# ===================== ניהול (אימות מעסיקים + דיווחים) =====================
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and request.form.get("code"): session["admin"] = request.form["code"] == ADMIN_CODE
    if not session.get("admin"): return render_template("admin", ok=False)
    if request.method == "POST" and request.form.get("verify"):
        ex("UPDATE employers SET verified=? WHERE user_id=?", (int(request.form["val"]), request.form["verify"]))
    if request.method == "POST" and request.form.get("close"):
        ex("UPDATE jobs SET active=0 WHERE id=?", (request.form["close"],))
    return render_template("admin", ok=True, emps=q("SELECT * FROM employers"),
                           reps=q("SELECT r.*, j.title FROM reports r JOIN jobs j ON j.id=r.job_id ORDER BY r.id DESC"))

# ===================== עיצוב =====================
CSS = """
:root{--pink:#ff5fa2;--pink2:#ffe3ef;--sky:#38bdf8;--sky2:#e0f4ff;--ink:#1f2a44;--mut:#6b7794;--bg:#f7fbff}
*{box-sizing:border-box}body{margin:0;font-family:Heebo,Arial,sans-serif;background:var(--bg);color:var(--ink);line-height:1.6}
a{color:inherit;text-decoration:none}main{max-width:1100px;margin:auto;padding:24px 16px;animation:in .45s ease}
@keyframes in{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.nav{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:12px 20px;background:#fff;box-shadow:0 2px 14px #38bdf822;position:sticky;top:0;z-index:9}
.logo{font-size:1.5rem;font-weight:800}.logo b{color:var(--pink)}nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
nav a{padding:6px 12px;border-radius:99px;font-weight:600}nav a:hover{background:var(--sky2)}
.btn{display:inline-block;border:0;cursor:pointer;font:inherit;font-weight:700;padding:11px 22px;border-radius:99px;background:linear-gradient(135deg,var(--pink),#ff8fc0);color:#fff;box-shadow:0 6px 18px #ff5fa240;transition:.2s}
.btn:hover{transform:translateY(-2px)}.btn.blue{background:linear-gradient(135deg,var(--sky),#7dd3fc);box-shadow:0 6px 18px #38bdf845}
.btn.ghost{background:#fff;color:var(--ink);box-shadow:0 0 0 2px var(--sky2)}.btn.sm{padding:6px 14px;font-size:.9rem}
.hero{text-align:center;padding:48px 16px;border-radius:32px;background:linear-gradient(135deg,var(--pink2),#fff 50%,var(--sky2))}
.hero h1{font-size:clamp(2rem,6vw,3.4rem);margin:0 0 8px;font-weight:800}.hero p{color:var(--mut);font-size:1.15rem}
.search{display:flex;gap:8px;max-width:680px;margin:22px auto;background:#fff;padding:8px;border-radius:99px;box-shadow:0 10px 30px #38bdf830}
.search input{flex:1;border:0;outline:0;font:inherit;padding:0 14px;background:none}
.card{background:#fff;border-radius:22px;padding:20px;box-shadow:0 6px 22px #1f2a4410;transition:.2s}
.card:hover{transform:translateY(-3px)}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.tag{display:inline-block;background:var(--sky2);color:#0b7bb0;border-radius:99px;padding:2px 10px;font-size:.82rem;margin:2px}
.tag.p{background:var(--pink2);color:#c2185b}.tag.g{background:#dcfce7;color:#15803d}.tag.o{background:#fef3c7;color:#b45309}
h2{margin:32px 0 14px}.mut{color:var(--mut)}.stars{color:#fbbf24;letter-spacing:2px}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.steps .card{text-align:center}.ic{font-size:2.2rem}
.stats{display:flex;gap:16px;justify-content:center;flex-wrap:wrap;margin-top:28px}.stats div{background:#fff;border-radius:18px;padding:12px 26px;box-shadow:0 4px 14px #0001}.stats b{font-size:1.8rem;color:var(--pink);display:block}
form.f{display:grid;gap:12px}label{font-weight:600;font-size:.92rem}
input,select,textarea{width:100%;font:inherit;padding:10px 14px;border:2px solid var(--sky2);border-radius:14px;background:#fff;outline:0}
input:focus,select:focus,textarea:focus{border-color:var(--sky)}input[type=checkbox]{width:auto}
.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;align-items:end}
.flash{max-width:1100px;margin:12px auto;padding:12px 18px;background:#fff7d6;border-radius:14px;font-weight:600}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:space-between}
.narrow{max-width:460px;margin:30px auto}.big{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}
.big .card{text-align:center;padding:36px}.empty{text-align:center;padding:40px;color:var(--mut)}
footer{text-align:center;color:var(--mut);padding:30px 16px;font-size:.88rem}
html{scroll-behavior:smooth}@view-transition{navigation:auto}
::view-transition-old(root){animation:.22s ease both fo}::view-transition-new(root){animation:.45s cubic-bezier(.2,.8,.2,1) both in}
@keyframes fo{to{opacity:0}}@keyframes rise{from{opacity:0;transform:translateY(22px) scale(.98)}to{opacity:1;transform:none}}
.card,.btn,nav a,.tag,input,select,textarea{transition:all .35s cubic-bezier(.2,.8,.2,1)}
.card:hover{transform:translateY(-6px);box-shadow:0 16px 36px #38bdf825}.btn:active{transform:scale(.96)}
.grid>*,.steps>*,.big>*,.trust>*{animation:rise .6s cubic-bezier(.2,.8,.2,1) both}
.grid>:nth-child(2),.steps>:nth-child(2){animation-delay:.08s}.grid>:nth-child(3),.steps>:nth-child(3){animation-delay:.16s}.grid>:nth-child(n+4){animation-delay:.24s}
.user{position:relative}.ubtn{display:flex;align-items:center;gap:8px;background:#fff;border:2px solid var(--sky2);border-radius:99px;padding:4px 14px 4px 6px;font:inherit;cursor:pointer}
.av{width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,var(--pink),var(--sky));color:#fff;display:grid;place-items:center;font-weight:800}
.menu{position:absolute;inset-inline-end:0;top:125%;min-width:240px;background:#fff;border-radius:16px;box-shadow:0 14px 40px #1f2a4426;padding:8px;opacity:0;visibility:hidden;transform:translateY(-10px) scale(.97);transition:.3s cubic-bezier(.2,.8,.2,1)}
.user.open .menu{opacity:1;visibility:visible;transform:none}
.menu a,.menu .out{display:block;width:100%;text-align:start;padding:9px 12px;border-radius:10px;border:0;background:none;font:inherit;cursor:pointer}
.menu a:hover,.menu .out:hover{background:var(--sky2)}.menu .out{color:#dc2626}.mail{padding:8px 12px;color:var(--mut);border-bottom:1px solid var(--sky2);margin-bottom:6px;font-size:.9rem}
.trust{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin:22px 0}.trust span{background:#fff;border-radius:99px;padding:6px 16px;box-shadow:0 3px 12px #0001;font-size:.9rem;font-weight:600}
.otp{text-align:center;font-size:2rem;letter-spacing:.5em;direction:ltr}.qr{text-align:center}.qr svg{width:260px;max-width:100%;height:auto;background:#fff;padding:14px;border-radius:20px;box-shadow:0 6px 22px #1f2a4415}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media print{.nav,footer,form,button{display:none!important}}
"""

# ===================== תבניות =====================
T = {}
T["card"] = """<a class="card" href="/job/{{j.id}}" style="display:block">
<div class="row"><b style="font-size:1.15rem">{{j.title}}</b>{% if j.verified %}<span class="tag g">✔ מאומת</span>{% else %}<span class="tag o">לא מאומת</span>{% endif %}</div>
<div class="mut">{{j.business}} · 📍 {{j.city|tr}}</div>
<div style="margin:8px 0"><span class="tag p">💰 {{ '%g'|format(j.wage) }} ₪ {{ 'לחודש' if j.wage_period=='month' else 'לשעה' }}</span><span class="tag">מגיל {{j.min_age}}</span><span class="tag">{{j.duration|tr}}</span>{% if j.no_exp %}<span class="tag g">ללא ניסיון</span>{% endif %}</div>
<div class="stars">{{ stars(j.avg) if j.cnt else '' }} <span class="mut">{{ '(%d)'|format(j.cnt) if j.cnt else 'עדיין ללא דירוגים' }}</span></div>
<p class="mut">{{ j.description[:110] }}…</p>
<p><small><b>דרישות:</b> {{ j.requirements or 'ללא דרישות מיוחדות' }}</small></p>
<span class="btn sm blue">לפרטים ולהגשת מועמדות</span></a>"""

T["home"] = """{% extends 'base' %}{% block c %}
<section class="hero"><h1>העבודה הראשונה שלך מתחילה כאן</h1>
<p>מוצאים עבודה שמתאימה לגיל שלך, לזמן שלך וליכולות שלך</p>
<form class="search" action="/jobs"><input name="q" placeholder="🔎 איזו עבודה מחפשים? (בייביסיטר, דוג ווקר, שיעורים פרטיים...)"><button class="btn">חיפוש</button></form>
<a class="btn" href="/switch/teen">מצאתי עבודה</a> <a class="btn blue" href="/switch/employer">אני מעסיק</a>
<div class="stats"><div><b>{{st.jobs}}</b>משרות פעילות</div><div><b>{{st.biz}}</b>עסקים מאומתים</div><div><b>{{st.teens}}</b>בני נוער רשומים</div></div>
<small class="mut">* כל המספרים אמיתיים ומתעדכנים לפי הפעילות בפלטפורמה</small></section>
<div class="trust"><span>🔒 כניסה עם קוד אימות במייל</span><span>✔ מעסיקים מאומתים</span><span>⚖ בהתאם לחוק עבודת הנוער</span><span>🚩 דיווח על משרות חשודות</span></div>
<h2>🔥 משרות פופולריות</h2>
{% if pop %}<div class="grid">{% for j in pop %}{% include 'card' %}{% endfor %}</div>
{% else %}<div class="card empty">עדיין לא פורסמו משרות. מעסיקים – היו הראשונים! 🚀</div>{% endif %}
<h2>איך זה עובד?</h2><div class="steps">
<div class="card"><div class="ic">📝</div><b>1. נרשמים</b><p class="mut">יוצרים פרופיל קצר עם גיל, עיר וכישורים</p></div>
<div class="card"><div class="ic">🔎</div><b>2. מחפשים</b><p class="mut">מסננים לפי עיר, שכר, שעות וגיל</p></div>
<div class="card"><div class="ic">🎉</div><b>3. מגישים ומתחילים</b><p class="mut">מגישים מועמדות ועוקבים אחרי הסטטוס</p></div></div>
{% endblock %}"""

T["jobs"] = """{% extends 'base' %}{% block c %}<h1>🔎 חיפוש משרות</h1>
<form class="card filters" method="get">
<div><label>חיפוש חופשי</label><input name="q" value="{{a.q or ''}}"></div>
<div><label>עיר</label><select name="city"><option value="">הכל</option>{% for c in CITIES %}<option {{'selected' if a.city==c}} value="{{c}}">{{c|tr}}</option>{% endfor %}</select></div>
<div><label>הגיל שלי</label><input type="number" min="14" max="21" name="age" value="{{a.age or ''}}"></div>
<div><label>סוג עבודה</label><select name="jtype"><option value="">הכל</option>{% for c in TYPES %}<option {{'selected' if a.jtype==c}} value="{{c}}">{{c|tr}}</option>{% endfor %}</select></div>
<div><label>שכר מינימלי לשעה</label><input type="number" name="wage" value="{{a.wage or ''}}"></div>
<div><label>שעות</label><select name="hours"><option value="">הכל</option><option value="morning" {{'selected' if a.hours=='morning'}}>בוקר</option><option value="afternoon" {{'selected' if a.hours=='afternoon'}}>צהריים</option><option value="evening" {{'selected' if a.hours=='evening'}}>ערב</option></select></div>
<div><label>סוג משרה</label><select name="duration"><option value="">הכל</option>{% for c in DURS %}<option {{'selected' if a.duration==c}} value="{{c}}">{{c|tr}}</option>{% endfor %}</select></div>
<div><label><input type="checkbox" name="no_exp" value="1" {{'checked' if a.no_exp}}> ללא ניסיון</label><label><input type="checkbox" name="weekend" value="1" {{'checked' if a.weekend}}> סופ"ש/חופשות</label></div>
<button class="btn">סינון</button><a class="btn ghost" href="/jobs">נקה</a></form>
<h2>{{res|length}} משרות נמצאו</h2>
{% if res %}<div class="grid">{% for j in res %}{% include 'card' %}{% endfor %}</div>
{% else %}<div class="card empty">לא נמצאו משרות לפי הסינון. נסו להרחיב את החיפוש.</div>{% endif %}{% endblock %}"""

T["job"] = """{% extends 'base' %}{% block c %}
<div class="card"><div class="row"><h1 style="margin:0">{{j.title}}</h1>
{% if j.verified %}<span class="tag g">✔ מעסיק מאומת</span>{% else %}<span class="tag o">⚠ מעסיק שטרם אומת – היזהרו ואל תמסרו מידע אישי</span>{% endif %}</div>
<p class="mut">{{j.business}} · 📍 {{j.city|tr}} · {{j.jtype|tr}}</p>
<div class="stars">{{ stars(j.avg) }} <span class="mut">{{ '%.1f (%d דירוגים)'|format(j.avg,j.cnt) if j.cnt else 'עדיין ללא דירוגים' }}</span></div>
<p>{{j.description}}</p>
<div class="grid"><div><b>💰 שכר:</b> {{ '%g'|format(j.wage) }} ₪ {{ 'לחודש' if j.wage_period=='month' else 'לשעה' }}</div><div><b>🎂 גיל מינימלי:</b> {{j.min_age}}</div>
<div><b>🕒 שעות:</b> {{'%02d'|format(j.start_h)}}:00–{{'%02d'|format(j.end_h)}}:00</div><div><b>📅 ימים:</b> {{j.days or 'לפי תיאום'}}</div>
<div><b>סוג:</b> {{j.duration|tr}}</div><div><b>ניסיון:</b> {{'לא נדרש' if j.no_exp else 'נדרש'}}</div></div>
<p><b>דרישות:</b> {{j.requirements or 'ללא דרישות מיוחדות'}}</p>
<p><b>על העסק:</b> {{emp.about or 'לא צוין'}}</p>
{% if ap %}<span class="tag g">הגשת מועמדות · סטטוס: {{ap.status|tr}}</span>
{% elif mode=='teen' %}<form class="f" method="post" action="/apply/{{j.id}}"><textarea name="message" placeholder="הודעה קצרה למעסיק (לא לכתוב טלפון/כתובת)"></textarea><div class="row"><button class="btn">הגשת מועמדות</button><a class="btn ghost" href="/fav/{{j.id}}">{{'💔 הסר ממועדפים' if fav else '❤ שמור למועדפים'}}</a></div></form>
{% elif not me %}<a class="btn" href="/login?next=/job/{{j.id}}">התחברו כדי להגיש מועמדות</a>
{% else %}<p class="mut">עברו למצב "מחפש עבודה" בתפריט כדי להגיש מועמדות.</p>{% endif %}</div>
{% if me %}<h2>⭐ דירוג</h2><form class="card f" method="post" action="/rate/{{j.id}}"><div class="row">
<select name="stars" style="max-width:200px"><option value="5">★★★★★ מעולה</option><option value="4">★★★★ טוב</option><option value="3">★★★ סביר</option><option value="2">★★ לא טוב</option><option value="1">★ גרוע</option></select></div>
<textarea name="comment" placeholder="איך הייתה החוויה? (אופציונלי)"></textarea><button class="btn sm blue" style="justify-self:start">שליחת דירוג</button></form>{% endif %}
{% for r in rv %}<div class="card" style="margin-top:10px"><span class="stars">{{stars(r.stars)}}</span> <b>{{r.name}}</b><p class="mut">{{r.comment}}</p></div>{% endfor %}
{% if me %}<details style="margin-top:20px"><summary class="mut">🚩 דיווח על משרה חשודה</summary><form class="f card" method="post" action="/report/{{j.id}}"><textarea name="reason" required placeholder="מה חשוד?"></textarea><button class="btn sm">שליחת דיווח</button></form></details>{% endif %}
{% endblock %}"""

T["auth"] = """{% extends 'base' %}{% block c %}<div class="card narrow"><h1>{{ 'הרשמה' if reg else 'התחברות' }}</h1>
<form class="f" method="post">{% if reg %}<div><label>שם (או כינוי – לא חובה שם מלא)</label><input name="name" required></div>{% endif %}
<div><label>אימייל</label><input type="email" name="email" required></div><div><label>סיסמה</label><input type="password" name="pw" required></div>
<button class="btn">{{ 'יצירת חשבון' if reg else 'כניסה' }}</button></form>
<p class="mut">{% if reg %}יש כבר חשבון? <a href="/login"><u>התחברות</u></a>{% else %}חדשים כאן? <a href="/register"><u>הרשמה</u></a>{% endif %}</p></div>{% endblock %}"""

T["choose"] = """{% extends 'base' %}{% block c %}<h1 style="text-align:center">איך תרצו להשתמש ב-{{BRAND}}?</h1>
<p class="mut" style="text-align:center">אפשר להחליף מצב בכל רגע מהתפריט העליון</p>
<div class="big"><a class="card" href="/switch/teen"><div class="ic">🎒</div><h2>אני מחפש/ת עבודה</h2></a>
<a class="card" href="/switch/employer"><div class="ic">🏢</div><h2>אני מפרסם/ת עבודה (עסק או פרטי)</h2></a></div>{% endblock %}"""

T["me"] = """{% extends 'base' %}{% block c %}<h1>👤 האזור האישי</h1>
<form class="card f" method="post"><div class="grid"><div><label>שם / כינוי</label><input name="name" value="{{me.name or ''}}"></div>
<div><label>גיל (14–21)</label><input type="number" name="age" value="{{me.age or ''}}" required></div>
<div><label>עיר</label><select name="city">{% for c in CITIES %}<option {{'selected' if me.city==c}} value="{{c}}">{{c|tr}}</option>{% endfor %}</select></div></div>
<div><label>כישורים ותחומי עניין</label><input name="skills" value="{{me.skills or ''}}" placeholder="למשל: אנגלית, עבודה עם ילדים, מחשבים"></div>
<div><label>ניסיון תעסוקתי (אם יש)</label><textarea name="experience">{{me.experience or ''}}</textarea></div>
<div><label>קצת עליי</label><textarea name="bio">{{me.bio or ''}}</textarea></div>
<small class="mut">🔒 לא מציגים טלפון או כתובת. אל תכתבו אותם בפרופיל.</small><button class="btn" style="justify-self:start">שמירה</button></form>
<h2>❤ משרות שמורות</h2>{% if favs %}<div class="grid">{% for j in favs %}{% include 'card' %}{% endfor %}</div>{% else %}<div class="card empty">אין עדיין משרות שמורות</div>{% endif %}
<h2 id="apps">📨 המועמדויות שלי</h2>{% for a in aps %}<div class="card row" style="margin-bottom:8px"><span><b>{{a.title}}</b> · {{a.business}}</span><span class="tag p">{{a.status|tr}}</span></div>
{% else %}<div class="card empty">עוד לא הגשת מועמדות</div>{% endfor %}{% endblock %}"""

T["emp"] = """{% extends 'base' %}{% block c %}<h1>🏢 אזור מעסיקים</h1>
<form class="card f" method="post"><h3 style="margin:0">פרופיל עסקי {% if emp %}{{ '<span class="tag g">✔ מאומת</span>'|safe if emp.verified else '<span class="tag o">ממתין לאימות</span>'|safe }}{% endif %}</h3>
<div class="grid"><div><label>שם העסק (עבודה)</label><input name="business" required maxlength="60" placeholder="למשל: בייביסיטר בראשון / דוג ווקר / מורה פרטי למתמטיקה / פיצה דומינו" value="{{emp.business if emp}}"></div>
<div><label>עיר</label><select name="city">{% for c in CITIES %}<option {{'selected' if emp and emp.city==c}} value="{{c}}">{{c|tr}}</option>{% endfor %}</select></div>
<div><label>אימות: מספר עוסק/ח"פ (לעסק) או "פרטי"</label><input name="reg_no" maxlength="30" value="{{emp.reg_no if emp}}"></div></div>
<div><label>על העסק / על העבודה</label><textarea name="about">{{emp.about if emp}}</textarea></div><button class="btn" style="justify-self:start">שמירת פרופיל</button></form>
{% if emp %}<div class="row"><h2>המשרות שלי</h2><a class="btn" href="/employer/job/0">+ פרסום משרה חדשה</a></div>
{% for j in js %}<div class="card row" style="margin-bottom:10px"><span><b>{{j.title}}</b> {{'' if j.active else '(לא פעילה)'}} · {{j.napps}} מועמדים</span>
<span><a class="btn sm blue" href="/employer/job/{{j.id}}/applicants">מועמדים</a> <a class="btn sm ghost" href="/employer/job/{{j.id}}">עריכה</a> <a class="btn sm ghost" href="/employer/job/{{j.id}}/toggle">{{'השהיה' if j.active else 'הפעלה'}}</a></span></div>
{% else %}<div class="card empty">עוד לא פרסמתם משרות</div>{% endfor %}{% endif %}{% endblock %}"""

T["jobform"] = """{% extends 'base' %}{% block c %}<h1>{{ 'עריכת משרה' if jid else 'פרסום משרה חדשה' }}</h1>
<div class="flash">⚖ לפי חוק עבודת הנוער: מגיל 14, עד 8 שעות ביום, עד גיל 16 לא בין 20:00–06:00, ועד 18 לא בין 22:00–06:00, שכר לא נמוך מהמינימום לגיל, ובעבודות מסוכנות (כמו מטבח ושליחויות) הגיל המינימלי באתר גבוה יותר.</div>
<form class="card f" method="post"><div class="grid">
<div><label>שם המשרה</label><input name="title" required value="{{j.title}}"></div>
<div><label>עיר</label><select name="city">{% for c in CITIES %}<option {{'selected' if j.city==c}} value="{{c}}">{{c|tr}}</option>{% endfor %}</select></div>
<div><label>סוג עבודה</label><select name="jtype">{% for c in TYPES %}<option {{'selected' if j.jtype==c}} value="{{c}}">{{c|tr}}</option>{% endfor %}</select></div>
<div><label>שכר (₪)</label><input type="number" step="0.1" name="wage" required value="{{j.wage}}"></div>
<div><label>תשלום לפי</label><select name="wage_period"><option value="hour" {{'selected' if j.wage_period!='month'}}>שעה</option><option value="month" {{'selected' if j.wage_period=='month'}}>חודש</option></select></div>
<div><label>ימים בשבוע</label><input type="number" min="1" max="6" name="days_per_week" value="{{j.days_per_week or 3}}"></div>
<div><label>גיל מינימלי (ומעלה)</label><input type="number" min="14" name="min_age" required value="{{j.min_age or 16}}"></div>
<div><label>משעה</label><input type="number" min="0" max="23" name="start_h" required value="{{j.start_h or 16}}"></div>
<div><label>עד שעה</label><input type="number" min="1" max="24" name="end_h" required value="{{j.end_h or 20}}"></div>
<div><label>ימים</label><input name="days" value="{{j.days}}" placeholder="א'-ה' / סופ״ש"></div>
<div><label>סוג משרה</label><select name="duration">{% for c in DURS %}<option {{'selected' if j.duration==c}} value="{{c}}">{{c|tr}}</option>{% endfor %}</select></div></div>
<label><input type="checkbox" name="no_exp" {{'checked' if j.no_exp}}> מתאים ללא ניסיון</label>
<label><input type="checkbox" name="weekend" {{'checked' if j.weekend}}> סופי שבוע / חופשות</label>
<div><label>תיאור המשרה</label><textarea name="description" required>{{j.description}}</textarea></div>
<div><label>דרישות</label><textarea name="requirements">{{j.requirements}}</textarea></div>
<small class="mut" id="ah"></small><button class="btn" style="justify-self:start">שמירה ופרסום</button></form>
<script>const M={{ TMA|tojson }},t=document.querySelector('[name=jtype]'),a=document.querySelector('[name=min_age]');
function f(){const m=M[t.value]||14;a.min=m;if(+a.value<m)a.value=m;document.getElementById('ah').textContent='גיל מינימלי לסוג עבודה זה: '+m}t.onchange=f;f()</script>{% endblock %}"""

T["applicants"] = """{% extends 'base' %}{% block c %}<h1>מועמדים – {{j.title}}</h1>
{% for a in aps %}<div class="card" style="margin-bottom:10px"><div class="row"><b>{{a.name}} · גיל {{a.age}} · {{a.city}}</b>
<form method="post" action="/employer/app/{{a.id}}" class="row"><select name="status" style="width:auto">{% for s in STATUSES %}<option {{'selected' if a.status==s}} value="{{s}}">{{s|tr}}</option>{% endfor %}</select><button class="btn sm">עדכון</button></form></div>
<p><b>כישורים:</b> {{a.skills}}<br><b>ניסיון:</b> {{a.experience}}<br><b>הודעה:</b> {{a.message}}</p></div>
{% else %}<div class="card empty">עדיין אין מועמדים</div>{% endfor %}<a href="/employer">→ חזרה</a>{% endblock %}"""

T["admin"] = """{% extends 'base' %}{% block c %}<h1>ניהול</h1>
{% if not ok %}<form class="card f narrow" method="post"><input type="password" name="code" placeholder="קוד ניהול"><button class="btn">כניסה</button></form>
{% else %}<h2>אימות מעסיקים</h2>{% for e in emps %}<form method="post" class="card row" style="margin-bottom:8px"><span><b>{{e.business}}</b> · ח"פ: {{e.reg_no}} · {{'✔ מאומת' if e.verified else 'לא מאומת'}}</span>
<input type="hidden" name="verify" value="{{e.user_id}}"><input type="hidden" name="val" value="{{ 0 if e.verified else 1 }}"><button class="btn sm">{{'בטל אימות' if e.verified else 'אמת'}}</button></form>{% endfor %}
<h2>דיווחים על משרות</h2>{% for r in reps %}<form method="post" class="card row" style="margin-bottom:8px"><span><a href="/job/{{r.job_id}}"><u>{{r.title}}</u></a>: {{r.reason}}</span>
<input type="hidden" name="close" value="{{r.job_id}}"><button class="btn sm">הסרת המשרה</button></form>{% else %}<div class="card empty">אין דיווחים</div>{% endfor %}{% endif %}{% endblock %}"""

T["base"] = """<!doctype html><html lang="{{ 'en' if lang=='en' else 'he' }}" dir="{{ 'ltr' if lang=='en' else 'rtl' }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{BRAND}} – העבודה הראשונה שלך</title>
<link href="https://fonts.googleapis.com/css2?family=Heebo:wght@400;600;800&display=swap" rel="stylesheet">
<style>{{CSS|safe}}</style></head><body>
<header class="nav"><a class="logo" href="/">{{BRAND}} 💼</a><nav>
<a href="/jobs">🔎 חיפוש עבודה</a>
{% if mode=='teen' %}<a href="/me#apps">📨 המועמדויות שלי</a>{% elif mode=='employer' %}<a href="/employer">📋 המשרות שלי</a>{% endif %}
<a href="/qr">▦ ברקוד לשיתוף</a>
<a href="/lang/{{ 'he' if lang=='en' else 'en' }}" title="Language / שפה">🌐 {{ 'עברית' if lang=='en' else 'EN' }}</a>
{% if me %}<div class="user"><button class="ubtn" type="button" onclick="this.parentNode.classList.toggle('open')">
<span class="av">{{ (me.name or me.email)[0]|upper }}</span><b>{{ me.name or me.email.split('@')[0] }}</b> ▾</button>
<div class="menu"><div class="mail">{{me.email}}</div>
<a href="{{ '/employer' if mode=='employer' else '/me' if mode=='teen' else '/choose' }}">👤 הפרופיל שלי</a>
<a href="/switch/{{ 'teen' if mode=='employer' else 'employer' }}">💼 {{ 'מעבר לחשבון נוער' if mode=='employer' else 'מעבר לחשבון עסק' }}</a>
<form method="post" action="/logout"><button class="out">↪ התנתקות</button></form></div></div>
{% else %}<a href="/login">התחברות</a><a class="btn sm" href="/register">הרשמה</a>{% endif %}
</nav></header>
{% for m in get_flashed_messages() %}<div class="flash">{{m|tr}}</div>{% endfor %}
<main>{% block c %}{% endblock %}</main>
<footer>{{BRAND}} © · המשרות מפורסמות על ידי מעסיקים ופרטיים · אנו פועלים לפי חוק עבודת הנוער, התשי"ג-1953 · נתקלתם במשרה חשודה? דווחו עליה בדף המשרה.</footer>
<script>document.addEventListener('click',e=>{const u=document.querySelector('.user');if(u&&!u.contains(e.target))u.classList.remove('open')});
document.querySelectorAll('form[method="post" i]').forEach(f=>{const i=document.createElement('input');i.type='hidden';i.name='_t';i.value='{{ session.t }}';f.appendChild(i)});</script>
</body></html>"""

T["verify"] = """{% extends 'base' %}{% block c %}<div class="card narrow"><h1>🔐 אימות כניסה</h1>
<p class="mut">שלחנו קוד בן 6 ספרות לאימייל <b dir="ltr">{{email}}</b></p>
{% if dev %}<div class="flash">מצב פיתוח: שירות האימייל לא הוגדר, הקוד מודפס בלוג השרת.</div>{% endif %}
<form class="f" method="post"><input name="code" class="otp" inputmode="numeric" maxlength="6" pattern="[0-9]{6}" autocomplete="one-time-code" autofocus required><button class="btn">אישור</button></form>
<form method="post" action="/resend" style="margin-top:10px"><button class="btn ghost sm">שליחת קוד חדש</button></form></div>{% endblock %}"""

T["qr"] = """{% extends 'base' %}{% block c %}<div class="card narrow qr"><h1>▦ ברקוד לשיתוף</h1>
{% if auto %}<script>location.replace('/qr?u='+encodeURIComponent(location.origin))</script>{% endif %}
{% if svg %}<p class="mut">סרקו כדי לפתוח את {{BRAND}} בטלפון</p><div>{{svg|safe}}</div><p class="mut" dir="ltr" style="word-break:break-all">{{url}}</p>{% endif %}
<form method="get" class="f"><input name="u" dir="ltr" placeholder="הדביקו כאן את כתובת האתר" value="{{url}}"><button class="btn">יצירת ברקוד</button></form>
<button class="btn ghost" style="margin-top:10px" onclick="print()">🖨 הדפסה</button></div>{% endblock %}"""

EX = dict(l.split(" => ") for l in """
תל אביב-יפו => Tel Aviv-Yafo
ירושלים => Jerusalem
חיפה => Haifa
ראשון לציון => Rishon LeZion
פתח תקווה => Petah Tikva
אשדוד => Ashdod
נתניה => Netanya
באר שבע => Beer Sheva
בני ברק => Bnei Brak
רמת גן => Ramat Gan
הרצליה => Herzliya
רעננה => Raanana
כפר סבא => Kfar Saba
מודיעין => Modiin
אור יהודה => Or Yehuda
אחר => Other
בייביסיטר ועזרה לילדים => Babysitting & childcare
דוג ווקר וטיפול בחיות => Dog walking & pet care
שיעורים פרטיים (מתמטיקה, אנגלית...) => Private tutoring (math, English...)
הדרכה וחוגים => Instruction & classes
קמעונאות וחנויות => Retail & shops
מסעדות וקפה – הגשה => Restaurants & cafes - serving
מטבח ופיצה => Kitchen & pizza
שליחויות => Deliveries
משרד ושירות לקוחות => Office & customer service
גינה ועבודות בית => Gardening & housework
קבועה => Permanent
חד-פעמית => One-time
התקבלה => Received
נצפתה => Viewed
זימון לראיון => Interview invite
התקבלת! => Accepted!
לא התקדמנו => Not moving forward
""".strip().splitlines())

EN = dict(l.split(" => ") for l in """
העבודה הראשונה שלך מתחילה כאן => Your first job starts here
העבודה הראשונה שלך => Your First Job
מוצאים עבודה שמתאימה לגיל שלך, לזמן שלך וליכולות שלך => Find a job that fits your age, your time and your skills
🔎 איזו עבודה מחפשים? (בייביסיטר, דוג ווקר, שיעורים פרטיים...) => 🔎 What job are you looking for? (babysitting, dog walking, tutoring...)
>חיפוש< => >Search<
מצאתי עבודה => I found a job
אני מעסיק => I am an employer
משרות פעילות => Active jobs
עסקים מאומתים => Verified employers
בני נוער רשומים => Registered teens
* כל המספרים אמיתיים ומתעדכנים לפי הפעילות בפלטפורמה => * All numbers are real and update with platform activity
🔒 כניסה עם קוד אימות במייל => 🔒 Email code login
✔ מעסיקים מאומתים => ✔ Verified employers
⚖ בהתאם לחוק עבודת הנוער => ⚖ Youth employment law compliant
🚩 דיווח על משרות חשודות => 🚩 Report suspicious jobs
🔥 משרות פופולריות => 🔥 Popular jobs
עדיין לא פורסמו משרות. מעסיקים – היו הראשונים! 🚀 => No jobs posted yet. Employers - be the first! 🚀
איך זה עובד? => How does it work?
1. נרשמים => 1. Sign up
יוצרים פרופיל קצר עם גיל, עיר וכישורים => Create a short profile with age, city and skills
2. מחפשים => 2. Search
מסננים לפי עיר, שכר, שעות וגיל => Filter by city, pay, hours and age
3. מגישים ומתחילים => 3. Apply and start
מגישים מועמדות ועוקבים אחרי הסטטוס => Apply and track your status
חיפוש משרות => Job search
חיפוש חופשי => Free search
>עיר< => >City<
הגיל שלי => My age
סוג עבודה => Job type
שכר מינימלי לשעה => Minimum hourly pay
>שעות< => >Hours<
סוג משרה => Job duration
>הכל< => >All<
>בוקר< => >Morning<
>צהריים< => >Afternoon<
>ערב< => >Evening<
ללא ניסיון => No experience
סופ"ש/חופשות => Weekends/holidays
>סינון< => >Filter<
>נקה< => >Clear<
משרות נמצאו => jobs found
לא נמצאו משרות לפי הסינון. נסו להרחיב את החיפוש. => No jobs match your filters. Try widening your search.
✔ מאומת => ✔ Verified
לא מאומת => Unverified
'לשעה' => 'per hour'
'לחודש' => 'per month'
מגיל => from age
עדיין ללא דירוגים => No ratings yet
דרישות: => Requirements:
ללא דרישות מיוחדות => No special requirements
לפרטים ולהגשת מועמדות => Details & apply
✔ מעסיק מאומת => ✔ Verified employer
⚠ מעסיק שטרם אומת – היזהרו ואל תמסרו מידע אישי => ⚠ Unverified employer - be careful and do not share personal info
💰 שכר: => 💰 Pay:
🎂 גיל מינימלי: => 🎂 Minimum age:
🕒 שעות: => 🕒 Hours:
📅 ימים: => 📅 Days:
לפי תיאום => By arrangement
<b>סוג:</b> => <b>Type:</b>
<b>ניסיון:</b> => <b>Experience:</b>
'לא נדרש' => 'Not required'
'נדרש' => 'Required'
על העסק: => About the employer:
לא צוין => Not specified
הגשת מועמדות · סטטוס: => Applied · Status:
הודעה קצרה למעסיק (לא לכתוב טלפון/כתובת) => Short message to the employer (no phone/address)
>הגשת מועמדות< => >Apply<
💔 הסר ממועדפים => 💔 Remove from favorites
❤ שמור למועדפים => ❤ Save to favorites
התחברו כדי להגיש מועמדות => Log in to apply
עברו למצב "מחפש עבודה" בתפריט כדי להגיש מועמדות. => Switch to a teen account in the menu to apply.
⭐ דירוג => ⭐ Rating
★★★★★ מעולה => ★★★★★ Excellent
★★★★ טוב => ★★★★ Good
★★★ סביר => ★★★ Okay
★★ לא טוב => ★★ Poor
★ גרוע => ★ Bad
איך הייתה החוויה? (אופציונלי) => How was the experience? (optional)
שליחת דירוג => Submit rating
דירוגים => ratings
🚩 דיווח על משרה חשודה => 🚩 Report a suspicious job
מה חשוד? => What looks suspicious?
שליחת דיווח => Send report
התחברות => Log in
הרשמה => Sign up
שם (או כינוי – לא חובה שם מלא) => Name (or nickname - full name not required)
אימייל => Email
סיסמה => Password
יצירת חשבון => Create account
כניסה => Enter
יש כבר חשבון? => Already have an account?
חדשים כאן? => New here?
איך תרצו להשתמש ב- => How would you like to use 
אפשר להחליף מצב בכל רגע מהתפריט העליון => You can switch account type at any time from the top menu
אני מחפש/ת עבודה => I am looking for a job
אני מפרסם/ת עבודה (עסק או פרטי) => I am offering a job (business or private)
👤 האזור האישי => 👤 My account
שם / כינוי => Name / nickname
גיל (14–21) => Age (14–21)
כישורים ותחומי עניין => Skills and interests
למשל: אנגלית, עבודה עם ילדים, מחשבים => e.g. English, working with kids, computers
ניסיון תעסוקתי (אם יש) => Work experience (if any)
קצת עליי => About me
🔒 לא מציגים טלפון או כתובת. אל תכתבו אותם בפרופיל. => 🔒 We never show phone numbers or addresses. Do not write them in your profile.
>שמירה< => >Save<
❤ משרות שמורות => ❤ Saved jobs
אין עדיין משרות שמורות => No saved jobs yet
עוד לא הגשת מועמדות => You have not applied yet
המועמדויות שלי => My applications
🏢 אזור מעסיקים => 🏢 Employer area
פרופיל עסקי => Business / work profile
ממתין לאימות => Pending verification
שם העסק (עבודה) => Business name (job)
למשל: בייביסיטר בראשון / דוג ווקר / מורה פרטי למתמטיקה / פיצה דומינו => e.g. Babysitter / dog walker / math tutor / pizza shop
אימות: מספר עוסק/ח"פ (לעסק) או "פרטי" => Verification: business ID (for businesses) or "private"
על העסק / על העבודה => About the business / job
שמירת פרופיל => Save profile
+ פרסום משרה חדשה => + Post a new job
המשרות שלי => My jobs
(לא פעילה) => (inactive)
מועמדים – => Applicants –
מועמדים => applicants
>עריכה< => >Edit<
'השהיה' => 'Pause'
'הפעלה' => 'Activate'
עוד לא פרסמתם משרות => You have not posted any jobs yet
עריכת משרה => Edit job
פרסום משרה חדשה => Post a new job
שם המשרה => Job title
שכר (₪) => Pay (₪)
תשלום לפי => Paid per
>שעה< => >Hour<
>חודש< => >Month<
ימים בשבוע => Days per week
גיל מינימלי (ומעלה) => Minimum age (and up)
משעה => From hour
עד שעה => Until hour
>ימים< => >Days<
א'-ה' / סופ״ש => Sun-Thu / weekends
מתאים ללא ניסיון => Suitable with no experience
סופי שבוע / חופשות => Weekends / holidays
תיאור המשרה => Job description
>דרישות< => >Requirements<
שמירה ופרסום => Save and publish
גיל מינימלי לסוג עבודה זה: => Minimum age for this job type:
⚖ לפי חוק עבודת הנוער: מגיל 14, עד 8 שעות ביום, עד גיל 16 לא בין 20:00–06:00, ועד 18 לא בין 22:00–06:00, שכר לא נמוך מהמינימום לגיל, ובעבודות מסוכנות (כמו מטבח ושליחויות) הגיל המינימלי באתר גבוה יותר. => ⚖ Youth employment law: from age 14, up to 8 hours a day, no work 20:00-06:00 under 16 and 22:00-06:00 under 18, pay not below the legal minimum for the age, and for risky jobs (such as kitchen and deliveries) the minimum age on the site is higher.
· גיל => · age
כישורים: => Skills:
הודעה: => Message:
עדיין אין מועמדים => No applicants yet
→ חזרה => → Back
>עדכון< => >Update<
המשרות מפורסמות על ידי מעסיקים ופרטיים => Jobs are posted by employers and individuals
אנו פועלים לפי חוק עבודת הנוער, התשי"ג-1953 => We follow the Youth Employment Law, 1953
נתקלתם במשרה חשודה? דווחו עליה בדף המשרה. => Found a suspicious job? Report it on the job page.
חיפוש עבודה => Find jobs
ברקוד לשיתוף => Share QR code
הפרופיל שלי => My profile
מעבר לחשבון נוער => Switch to Teen account
מעבר לחשבון עסק => Switch to Business account
התנתקות => Log out
אימות כניסה => Login verification
שלחנו קוד בן 6 ספרות לאימייל => We sent a 6-digit code to
מצב פיתוח: שירות האימייל לא הוגדר, הקוד מודפס בלוג השרת. => Dev mode: email is not configured, the code is printed in the server log.
אישור => Confirm
שליחת קוד חדש => Send a new code
סרקו כדי לפתוח את => Scan to open
בטלפון => on your phone
הדביקו כאן את כתובת האתר => Paste the site address here
יצירת ברקוד => Create QR code
🖨 הדפסה => 🖨 Print
הפעולה הזו זמינה במצב אחר – אפשר להחליף מצב בכל רגע מהתפריט. => This action is available in the other account type - switch any time from the menu.
האתר מיועד לגילאי 14–21. => This site is for ages 14–21.
הפרופיל נשמר ✔ => Profile saved ✔
כדי להגיש מועמדות יש להשלים גיל בפרופיל. => Please add your age to your profile before applying.
המשרה מיועדת מגיל => This job is for age
כבר הגשת מועמדות למשרה הזו. => You already applied to this job.
המועמדות נשלחה! 🎉 אפשר לעקוב אחרי הסטטוס באזור האישי. => Application sent! 🎉 Track the status in your account.
אי אפשר לדרג משרה של עצמך. => You cannot rate your own job.
תודה על הדירוג! => Thanks for your rating!
הדיווח התקבל ויבדק על ידי הצוות. תודה ששומרים על הקהילה 💙 => Report received and will be reviewed by our team. Thanks for keeping the community safe 💙
קודם יוצרים פרופיל עסקי. => Create a business profile first.
פרופיל העסק נשמר ✔ – הבקשה לאימות הועברה לצוות. => Profile saved ✔ - the verification request was sent to our team.
המשרה נשמרה ✔ => Job saved ✔
כתובת האימייל אינה תקינה. => Invalid email address.
הסיסמה חייבת להכיל לפחות 8 תווים. => Password must be at least 8 characters.
האימייל כבר רשום – נסו להתחבר. => Email already registered - try logging in.
יותר מדי ניסיונות. נסו שוב בעוד כמה דקות. => Too many attempts. Try again in a few minutes.
אימייל או סיסמה שגויים. => Wrong email or password.
הקוד פג תוקף. התחברו מחדש. => Code expired. Please log in again.
קוד שגוי. => Wrong code.
אפשר לבקש קוד חדש רק אחרי 30 שניות. => You can request a new code after 30 seconds.
נשלח קוד חדש. => A new code was sent.
הדף פג תוקף – נסו שוב. => The page expired - please try again.
גיל העסקה מינימלי בישראל הוא 14. => The minimum employment age in Israel is 14.
משמרת של נוער לא יכולה לעלות על 8 שעות ביום. => A youth shift cannot exceed 8 hours a day.
מתחת לגיל 16 אסור לעבוד בין 20:00 ל-06:00. => Under 16, work between 20:00 and 06:00 is not allowed.
עד גיל 18 אסור לעבוד בין 22:00 ל-06:00. => Under 18, work between 22:00 and 06:00 is not allowed.
שעות העבודה אינן תקינות. => Invalid working hours.
יש למלא מספרים תקינים בגיל, שכר ושעות. => Please enter valid numbers for age, pay and hours.
יש לכתוב תיאור משרה של לפחות 20 תווים. => Please write a job description of at least 20 characters.
בעבודה מסוג זה הגיל המינימלי באתר הוא => For this job type the minimum age on the site is
השכר החודשי נמוך מהמינימום החוקי לשעות שהוגדרו => Monthly pay is below the legal minimum for the defined hours
השכר נמוך מהמינימום החוקי לגיל זה => Pay is below the legal minimum for this age
(כ- => (about 
₪ לחודש => ₪ per month
₪ לשעה => ₪ per hour
""".strip().splitlines())

T_all = dict(T)
T_all.update({"en/" + n: en(t.replace("extends 'base'", "extends 'en/base'").replace("include 'card'", "include 'en/card'")) for n, t in T.items()})
app.jinja_loader = DictLoader(T_all)

if __name__ == "__main__":      # הרצה מקומית לבדיקה; בשרת מריצים עם gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
