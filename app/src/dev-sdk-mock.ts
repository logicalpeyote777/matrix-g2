// Mock browser dell'SDK Even Hub — SOLO per sviluppo (attivo con VITE_MOCK=1, via
// alias in vite.config.ts). Il simulatore ufficiale e' un'app desktop nativa (serve
// GPU/display); questo harness invece fa girare l'app in un browser qualsiasi:
// disegna l'HUD (i due container) come DOM e offre bottoni gesti + casella comandi.
//
// NON e' l'SDK reale: valida layout, flusso sessioni, scroll, notifiche e protocollo
// WS. Le quirk BLE/ottiche vere restano da provare su device/sim nativo.
// L'estetica CRT e' SOLO dell'harness: sul device arriva testo puro.
//
// Espone gli stessi nomi importati da main.ts. main.ts resta invariato.

export class TextContainerProperty {
  constructor(d: Record<string, any>) { Object.assign(this, d) }
}
export class TextContainerUpgrade {
  constructor(d: Record<string, any>) { Object.assign(this, d) }
}
export class CreateStartUpPageContainer {
  constructor(d: Record<string, any>) { Object.assign(this, d) }
}
export class RebuildPageContainer {
  constructor(d: Record<string, any>) { Object.assign(this, d) }
}

export const AudioInputSource = { Glasses: 1, Phone: 2 } as const
export const OsEventTypeList = {
  CLICK_EVENT: 0, SCROLL_TOP_EVENT: 1, SCROLL_BOTTOM_EVENT: 2, DOUBLE_CLICK_EVENT: 3,
  FOREGROUND_ENTER_EVENT: 4, FOREGROUND_EXIT_EVENT: 5, IMU_DATA_REPORT: 8,
} as const

const HUD_W = 576, HUD_H = 288

const CSS = `
  :root { --ph: #39ff70; --ph-dim: #1d7a3c; --ph-faint: #103d20; --bg: #030503 }
  * { box-sizing: border-box }
  body { margin: 0; background: var(--bg); color: var(--ph);
         font-family: 'IBM Plex Mono', ui-monospace, Menlo, Consolas, monospace;
         min-height: 100vh;
         background-image: radial-gradient(ellipse at 50% -20%, #0a1f0e 0%, var(--bg) 60%) }
  .wrap { display: flex; flex-direction: column; align-items: center; gap: 18px; padding: 28px 16px }
  .hdr { font-size: 12px; letter-spacing: .35em; color: var(--ph-dim); text-transform: uppercase;
         text-shadow: 0 0 8px #39ff7040 }
  .hdr b { color: var(--ph); font-weight: 400 }
  .bezel { padding: 14px; border: 1px solid var(--ph-faint); border-radius: 14px;
           background: #060a06; box-shadow: 0 0 60px #39ff7014, inset 0 0 20px #000 }
  .bezel-label { display: flex; justify-content: space-between; font-size: 10px;
                 letter-spacing: .25em; color: var(--ph-dim); padding: 0 2px 8px }
  /* HUD fedele: testo verde su nero, niente effetti — il device non ne ha.
     La resa VERA (font/ottica) si vede solo su simulatore nativo o device. */
  .crt { position: relative; width: ${HUD_W}px; height: ${HUD_H}px; background: #000;
         border-radius: 8px; overflow: hidden; border: 1px solid var(--ph-faint) }
  .crt .cont { position: absolute; white-space: pre-wrap; line-height: 1.2;
               text-shadow: 0 0 4px #39ff7066 }
  .crt .notif { border: 1px solid var(--ph-dim); border-radius: 3px }
  .row { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center }
  button { font: inherit; font-size: 13px; letter-spacing: .15em; text-transform: uppercase;
           color: var(--ph); background: transparent; border: 1px solid var(--ph-dim);
           border-radius: 3px; padding: 8px 16px; cursor: pointer;
           text-shadow: 0 0 6px #39ff7066; transition: all .12s }
  button:hover { background: var(--ph); color: #000; text-shadow: none;
                 box-shadow: 0 0 18px #39ff7088 }
  button:active { transform: translateY(1px) }
  form { display: flex; align-items: center; gap: 10px; width: ${HUD_W + 32}px; max-width: 100%;
         border: 1px solid var(--ph-faint); border-radius: 3px; padding: 4px 4px 4px 12px;
         background: #000c }
  .ps1 { color: var(--ph); text-shadow: 0 0 8px #39ff7088 }
  input { flex: 1; font: inherit; font-size: 14px; color: var(--ph); background: transparent;
          border: 0; outline: 0; caret-color: var(--ph); padding: 8px 0 }
  input::placeholder { color: var(--ph-dim); opacity: .7 }
`

