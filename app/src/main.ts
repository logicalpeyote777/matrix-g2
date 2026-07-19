import {
  AudioInputSource,
  CreateStartUpPageContainer,
  OsEventTypeList,
  RebuildPageContainer,
  TextContainerProperty,
  waitForEvenAppBridge,
} from '@evenrealities/even_hub_sdk'

const BRIDGE_URL: string =
  (import.meta as any).env?.VITE_BRIDGE_URL ??
  `wss://${location.hostname}/ws/matrix-g2`

// solo prefill (homeserver + user), niente segreti: la sessione la tiene il bridge
const LS_KEY = 'matrix_g2_login'

type Screen = 'login' | 'seckey' | 'connected'

let ws: WebSocket | null = null
let evenBridge: any      = null
let autoT: ReturnType<typeof setTimeout> | null = null
let pendingLogin: { homeserver: string, user: string, password: string } | null = null
let recording            = false

// ── HUD: container unico 44×9, chrome stile Terminal Mode ────────────────────
// righe 1-7 = contenuto (lines dal bridge), riga 8 = separatore "-"×44,
// riga 9 = status line: title (sx) + footer (dx, right-aligned a col 44).
// NB: il font è FISSO dal firmware — rimpicciolire il container CLIPPA il testo.
const HUD_W = 576, HUD_H = 288, BORDER = 3   // container occhiali (px)
const COLS = 44, ROWS = 9                    // griglia caratteri
const IW = COLS, IH = ROWS - 2               // contenuto: 7 righe full-width

const SEP = '-'.repeat(COLS)

type Dialog = { text: string[], opts: string[] }

// stato dell'ultimo frame (serve al redraw locale dell'animazione dots)
let curTitle  = ''
let curLines: string[] = []
let curFooter = ''
let curDialog: Dialog | null = null

// riga 9: title + spazi + footer right-aligned; se non ci sta, tronca title secco
function statusLine(title: string, footer: string): string {
  let t = title
  const max = COLS - footer.length - (footer ? 1 : 0)
  if (t.length > max) t = t.slice(0, Math.max(0, max))
  return t + ' '.repeat(Math.max(0, COLS - t.length - footer.length)) + footer
}

function clip(s: string): string {
  return (s.length > IW ? s.slice(0, IW) : s).padEnd(IW)
}

// CONFIRM: box dialog ASCII sulle righe 2-8 (7 righe), disegnato dall'app
function dialogRows(d: Dialog): string[] {
  const hb    = '+' + '-'.repeat(42) + '+'
  const isep  = '|' + '-'.repeat(42) + '|'
  const inner = (s: string) => '| ' + s.slice(0, 40).padEnd(40) + ' |'
  const text  = d.text ?? []
  const opts  = d.opts ?? ['Send', 'Cancel']
  return [
    hb,
    inner('.|. ' + (text[0] ?? '')),
    inner('    ' + (text[1] ?? '')),
    isep,
    inner('> ' + (opts[0] ?? '')),
    inner('  ' + (opts[1] ?? '')),
    hb,
  ]
}

// frame completo 9×44: contenuto (o riga contesto + box dialog) + separatore + status
function renderFrame(title: string, lines: string[], footer: string, dlg: Dialog | null): string {
  if (dlg) {
    // dialog: riga 1 contesto + box righe 2-8 + status riga 9 = 9 righe esatte.
    // NIENTE SEP qui: aggiungerlo fa 10 righe e la status line viene clippata.
    return [clip(lines[0] ?? ''), ...dialogRows(dlg), statusLine(title, footer)].join('\n')
  }
  const rows: string[] = []
  for (let i = 0; i < IH; i++) rows.push(clip(lines[i] ?? ''))
  return [...rows, SEP, statusLine(title, footer)].join('\n')
}

let hudText = renderFrame('- Link', ['  boot...'], '', null)
let hudTimer: ReturnType<typeof setInterval> | null = null

