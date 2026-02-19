import os, json, sqlite3, asyncio, base64, re, logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Optional, Tuple

import requests, qrcode
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, Request
import uvicorn

# ---------------- LOGS ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vip-bot")
log.info("APP SUBIU")

# ---------------- ENV ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_TOKEN = os.getenv("INVICTUS_API_TOKEN")          # token do gateway (mantive o nome)
POSTBACK_URL = os.getenv("POSTBACK_URL")             # https://SEUAPP.railway.app/invictus/postback

PRICE_CENTS = int(os.getenv("PRICE_CENTS", "599"))   # exemplo: R$ 5,99
OFFER_HASH = os.getenv("OFFER_HASH", "")
PRODUCT_HASH = os.getenv("PRODUCT_HASH", "")

FIXED_NAME = os.getenv("FIXED_NAME", "Cliente VIP")
FIXED_EMAIL = os.getenv("FIXED_EMAIL", "cliente@exemplo.com")
FIXED_PHONE = os.getenv("FIXED_PHONE", "11999999999")
FIXED_DOCUMENT = os.getenv("FIXED_DOCUMENT", "00000000000")

GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK")   # link fixo do grupo (mais simples)
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")           # -100... (opcional, link temporário)

APP_HOST = "0.0.0.0"
APP_PORT = int(os.getenv("PORT", "8080"))            # Railway costuma usar PORT
DB_PATH = "db.sqlite3"

missing = []
for k, v in [
    ("BOT_TOKEN", BOT_TOKEN),
    ("INVICTUS_API_TOKEN", API_TOKEN),
    ("POSTBACK_URL", POSTBACK_URL),
    ("OFFER_HASH", OFFER_HASH),
    ("PRODUCT_HASH", PRODUCT_HASH),
]:
    if not v:
        missing.append(k)
if missing:
    raise RuntimeError(f"Faltam env vars: {', '.join(missing)}")

# ---------------- DB ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'inactive',
            expires_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            tx_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            raw_response TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_user(telegram_id: int) -> Tuple[str, Optional[str]]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT status, expires_at FROM users WHERE telegram_id=?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return "inactive", None
    return row[0], row[1]

def set_user_active(telegram_id: int) -> datetime:
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    conn = db()
    conn.execute(
        "INSERT INTO users(telegram_id, status, expires_at) VALUES(?,?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET status='active', expires_at=excluded.expires_at",
        (telegram_id, "active", expires.isoformat()),
    )
    conn.commit()
    conn.close()
    return expires

def set_user_inactive(telegram_id: int):
    conn = db()
    conn.execute(
        "INSERT INTO users(telegram_id, status, expires_at) VALUES(?,?,NULL) "
        "ON CONFLICT(telegram_id) DO UPDATE SET status='inactive', expires_at=NULL",
        (telegram_id, "inactive"),
    )
    conn.commit()
    conn.close()

