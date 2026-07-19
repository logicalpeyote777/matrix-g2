#!/usr/bin/env python3
"""matrix_bridge.py — Matrix client per Even G2.

HUD state machine:
  ROOMS   → swipe=cursore · tap=apri · 2x=HUD off       (resa: session picker TM)
  CHAT    → swipe=scorri msg · tap=avvia mic · 2x=torna lista
  REC     → tap=stop → PROC   (chat visibile + live caption del parziale STT in basso,
                               status in riga 9; parziali ogni ~2.5s, skip-if-busy)
  PROC    → whisper trascrive → CONFIRM   (status in riga 9 + fx="dots" animato dall'app)
  CONFIRM → swipe=scorri testo · tap=invia · 2x=scarta  (resa: dialog box via campo "dialog")
  OFF     → tap=torna ROOMS

WS protocol
  App→Bridge: PCM bytes | {"t":"login",...} | {"t":"logout"} | {"t":"gesture","g":"..."}
  Bridge→App: {"t":"login_ok"} | {"t":"login_error"} | {"t":"logged_out"}
               {"t":"verify_ok"} | {"t":"verify_err"}
               {"t":"hud","title":"...","lines":[...],"footer":"...",
                "fx":"type"|"dots",                       # opzionale
                "dialog":{"text":[...],"opts":[...]}}      # opzionale, solo CONFIRM
               {"t":"notif","text":"..."} | {"t":"mic_start"} | {"t":"mic_stop"}
"""
import asyncio, base64, hashlib, hmac, io, json, os, struct, textwrap, time, unicodedata, uuid, wave
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto

import numpy as np
import websockets
import nio
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

HOST, PORT  = "0.0.0.0", 8792
# HUD: container unico 576×288 border 3, griglia testo 44×9 (font FISSO firmware).
# Il bridge riempie le righe 1..7 (contenuto, W=44); l'app disegna il chrome:
# riga 8 = separatore 44×"-", riga 9 = status line "title ... footer" (right-aligned)
# coi testi title/footer del payload. Solo ASCII in posizioni allineate; "●" ammesso
# SOLO a inizio riga 9 (title). Niente "…" U+2026: ellissi sempre "..." ASCII.
W           = 44      # caratteri per riga di contenuto
H           = 7       # righe di contenuto visibili (riga 8-9 = chrome dell'app)
WHISPER_MODEL = os.environ.get("BRIDGE_WHISPER_MODEL", "medium")  # STT: medium (bilanciato)
_BPS        = 16000 * 2
REC_MAX_SEC = 120.0   # tetto di sicurezza: stop registrazione (start/stop è a tap)
LIVE_WIN_SEC  = 2.5   # live caption: finestra minima tra due trascrizioni parziali
LIVE_TAIL_SEC = 20.0  # live caption: trascrive solo la coda (sul totale >30s è lento)
SCROLL_DEB  = 0.35   # seconds between scroll events
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CREDS_FILE  = os.environ.get("BRIDGE_CREDS_FILE", os.path.join(_BASE_DIR, "matrix_creds.json"))
STORE_PATH  = os.environ.get("BRIDGE_STORE_PATH", os.path.join(_BASE_DIR, "nio_store"))
# lazy_load_members: senza, il sync iniziale full_state scarica lo stato di TUTTE
# le stanze e tiene l'HUD su "connessione…" per 30-60s
SYNC_FILTER = {"room": {"state": {"lazy_load_members": True},
                        "timeline": {"limit": 30}}}


# ── State ─────────────────────────────────────────────────────────────────────
class S(Enum):
    ROOMS = auto()
    CHAT  = auto()
    REC   = auto()
    PROC  = auto()   # whisper sta trascrivendo (tra lo stop e la conferma)
    CONFIRM = auto()
    OFF   = auto()

_state:           S     = S.ROOMS
_pending_text:    str   = ""
_confirm_scroll:  int   = 0      # scroll del testo trascritto in CONFIRM
_security_key:    str   = ""     # recovery key/passphrase (per ri-import backup on-demand)
_resolve_t:       float = 0.0    # throttle del ri-import backup su [cifrato]
_room_scroll:     int   = 0
_msg_scroll:      int   = 0
_current_room_id: str   = ""    # tracks cursor by room ID (stable across reorders)
_last_scroll_t:   float = 0.0   # debounce timestamp
_capturing:       bool  = False
_stt_busy:        bool  = False
_pcm:             bytearray = bytearray()
_live_text:       str   = ""     # parziale STT mostrato in REC (live caption)
_live_gen:        int   = 0      # generazione REC: scarta parziali in volo dopo lo stop
_matrix           = None
_sync_task        = None
_rooms:           dict  = {}
_clients:         set   = set()
_whisper_model    = None
_logging_in:      bool  = False
_confirm_t:       float = 0.0    # quando siamo entrati in CONFIRM (gate anti tap accodati)
_new_in_cur:      bool  = False  # ultimo _ingest ha portato un msg nuovo nella chat aperta


# ── STT (local only) ──────────────────────────────────────────────────────────
def _load_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                      cpu_threads=min(8, os.cpu_count() or 4))
    return _whisper_model

def stt(pcm: bytes) -> str:
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segs, _ = _load_model().transcribe(
        audio, language="it", beam_size=5,
        vad_filter=True,                    # scarta il silenzio → meno allucinazioni
        condition_on_previous_text=False,   # niente drift dal contesto precedente
        initial_prompt="Messaggio vocale in italiano, con punteggiatura.")
    return " ".join(s.text for s in segs).strip()

def _stt_partial(pcm: bytes) -> str:
    """STT del parziale per il live caption: greedy (beam 1), niente contesto.
    Non e' la finale: basta che si legga mentre si detta."""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segs, _ = _load_model().transcribe(
        audio, language="it", beam_size=1,
        vad_filter=True, condition_on_previous_text=False)
    return " ".join(s.text for s in segs).strip()