function pushContainer(text: string) {
  hudText = text
  if (!evenBridge) return
  // createStartUpPageContainer vale SOLO all'avvio (doc SDK): gli update passano
  // da rebuildPageContainer, sennò il device ignora e l'HUD resta congelato
  evenBridge.rebuildPageContainer(new RebuildPageContainer({
    containerTotalNum: 1,
    textObject: [new TextContainerProperty({
      xPosition: 0, yPosition: 0, width: HUD_W, height: HUD_H,
      borderWidth: BORDER,
      containerID: 1, containerName: 'hud',
      content: text,
      isEventCapture: 1,
    })],
  })).catch(() => {})
}

function stopType() { if (hudTimer) { clearInterval(hudTimer); hudTimer = null } }

// ── animazione dots (fx="dots"): redraw LOCALE della sola riga 9 ─────────────
// Il bridge manda UN frame con title=".   Label..."; l'app cicla lo slot prefisso
// (4 char, col 1-4): ".   " → "..  " → "... ". 500ms (vincolo >= 400ms), si ferma
// al primo payload successivo senza fx dots, a WS close, o dopo 60s (safety cap).
const DOT_FRAMES = ['.   ', '..  ', '... ']
let dotsTimer: ReturnType<typeof setInterval> | null = null
let dotsPhase = 0
let dotsStart = 0

function stopDots() { if (dotsTimer) { clearInterval(dotsTimer); dotsTimer = null } }

function startDots() {
  stopDots()
  dotsPhase = 0
  dotsStart = Date.now()
  dotsTimer = setInterval(() => {
    if (Date.now() - dotsStart > 60000) { stopDots(); return }   // safety cap
    dotsPhase = (dotsPhase + 1) % DOT_FRAMES.length
    const animTitle = DOT_FRAMES[dotsPhase] + curTitle.slice(4)
    pushContainer(renderFrame(animTitle, curLines, curFooter, curDialog))
  }, 500)
}

// fx==='type' → svela il contenuto a scaglioni dentro il frame, 4 step/60ms.
// fx==='dots' → frame subito + animazione locale riga 9. Altrimenti render secco.
function setHudStructured(title: string, lines: string[], footer: string, fx?: string, dlg?: Dialog | null) {
  stopType()
  stopDots()
  curTitle = title; curLines = lines; curFooter = footer; curDialog = dlg ?? null
  if (!title && !footer && lines.every(l => !l) && !curDialog) { pushContainer(''); return }   // HUD off
  if (fx === 'dots') {
    pushContainer(renderFrame(title, lines, footer, curDialog))
    startDots()
    return
  }
  if (fx !== 'type') { pushContainer(renderFrame(title, lines, footer, curDialog)); return }
  const total = lines.reduce((a, l) => a + l.length, 0) || 1
  const steps = 4   // pochi passi: meno write BLE → meno gesti persi durante l'anim
  let s = 0
  const tick = () => {
    s++
    const budget = Math.ceil((total * s) / steps)
    const partial: string[] = []
    let used = 0
    for (const l of lines) {
      if (used >= budget) { partial.push(''); continue }
      const take = Math.min(l.length, budget - used)
      partial.push(l.slice(0, take)); used += take
    }
    pushContainer(renderFrame(title, partial, footer, curDialog))
    if (s >= steps) stopType()
  }
  tick()
  hudTimer = setInterval(tick, 60)
}

// ── gesture (usato anche da setupHud, definito per hoist) ────────────────────

function sendGesture(g: string) {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'gesture', g }))
}

// ── HUD occhiali ──────────────────────────────────────────────────────────────