export async function waitForEvenAppBridge(): Promise<any> {
  const containers = new Map<number, HTMLDivElement>()
  let onEvent: ((e: any) => void) | null = null

  // --- scena: bezel "occhiali" stile CRT + controlli ---
  const style = document.createElement('style')
  style.textContent = CSS
  document.head.appendChild(style)

  const root = document.createElement('div')
  root.className = 'wrap'
  root.innerHTML = `
    <div class="hdr"><b>eveng2</b> // harness browser (mock sdk) — non è il device reale</div>`

  const bezel = document.createElement('div')
  bezel.className = 'bezel'
  bezel.innerHTML = `<div class="bezel-label"><span>even g2 · hud</span><span>${HUD_W}×${HUD_H}</span></div>`
  const hud = document.createElement('div')
  hud.className = 'crt'
  bezel.appendChild(hud)
  root.appendChild(bezel)

  const mkBtn = (label: string, fn: () => void) => {
    const b = document.createElement('button')
    b.textContent = label
    b.onclick = fn
    return b
  }
  const fire = (e: any) => onEvent?.(e)
  const controls = document.createElement('div')
  controls.className = 'row'
  controls.append(
    mkBtn('tap · parla', () => fire({ sysEvent: { eventType: 0 } })),
    mkBtn('double-tap', () => fire({ sysEvent: { eventType: 3 } })),
    mkBtn('▲ swipe', () => fire({ textEvent: { eventType: 1 } })),
    mkBtn('▼ swipe', () => fire({ textEvent: { eventType: 2 } })),
  )
  root.appendChild(controls)

  // casella comandi: al posto del mic, inietta {"t":"text"} nel WS dell'app
  const form = document.createElement('form')
  const ps1 = document.createElement('span')
  ps1.className = 'ps1'
  ps1.textContent = '❯'
  const input = document.createElement('input')
  input.placeholder = 'parla con la sessione attiva — o: "nuova sessione" · "sessione due" · "stato" · "vai" · "pausa" · "scrolla giù" · "fermati"'
  form.append(ps1, input, mkBtn('invia', () => {}))
  form.onsubmit = (ev) => {
    ev.preventDefault()
    const t = input.value.trim()
    if (t && (window as any).__eveng2?.sendText) { (window as any).__eveng2.sendText(t); input.value = '' }
  }
  root.appendChild(form)
  // NON svuotare il body: main.ts tocca i nodi del phone UI (#verify-btn, #conn-user…)
  // sui messaggi login_* del bridge → rimuoverli fa crashare handleWsMsg. Nascondili.
  for (const c of Array.from(document.body.children)) (c as HTMLElement).style.display = 'none'
  document.body.appendChild(root)

  const setText = (el: HTMLDivElement, _cid: number, text: string) => {
    el.textContent = text
    if (el.classList.contains('notif')) el.style.visibility = text ? 'visible' : 'hidden'
  }

  const render = (p: any) => {
    let el = containers.get(p.containerID)
    if (!el) {
      el = document.createElement('div')
      el.className = 'cont'
      hud.appendChild(el)
      containers.set(p.containerID, el)
    }
    el.style.left = `${p.xPosition ?? 0}px`
    el.style.top = `${p.yPosition ?? 0}px`
    el.style.width = `${p.width ?? HUD_W}px`
    el.style.height = `${p.height ?? HUD_H}px`
    el.style.padding = `${p.paddingLength ?? 0}px`
    // font agganciato alla griglia dell'app: COLS=44 char per riga (main.ts) devono
    // riempire la larghezza del container → vedi ESATTAMENTE dove wrappa e quante righe entrano
    const charW = ((p.width ?? HUD_W) - 2 * (p.paddingLength ?? 0)) / 44
    el.style.fontSize = (p.height ?? HUD_H) < 100 ? '14px' : `${Math.floor(charW / 0.602)}px`
    if (p.borderWidth) el.classList.add('notif')
    setText(el, p.containerID, p.content ?? '')
  }

  return {
    createStartUpPageContainer: async (cfg: any) => { (cfg.textObject ?? []).forEach(render); return true },
    rebuildPageContainer:       async (cfg: any) => { (cfg.textObject ?? []).forEach(render); return true },
    textContainerUpgrade: async (upg: any) => {
      const el = containers.get(upg.containerID)
      if (el) setText(el, upg.containerID, upg.content ?? '')
      return true
    },
    audioControl: async (_open: boolean, _src?: any) => true,   // niente mic reale: no-op
    onEvenHubEvent: (cb: (e: any) => void) => { onEvent = cb },
  }
}