# ── Room state ────────────────────────────────────────────────────────────────
@dataclass
class RoomState:
    room_id:  str
    name:     str
    messages: deque = field(default_factory=lambda: deque(maxlen=40))
    unread:   int   = 0
    last_ts:  int   = 0

def _sorted_rooms():
    return [r.room_id for r in sorted(
        _rooms.values(), key=lambda r: (-(r.unread > 0), -r.unread, -r.last_ts))]

def _room_idx() -> int:
    """Cursor index in sorted order — stable across reorders."""
    global _current_room_id
    order = _sorted_rooms()
    if not order: return 0
    if _current_room_id not in _rooms:
        _current_room_id = order[0]
    try: return order.index(_current_room_id)
    except ValueError: _current_room_id = order[0]; return 0

def _cur():
    o = _sorted_rooms()
    return _rooms.get(o[_room_idx()]) if o else None

def _sender(client, room_id: str, uid: str) -> str:
    room   = client.rooms.get(room_id)
    member = room.users.get(uid) if room else None
    return member.display_name if (member and member.display_name) \
           else uid.split(":")[0].lstrip("@")


# ── HUD renders ───────────────────────────────────────────────────────────────
# Stile Terminal Mode: ogni render ritorna un dict {"lines","title","footer"}
# (+ "dialog" solo in CONFIRM). lines = contenuto righe 1..7; title/footer vanno
# nella status line (riga 9) disegnata dall'app.
# Grammatica prefissi (colonna 1-2 fissa, testo da col 3, hanging indent 2):
#   "/ " nostro · "? " unread · "> " cursore · ". " voce letta · "  " arrivo/wrap

def _t(s: str, n: int) -> str: return s if len(s) <= n else s[:n]   # taglio secco, no ellissi