async function setupHud() {
  try {
    const bridge = await waitForEvenAppBridge()

    await bridge.createStartUpPageContainer(new CreateStartUpPageContainer({
      containerTotalNum: 1,
      textObject: [new TextContainerProperty({
        xPosition: 0, yPosition: 0, width: HUD_W, height: HUD_H,
        borderWidth: BORDER,
        containerID: 1, containerName: 'hud',
        content: hudText,
        isEventCapture: 1,
      })],
    }))

    // replay AFTER create: eventuali frame arrivati durante l'await hanno solo
    // aggiornato hudText, ora spingilo davvero al device
    evenBridge = bridge
    pushContainer(hudText)

    // Gesti fisici occhiali → bridge WS
    bridge.onEvenHubEvent((ev: any) => {
      // PCM del mic (mentre registriamo) → stream binario al bridge
      const pcm = ev?.audioEvent?.audioPcm
      if (pcm) {
        if (recording && ws?.readyState === WebSocket.OPEN) ws.send(pcm)
        return
      }
      // Robustezza: NON fondere textEvent/sysEvent con ?? (prendeva il campo
      // sbagliato → tap persi). Click/double arrivano da sysEvent, scroll da
      // textEvent; controllo entrambi e prendo il primo che mappa un gesto.
      // proto3 omette i campi a valore default: eventType assente = CLICK(0).
      const MAP: Record<number, string> = {
        [OsEventTypeList.CLICK_EVENT]:         'tap',
        [OsEventTypeList.DOUBLE_CLICK_EVENT]:  'double_tap',
        [OsEventTypeList.SCROLL_TOP_EVENT]:    'swipe_up',
        [OsEventTypeList.SCROLL_BOTTOM_EVENT]: 'swipe_down',
      }
      const toGesture = (f: any): string | undefined => {
        if (!f) return undefined
        const t = OsEventTypeList.fromJson(f.eventType) ?? OsEventTypeList.CLICK_EVENT
        return MAP[t]
      }
      const g = toGesture(ev?.sysEvent) ?? toGesture(ev?.textEvent)
      if (g) sendGesture(g)
    })
  } catch { /* non in contesto occhiali */ }
}

// ── helpers UI ────────────────────────────────────────────────────────────────

function el<T extends HTMLElement>(id: string): T {
  return document.getElementById(id) as T
}

function show(s: Screen) {
  (['login', 'seckey', 'connected'] as Screen[]).forEach(
    id => el(id).classList.toggle('hidden', id !== s)
  )
}

function loginErr(msg: string) {
  const e = el('login-err')
  e.textContent = msg
  e.classList.toggle('hidden', !msg)
}

function normalizeHS(raw: string): string {
  const s = raw.trim().replace(/\/$/, '')
  if (!s) return ''
  return s.startsWith('http') ? s : `https://${s}`
}

function resetSeckeyBtns() {
  const vb = el<HTMLButtonElement>('verify-btn'), sb = el<HTMLButtonElement>('skip-btn')
  vb.disabled = sb.disabled = false
  vb.textContent = 'Verifica e continua'
}

// ── bridge WebSocket ──────────────────────────────────────────────────────────

function handleWsMsg(m: any) {
  if (m.t === 'hud') {
    // payload {title,lines,footer,fx,dialog}; fallback su text per compat
    const title  = m.title ?? ''
    const lines  = m.lines ?? (m.text ? String(m.text).split('\n') : [])
    const footer = m.footer ?? ''
    setHudStructured(title, lines, footer, m.fx, m.dialog ?? null)
  } else if (m.t === 'login_ok') {
    if (autoT) { clearTimeout(autoT); autoT = null }
    pendingLogin = null
    resetSeckeyBtns()
    loginErr('')
    el('conn-user').textContent = m.user
    show('connected')
  } else if (m.t === 'login_error') {
    if (autoT) { clearTimeout(autoT); autoT = null }
    pendingLogin = null
    resetSeckeyBtns()
    loginErr(m.msg ?? 'Sessione scaduta, accedi di nuovo')
    show('login')
  } else if (m.t === 'logged_out') {
    show('login')
  } else if (m.t === 'mic_start') {
    if (!recording && evenBridge) {
      recording = true
      evenBridge.audioControl(true, AudioInputSource.Glasses).catch(() => { recording = false })
    }
  } else if (m.t === 'mic_stop') {
    if (recording) {
      recording = false
      evenBridge?.audioControl(false).catch(() => {})
    }
  } else if (m.t === 'verify_ok') {
    el('verify-status').textContent = 'Sessione verificata'
  } else if (m.t === 'verify_err') {
    el('verify-status').textContent = 'Verifica fallita — rifai login con la security key'
  }
}

