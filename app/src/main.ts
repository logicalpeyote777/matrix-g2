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

// ── HUD hacker: geometria + barre ASCII (solo sopra/sotto, niente bordi laterali) ─
// Frame ASCII: i box-drawing Unicode (═║╔) sul G2 renderizzano più larghi del testo.
// 44 col = larghezza che il G2 non manda a capo. Se serve, taralo in coppia con W.
// Dimensione che il G2 mostra correttamente. NB: il font NON è ridimensionabile via
// SDK — rimpicciolire il container non rimpicciolisce il testo, lo taglia soltanto.
const HUD_W = 576, HUD_H = 288, BORDER = 3   // container occhiali (px)
const COLS = 44, ROWS = 9                    // barre e altezza (caratteri)
const IW = COLS, IH = ROWS - 2               // contenuto: piena larghezza, deve = W/H nel bridge
const HUD_TITLE = 'MATRIX//G2'

function bar(label: string): string {
  const inner = COLS - 2
  let seg = label ? `=[ ${label} ]` : ''
  if (seg.length > inner) seg = seg.slice(0, inner)
  seg += '='.repeat(Math.max(0, inner - seg.length))
  return '+' + seg + '+'
}

// barra sopra (title) + contenuto a piena larghezza + barra sotto (footer).
// Ricostruito a ogni frame → le barre restano integre anche a metà typewriter.
function renderFrame(title: string, lines: string[], footer: string): string {
  const rows: string[] = []
  for (let i = 0; i < IH; i++) {
    let c = lines[i] ?? ''
    if (c.length > IW) c = c.slice(0, IW)
    rows.push(c.padEnd(IW))
  }
  return [bar(title), ...rows, bar(footer)].join('\n')
}

let hudText = renderFrame(HUD_TITLE, ['', '  > boot…'], 'INIT')
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

// fx==='type' → svela il contenuto a scaglioni (stile terminale), sempre dentro
// il frame; a scatti fissi (6) per non intasare il BLE. Altrimenti render istantaneo.
function setHudStructured(title: string, lines: string[], footer: string, fx?: string) {
  stopType()
  if (!title && lines.length === 0 && !footer) { pushContainer(''); return }   // HUD off
  if (fx !== 'type') { pushContainer(renderFrame(title, lines, footer)); return }
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
    pushContainer(renderFrame(title, partial, footer))
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
    // ponytail: no containerID filter — tap/double_tap may arrive with id≠1 or via sysEvent
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
  vb.textContent = 'Verifica e continua →'
}

// ── bridge WebSocket ──────────────────────────────────────────────────────────

function handleWsMsg(m: any) {
  if (m.t === 'hud') {
    // payload strutturato {title,lines,footer,fx}; fallback su text per compat
    const title  = m.title ?? HUD_TITLE
    const lines  = m.lines ?? (m.text ? String(m.text).split('\n') : [])
    const footer = m.footer ?? ''
    setHudStructured(title, lines, footer, m.fx)
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
    el('verify-status').textContent = '🔐 Sessione verificata'
  } else if (m.t === 'verify_err') {
    el('verify-status').textContent = '⚠️ Verifica fallita — rifai login con la security key'
  }
}

function openBridge() {
  if (ws) { ws.onclose = null; ws.onerror = null; ws.close() }  // niente handler fantasma dal socket vecchio
  ws = new WebSocket(BRIDGE_URL)
  ws.onmessage = ev => handleWsMsg(JSON.parse(ev.data))
  ws.onerror = () => {
    if (autoT) { clearTimeout(autoT); autoT = null }
    resetSeckeyBtns()
    loginErr('Bridge non raggiungibile')
    show('login')
  }
  // il WebView può buttare giù il WS in background: riaggancia da solo
  ws.onclose = () => { setTimeout(openBridge, 3000) }
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
    show('login')   // nessun login_ok dal bridge → serve accesso manuale
  }, 15000)
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY) ?? '{}')
    if (saved.homeserver) el<HTMLInputElement>('homeserver').value = saved.homeserver
    if (saved.user)       el<HTMLInputElement>('username').value   = saved.user
  } catch { /* prefill best-effort */ }
}

// ── login form ────────────────────────────────────────────────────────────────

el('login-btn').addEventListener('click', () => {
  if (autoT) { clearTimeout(autoT); autoT = null }   // il timer auto-login non deve strappare la seckey
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
  vb.textContent = 'Accesso…'
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
