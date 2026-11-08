# TeenJob – פלטפורמת עבודה לבני נוער

Flask + PostgreSQL (Neon) + Resend. מיועד להרצה ב-Render.

## הגדרות ב-Render
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app --workers 2 --threads 4 --timeout 60`
- **Health Check Path:** `/health`

## משתני סביבה (Environment Variables)
| שם | תיאור |
|---|---|
| `DATABASE_URL` | כתובת החיבור ל-PostgreSQL (מ-Neon) |
| `SECRET_KEY` | מחרוזת אקראית ארוכה (למשל 40+ תווים) |
| `ADMIN_CODE` | קוד כניסה לדף הניהול `/admin` |
| `RESEND_API_KEY` | מפתח API מ-Resend לשליחת קודי אימות |
| `EMAIL_FROM` | למשל `TeenJob <noreply@your-domain.co.il>` (דורש דומיין מאומת ב-Resend) |
| `BRAND` | (לא חובה) שם האתר |

אסור לשים סודות בקוד או ב-GitHub – רק במשתני הסביבה.

## הרצה מקומית לבדיקה
`pip install -r requirements.txt && python app.py` (משתמש ב-SQLite, וקודי האימות מודפסים בטרמינל).