function openBridge() {
  if (ws) { ws.onclose = null; ws.onerror = null; ws.close() }
  ws = new WebSocket(BRIDGE_URL)
  ws.onmessage = ev => handleWsMsg(JSON.parse(ev.data))
  ws.onerror = () => {
    if (autoT) { clearTimeout(autoT); autoT = null }
    resetSeckeyBtns()
    loginErr('Bridge non raggiungibile')
    show('login')
  }
  ws.onclose = () => { stopDots(); setTimeout(openBridge, 3000) }
}

function wsSend(obj: any) {
  if (ws?.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); return }
  openBridge()
  ws!.addEventListener('open', () => ws!.send(JSON.stringify(obj)), { once: true })
}

// ── auto-login: la sessione vive nel bridge, basta connettersi ───────────────

function tryAutoLogin() {
  openBridge()
  autoT = setTimeout(() => {
    autoT = null
    show('login')
  }, 15000)
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY) ?? '{}')
    if (saved.homeserver) el<HTMLInputElement>('homeserver').value = saved.homeserver
    if (saved.user)       el<HTMLInputElement>('username').value   = saved.user
  } catch { /* prefill best-effort */ }
}

// ── login form ────────────────────────────────────────────────────────────────

el('login-btn').addEventListener('click', () => {
  if (autoT) { clearTimeout(autoT); autoT = null }
  loginErr('')
  const hs  = normalizeHS(el<HTMLInputElement>('homeserver').value)
  const usr = el<HTMLInputElement>('username').value.trim()
  const pwd = el<HTMLInputElement>('password').value
  if (!hs || !usr || !pwd) { loginErr('Compila tutti i campi'); return }
  pendingLogin = { homeserver: hs, user: usr, password: pwd }
  localStorage.setItem(LS_KEY, JSON.stringify({ homeserver: hs, user: usr }))
  show('seckey')
})

// ── security key → login via bridge ──────────────────────────────────────────

function finalize(key?: string) {
  if (!pendingLogin) return
  const vb = el<HTMLButtonElement>('verify-btn'), sb = el<HTMLButtonElement>('skip-btn')
  vb.disabled = sb.disabled = true
  vb.textContent = 'Accesso...'
  wsSend({
    t: 'login',
    homeserver: pendingLogin.homeserver,
    user: pendingLogin.user,
    password: pendingLogin.password,
    ...(key ? { security_key: key } : {}),
  })
}

el('verify-btn').addEventListener('click', () => {
  finalize(el<HTMLInputElement>('security-key').value.trim() || undefined)
})
el('skip-btn').addEventListener('click', () => finalize())

// ── logout ────────────────────────────────────────────────────────────────────

el('logout-btn').addEventListener('click', () => {
  wsSend({ t: 'logout' })
  el<HTMLInputElement>('password').value = ''
  el('verify-status').textContent = ''
  loginErr('')
  show('login')
})

// ── phone touch fallback (per uso senza occhiali) ────────────────────────────

let _touchY = 0, _lastTap = 0
let _tapTimer: ReturnType<typeof setTimeout> | null = null

el('connected').addEventListener('touchstart', e => {
  if ((e.target as HTMLElement).closest('button')) return
  _touchY = e.touches[0].clientY
}, { passive: true })

el('connected').addEventListener('touchend', e => {
  if ((e.target as HTMLElement).closest('button')) return
  const dy = e.changedTouches[0].clientY - _touchY
  const now = Date.now()
  if (Math.abs(dy) > 30) { sendGesture(dy > 0 ? 'swipe_down' : 'swipe_up'); return }
  if (_tapTimer && now - _lastTap < 350) {
    clearTimeout(_tapTimer); _tapTimer = null
    sendGesture('double_tap'); _lastTap = 0; return
  }
  _lastTap = now
  _tapTimer = setTimeout(() => { _tapTimer = null; sendGesture('tap') }, 350)
}, { passive: true })

// ── boot ──────────────────────────────────────────────────────────────────────

setupHud()
tryAutoLogin()