def save_tx(telegram_id: int, tx_id: Optional[str], status: str, raw: dict):
    conn = db()
    conn.execute(
        "INSERT INTO transactions(telegram_id, tx_id, status, created_at, raw_response) VALUES(?,?,?,?,?)",
        (telegram_id, tx_id, status, datetime.now(timezone.utc).isoformat(), json.dumps(raw, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()

def find_user_by_tx(tx_id: str) -> Optional[int]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM transactions WHERE tx_id=? ORDER BY id DESC LIMIT 1", (tx_id,))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else None

# ---------------- Extractors ----------------
EMV_START = "000201"

def walk_values(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk_values(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from walk_values(it)
    else:
        yield obj

def find_emv(resp_json: dict, raw_text: str) -> Optional[str]:
    for v in walk_values(resp_json):
        if isinstance(v, str):
            s = v.strip()
            if EMV_START in s:
                if s.startswith(EMV_START) and len(s) > 50:
                    return s
                i = s.find(EMV_START)
                cand = s[i:].strip()
                if len(cand) > 50:
                    return cand
    if raw_text and EMV_START in raw_text:
        i = raw_text.find(EMV_START)
        cand = raw_text[i:i+3000].split('"')[0].split("\\")[0].strip()
        if len(cand) > 50:
            return cand
    return None

def find_qr_base64(resp_json: dict, raw_text: str) -> Optional[str]:
    for v in walk_values(resp_json):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("data:image/") and "base64," in s:
                return s.split("base64,", 1)[-1]
            if len(s) > 300 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r" for c in s[:120]):
                return s
    m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\n\r]+)", raw_text or "")
    return m.group(1) if m else None

def qr_from_emv(emv: str) -> BytesIO:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(emv)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = BytesIO()
    bio.name = "pix_qr.png"
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio

# ---------------- Gateway call (ajuste URL se seu provedor não for Invictus) ----------------
def create_pix_transaction(telegram_id: int):
    url = f"https://api.invictuspay.app.br/api/public/v1/transactions?api_token={API_TOKEN}&postback_url={POSTBACK_URL}"
    payload = {
        "amount": PRICE_CENTS,
        "offer_hash": OFFER_HASH,
        "payment_method": "pix",
        "customer": {
            "name": FIXED_NAME,
            "email": FIXED_EMAIL,
            "phone_number": FIXED_PHONE,
            "document": FIXED_DOCUMENT
        },
        "cart": [{
            "product_hash": PRODUCT_HASH,
            "title": "Assinatura VIP - 30 dias",
            "price": PRICE_CENTS,
            "quantity": 1,
            "operation_type": 1,
            "tangible": False
        }],
        "expire_in_days": 1,
        "tracking": {"telegram_id": telegram_id}
    }

    r = requests.post(url, json=payload, timeout=30)
    raw = r.text or ""
    log.info(f"GW status={r.status_code}")
    log.info(f"GW raw_first_1200={raw[:1200]}")
    r.raise_for_status()

    try:
        resp = r.json()
    except Exception:
        resp = {"_non_json": raw[:2000]}

    log.info("GW_JSON: " + json.dumps(resp, ensure_ascii=False)[:2000])

    tx_id = str(resp.get("id") or resp.get("transaction_id") or resp.get("uuid") or (resp.get("data") or {}).get("id") or "").strip() or None
    emv = find_emv(resp, raw)
    qr_b64 = find_qr_base64(resp, raw)
    return resp, tx_id, emv, qr_b64

# ---------------- Telegram ----------------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Assinar / Renovar (30 dias)", callback_data="pay")],
        [InlineKeyboardButton(text="📌 Ver assinatura", callback_data="status")]
    ])

def fmt_dt(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        f"🔒 Área VIP (30 dias)\n"
        f"💰 Valor: R$ {PRICE_CENTS/100:.2f}\n\n"
        f"Use os botões:",
        reply_markup=kb_main()
    )

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    status, expires = get_user(message.from_user.id)
    if status == "active" and expires:
        await message.answer(f"✅ Assinatura ATIVA\n📅 Válida até: {fmt_dt(expires)}\n\nPara renovar, use /start.")
    else:
        await message.answer("⚠️ Você está SEM assinatura ativa.\nUse /start para assinar/renovar.")

@dp.callback_query(lambda c: c.data == "status")
async def status_cb(call: types.CallbackQuery):
    status, expires = get_user(call.from_user.id)
    if status == "active" and expires:
        await call.message.answer(f"✅ Assinatura ATIVA\n📅 Válida até: {fmt_dt(expires)}")
    else:
        await call.message.answer("⚠️ Você está SEM assinatura ativa.\nClique em Assinar/Renovar.")
    await call.answer()

@dp.callback_query(lambda c: c.data == "pay")
async def pay_cb(call: types.CallbackQuery):
    telegram_id = call.from_user.id
    try:
        resp, tx_id, emv, qr_b64 = create_pix_transaction(telegram_id)
        save_tx(telegram_id, tx_id, "pending", resp)

        # 1) mensagem “segue o pix copia e cola…”
        if emv:
            await call.message.answer("✅ Segue o Pix Copia e Cola para fazer o pagamento:")
            await call.message.answer(f"`{emv}`", parse_mode="Markdown")
        else:
            await call.message.answer("⚠️ Não encontrei o Pix Copia e Cola na resposta do gateway. Veja logs GW_JSON.")

        # 2) depois manda o QR
        await call.message.answer("📌 Aqui está o QR Code se preferir:")

        if qr_b64:
            try:
                img_bytes = base64.b64decode(qr_b64)
                bio = BytesIO(img_bytes)
                bio.name = "pix_qr.png"
                bio.seek(0)
                await bot.send_photo(call.message.chat.id, photo=bio)
            except Exception:
                if emv:
                    await bot.send_photo(call.message.chat.id, photo=qr_from_emv(emv))
        else:
            if emv:
                await bot.send_photo(call.message.chat.id, photo=qr_from_emv(emv))

        await call.message.answer("⏳ Assim que o pagamento for confirmado, o acesso será liberado automaticamente.")
        await call.answer()

    except Exception as e:
        log.error(f"PAY_ERR: {e}")
        await call.message.answer("❌ Erro ao gerar Pix. Abra Railway → Logs (GW status/raw/GW_JSON).")
        await call.answer()

# ---------------- Webhook / Postback ----------------
app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/invictus/postback")
async def postback(request: Request):
    payload = await request.json()
    log.info("POSTBACK_JSON: " + json.dumps(payload, ensure_ascii=False)[:2000])

    tx_id = str(payload.get("id") or payload.get("transaction_id") or payload.get("uuid") or "").strip()
    status = (payload.get("status") or payload.get("payment_status") or payload.get("state") or "").strip().lower()

    if (not tx_id) and isinstance(payload.get("data"), dict):
        d = payload["data"]
        tx_id = str(d.get("id") or d.get("transaction_id") or d.get("uuid") or "").strip()
        status = (d.get("status") or d.get("payment_status") or d.get("state") or "").strip().lower()

    approved = {"approved", "paid", "confirmed", "completed", "success", "aprovado", "pago"}

    if tx_id and status in approved:
        telegram_id = find_user_by_tx(tx_id)

        # fallback tracking.telegram_id
        if not telegram_id:
            tracking = payload.get("tracking") or (payload.get("data") or {}).get("tracking")
            if isinstance(tracking, dict) and tracking.get("telegram_id"):
                telegram_id = int(tracking["telegram_id"])

        if telegram_id:
            expires = set_user_active(int(telegram_id))

            # entrega acesso
            if GROUP_INVITE_LINK:
                await bot.send_message(
                    int(telegram_id),
                    "✅ Pagamento confirmado!\n\n"
                    f"Aqui está seu acesso:\n{GROUP_INVITE_LINK}\n\n"
                    f"📅 Válido até: {expires.strftime('%Y-%m-%d %H:%M UTC')}"
                )
            elif GROUP_CHAT_ID:
                invite = await bot.create_chat_invite_link(
                    chat_id=int(GROUP_CHAT_ID),
                    member_limit=1,
                    expire_date=int((datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp())
                )
                await bot.send_message(
                    int(telegram_id),
                    "✅ Pagamento confirmado!\n\n"
                    f"Link (expira em 30 min):\n{invite.invite_link}\n\n"
                    f"📅 Válido até: {expires.strftime('%Y-%m-%d %H:%M UTC')}"
                )
            else:
                await bot.send_message(int(telegram_id), "✅ Pago, mas falta configurar GROUP_INVITE_LINK ou GROUP_CHAT_ID.")
    return {"ok": True}

# ---------------- Expiração ----------------
async def expiration_job():
    while True:
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("SELECT telegram_id, expires_at FROM users WHERE status='active' AND expires_at IS NOT NULL")
            rows = cur.fetchall()
            conn.close()

            now = datetime.now(timezone.utc)
            for telegram_id, expires_at in rows:
                try:
                    exp = datetime.fromisoformat(expires_at)
                    if exp < now:
                        set_user_inactive(int(telegram_id))
                        await bot.send_message(
                            int(telegram_id),
                            "⚠️ Sua assinatura expirou.\n\n"
                            f"💰 Renovação: R$ {PRICE_CENTS/100:.2f} / 30 dias\n"
                            "Use /start para gerar um novo Pix."
                        )
                except Exception:
                    continue
        except Exception as e:
            log.error(f"EXP_JOB_ERR: {e}")

        await asyncio.sleep(600)

# ---------------- Run all ----------------
async def run_all():
    init_db()
    config = uvicorn.Config(app, host="0.0.0.0", port=APP_PORT, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(
        dp.start_polling(bot),
        expiration_job(),
        server.serve()
    )

if __name__ == "__main__":
    asyncio.run(run_all())