def _ascii(s: str) -> str:
    """Font firmware: accenti → vocale piatta, resto non-ASCII scartato."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()

def _live_caption_rows() -> list:
    """Parziale live in REC: ultime 1-2 righe wrappate a W-2, prefisso '> '
    sulla prima e hanging indent 2 sulla seconda. Sempre <= 44 char."""
    if not _live_text: return []
    wrapped = textwrap.wrap(_ascii(_live_text), W - 2)
    if not wrapped: return []
    rows = wrapped[-2:]
    return ["> " + rows[0]] + ["  " + l for l in rows[1:]]

def _chat_lines(rs) -> list:
    out = []
    for m in rs.messages:
        sender, body = m[0], m[1]
        mine = bool(m[3]) if len(m) > 3 else False
        wrapped = textwrap.wrap(body if mine else f"{sender}: {body}", W - 2) or [""]
        out.append(("/ " if mine else "  ") + wrapped[0])
        out.extend("  " + l for l in wrapped[1:])
    return out

def _chat_window() -> list:
    rs = _cur()
    return _chat_lines(rs)[_msg_scroll: _msg_scroll + H] if rs else []

def _render_rooms():
    order = _sorted_rooms()
    if not order:
        return {"lines": ["", "  . no chats"],
                "title": "/ Select chat", "footer": "[Tap to open]"}
    cur = _room_idx()
    rows = []
    for i, rid in enumerate(order):
        rs  = _rooms[rid]
        unr = str(rs.unread) if rs.unread else ""
        if i == cur:
            # box su 1 riga, 44 char esatti: "[ ? nome ....... n ]" (unread → col 42)
            mark  = "?" if rs.unread else " "
            inner = W - 6                                  # col 5..42
            name  = _t(rs.name, inner - len(unr) - 1 if unr else inner)
            pad   = inner - len(name) - len(unr)
            rows.append(f"[ {mark} {name}{' ' * pad}{unr} ]")
        else:
            mark = "?" if rs.unread else "."
            if unr:                                        # unread termina a col 42
                name = _t(rs.name, W - 7 - len(unr))
                rows.append(f"  {mark} {name}{' ' * (W - 6 - len(name) - len(unr))}{unr}")
            else:
                rows.append(f"  {mark} {_t(rs.name, W - 4)}")
    sc = max(0, min(_room_scroll, cur, len(rows) - H))
    return {"lines": rows[sc: sc + H],
            "title": "/ Select chat", "footer": "[Tap to open]"}

def _render_chat(rs):
    # niente header in alto: il nome stanza vive nella status line (riga 9)
    return {"lines": _chat_lines(rs)[_msg_scroll: _msg_scroll + H],
            "title": "/ #" + _t(rs.name, 27), "footer": "[Tap to talk]"}

def _render_rec():
    # REC: chat visibile sopra, live caption (parziale STT) ancorato in fondo
    cap = _live_caption_rows()
    if not cap:
        return {"lines": _chat_window(),
                "title": "● Listening...", "footer": "[Tap to finish]"}
    room = H - len(cap)
    chat = _chat_window()[-room:]            # tiene i msg piu' recenti della finestra
    chat += [""] * (room - len(chat))        # caption sempre nelle righe basse
    return {"lines": chat + cap,
            "title": "● Listening...", "footer": "[Tap to finish]"}

def _render_proc():
    # PROC idem; i pallini li anima l'app in locale (fx="dots" sul frame d'ingresso)
    return {"lines": _chat_window(),
            "title": ".   Transcribing...", "footer": ""}

def _dialog_lines() -> list:
    """Testo dettato wrappato per il dialog box (36 char utili)."""
    return textwrap.wrap(_pending_text, 36) or [""]

def _confirm_lines() -> list:
    """Render legacy del CONFIRM: fallback per app senza supporto 'dialog'."""
    return textwrap.wrap(f"> {_pending_text}", W) or [""]

def _render_confirm():
    rs = _cur()
    header = f"#{_t(rs.name, W - 1)}" if rs else ""
    body   = _confirm_lines()[_confirm_scroll: _confirm_scroll + H - 1]
    text   = _dialog_lines()[_confirm_scroll: _confirm_scroll + 2]
    while len(text) < 2: text.append("")     # sempre 2 righe (la 2a vuota se corto)
    return {"lines": [header] + body,        # legacy: app vecchia mostra questo
            "title": "/ Confirm", "footer": "[Tap send 2tap cancel]",
            "dialog": {"text": text, "opts": ["Send", "Cancel"]}}

def _broadcast(msg: dict):
    for q in _clients: q.put_nowait(msg)

def _hud_payload() -> dict:
    if   _state == S.ROOMS:   return _render_rooms()
    elif _state == S.CHAT:
        rs = _cur();          return _render_chat(rs) if rs else _render_rooms()
    elif _state == S.REC:     return _render_rec()
    elif _state == S.PROC:    return _render_proc()
    elif _state == S.CONFIRM: return _render_confirm()
    return {"lines": [], "title": "", "footer": ""}   # OFF → l'app spegne l'HUD

def _push_hud(fx=None):
    # in PROC ogni re-push (es. sync arrivato durante la trascrizione) deve portare
    # fx="dots": l'app ferma l'animazione a OGNI payload e la riavvia solo con fx
    if fx is None and _state == S.PROC: fx = "dots"
    p = _hud_payload()
    msg = {"t": "hud", "title": p["title"], "lines": p["lines"], "footer": p["footer"]}
    if "dialog" in p: msg["dialog"] = p["dialog"]
    if fx: msg["fx"] = fx
    _broadcast(msg)

def _hud_text(*lines, title="", footer=""):
    """Frame HUD 'di sistema' (boot / errori)."""
    _broadcast({"t": "hud", "title": title, "lines": list(lines), "footer": footer})


# ── Matrix sync loop ──────────────────────────────────────────────────────────
def _ingest(client, resp, first: bool) -> bool:
    """Merge di una risposta sync in _rooms. True se qualcosa è cambiato."""
    global _msg_scroll, _new_in_cur
    _new_in_cur = False
    order  = _sorted_rooms()
    cur_id = order[_room_idx()] if order else None
    own     = client.user_id
    changed = False
    for room_id, joined in resp.rooms.join.items():
        room = client.rooms.get(room_id)
        name = (room.display_name or room_id) if room else room_id
        if room_id not in _rooms: _rooms[room_id] = RoomState(room_id=room_id, name=name)
        rs = _rooms[room_id]; rs.name = name
        for ev in joined.timeline.events:
            ts = getattr(ev, 'server_timestamp', None)
            if ts: rs.last_ts = ts; changed = True
            enc_ev = None   # tenuto da parte per ri-decrypt dopo l'import del backup
            if isinstance(ev, nio.RoomMessageText):
                body = ev.body
            elif isinstance(ev, nio.MegolmEvent):
                try:
                    dec  = client.decrypt_event(ev)
                    if isinstance(dec, nio.RoomMessageText): body = dec.body
                    else: body = "[cifrato]"; enc_ev = ev
                except Exception:
                    body = "[cifrato]"; enc_ev = ev
            else:
                continue
            rs.messages.append([_sender(client, room_id, ev.sender), body, enc_ev,
                                ev.sender == own])
            changed = True
            if enc_ev is not None:   # chiave mancante → prova a recuperarla dal backup
                asyncio.create_task(_resolve_encrypted())
            if first: continue
            if room_id == cur_id and _state == S.CHAT:
                # autoscroll: qualsiasi msg (anche i miei) nella chat aperta → a fondo
                _msg_scroll = max(0, len(_chat_lines(rs)) - H)
                _new_in_cur = True
            elif room_id != cur_id and ev.sender != own:
                rs.unread += 1; _broadcast({"t": "beep"})
    return changed or first

async def _backfill(rs) -> None:
    """Dopo un restart il sync riparte dal token salvato: niente backlog.
    Scarica gli ultimi messaggi via /messages quando si apre una chat vuota."""
    if not _matrix or len(rs.messages) >= 10: return
    try:
        resp = await _matrix.room_messages(rs.room_id, start=_matrix.next_batch,
                                           direction=nio.MessageDirection.back, limit=24)
    except Exception as e:
        print("backfill err:", e); return
    if not isinstance(resp, nio.RoomMessagesResponse): return
    out = []
    for ev in resp.chunk:            # newest → oldest
        body = None; enc = None
        if isinstance(ev, nio.RoomMessageText):
            body = ev.body
        elif isinstance(ev, nio.MegolmEvent):
            try:
                dec = _matrix.decrypt_event(ev)
                if isinstance(dec, nio.RoomMessageText): body = dec.body
                else: body = "[cifrato]"; enc = ev
            except Exception: body = "[cifrato]"; enc = ev
        if body is None: continue
        out.append([_sender(_matrix, rs.room_id, ev.sender), body, enc,
                    ev.sender == _matrix.user_id])
    if out:
        out.reverse()
        rs.messages = deque(out, maxlen=40)

async def _fix_room_names(client) -> None:
    """Coi member lazy le DM senza m.room.name escono 'Empty Room': carica i membri."""
    for rid, rs in list(_rooms.items()):
        room = client.rooms.get(rid)
        if not room: continue
        name = room.display_name or ""
        if name and name != rid and not name.startswith("Empty Room"): continue
        try: await client.joined_members(rid)
        except Exception: continue
        rs.name = room.display_name or rid
    # joined_members marca members_synced=True e accoda utenti al key query:
    # senza questo flush room_send cifrerebbe per zero device (UISI)
    await _e2ee_maintenance(client)
    _push_hud()

async def _e2ee_maintenance(client) -> None:
    """La manutenzione E2EE di nio vive solo in sync_forever: col sync manuale
    va replicata (OTK replenish, key query/claim, to-device out) o le chiavi muoiono."""
    try:
        if client.outgoing_to_device_messages: await client.send_to_device_messages()
        if client.should_upload_keys: await client.keys_upload()
        if client.should_query_keys:  await client.keys_query()
        if client.should_claim_keys:  await client.keys_claim(client.get_users_for_key_claiming())
    except Exception as e: print("e2ee maint err:", e)

async def _sync_loop(client) -> None:
    _hud_text("  syncing...", title="- Link")
    resp = await client.sync(timeout=30000, full_state=True, sync_filter=SYNC_FILTER)
    if isinstance(resp, nio.SyncError):
        print("sync init err:", resp)
        if resp.status_code == "M_UNKNOWN_TOKEN": await _drop_session()
        return
    _ingest(client, resp, first=True)
    _push_hud()
    await _e2ee_maintenance(client)
    asyncio.create_task(_fix_room_names(client))
    while True:
        try: resp = await client.sync(timeout=30000, sync_filter=SYNC_FILTER)
        except asyncio.CancelledError: return
        except Exception as e: print("sync err:", e); await asyncio.sleep(5); continue
        if isinstance(resp, nio.SyncError):
            if resp.status_code == "M_UNKNOWN_TOKEN": await _drop_session(); return
            await asyncio.sleep(5); continue
        if _ingest(client, resp, first=False):
            _push_hud(fx="type" if _new_in_cur else None)
        await _e2ee_maintenance(client)

async def _drop_session():
    """Token invalidato dal server: riprova con la password salvata, o torna al login."""
    global _matrix
    if _matrix:
        try: await _matrix.close()
        except Exception: pass
        _matrix = None
    try: c = json.load(open(CREDS_FILE))
    except Exception: c = {}
    if c.get("password"):
        print("token scaduto → relogin con password")
        asyncio.create_task(_do_login(c["homeserver"], c["user"], c["password"],
                                      security_key=c.get("security_key", "")))
        return
    try: os.unlink(CREDS_FILE)
    except OSError: pass
    _broadcast({"t": "login_error", "msg": "Sessione scaduta, accedi di nuovo"})
    _hud_text("  session expired", title="! Offline", footer="[Reopen app]")


# ── Security key / backup import ─────────────────────────────────────────────

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

async def _ssss_key(s, hs: str, uid: str, headers: dict, key_str: str):
    """(raw_key, key_id) dalla stringa utente: recovery key base58 O passphrase pbkdf2
    (Element/Matrix accettano entrambe per lo Secure Backup)."""
    async with s.get(f"{hs}/_matrix/client/v3/user/{uid}/account_data/m.secret_storage.default_key",
                     headers=headers) as r:
        if r.status != 200: raise ValueError("account senza SSSS default key")
        key_id = (await r.json()).get("key")
    if not key_id: raise ValueError("SSSS key_id vuoto")
    stripped = "".join(key_str.split())
    if len(stripped) == 48 and all(ch in _B58 for ch in stripped):
        return _decode_recovery_key(key_str), key_id
    async with s.get(f"{hs}/_matrix/client/v3/user/{uid}/account_data/m.secret_storage.key.{key_id}",
                     headers=headers) as r:
        meta = (await r.json()) if r.status == 200 else {}
    pp = meta.get("passphrase") or {}
    if pp.get("algorithm") != "m.pbkdf2":
        raise ValueError("non è una recovery key e la chiave SSSS non ha passphrase")
    raw = hashlib.pbkdf2_hmac("sha512", key_str.encode(), pp["salt"].encode(),
                              int(pp["iterations"]), int(pp.get("bits", 256)) // 8)
    return raw, key_id

def _decode_recovery_key(key_str: str) -> bytes:
    """Parse Matrix recovery key (base58 + 2-byte prefix/checksum) → 32-byte raw key."""
    B58 = _B58
    key_str = "".join(key_str.split())  # strip spaces/dashes
    n = 0
    for ch in key_str:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes(35, "big")         # 2 prefix + 32 key + 1 parity (padded to 35)
    if len(raw) > 35:
        raw = raw[-35:]
    # ignore prefix bytes and parity; extract the 32-byte key
    return raw[2:34]

def _b64(s) -> bytes:
    """b64decode tollerante: i secret Matrix sono base64 senza padding."""
    if isinstance(s, bytes): s = s.decode()
    return base64.b64decode(s + "=" * (-len(s) % 4))

def _ssss_decrypt(raw_key: bytes, secret_name: str, iv_b64: str, ciphertext_b64: str, mac_b64: str) -> bytes:
    """AES-256-CTR decrypt di un secret SSSS (spec: HKDF con info = NOME del secret)."""
    prk = hmac.new(b"\x00" * 32, raw_key, hashlib.sha256).digest()
    def _expand(info: bytes, length: int) -> bytes:
        okm, t, i = b"", b"", 1
        while len(okm) < length:
            t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
            okm += t; i += 1
        return okm[:length]
    derived  = _expand(secret_name.encode(), 64)
    aes_key  = derived[:32]
    mac_key  = derived[32:]

    iv         = _b64(iv_b64)
    ciphertext = _b64(ciphertext_b64)
    expected   = _b64(mac_b64)

    got_mac = hmac.new(mac_key, ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(got_mac, expected):
        raise ValueError("SSSS MAC mismatch — wrong security key?")

    # AES-256-CTR: iv is 16 bytes; counter starts at 0
    cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv), backend=default_backend())
    d = cipher.decryptor()
    return d.update(ciphertext) + d.finalize()

async def _import_backup_keys(client: nio.AsyncClient, security_key: str) -> None:
    """Use the Matrix recovery key to import megolm sessions from key backup."""
    import aiohttp
    try:
        token   = client.access_token
        hs      = client.homeserver
        headers = {"Authorization": f"Bearer {token}"}
        uid     = client.user_id

        async with aiohttp.ClientSession() as session:
            # 1. SSSS key (recovery key o passphrase)
            raw_key, key_id = await _ssss_key(session, hs, uid, headers, security_key)

            # 2. Backup version
            async with session.get(f"{hs}/_matrix/client/v3/room_keys/version", headers=headers) as r:
                if r.status != 200: print("[backup] no backup version"); return
                vinfo = await r.json()
                backup_version = vinfo.get("version")
                backup_tag = f"{backup_version}:{vinfo.get('etag')}:{vinfo.get('count')}"
            marker = os.path.join(STORE_PATH, f".backup_{client.device_id}")
            try:
                if open(marker).read() == backup_tag:
                    print(f"[backup] {backup_tag} gia' importato, skip"); return 0
            except OSError: pass

            # 3. Decrypt backup private key from SSSS
            async with session.get(f"{hs}/_matrix/client/v3/user/{uid}/account_data/m.megolm_backup.v1",
                                   headers=headers) as r:
                if r.status != 200: print("[backup] no megolm_backup.v1 in SSSS"); return
                secret_data = await r.json()

            enc = secret_data.get("encrypted", {}).get(key_id, {})
            if not enc: print("[backup] secret not encrypted with this key"); return
            backup_key_b64 = _ssss_decrypt(raw_key, "m.megolm_backup.v1", enc["iv"], enc["ciphertext"], enc["mac"])
            backup_priv = _b64(backup_key_b64)

            # 4. Download all backup keys
            async with session.get(f"{hs}/_matrix/client/v3/room_keys/keys?version={backup_version}",
                                   headers=headers) as r:
                if r.status != 200: print("[backup] failed to download backup keys"); return
                rooms_data = (await r.json()).get("rooms", {})

        # 5. Create PkDecryption with the specific backup private key
        from _libolm import ffi, lib
        from olm.pk import PkDecryption as _PkDec, _clear_pk_decryption
        from olm._finalize import track_for_finalization
        from olm.pk import PkMessage
        from nio.crypto.key_export import encrypt_and_save
        import tempfile

        dec = object.__new__(_PkDec)
        dec._buf = ffi.new("char[]", lib.olm_pk_decryption_size())
        dec._pk_decryption = lib.olm_pk_decryption(dec._buf)
        track_for_finalization(dec, dec._pk_decryption, _clear_pk_decryption)
        pk_len = lib.olm_pk_private_key_length()
        pk_buf = ffi.new("char[]", backup_priv[:pk_len])
        key_len = lib.olm_pk_key_length()
        key_buf = ffi.new("char[]", key_len)
        ret = lib.olm_pk_key_from_private(dec._pk_decryption, key_buf, key_len, pk_buf, pk_len)
        if ret == lib.olm_error():
            err = ffi.string(lib.olm_pk_decryption_last_error(dec._pk_decryption)).decode()
            print(f"[backup] PkDecryption init failed: {err}"); return
        dec.public_key = ffi.unpack(key_buf, key_len).decode()

        # 6. Decrypt each session
        sessions = []
        for room_id, room_data in rooms_data.items():
            for session_id, session_data in room_data.get("sessions", {}).items():
                sd = session_data.get("session_data", {})
                try:
                    msg = PkMessage(sd["ephemeral"], sd["mac"], sd["ciphertext"])
                    kd = json.loads(dec.decrypt(msg))
                    sessions.append({
                        "algorithm": "m.megolm.v1.aes-sha2",
                        "forwarding_curve25519_key_chain": kd.get("forwarding_curve25519_key_chain", []),
                        "room_id": room_id,
                        "sender_key": kd.get("sender_key", ""),
                        "sender_claimed_keys": kd.get("sender_claimed_keys", {}),
                        "session_id": session_id,
                        "session_key": kd.get("session_key", ""),
                    })
                except Exception as e:
                    print(f"[backup] skip {session_id}: {e}")

        if not sessions:
            print("[backup] no sessions to import"); return

        # 7. Write key export and import via nio
        passphrase = base64.b64encode(os.urandom(16)).decode()
        # niente pre-creazione: nio scrive atomico e collide con un file esistente
        tmpfile = os.path.join(tempfile.gettempdir(), f"g2keys-{uuid.uuid4().hex}.keys")
        try:
            encrypt_and_save(json.dumps(sessions).encode(), tmpfile, passphrase)
            # niente client.import_keys: fa 1 commit sqlite per sessione SUL loop
            # (48s bloccato → keepalive WS morti). Parse in thread, save con yield.
            from functools import partial as _partial
            _loop = asyncio.get_running_loop()
            parsed = await _loop.run_in_executor(
                None, _partial(client.olm.import_keys_static, tmpfile, passphrase))
            for _i, _sess in enumerate(parsed):
                if client.olm.inbound_group_store.add(_sess):
                    client.store.save_inbound_group_session(_sess)
                if _i % 25 == 0: await asyncio.sleep(0)
        finally:
            try: os.unlink(tmpfile)
            except OSError: pass
        try: open(marker, "w").write(backup_tag)
        except OSError: pass
        print(f"[backup] imported {len(sessions)} megolm sessions")
        return len(sessions)
    except Exception as e:
        print(f"[backup] import failed: {e}")
    return 0


async def _self_verify(client: nio.AsyncClient, security_key: str) -> bool:
    """Firma il device con la self-signing key presa da SSSS → sessione verificata
    (scudo verde) per gli altri client."""
    import aiohttp
    from olm.pk import PkSigning
    try:
        hs, uid  = client.homeserver, client.user_id
        dev      = client.device_id
        headers  = {"Authorization": f"Bearer {client.access_token}"}
        async with aiohttp.ClientSession() as s:
            raw_key, key_id = await _ssss_key(s, hs, uid, headers, security_key)
            async with s.get(f"{hs}/_matrix/client/v3/user/{uid}/account_data/m.cross_signing.self_signing",
                             headers=headers) as r:
                if r.status != 200: print("[verify] no self_signing secret in SSSS"); return False
                enc = (await r.json()).get("encrypted", {}).get(key_id, {})
            if not enc: print("[verify] secret non cifrato con questa chiave"); return False
            seed   = _b64(_ssss_decrypt(raw_key, "m.cross_signing.self_signing", enc["iv"], enc["ciphertext"], enc["mac"]))
            signer = PkSigning(seed)

            async with s.post(f"{hs}/_matrix/client/v3/keys/query", headers=headers,
                              json={"device_keys": {uid: []}}) as r:
                data = await r.json()
            ssk_pub = next(iter(data.get("self_signing_keys", {}).get(uid, {}).get("keys", {}).values()), None)
            if ssk_pub != signer.public_key:
                print(f"[verify] self-signing key mismatch ({ssk_pub} != {signer.public_key})"); return False
            dk = data.get("device_keys", {}).get(uid, {}).get(dev)
            if not dk: print(f"[verify] device keys di {dev} non trovate sul server"); return False

            signed = {k: v for k, v in dk.items() if k not in ("signatures", "unsigned")}
            canonical = json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            signed["signatures"] = {uid: {f"ed25519:{signer.public_key}": signer.sign(canonical)}}
            async with s.post(f"{hs}/_matrix/client/v3/keys/signatures/upload",
                              headers=headers, json={uid: {dev: signed}}) as r:
                res = await r.json()
                if r.status == 200 and not res.get("failures"):
                    print(f"[verify] device {dev} firmato (cross-signing)"); return True
                print("[verify] upload firma fallito:", res); return False
    except Exception as e:
        print(f"[verify] err: {e}")
        return False


def _reanchor_scroll():
    """Riporta la vista a fondo chat (dopo che il contenuto è cambiato di lunghezza)."""
    global _msg_scroll
    if _state == S.CHAT:
        rs = _cur()
        if rs: _msg_scroll = max(0, len(_chat_lines(rs)) - H)

def _redecrypt_memory() -> int:
    """Ri-decripta i [cifrato] tenuti in memoria dopo un import di chiavi. N risolti."""
    n = 0
    for rs in _rooms.values():
        for m in rs.messages:
            if len(m) > 2 and m[2] is not None:
                try:
                    dec = _matrix.decrypt_event(m[2])
                    if isinstance(dec, nio.RoomMessageText):
                        m[1] = dec.body; m[2] = None; n += 1
                except Exception: pass
    if n: _reanchor_scroll()   # il testo decifrato è più lungo → resta a fondo
    return n

async def _setup_e2ee(client: nio.AsyncClient, security_key: str) -> None:
    """Security key → verifica sessione (cross-signing) + import del key backup."""
    global _security_key
    _security_key = security_key
    verified = await _self_verify(client, security_key)
    _broadcast({"t": "verify_ok"} if verified else {"t": "verify_err"})
    imported = await _import_backup_keys(client, security_key)
    if imported and _redecrypt_memory():
        _push_hud()

async def _resolve_encrypted() -> None:
    """Un [cifrato] è comparso: la sua chiave può essere nel key backup (caricata dal
    mittente dopo il nostro import). Ri-importa il backup (throttled) e ri-decripta."""
    global _resolve_t
    now = time.monotonic()
    if now - _resolve_t < 20: return   # throttle: import backup è pesante
    _resolve_t = now
    if not (_matrix and _security_key): return
    if await _import_backup_keys(_matrix, _security_key) and _redecrypt_memory():
        _push_hud()


# ── Login / logout ────────────────────────────────────────────────────────────
def _save_creds(security_key: str = "", password: str = ""):
    if not _matrix: return
    try: old = json.load(open(CREDS_FILE))
    except Exception: old = {}
    creds = {"homeserver": _matrix.homeserver, "user": _matrix.user_id,
             "device_id": _matrix.device_id, "access_token": _matrix.access_token}
    pwd = password or old.get("password")
    key = security_key or old.get("security_key")
    if pwd: creds["password"] = pwd
    if key: creds["security_key"] = key
    try:
        with open(CREDS_FILE, "w") as f: json.dump(creds, f)
    except Exception as e: print("creds save err:", e)

async def _do_login(homeserver: str, user: str, password: str = "",
                    device_id: str = "", access_token: str = "", security_key: str = ""):
    global _matrix, _sync_task, _logging_in
    global _state, _current_room_id, _room_scroll, _msg_scroll, _pending_text, _capturing

    # sessione già viva → ack e basta; una security_key nuova fa (ri)partire la verifica
    if _matrix is not None:
        _broadcast({"t": "login_ok", "user": _matrix.user_id})
        if security_key:
            _save_creds(security_key=security_key)
            asyncio.create_task(_setup_e2ee(_matrix, security_key))
        _push_hud()
        return
    if _logging_in:  # single-flight: il login in corso farà il broadcast per tutti
        return
    _logging_in = True
    try:
        await _do_login_inner(homeserver, user, password, device_id, access_token, security_key)
    finally:
        _logging_in = False

async def _do_login_inner(homeserver: str, user: str, password: str,
                          device_id: str, access_token: str, security_key: str):
    global _matrix, _sync_task
    global _state, _current_room_id, _room_scroll, _msg_scroll, _pending_text, _capturing

    # login dal form con password: se ho gia' un token per lo stesso utente riusalo
    # (ogni password-login crea un device Matrix nuovo → churn di sessioni)
    if password and not access_token:
        try: c = json.load(open(CREDS_FILE))
        except Exception: c = {}
        same_user = (c.get("user", "").lstrip("@").split(":")[0]
                     == user.lstrip("@").split(":")[0])
        if same_user and c.get("access_token") and c.get("device_id"):
            homeserver  = c["homeserver"]
            user        = c["user"]
            device_id   = c["device_id"]
            access_token = c["access_token"]
            # password resta come fallback: _drop_session la usa se il token e' morto

    if _sync_task and not _sync_task.done():
        _sync_task.cancel()
        try: await _sync_task
        except asyncio.CancelledError: pass
    _sync_task = None
    if _matrix:
        try: await _matrix.close()
        except Exception: pass
        _matrix = None

    _rooms.clear(); _current_room_id = ""; _room_scroll = _msg_scroll = 0
    _pending_text = ""; _state = S.ROOMS; _capturing = False

    os.makedirs(STORE_PATH, exist_ok=True)
    client = nio.AsyncClient(
        homeserver, user,
        device_id=device_id or None,
        store_path=STORE_PATH,
        config=nio.AsyncClientConfig(encryption_enabled=True, store_sync_tokens=True),
    )

    if access_token:
        client.restore_login(user_id=user, device_id=device_id, access_token=access_token)
        resp = type("_OK", (), {})()  # sentinel — restore_login is sync, no LoginError
    else:
        resp = await client.login(password, device_name="Even G2")

    if isinstance(resp, nio.LoginError):
        await client.close()
        err_msg = getattr(resp, "message", str(resp))
        _broadcast({"t": "login_error", "msg": err_msg})
        _hud_text("  login failed", title="! Error")
        return

    if client.should_upload_keys:
        await client.keys_upload()

    _matrix    = client
    _sync_task = asyncio.create_task(_sync_loop(client))
    uid = client.user_id or user
    _broadcast({"t": "login_ok", "user": uid})
    print(f"login OK: {uid} @ {homeserver} dev={client.device_id}")
    _save_creds(security_key=security_key, password=password)
    if security_key:
        asyncio.create_task(_setup_e2ee(client, security_key))

async def _do_logout():
    global _matrix, _sync_task, _state, _capturing
    global _current_room_id, _room_scroll, _msg_scroll, _pending_text
    if _sync_task and not _sync_task.done():
        _sync_task.cancel()
        try: await _sync_task
        except asyncio.CancelledError: pass
    _sync_task = None
    if _matrix:
        try: await _matrix.logout()
        except Exception: pass
        try: await _matrix.close()
        except Exception: pass
        _matrix = None
    _rooms.clear(); _current_room_id = ""; _room_scroll = _msg_scroll = 0
    _pending_text = ""; _state = S.ROOMS; _capturing = False
    try: os.unlink(CREDS_FILE)
    except OSError: pass
    _broadcast({"t": "logged_out"}); print("logout OK")


# ── STT → CONFIRM ─────────────────────────────────────────────────────────────
async def _live_caption_loop():
    """Live caption durante REC: ogni ~LIVE_WIN_SEC trascrive l'accumulato e pusha
    il parziale sull'HUD. Skip-if-busy sul flag _stt_busy: se whisper e' occupato
    la finestra salta → la cadenza si dirada da sola col carico. Per non pagare
    il totale a dettature lunghe trascrive solo la coda (LIVE_TAIL_SEC): il caption
    mostra comunque solo le ultime 1-2 righe. Allo stop il loop muore da solo e un
    parziale in volo viene ignorato (_live_gen): la finale non viene mai toccata."""
    global _stt_busy, _live_text
    gen  = _live_gen
    loop = asyncio.get_running_loop()
    while _capturing and _state == S.REC and gen == _live_gen:
        await asyncio.sleep(LIVE_WIN_SEC)
        if not (_capturing and _state == S.REC and gen == _live_gen): break
        if _stt_busy or not _pcm: continue      # skip-if-busy: si riprova alla prossima
        tail = bytes(_pcm[-int(LIVE_TAIL_SEC * _BPS):])
        _stt_busy = True
        try: text = await loop.run_in_executor(None, _stt_partial, tail)
        except Exception as e: print("live stt err:", e); continue   # silenzioso
        finally: _stt_busy = False
        if not (_capturing and _state == S.REC and gen == _live_gen): break
        if text and text != _live_text:
            _live_text = text
            _push_hud()                         # max 1 push per finestra

def _stop_recording():
    """Stop registrazione → 'Transcribing...' in riga 9 e avvia la STT in un task."""
    global _capturing, _state
    _capturing = False
    _broadcast({"t": "mic_stop"})
    # fx="dots" SOLO sul frame di ingresso in PROC: l'app anima i pallini in locale
    _state = S.PROC; _push_hud(fx="dots")   # feedback immediato: non resta su REC
    asyncio.create_task(_finish_audio())

async def _finish_audio():
    global _state, _pending_text, _stt_busy, _capturing, _confirm_scroll, _confirm_t
    _capturing = False
    if _pcm:
        print(f"audio: {len(_pcm)}B ({len(_pcm)/_BPS:.1f}s)")
    if not _pcm:
        _state = S.CHAT; _push_hud(); return
    while _stt_busy: await asyncio.sleep(0.05)
    _stt_busy = True
    loop = asyncio.get_running_loop()
    try: text = await loop.run_in_executor(None, stt, bytes(_pcm))
    except Exception as e: print("stt err:", e); _state = S.CHAT; _push_hud(); return
    finally: _stt_busy = False
    if not text: _state = S.CHAT; _push_hud(); return
    _pending_text = text; _confirm_scroll = 0
    _confirm_t = time.monotonic()
    _state = S.CONFIRM; _push_hud(fx="type")   # testo trascritto rivelato a typewriter


# ── Gesture dispatcher ────────────────────────────────────────────────────────
async def _on_gesture(g: str):
    global _state, _current_room_id, _room_scroll, _msg_scroll, _pending_text
    global _capturing, _pcm, _confirm_scroll, _last_scroll_t, _live_text, _live_gen

    order = _sorted_rooms(); n = len(order)

    if _state == S.OFF:
        _state = S.ROOMS; _push_hud()   # qualsiasi gesto riaccende l'HUD

    elif _state == S.ROOMS:
        if g == "double_tap":
            _state = S.OFF; _push_hud()
        elif g in ("swipe_down", "swipe_up"):
            now = time.monotonic()
            if now - _last_scroll_t < SCROLL_DEB: return
            _last_scroll_t = now
            idx = _room_idx()
            if g == "swipe_down":
                new_idx = min(idx + 1, n - 1) if n else 0
                if idx >= _room_scroll + H - 1: _room_scroll += 1
            else:
                new_idx = max(idx - 1, 0)
                if new_idx < _room_scroll: _room_scroll = max(0, _room_scroll - 1)
            _current_room_id = order[new_idx] if order else ""
            _push_hud()
        elif g == "tap":
            rs = _cur()
            if rs:
                rs.unread = 0
                await _backfill(rs)
                if any(len(m) > 2 and m[2] is not None for m in rs.messages):
                    asyncio.create_task(_resolve_encrypted())   # recupera chiavi mancanti
                _msg_scroll = max(0, len(_chat_lines(rs)) - H)
                _state = S.CHAT; _push_hud(fx="type")

    elif _state == S.CHAT:
        if g == "double_tap":
            _state = S.ROOMS; _msg_scroll = 0; _push_hud()
        elif g in ("swipe_up", "swipe_down"):
            now = time.monotonic()
            if now - _last_scroll_t < SCROLL_DEB: return
            _last_scroll_t = now
            rs = _cur()
            if rs:
                cap   = max(0, len(_chat_lines(rs)) - H)
                _msg_scroll = max(0, _msg_scroll - 1) if g == "swipe_up" \
                              else min(_msg_scroll + 1, cap)
            _push_hud()
        elif g == "tap":
            _pcm = bytearray(); _capturing = True
            _live_text = ""; _live_gen += 1     # invalida eventuali parziali vecchi
            _state = S.REC; _push_hud(); _broadcast({"t": "mic_start"})
            asyncio.create_task(_live_caption_loop())

    elif _state == S.REC:
        if g == "tap" and _capturing:
            _stop_recording()   # → PROC ('Transcribing...') poi CONFIRM

    elif _state == S.CONFIRM:
        # tap partiti mentre girava la STT (stato ancora REC sull'HUD) arrivano qui
        # accodati: senza gate invierebbero un testo mai visto
        if time.monotonic() - _confirm_t < 0.7: return
        if g in ("swipe_up", "swipe_down"):            # scorri il testo trascritto
            # finestra dialog = 2 righe wrappate a 36 (dialog.text del payload)
            cap = max(0, len(_dialog_lines()) - 2)
            _confirm_scroll = max(0, _confirm_scroll - 1) if g == "swipe_up" \
                              else min(_confirm_scroll + 1, cap)
            _push_hud()
        elif g == "tap":                                # 1 tap = invia
            rs = _cur()
            if rs and _matrix and _pending_text:
                try:
                    await _matrix.room_send(rs.room_id, "m.room.message",
                                            {"msgtype": "m.text", "body": _pending_text},
                                            ignore_unverified_devices=True)
                except Exception as e: print("send err:", e)
            _pending_text = ""; _state = S.CHAT; _push_hud()
        elif g == "double_tap":                         # 2 tap = scarta
            _pending_text = ""; _state = S.CHAT; _push_hud()


# ── WebSocket handler ─────────────────────────────────────────────────────────
async def handler(ws):
    global _capturing
    q: asyncio.Queue = asyncio.Queue(); _clients.add(q)

    async def pump():
        while True: await ws.send(json.dumps(await q.get(), ensure_ascii=False))

    pump_task = asyncio.create_task(pump())
    if _matrix:
        q.put_nowait({"t": "login_ok", "user": _matrix.user_id})
        _push_hud()
    elif os.path.exists(CREDS_FILE):
        try:
            c = json.loads(open(CREDS_FILE).read())
            _hud_text("  connecting...", title="- Link")
            if c.get("access_token") and c.get("device_id"):
                asyncio.create_task(_do_login(
                    c["homeserver"], c["user"],
                    device_id=c["device_id"], access_token=c["access_token"],
                    security_key=c.get("security_key", "")))
            else:
                asyncio.create_task(_do_login(c["homeserver"], c["user"], c.get("password", ""),
                                              security_key=c.get("security_key", "")))
        except Exception as e: print("auto-login err:", e)
    print("G2 connessa")
    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                if _capturing:
                    _pcm.extend(msg)
                    if len(_pcm) >= REC_MAX_SEC * _BPS:   # solo tetto di sicurezza
                        _stop_recording()
                continue
            m = json.loads(msg); t = m.get("t")
            if   t == "login":  await _do_login(m.get("homeserver",""), m.get("user",""), m.get("password",""),
                                                 security_key=m.get("security_key",""))
            elif t == "login_token": await _do_login(
                    m.get("homeserver",""), m.get("user_id",""),
                    device_id=m.get("device_id",""), access_token=m.get("access_token",""),
                    security_key=m.get("security_key",""))
            elif t == "logout":  await _do_logout()
            elif t == "gesture": await _on_gesture(m.get("g",""))
            elif t == "audio_end" and _capturing:
                _capturing = False
                _broadcast({"t": "mic_stop"})
                asyncio.create_task(_finish_audio())
    except websockets.ConnectionClosed: pass
    finally: _clients.discard(q); pump_task.cancel(); print("G2 disconnessa")


# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    async with websockets.serve(handler, HOST, PORT, max_size=None):
        print(f"bridge WS su ws://{HOST}:{PORT}"); await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
